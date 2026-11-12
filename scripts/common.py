"""free-llm-hub 公共模块：配置加载、鉴权头、状态分类、打分。

被 scripts/probe.py 与 scripts/gateway.py 共用。
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "providers.yaml"
OUTPUT_DIR = ROOT / "output"
BJ_TZ = timezone(timedelta(hours=8))

_ENV_PLACEHOLDER = re.compile(r"\${([A-Za-z_][A-Za-z0-9_]*)}")

# 探测结果状态
STATUS_OK = "ok"
STATUS_DEGRADED = "degraded"
STATUS_AUTH = "auth_error"
STATUS_NOT_FOUND = "model_not_found"
STATUS_RATE = "rate_limited"
STATUS_QUOTA = "quota_exhausted"
STATUS_BAD = "bad_request"
STATUS_UPSTREAM = "upstream_error"
STATUS_TIMEOUT = "timeout"
STATUS_EMPTY = "empty_response"
STATUS_SKIPPED = "skipped"

LIVE_STATUSES = (STATUS_OK, STATUS_DEGRADED)


def now_bj() -> datetime:
    return datetime.now(BJ_TZ)


def stamp(dt: datetime | None = None) -> str:
    dt = dt or now_bj()
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def expand_env(value: str) -> str:
    """把 ${VAR} 替换成环境变量，缺失时替换为空串。"""
    return _ENV_PLACEHOLDER.sub(lambda m: os.getenv(m.group(1), ""), value)


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    path = Path(path) if path else Path(os.getenv("CONFIG_PATH") or CONFIG_PATH)
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def provider_key(provider: dict[str, Any]) -> str:
    return os.getenv(provider.get("key_env", ""), "").strip()


def has_key(provider: dict[str, Any]) -> bool:
    expanded_url = expand_env(provider.get("base_url", ""))
    if "${" in expanded_url:
        return False
    return bool(provider_key(provider))


def provider_base(provider: dict[str, Any]) -> str:
    return expand_env(provider.get("base_url", "")).rstrip("/")


def join_url(base: str, path: str) -> str:
    return f"{base.rstrip('/')}/{path.lstrip('/')}"


def build_headers(provider: dict[str, Any], extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "free-llm-hub/1.0",
    }
    key = provider_key(provider)
    if key:
        headers["Authorization"] = f"Bearer {key}"
    headers.update(provider.get("extra_headers") or {})
    if extra:
        headers.update(extra)
    return headers


def make_client(timeout: float = 30.0) -> httpx.AsyncClient:
    """单点出口：测试时可直接 monkeypatch 本函数注入 MockTransport。"""
    return httpx.AsyncClient(timeout=timeout, follow_redirects=True)


def classify(status_code: int, text: str = "") -> str:
    """把 HTTP 状态码 + 响应体映射成可统计的状态枚举。"""
    low = (text or "").lower()
    quota_hint = ("quota", "balance", "余额", "额度", "credit", "insufficient", "exceeded")
    if status_code in (401, 403):
        return STATUS_AUTH
    if status_code == 402:
        return STATUS_QUOTA
    if status_code == 404:
        return STATUS_NOT_FOUND
    if status_code == 429:
        if any(k in low for k in ("quota", "balance", "余额", "额度", "credit")):
            return STATUS_QUOTA
        return STATUS_RATE
    if 400 <= status_code < 500:
        if any(k in low for k in quota_hint):
            return STATUS_QUOTA
        if "not found" in low or "does not exist" in low or "no such model" in low:
            return STATUS_NOT_FOUND
        return STATUS_BAD
    return STATUS_UPSTREAM


def extract_content(data: dict[str, Any]) -> str:
    """兼容 choices/chat 与 responses 两种返回结构。"""
    if not isinstance(data, dict):
        return ""
    choices = data.get("choices") or []
    if choices:
        first = choices[0] or {}
        msg = first.get("message") or {}
        content = msg.get("content")
        if isinstance(content, list):
            parts = [p.get("text", "") for p in content if isinstance(p, dict)]
            return "".join(parts).strip()
        if isinstance(content, str):
            return content.strip()
        if isinstance(first.get("text"), str):
            return first["text"].strip()
    output = data.get("output_text")
    if isinstance(output, str):
        return output.strip()
    return ""


def score_model(
    latency_ms: float | None,
    status: str,
    weight: float = 1.0,
    max_latency_ms: int = 15000,
) -> float:
    """可用性 × 厂商权重 × 延迟惩罚。ok≈1.0，429≈0.35，失败=0。"""
    base = {STATUS_OK: 1.0, STATUS_DEGRADED: 0.35, STATUS_RATE: 0.35}.get(status, 0.0)
    lat = 0.0 if latency_ms is None else min(max(float(latency_ms), 0.0) / max(max_latency_ms, 1), 1.0)
    value = base * float(weight) * (1.0 - 0.4 * lat)
    return round(max(0.0, min(1.2, value)), 3)


def write_json(path: str | Path, payload: Any) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    path.write_text(text, encoding="utf-8")
    return path


def mask(value: str, keep: int = 4) -> str:
    if not value:
        return "(未配置)"
    if len(value) <= keep:
        return "*" * len(value)
    return f"{value[:keep]}…{value[-2:]}"


def git_sha() -> str:
    sha = os.getenv("GITHUB_SHA", "")
    return sha[:7] if sha else ""


def elapsed_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 1)
