#!/usr/bin/env python3
"""authelia-easy-deploy configuration engine."""

from __future__ import annotations

import argparse
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_DIR = PROJECT_ROOT / "compose"
STATE_DIR = PROJECT_ROOT / ".authelia-easy-deploy"
SECRETS_PATH = STATE_DIR / "secrets.yaml"
COMPOSE_ENV_PATH = STATE_DIR / "compose.env"
COMPOSE_OVERRIDE_PATH = STATE_DIR / "compose.override.yml"
DEPLOY_PATH = PROJECT_ROOT / "deploy.yaml"
CADDY_TEMPLATE = PROJECT_ROOT / "caddy" / "Caddyfile.template"
CADDYFILE = PROJECT_ROOT / "caddy" / "Caddyfile"

SECRET_KEYS = (
    "JWT_SECRET",
    "SESSION_SECRET",
    "STORAGE_PASSWORD",
    "STORAGE_ENCRYPTION_KEY",
    "OIDC_HMAC_SECRET",
    "ADMIN_PASSWORD",
)

PRESERVED_SECRET_KEYS = frozenset(SECRET_KEYS)


def to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def load_yaml(path: Path) -> dict:
    with path.open() as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: root must be a mapping")
    return data


def save_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as handle:
        yaml.safe_dump(data, handle, default_flow_style=False, sort_keys=False)


def render_template(template: str, values: dict[str, str]) -> str:
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace(f"{{{{{key}}}}}", value)
    if "{{" in rendered:
        missing = sorted({part.split("}")[0] for part in rendered.split("{{")[1:]})
        raise ValueError(f"Unresolved template placeholders: {', '.join(missing)}")
    return rendered


def load_config(path: Path = DEPLOY_PATH) -> dict:
    if not path.exists():
        raise FileNotFoundError(
            f"Missing {path.name}. Copy deploy.yaml.example to deploy.yaml or run wizard.sh."
        )
    return load_yaml(path)


def validate_config(config: dict) -> None:
    authelia = config.get("authelia") or {}
    if not isinstance(authelia, dict):
        raise ValueError("authelia section must be a mapping")

    domain = str(authelia.get("domain") or "").strip()
    if not domain or domain == "auth.example.com":
        raise ValueError("authelia.domain must be set to your real auth portal domain")

    sso_domain = str(authelia.get("sso_domain") or "").strip()
    if not sso_domain or sso_domain == "example.com":
        raise ValueError("authelia.sso_domain must be set to your real SSO domain")

    data_dir = str(authelia.get("data_dir") or "").strip()
    if not data_dir:
        raise ValueError("authelia.data_dir must be set")

    proxy_type = (config.get("proxy") or {}).get("type", "caddy")
    if proxy_type != "caddy":
        raise ValueError("proxy.type must be 'caddy' in v1")

    storage_type = (config.get("storage") or {}).get("type", "postgres")
    if storage_type != "postgres":
        raise ValueError("storage.type must be 'postgres' in v1")

    auth_backend = (config.get("authentication") or {}).get("backend", "file")
    if auth_backend != "file":
        raise ValueError("authentication.backend must be 'file' in v1")

    users = config.get("users") or []
    if not isinstance(users, list) or not users:
        raise ValueError("users must contain at least one user when using file authentication")

    notifier = config.get("notifier") or {}
    notifier_type = str(notifier.get("type") or "smtp").strip().lower()
    if notifier_type not in {"smtp", "filesystem"}:
        raise ValueError("notifier.type must be 'smtp' or 'filesystem'")
    if notifier_type == "smtp":
        smtp = notifier.get("smtp") or {}
        if not str(smtp.get("host") or "").strip():
            raise ValueError("notifier.smtp.host is required when notifier.type=smtp")


def compose_file_paths(config: dict) -> list[Path]:
    files = [COMPOSE_DIR / "docker-compose.yml"]
    if to_bool((config.get("session") or {}).get("redis", {}).get("enabled")):
        files.append(COMPOSE_DIR / "redis.yml")
    if COMPOSE_OVERRIDE_PATH.is_file():
        files.append(COMPOSE_OVERRIDE_PATH)
    return files


def derive_compose_files(config: dict) -> list[str]:
    files = ["docker-compose.yml"]
    if to_bool((config.get("session") or {}).get("redis", {}).get("enabled")):
        files.append("redis.yml")
    return files


