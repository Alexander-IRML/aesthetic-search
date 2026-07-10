from __future__ import annotations

import argparse

from artsearch.retrieval.demo import write_gallery_demo, write_search_demo


def search_demo_main() -> None:
    parser = argparse.ArgumentParser(description="Write a local ArtSearch HTML demo.")
    parser.add_argument("artwork_id", help="Query artwork_id to search from.")
    parser.add_argument("--config", default="config/config.yaml")
    parser.add_argument("--output")
    parser.add_argument("--top-k", type=int)
    args = parser.parse_args()
    output_path = write_search_demo(
        args.artwork_id,
        config_path=args.config,
        output_path=args.output,
        top_k=args.top_k,
    )
    print(f"Wrote search demo: {output_path}")


def gallery_demo_main() -> None:
    parser = argparse.ArgumentParser(description="Write a local ArtSearch gallery demo.")
    parser.add_argument("--config", default="config/config.yaml")
    parser.add_argument("--output")
    parser.add_argument("--sample-per-artist", type=int, default=3)
    parser.add_argument("--top-k", type=int, default=10)
    args = parser.parse_args()
    output_path = write_gallery_demo(
        config_path=args.config,
        output_path=args.output,
        sample_per_artist=args.sample_per_artist,
        top_k=args.top_k,
    )
    print(f"Wrote gallery demo: {output_path}")
