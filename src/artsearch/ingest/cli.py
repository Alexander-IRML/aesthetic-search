from __future__ import annotations

import argparse
from pathlib import Path

from artsearch.ingest.pipeline import (
    initialize_database,
    register_artists_from_manifest,
    standardize_corpus,
)


def init_db_main() -> None:
    parser = argparse.ArgumentParser(description="Initialize the ArtSearch SQLite database.")
    parser.add_argument("--config", default="config/config.yaml")
    args = parser.parse_args()
    database_path = initialize_database(args.config)
    print(f"Initialized database: {database_path}")


def register_artists_main() -> None:
    parser = argparse.ArgumentParser(description="Register artists from the manifest.")
    parser.add_argument("--config", default="config/config.yaml")
    parser.add_argument("--artists")
    args = parser.parse_args()
    artists_path = args.artists or _default_artists_manifest(args.config)
    count = register_artists_from_manifest(args.config, artists_path)
    print(f"Registered artists: {count}")


def standardize_main() -> None:
    parser = argparse.ArgumentParser(description="Standardize raw images into processed JPEGs.")
    parser.add_argument("--config", default="config/config.yaml")
    parser.add_argument("--artists")
    args = parser.parse_args()
    artists_path = args.artists or _default_artists_manifest(args.config)
    result = standardize_corpus(args.config, artists_path)
    print(
        "Standardization complete: "
        f"{result['processed']} processed, {result['skipped']} skipped, {result['errors']} errors"
    )


def _default_artists_manifest(config_path: str) -> str:
    config_dir = Path(config_path).resolve().parent
    local_manifest = config_dir / "artists.local.yaml"
    if local_manifest.exists():
        return str(local_manifest)
    return str(config_dir / "artists.yaml")
