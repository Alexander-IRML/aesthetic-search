# ArtSearch CI/CD

ArtSearch uses GitHub Actions for verification and controlled deployment of the
public Cloudflare alpha. The pipeline never uploads the local corpus, SQLite
database, model caches, ignored production configuration, or provider secrets.

## Continuous Integration

`.github/workflows/ci.yml` runs for pull requests into `main`, pushes to `main`,
manual checks, and calls from the deployment workflow. Its jobs are independent
so failures identify the affected boundary quickly:

| Job | Contract |
| --- | --- |
| Python quality and tests | CPython 3.12.14, constrained CPU model/runtime dependencies, `pip check`, Ruff, bytecode compilation, and the offline pytest suite |
| PostgreSQL migrations | Applies every Alembic revision twice against a disposable PostgreSQL 17 plus pgvector database, then checks canonical status |
| Cloudflare Worker | Uses `npm ci`, regenerates Wrangler bindings, type-checks TypeScript, and bundles with `wrangler deploy --dry-run` while explicitly skipping Container rollout |
| Container configuration | Parses the local, alpha, and Airflow Compose stacks without starting services |

The Python tests force Hugging Face and Transformers offline. A repository guard
fails CI if tracked files include `data/`, credential containers, databases,
model artifacts, or NumPy feature files. Pull-request jobs receive read-only
repository permissions and no production secrets. The workflow deliberately
does not use `pull_request_target`.

## Continuous Delivery

`.github/workflows/deploy-alpha.yml` runs for a `v*` tag or a manual dispatch.
It first calls the complete CI workflow. One serialized deployment job then:

1. waits on the protected GitHub `alpha` environment;
2. verifies Python, Worker, Container, and tag versions agree;
3. applies Alembic migrations through the Neon direct endpoint;
4. verifies the resulting database revision;
5. installs the lockfile-pinned Wrangler toolchain;
6. builds and deploys the Cloudflare Worker and model Container;
7. probes `/health/edge`, `/search`, and `/api/runtime-status` with retries.

The smoke test does not submit an image or force the model Container awake. A
real post-release image query remains an explicit operator canary because it
uses paid compute and needs a known-safe query image.

## One-Time GitHub Setup

In the repository settings, create an environment named `alpha`. Restrict it to
the `main` branch and release tags, and add a required reviewer if the repository
plan supports environment protection. Store these as environment secrets:

| Name | Purpose |
| --- | --- |
| `CLOUDFLARE_ACCOUNT_ID` | Selects the deployment account |
| `CLOUDFLARE_API_TOKEN` | Narrowly scoped Worker and Container deployment token |
| `DATABASE_DIRECT_URL` | Neon direct connection used only by Alembic |

Optionally set the environment variable `ARTSEARCH_ALPHA_BASE_URL` when moving
from the current `workers.dev` URL to a custom domain. Set it to the origin with
no trailing route, for example `https://search.example.com`. Runtime
`DATABASE_URL`, `QDRANT_URL`, and `QDRANT_API_KEY` remain Cloudflare Worker
secrets and are not copied into GitHub Actions. R2 write credentials remain
exclusive to the intake control plane.

The Cloudflare token should be scoped to the single ArtSearch account and only
the resources Wrangler needs. Never paste a token into a workflow file, commit,
tag, job argument, or build argument.

## Release

Before making a release, update all three version declarations:

- `pyproject.toml`;
- `deploy/cloudflare-search/package.json`;
- `ARG APP_VERSION` in `Dockerfile`.

Then run locally:

```bash
VERSION=0.7.0
python scripts/check_release_version.py --tag "v${VERSION}"
.venv/bin/ruff check .
.venv/bin/pytest -q
```

Pushing a matching tag starts the protected deployment after CI succeeds:

```bash
git tag "v${VERSION}"
git push origin "v${VERSION}"
```

For a code-only repair, select **Deploy alpha** under GitHub Actions and use
**Run workflow** on the intended commit. GitHub environment approval still
applies.

## Failure And Rollback

A CI failure publishes nothing. A migration or Container build failure stops
the remaining deployment steps. Cloudflare retains Worker versions, so an
operator can use the Cloudflare deployment history or `npx wrangler rollback`
from `deploy/cloudflare-search` to restore a previous Worker version.

Database state is not part of a Cloudflare rollback. Production migrations must
therefore remain backward compatible with the currently deployed application.
Do not automatically run Alembic downgrades. If the edge smoke check fails,
inspect the GitHub job and Cloudflare logs before running the documented manual
rollback.

Monthly Dependabot groups cover Python, the Cloudflare npm package, GitHub
Actions, and the root Dockerfile. Dependency pull requests pass through the same
CI gates and never deploy by themselves.
