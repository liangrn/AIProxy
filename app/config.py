from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
from fnmatch import fnmatchcase
from typing import Any


DEFAULT_USER_AGENT = "curl/8.7.1"
DEFAULT_TIMEOUT_SECONDS = 300.0


@dataclass(frozen=True)
class Settings:
    active_profile_id: str
    upstream_provider_name: str
    upstream_base_url: str
    upstream_api_key: str
    upstream_model: str
    upstream_models: list[str]
    upstream_api_style: str
    upstream_user_agent: str
    request_timeout_seconds: float
    listen_host: str
    listen_port: int


@dataclass(frozen=True)
class ClaudeSettings:
    active_profile_id: str
    upstream_provider_name: str
    upstream_base_url: str
    upstream_api_key: str
    upstream_model: str
    upstream_models: list[str]
    upstream_api_style: str
    upstream_user_agent: str
    request_timeout_seconds: float
    listen_host: str
    listen_port: int
    model_mappings: list[dict[str, str]]


def get_settings() -> Settings:
    config = normalized_runtime_config()
    profile = active_profile(config, "codex")
    return Settings(
        active_profile_id=config["codex"]["active_profile"],
        upstream_provider_name=profile["name"],
        upstream_base_url=profile["base_url"],
        upstream_api_key=profile["api_key"],
        upstream_model=profile["default_model"],
        upstream_models=profile["models"],
        upstream_api_style=config["codex"]["api_style"],
        upstream_user_agent=profile["user_agent"],
        request_timeout_seconds=float(profile["timeout_seconds"]),
        listen_host=os.getenv("LISTEN_HOST", "127.0.0.1"),
        listen_port=int(os.getenv("LISTEN_PORT", "8383")),
    )


def get_claude_settings() -> ClaudeSettings:
    config = normalized_runtime_config()
    profile = active_profile(config, "claude")
    return ClaudeSettings(
        active_profile_id=config["claude"]["active_profile"],
        upstream_provider_name=profile["name"],
        upstream_base_url=profile["base_url"],
        upstream_api_key=profile["api_key"],
        upstream_model=profile["default_model"],
        upstream_models=profile["models"],
        upstream_api_style=config["claude"]["api_style"],
        upstream_user_agent=profile["user_agent"],
        request_timeout_seconds=float(profile["timeout_seconds"]),
        listen_host=os.getenv("LISTEN_HOST", "127.0.0.1"),
        listen_port=int(os.getenv("LISTEN_PORT", "8383")),
        model_mappings=config["claude"]["model_mappings"],
    )


def parse_csv(value: str) -> list[str]:
    items = []
    for item in value.split(","):
        normalized = item.strip()
        if normalized and normalized not in items:
            items.append(normalized)
    return items


def runtime_config_path() -> Path:
    configured = os.getenv("AIPROXY_CONFIG_PATH") or os.getenv("CODEXPROXY_CONFIG_PATH")
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


def normalize_api_style(value: Any) -> str:
    api_style = str(value or "auto").strip().lower()
    if api_style not in {"auto", "chat", "responses"}:
        return "auto"
    return api_style


def normalize_claude_api_style(value: Any) -> str:
    api_style = str(value or "auto").strip().lower()
    if api_style not in {"anthropic", "chat", "auto"}:
        return "auto"
    return api_style


def default_claude_model_mappings() -> list[dict[str, str]]:
    return [
        {"claude_model": "claude-opus-4.6", "upstream_model": "glm-5.1"},
        {"claude_model": "claude-sonnet-4.6", "upstream_model": "glm-5.1"},
        {"claude_model": "claude-haiku-4.6", "upstream_model": "glm-5.1"},
    ]


def normalize_model_mappings(value: Any) -> list[dict[str, str]]:
    if isinstance(value, dict):
        iterable = [{"claude_model": key, "upstream_model": model} for key, model in value.items()]
    elif isinstance(value, list):
        iterable = value
    else:
        iterable = []

    mappings: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in iterable:
        if not isinstance(item, dict):
            continue
        claude_model = str(item.get("claude_model") or item.get("source") or item.get("from") or "").strip()
        upstream_model = str(item.get("upstream_model") or item.get("target") or item.get("to") or "").strip()
        if not claude_model or not upstream_model or claude_model in seen:
            continue
        seen.add(claude_model)
        mappings.append({"claude_model": claude_model, "upstream_model": upstream_model})
    return mappings


