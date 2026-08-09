# Integrating with Easy Deploy Engine

For a **multi-service VPS**, use `proxy.mode: integrate` so this kit does not bind :443.

```yaml
proxy:
  type: caddy
  mode: integrate
  integrate:
    network: easydeploy-net
```

Then:

1. `bash apply.sh` here — writes `.authelia-easy-deploy/integration/caddy.caddy`
2. `bash apply.sh` in [easydeploy-engine](../easydeploy-engine/) with Authelia enabled in `engine.yaml`

Standalone mode (`mode: standalone`, default) keeps the local `authelia_caddy` container.

See [easydeploy-engine/docs/integrated-vps.md](../easydeploy-engine/docs/integrated-vps.md).
