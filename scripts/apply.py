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

try:
    from yaml import CSafeDumper as _YamlDumper
except ImportError:
    from yaml import SafeDumper as _YamlDumper


class LiteralStr(str):
    """YAML literal block scalar (|) for multiline PEM content."""


def _literal_str_representer(dumper: yaml.Dumper, data: LiteralStr) -> yaml.nodes.ScalarNode:
    return dumper.represent_scalar("tag:yaml.org,2002:str", str(data), style="|")


yaml.add_representer(LiteralStr, _literal_str_representer, Dumper=_YamlDumper)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_DIR = PROJECT_ROOT / "compose"
COMPOSE_PROJECT_NAME = "authelia-easy-deploy"
STATE_DIR = PROJECT_ROOT / ".authelia-easy-deploy"
SECRETS_PATH = STATE_DIR / "secrets.yaml"
COMPOSE_ENV_PATH = STATE_DIR / "compose.env"
COMPOSE_OVERRIDE_PATH = STATE_DIR / "compose.override.yml"
DEPLOY_PATH = PROJECT_ROOT / "deploy.yaml"
CADDY_TEMPLATE = PROJECT_ROOT / "caddy" / "Caddyfile.template"
CADDYFILE = PROJECT_ROOT / "caddy" / "Caddyfile"
INTEGRATION_DIR = STATE_DIR / "integration"
INTEGRATION_CADDY_FRAGMENT = INTEGRATION_DIR / "caddy.caddy"
DEFAULT_INTEGRATE_NETWORK = "easydeploy-net"

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


def oidc_clients(config: dict) -> list[Any]:
    oidc = config.get("oidc") or {}
    if not to_bool(oidc.get("enabled")):
        return []
    clients = oidc.get("clients") or []
    if not isinstance(clients, list):
        return []
    return clients


def oidc_provider_enabled(config: dict) -> bool:
    """Authelia requires at least one OIDC client when the provider is configured."""
    return len(oidc_clients(config)) > 0


def proxy_mode(config: dict) -> str:
    mode = str((config.get("proxy") or {}).get("mode") or "standalone").strip().lower()
    if mode not in {"standalone", "integrate"}:
        raise ValueError("proxy.mode must be 'standalone' or 'integrate'")
    return mode


def integrate_network_name(config: dict) -> str:
    integrate = (config.get("proxy") or {}).get("integrate") or {}
    name = str(integrate.get("network") or DEFAULT_INTEGRATE_NETWORK).strip()
    return name or DEFAULT_INTEGRATE_NETWORK


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


def save_users_database(path: Path, users: dict[str, dict[str, Any]]) -> None:
    """Write Authelia file auth database (users keyed by username, not a list)."""
    if not users:
        raise ValueError("users_database requires at least one user")
    save_yaml(path, {"users": users})
    loaded = load_yaml(path).get("users")
    if isinstance(loaded, list):
        raise RuntimeError(
            f"{path} was written in the wrong shape (list under users). "
            "This is a bug — please report it."
        )
    if not isinstance(loaded, dict):
        raise RuntimeError(f"{path}: expected users to be a mapping, got {type(loaded).__name__}")


def normalize_pem(text: str) -> str:
    lines = [line.rstrip() for line in text.strip().splitlines()]
    return "\n".join(lines) + "\n"