def random_secret(length: int = 64) -> str:
    return secrets.token_urlsafe(length)[:length]


def load_or_create_secrets() -> dict:
    if SECRETS_PATH.is_file():
        data = load_yaml(SECRETS_PATH)
    else:
        data = {}
    for key in SECRET_KEYS:
        if key not in data or not str(data.get(key) or "").strip():
            if key == "STORAGE_ENCRYPTION_KEY":
                data[key] = random_secret(32)
            elif key == "ADMIN_PASSWORD":
                data[key] = random_secret(16)
            else:
                data[key] = random_secret(64)
    save_yaml(SECRETS_PATH, data)
    return data


def generate_oidc_jwks() -> tuple[str, str]:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        key_path = tmp_path / "oidc.key"
        cert_path = tmp_path / "oidc.crt"
        subprocess.run(
            ["openssl", "genrsa", "-out", str(key_path), "2048"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "openssl",
                "req",
                "-new",
                "-x509",
                "-key",
                str(key_path),
                "-out",
                str(cert_path),
                "-days",
                "3650",
                "-subj",
                "/CN=authelia-oidc",
            ],
            check=True,
            capture_output=True,
        )
        private_key = key_path.read_text()
        certificate = cert_path.read_text()
    return private_key, certificate


def load_or_create_jwks(secrets: dict) -> tuple[str, str]:
    if secrets.get("OIDC_JWKS_PRIVATE_KEY") and secrets.get("OIDC_JWKS_CERTIFICATE"):
        return str(secrets["OIDC_JWKS_PRIVATE_KEY"]), str(secrets["OIDC_JWKS_CERTIFICATE"])
    private_key, certificate = generate_oidc_jwks()
    secrets["OIDC_JWKS_PRIVATE_KEY"] = private_key
    secrets["OIDC_JWKS_CERTIFICATE"] = certificate
    save_yaml(SECRETS_PATH, secrets)
    return private_key, certificate


def hash_password_argon2(password: str, image: str) -> str:
    try:
        result = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                image,
                "authelia",
                "crypto",
                "hash",
                "generate",
                "argon2",
                "--password",
                password,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            "Failed to hash password via Authelia Docker image — ensure Docker can pull "
            f"{image} and re-run apply"
        ) from exc
    digest = result.stdout.strip()
    if not digest.startswith("$argon2"):
        raise RuntimeError(f"Unexpected password hash output: {digest[:80]!r}")
    return digest


def resolve_user_passwords(config: dict, secrets: dict, image: str) -> list[dict]:
    users_out: list[dict] = []
    for index, user in enumerate(config.get("users") or []):
        if not isinstance(user, dict):
            raise ValueError(f"users[{index}] must be a mapping")
        username = str(user.get("username") or "").strip()
        if not username:
            raise ValueError(f"users[{index}].username is required")
        entry: dict[str, Any] = {
            "username": username,
            "displayname": str(user.get("display_name") or username),
            "email": str(user.get("email") or f"{username}@local"),
            "groups": list(user.get("groups") or []),
        }
        plain = str(user.get("password") or "").strip()
        if plain.startswith("$argon2"):
            entry["password"] = plain
        elif plain:
            entry["password"] = hash_password_argon2(plain, image)
        else:
            secret_key = f"USER_PASSWORD_HASH_{username.upper()}"
            if secret_key not in secrets or not str(secrets.get(secret_key) or "").startswith("$"):
                secrets[secret_key] = hash_password_argon2(secrets["ADMIN_PASSWORD"], image)
                save_yaml(SECRETS_PATH, secrets)
            entry["password"] = secrets[secret_key]
        users_out.append(entry)
    return users_out


