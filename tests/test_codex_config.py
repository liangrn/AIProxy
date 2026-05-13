from pathlib import Path

from scripts.codex_config import install, restore, status
from app.main import create_app
from fastapi.testclient import TestClient


def test_install_replaces_top_level_model_and_adds_provider(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(
        'model = "gpt-5.5"\n\n[features]\nmulti_agent = true\n',
        encoding="utf-8",
    )

    backup = install(config, model="gpt-5.4", proxy_url="http://127.0.0.1:8383")

    text = config.read_text(encoding="utf-8")
    assert backup.exists()
    assert 'model_provider = "codex_proxy"' in text
    assert 'model = "gpt-5.4"' in text
    assert '[model_providers.codex_proxy]' in text
    assert 'base_url = "http://127.0.0.1:8383/v1"' in text
    assert "[features]" in text
    assert status(config) == "installed"
    assert backup.name == "config.toml.codexproxy-original-backup"


def test_install_openai_compatible_mode_is_rejected(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(
        'model_provider = "openai"\nmodel = "gpt-5.5"\n\n[model_providers.openai]\nname = "OpenAI"\nbase_url = "https://api.openai.com/v1"\nwire_api = "responses"\nrequires_openai_auth = true\n',
        encoding="utf-8",
    )

    try:
        install(config, model="gpt-5.4", proxy_url="http://127.0.0.1:8383", mode="openai-compatible")
    except ValueError as exc:
        assert "reserved built-in provider" in str(exc)
    else:
        raise AssertionError("openai-compatible mode should be rejected")

    text = config.read_text(encoding="utf-8")
    assert 'model_provider = "openai"' in text
    assert 'model = "gpt-5.5"' in text
    assert text.count("[model_providers.openai]") == 1
    assert 'base_url = "https://api.openai.com/v1"' in text
    assert 'requires_openai_auth = true' in text


def test_install_auth_proxy_keeps_custom_provider_and_requires_openai_auth(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(
        'model_provider = "openai"\n'
        'model = "gpt-5.5"\n\n'
        '[model_providers.openai]\n'
        'name = "OpenAI"\n'
        'base_url = "https://api.openai.com/v1"\n'
        'wire_api = "responses"\n'
        'requires_openai_auth = true\n',
        encoding="utf-8",
    )

    install(config, model="gpt-5.4", proxy_url="http://127.0.0.1:8383", mode="auth-proxy")

    text = config.read_text(encoding="utf-8")
    assert 'model_provider = "codex_proxy"' in text
    assert '[model_providers.codex_proxy]' in text
    assert '[model_providers.openai]' in text
    assert 'base_url = "http://127.0.0.1:8383/v1"' in text
    assert 'requires_openai_auth = true' in text
    assert text.count("[model_providers.openai]") == 1


def test_restore_uses_original_backup(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('model = "gpt-5.5"\n', encoding="utf-8")

    install(config, model="gpt-5.4", proxy_url="http://127.0.0.1:8383")
    assert 'model_provider = "codex_proxy"' in config.read_text(encoding="utf-8")

    restored_from = restore(config)

    assert restored_from.exists()
    assert config.read_text(encoding="utf-8") == 'model = "gpt-5.5"\n'


def test_restore_returns_original_config_after_multiple_installs(tmp_path):
    config = tmp_path / "config.toml"
    original = (
        'model_provider = "openai"\n'
        'model = "gpt-5.5"\n\n'
        '[model_providers.openai]\n'
        'name = "OpenAI"\n'
        'base_url = "https://api.openai.com/v1"\n'
        'wire_api = "responses"\n'
        'requires_openai_auth = true\n'
    )
    config.write_text(original, encoding="utf-8")

    install(config, model="gpt-5.4", proxy_url="http://127.0.0.1:8383", mode="proxy")
    install(config, model="gpt-5.3-codex", proxy_url="http://127.0.0.1:8383", mode="proxy")
    restored_from = restore(config)

    assert restored_from.exists()
    assert config.read_text(encoding="utf-8") == original


def test_install_does_not_create_timestamp_backups(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('model = "gpt-5.5"\n', encoding="utf-8")

    install(config, model="gpt-5.4", proxy_url="http://127.0.0.1:8383")
    install(config, model="gpt-5.3-codex", proxy_url="http://127.0.0.1:8383")

    backups = list(tmp_path.glob("config.toml.codexproxy-backup-*"))
    assert backups == []
    assert (tmp_path / "config.toml.codexproxy-original-backup").exists()


def test_admin_codex_desktop_install_and_restore(monkeypatch, tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('model = "gpt-5.5"\n', encoding="utf-8")
    monkeypatch.setenv("CODEX_CONFIG_PATH", str(config))

    client = TestClient(create_app())

    install_response = client.post(
        "/admin/codex/install",
        json={"mode": "proxy", "proxy_url": "http://127.0.0.1:8383"},
    )
    assert install_response.status_code == 200
    assert install_response.json()["status"] == "installed"
    assert 'base_url = "http://127.0.0.1:8383/v1"' in config.read_text(encoding="utf-8")

    status_response = client.get("/admin/codex/status")
    assert status_response.status_code == 200
    assert status_response.json()["status"] == "installed"
    assert status_response.json()["backup_available"] is True

    restore_response = client.post("/admin/codex/restore")
    assert restore_response.status_code == 200
    assert restore_response.json()["status"] == "restored"
    assert config.read_text(encoding="utf-8") == 'model = "gpt-5.5"\n'


def test_admin_codex_desktop_install_uses_active_profile_default_model(monkeypatch, tmp_path):
    runtime_config = tmp_path / "config.local.json"
    codex_config = tmp_path / "config.toml"
    codex_config.write_text('model = "gpt-5.5"\n', encoding="utf-8")
    monkeypatch.setenv("CODEXPROXY_CONFIG_PATH", str(runtime_config))
    monkeypatch.setenv("CODEX_CONFIG_PATH", str(codex_config))

    client = TestClient(create_app())
    client.post(
        "/admin/profiles/aicoego",
        json={
            "name": "AiCoeGo",
            "base_url": "https://aicoego.example",
            "api_key": "key",
            "default_model": "aicoego-default",
            "models": "aicoego-default",
        },
    )

    response = client.post("/admin/codex/install", json={"proxy_url": "http://127.0.0.1:8383"})

    assert response.status_code == 200
    assert 'model = "aicoego-default"' in codex_config.read_text(encoding="utf-8")


def test_admin_codex_desktop_install_openai_compatible_mode_is_rejected(monkeypatch, tmp_path):
    runtime_config = tmp_path / "config.local.json"
    codex_config = tmp_path / "config.toml"
    codex_config.write_text(
        'model_provider = "openai"\nmodel = "gpt-5.5"\n\n[model_providers.openai]\nname = "OpenAI"\nbase_url = "https://api.openai.com/v1"\nwire_api = "responses"\nrequires_openai_auth = true\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEXPROXY_CONFIG_PATH", str(runtime_config))
    monkeypatch.setenv("CODEX_CONFIG_PATH", str(codex_config))

    client = TestClient(create_app())
    client.post(
        "/admin/profiles/example-provider",
        json={
            "name": "Example Provider",
            "base_url": "https://example.com",
            "api_key": "key",
            "default_model": "gpt-5.5",
            "models": "gpt-5.5",
        },
    )

    response = client.post(
        "/admin/codex/install",
        json={"mode": "openai-compatible", "proxy_url": "http://127.0.0.1:8383"},
    )

    assert response.status_code == 400
    assert "reserved built-in provider" in response.json()["detail"]
    text = codex_config.read_text(encoding="utf-8")
    assert 'model_provider = "openai"' in text
    assert text.count("[model_providers.openai]") == 1
    assert 'base_url = "https://api.openai.com/v1"' in text
    assert 'requires_openai_auth = true' in text


def test_status_is_not_installed_when_managed_block_exists_but_provider_switched_away(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(
        'model_provider = "other"\n'
        'model = "gpt-5.5"\n\n'
        '# BEGIN CODEXPROXY MANAGED BLOCK\n'
        '[model_providers.codex_proxy]\n'
        'name = "AI Proxy"\n'
        'base_url = "http://127.0.0.1:8383/v1"\n'
        'wire_api = "responses"\n'
        'requires_openai_auth = false\n'
        'stream_max_retries = 0\n'
        '# END CODEXPROXY MANAGED BLOCK\n',
        encoding="utf-8",
    )

    assert status(config) == "not-installed"
