#!/usr/bin/env python3
"""free-llm-hub 统一网关：一个 Key 调用所有可用免费模型，失败自动切下一个。

启动：
    set GATEWAY_KEY=sk-hub-xxx   (PowerShell: $env:GATEWAY_KEY="sk-hub-xxx")
    python scripts/gateway.py --port 8080

对外接口（OpenAI 兼容）：
    GET  /v1/models
    POST /v1/chat/completions   （支持 stream=true）
    GET  /healthz

模型名写法：
    auto                       自动选当前分数最高、延迟最低的可用模型
    agnes/agnes-3.0-flash      指定厂商/模型
    agnes-3.0-flash            只写模型名，网关在所有厂商里找
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse

import common

CONFIG = common.load_config()
GW = CONFIG.get("gateway") or {}
COOLDOWN_S = int(GW.get("cooldown_s", 180))
MAX_ATTEMPTS = int(GW.get("max_attempts", 4))
TIMEOUT_S = float(GW.get("request_timeout_s", 90))
STATE_TTL = float(GW.get("state_refresh_s", 600))

_state_cache: dict[str, Any] = {"data": None, "at": 0.0, "source": ""}
_cooldown: dict[tuple[str, str], float] = {}

app = FastAPI(title="free-llm-hub gateway", version="1.0")


# --------------------------------------------------------------------------
# 状态
# --------------------------------------------------------------------------
def _state_path() -> Path:
    return Path(os.getenv("STATE_FILE") or (common.OUTPUT_DIR / "models.json"))


def load_state(force: bool = False) -> dict[str, Any]:
    now = time.time()
    if not force and _state_cache["data"] and now - _state_cache["at"] < STATE_TTL:
        return _state_cache["data"]

    data: dict[str, Any] | None = None
    source = ""

    url = (os.getenv("STATE_URL") or GW.get("state_url") or "").strip()
    if url:
        try:
            with httpx.Client(timeout=10, follow_redirects=True) as client:
                resp = client.get(url)
            if resp.status_code == 200:
                data = resp.json()
                source = url
        except (httpx.HTTPError, ValueError) as exc:
            print(f"[state] 拉取远端状态失败（{url}）：{type(exc).__name__}: {exc}", flush=True)

    if data is None:
        path = _state_path()
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                source = str(path)
            except ValueError as exc:
                print(f"[state] 本地状态文件解析失败（{path}）：{exc}", flush=True)

    if data is None:
        data = {"models": [], "providers": [], "stats": {}}
        source = "empty"
        print("[state] 没有可用状态，请先跑 scripts/probe.py", flush=True)

    _state_cache.update({"data": data, "at": now, "source": source})
    return data


# --------------------------------------------------------------------------
# 鉴权
# --------------------------------------------------------------------------
def gateway_keys() -> list[str]:
    raw = os.getenv(GW.get("key_env", "GATEWAY_KEY"), "") or ""
    return [k.strip() for k in raw.split(",") if k.strip()]


def check_auth(authorization: str) -> bool:
    keys = gateway_keys()
    if not keys:
        return True  # 没配 Key 时不做鉴权（仅建议本地调试）
    token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else authorization.strip()
    return token in keys


# --------------------------------------------------------------------------
# 选路
# --------------------------------------------------------------------------
def _is_cooled(provider: str, upstream: str) -> bool:
    until = _cooldown.get((provider, upstream))
    if not until:
        return False
    if until <= time.time():
        _cooldown.pop((provider, upstream), None)
        return False
    return True


def mark_failure(model: dict[str, Any], status: str) -> None:
    key = (model.get("provider", ""), model.get("upstream", ""))
    ttl = COOLDOWN_S if status in (common.STATUS_RATE, common.STATUS_TIMEOUT, common.STATUS_UPSTREAM) else COOLDOWN_S * 3
    _cooldown[key] = time.time() + ttl


def provider_ready(provider_id: str) -> bool:
    conf = provider_conf(provider_id)
    return bool(conf) and bool(common.provider_key(conf))


def candidates(requested: str, state: dict[str, Any]) -> list[dict[str, Any]]:
    pools = [
        m for m in (state.get("models") or [])
        if m.get("status") in common.LIVE_STATUSES
        and provider_ready(m.get("provider", ""))
        and not _is_cooled(m.get("provider", ""), m.get("upstream", ""))
    ]
    pools.sort(key=lambda m: (-float(m.get("score") or 0), m.get("latency_ms") or 10 ** 9))

    name = (requested or "auto").strip()
    if name in ("auto", "*", ""):
        return pools[:MAX_ATTEMPTS]

    exact = [m for m in pools if m.get("id") == name or f"{m.get('provider')}/{m.get('upstream')}" == name]
    if exact:
        rest = [m for m in pools if m not in exact]
        return (exact + rest)[:MAX_ATTEMPTS]

    bare = [m for m in pools if m.get("upstream") == name or str(m.get("id", "")).endswith(f"/{name}")]
    rest = [m for m in pools if m not in bare]
    return (bare + rest)[:MAX_ATTEMPTS]


def provider_conf(provider_id: str) -> dict[str, Any]:
    return (CONFIG.get("providers") or {}).get(provider_id) or {}


def build_payload(body: dict[str, Any], model: dict[str, Any]) -> dict[str, Any]:
    payload = dict(body)
    payload["model"] = model.get("upstream")
    if provider_conf(model.get("provider", "")).get("strip_stream_options"):
        payload.pop("stream_options", None)
    return payload


def target_url(provider_id: str) -> str:
    conf = provider_conf(provider_id)
    return common.join_url(common.provider_base(conf), "chat/completions")


def headers_for(provider_id: str) -> dict[str, str]:
    conf = provider_conf(provider_id)
    headers = common.build_headers(conf)
    headers["Accept-Encoding"] = "identity"
    return headers


# --------------------------------------------------------------------------
# 路由
# --------------------------------------------------------------------------
@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    state = load_state()
    stats = state.get("stats") or {}
    return {
        "ok": bool(state.get("models")),
        "state_source": _state_cache.get("source"),
        "state_age_s": round(time.time() - float(_state_cache.get("at") or 0), 1),
        "live_models": stats.get("models_live", len(state.get("models") or [])),
        "providers_up": stats.get("providers_up"),
        "generated_at": state.get("generated_at_bj"),
        "cooldowns": len(_cooldown),
    }


@app.get("/v1/models")
async def list_models(authorization: str = Header(default="")) -> Any:
    if not check_auth(authorization):
        return JSONResponse({"error": {"message": "invalid api key"}}, status_code=401)
    state = load_state()
    data = [
        {
            "id": "auto",
            "object": "model",
            "created": 0,
            "owned_by": "free-llm-hub",
            "provider": "router",
            "latency_ms": None,
            "score": 1.0,
        }
    ]
    for m in state.get("models") or []:
        data.append(
            {
                "id": m.get("id"),
                "object": "model",
                "created": 0,
                "owned_by": m.get("provider"),
                "provider": m.get("provider"),
                "latency_ms": m.get("latency_ms"),
                "score": m.get("score"),
            }
        )
    return {"object": "list", "data": data}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request, authorization: str = Header(default="")) -> Any:
    if not check_auth(authorization):
        return JSONResponse({"error": {"message": "invalid api key"}}, status_code=401)

    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": {"message": "body must be json"}}, status_code=400)
    if not isinstance(body, dict) or not body.get("messages"):
        return JSONResponse({"error": {"message": "messages is required"}}, status_code=400)

    requested = str(body.get("model") or "auto")
    state = load_state()
    tries = candidates(requested, state)
    if not tries:
        missing = [
            pid for pid, conf in (CONFIG.get("providers") or {}).items()
            if not common.provider_key(conf)
        ]
        return JSONResponse(
            {
                "error": {
                    "message": (
                        "no live model available: "
                        f"{len(missing)} 家上游未配 Key"
                        + (f"（{', '.join(missing)}）" if missing else "")
                        + f"，{len(_cooldown)} 个模型在冷却中；请检查环境变量或先跑 scripts/probe.py"
                    ),
                    "type": "no_upstream",
                    "providers_without_key": missing,
                }
            },
            status_code=503,
        )

    is_stream = bool(body.get("stream"))
    if is_stream:
        return StreamingResponse(_stream(tries, body), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache"})
    return await _forward(tries, body)


async def _forward(tries: list[dict[str, Any]], body: dict[str, Any]) -> Any:
    errors: list[dict[str, Any]] = []
    for idx, model in enumerate(tries, 1):
        payload = build_payload(body, model)
        try:
            async with common.make_client(TIMEOUT_S) as client:
                resp = await client.post(
                    target_url(model["provider"]), headers=headers_for(model["provider"]), json=payload
                )
        except httpx.TimeoutException:
            mark_failure(model, common.STATUS_TIMEOUT)
            errors.append({"model": model.get("id"), "status": common.STATUS_TIMEOUT})
            continue
        except (httpx.HTTPError, OSError) as exc:
            mark_failure(model, common.STATUS_UPSTREAM)
            errors.append({"model": model.get("id"), "status": common.STATUS_UPSTREAM, "error": str(exc)[:150]})
            continue

        if resp.status_code < 400:
            try:
                data = resp.json()
            except ValueError:
                mark_failure(model, common.STATUS_EMPTY)
                errors.append({"model": model.get("id"), "status": common.STATUS_EMPTY})
                continue
            data.setdefault("model", body.get("model") or model.get("id"))
            return JSONResponse(
                data,
                headers={
                    "X-Gateway-Provider": str(model.get("provider")),
                    "X-Gateway-Model": str(model.get("upstream")),
                    "X-Gateway-Attempt": str(idx),
                },
            )

        status = common.classify(resp.status_code, resp.text[:400])
        mark_failure(model, status)
        errors.append({"model": model.get("id"), "status": status, "http": resp.status_code,
                       "error": resp.text[:150]})

    return JSONResponse(
        {"error": {"message": "all upstreams failed", "type": "upstream_error", "attempts": errors}},
        status_code=502,
    )


async def _stream(tries: list[dict[str, Any]], body: dict[str, Any]):
    errors: list[dict[str, Any]] = []
    for model in tries:
        payload = build_payload(body, model)
        payload["stream"] = True
        client = common.make_client(TIMEOUT_S)
        try:
            req = client.build_request(
                "POST", target_url(model["provider"]),
                headers=headers_for(model["provider"]), json=payload,
            )
            resp = await client.send(req, stream=True)
            if resp.status_code >= 400:
                text = (await resp.aread()).decode("utf-8", "ignore")[:200]
                status = common.classify(resp.status_code, text)
                mark_failure(model, status)
                errors.append({"model": model.get("id"), "status": status, "http": resp.status_code})
                await resp.aclose()
                await client.aclose()
                continue
            try:
                async for chunk in resp.aiter_raw():
                    if chunk:
                        yield chunk
            finally:
                await resp.aclose()
                await client.aclose()
            return
        except httpx.TimeoutException:
            mark_failure(model, common.STATUS_TIMEOUT)
            errors.append({"model": model.get("id"), "status": common.STATUS_TIMEOUT})
            await client.aclose()
        except (httpx.HTTPError, OSError) as exc:
            mark_failure(model, common.STATUS_UPSTREAM)
            errors.append({"model": model.get("id"), "status": common.STATUS_UPSTREAM, "error": str(exc)[:120]})
            await client.aclose()

    payload = json.dumps({"error": {"message": "all upstreams failed", "attempts": errors}}, ensure_ascii=False)
    yield f"data: {payload}\n\n".encode("utf-8")
    yield b"data: [DONE]\n\n"


@app.get("/")
async def index() -> dict[str, Any]:
    state = load_state()
    return {
        "name": "free-llm-hub gateway",
        "endpoints": ["/v1/models", "/v1/chat/completions", "/healthz"],
        "live_models": len(state.get("models") or []),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启动统一网关")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args(argv)

    import uvicorn

    if not gateway_keys():
        print("[warn] 未设置 GATEWAY_KEY，网关不鉴权，仅适合本机调试", flush=True)
    load_state(force=True)
    print(f"[gateway] http://{args.host}:{args.port}  (state={_state_cache.get('source')})", flush=True)
    if args.reload:
        print("[warn] --reload 会二次导入本模块，已忽略", flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
