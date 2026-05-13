#!/usr/bin/env python3
import argparse
from pathlib import Path
import re
import shutil


PROVIDER_NAME = "codex_proxy"
OPENAI_PROVIDER_NAME = "openai"
VALID_MODES = ("proxy", "auth-proxy", "openai-compatible")
BEGIN = "# BEGIN CODEXPROXY MANAGED BLOCK"
END = "# END CODEXPROXY MANAGED BLOCK"
ORIGINAL_BACKUP_SUFFIX = ".codexproxy-original-backup"


def strip_managed_block(text: str) -> str:
    pattern = re.compile(rf"\n?{re.escape(BEGIN)}.*?{re.escape(END)}\n?", re.DOTALL)
    return pattern.sub("\n", text).strip() + "\n"


def strip_provider_block(text: str, provider_name: str) -> str:
    pattern = re.compile(
        rf"\n?\[model_providers\.{re.escape(provider_name)}\]\n.*?(?=\n\[|$)",
        re.DOTALL,
    )
    return pattern.sub("\n", text).strip() + "\n"


def replace_top_level_keys(text: str, model: str, model_provider: str) -> str:
    lines = text.splitlines()
    result: list[str] = []
    in_top_level = True
    seen_model_provider = False
    seen_model = False

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            if in_top_level:
                if not seen_model_provider:
                    result.append(f'model_provider = "{model_provider}"')
                    seen_model_provider = True
                if not seen_model:
                    result.append(f'model = "{model}"')
                    seen_model = True
            in_top_level = False

        if in_top_level and stripped.startswith("model_provider"):
            result.append(f'model_provider = "{model_provider}"')
            seen_model_provider = True
            continue

        if in_top_level and stripped.startswith("model ="):
            result.append(f'model = "{model}"')
            seen_model = True
            continue

        result.append(line)

    if in_top_level:
        if not seen_model_provider:
            result.append(f'model_provider = "{model_provider}"')
        if not seen_model:
            result.append(f'model = "{model}"')

    return "\n".join(result).strip() + "\n"


def managed_block(base_url: str, mode: str) -> str:
    if mode == "openai-compatible":
        raise ValueError("openai-compatible mode cannot override reserved built-in provider ID: openai")
    provider_name = PROVIDER_NAME
    display_name = "AI Proxy"
    requires_openai_auth = "true" if mode == "auth-proxy" else "false"
    return f"""
{BEGIN}
[model_providers.{provider_name}]
name = "{display_name}"
base_url = "{base_url.rstrip("/")}/v1"
wire_api = "responses"
requires_openai_auth = {requires_openai_auth}
stream_max_retries = 0
{END}
""".strip()
def original_backup_path(config_path: Path) -> Path:
    return config_path.with_name(f"{config_path.name}{ORIGINAL_BACKUP_SUFFIX}")


def backup_original_config(config_path: Path) -> Path:
    backup_path = original_backup_path(config_path)
    if not backup_path.exists():
        shutil.copy2(config_path, backup_path)
    return backup_path


def install(config_path: Path, model: str, proxy_url: str, mode: str = "proxy") -> Path:
    if mode == "openai-compatible":
        raise ValueError("openai-compatible mode cannot override reserved built-in provider ID: openai")
    if mode not in VALID_MODES:
        raise ValueError(f"Unknown AIProxy Codex install mode: {mode}")

    if not config_path.exists():
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text("", encoding="utf-8")

    existing_text = config_path.read_text(encoding="utf-8")
    backup_path = backup_original_config(config_path)
    text = strip_managed_block(existing_text)
    model_provider = PROVIDER_NAME
    text = strip_provider_block(text, model_provider)
    text = replace_top_level_keys(text, model, model_provider)
    text = text.rstrip() + "\n\n" + managed_block(proxy_url, mode) + "\n"
    config_path.write_text(text, encoding="utf-8")
    return backup_path


def restore(config_path: Path) -> Path:
    original_backup = original_backup_path(config_path)
    if original_backup.exists():
        shutil.copy2(original_backup, config_path)
        return original_backup
    raise SystemExit(f"No AIProxy original backup found next to {config_path}")


def status(config_path: Path) -> str:
    if not config_path.exists():
        return "missing"
    text = config_path.read_text(encoding="utf-8")
    if BEGIN not in text:
        return "not-installed"
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            break
        if stripped.startswith("model_provider"):
            if f'"{PROVIDER_NAME}"' in stripped or f'"{OPENAI_PROVIDER_NAME}"' in stripped:
                return "installed"
            return "not-installed"
    return "not-installed"


def main() -> None:
    parser = argparse.ArgumentParser(description="Install or restore Codex Desktop config for AIProxy.")
    parser.add_argument("command", choices=["install", "restore", "status"])
    parser.add_argument("--config", default=str(Path.home() / ".codex" / "config.toml"))
    parser.add_argument("--model", default="gpt-5.5")
    parser.add_argument("--proxy-url", default="http://127.0.0.1:8383")
    parser.add_argument("--mode", choices=VALID_MODES, default="proxy")
    args = parser.parse_args()

    config_path = Path(args.config).expanduser()

    if args.command == "install":
        backup_path = install(config_path, args.model, args.proxy_url, mode=args.mode)
        print(f"installed AIProxy Codex config: {config_path}")
        print(f"backup: {backup_path}")
    elif args.command == "restore":
        backup_path = restore(config_path)
        print(f"restored Codex config from: {backup_path}")
    else:
        print(status(config_path))


if __name__ == "__main__":
    main()
