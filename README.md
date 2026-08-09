# Authelia Easy Deploy

Opinionated, wizard-driven [Authelia](https://www.authelia.com/) deployment for a single VPS: Docker Compose (Authelia, PostgreSQL, optional Redis, Caddy), `deploy.yaml` configuration, and generated secrets.

Designed to become the shared authentication/authorization server for other **easy-deploy** projects (OpenCloud, Matrix, and more). Standalone MVP serves the login portal on HTTPS; service integration is documented in [docs/integrating-services.md](docs/integrating-services.md).

## Requirements

- Linux host with Docker Engine and Docker Compose v2
- DNS `A`/`AAAA` for your Authelia portal domain (e.g. `auth.example.com`)
- [uv](https://docs.astral.sh/uv/) (installed automatically by `ensure-dependencies.sh`)

Authelia must be reached over **HTTPS** (Caddy obtains certificates automatically).

### Proxy modes

- **`proxy.mode: standalone`** (default) — this repo runs `authelia_caddy` on ports 80/443.
- **`proxy.mode: integrate`** — no local Caddy; emits a fragment for [easydeploy-engine](../easydeploy-engine/) (multi-service VPS). See [docs/integrating-engine.md](docs/integrating-engine.md).

## Quick start

```bash
git clone --recurse-submodules https://github.com/YOUR_ORG/authelia-easy-deploy.git
cd authelia-easy-deploy
bash ensure-dependencies.sh
bash wizard.sh
```

Or manually:

```bash
cp deploy.yaml.example deploy.yaml
# edit deploy.yaml
bash apply.sh
```

## Configuration

- **`deploy.yaml`** — operator settings (domains, users, SMTP, Redis, OIDC).
- **`.authelia-easy-deploy/secrets.yaml`** — generated secrets (auto-created on first apply; do not commit).
- **`/var/lib/authelia`** (default) — Authelia config, user database, and secret files mounted into the container.

Pin the Authelia image tag in `deploy.yaml` (`authelia.tag`) instead of floating `latest` for production.

### Notifier

- **SMTP** — recommended for production (2FA device registration emails).
- **filesystem** — writes mail to `config/notification.txt` inside the data dir (testing only).

## Day-to-day

```bash
bash apply.sh              # re-render config and reconcile stack
bash apply.sh --skip-runtime   # render only, no docker
bash start.sh              # compose up (via apply, skip pull)
bash stop.sh               # compose down
```

## Backups

Back up:

- PostgreSQL volume `authelia_postgres_data` (or dump the DB)
- `authelia.data_dir` (configuration, users, secrets on disk)

## Development

```bash
uv sync --dev
uv run pytest
```

## License

Same as sibling easy-deploy projects (add license file if publishing).
