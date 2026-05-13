import json
import httpx

import pytest
from fastapi.testclient import TestClient

import app.main as app_main
from app.main import create_app
from app.config import get_settings, resolve_claude_model, runtime_config_path


def test_non_stream_response_uses_chat_completion(monkeypatch, tmp_path):
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
    monkeypatch.setenv("UPSTREAM_API_STYLE", "chat")
    monkeypatch.setenv("CODEXPROXY_CONFIG_PATH", str(tmp_path / "config.local.json"))
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


def test_non_stream_response_passes_through_upstream_responses_when_configured(monkeypatch, tmp_path):
    async def fake_create_upstream_response(payload):
        assert payload["input"] == "say hi"
        return {
            "id": "resp_upstream",
            "object": "response",
            "status": "completed",
            "model": "gpt-5.5",
            "output": [{"id": "msg_1", "type": "message", "status": "completed", "role": "assistant", "content": [{"type": "output_text", "text": "Hi!"}]}],
            "usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
        }

    monkeypatch.setenv("CODEXPROXY_CONFIG_PATH", str(tmp_path / "config.local.json"))
    monkeypatch.setattr("app.main.create_upstream_response", fake_create_upstream_response)

    client = TestClient(create_app())
    client.post(
        "/admin/profiles/responses-provider",
        json={
            "name": "Responses Provider",
            "base_url": "https://responses.example",
            "api_key": "test-key",
            "default_model": "gpt-5.5",
            "models": "gpt-5.5",
        },
    )
    client.post("/admin/codex/config", json={"api_style": "responses"})

    response = client.post("/v1/responses", json={"input": "say hi", "stream": False})

    assert response.status_code == 200
    assert response.json()["id"] == "resp_upstream"
    assert response.json()["output"][0]["content"][0]["text"] == "Hi!"


