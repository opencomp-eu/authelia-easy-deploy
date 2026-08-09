"""Tests for scripts/apply.py."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from scripts.apply import (
    build_configuration,
    derive_compose_files,
    load_or_create_secrets,
    render_caddyfile,
    render_template,
    validate_config,
)

FAKE_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4$Zzz$zzz"
)


def _base_config(**overrides) -> dict:
    config = {
        "authelia": {
            "domain": "auth.test.example",
            "sso_domain": "test.example",
            "image": "docker.io/authelia/authelia",
            "tag": "4.39.4",
            "data_dir": "/var/lib/authelia",
        },
        "proxy": {"type": "caddy"},
        "storage": {"type": "postgres"},
        "session": {"redis": {"enabled": False}},
        "authentication": {"backend": "file"},
        "users": [
            {
                "username": "admin",
                "display_name": "Admin",
                "email": "admin@test.example",
                "password": FAKE_HASH,
                "groups": ["opencloud-admin"],
            }
        ],
        "notifier": {
            "type": "smtp",
            "smtp": {
                "host": "smtp.test.example",
                "port": 587,
                "username": "",
                "from": "Authelia <noreply@test.example>",
            },
        },
        "access_control": {
            "default_policy": "deny",
            "rules": [{"domain": "*.test.example", "policy": "one_factor"}],
        },
        "oidc": {
            "enabled": True,
            "clients": [
                {
                    "client_id": "test-client",
                    "client_name": "Test",
                    "authorization_policy": "one_factor",
                    "redirect_uris": ["https://app.test.example/oidc/callback"],
                    "scopes": ["openid", "profile", "email"],
                    "grant_types": ["authorization_code"],
                    "response_types": ["code"],
                    "token_endpoint_auth_method": "client_secret_basic",
                }
            ],
        },
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and key in config and isinstance(config[key], dict):
            config[key].update(value)
        else:
            config[key] = value
    return config


def test_validate_config_rejects_placeholder_domain():
    with pytest.raises(ValueError, match="authelia.domain"):
        validate_config(_base_config(authelia={"domain": "auth.example.com", "sso_domain": "real.example", "data_dir": "/x"}))


def test_validate_config_requires_smtp_host():
    config = _base_config()
    config["notifier"] = {"type": "smtp", "smtp": {"host": "", "port": 587}}
    with pytest.raises(ValueError, match="notifier.smtp.host"):
        validate_config(config)


def test_derive_compose_files_redis_overlay():
    assert derive_compose_files(_base_config()) == ["docker-compose.yml"]
    assert derive_compose_files(_base_config(session={"redis": {"enabled": True}})) == [
        "docker-compose.yml",
        "redis.yml",
    ]


def test_build_configuration_session_and_oidc():
    secrets = {
        "JWT_SECRET": "j",
        "SESSION_SECRET": "s",
        "STORAGE_PASSWORD": "p",
        "STORAGE_ENCRYPTION_KEY": "e" * 20,
        "OIDC_HMAC_SECRET": "h",
        "OIDC_JWKS_PRIVATE_KEY": "-----BEGIN PRIVATE KEY-----\nK\n-----END PRIVATE KEY-----\n",
        "OIDC_JWKS_CERTIFICATE": "-----BEGIN CERTIFICATE-----\nC\n-----END CERTIFICATE-----\n",
    }
    config = _base_config()
    doc = build_configuration(config, secrets, "docker.io/authelia/authelia:4.39.4")
    assert doc["session"]["cookies"][0]["authelia_url"] == "https://auth.test.example"
    assert doc["storage"]["postgres"]["address"] == "tcp://postgres:5432"
    assert "identity_providers" in doc
    assert doc["identity_providers"]["oidc"]["jwks"][0]["algorithm"] == "RS256"


def test_build_configuration_oidc_enabled_without_clients_skips_provider():
    secrets = {
        "JWT_SECRET": "j",
        "SESSION_SECRET": "s",
        "STORAGE_PASSWORD": "p",
        "STORAGE_ENCRYPTION_KEY": "e" * 20,
        "OIDC_HMAC_SECRET": "h",
    }
    config = _base_config(oidc={"enabled": True, "clients": []})
    doc = build_configuration(config, secrets, "docker.io/authelia/authelia:4.39.4")
    assert "identity_providers" not in doc


def test_save_authelia_configuration_uses_literal_pem_blocks(tmp_path):
    from scripts.apply import save_authelia_configuration

    doc = {
        "identity_providers": {
            "oidc": {
                "jwks": [
                    {
                        "key": "-----BEGIN PRIVATE KEY-----\nline\n-----END PRIVATE KEY-----\n",
                        "certificate_chain": "-----BEGIN CERTIFICATE-----\ncert\n-----END CERTIFICATE-----\n",
                    }
                ]
            }
        }
    }
    path = tmp_path / "configuration.yml"
    save_authelia_configuration(path, doc)
    text = path.read_text()
    assert "key: |" in text
    assert "certificate_chain: |" in text
    assert "'-----BEGIN" not in text


def test_build_configuration_redis_and_filesystem_notifier():
    secrets = {
        "JWT_SECRET": "j",
        "SESSION_SECRET": "s",
        "STORAGE_PASSWORD": "p",
        "STORAGE_ENCRYPTION_KEY": "e" * 20,
        "OIDC_HMAC_SECRET": "h",
    }
    config = _base_config(
        session={"redis": {"enabled": True}},
        oidc={"enabled": False, "clients": []},
        notifier={"type": "filesystem"},
    )
    doc = build_configuration(config, secrets, "docker.io/authelia/authelia:4.39.4")
    assert doc["session"]["redis"]["host"] == "redis"
    assert "filesystem" in doc["notifier"]
    assert "identity_providers" not in doc


def test_resolve_user_passwords_map_format():
    from scripts.apply import resolve_user_passwords

    config = _base_config()
    secrets = {"ADMIN_PASSWORD": "x"}
    users = resolve_user_passwords(config, secrets, "docker.io/authelia/authelia:4.39.4")
    assert "admin" in users
    assert "username" not in users["admin"]
    assert users["admin"]["displayname"] == "Admin"
    assert users["admin"]["password"].startswith("$argon2")


def test_render_caddyfile(tmp_path, monkeypatch):
    caddy_dir = tmp_path / "caddy"
    caddy_dir.mkdir()
    template = caddy_dir / "Caddyfile.template"
    template.write_text("{{AUTH_DOMAIN_BLOCK}}\n")
    caddyfile = caddy_dir / "Caddyfile"
    monkeypatch.setattr("scripts.apply.CADDY_TEMPLATE", template)
    monkeypatch.setattr("scripts.apply.CADDYFILE", caddyfile)

    render_caddyfile(_base_config())
    text = caddyfile.read_text()
    assert "auth.test.example" in text
    assert "reverse_proxy authelia:9091" in text


def test_render_template_missing_placeholder():
    with pytest.raises(ValueError, match="Unresolved"):
        render_template("{{A}}", {})


def test_parse_authelia_password_hash_digest_prefix():
    from scripts.apply import parse_authelia_password_hash

    raw = "Digest: $argon2id$v=19$m=65536,t=3,p=4$abc$def\n"
    assert parse_authelia_password_hash(raw).startswith("$argon2id$")


def test_parse_authelia_password_hash_bare():
    from scripts.apply import parse_authelia_password_hash

    raw = "$argon2id$v=19$m=65536,t=3,p=4$abc$def"
    assert parse_authelia_password_hash(raw) == raw


def test_load_or_create_secrets_preserves_existing(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    secrets_path = state / "secrets.yaml"
    secrets_path.write_text(yaml.safe_dump({"JWT_SECRET": "keep-me"}))
    monkeypatch.setattr("scripts.apply.STATE_DIR", state)
    monkeypatch.setattr("scripts.apply.SECRETS_PATH", secrets_path)

    data = load_or_create_secrets()
    assert data["JWT_SECRET"] == "keep-me"
    assert data["SESSION_SECRET"]
