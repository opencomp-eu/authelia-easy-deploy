# Integrating other Easy Deploy services

Authelia Easy Deploy (standalone MVP) runs its own Caddy on ports 80/443 for the **auth portal** only. Other stacks (OpenCloud, Matrix) normally run their own reverse proxy on the same host; combining them on one VPS is **phase 2** (shared network, merged Caddy, detection in the wizard).

## OpenCloud

OpenCloud can use an external OIDC provider when `auth.mode: oidc` in [`opencloud-easy-deploy`](https://github.com/opencloud-eu/opencloud-easy-deploy) `deploy.yaml`. The upstream compose kit includes hints for Authelia in `opencloud-compose/idm/external-authelia.yml` (groups claim, role assignment bootstrap).

Typical settings once Authelia OIDC is enabled:

- **Issuer URL:** `https://<authelia.domain>/.well-known/openid-configuration` (confirm the discovery URL from your Authelia portal domain in `deploy.yaml`).
- **Client:** register an OpenID client in Authelia (`oidc.clients` in `deploy.yaml`) with redirect URIs for OpenCloud.
- **Groups:** map Authelia groups (e.g. `opencloud-admin`) to OpenCloud roles via OIDC claims — see OpenCloud proxy role assignment docs.

## Matrix

Matrix Easy Deploy uses Matrix Authentication Service (MAS) for modern OIDC login flows. Point MAS at Authelia as the upstream OIDC provider (issuer, client ID/secret, scopes including `groups` if you rely on group claims).

Details depend on your Matrix `deploy.yaml` SSO section; wire issuer and client after Authelia is healthy.

## Forward authentication (Caddy)

For apps protected by **forward auth** rather than OIDC, add Caddy `forward_auth` blocks pointing at `authelia:9091` with URI `/api/authz/forward-auth`. See [Authelia Caddy integration](https://www.authelia.com/integration/proxies/caddy/).

Phase 2 of Authelia Easy Deploy will generate these snippets and attach Authelia to existing `opencloud-net` / `caddy_net` without a second public Caddy.
