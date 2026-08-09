#!/usr/bin/env bash
# start.sh — start Authelia stack (includes Caddy)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib.sh
source "${SCRIPT_DIR}/scripts/lib.sh"

IFS=' ' read -ra DOCKER_COMPOSE <<< "$(docker_compose_cmd)"
ENV_FILE="${SCRIPT_DIR}/.authelia-easy-deploy/compose.env"

compose_args=(-f "${SCRIPT_DIR}/compose/docker-compose.yml")
if [[ -f "${SCRIPT_DIR}/deploy.yaml" ]] && grep -A2 "redis:" "${SCRIPT_DIR}/deploy.yaml" | grep -q "enabled: true"; then
	compose_args+=(-f "${SCRIPT_DIR}/compose/redis.yml")
fi
if [[ -f "${SCRIPT_DIR}/.authelia-easy-deploy/compose.override.yml" ]]; then
	compose_args+=(-f "${SCRIPT_DIR}/.authelia-easy-deploy/compose.override.yml")
fi

if [[ ! -f "$ENV_FILE" ]]; then
	die "Missing ${ENV_FILE} — run bash apply.sh first"
fi

env_args=()
while IFS= read -r line || [[ -n "$line" ]]; do
	[[ -z "$line" || "$line" == \#* ]] && continue
	env_args+=("$line")
done <"$ENV_FILE"

info "Starting Authelia stack…"
(
	cd "${SCRIPT_DIR}/compose"
	env "${env_args[@]}" "${DOCKER_COMPOSE[@]}" "${compose_args[@]}" up -d
)

success "All services started."
