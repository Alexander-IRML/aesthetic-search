from __future__ import annotations

import argparse
import json
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify that ArtSearch release metadata agrees across runtimes."
    )
    parser.add_argument("--tag", help="Optional Git tag, expected to be v<project version>.")
    args = parser.parse_args()

    versions = _release_versions(ROOT)
    expected = versions["python"]
    mismatches = {name: value for name, value in versions.items() if value != expected}
    if mismatches:
        rendered = ", ".join(f"{name}={value}" for name, value in versions.items())
        parser.error(f"release versions disagree: {rendered}")

    if args.tag and args.tag != f"v{expected}":
        parser.error(f"release tag {args.tag!r} does not match v{expected}")

    print(f"ArtSearch release metadata agrees on {expected}.")
    return 0


def _release_versions(root: Path) -> dict[str, str]:
    import tomllib

    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    worker = json.loads(
        (root / "deploy/cloudflare-search/package.json").read_text(encoding="utf-8")
    )
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    match = re.search(r"^ARG APP_VERSION=([^\s]+)$", dockerfile, flags=re.MULTILINE)
    if match is None:
        raise RuntimeError("Dockerfile does not declare ARG APP_VERSION")
    return {
        "python": str(project["project"]["version"]),
        "worker": str(worker["version"]),
        "container": match.group(1),
    }


if __name__ == "__main__":
    raise SystemExit(main())
