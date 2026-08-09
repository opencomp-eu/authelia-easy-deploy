#!/usr/bin/env python3
"""Read and write deploy.yaml for the Authelia wizard."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DEPLOY_PATH = PROJECT_ROOT / "deploy.yaml"


def load_or_init(path: Path = DEFAULT_DEPLOY_PATH) -> dict:
    if not path.exists():
        example = PROJECT_ROOT / "deploy.yaml.example"
        if example.is_file():
            with example.open() as handle:
                return yaml.safe_load(handle) or {}
        return {}

    with path.open() as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError("deploy.yaml root must be a mapping")
    return data


def save(path: Path, data: dict) -> None:
    with path.open("w") as handle:
        yaml.safe_dump(data, handle, default_flow_style=False, sort_keys=False)


def update_from_wizard(
    *,
    auth_domain: str,
    sso_domain: str,
    data_dir: str,
    admin_username: str,
    admin_display_name: str,
    admin_email: str,
    admin_password: str | None,
    notifier_type: str,
    smtp_host: str,
    smtp_port: int,
    smtp_username: str,
    smtp_from: str,
    redis_enabled: bool,
    oidc_enabled: bool,
    path: Path = DEFAULT_DEPLOY_PATH,
) -> None:
    config = load_or_init(path)

    authelia = config.setdefault("authelia", {})
    authelia["domain"] = auth_domain
    authelia["sso_domain"] = sso_domain
    authelia.setdefault("image", "docker.io/authelia/authelia")
    authelia.setdefault("tag", "4.39.4")
    authelia["data_dir"] = data_dir.rstrip("/")

    config["proxy"] = {"type": "caddy"}
    config["storage"] = {"type": "postgres"}
    config["session"] = {"redis": {"enabled": redis_enabled}}
    config["authentication"] = {"backend": "file"}

    user_entry: dict[str, Any] = {
        "username": admin_username,
        "display_name": admin_display_name,
        "email": admin_email,
        "groups": ["opencloud-admin", "matrix-admins"],
    }
    if admin_password:
        user_entry["password"] = admin_password
    config["users"] = [user_entry]

    notifier: dict[str, Any] = {"type": notifier_type}
    if notifier_type == "smtp":
        notifier["smtp"] = {
            "host": smtp_host,
            "port": smtp_port,
            "username": smtp_username,
            "from": smtp_from,
        }
    config["notifier"] = notifier

    base = sso_domain
    config["access_control"] = {
        "default_policy": "deny",
        "rules": [{"domain": f"*.{base}", "policy": "one_factor"}],
    }

    config["oidc"] = {"enabled": oidc_enabled, "clients": list(config.get("oidc", {}).get("clients") or [])}

    save(path, config)


def main() -> None:
    parser = argparse.ArgumentParser(description="Update deploy.yaml from wizard")
    parser.add_argument("--deploy-yaml", type=Path, default=DEFAULT_DEPLOY_PATH)
    args = parser.parse_args()
    if not args.deploy_yaml.exists():
        raise SystemExit(f"Missing {args.deploy_yaml}")


if __name__ == "__main__":
    main()
