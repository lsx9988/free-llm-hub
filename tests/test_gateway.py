"""网关测试：MockTransport 模拟上游，验证鉴权、自动选路、fallback、流式。"""

import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

import common
import gateway

GATEWAY_KEY = "sk-hub-test"


def make_transport():
    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == "bad.test":
            return httpx.Response(500, json={"error": {"message": "boom"}})
        if host == "good.test":
            body = json.loads(request.content)
            if body.get("stream"):
                chunks = (
                    b'data: {"choices":[{"delta":{"content":"OK"}}]}\n\n'
                    b"data: [DONE]\n\n"
                )
                return httpx.Response(200, stream=httpx.ByteStream(chunks),
                                      headers={"content-type": "text/event-stream"})
            return httpx.Response(200, json={
                "id": "chatcmpl-good",
                "object": "chat.completion",
                "model": body.get("model"),
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"}}],
            })
        return httpx.Response(404, json={"error": {"message": "unknown host"}})

    return httpx.MockTransport(handler)


@pytest.fixture()
def client(monkeypatch, tmp_path):
    state = {
        "generated_at_bj": "2026-11-12 16:00:00",
        "stats": {"models_live": 2, "providers_up": 2},
        "models": [
            {"id": "bad/m1", "provider": "bad", "upstream": "m1", "status": "ok",
             "latency_ms": 100, "score": 1.0},
            {"id": "good/m1", "provider": "good", "upstream": "m1", "status": "ok",
             "latency_ms": 900, "score": 0.8},
        ],
    }
    state_file = tmp_path / "models.json"
    state_file.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    monkeypatch.setenv("STATE_FILE", str(state_file))
    monkeypatch.setenv("GATEWAY_KEY", GATEWAY_KEY)
    monkeypatch.setenv("TEST_KEY", "sk-upstream")
    monkeypatch.setattr(common, "make_client",
                        lambda timeout=30.0: httpx.AsyncClient(
                            transport=make_transport(), timeout=timeout))

    gateway.CONFIG = {
        "gateway": {"key_env": "GATEWAY_KEY", "max_attempts": 3, "cooldown_s": 60,
                    "request_timeout_s": 10, "state_refresh_s": 600},
        "providers": {
            "bad": {"base_url": "https://bad.test/v1", "key_env": "TEST_KEY"},
            "good": {"base_url": "https://good.test/v1", "key_env": "TEST_KEY"},
        },
    }
    gateway._state_cache.update({"data": None, "at": 0.0, "source": ""})
    gateway._cooldown.clear()
    return TestClient(gateway.app)


def auth_header():
    return {"Authorization": f"Bearer {GATEWAY_KEY}"}


def test_auth_required(client):
    assert client.post("/v1/chat/completions", json={"messages": []}).status_code == 401
    assert client.get("/v1/models").status_code == 401
    assert client.get("/v1/models", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_models_list(client):
    resp = client.get("/v1/models", headers=auth_header())
    assert resp.status_code == 200
    ids = [m["id"] for m in resp.json()["data"]]
    assert ids[0] == "auto"
    assert "good/m1" in ids


def test_auto_fallback_skips_broken_upstream(client):
    resp = client.post("/v1/chat/completions", headers=auth_header(),
                       json={"model": "auto", "messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 200
    assert resp.headers["x-gateway-provider"] == "good"
    assert resp.headers["x-gateway-attempt"] == "2"
    assert resp.json()["choices"][0]["message"]["content"] == "OK"


def test_broken_provider_enters_cooldown(client):
    client.post("/v1/chat/completions", headers=auth_header(),
                json={"model": "bad/m1", "messages": [{"role": "user", "content": "hi"}]})
    assert gateway._cooldown  # 失败后被标记冷却

    # 再请求同一个模型：已冷却，直接走 fallback，不再打 bad
    resp = client.post("/v1/chat/completions", headers=auth_header(),
                       json={"model": "bad/m1", "messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 200
    assert resp.headers["x-gateway-provider"] == "good"


def test_bare_model_name_matches(client):
    resp = client.post("/v1/chat/completions", headers=auth_header(),
                       json={"model": "m1", "messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 200
    assert resp.headers["x-gateway-provider"] == "good"


def test_stream_passthrough(client):
    resp = client.post("/v1/chat/completions", headers=auth_header(),
                       json={"model": "good/m1", "stream": True,
                             "messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 200
    assert "data:" in resp.text and "[DONE]" in resp.text


def test_healthz(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["live_models"] == 2
    assert body["ok"] is True


def test_no_live_model_returns_503(client, monkeypatch, tmp_path):
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"models": []}), encoding="utf-8")
    monkeypatch.setenv("STATE_FILE", str(empty))
    gateway._state_cache.update({"data": None, "at": 0.0, "source": ""})
    resp = client.post("/v1/chat/completions", headers=auth_header(),
                       json={"model": "auto", "messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 503
