#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_VERSION="$(tr -d '[:space:]' < "${ROOT}/.python-version")"
VENV="${ROOT}/.venv"
PYTHON_INSTALL_DIR="${UV_PYTHON_INSTALL_DIR:-${ROOT}/.python}"
UV_CACHE_DIR="${UV_CACHE_DIR:-${ROOT}/data/cache/uv}"
REPLACE=0

usage() {
    printf '%s\n' \
        "Usage: scripts/bootstrap_dev.sh [--replace]" \
        "" \
        "Create ArtSearch's Python ${PYTHON_VERSION} development environment." \
        "Use --replace to preserve a mismatched .venv as .venv-backup-<timestamp>."
}

case "${1:-}" in
    "") ;;
    --replace) REPLACE=1 ;;
    --help|-h)
        usage
        exit 0
        ;;
    *)
        usage >&2
        exit 2
        ;;
esac

if command -v uv >/dev/null 2>&1; then
    UV_BIN="$(command -v uv)"
elif [[ -x "${VENV}/bin/uv" ]]; then
    UV_BIN="${VENV}/bin/uv"
elif [[ -x "${ROOT}/.tools/bin/uv" ]]; then
    UV_BIN="${ROOT}/.tools/bin/uv"
else
    printf '%s\n' \
        "uv is required to install the pinned Python runtime." \
        "Install it from https://docs.astral.sh/uv/getting-started/installation/" >&2
    exit 2
fi

if [[ -x "${VENV}/bin/python" ]]; then
    CURRENT_VERSION="$(${VENV}/bin/python -c 'import platform; print(platform.python_version())')"
    if [[ "${CURRENT_VERSION}" != "${PYTHON_VERSION}" ]]; then
        if [[ "${REPLACE}" -ne 1 ]]; then
            printf 'Existing .venv uses Python %s; expected %s. Re-run with --replace.\n' \
                "${CURRENT_VERSION}" "${PYTHON_VERSION}" >&2
            exit 2
        fi
        BACKUP="${ROOT}/.venv-backup-$(date -u +%Y%m%dT%H%M%SZ)"
        mv "${VENV}" "${BACKUP}"
        if [[ "${UV_BIN}" == "${VENV}/"* ]]; then
            UV_BIN="${BACKUP}/${UV_BIN#"${VENV}/"}"
        fi
        printf 'Preserved the previous environment at %s\n' "${BACKUP}"
    fi
fi

export UV_PYTHON_INSTALL_DIR="${PYTHON_INSTALL_DIR}"
export UV_CACHE_DIR

"${UV_BIN}" python install "${PYTHON_VERSION}"

if [[ ! -x "${VENV}/bin/python" ]]; then
    "${UV_BIN}" venv --seed --python "${PYTHON_VERSION}" "${VENV}"
fi

if ! "${VENV}/bin/python" -m pip --version >/dev/null 2>&1; then
    "${UV_BIN}" pip install --python "${VENV}/bin/python" pip
fi

"${UV_BIN}" pip install \
    --python "${VENV}/bin/python" \
    --constraint "${ROOT}/constraints/runtime-py312.txt" \
    --editable "${ROOT}[dev,data,postgres,qdrant,scale]"
"${UV_BIN}" pip install \
    --python "${VENV}/bin/python" \
    --index-url https://download.pytorch.org/whl/cpu \
    --constraint "${ROOT}/constraints/runtime-py312.txt" \
    "torch==2.13.0" \
    "torchvision==0.28.0"
"${UV_BIN}" pip install \
    --python "${VENV}/bin/python" \
    --constraint "${ROOT}/constraints/runtime-py312.txt" \
    --editable "${ROOT}[embed,filter]"

"${UV_BIN}" pip check --python "${VENV}/bin/python"
"${VENV}/bin/python" -c \
    'import platform; print(f"ArtSearch environment ready: Python {platform.python_version()}")'