def build_configuration(config: dict, secrets: dict, image: str) -> dict:
    authelia = config["authelia"]
    domain = str(authelia["domain"])
    sso_domain = str(authelia["sso_domain"])
    auth_url = f"https://{domain}"
    default_redirect = f"https://{sso_domain}"

    session_block: dict[str, Any] = {
        "cookies": [
            {
                "domain": sso_domain,
                "authelia_url": auth_url,
                "default_redirection_url": default_redirect,
            }
        ]
    }
    if to_bool((config.get("session") or {}).get("redis", {}).get("enabled")):
        session_block["redis"] = {
            "host": "redis",
            "port": 6379,
            "maximum_active_connections": 8,
        }

    notifier = config.get("notifier") or {}
    notifier_type = str(notifier.get("type") or "smtp").lower()
    if notifier_type == "filesystem":
        notifier_block = {
            "filesystem": {
                "filename": "/config/notification.txt",
            }
        }
    else:
        smtp = notifier.get("smtp") or {}
        notifier_block = {
            "smtp": {
                "address": f"smtp://{smtp['host']}:{smtp.get('port', 587)}",
                "username": str(smtp.get("username") or ""),
                "sender": str(smtp.get("from") or f"Authelia <noreply@{sso_domain}>"),
            }
        }
        smtp_password = str(secrets.get("SMTP_PASSWORD") or "").strip()
        if smtp_password:
            notifier_block["smtp"]["password"] = smtp_password

    access = config.get("access_control") or {}
    access_block = {
        "default_policy": str(access.get("default_policy") or "deny"),
        "rules": list(access.get("rules") or []),
    }

    configuration: dict[str, Any] = {
        "theme": "auto",
        "server": {
            "address": "tcp://0.0.0.0:9091/",
            "endpoints": {
                "authz": {
                    "forward-auth": {
                        "implementation": "ForwardAuth",
                    }
                }
            },
        },
        "log": {"level": "info"},
        "authentication_backend": {
            "file": {
                "path": "/config/users_database.yml",
            }
        },
        "session": session_block,
        "storage": {
            "postgres": {
                "address": "tcp://postgres:5432",
                "database": "authelia",
                "username": "authelia",
            },
        },
        "notifier": notifier_block,
        "access_control": access_block,
    }

    if to_bool((config.get("oidc") or {}).get("enabled")):
        private_key, certificate = load_or_create_jwks(secrets)
        clients = config.get("oidc", {}).get("clients") or []
        configuration["identity_providers"] = {
            "oidc": {
                "jwks": [
                    {
                        "key_id": "main",
                        "algorithm": "RS256",
                        "use": "sig",
                        "key": private_key,
                        "certificate_chain": certificate,
                    }
                ],
                "clients": clients,
            }
        }

    return configuration


def write_secret_files(data_dir: Path, secrets: dict, oidc_enabled: bool) -> None:
    secret_dir = data_dir / "secrets"
    secret_dir.mkdir(parents=True, exist_ok=True)
    mapping = {
        "JWT_SECRET": secrets["JWT_SECRET"],
        "SESSION_SECRET": secrets["SESSION_SECRET"],
        "STORAGE_PASSWORD": secrets["STORAGE_PASSWORD"],
        "STORAGE_ENCRYPTION_KEY": secrets["STORAGE_ENCRYPTION_KEY"],
    }
    if oidc_enabled:
        mapping["OIDC_HMAC_SECRET"] = secrets["OIDC_HMAC_SECRET"]
    for name, value in mapping.items():
        path = secret_dir / name
        path.write_text(str(value).strip() + "\n")
        path.chmod(0o600)


def render_caddyfile(config: dict) -> None:
    domain = str(config["authelia"]["domain"])
    block = f"""{domain} {{
    reverse_proxy authelia:9091 {{
        header_up X-Forwarded-Proto {{scheme}}
    }}
    encode gzip
    log
}}"""
    rendered = render_template(CADDY_TEMPLATE.read_text(), {"AUTH_DOMAIN_BLOCK": block.strip()})
    CADDYFILE.parent.mkdir(parents=True, exist_ok=True)
    CADDYFILE.write_text(rendered + "\n")


def render_compose_override(oidc_enabled: bool) -> None:
    if not oidc_enabled:
        if COMPOSE_OVERRIDE_PATH.exists():
            COMPOSE_OVERRIDE_PATH.unlink()
        return
    override = {
        "services": {
            "authelia": {
                "environment": {
                    "AUTHELIA_IDENTITY_PROVIDERS_OIDC_HMAC_SECRET_FILE": "/secrets/OIDC_HMAC_SECRET",
                }
            }
        }
    }
    save_yaml(COMPOSE_OVERRIDE_PATH, override)