def literalize_pem_fields(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if key in {"key", "certificate_chain"} and isinstance(item, str) and "BEGIN" in item:
                result[key] = LiteralStr(normalize_pem(item))
            else:
                result[key] = literalize_pem_fields(item)
        return result
    if isinstance(value, list):
        return [literalize_pem_fields(item) for item in value]
    return value


def save_authelia_configuration(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    prepared = literalize_pem_fields(data)
    with path.open("w") as handle:
        yaml.dump(
            prepared,
            handle,
            Dumper=_YamlDumper,
            default_flow_style=False,
            sort_keys=False,
        )


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

    proxy_mode(config)


def compose_file_paths(config: dict) -> list[Path]:
    files = [COMPOSE_DIR / "docker-compose.yml"]
    if to_bool((config.get("session") or {}).get("redis", {}).get("enabled")):
        files.append(COMPOSE_DIR / "redis.yml")
    if proxy_mode(config) == "integrate":
        files.append(COMPOSE_DIR / "integrate.yml")
    else:
        files.append(COMPOSE_DIR / "caddy.yml")
    if COMPOSE_OVERRIDE_PATH.is_file():
        files.append(COMPOSE_OVERRIDE_PATH)
    return files


def derive_compose_files(config: dict) -> list[str]:
    files = ["docker-compose.yml"]
    if to_bool((config.get("session") or {}).get("redis", {}).get("enabled")):
        files.append("redis.yml")
    if proxy_mode(config) == "integrate":
        files.append("integrate.yml")
    else:
        files.append("caddy.yml")
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


def parse_authelia_password_hash(output: str) -> str:
    """Extract argon2 digest from `authelia crypto hash generate` stdout."""
    for line in output.splitlines():
        candidate = line.strip()
        if candidate.lower().startswith("digest:"):
            candidate = candidate.split(":", 1)[1].strip()
        if candidate.startswith("$argon2"):
            return candidate
    stripped = output.strip()
    if stripped.lower().startswith("digest:"):
        stripped = stripped.split(":", 1)[1].strip()
    if stripped.startswith("$argon2"):
        return stripped
    raise RuntimeError(f"Unexpected password hash output: {output.strip()[:80]!r}")


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
    combined = "\n".join(part for part in (result.stdout, result.stderr) if part)
    return parse_authelia_password_hash(combined)


def resolve_user_passwords(config: dict, secrets: dict, image: str) -> dict[str, dict[str, Any]]:
    users_out: dict[str, dict[str, Any]] = {}
    for index, user in enumerate(config.get("users") or []):
        if not isinstance(user, dict):
            raise ValueError(f"users[{index}] must be a mapping")
        username = str(user.get("username") or "").strip()
        if not username:
            raise ValueError(f"users[{index}].username is required")
        entry: dict[str, Any] = {
            "disabled": False,
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
        users_out[username] = entry
    return users_out


def prepare_oidc_clients(clients: list[Any]) -> list[Any]:
    """Ensure OpenCloud clients get claims needed by Authelia 4.39+."""
    prepared: list[Any] = []
    for client in clients:
        if not isinstance(client, dict):
            prepared.append(client)
            continue
        entry = dict(client)
        client_id = str(entry.get("client_id") or "")
        if client_id == "opencloud" or client_id.startswith("opencloud-"):
            entry.setdefault("claims_policy", "opencloud")
        prepared.append(entry)
    return prepared


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

    if oidc_provider_enabled(config):
        private_key, certificate = load_or_create_jwks(secrets)
        clients = prepare_oidc_clients(oidc_clients(config))
        configuration["identity_providers"] = {
            "oidc": {
                "claims_policies": {
                    "opencloud": {
                        "id_token": [
                            "preferred_username",
                            "name",
                            "email",
                            "groups",
                        ],
                    }
                },
                "cors": {
                    "endpoints": [
                        "authorization",
                        "token",
                        "revocation",
                        "userinfo",
                        "introspection",
                    ],
                    "allowed_origins_from_client_redirect_uris": True,
                },
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


def authelia_portal_caddy_block(domain: str) -> str:
    return f"""# authelia-easy-deploy — auth portal
{domain} {{
    reverse_proxy authelia:9091 {{
        header_up Host {{host}}
        header_up X-Forwarded-Host {{host}}
        header_up X-Forwarded-Proto {{scheme}}
        header_up Origin {{header.Origin}}
    }}
    encode gzip
    log
}}"""


def render_caddyfile(config: dict) -> None:
    domain = str(config["authelia"]["domain"])
    block = authelia_portal_caddy_block(domain)
    rendered = render_template(CADDY_TEMPLATE.read_text(), {"AUTH_DOMAIN_BLOCK": block.strip()})
    CADDYFILE.parent.mkdir(parents=True, exist_ok=True)
    CADDYFILE.write_text(rendered + "\n")


def render_integration_fragment(config: dict) -> None:
    domain = str(config["authelia"]["domain"])
    INTEGRATION_DIR.mkdir(parents=True, exist_ok=True)
    INTEGRATION_CADDY_FRAGMENT.write_text(authelia_portal_caddy_block(domain) + "\n")


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
        f"POSTGRES_PASSWORD={secrets['STORAGE_PASSWORD']}",
    ]
    if proxy_mode(config) == "standalone":
        lines.append(f"AED_CADDYFILE={CADDYFILE.resolve()}")
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
    users_db_path = config_dir / "users_database.yml"
    save_users_database(users_db_path, users)

    configuration = build_configuration(config, secrets, image)
    oidc_active = oidc_provider_enabled(config)
    save_authelia_configuration(config_dir / "configuration.yml", configuration)

    if str((config.get("notifier") or {}).get("type") or "").lower() == "filesystem":
        notification_file = config_dir / "notification.txt"
        if not notification_file.exists():
            notification_file.write_text("")

    write_secret_files(data_dir, secrets, oidc_active)
    if proxy_mode(config) == "integrate":
        render_integration_fragment(config)
    else:
        render_caddyfile(config)
    render_compose_override(oidc_active)
    write_compose_env(config, secrets)


def stop_standalone_caddy() -> None:
    if subprocess.run(["docker", "inspect", "authelia_caddy"], capture_output=True).returncode == 0:
        print("Stopping standalone authelia_caddy (integrate mode uses easydeploy-engine Caddy)…")
        subprocess.run(["docker", "stop", "authelia_caddy"], check=False)
        subprocess.run(["docker", "rm", "authelia_caddy"], check=False)


def ensure_docker_network(name: str) -> None:
    result = subprocess.run(
        ["docker", "network", "inspect", name],
        capture_output=True,
    )
    if result.returncode != 0:
        subprocess.run(["docker", "network", "create", name], check=True)


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


def validate_authelia_configuration(config: dict, data_dir: Path, secrets: dict) -> None:
    authelia = config["authelia"]
    image = f"{authelia.get('image', 'docker.io/authelia/authelia')}:{authelia.get('tag', 'latest')}"
    oidc_active = oidc_provider_enabled(config)
    env = [
        "-e",
        "AUTHELIA_IDENTITY_VALIDATION_RESET_PASSWORD_JWT_SECRET_FILE=/secrets/JWT_SECRET",
        "-e",
        "AUTHELIA_SESSION_SECRET_FILE=/secrets/SESSION_SECRET",
        "-e",
        "AUTHELIA_STORAGE_POSTGRES_PASSWORD_FILE=/secrets/STORAGE_PASSWORD",
        "-e",
        "AUTHELIA_STORAGE_ENCRYPTION_KEY_FILE=/secrets/STORAGE_ENCRYPTION_KEY",
    ]
    if oidc_active:
        env.extend(["-e", "AUTHELIA_IDENTITY_PROVIDERS_OIDC_HMAC_SECRET_FILE=/secrets/OIDC_HMAC_SECRET"])
    cmd = [
        "docker",
        "run",
        "--rm",
        *env,
        "-v",
        f"{data_dir / 'config'}:/config:ro",
        "-v",
        f"{data_dir / 'secrets'}:/secrets:ro",
        image,
        "authelia",
        "config",
        "validate",
        "--config",
        "/config/configuration.yml",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        users_path = data_dir / "config" / "users_database.yml"
        if users_path.is_file():
            preview = "\n".join(users_path.read_text().splitlines()[:8])
            detail = f"{detail}\n\n--- {users_path} (first lines) ---\n{preview}"
        raise RuntimeError(f"Authelia configuration validation failed:\n{detail}")


def print_authelia_logs() -> None:
    if subprocess.run(["docker", "inspect", "authelia"], capture_output=True).returncode != 0:
        return
    print("\n--- authelia container logs (last 80 lines) ---", file=sys.stderr)
    subprocess.run(["docker", "logs", "authelia", "--tail", "80"], check=False)


def run_compose(*args: str) -> None:
    cmd = docker_compose_cmd()
    for compose_file in compose_file_paths(load_config()):
        cmd.extend(["-f", str(compose_file)])
    cmd.extend(args)

    env = os.environ.copy()
    env["COMPOSE_PROJECT_NAME"] = COMPOSE_PROJECT_NAME
    if COMPOSE_ENV_PATH.is_file():
        for line in COMPOSE_ENV_PATH.read_text().splitlines():
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip()

    try:
        subprocess.run(cmd, cwd=COMPOSE_DIR, check=True, env=env)
    except subprocess.CalledProcessError:
        print_authelia_logs()
        raise


def reconcile_runtime(skip_pull: bool = False) -> None:
    config = load_config()
    data_dir = Path(str(config["authelia"]["data_dir"]))
    secrets = load_yaml(SECRETS_PATH)
    mode = proxy_mode(config)
    ensure_docker_network("authelia-net")
    if mode == "integrate":
        net = integrate_network_name(config)
        if net != DEFAULT_INTEGRATE_NETWORK:
            print(
                f"Warning: custom integrate network {net!r} is not yet supported in compose/integrate.yml; "
                f"using {DEFAULT_INTEGRATE_NETWORK}",
                file=sys.stderr,
            )
        ensure_docker_network(DEFAULT_INTEGRATE_NETWORK)
        stop_standalone_caddy()
    print("Validating Authelia configuration…")
    validate_authelia_configuration(config, data_dir, secrets)
    if not skip_pull:
        print("Pulling Authelia stack images…")
        run_compose("pull")
    print("Starting Authelia stack…")
    try:
        run_compose("up", "-d", "--wait", "--remove-orphans")
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            "Docker Compose failed while starting the stack. "
            "If you see a network warning about authelia-net, run: "
            "docker compose -p authelia-easy-deploy -f compose/docker-compose.yml down && docker network rm authelia-net "
            "then re-run apply.sh"
        ) from exc
    restart_authelia_if_running()


def restart_authelia_if_running() -> None:
    """configuration.yml is bind-mounted; Authelia only reloads OIDC clients on process restart."""
    if subprocess.run(["docker", "inspect", "authelia"], capture_output=True).returncode != 0:
        return
    print("Restarting Authelia to load configuration changes…")
    subprocess.run(["docker", "restart", "authelia"], check=True)


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
    oidc = config.get("oidc") or {}
    if to_bool(oidc.get("enabled")) and not oidc_provider_enabled(config):
        print(
            "OIDC: enabled in deploy.yaml but no clients defined — "
            "portal runs without OIDC until you add oidc.clients and re-run apply."
        )
    elif oidc_provider_enabled(config):
        print(f"OIDC provider:   active ({len(oidc_clients(config))} client(s))")
    if proxy_mode(config) == "integrate":
        print(f"Proxy mode:      integrate (Caddy fragment: {INTEGRATION_CADDY_FRAGMENT})")
        print("                 Run easydeploy-engine apply.sh to refresh the shared Caddy.")
    else:
        print("Proxy mode:      standalone (local authelia_caddy on :443)")
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
