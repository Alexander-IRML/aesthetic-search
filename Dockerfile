# syntax=docker/dockerfile:1.7

ARG PYTHON_IMAGE=python:3.12.14-slim-bookworm

FROM ${PYTHON_IMAGE} AS control-builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    VIRTUAL_ENV=/opt/venv
ENV PATH="${VIRTUAL_ENV}/bin:${PATH}"

RUN python -m venv "${VIRTUAL_ENV}"
WORKDIR /build
COPY constraints/runtime-py312.txt /tmp/runtime-py312.txt
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --constraint /tmp/runtime-py312.txt ".[api,data,postgres,qdrant]" \
    && pip check


FROM control-builder AS ml-builder

RUN pip install \
        --index-url https://download.pytorch.org/whl/cpu \
        --constraint /tmp/runtime-py312.txt \
        "torch==2.13.0" \
        "torchvision==0.28.0" \
    && pip install --constraint /tmp/runtime-py312.txt ".[embed,filter]" \
    && pip check


FROM ml-builder AS test-builder

RUN pip install --constraint /tmp/runtime-py312.txt ".[dev]" \
    && pip check


FROM ${PYTHON_IMAGE} AS runtime-base

ARG APP_VERSION=0.3.2
LABEL org.opencontainers.image.title="ArtSearch" \
      org.opencontainers.image.description="ArtSearch control and ML runtime" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.source="https://github.com/Alexander-IRML/aesthetic-search"

ENV ARTSEARCH_PROJECT_ROOT=/opt/artsearch \
    HF_HOME=/opt/artsearch/data/cache/huggingface \
    PYTHONFAULTHANDLER=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TORCH_HOME=/opt/artsearch/data/cache/torch \
    VIRTUAL_ENV=/opt/venv
ENV PATH="${VIRTUAL_ENV}/bin:${PATH}"

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates libgomp1 tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 artsearch \
    && useradd --uid 10001 --gid artsearch --create-home --shell /usr/sbin/nologin artsearch \
    && mkdir -p /opt/artsearch/data \
    && chown -R artsearch:artsearch /opt/artsearch

WORKDIR /opt/artsearch
COPY --chown=artsearch:artsearch alembic.ini ./
COPY --chown=artsearch:artsearch config ./config
COPY --chown=artsearch:artsearch configs ./configs
COPY --chown=artsearch:artsearch migrations ./migrations

USER artsearch
EXPOSE 8000 8765
ENTRYPOINT ["/usr/bin/tini", "--"]


FROM runtime-base AS runtime

COPY --from=control-builder --chown=artsearch:artsearch /opt/venv /opt/venv
CMD ["artsearch-model-dashboard", "--host", "0.0.0.0", "--port", "8765", "--allow-remote", "--no-open"]


FROM runtime-base AS ml

COPY --from=ml-builder --chown=artsearch:artsearch /opt/venv /opt/venv
CMD ["artsearch-bluesky-pipeline", "--help"]


FROM runtime-base AS test

COPY --from=test-builder --chown=artsearch:artsearch /opt/venv /opt/venv
COPY --chown=artsearch:artsearch . /opt/artsearch
CMD ["pytest", "-q"]


FROM ml AS cloudflare-search

# Container disks are ephemeral. Resolve and validate pinned model artifacts at
# image-build time so a cold start never depends on Hugging Face availability.
RUN python -m artsearch.api.model_cache --config config/config.yaml
ENV HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    TOKENIZERS_PARALLELISM=false \
    OMP_NUM_THREADS=2 \
    MKL_NUM_THREADS=2
CMD ["artsearch-api", "--app-config", "config/config.yaml", "--production-config", "configs/production.cloudflare.toml", "--api-config", "configs/search_api.cloudflare.toml", "--host", "0.0.0.0", "--port", "8000"]
