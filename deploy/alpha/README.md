# Alpha Control Plane

This Compose file runs only the private scheduled-work control plane on a cloud
VM. It does not expose the local model dashboard as a public product and it does
not claim to contain the future ArtSearch query API.

## Before Starting

1. Complete the provider setup in `docs/alpha_deployment_runbook.md`.
2. Place the ignored actor roster at `config/bluesky_actors.local.txt`.
3. Copy and configure `configs/production.alpha-cloud.local.toml`.
4. Copy `.env.example` to `.env`, set permissions to owner read/write only, and
   fill the runtime secrets.
5. Run the canary preflight from the project environment.

The same ignored `.env` also holds the public search deployment's managed
database, Qdrant, and generated alpha-access secrets. Generate the access values
without printing them by running `npm run access:init` from
`deploy/cloudflare-search/`.

```bash
.venv/bin/artsearch-deploy preflight \
  --production-config configs/production.alpha-cloud.local.toml
```

## Start

```bash
docker compose --env-file deploy/alpha/.env \
  -f deploy/alpha/compose.yaml up --build -d
```

Airflow binds to VM loopback only. From a trusted workstation, open a tunnel:

```bash
ssh -L 8080:127.0.0.1:8080 artsearch-alpha-01
```

Then visit `http://127.0.0.1:8080`. Do not add a public `0.0.0.0` binding to
Airflow. A future authenticated public API receives its own container, health
checks, domain, and resource limits.

## Stop and Inspect

```bash
docker compose --env-file deploy/alpha/.env \
  -f deploy/alpha/compose.yaml ps
docker compose --env-file deploy/alpha/.env \
  -f deploy/alpha/compose.yaml logs --tail 200 airflow
docker compose --env-file deploy/alpha/.env \
  -f deploy/alpha/compose.yaml down
```

The named `artsearch-data` and `airflow-state` volumes survive `down`. Do not use
`down --volumes` during ordinary operation.
