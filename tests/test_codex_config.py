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


def test_restore_uses_latest_backup(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('model = "gpt-5.5"\n', encoding="utf-8")

    install(config, model="gpt-5.4", proxy_url="http://127.0.0.1:8383")
    assert 'model_provider = "codex_proxy"' in config.read_text(encoding="utf-8")

    restored_from = restore(config)

    assert restored_from.exists()
    assert config.read_text(encoding="utf-8") == 'model = "gpt-5.5"\n'


def test_admin_codex_desktop_install_and_restore(monkeypatch, tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('model = "gpt-5.5"\n', encoding="utf-8")
    monkeypatch.setenv("CODEX_CONFIG_PATH", str(config))

    client = TestClient(create_app())

    install_response = client.post(
        "/admin/codex/install",
        json={"model": "gpt-5.4", "proxy_url": "http://127.0.0.1:8383"},
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
