from __future__ import annotations

import argparse

from artsearch.embed.pipeline import generate_embeddings


def embed_main() -> None:
    parser = argparse.ArgumentParser(description="Generate CLIP and DINO embeddings.")
    parser.add_argument("--config", default="config/config.yaml")
    args = parser.parse_args()
    result = generate_embeddings(args.config)
    print(
        "Embedding generation complete: "
        f"{result['processed']} processed, {result['skipped']} skipped, {result['errors']} errors"
    )
