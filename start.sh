#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib.sh
source "${SCRIPT_DIR}/scripts/lib.sh"

IFS=' ' read -ra DOCKER_COMPOSE <<< "$(docker_compose_cmd)"
ENV_FILE="${SCRIPT_DIR}/.authelia-easy-deploy/compose.env"
DEPLOY="${SCRIPT_DIR}/deploy.yaml"

COMPOSE_PROJECT_NAME="authelia-easy-deploy"

compose_args=(-p "$COMPOSE_PROJECT_NAME" -f "${SCRIPT_DIR}/compose/docker-compose.yml")
integrate="false"
if [[ -f "$DEPLOY" ]] && grep -qE 'mode:\s*integrate' "$DEPLOY" 2>/dev/null; then
	integrate="true"
fi
if [[ -f "$DEPLOY" ]] && grep -A2 "redis:" "$DEPLOY" | grep -q "enabled: true"; then
	compose_args+=(-f "${SCRIPT_DIR}/compose/redis.yml")
fi
if [[ "$integrate" == "true" ]]; then
	compose_args+=(-f "${SCRIPT_DIR}/compose/integrate.yml")
else
	compose_args+=(-f "${SCRIPT_DIR}/compose/caddy.yml")
fi
[[ -f "${SCRIPT_DIR}/.authelia-easy-deploy/compose.override.yml" ]] && \
	compose_args+=(-f "${SCRIPT_DIR}/.authelia-easy-deploy/compose.override.yml")

[[ -f "$ENV_FILE" ]] || die "Missing compose env — run bash apply.sh first"

env_args=()
while IFS= read -r line || [[ -n "$line" ]]; do
	[[ -z "$line" || "$line" == \#* ]] && continue
	env_args+=("$line")
done <"$ENV_FILE"

info "Starting Authelia stack…"
(cd "${SCRIPT_DIR}/compose" && env "${env_args[@]}" "${DOCKER_COMPOSE[@]}" "${compose_args[@]}" up -d)
success "Started."
