#!/usr/bin/env python3
"""每日探测：把配置里所有厂商的免费模型各打一遍 → 筛出可用的 → 写 output/。

用法：
    python scripts/probe.py                 # 正常探测
    python scripts/probe.py --dry-run       # 只打印将要探测的模型，不发请求
    python scripts/probe.py --only agnes zhipu
    python scripts/probe.py --require-live  # 一个可用模型都没有时退出码 1（CI 用）

产物：
    output/models.json   网关读取的可用模型池（含分数、延迟、状态）
    output/status.json   本次探测的汇总与失败明细
    output/history.json  最近 30 次的可用数趋势
    README.md            STATUS_START / STATUS_END 之间自动刷新
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

import httpx

import common

ROOT = common.ROOT
OUTPUT_DIR = common.OUTPUT_DIR
README = ROOT / "README.md"
STATUS_START = "<!-- STATUS_START -->"
STATUS_END = "<!-- STATUS_END -->"
HISTORY_LIMIT = 30


def log(msg: str) -> None:
    print(msg, flush=True)


# --------------------------------------------------------------------------
# 模型清单
# --------------------------------------------------------------------------
def _model_filter(cfg: dict[str, Any]) -> tuple[re.Pattern | None, re.Pattern | None]:
    probe_cfg = cfg.get("probe") or {}
    include = probe_cfg.get("include") or ""
    exclude = probe_cfg.get("exclude") or ""
    inc = re.compile(include, re.I) if include else None
    exc = re.compile(exclude, re.I) if exclude else None
    return inc, exc


def _pick(models: list[str], cfg: dict[str, Any]) -> list[str]:
    inc, exc = _model_filter(cfg)
    out: list[str] = []
    for name in models:
        if not name:
            continue
        if inc and not inc.search(name):
            continue
        if exc and exc.search(name):
            continue
        if name not in out:
            out.append(name)
    return out


async def resolve_models(client: httpx.AsyncClient, provider: dict[str, Any], cfg: dict[str, Any]) -> list[str]:
    """discover=true 时拉 /models 自动发现，失败或为空则回退到静态清单。"""
    configured = [str(m) for m in (provider.get("models") or [])]
    if not provider.get("discover"):
        return _pick(configured, cfg)

    url = common.join_url(common.provider_base(provider), "models")
    try:
        resp = await client.get(url, headers=common.build_headers(provider), timeout=20)
        if resp.status_code == 200:
            payload = resp.json()
            ids = [str(item.get("id")) for item in (payload.get("data") or []) if item.get("id")]
            discovered = _pick(ids, cfg)
            if discovered:
                return discovered
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        log(f"  [discover] 拉取模型列表失败：{type(exc).__name__}: {exc}")
    return _pick(configured, cfg)


# --------------------------------------------------------------------------
# 单个模型探测
# --------------------------------------------------------------------------
async def probe_model(
    client: httpx.AsyncClient,
    provider_id: str,
    provider: dict[str, Any],
    upstream: str,
    cfg: dict[str, Any],
    sem: asyncio.Semaphore,
) -> dict[str, Any]:
    probe_cfg = cfg.get("probe") or {}
    url = common.join_url(common.provider_base(provider), "chat/completions")
    payload = {
        "model": upstream,
        "messages": [{"role": "user", "content": probe_cfg.get("prompt", "Reply with exactly: OK")}],
        "max_tokens": int(probe_cfg.get("max_tokens", 16)),
        "temperature": 0,
        "stream": False,
    }
    row: dict[str, Any] = {
        "provider": provider_id,
        "upstream": upstream,
        "status": common.STATUS_UPSTREAM,
        "latency_ms": None,
        "http": None,
        "error": None,
    }
    async with sem:
        started = time.perf_counter()
        try:
            resp = await client.post(
                url,
                headers=common.build_headers(provider),
                json=payload,
                timeout=float(probe_cfg.get("timeout_s", 30)),
            )
            row["latency_ms"] = common.elapsed_ms(started)
            row["http"] = resp.status_code
            if resp.status_code >= 400:
                row["status"] = common.classify(resp.status_code, resp.text[:400])
                row["error"] = short_error(resp)
            else:
                try:
                    data = resp.json()
                except ValueError:
                    row["status"] = common.STATUS_EMPTY
                    ctype = resp.headers.get("content-type", "?")
                    snippet = re.sub(r"\s+", " ", (resp.text or ""))[:120]
                    row["error"] = f"响应不是 JSON (ct={ctype})：{snippet}"
                else:
                    content = common.extract_content(data)
                    if content:
                        row["status"] = common.STATUS_OK
                    else:
                        row["status"] = common.STATUS_EMPTY
                        row["error"] = "200 但内容为空"
        except httpx.TimeoutException:
            row["latency_ms"] = common.elapsed_ms(started)
            row["status"] = common.STATUS_TIMEOUT
            row["error"] = f"超时 >{probe_cfg.get('timeout_s', 30)}s"
        except (httpx.HTTPError, OSError) as exc:
            row["latency_ms"] = common.elapsed_ms(started)
            row["status"] = common.STATUS_UPSTREAM
            row["error"] = f"{type(exc).__name__}: {exc}"[:200]
    return row


def short_error(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        err = body.get("error") if isinstance(body, dict) else None
        if isinstance(err, dict):
            msg = err.get("message") or err.get("code") or ""
        elif isinstance(err, str):
            msg = err
        else:
            msg = str(body)[:200]
    except ValueError:
        msg = resp.text[:200]
    msg = re.sub(r"\s+", " ", str(msg)).strip()
    return msg[:200]


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
async def probe_all(cfg: dict[str, Any], only: list[str], limit: int | None, dry_run: bool) -> dict[str, Any]:
    probe_cfg = cfg.get("probe") or {}
    concurrency = int(probe_cfg.get("concurrency", 6))
    max_latency = int(probe_cfg.get("max_latency_ms", 15000))
    min_score = float(probe_cfg.get("min_score", 0.55))
    keep_degraded = bool(probe_cfg.get("keep_degraded", True))

    providers: dict[str, Any] = cfg.get("providers") or {}
    global_sem = asyncio.Semaphore(concurrency)
    provider_rows: list[dict[str, Any]] = []
    model_rows: list[dict[str, Any]] = []

    async with common.make_client(timeout=float(probe_cfg.get("timeout_s", 30)) + 5) as client:
        for pid, provider in providers.items():
            label = provider.get("label", pid)
            if only and pid not in only:
                continue
            if not provider.get("enabled", True):
                log(f"- {label}: 已禁用，跳过")
                provider_rows.append(skip_row(pid, label, "配置中已禁用"))
                continue
            if not common.has_key(provider):
                log(f"- {label}: 未配置 Key（{provider.get('key_env')}），跳过")
                provider_rows.append(skip_row(pid, label, "未配置 Key"))
                continue

            models = await resolve_models(client, provider, cfg)
            if limit:
                models = models[:limit]
            if not models:
                log(f"- {label}: 没有待探测模型")
                provider_rows.append(skip_row(pid, label, "模型清单为空"))
                continue
            if dry_run:
                log(f"- {label}: 将探测 {len(models)} 个模型 -> {', '.join(models)}")
                provider_rows.append(skip_row(pid, label, "dry-run"))
                continue

            log(f"- {label}: 探测 {len(models)} 个模型")
            sem = asyncio.Semaphore(int(provider.get("concurrency", 2)))
            rows = await asyncio.gather(
                *[probe_model(client, pid, provider, m, cfg, sem) for m in models]
            )
            for row in rows:
                row["score"] = common.score_model(
                    row.get("latency_ms"),
                    row["status"],
                    float(provider.get("weight", 1.0)),
                    max_latency,
                )
                row["checked_at"] = common.stamp()
                row["id"] = f"{pid}/{row['upstream']}"
                if row.get("http") == 200 and row["status"] == common.STATUS_OK and row["score"] < min_score:
                    row["status"] = common.STATUS_EMPTY
                    row["error"] = f"分数 {row['score']} 低于阈值 {min_score}"
                model_rows.append(row)
                mark = "OK" if row["status"] == common.STATUS_OK else row["status"]
                log(f"    {row['upstream']:<40} {mark:<14} {row.get('latency_ms')}ms "
                    f"{('' if row['status'] == common.STATUS_OK else (row.get('error') or ''))[:60]}")

            ok_rows = [r for r in rows if r["status"] == common.STATUS_OK]
            bad_rows = [r for r in rows if r["status"] != common.STATUS_OK]
            best = min([r["latency_ms"] for r in ok_rows if r["latency_ms"]], default=None)
            first_error = next((r.get("error") for r in bad_rows if r.get("error")), None) if not ok_rows else None
            provider_rows.append(
                {
                    "id": pid,
                    "label": label,
                    "status": "up" if ok_rows else ("auth_error" if all(r["status"] == common.STATUS_AUTH for r in bad_rows) else "down"),
                    "tested": len(rows),
                    "live": len(ok_rows),
                    "latency_ms": best,
                    "error": first_error,
                }
            )

    # ---- 汇总 ----
    live_models = [
        r for r in model_rows
        if r["status"] == common.STATUS_OK or (keep_degraded and r["status"] == common.STATUS_RATE)
    ]
    if keep_degraded:
        for r in live_models:
            if r["status"] == common.STATUS_RATE:
                r["status"] = common.STATUS_DEGRADED
    live_models.sort(key=lambda r: (-r["score"], r.get("latency_ms") or 10 ** 9))

    tested = len(model_rows)
    up = len([p for p in provider_rows if p.get("status") == "up"])
    configured = len([p for p in provider_rows if p.get("status") != "skipped"]) or 0

    state = {
        "version": 1,
        "generated_at": common.now_bj().isoformat(timespec="seconds"),
        "generated_at_bj": common.stamp(),
        "git_sha": common.git_sha(),
        "stats": {
            "providers_total": len(providers),
            "providers_configured": configured,
            "providers_up": up,
            "models_tested": tested,
            "models_live": len(live_models),
        },
        "providers": provider_rows,
        "models": live_models,
    }

    status = {
        "generated_at": state["generated_at"],
        "generated_at_bj": state["generated_at_bj"],
        "stats": state["stats"],
        "providers": provider_rows,
        "failures": [
            {
                "id": r["id"],
                "provider": r["provider"],
                "status": r["status"],
                "http": r.get("http"),
                "error": r.get("error"),
            }
            for r in model_rows
            if r["status"] not in common.LIVE_STATUSES
        ],
        "all_models": model_rows,
    }
    state["_model_rows"] = model_rows
    return {"state": state, "status": status}


def skip_row(pid: str, label: str, reason: str) -> dict[str, Any]:
    return {
        "id": pid,
        "label": label,
        "status": common.STATUS_SKIPPED,
        "tested": 0,
        "live": 0,
        "latency_ms": None,
        "error": reason,
    }


# --------------------------------------------------------------------------
# README 状态块
# --------------------------------------------------------------------------
BADGE = {"up": "✅", "down": "❌", "auth_error": "🔑", "skipped": "⚪"}


def render_block(state: dict[str, Any], status: dict[str, Any]) -> str:
    stats = state["stats"]
    denom = stats["providers_configured"] or stats["providers_total"]
    lines: list[str] = []
    lines.append(
        f"> 🔄 最近刷新：{state['generated_at_bj']}（北京时间） · "
        f"在线 **{stats['providers_up']}/{denom}** 家 · "
        f"可用模型 **{stats['models_live']}** 个（探测 {stats['models_tested']} 个）"
    )
    lines.append("")

    if stats["models_live"] == 0:
        lines.append("> ⚠️ 当前没有可用模型：请在仓库 Settings → Secrets 中配置厂商 API Key，"
                     "然后到 Actions 页手动跑一次 `Daily Probe`。")
        lines.append("")

    lines.append("| 厂商 | 状态 | 可用/探测 | 最快延迟 | 备注 |")
    lines.append("| --- | :---: | :---: | :---: | --- |")
    for p in state["providers"]:
        icon = BADGE.get(p.get("status"), "❔")
        latency = f"{int(p['latency_ms'])}ms" if p.get("latency_ms") else "—"
        note = (p.get("error") or "—")
        note = re.sub(r"\s+", " ", str(note))[:60].replace("|", "/")
        lines.append(
            f"| {p.get('label', p['id'])} | {icon} | {p.get('live', 0)}/{p.get('tested', 0)} | {latency} | {note} |"
        )

    top = state["models"][:10]
    if top:
        lines.append("")
        lines.append("### 当前最快模型 TOP 10")
        lines.append("")
        lines.append("| 排名 | 模型 ID | 厂商 | 延迟 | 分数 |")
        lines.append("| :---: | --- | --- | :---: | :---: |")
        for idx, row in enumerate(top, 1):
            lines.append(
                f"| {idx} | `{row['id']}` | {row['provider']} | "
                f"{int(row['latency_ms']) if row.get('latency_ms') else '—'}ms | {row.get('score', 0):.2f} |"
            )

    failures = status.get("failures") or []
    if failures:
        lines.append("")
        lines.append(f"### 本次被筛掉（{len(failures)} 个）")
        lines.append("")
        lines.append("<details><summary>展开查看原因</summary>")
        lines.append("")
        lines.append("| 模型 | 状态 | 原因 |")
        lines.append("| --- | --- | --- |")
        for f in failures[:25]:
            err = re.sub(r"\s+", " ", str(f.get("error") or ""))[:80].replace("|", "/")
            lines.append(f"| `{f['id']}` | {f['status']} | {err} |")
        lines.append("")
        lines.append("</details>")
    return "\n".join(lines)


def update_readme(state: dict[str, Any], status: dict[str, Any]) -> None:
    if not README.exists():
        return
    text = README.read_text(encoding="utf-8")
    if STATUS_START not in text or STATUS_END not in text:
        log(f"README 缺少状态标记，跳过更新（{STATUS_START} / {STATUS_END}）")
        return
    block = f"{STATUS_START}\n{render_block(state, status)}\n{STATUS_END}"
    pattern = re.compile(re.escape(STATUS_START) + r".*?" + re.escape(STATUS_END), re.S)
    # 关键：用 lambda 返回，避免 block 里的反斜杠被当成替换转义
    new_text = pattern.sub(lambda _m: block, text)
    if new_text != text:
        README.write_text(new_text, encoding="utf-8")


def update_history(state: dict[str, Any]) -> None:
    path = OUTPUT_DIR / "history.json"
    rows: list[dict[str, Any]] = []
    if path.exists():
        try:
            rows = json.loads(path.read_text(encoding="utf-8")).get("rows") or []
        except (ValueError, AttributeError):
            rows = []
    rows.append(
        {
            "at": state["generated_at_bj"],
            "live": state["stats"]["models_live"],
            "tested": state["stats"]["models_tested"],
            "providers_up": state["stats"]["providers_up"],
        }
    )
    common.write_json(path, {"rows": rows[-HISTORY_LIMIT:]})


# --------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="探测多厂商免费模型并生成可用池")
    parser.add_argument("--config", default=str(common.CONFIG_PATH))
    parser.add_argument("--out", default=str(OUTPUT_DIR / "models.json"))
    parser.add_argument("--status", default=str(OUTPUT_DIR / "status.json"))
    parser.add_argument("--only", nargs="*", default=[], help="只探测指定 provider id")
    parser.add_argument("--limit", type=int, default=None, help="每家最多探测几个模型")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--require-live", action="store_true", help="可用模型为 0 时退出码 1")
    parser.add_argument("--no-readme", action="store_true")
    args = parser.parse_args(argv)

    cfg = common.load_config(args.config)
    result = asyncio.run(probe_all(cfg, args.only, args.limit, args.dry_run))
    state, status = result["state"], result["status"]
    model_rows = state.pop("_model_rows", [])

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    common.write_json(args.out, state)
    common.write_json(args.status, status)
    if not args.dry_run:
        update_history(state)
        if not args.no_readme:
            update_readme(state, status)

    stats = state["stats"]
    log(
        f"\n完成：在线 {stats['providers_up']}/{stats['providers_configured']} 家，"
        f"可用模型 {stats['models_live']}/{stats['models_tested']} 个 -> {args.out}"
    )
    if stats["models_tested"] == 0:
        log("提示：所有厂商都未配置 Key 或配置被跳过，本次没有探测任何模型。")
        return 0
    if stats["models_live"] == 0 and args.require_live:
        log("错误：没有任何可用模型（--require-live）")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
