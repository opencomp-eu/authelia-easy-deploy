#!/usr/bin/env bash
# stop.sh — stop Authelia stack (data preserved)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib.sh
source "${SCRIPT_DIR}/scripts/lib.sh"

IFS=' ' read -ra DOCKER_COMPOSE <<< "$(docker_compose_cmd)"
ENV_FILE="${SCRIPT_DIR}/.authelia-easy-deploy/compose.env"

compose_args=(-f "${SCRIPT_DIR}/compose/docker-compose.yml")
if [[ -f "${SCRIPT_DIR}/compose/redis.yml" ]] && [[ -f "${SCRIPT_DIR}/deploy.yaml" ]]; then
	if grep -q "enabled: true" "${SCRIPT_DIR}/deploy.yaml" 2>/dev/null && grep -A2 "redis:" "${SCRIPT_DIR}/deploy.yaml" | grep -q "enabled: true"; then
		compose_args+=(-f "${SCRIPT_DIR}/compose/redis.yml")
	fi
fi
if [[ -f "${SCRIPT_DIR}/.authelia-easy-deploy/compose.override.yml" ]]; then
	compose_args+=(-f "${SCRIPT_DIR}/.authelia-easy-deploy/compose.override.yml")
fi

env_args=()
if [[ -f "$ENV_FILE" ]]; then
	while IFS= read -r line || [[ -n "$line" ]]; do
		[[ -z "$line" || "$line" == \#* ]] && continue
		env_args+=("$line")
	done <"$ENV_FILE"
fi

info "Stopping Authelia stack…"
(
	cd "${SCRIPT_DIR}/compose"
	if ((${#env_args[@]})); then
		env "${env_args[@]}" "${DOCKER_COMPOSE[@]}" "${compose_args[@]}" down --remove-orphans || true
	else
		"${DOCKER_COMPOSE[@]}" "${compose_args[@]}" down --remove-orphans || true
	fi
)

success "All services stopped. Data directories are unchanged."
