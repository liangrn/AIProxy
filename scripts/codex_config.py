#!/usr/bin/env python3
import argparse
from datetime import datetime
from pathlib import Path
import re
import shutil


PROVIDER_NAME = "codex_proxy"
BEGIN = "# BEGIN CODEXPROXY MANAGED BLOCK"
END = "# END CODEXPROXY MANAGED BLOCK"


def strip_managed_block(text: str) -> str:
    pattern = re.compile(rf"\n?{re.escape(BEGIN)}.*?{re.escape(END)}\n?", re.DOTALL)
    return pattern.sub("\n", text).strip() + "\n"


def replace_top_level_keys(text: str, model: str) -> str:
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
                    result.append(f'model_provider = "{PROVIDER_NAME}"')
                    seen_model_provider = True
                if not seen_model:
                    result.append(f'model = "{model}"')
                    seen_model = True
            in_top_level = False

        if in_top_level and stripped.startswith("model_provider"):
            result.append(f'model_provider = "{PROVIDER_NAME}"')
            seen_model_provider = True
            continue

        if in_top_level and stripped.startswith("model ="):
            result.append(f'model = "{model}"')
            seen_model = True
            continue

        result.append(line)

    if in_top_level:
        if not seen_model_provider:
            result.append(f'model_provider = "{PROVIDER_NAME}"')
        if not seen_model:
            result.append(f'model = "{model}"')

    return "\n".join(result).strip() + "\n"


def managed_block(base_url: str) -> str:
    return f"""
{BEGIN}
[model_providers.{PROVIDER_NAME}]
name = "Codex Proxy"
base_url = "{base_url.rstrip("/")}/v1"
wire_api = "responses"
requires_openai_auth = false
stream_max_retries = 0
{END}
""".strip()


def backup_config(config_path: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = config_path.with_name(f"{config_path.name}.codexproxy-backup-{timestamp}")
    shutil.copy2(config_path, backup_path)
    return backup_path


def install(config_path: Path, model: str, proxy_url: str) -> Path:
    if not config_path.exists():
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text("", encoding="utf-8")

    backup_path = backup_config(config_path)
    text = strip_managed_block(config_path.read_text(encoding="utf-8"))
    text = replace_top_level_keys(text, model)
    text = text.rstrip() + "\n\n" + managed_block(proxy_url) + "\n"
    config_path.write_text(text, encoding="utf-8")
    return backup_path


def restore(config_path: Path) -> Path:
    backups = sorted(config_path.parent.glob(f"{config_path.name}.codexproxy-backup-*"))
    if not backups:
        raise SystemExit(f"No CodexProxy backup found next to {config_path}")
    latest = backups[-1]
    shutil.copy2(latest, config_path)
    return latest


def status(config_path: Path) -> str:
    if not config_path.exists():
        return "missing"
    text = config_path.read_text(encoding="utf-8")
    if BEGIN in text and f'model_provider = "{PROVIDER_NAME}"' in text:
        return "installed"
    return "not-installed"


def main() -> None:
    parser = argparse.ArgumentParser(description="Install or restore Codex Desktop config for CodexProxy.")
    parser.add_argument("command", choices=["install", "restore", "status"])
    parser.add_argument("--config", default=str(Path.home() / ".codex" / "config.toml"))
    parser.add_argument("--model", default="gpt-5.5")
    parser.add_argument("--proxy-url", default="http://127.0.0.1:8383")
    args = parser.parse_args()

    config_path = Path(args.config).expanduser()

    if args.command == "install":
        backup_path = install(config_path, args.model, args.proxy_url)
        print(f"installed CodexProxy config: {config_path}")
        print(f"backup: {backup_path}")
    elif args.command == "restore":
        backup_path = restore(config_path)
        print(f"restored Codex config from: {backup_path}")
    else:
        print(status(config_path))


if __name__ == "__main__":
    main()
