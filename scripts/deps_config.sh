#!/usr/bin/env bash
# scripts/deps_config.sh — Authelia Easy Deploy dependency list (easydeploy-lib hook)

easydeploy_required_deps() {
	printf '%s\n' docker docker-compose git
}