def resolve_claude_model(model: str, mappings: list[dict[str, str]]) -> str | None:
    for mapping in mappings:
        if mapping["claude_model"] == model:
            return mapping["upstream_model"]
    for mapping in mappings:
        pattern = mapping["claude_model"]
        if "*" in pattern and fnmatchcase(model, pattern):
            return mapping["upstream_model"]
    return None


def normalize_base_url(value: str) -> str:
    base_url = value.strip().rstrip("/")
    if not base_url:
        return "https://example.com/v1"
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
            "base_url": os.getenv("UPSTREAM_BASE_URL", "https://example.com/v1"),
            "api_key": os.getenv("UPSTREAM_API_KEY", ""),
            "default_model": upstream_model,
            "models": os.getenv("UPSTREAM_MODELS", f"{upstream_model},gpt-5.5,gpt-5.4"),
            "user_agent": os.getenv("UPSTREAM_USER_AGENT", DEFAULT_USER_AGENT),
            "timeout_seconds": os.getenv("REQUEST_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS)),
        },
        ensure_default_model=False,
    )


def normalize_profile(data: dict[str, Any], current: dict[str, Any] | None = None, ensure_default_model: bool = False) -> dict[str, Any]:
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
        "base_url": normalize_base_url(str(data.get("base_url") or data.get("upstream_base_url") or current.get("base_url") or current.get("upstream_base_url") or "https://example.com/v1")),
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
        fallback_active = str(raw.get("active_profile") or next(iter(profiles), "default"))
        if fallback_active not in profiles:
            fallback_active = next(iter(profiles), "default")
        codex_raw = raw.get("codex") if isinstance(raw.get("codex"), dict) else {}
        claude_raw = raw.get("claude") if isinstance(raw.get("claude"), dict) else {}
        codex_active = str(codex_raw.get("active_profile") or fallback_active)
        if codex_active not in profiles:
            codex_active = fallback_active
        byte_profile = next((profile_id for profile_id, profile in profiles.items() if profile["name"] == "字节跳动"), None)
        claude_active = str(claude_raw.get("active_profile") or byte_profile or fallback_active)
        if claude_active not in profiles:
            claude_active = fallback_active
        mappings = normalize_model_mappings(claude_raw.get("model_mappings"))
        if not mappings:
            mappings = default_claude_model_mappings()
        return {
            "active_profile": codex_active,
            "codex": {
                "active_profile": codex_active,
                "api_style": normalize_api_style(codex_raw.get("api_style")),
            },
            "claude": {
                "active_profile": claude_active,
                "api_style": normalize_claude_api_style(claude_raw.get("api_style")),
                "model_mappings": mappings,
            },
            "profiles": profiles,
        }

    profile = default_profile() if not raw else normalize_profile(raw)
    profile_id = profile_id_from_name(profile["name"])
    return {
        "active_profile": profile_id,
        "codex": {
            "active_profile": profile_id,
            "api_style": normalize_api_style(os.getenv("UPSTREAM_API_STYLE", "auto")),
        },
        "claude": {
            "active_profile": profile_id,
            "api_style": "auto",
            "model_mappings": default_claude_model_mappings(),
        },
        "profiles": {profile_id: profile},
    }


