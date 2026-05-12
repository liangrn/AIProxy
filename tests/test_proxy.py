import json
import httpx

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.config import get_settings


def test_non_stream_response_uses_chat_completion(monkeypatch):
    captured = {}

    async def fake_create_chat_completion(payload, stream):
        captured["payload"] = payload
        captured["stream"] = stream
        return {
            "id": "chat_1",
            "model": "gpt-5.5",
            "choices": [
                {
                    "message": {"role": "assistant", "content": "Hi!"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
        }

    monkeypatch.setenv("UPSTREAM_API_KEY", "test-key")
    monkeypatch.setenv("UPSTREAM_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("UPSTREAM_MODEL", "gpt-5.5")
    monkeypatch.setattr("app.main.create_chat_completion", fake_create_chat_completion)

    client = TestClient(create_app())
    response = client.post("/v1/responses", json={"input": "say hi", "stream": False})

    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "response"
    assert body["status"] == "completed"
    assert body["model"] == "gpt-5.5"
    assert body["output"][0]["content"][0]["text"] == "Hi!"
    assert captured["stream"] is False
    assert captured["payload"]["model"] == "gpt-5.5"
    assert captured["payload"]["messages"] == [{"role": "user", "content": "say hi"}]


def test_stream_response_finishes_with_response_completed(monkeypatch):
    async def fake_stream_chat_completion(payload):
        yield {"choices": [{"delta": {"content": "Hel"}, "finish_reason": None}]}
        yield {"choices": [{"delta": {"content": "lo"}, "finish_reason": None}]}
        yield {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"total_tokens": 3}}
        yield "[DONE]"

    monkeypatch.setenv("UPSTREAM_API_KEY", "test-key")
    monkeypatch.setenv("UPSTREAM_BASE_URL", "https://example.test/v1")
    monkeypatch.setattr("app.main.stream_chat_completion", fake_stream_chat_completion)

    client = TestClient(create_app())
    with client.stream("POST", "/v1/responses", json={"model": "gpt-5.4", "input": "hello", "stream": True}) as response:
        assert response.status_code == 200
        text = "".join(response.iter_text())

    assert "event: response.created" in text
    assert '"type":"response.output_text.delta"' in text
    assert '"delta":"Hel"' in text
    assert '"delta":"lo"' in text
    assert "event: response.completed" in text
    assert text.rstrip().endswith("data: [DONE]")


def test_input_items_are_converted_to_chat_messages(monkeypatch):
    captured = {}

    async def fake_create_chat_completion(payload, stream):
        captured["payload"] = payload
        return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setenv("UPSTREAM_API_KEY", "test-key")
    monkeypatch.setenv("UPSTREAM_BASE_URL", "https://example.test/v1")
    monkeypatch.setattr("app.main.create_chat_completion", fake_create_chat_completion)

    client = TestClient(create_app())
    response = client.post(
        "/v1/responses",
        json={
            "model": "gpt-5.5",
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "first"},
                        {"type": "text", "text": "second"},
                    ],
                }
            ],
        },
    )

    assert response.status_code == 200
    assert captured["payload"]["messages"] == [{"role": "user", "content": "first\nsecond"}]