def test_auto_api_style_falls_back_to_chat_when_responses_is_unsupported(monkeypatch, tmp_path):
    async def fake_create_upstream_response(payload):
        raise app_main.UpstreamProtocolUnsupported("responses unsupported")

    async def fake_create_chat_completion(payload, stream):
        return {
            "id": "chat_1",
            "model": "gpt-5.5",
            "choices": [{"message": {"role": "assistant", "content": "Fallback ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    monkeypatch.setenv("CODEXPROXY_CONFIG_PATH", str(tmp_path / "config.local.json"))
    monkeypatch.setattr("app.main.create_chat_completion", fake_create_chat_completion)
    monkeypatch.setattr("app.main.create_upstream_response", fake_create_upstream_response)

    client = TestClient(create_app())
    client.post(
        "/admin/profiles/auto-provider",
        json={
            "name": "Auto Provider",
            "base_url": "https://auto.example",
            "api_key": "test-key",
            "default_model": "gpt-5.5",
            "models": "gpt-5.5",
        },
    )

    response = client.post("/v1/responses", json={"input": "say hi", "stream": False})

    assert response.status_code == 200
    assert response.json()["output"][0]["content"][0]["text"] == "Fallback ok"


def test_stream_response_finishes_with_response_completed(monkeypatch, tmp_path):
    async def fake_stream_chat_completion(payload):
        yield {"choices": [{"delta": {"content": "Hel"}, "finish_reason": None}]}
        yield {"choices": [{"delta": {"content": "lo"}, "finish_reason": None}]}
        yield {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"total_tokens": 3}}
        yield "[DONE]"

    monkeypatch.setenv("UPSTREAM_API_KEY", "test-key")
    monkeypatch.setenv("UPSTREAM_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("UPSTREAM_API_STYLE", "chat")
    monkeypatch.setenv("CODEXPROXY_CONFIG_PATH", str(tmp_path / "config.local.json"))
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
    assert '"input_tokens":0' in text
    assert '"output_tokens":0' in text
    assert '"total_tokens":3' in text
    assert text.rstrip().endswith("data: [DONE]")


def test_non_stream_response_translates_chat_usage_to_responses_usage(monkeypatch, tmp_path):
    async def fake_create_chat_completion(payload, stream):
        return {
            "id": "chat_1",
            "model": "gpt-5.5",
            "choices": [{"message": {"role": "assistant", "content": "Hi!"}}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 5, "total_tokens": 9},
        }

    monkeypatch.setenv("UPSTREAM_API_KEY", "test-key")
    monkeypatch.setenv("UPSTREAM_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("UPSTREAM_API_STYLE", "chat")
    monkeypatch.setenv("CODEXPROXY_CONFIG_PATH", str(tmp_path / "config.local.json"))
    monkeypatch.setattr("app.main.create_chat_completion", fake_create_chat_completion)

    client = TestClient(create_app())
    response = client.post("/v1/responses", json={"input": "say hi", "stream": False})

    assert response.status_code == 200
    assert response.json()["usage"] == {"input_tokens": 4, "output_tokens": 5, "total_tokens": 9}


def test_non_stream_response_uses_reasoning_content_when_content_is_empty(monkeypatch, tmp_path):
    async def fake_create_chat_completion(payload, stream):
        return {
            "id": "chat_1",
            "model": "glm-5.1",
            "choices": [{"message": {"role": "assistant", "content": "", "reasoning_content": "OK"}}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 5, "total_tokens": 9},
        }

    monkeypatch.setenv("UPSTREAM_API_KEY", "test-key")
    monkeypatch.setenv("UPSTREAM_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("UPSTREAM_API_STYLE", "chat")
    monkeypatch.setenv("CODEXPROXY_CONFIG_PATH", str(tmp_path / "config.local.json"))
    monkeypatch.setattr("app.main.create_chat_completion", fake_create_chat_completion)

    client = TestClient(create_app())
    response = client.post("/v1/responses", json={"model": "glm-5.1", "input": "say ok", "stream": False})

    assert response.status_code == 200
    assert response.json()["output"][0]["content"][0]["text"] == "OK"


def test_stream_response_uses_reasoning_content_delta(monkeypatch, tmp_path):
    async def fake_stream_chat_completion(payload):
        yield {"choices": [{"delta": {"reasoning_content": "O"}, "finish_reason": None}]}
        yield {"choices": [{"delta": {"reasoning_content": "K"}, "finish_reason": None}]}
        yield {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"total_tokens": 2}}
        yield "[DONE]"

    monkeypatch.setenv("UPSTREAM_API_KEY", "test-key")
    monkeypatch.setenv("UPSTREAM_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("UPSTREAM_API_STYLE", "chat")
    monkeypatch.setenv("CODEXPROXY_CONFIG_PATH", str(tmp_path / "config.local.json"))
    monkeypatch.setattr("app.main.stream_chat_completion", fake_stream_chat_completion)

    client = TestClient(create_app())
    with client.stream("POST", "/v1/responses", json={"model": "glm-5.1", "input": "say ok", "stream": True}) as response:
        assert response.status_code == 200
        text = "".join(response.iter_text())

    assert '"delta":"O"' in text
    assert '"delta":"K"' in text
    assert '"text":"OK"' in text


def test_input_items_are_converted_to_chat_messages(monkeypatch, tmp_path):
    captured = {}

    async def fake_create_chat_completion(payload, stream):
        captured["payload"] = payload
        return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setenv("UPSTREAM_API_KEY", "test-key")
    monkeypatch.setenv("UPSTREAM_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("UPSTREAM_API_STYLE", "chat")
    monkeypatch.setenv("CODEXPROXY_CONFIG_PATH", str(tmp_path / "config.local.json"))
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


def test_admin_profile_persists_and_models_use_it(monkeypatch, tmp_path):
    config_path = tmp_path / "config.local.json"
    monkeypatch.setenv("CODEXPROXY_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("UPSTREAM_API_KEY", "old-key")

    client = TestClient(create_app())
    response = client.post(
        "/admin/profiles/new-provider",
        json={
            "name": "new-provider",
            "base_url": "https://new.example/v1/",
            "api_key": "new-key",
            "default_model": "model-b",
            "models": ["model-a", "model-b", "model-a", ""],
            "user_agent": "test-agent",
            "timeout_seconds": 45,
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


def test_aiproxy_config_path_takes_precedence_over_legacy_env(monkeypatch, tmp_path):
    legacy_path = tmp_path / "legacy.json"
    aiproxy_path = tmp_path / "aiproxy.json"
    monkeypatch.setenv("CODEXPROXY_CONFIG_PATH", str(legacy_path))
    monkeypatch.setenv("AIPROXY_CONFIG_PATH", str(aiproxy_path))

    assert runtime_config_path() == aiproxy_path


def test_legacy_codexproxy_config_path_still_works(monkeypatch, tmp_path):
    legacy_path = tmp_path / "legacy.json"
    monkeypatch.delenv("AIPROXY_CONFIG_PATH", raising=False)
    monkeypatch.setenv("CODEXPROXY_CONFIG_PATH", str(legacy_path))

    assert runtime_config_path() == legacy_path


def test_edit_profile_keeps_existing_api_key_when_blank(monkeypatch, tmp_path):
    config_path = tmp_path / "config.local.json"
    monkeypatch.setenv("CODEXPROXY_CONFIG_PATH", str(config_path))

    client = TestClient(create_app())
    assert client.post(
        "/admin/profiles/first",
        json={
            "name": "first",
            "base_url": "https://first.example/v1",
            "api_key": "secret-key",
            "default_model": "model-a",
            "models": "model-a",
        },
    ).status_code == 200

    response = client.post(
        "/admin/profiles/first",
        json={
            "name": "second",
            "base_url": "https://second.example/v1",
            "api_key": "",
            "default_model": "model-b",
            "models": "model-b",
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
    assert settings.upstream_api_style == "auto"

    response = client.get("/v1/models")
    assert response.json()["data"] == [
        {"id": "aicoego-default", "object": "model", "owned_by": "AiCoeGo"},
        {"id": "aicoego-long", "object": "model", "owned_by": "AiCoeGo"},
    ]


def test_claude_active_profile_is_independent_from_codex(monkeypatch, tmp_path):
    config_path = tmp_path / "config.local.json"
    monkeypatch.setenv("CODEXPROXY_CONFIG_PATH", str(config_path))

    client = TestClient(create_app())
    client.post(
        "/admin/profiles/codex-provider",
        json={
            "name": "Codex Provider",
            "base_url": "https://codex.example",
            "api_key": "codex-key",
            "default_model": "codex-model",
            "models": "codex-model",
        },
    )
    client.post(
        "/admin/profiles/claude-provider",
        json={
            "name": "Claude Provider",
            "base_url": "https://claude.example",
            "api_key": "claude-key",
            "default_model": "glm-5.1",
            "models": "glm-5.1",
        },
    )

    response = client.post(
        "/admin/claude/config",
        json={
            "active_profile": "claude-provider",
            "model_mappings": [{"claude_model": "claude-opus-4.6", "upstream_model": "glm-5.1"}],
        },
    )

    assert response.status_code == 200
    config = client.get("/admin/config").json()["config"]
    assert config["active_profile"] == "claude-provider"
    assert config["codex"]["active_profile"] == "claude-provider"
    assert config["claude"]["active_profile"] == "claude-provider"

    client.post("/admin/profiles/codex-provider/activate")
    config = client.get("/admin/config").json()["config"]
    assert config["codex"]["active_profile"] == "codex-provider"
    assert config["claude"]["active_profile"] == "claude-provider"


def test_claude_messages_maps_model_and_rewrites_response_model(monkeypatch, tmp_path):
    config_path = tmp_path / "config.local.json"
    monkeypatch.setenv("CODEXPROXY_CONFIG_PATH", str(config_path))
    captured = {}

    async def fake_post(self, url, headers, json):
        captured["url"] = url
        captured["headers"] = headers
        captured["payload"] = json
        return httpx.Response(
            200,
            json={
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "glm-5.1",
                "content": [{"type": "text", "text": "ok"}],
            },
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    client = TestClient(create_app())
    client.post(
        "/admin/profiles/byte",
        json={
            "name": "字节跳动",
            "base_url": "https://ark.cn-beijing.volces.com/api/coding/v1",
            "api_key": "byte-key",
            "default_model": "glm-5.1",
            "models": "glm-5.1",
        },
    )
    client.post(
        "/admin/claude/config",
        json={
            "active_profile": "byte",
            "model_mappings": [{"claude_model": "claude-opus-4.6", "upstream_model": "glm-5.1"}],
        },
    )

    response = client.post(
        "/anthropic/v1/messages",
        headers={"anthropic-version": "2023-06-01", "anthropic-beta": "test-beta", "x-api-key": "local-key"},
        json={
            "model": "claude-opus-4.6",
            "max_tokens": 8,
            "messages": [{"role": "user", "content": "ping"}],
        },
    )

    assert response.status_code == 200
    assert captured["url"] == "https://ark.cn-beijing.volces.com/api/coding/v1/messages"
    assert captured["payload"]["model"] == "glm-5.1"
    assert captured["payload"]["messages"] == [{"role": "user", "content": "ping"}]
    assert captured["headers"]["Authorization"] == "Bearer byte-key"
    assert captured["headers"]["anthropic-version"] == "2023-06-01"
    assert captured["headers"]["anthropic-beta"] == "test-beta"
    assert "x-api-key" not in captured["headers"]
    assert response.json()["model"] == "claude-opus-4.6"


def test_claude_chat_protocol_uses_chat_completions(monkeypatch, tmp_path):
    config_path = tmp_path / "config.local.json"
    monkeypatch.setenv("CODEXPROXY_CONFIG_PATH", str(config_path))
    captured = {}

    async def fake_post(self, url, headers, json):
        captured["url"] = url
        captured["headers"] = headers
        captured["payload"] = json
        return httpx.Response(
            200,
            json={
                "id": "chat_1",
                "model": "glm-5.1",
                "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            },
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    client = TestClient(create_app())
    client.post(
        "/admin/profiles/aigocode",
        json={
            "name": "AiGoCode",
            "base_url": "https://api.aigocode.com/v1",
            "api_key": "aigocode-key",
            "default_model": "glm-5.1",
            "models": "glm-5.1",
        },
    )
    client.post(
        "/admin/claude/config",
        json={
            "active_profile": "aigocode",
            "api_style": "chat",
            "model_mappings": [{"claude_model": "claude-opus-4.6", "upstream_model": "glm-5.1"}],
        },
    )

    response = client.post(
        "/anthropic/v1/messages",
        headers={"anthropic-version": "2023-06-01", "anthropic-beta": "test-beta"},
        json={
            "model": "claude-opus-4.6",
            "max_tokens": 8,
            "system": "follow system",
            "messages": [{"role": "user", "content": [{"type": "text", "text": "ping"}]}],
        },
    )

    assert response.status_code == 200
    assert captured["url"] == "https://api.aigocode.com/v1/chat/completions"
    assert captured["payload"]["model"] == "glm-5.1"
    assert captured["payload"]["messages"] == [
        {"role": "system", "content": "follow system"},
        {"role": "user", "content": "ping"},
    ]
    assert "anthropic-version" not in captured["headers"]
    assert "anthropic-beta" not in captured["headers"]
    body = response.json()
    assert body["model"] == "claude-opus-4.6"
    assert body["content"] == [{"type": "text", "text": "ok"}]


def test_claude_auto_protocol_falls_back_to_chat_when_messages_unsupported(monkeypatch, tmp_path):
    config_path = tmp_path / "config.local.json"
    monkeypatch.setenv("CODEXPROXY_CONFIG_PATH", str(config_path))
    calls = []

    async def fake_post(self, url, headers, json):
        calls.append(url)
        if url.endswith("/messages"):
            return httpx.Response(
                403,
                json={"error": {"message": "This group does not allow /v1/messages dispatch", "type": "permission_error"}, "type": "error"},
            )
        return httpx.Response(
            200,
            json={
                "id": "chat_1",
                "model": "glm-5.1",
                "choices": [{"message": {"role": "assistant", "content": "fallback ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            },
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    client = TestClient(create_app())
    client.post(
        "/admin/profiles/aigocode",
        json={
            "name": "AiGoCode",
            "base_url": "https://api.aigocode.com/v1",
            "api_key": "aigocode-key",
            "default_model": "glm-5.1",
            "models": "glm-5.1",
        },
    )
    client.post(
        "/admin/claude/config",
        json={
            "active_profile": "aigocode",
            "api_style": "auto",
            "model_mappings": [{"claude_model": "claude-opus-4.6", "upstream_model": "glm-5.1"}],
        },
    )

    response = client.post(
        "/anthropic/v1/messages",
        json={
            "model": "claude-opus-4.6",
            "max_tokens": 8,
            "messages": [{"role": "user", "content": "ping"}],
        },
    )

    assert response.status_code == 200
    assert calls == [
        "https://api.aigocode.com/v1/messages",
        "https://api.aigocode.com/v1/chat/completions",
    ]
    assert response.json()["content"] == [{"type": "text", "text": "fallback ok"}]


def test_claude_models_expose_mapped_claude_names(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEXPROXY_CONFIG_PATH", str(tmp_path / "config.local.json"))

    client = TestClient(create_app())
    client.post(
        "/admin/profiles/byte",
        json={
            "name": "字节跳动",
            "base_url": "https://ark.cn-beijing.volces.com/api/coding/v1",
            "api_key": "byte-key",
            "default_model": "glm-5.1",
            "models": "glm-5.1",
        },
    )
    client.post(
        "/admin/claude/config",
        json={
            "active_profile": "byte",
            "model_mappings": [
                {"claude_model": "claude-opus-4.6", "upstream_model": "glm-5.1"},
                {"claude_model": "claude-sonnet-*", "upstream_model": "glm-5.1"},
            ],
        },
    )

    response = client.get("/anthropic/v1/models")

    assert response.status_code == 200
    assert response.json()["data"] == [
        {"id": "claude-opus-4.6", "type": "model", "display_name": "claude-opus-4.6"},
        {"id": "claude-sonnet-*", "type": "model", "display_name": "claude-sonnet-*"},
    ]


def test_claude_messages_rejects_unmapped_model(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEXPROXY_CONFIG_PATH", str(tmp_path / "config.local.json"))

    client = TestClient(create_app())
    client.post(
        "/admin/profiles/byte",
        json={
            "name": "字节跳动",
            "base_url": "https://ark.cn-beijing.volces.com/api/coding/v1",
            "api_key": "byte-key",
            "default_model": "glm-5.1",
            "models": "glm-5.1",
        },
    )
    client.post(
        "/admin/claude/config",
        json={
            "active_profile": "byte",
            "model_mappings": [{"claude_model": "claude-opus-4.6", "upstream_model": "glm-5.1"}],
        },
    )

    response = client.post(
        "/anthropic/v1/messages",
        json={"model": "claude-unmapped-1", "max_tokens": 1, "messages": [{"role": "user", "content": "ping"}]},
    )

    assert response.status_code == 400
    assert "Claude model mapping is not configured" in response.json()["detail"]


def test_claude_model_exact_mapping_takes_precedence_over_wildcard():
    mappings = [
        {"claude_model": "claude-opus-*", "upstream_model": "fallback-model"},
        {"claude_model": "claude-opus-4.6", "upstream_model": "glm-5.1"},
    ]

    assert resolve_claude_model("claude-opus-4.6", mappings) == "glm-5.1"
    assert resolve_claude_model("claude-opus-4.7", mappings) == "fallback-model"


def test_claude_protocol_check_uses_selected_chat_protocol(monkeypatch, tmp_path):
    config_path = tmp_path / "config.local.json"
    monkeypatch.setenv("CODEXPROXY_CONFIG_PATH", str(config_path))

    async def fake_post(self, url, headers, json):
        assert url == "https://api.aigocode.com/v1/chat/completions"
        return httpx.Response(
            200,
            json={
                "id": "chat_1",
                "model": "glm-5.1",
                "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            },
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    client = TestClient(create_app())
    client.post(
        "/admin/profiles/aigocode",
        json={
            "name": "AiGoCode",
            "base_url": "https://api.aigocode.com/v1",
            "api_key": "aigocode-key",
            "default_model": "glm-5.1",
            "models": "glm-5.1",
        },
    )
    client.post(
        "/admin/claude/config",
        json={
            "active_profile": "aigocode",
            "api_style": "chat",
            "model_mappings": [{"claude_model": "claude-opus-4.6", "upstream_model": "glm-5.1"}],
        },
    )

    response = client.post("/admin/claude/protocol/check", json={"model": "claude-opus-4.6"})

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["upstream"]["messages_url"] == "https://api.aigocode.com/v1/chat/completions"
    assert body["response_model"] == "claude-opus-4.6"


def test_admin_config_exposes_api_style(monkeypatch, tmp_path):
    config_path = tmp_path / "config.local.json"
    monkeypatch.setenv("CODEXPROXY_CONFIG_PATH", str(config_path))

    client = TestClient(create_app())
    client.post(
        "/admin/profiles/responses-provider",
        json={
            "name": "Responses Provider",
            "base_url": "https://responses.example",
            "api_key": "test-key",
            "default_model": "gpt-5.5",
            "models": "gpt-5.5",
        },
    )

    response = client.get("/admin/config")

    assert response.status_code == 200
    body = response.json()["config"]
    assert body["codex"]["api_style"] == "auto"
    assert body["claude"]["api_style"] == "auto"
    assert "api_style" not in body["active"]
    assert "api_style" not in body["profiles"]["responses-provider"]


def test_update_codex_config_changes_active_profile_and_api_style(monkeypatch, tmp_path):
    config_path = tmp_path / "config.local.json"
    monkeypatch.setenv("CODEXPROXY_CONFIG_PATH", str(config_path))

    client = TestClient(create_app())
    client.post(
        "/admin/profiles/uocode",
        json={
            "name": "UoCode",
            "base_url": "https://www.uocode.com",
            "api_key": "uocode-key",
            "default_model": "gpt-5.5",
            "models": "gpt-5.5",
        },
    )
    client.post(
        "/admin/profiles/aicoego",
        json={
            "name": "AiCoeGo",
            "base_url": "https://aicoego.example",
            "api_key": "aicoego-key",
            "default_model": "gpt-5.4",
            "models": "gpt-5.4",
        },
    )
    initial_claude_profile = client.get("/admin/config").json()["config"]["claude"]["active_profile"]

    response = client.post(
        "/admin/codex/config",
        json={"active_profile": "aicoego", "api_style": "responses"},
    )

    assert response.status_code == 200
    body = response.json()["config"]
    assert body["codex"]["active_profile"] == "aicoego"
    assert body["codex"]["api_style"] == "responses"
    assert body["claude"]["active_profile"] == initial_claude_profile
    assert get_settings().upstream_api_style == "responses"


def test_codex_protocol_check_uses_selected_responses_protocol(monkeypatch, tmp_path):
    config_path = tmp_path / "config.local.json"
    monkeypatch.setenv("CODEXPROXY_CONFIG_PATH", str(config_path))

    async def fake_post(self, url, headers, json):
        assert url == "https://api.aigocode.com/v1/responses"
        assert json["model"] == "gpt-5.4"
        assert json["input"] == "ping"
        assert json["stream"] is False
        return httpx.Response(
            200,
            json={
                "id": "resp_1",
                "object": "response",
                "status": "completed",
                "model": "gpt-5.4",
                "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "ok"}]}],
            },
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    client = TestClient(create_app())
    client.post(
        "/admin/profiles/aigocode",
        json={
            "name": "AiGoCode",
            "base_url": "https://api.aigocode.com/v1",
            "api_key": "aigocode-key",
            "default_model": "gpt-5.4",
            "models": "gpt-5.4",
        },
    )
    client.post("/admin/codex/config", json={"active_profile": "aigocode", "api_style": "responses"})

    response = client.post("/admin/codex/protocol/check")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["upstream"]["resolved_api_style"] == "responses"
    assert body["upstream"]["protocol_url"] == "https://api.aigocode.com/v1/responses"
    assert body["upstream"]["model"] == "gpt-5.4"


def test_codex_protocol_check_uses_selected_chat_protocol(monkeypatch, tmp_path):
    config_path = tmp_path / "config.local.json"
    monkeypatch.setenv("CODEXPROXY_CONFIG_PATH", str(config_path))

    async def fake_post(self, url, headers, json):
        assert url == "https://api.aigocode.com/v1/chat/completions"
        assert json["model"] == "gpt-5.4"
        assert json["messages"] == [{"role": "user", "content": "ping"}]
        assert json["stream"] is False
        return httpx.Response(
            200,
            json={
                "id": "chat_1",
                "model": "gpt-5.4",
                "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    client = TestClient(create_app())
    client.post(
        "/admin/profiles/aigocode",
        json={
            "name": "AiGoCode",
            "base_url": "https://api.aigocode.com/v1",
            "api_key": "aigocode-key",
            "default_model": "gpt-5.4",
            "models": "gpt-5.4",
        },
    )
    client.post("/admin/codex/config", json={"active_profile": "aigocode", "api_style": "chat"})

    response = client.post("/admin/codex/protocol/check")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["upstream"]["resolved_api_style"] == "chat"
    assert body["upstream"]["protocol_url"] == "https://api.aigocode.com/v1/chat/completions"
    assert body["upstream"]["model"] == "gpt-5.4"


def test_codex_protocol_check_auto_falls_back_to_chat(monkeypatch, tmp_path):
    config_path = tmp_path / "config.local.json"
    monkeypatch.setenv("CODEXPROXY_CONFIG_PATH", str(config_path))
    calls = []

    async def fake_post(self, url, headers, json):
        calls.append(url)
        if url.endswith("/responses"):
            return httpx.Response(404, text="not found", headers={"content-type": "text/plain"})
        return httpx.Response(
            200,
            json={
                "id": "chat_1",
                "model": "gpt-5.4",
                "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    client = TestClient(create_app())
    client.post(
        "/admin/profiles/aigocode",
        json={
            "name": "AiGoCode",
            "base_url": "https://api.aigocode.com/v1",
            "api_key": "aigocode-key",
            "default_model": "gpt-5.4",
            "models": "gpt-5.4",
        },
    )
    client.post("/admin/codex/config", json={"active_profile": "aigocode", "api_style": "auto"})

    response = client.post("/admin/codex/protocol/check")

    assert response.status_code == 200
    assert calls == [
        "https://api.aigocode.com/v1/responses",
        "https://api.aigocode.com/v1/chat/completions",
    ]
    body = response.json()
    assert body["ok"] is True
    assert body["upstream"]["resolved_api_style"] == "chat"
    assert body["upstream"]["protocol_url"] == "https://api.aigocode.com/v1/chat/completions"


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


def test_refresh_models_reports_compact_upstream_html_error(monkeypatch, tmp_path):
    config_path = tmp_path / "config.local.json"
    monkeypatch.setenv("CODEXPROXY_CONFIG_PATH", str(config_path))

    async def fake_get(self, url, headers):
        return httpx.Response(
            404,
            text="<!DOCTYPE html><html><head><title>404</title></head><body>not found</body></html>",
            headers={"content-type": "text/html; charset=utf-8"},
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    client = TestClient(create_app())
    client.post(
        "/admin/profiles/aigocode",
        json={
            "name": "AIGoCode",
            "base_url": "https://api.aigocode.com",
            "api_key": "aigocode-key",
            "default_model": "gpt-5.5",
            "models": "gpt-5.5",
        },
    )

    response = client.post("/admin/models/refresh")

    assert response.status_code == 502
    assert response.json()["detail"] == (
        "Upstream request failed: GET https://api.aigocode.com/v1/models -> 404 Not Found (text/html). HTML page returned"
    )
    assert get_settings().upstream_models == ["gpt-5.5"]


def test_draft_refresh_models_does_not_change_active_profiles(monkeypatch, tmp_path):
    config_path = tmp_path / "config.local.json"
    monkeypatch.setenv("CODEXPROXY_CONFIG_PATH", str(config_path))

    async def fake_get(self, url, headers):
        assert url == "https://draft.example/v1/models"
        assert headers["Authorization"] == "Bearer draft-key"
        return httpx.Response(200, json={"data": [{"id": "draft-model"}]})

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    client = TestClient(create_app())
    client.post(
        "/admin/profiles/codex",
        json={
            "name": "Codex",
            "base_url": "https://codex.example",
            "api_key": "codex-key",
            "default_model": "codex-model",
            "models": "codex-model",
        },
    )
    client.post(
        "/admin/profiles/claude",
        json={
            "name": "Claude",
            "base_url": "https://claude.example",
            "api_key": "claude-key",
            "default_model": "claude-model",
            "models": "claude-model",
        },
    )
    client.post("/admin/claude/config", json={"active_profile": "claude"})

    response = client.post(
        "/admin/profiles/draft/models/refresh",
        json={
            "name": "Draft",
            "base_url": "https://draft.example",
            "api_key": "draft-key",
            "default_model": "draft-model",
            "models": "draft-model",
        },
    )

    assert response.status_code == 200
    assert response.json()["models"] == ["draft-model"]
    config = client.get("/admin/config").json()["config"]
    assert config["codex"]["active_profile"] == "claude"
    assert config["claude"]["active_profile"] == "claude"


def test_draft_protocol_check_does_not_change_active_profiles(monkeypatch, tmp_path):
    config_path = tmp_path / "config.local.json"
    monkeypatch.setenv("CODEXPROXY_CONFIG_PATH", str(config_path))

    async def fake_get(self, url, headers):
        assert url == "https://draft.example/v1/models"
        return httpx.Response(200, json={"data": [{"id": "draft-model"}]})

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    client = TestClient(create_app())
    client.post(
        "/admin/profiles/codex",
        json={
            "name": "Codex",
            "base_url": "https://codex.example",
            "api_key": "codex-key",
            "default_model": "codex-model",
            "models": "codex-model",
        },
    )

    response = client.post(
        "/admin/profiles/draft/protocol/check",
        json={
            "name": "Draft",
            "base_url": "https://draft.example",
            "api_key": "draft-key",
            "default_model": "draft-model",
            "models": "draft-model",
        },
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["upstream"]["models_count"] == 1
    assert "configured_api_style" not in response.json()["upstream"]
    assert "resolved_api_style" not in response.json()["upstream"]
    assert client.get("/admin/config").json()["config"]["codex"]["active_profile"] == "codex"


def test_admin_page_uses_searchable_model_picker_and_aiproxy_labels():
    client = TestClient(create_app())

    response = client.get("/")

    assert response.status_code == 200
    text = response.text
    assert "AI Proxy管理" in text
    assert "CodexProxy 管理" not in text
    assert "添加中转平台" in text
    assert "修改当前平台" in text
    assert "profileDialog" in text
    assert 'id="modelMenu"' in text
    assert 'renderModelMenu' in text
    assert 'showAllModels' in text
    assert 'setRefreshLoading' in text
    assert '正在刷新模型' in text
    assert "保存并生效" in text
    assert "保存映射" in text
    assert "修改Codex配置" in text
    assert "验证中..." in text
    assert "保存中..." in text
    assert "修改中..." in text
    assert "恢复中..." in text
    assert 'id="checkCodexProtocol"' in text
    assert 'id="checkClaudeProtocol"' in text
    assert 'id="checkClaudeModel"' in text
    assert 'id="codexProtocolStatus"' in text
    assert 'id="claudeProtocolStatus"' in text
    assert 'id="claudeModelStatus"' in text
    assert 'id="claudeStatus"' in text
    assert '.status:empty' in text
    assert '.status-row' in text
    assert "v1/responses" in text
    assert "v1/chat/completions" in text
    assert "v1/messages" in text
    assert "Auto" in text
    assert "验证模型" in text
    assert "CodexProxy 配置" not in text
    assert "ClaudeProxy Gateway 配置" not in text
    assert "验证 ClaudeProxy" not in text
    assert "Responses" not in text
    assert "Chat Completions" not in text
    assert "Anthropic Messages" not in text
    assert "自动适配" not in text
    assert text.count("选择中转平台") == 2
    assert "Claude 使用的中转平台" not in text
    assert "选择后自动生效。" in text
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
    assert body["codex_desktop"]["base_url"] == "http://127.0.0.1:8383/v1"
    assert body["upstream"]["models_ok"] is True
    assert body["upstream"]["models_url"] == "https://www.uocode.com/v1/models"
    assert "configured_api_style" not in body["upstream"]
    assert "resolved_api_style" not in body["upstream"]
    assert "responses_url" not in body["upstream"]


def test_protocol_check_reports_compact_upstream_html_error(monkeypatch, tmp_path):
    config_path = tmp_path / "config.local.json"
    monkeypatch.setenv("CODEXPROXY_CONFIG_PATH", str(config_path))

    async def fake_get(self, url, headers):
        return httpx.Response(
            404,
            text="<!DOCTYPE html><html><body>not found</body></html>",
            headers={"content-type": "text/html; charset=utf-8"},
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    client = TestClient(create_app())
    client.post(
        "/admin/profiles/aigocode",
        json={
            "name": "AIGoCode",
            "base_url": "https://api.aigocode.com",
            "api_key": "aigocode-key",
            "default_model": "gpt-5.5",
            "models": "gpt-5.5",
        },
    )

    response = client.post("/admin/protocol/check")

    assert response.status_code == 502
    assert response.json()["error"] == (
        "Upstream request failed: GET https://api.aigocode.com/v1/models -> 404 Not Found (text/html). HTML page returned"
    )
