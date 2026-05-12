from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
from typing import Any


DEFAULT_USER_AGENT = "curl/8.7.1"
DEFAULT_TIMEOUT_SECONDS = 300.0


@dataclass(frozen=True)
class Settings:
    upstream_provider_name: str
    upstream_base_url: str
    upstream_api_key: str
    upstream_model: str
    upstream_models: list[str]
    upstream_user_agent: str
    request_timeout_seconds: float
    listen_host: str
    listen_port: int


def get_settings() -> Settings:
    config = normalized_runtime_config()
    profile = active_profile(config)
    return Settings(
        upstream_provider_name=profile["name"],
        upstream_base_url=profile["base_url"],
        upstream_api_key=profile["api_key"],
        upstream_model=profile["default_model"],
        upstream_models=profile["models"],
        upstream_user_agent=profile["user_agent"],
        request_timeout_seconds=float(profile["timeout_seconds"]),
        listen_host=os.getenv("LISTEN_HOST", "127.0.0.1"),
        listen_port=int(os.getenv("LISTEN_PORT", "8383")),
    )


def parse_csv(value: str) -> list[str]:
    items = []
    for item in value.split(","):
        normalized = item.strip()
        if normalized and normalized not in items:
            items.append(normalized)
    return items


def runtime_config_path() -> Path:
    configured = os.getenv("CODEXPROXY_CONFIG_PATH")
    if configured:
        return Path(configured).expanduser()
    return Path.cwd() / "config.local.json"


def load_runtime_config() -> dict[str, Any]:
    path = runtime_config_path()
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def normalize_models(value: Any) -> list[str]:
    if isinstance(value, list):
        return parse_csv(",".join(str(item) for item in value))
    return parse_csv(str(value))


def normalize_base_url(value: str) -> str:
    base_url = value.strip().rstrip("/")
    if not base_url:
        return "https://www.uocode.com/v1"
    if base_url.endswith("/v1"):
        return base_url
    return f"{base_url}/v1"


def display_base_url(value: str) -> str:
    base_url = value.strip().rstrip("/")
    if base_url.endswith("/v1"):
        return base_url[:-3]
    return base_url


