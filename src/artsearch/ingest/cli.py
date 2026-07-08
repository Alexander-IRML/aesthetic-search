from __future__ import annotations

import argparse

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
    parser.add_argument("--artists", default="config/artists.yaml")
    args = parser.parse_args()
    count = register_artists_from_manifest(args.config, args.artists)
    print(f"Registered artists: {count}")


def standardize_main() -> None:
    parser = argparse.ArgumentParser(description="Standardize raw images into processed JPEGs.")
    parser.add_argument("--config", default="config/config.yaml")
    parser.add_argument("--artists", default="config/artists.yaml")
    args = parser.parse_args()
    result = standardize_corpus(args.config, args.artists)
    print(
        "Standardization complete: "
        f"{result['processed']} processed, {result['skipped']} skipped, {result['errors']} errors"
    )