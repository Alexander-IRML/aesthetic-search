from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import subprocess
import webbrowser

from artsearch.ingest.config import load_config
from artsearch.ingest.db import connect, init_db
from artsearch.retrieval.diagnostics import patch_maxsim_diagnostics
from artsearch.retrieval.demo import write_gallery_demo, write_search_demo
from artsearch.retrieval.search import SUPPORTED_RETRIEVAL_MODES


def search_demo_main() -> None:
    parser = argparse.ArgumentParser(description="Write a local ArtSearch HTML demo.")
    parser.add_argument("artwork_id", help="Query artwork_id to search from.")
    parser.add_argument("--config", default="config/config.yaml")
    parser.add_argument("--output")
    parser.add_argument("--top-k", type=int)
    parser.add_argument(
        "--mode",
        choices=["all", *(mode.value for mode in SUPPORTED_RETRIEVAL_MODES)],
        default="all",
    )
    parser.add_argument("--include-same-artist", action="store_true")
    parser.add_argument("--review-status")
    parser.add_argument(
        "--is-sfw",
        choices=["all", "true", "false"],
        default="all",
        help="Filter candidates by SFW metadata.",
    )
    args = parser.parse_args()
    output_path = write_search_demo(
        args.artwork_id,
        config_path=args.config,
        output_path=args.output,
        top_k=args.top_k,
        mode=args.mode,
        include_same_artist=args.include_same_artist,
        review_status=args.review_status,
        is_sfw=_parse_is_sfw(args.is_sfw),
    )
    print(f"Wrote search demo: {output_path}")


def gallery_demo_main() -> None:
    parser = argparse.ArgumentParser(description="Write a local ArtSearch gallery demo.")
    parser.add_argument("--config", default="config/config.yaml")
    parser.add_argument("--output")
    parser.add_argument("--sample-per-artist", type=int, default=3)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument(
        "--mode",
        choices=[mode.value for mode in SUPPORTED_RETRIEVAL_MODES],
        default="dino_pooled",
    )
    parser.add_argument("--include-same-artist", action="store_true")
    parser.add_argument("--review-status")
    parser.add_argument(
        "--is-sfw",
        choices=["all", "true", "false"],
        default="all",
        help="Filter queries and candidates by SFW metadata.",
    )
    args = parser.parse_args()
    output_path = write_gallery_demo(
        config_path=args.config,
        output_path=args.output,
        sample_per_artist=args.sample_per_artist,
        top_k=args.top_k,
        mode=args.mode,
        include_same_artist=args.include_same_artist,
        review_status=args.review_status,
        is_sfw=_parse_is_sfw(args.is_sfw),
    )
    print(f"Wrote gallery demo: {output_path}")


def open_gallery_main() -> None:
    parser = argparse.ArgumentParser(
        description="Write a fresh local ArtSearch gallery demo and open it.",
    )
    parser.add_argument("--config", default="config/config.yaml")
    parser.add_argument("--sample-per-artist", type=int, default=3)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument(
        "--mode",
        choices=[mode.value for mode in SUPPORTED_RETRIEVAL_MODES],
        default="dino_pooled",
    )
    parser.add_argument("--include-same-artist", action="store_true")
    parser.add_argument("--review-status")
    parser.add_argument(
        "--is-sfw",
        choices=["all", "true", "false"],
        default="all",
        help="Filter queries and candidates by SFW metadata.",
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="Write a fresh gallery file without launching it.",
    )
    args = parser.parse_args()

    output_path = _fresh_gallery_output_path(args.config, args.mode)
    output_path = write_gallery_demo(
        config_path=args.config,
        output_path=output_path,
        sample_per_artist=args.sample_per_artist,
        top_k=args.top_k,
        mode=args.mode,
        include_same_artist=args.include_same_artist,
        review_status=args.review_status,
        is_sfw=_parse_is_sfw(args.is_sfw),
    )
    print(f"Wrote fresh gallery demo: {output_path}")
    if not args.no_open:
        _open_path(output_path)


def patch_diagnostics_main() -> None:
    parser = argparse.ArgumentParser(description="Inspect DINO patch MaxSim matches.")
    parser.add_argument("query_artwork_id")
    parser.add_argument("candidate_artwork_id")
    parser.add_argument("--config", default="config/config.yaml")
    parser.add_argument("--top-n", type=int, default=20)
    args = parser.parse_args()

    config = load_config(args.config)
    with connect(config.database_path) as conn:
        init_db(conn)
        matches = patch_maxsim_diagnostics(
            conn,
            config,
            args.query_artwork_id,
            args.candidate_artwork_id,
            top_n=args.top_n,
        )

    print("query_patch\tcandidate_patch\tquery_row\tquery_col\tcandidate_row\tcandidate_col\tscore")
    for match in matches:
        print(
            f"{match.query_patch_index}\t"
            f"{match.candidate_patch_index}\t"
            f"{match.query_row}\t"
            f"{match.query_col}\t"
            f"{match.candidate_row}\t"
            f"{match.candidate_col}\t"
            f"{match.score:.6f}"
        )


def _parse_is_sfw(value: str) -> bool | None:
    if value == "all":
        return None
    return value == "true"


def _fresh_gallery_output_path(config_path: str, mode: str) -> Path:
    config = load_config(config_path)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    filename = f"search_gallery_{timestamp}_{mode}.html"
    return config.root_dir / "data" / filename


def _open_path(path: Path) -> None:
    if _running_under_wsl():
        windows_path = subprocess.check_output(
            ["wslpath", "-w", str(path)],
            text=True,
        ).strip()
        subprocess.Popen(["explorer.exe", windows_path])
        return
    webbrowser.open(path.resolve().as_uri())


def _running_under_wsl() -> bool:
    try:
        release = Path("/proc/sys/kernel/osrelease").read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    return "microsoft" in release.lower() or "wsl" in release.lower()