def profile_id_from_name(name: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return normalized or "default"


def default_profile() -> dict[str, Any]:
    upstream_model = os.getenv("UPSTREAM_MODEL", "gpt-5.5")
    return normalize_profile(
        {
            "name": os.getenv("UPSTREAM_PROVIDER_NAME", "openai-compatible"),
            "base_url": os.getenv("UPSTREAM_BASE_URL", "https://www.uocode.com/v1"),
            "api_key": os.getenv("UPSTREAM_API_KEY", ""),
            "default_model": upstream_model,
            "models": os.getenv("UPSTREAM_MODELS", f"{upstream_model},gpt-5.5,gpt-5.4"),
            "user_agent": os.getenv("UPSTREAM_USER_AGENT", DEFAULT_USER_AGENT),
            "timeout_seconds": os.getenv("REQUEST_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS)),
        },
        ensure_default_model=False,
    )


def normalize_profile(data: dict[str, Any], current: dict[str, Any] | None = None, ensure_default_model: bool = True) -> dict[str, Any]:
    current = current or {}
    default_model = str(
        data.get("default_model")
        or data.get("upstream_model")
        or current.get("default_model")
        or current.get("upstream_model")
        or "gpt-5.5"
    ).strip()
    models = normalize_models(data.get("models") or data.get("upstream_models") or current.get("models") or current.get("upstream_models") or default_model)
    if ensure_default_model and default_model and default_model not in models:
        models.insert(0, default_model)

    api_key = str(data.get("api_key") or data.get("upstream_api_key") or "").strip()
    if not api_key:
        api_key = str(current.get("api_key") or current.get("upstream_api_key") or "").strip()

    return {
        "name": str(data.get("name") or data.get("upstream_provider_name") or current.get("name") or current.get("upstream_provider_name") or "openai-compatible").strip(),
        "base_url": normalize_base_url(str(data.get("base_url") or data.get("upstream_base_url") or current.get("base_url") or current.get("upstream_base_url") or "https://www.uocode.com/v1")),
        "api_key": api_key,
        "default_model": default_model,
        "models": models,
        "user_agent": str(data.get("user_agent") or data.get("upstream_user_agent") or current.get("user_agent") or current.get("upstream_user_agent") or DEFAULT_USER_AGENT).strip(),
        "timeout_seconds": float(data.get("timeout_seconds") or data.get("request_timeout_seconds") or current.get("timeout_seconds") or current.get("request_timeout_seconds") or DEFAULT_TIMEOUT_SECONDS),
    }


def normalized_runtime_config() -> dict[str, Any]:
    raw = load_runtime_config()
    if raw.get("profiles"):
        profiles = {
            profile_id: normalize_profile(profile)
            for profile_id, profile in raw["profiles"].items()
        }
        active = str(raw.get("active_profile") or next(iter(profiles), "default"))
        if active not in profiles:
            active = next(iter(profiles), "default")
        return {"active_profile": active, "profiles": profiles}

    profile = default_profile() if not raw else normalize_profile(raw)
    profile_id = profile_id_from_name(profile["name"])
    return {"active_profile": profile_id, "profiles": {profile_id: profile}}


def active_profile(config: dict[str, Any] | None = None) -> dict[str, Any]:
    config = config or normalized_runtime_config()
    return config["profiles"][config["active_profile"]]


def save_runtime_config_document(config: dict[str, Any]) -> None:
    path = runtime_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def public_profile(profile: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": profile["name"],
        "base_url": display_base_url(profile["base_url"]),
        "api_key": profile["api_key"],
        "default_model": profile["default_model"],
        "models": profile["models"],
        "user_agent": profile["user_agent"],
        "timeout_seconds": profile["timeout_seconds"],
    }


def public_settings(settings: Settings | None = None) -> dict[str, Any]:
    config = normalized_runtime_config()
    settings = settings or get_settings()
    return {
        "active_profile": config["active_profile"],
        "profiles": {
            profile_id: public_profile(profile)
            for profile_id, profile in config["profiles"].items()
        },
        "active": public_profile(active_profile(config)),
        "upstream_provider_name": settings.upstream_provider_name,
        "upstream_base_url": settings.upstream_base_url,
        "upstream_api_key": settings.upstream_api_key,
        "upstream_model": settings.upstream_model,
        "upstream_models": settings.upstream_models,
        "upstream_user_agent": settings.upstream_user_agent,
        "request_timeout_seconds": settings.request_timeout_seconds,
        "listen_host": settings.listen_host,
        "listen_port": settings.listen_port,
    }


def save_runtime_config(data: dict[str, Any]) -> Settings:
    config = normalized_runtime_config()
    active = config["active_profile"]
    config["profiles"][active] = normalize_profile(data, current=config["profiles"][active])
    save_runtime_config_document(config)
    return get_settings()


def save_profile(profile_id: str, data: dict[str, Any]) -> dict[str, Any]:
    config = normalized_runtime_config()
    current = config["profiles"].get(profile_id)
    profile = normalize_profile(data, current=current)
    config["profiles"][profile_id] = profile
    config["active_profile"] = profile_id
    save_runtime_config_document(config)
    return public_profile(profile)


def activate_profile(profile_id: str) -> dict[str, Any] | None:
    config = normalized_runtime_config()
    if profile_id not in config["profiles"]:
        return None
    config["active_profile"] = profile_id
    save_runtime_config_document(config)
    return public_profile(config["profiles"][profile_id])


def update_active_profile_models(models: list[str]) -> dict[str, Any]:
    config = normalized_runtime_config()
    profile = config["profiles"][config["active_profile"]]
    profile["models"] = models
    if profile["default_model"] not in models and models:
        profile["default_model"] = models[0]
    save_runtime_config_document(config)
    return public_profile(profile)
