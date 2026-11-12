"""探测逻辑测试：用 MockTransport 模拟三家厂商，不需要真实 Key、不联网。"""

import json

import httpx
import pytest

import common
import probe


def make_transport():
    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == "ok.test":
            body = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-1",
                    "model": body.get("model"),
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"}}],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
                },
            )
        if host == "auth.test":
            return httpx.Response(401, json={"error": {"message": "invalid api key"}})
        if host == "rate.test":
            return httpx.Response(429, json={"error": {"message": "rate limit exceeded"}})
        return httpx.Response(500, json={"error": {"message": "boom"}})

    return httpx.MockTransport(handler)


@pytest.fixture()
def fake_client(monkeypatch):
    def factory(timeout: float = 30.0):
        return httpx.AsyncClient(transport=make_transport(), timeout=timeout)

    monkeypatch.setattr(common, "make_client", factory)
    return factory


def build_config(**probe_overrides):
    probe_cfg = {
        "prompt": "Reply with exactly: OK",
        "max_tokens": 16,
        "timeout_s": 5,
        "concurrency": 4,
        "min_score": 0.0,
        "max_latency_ms": 15000,
        "keep_degraded": False,
        "include": "",
        "exclude": "",
    }
    probe_cfg.update(probe_overrides)
    return {
        "probe": probe_cfg,
        "providers": {
            "ok": {
                "label": "OK 厂商",
                "base_url": "https://ok.test/v1",
                "key_env": "TEST_KEY",
                "models": ["m-fast", "m-slow"],
                "weight": 1.0,
            },
            "auth": {
                "label": "鉴权失败",
                "base_url": "https://auth.test/v1",
                "key_env": "TEST_KEY",
                "models": ["m1"],
            },
            "rate": {
                "label": "限流",
                "base_url": "https://rate.test/v1",
                "key_env": "TEST_KEY",
                "models": ["m1"],
            },
        },
    }


@pytest.mark.asyncio
async def test_probe_filters_and_scores(fake_client, monkeypatch):
    monkeypatch.setenv("TEST_KEY", "sk-test")
    result = await probe.probe_all(build_config(), [], None, False)
    state, status = result["state"], result["status"]

    assert state["stats"]["models_tested"] == 4
    assert state["stats"]["models_live"] == 2
    assert {m["id"] for m in state["models"]} == {"ok/m-fast", "ok/m-slow"}
    assert state["providers"][0]["status"] == "up"

    auth_row = next(p for p in state["providers"] if p["id"] == "auth")
    assert auth_row["status"] == "auth_error"
    assert "invalid api key" in (auth_row["error"] or "")

    assert len(status["failures"]) == 2
    assert all(m["score"] > 0 for m in state["models"])
    assert state["models"][0]["score"] >= state["models"][-1]["score"]


@pytest.mark.asyncio
async def test_probe_keeps_degraded_when_enabled(fake_client, monkeypatch):
    monkeypatch.setenv("TEST_KEY", "sk-test")
    cfg = build_config(keep_degraded=True)
    result = await probe.probe_all(cfg, [], None, False)
    state = result["state"]

    assert state["stats"]["models_live"] == 3
    degraded = [m for m in state["models"] if m["status"] == "degraded"]
    assert len(degraded) == 1
    assert degraded[0]["provider"] == "rate"
    assert degraded[0]["score"] < 0.5


@pytest.mark.asyncio
async def test_probe_skips_provider_without_key(fake_client, monkeypatch):
    monkeypatch.delenv("TEST_KEY", raising=False)
    result = await probe.probe_all(build_config(), [], None, False)
    state = result["state"]

    assert state["stats"]["models_tested"] == 0
    assert all(p["status"] == "skipped" for p in state["providers"])


@pytest.mark.asyncio
async def test_probe_readme_block_is_replaced(fake_client, monkeypatch, tmp_path):
    monkeypatch.setenv("TEST_KEY", "sk-test")
    readme = tmp_path / "README.md"
    original = "头部\n<!-- STATUS_START -->\n旧内容\n<!-- STATUS_END -->\n尾部"
    readme.write_text(original, encoding="utf-8")
    monkeypatch.setattr(probe, "README", readme)

    result = await probe.probe_all(build_config(), [], None, False)
    probe.update_readme(result["state"], result["status"])

    text = readme.read_text(encoding="utf-8")
    assert "旧内容" not in text
    assert "可用模型 **2** 个" in text
    assert text.startswith("头部")
    assert text.rstrip().endswith("尾部")
    assert text.count("<!-- STATUS_START -->") == 1
