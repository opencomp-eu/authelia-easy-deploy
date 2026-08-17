#!/usr/bin/env bash
# wizard.sh — interactive setup for authelia-easy-deploy
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib.sh
source "${SCRIPT_DIR}/scripts/lib.sh"

DEPLOY_YAML="${SCRIPT_DIR}/deploy.yaml"
NO_APPLY=0
PROXY_MODE=""

usage() {
	echo "Usage: bash wizard.sh [--from-engine] [--no-apply] [--proxy-mode standalone|integrate]"
}

while [[ $# -gt 0 ]]; do
	case "$1" in
		--help|-h)
			usage
			exit 0
			;;
		--from-engine)
			NO_APPLY=1
			PROXY_MODE="integrate"
			shift
			;;
		--no-apply)
			NO_APPLY=1
			shift
			;;
		--proxy-mode)
			PROXY_MODE="${2:-}"
			shift 2
			;;
		--proxy-mode=*)
			PROXY_MODE="${1#*=}"
			shift
			;;
		*)
			die "Unknown option: $1"
			;;
	esac
done

print_banner() {
	echo
	echo -e "${BOLD}  Authelia Easy Deploy — Setup Wizard${RESET}"
	echo -e "  ─────────────────────────────────────────────────────"
	echo
}

gather_config() {
	local auth_domain sso_domain data_dir
	local admin_username admin_display_name admin_email admin_password
	local notifier_type smtp_host smtp_port smtp_username smtp_from
	local redis_enabled oidc_enabled proceed proxy_mode
	local base_domain

	print_banner
	echo -e "  Press Enter to accept a ${CYAN}[default]${RESET}.\n"

	ask auth_domain "Authelia portal domain (e.g. auth.example.com)" "auth.example.com"
	base_domain="$(base_domain_from_host "$auth_domain")"

	ask sso_domain "SSO cookie domain (e.g. example.com)" "$base_domain"

	ask data_dir "Data directory" "/var/lib/authelia"

	echo
	echo -e "${BOLD}  Initial admin user${RESET}"
	ask admin_username "Username" "admin"
	ask admin_display_name "Display name" "Admin"
	ask admin_email "Email" "admin@${sso_domain}"
	ask_secret admin_password "Password (leave empty to auto-generate on apply)"

	echo
	echo -e "${BOLD}  Notifications${RESET}"
	echo "  SMTP is recommended for production (2FA device registration emails)."
	ask notifier_type "Notifier type: smtp or filesystem" "smtp"
	notifier_type="${notifier_type,,}"
	if [[ "$notifier_type" != "smtp" && "$notifier_type" != "filesystem" ]]; then
		die "notifier type must be 'smtp' or 'filesystem'"
	fi

	smtp_host=""
	smtp_port="587"
	smtp_username=""
	smtp_from="Authelia <noreply@${sso_domain}>"
	if [[ "$notifier_type" == "smtp" ]]; then
		ask smtp_host "SMTP host" "smtp.${base_domain}"
		ask smtp_port "SMTP port" "587"
		ask smtp_username "SMTP username (optional)" ""
		ask smtp_from "From address" "$smtp_from"
	fi

	echo
	echo -e "${BOLD}  Session storage${RESET}"
	ask_yn redis_enabled "Use Redis for sessions? (recommended for production)" "n"

	echo
	echo -e "${BOLD}  OpenID Connect${RESET}"
	ask_yn oidc_enabled "Enable OIDC provider? (needed for OpenCloud / Matrix later)" "y"

	echo
	echo -e "${BOLD}  Reverse proxy${RESET}"
	if [[ -n "${PROXY_MODE}" ]]; then
		proxy_mode="${PROXY_MODE,,}"
		info "Proxy mode: ${proxy_mode} (set by easydeploy-engine)"
	else
		echo "  standalone — this kit runs Caddy on :443 (single-service VPS)"
		echo "  integrate  — shared Caddy via easydeploy-engine (multi-service VPS)"
		ask proxy_mode "Proxy mode: standalone or integrate" "standalone"
		proxy_mode="${proxy_mode,,}"
	fi
	if [[ "$proxy_mode" != "standalone" && "$proxy_mode" != "integrate" ]]; then
		die "proxy mode must be 'standalone' or 'integrate'"
	fi

	echo
	echo -e "${BOLD}  Summary${RESET}"
	echo "  Portal:        https://${auth_domain}"
	echo "  SSO domain:    ${sso_domain}"
	echo "  Data dir:      ${data_dir}"
	echo "  Admin user:    ${admin_username}"
	echo "  Notifier:      ${notifier_type}"
	echo "  Redis:         ${redis_enabled}"
	echo "  OIDC:          ${oidc_enabled}"
	echo "  Proxy mode:    ${proxy_mode}"
	echo
	echo "  Ensure DNS A/AAAA for ${auth_domain} points to this server before continuing."
	echo

	if [[ "${NO_APPLY}" == "1" ]]; then
		ask_yn proceed "Write deploy.yaml?" "y"
	else
		ask_yn proceed "Write deploy.yaml and deploy now?" "y"
	fi
	[[ "$proceed" == "y" ]] || {
		info "Cancelled."
		exit 0
	}

	cd "${SCRIPT_DIR}"
	uv run python - <<PY
from scripts.config_edit import update_from_wizard
from pathlib import Path

update_from_wizard(
    auth_domain=${auth_domain@Q},
    sso_domain=${sso_domain@Q},
    data_dir=${data_dir@Q},
    admin_username=${admin_username@Q},
    admin_display_name=${admin_display_name@Q},
    admin_email=${admin_email@Q},
    admin_password=${admin_password@Q} or None,
    notifier_type=${notifier_type@Q},
    smtp_host=${smtp_host@Q},
    smtp_port=int(${smtp_port@Q}),
    smtp_username=${smtp_username@Q},
    smtp_from=${smtp_from@Q},
    redis_enabled=${redis_enabled@Q} == "y",
    oidc_enabled=${oidc_enabled@Q} == "y",
    proxy_mode=${proxy_mode@Q},
    path=Path(${DEPLOY_YAML@Q}),
)
PY

	success "Wrote ${DEPLOY_YAML}"
}

main() {
	bash "${SCRIPT_DIR}/ensure-dependencies.sh"
	cd "${SCRIPT_DIR}"
	gather_config
	if [[ "${NO_APPLY}" == "1" ]]; then
		info "Skipping apply (--no-apply / --from-engine). easydeploy-engine will apply."
		return 0
	fi
	bash "${SCRIPT_DIR}/apply.sh"
}

main "$@"
