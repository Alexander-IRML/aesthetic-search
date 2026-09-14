# Runtime Compatibility And Containers

Status: implemented and locally verified on Docker Desktop 4.87.0

## Decision

ArtSearch local development, production containers, and CI use CPython 3.12.
The supported package metadata is intentionally `>=3.12,<3.13`, even though
several dependencies advertise support for newer Python releases. One minor
Python version keeps compiled wheels, Airflow tasks, Spark drivers/workers,
local tests, and deployment images on the same ABI.

`.python-version` is the canonical application and CI pin. Application images
assert that their interpreter matches it exactly during the build. The
official Airflow image asserts the same 3.12 minor while retaining the patch
release tested and published by Airflow.

Python 3.12 remains in security-fix support through October 2028. Container tags
must move to each new 3.12 security patch after a clean image build and test
run. Moving the application to Python 3.13 or 3.14 is a deliberate migration,
not an incidental virtualenv upgrade.

## Compatibility Findings

| Component | Selected | Upstream compatibility | ArtSearch decision |
| --- | --- | --- | --- |
| CPython | 3.12.14 app / 3.12.13 Airflow | 3.12 security fixes continue through Oct. 2028 | Exact 3.12 minor; follow each upstream image's security patch |
| Apache Airflow | 3.3.1 | Python 3.10-3.14; Linux production runtime | Official Python 3.12 image |
| Spark / PySpark | 4.2.0 | Python 3.10+; Java 17/21/25 | Python 3.12 and Java 17 |
| pandas | 2.3.3 | PySpark 4.2 warns that pandas 3.x is not fully supported | Explicit Airflow-image compatibility override |
| Airflow Spark provider | 6.3.1 | Airflow 2.11+, Python 3.10+ | Exact provider patch with `pyspark` extra |
| PyTorch / torchvision | 2.13.0 / 0.28.0 | Python 3.10-3.14 wheels | CPU wheel index in local containers |
| Transformers | 5.13.0 | Python 3.10+, PyTorch 2.4+ | Retain the version exercised by the model tests |
| PostgreSQL / pgvector | 17 / 0.8.6 | Current pgvector PG17 image | Canonical local control-plane service |
| Qdrant | 1.18.2 | Matches the pinned Python client minor | Rebuildable local ANN service |

The previous workstation environment used Python 3.14 and had pulled CUDA 13
libraries despite having no visible CUDA device. Local bootstrap, the `ml`
image, and the Airflow image now install the CPU wheel from PyTorch's CPU index.
A future GPU worker image should select and test one explicit CUDA runtime
separately.

Airflow dependencies are constrained by the constraint file shipped in its
official image. Application direct dependencies are separately pinned in
`constraints/runtime-py312.txt`; every image runs `pip check` during build.
This avoids letting an unrelated package release silently rewrite Airflow or
the model runtime.

The application base image currently resolves to CPython 3.12.14. The official
Airflow 3.3.1 Python 3.12 image currently resolves to CPython 3.12.13. Python's
wheel ABI is scoped to the shared 3.12 minor, so this patch-level difference is
intentional; replacing Airflow's tested official image to force patch equality
would be a worse trade.

The official Airflow 3.3.1 constraints currently select pandas 3.0.5. A live
Spark 4.2 smoke test completed but emitted PySpark's warning that pandas 3.x is
not fully supported. The Airflow image therefore applies a narrow pandas 2.3.3
override after installing under Airflow's constraints, then runs `pip check`.

## Images

The root `Dockerfile` provides four final targets:

- `runtime`: dashboard, PostgreSQL, Qdrant, data, and control-plane libraries;
  no Torch installation.
- `ml`: adds CPU-only Torch, torchvision, Transformers, SigLIP, CLIP, and CSD
  execution.
- `test`: adds development dependencies and the complete private-data-free
  source tree, then runs the repository suite.
- `cloudflare-search`: extends `ml`, validates and bakes the pinned CLIP and CSD
  model artifacts, then starts the public FastAPI search service with network-
  offline model loading.

`orchestration/airflow/Dockerfile` extends the official Airflow 3.3.1 Python
3.12 image, adds Java 17, Spark 4.2, the Spark provider, and the ArtSearch ML
runtime. Airflow still executes the current in-process DAG tasks; splitting ML
jobs into remote workers can happen after the local container contract is
proven.

## Local Stack

Install `uv`, then create the canonical local environment before using the
host-side CLIs:

```bash
scripts/bootstrap_dev.sh
source .venv/bin/activate
```

If an existing `.venv` uses another interpreter, run
`scripts/bootstrap_dev.sh --replace`. The script preserves that environment in
an ignored timestamped backup, installs CPython from `.python-version`, and
uses `constraints/runtime-py312.txt` for the same CPU dependency family as CI.

The root `compose.yaml` starts:

1. PostgreSQL 17 with pgvector and `pg_stat_statements`.
2. Qdrant 1.18.2 with a named persistent volume.
3. A one-shot Alembic migration container.
4. The model operations and review dashboard.
5. Airflow standalone with its own PostgreSQL database.
6. An optional `ml` toolbox profile for manual model commands.

All host ports bind to `127.0.0.1`. The dashboard explicitly enables review
writes on its container-wide bind because the host publication remains local.
Do not change that port to `0.0.0.0` without adding authentication and CSRF
protection.

```bash
cp .env.example .env
docker compose build
docker compose up -d
docker compose ps
```

Open `http://127.0.0.1:8765` for ArtSearch and `http://127.0.0.1:8081` for
Airflow. The root stack publishes Qdrant on host ports 6335/6336 so it can
coexist with the older development Compose stack; containers still use the
canonical internal endpoint `qdrant:6333`. View startup state with:

```bash
docker compose logs -f migrate dashboard airflow
docker compose run --rm ml artsearch-postgres \
  --config configs/production.docker.toml status
```

Run the isolated container test target with:

```bash
docker build --target test --tag artsearch-test:local .
docker run --rm artsearch-test:local
```

The current CPython 3.12.14 local verification completes with 291 tests passing
and 3 optional tests skipped. The live Airflow health endpoint previously
reported healthy metadata database, scheduler, triggerer, and DAG processor
components.

`data/` is bind-mounted for the local application but excluded from every image
build context. PostgreSQL, Qdrant, and Airflow state use Docker volumes. Local
`.env`, actor rosters, cloud configuration, model downloads, databases, and
images remain outside Git and image layers.

## Upgrade Policy

Patch upgrades are handled one family at a time. Rebuild with `--pull`, run
`pip check`, the full test target, a model smoke test, Alembic offline SQL, and
an end-to-end one-page Bluesky intake before changing a pinned version. Minor
Python, Torch, Transformers, Airflow, Spark, PostgreSQL, or Qdrant upgrades need
a dedicated compatibility change and rollback note.

Upstream references:

- <https://www.python.org/downloads/release/python-31214/>
- <https://airflow.apache.org/docs/apache-airflow/stable/installation/prerequisites.html>
- <https://airflow.apache.org/docs/docker-stack/build.html>
- <https://spark.apache.org/docs/latest/>
- <https://pypi.org/project/torch/2.13.0/>
- <https://pypi.org/project/transformers/5.13.0/>
- <https://hub.docker.com/r/pgvector/pgvector/tags>
- <https://qdrant.tech/documentation/installation/>