def test_healthz_does_not_require_upstream_key(monkeypatch):
    monkeypatch.delenv("UPSTREAM_API_KEY", raising=False)

    client = TestClient(create_app())
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_models_are_configurable(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEXPROXY_CONFIG_PATH", str(tmp_path / "config.local.json"))
    monkeypatch.setenv("UPSTREAM_PROVIDER_NAME", "other-platform")
    monkeypatch.setenv("UPSTREAM_MODELS", "model-a, model-b,model-a")

    client = TestClient(create_app())
    response = client.get("/v1/models")

    assert response.status_code == 200
    assert response.json() == {
        "object": "list",
        "data": [
            {"id": "model-a", "object": "model", "owned_by": "other-platform"},
            {"id": "model-b", "object": "model", "owned_by": "other-platform"},
        ],
    }


def test_default_port_is_8383(monkeypatch):
    monkeypatch.delenv("LISTEN_PORT", raising=False)

    assert get_settings().listen_port == 8383


def test_admin_config_persists_and_models_use_it(monkeypatch, tmp_path):
    config_path = tmp_path / "config.local.json"
    monkeypatch.setenv("CODEXPROXY_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("UPSTREAM_API_KEY", "old-key")

    client = TestClient(create_app())
    response = client.post(
        "/admin/config",
        json={
            "upstream_provider_name": "new-provider",
            "upstream_base_url": "https://new.example/v1/",
            "upstream_api_key": "new-key",
            "upstream_model": "model-b",
            "upstream_models": ["model-a", "model-b", "model-a", ""],
            "upstream_user_agent": "test-agent",
            "request_timeout_seconds": 45,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["config"]["upstream_base_url"] == "https://new.example/v1"
    assert body["config"]["upstream_api_key"] == "new-key"
    assert body["config"]["active"]["base_url"] == "https://new.example"
    assert body["config"]["active"]["api_key"] == "new-key"

    models = client.get("/v1/models")
    assert models.json() == {
        "object": "list",
        "data": [
            {"id": "model-a", "object": "model", "owned_by": "new-provider"},
            {"id": "model-b", "object": "model", "owned_by": "new-provider"},
        ],
    }


def test_admin_config_keeps_existing_api_key_when_blank(monkeypatch, tmp_path):
    config_path = tmp_path / "config.local.json"
    monkeypatch.setenv("CODEXPROXY_CONFIG_PATH", str(config_path))

    client = TestClient(create_app())
    assert client.post(
        "/admin/config",
        json={
            "upstream_provider_name": "first",
            "upstream_base_url": "https://first.example/v1",
            "upstream_api_key": "secret-key",
            "upstream_model": "model-a",
            "upstream_models": "model-a",
        },
    ).status_code == 200

    response = client.post(
        "/admin/config",
        json={
            "upstream_provider_name": "second",
            "upstream_base_url": "https://second.example/v1",
            "upstream_api_key": "",
            "upstream_model": "model-b",
            "upstream_models": "model-b",
        },
    )

    assert response.status_code == 200
    assert get_settings().upstream_api_key == "secret-key"


def test_admin_profiles_switch_active_profile_and_normalize_base_url(monkeypatch, tmp_path):
    config_path = tmp_path / "config.local.json"
    monkeypatch.setenv("CODEXPROXY_CONFIG_PATH", str(config_path))

    client = TestClient(create_app())
    first = client.post(
        "/admin/profiles/uocode",
        json={
            "name": "UoCode",
            "base_url": "https://www.uocode.com",
            "api_key": "uocode-key",
            "default_model": "uocode-default",
            "models": "uocode-default,uocode-fast",
        },
    )
    second = client.post(
        "/admin/profiles/aicoego",
        json={
            "name": "AiCoeGo",
            "base_url": "https://aicoego.example/v1",
            "api_key": "aicoego-key",
            "default_model": "aicoego-default",
            "models": ["aicoego-default", "aicoego-long"],
        },
    )
    activate = client.post("/admin/profiles/aicoego/activate")

    assert first.status_code == 200
    assert first.json()["profile"]["base_url"] == "https://www.uocode.com"
    assert second.status_code == 200
    assert second.json()["profile"]["base_url"] == "https://aicoego.example"
    assert activate.status_code == 200

    config = client.get("/admin/config").json()["config"]
    assert config["active_profile"] == "aicoego"
    assert config["active"]["name"] == "AiCoeGo"
    assert config["active"]["api_key"] == "aicoego-key"
    assert config["active"]["base_url"] == "https://aicoego.example"

    settings = get_settings()
    assert settings.upstream_provider_name == "AiCoeGo"
    assert settings.upstream_base_url == "https://aicoego.example/v1"
    assert settings.upstream_api_key == "aicoego-key"
    assert settings.upstream_model == "aicoego-default"

    response = client.get("/v1/models")
    assert response.json()["data"] == [
        {"id": "aicoego-default", "object": "model", "owned_by": "AiCoeGo"},
        {"id": "aicoego-long", "object": "model", "owned_by": "AiCoeGo"},
    ]


def test_refresh_models_updates_active_profile(monkeypatch, tmp_path):
    config_path = tmp_path / "config.local.json"
    monkeypatch.setenv("CODEXPROXY_CONFIG_PATH", str(config_path))

    async def fake_get(self, url, headers):
        assert url == "https://www.uocode.com/v1/models"
        assert headers["Authorization"] == "Bearer uocode-key"
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {"id": "model-a"},
                    {"id": "model-b"},
                    {"id": "model-a"},
                ],
            },
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    client = TestClient(create_app())
    client.post(
        "/admin/profiles/uocode",
        json={
            "name": "UoCode",
            "base_url": "https://www.uocode.com",
            "api_key": "uocode-key",
            "default_model": "model-a",
            "models": "old-model",
        },
    )

    response = client.post("/admin/models/refresh")

    assert response.status_code == 200
    assert response.json()["profile"]["models"] == ["model-a", "model-b"]
    assert get_settings().upstream_models == ["model-a", "model-b"]


def test_refresh_models_failure_keeps_existing_models(monkeypatch, tmp_path):
    config_path = tmp_path / "config.local.json"
    monkeypatch.setenv("CODEXPROXY_CONFIG_PATH", str(config_path))

    async def fake_get(self, url, headers):
        return httpx.Response(500, text="bad gateway")

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    client = TestClient(create_app())
    client.post(
        "/admin/profiles/uocode",
        json={
            "name": "UoCode",
            "base_url": "https://www.uocode.com",
            "api_key": "uocode-key",
            "default_model": "old-model",
            "models": "old-model",
        },
    )

    response = client.post("/admin/models/refresh")

    assert response.status_code == 502
    assert get_settings().upstream_models == ["old-model"]


def test_admin_page_uses_searchable_model_picker():
    client = TestClient(create_app())

    response = client.get("/")

    assert response.status_code == 200
    text = response.text
    assert 'id="modelMenu"' in text
    assert 'renderModelMenu' in text
    assert 'datalist' not in text


def test_protocol_check_reports_codex_and_upstream_contract(monkeypatch, tmp_path):
    config_path = tmp_path / "config.local.json"
    monkeypatch.setenv("CODEXPROXY_CONFIG_PATH", str(config_path))

    async def fake_get(self, url, headers):
        assert url == "https://www.uocode.com/v1/models"
        return httpx.Response(200, json={"data": [{"id": "model-a"}]})

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    client = TestClient(create_app())
    client.post(
        "/admin/profiles/uocode",
        json={
            "name": "UoCode",
            "base_url": "https://www.uocode.com",
            "api_key": "uocode-key",
            "default_model": "model-a",
            "models": "model-a",
        },
    )

    response = client.post("/admin/protocol/check")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["codex_desktop"]["wire_api"] == "responses"
    assert body["codex_desktop"]["base_url"] == "http://127.0.0.1:8383/v1"
    assert body["upstream"]["chat_completions_url"] == "https://www.uocode.com/v1/chat/completions"
    assert body["upstream"]["models_ok"] is True