def active_profile(config: dict[str, Any] | None = None, namespace: str = "codex") -> dict[str, Any]:
    config = config or normalized_runtime_config()
    if namespace == "claude":
        return config["profiles"][config["claude"]["active_profile"]]
    return config["profiles"][config["codex"]["active_profile"]]


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
    claude_settings = get_claude_settings()
    return {
        "active_profile": config["codex"]["active_profile"],
        "codex": {
            "active_profile": config["codex"]["active_profile"],
            "api_style": config["codex"]["api_style"],
        },
        "claude": {
            "active_profile": config["claude"]["active_profile"],
            "api_style": config["claude"]["api_style"],
            "model_mappings": config["claude"]["model_mappings"],
            "active": public_profile(active_profile(config, "claude")),
            "base_url": f"http://{claude_settings.listen_host}:{claude_settings.listen_port}/anthropic",
        },
        "profiles": {
            profile_id: public_profile(profile)
            for profile_id, profile in config["profiles"].items()
        },
        "active": public_profile(active_profile(config, "codex")),
        "upstream_provider_name": settings.upstream_provider_name,
        "upstream_base_url": settings.upstream_base_url,
        "upstream_api_key": settings.upstream_api_key,
        "upstream_model": settings.upstream_model,
        "upstream_models": settings.upstream_models,
        "upstream_api_style": settings.upstream_api_style,
        "upstream_user_agent": settings.upstream_user_agent,
        "request_timeout_seconds": settings.request_timeout_seconds,
        "listen_host": settings.listen_host,
        "listen_port": settings.listen_port,
    }


def save_codex_config(data: dict[str, Any]) -> dict[str, Any]:
    config = normalized_runtime_config()
    active = str(data.get("active_profile") or config["codex"]["active_profile"])
    if active not in config["profiles"]:
        raise KeyError(active)
    config["codex"] = {
        "active_profile": active,
        "api_style": normalize_api_style(data.get("api_style") or config["codex"].get("api_style")),
    }
    config["active_profile"] = active
    save_runtime_config_document(config)
    return public_settings()["codex"]


def save_profile(profile_id: str, data: dict[str, Any], namespace: str = "codex") -> dict[str, Any]:
    config = normalized_runtime_config()
    current = config["profiles"].get(profile_id)
    profile = normalize_profile(data, current=current)
    config["profiles"][profile_id] = profile
    if namespace == "claude":
        config["claude"]["active_profile"] = profile_id
    else:
        config["codex"]["active_profile"] = profile_id
    config["active_profile"] = config["codex"]["active_profile"]
    if config["claude"]["active_profile"] not in config["profiles"]:
        config["claude"]["active_profile"] = profile_id
    save_runtime_config_document(config)
    return public_profile(profile)


def activate_profile(profile_id: str, namespace: str = "codex") -> dict[str, Any] | None:
    config = normalized_runtime_config()
    if profile_id not in config["profiles"]:
        return None
    if namespace == "claude":
        config["claude"]["active_profile"] = profile_id
    else:
        config["codex"]["active_profile"] = profile_id
        config["active_profile"] = profile_id
    save_runtime_config_document(config)
    return public_profile(config["profiles"][profile_id])


def save_claude_config(data: dict[str, Any]) -> dict[str, Any]:
    config = normalized_runtime_config()
    active = str(data.get("active_profile") or config["claude"]["active_profile"])
    if active not in config["profiles"]:
        raise KeyError(active)
    mappings = normalize_model_mappings(data.get("model_mappings"))
    if not mappings:
        mappings = config["claude"].get("model_mappings") or default_claude_model_mappings()
    config["claude"] = {
        "active_profile": active,
        "api_style": normalize_claude_api_style(data.get("api_style") or config["claude"].get("api_style")),
        "model_mappings": mappings,
    }
    config["active_profile"] = config["codex"]["active_profile"]
    save_runtime_config_document(config)
    return public_settings()["claude"]


def update_active_profile_models(models: list[str]) -> dict[str, Any]:
    config = normalized_runtime_config()
    profile = config["profiles"][config["codex"]["active_profile"]]
    profile["models"] = models
    if profile["default_model"] not in models and models:
        profile["default_model"] = models[0]
    config["active_profile"] = config["codex"]["active_profile"]
    save_runtime_config_document(config)
    return public_profile(profile)


def update_profile_models(profile_id: str, models: list[str]) -> dict[str, Any]:
    config = normalized_runtime_config()
    if profile_id not in config["profiles"]:
        raise KeyError(profile_id)
    profile = config["profiles"][profile_id]
    profile["models"] = models
    if profile["default_model"] not in models and models:
        profile["default_model"] = models[0]
    config["active_profile"] = config["codex"]["active_profile"]
    save_runtime_config_document(config)
    return public_profile(profile)