def write_compose_env(config: dict, secrets: dict) -> None:
    authelia = config["authelia"]
    image = f"{authelia.get('image', 'docker.io/authelia/authelia')}:{authelia.get('tag', 'latest')}"
    lines = [
        f"AUTHELIA_IMAGE={image}",
        f"AUTHELIA_DATA_DIR={authelia['data_dir']}",
        f"AED_CADDYFILE={CADDYFILE.resolve()}",
        f"POSTGRES_PASSWORD={secrets['STORAGE_PASSWORD']}",
    ]
    COMPOSE_ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    COMPOSE_ENV_PATH.write_text("\n".join(lines) + "\n")
    COMPOSE_ENV_PATH.chmod(0o600)


def render_runtime_artifacts(config: dict, secrets: dict) -> None:
    authelia = config["authelia"]
    data_dir = Path(str(authelia["data_dir"]))
    config_dir = data_dir / "config"
    config_dir.mkdir(parents=True, exist_ok=True)

    image = f"{authelia.get('image', 'docker.io/authelia/authelia')}:{authelia.get('tag', 'latest')}"
    users = resolve_user_passwords(config, secrets, image)
    users_doc = {"users": users}
    save_yaml(config_dir / "users_database.yml", users_doc)

    oidc_enabled = to_bool((config.get("oidc") or {}).get("enabled"))
    configuration = build_configuration(config, secrets, image)
    save_yaml(config_dir / "configuration.yml", configuration)

    write_secret_files(data_dir, secrets, oidc_enabled)
    render_caddyfile(config)
    render_compose_override(oidc_enabled)
    write_compose_env(config, secrets)


def docker_compose_cmd() -> list[str]:
    if shutil.which("docker"):
        result = subprocess.run(
            ["docker", "compose", "version"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return ["docker", "compose"]
    compose = shutil.which("docker-compose")
    if compose:
        return [compose]
    raise RuntimeError("Docker Compose v2 is required (docker compose)")


def ensure_docker_network(name: str) -> None:
    result = subprocess.run(
        ["docker", "network", "inspect", name],
        capture_output=True,
    )
    if result.returncode != 0:
        subprocess.run(["docker", "network", "create", name], check=True)


def run_compose(*args: str) -> None:
    cmd = docker_compose_cmd()
    for compose_file in compose_file_paths(load_config()):
        cmd.extend(["-f", str(compose_file)])
    cmd.extend(args)

    env = os.environ.copy()
    if COMPOSE_ENV_PATH.is_file():
        for line in COMPOSE_ENV_PATH.read_text().splitlines():
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip()

    subprocess.run(cmd, cwd=COMPOSE_DIR, check=True, env=env)


def reconcile_runtime(skip_pull: bool = False) -> None:
    ensure_docker_network("authelia-net")
    if not skip_pull:
        print("Pulling Authelia stack images…")
        run_compose("pull")
    print("Starting Authelia stack…")
    run_compose("up", "-d", "--wait", "--remove-orphans")


def print_summary(config: dict, secrets: dict) -> None:
    authelia = config["authelia"]
    domain = authelia["domain"]
    data_dir = authelia["data_dir"]
    print()
    print("=== Deployment summary ===")
    print(f"Authelia portal: https://{domain}")
    print(f"Data directory:  {data_dir}")
    print(f"Secrets file:    {SECRETS_PATH}")
    if str((config.get("notifier") or {}).get("type") or "").lower() == "filesystem":
        print(f"Notifications:   {data_dir}/config/notification.txt")
    admin = (config.get("users") or [{}])[0]
    username = admin.get("username", "admin")
    if not str(admin.get("password") or "").strip():
        print(f"Admin password:  {secrets.get('ADMIN_PASSWORD')} (auto-generated; stored in secrets.yaml)")
    print()
    print("Next: see docs/integrating-services.md to wire OpenCloud or Matrix to this IdP.")
    print()


def apply_configuration(*, skip_runtime: bool = False, skip_pull: bool = False) -> None:
    config = load_config()
    validate_config(config)
    secrets = load_or_create_secrets()
    render_runtime_artifacts(config, secrets)
    if not skip_runtime:
        reconcile_runtime(skip_pull=skip_pull)
    print_summary(config, secrets)


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply authelia-easy-deploy configuration")
    parser.add_argument(
        "--skip-runtime",
        action="store_true",
        help="Render configs and secrets only; do not run docker compose",
    )
    parser.add_argument(
        "--skip-pull",
        action="store_true",
        help="Skip docker compose pull before up",
    )
    args = parser.parse_args()
    try:
        apply_configuration(skip_runtime=args.skip_runtime, skip_pull=args.skip_pull)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
