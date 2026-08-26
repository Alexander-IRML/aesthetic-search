from __future__ import annotations

import argparse
import json
from pathlib import Path

from artsearch.ingest.config import load_config
from artsearch.monitoring.launcher import install_desktop_launcher
from artsearch.monitoring.server import serve_monitoring_dashboard
from artsearch.monitoring.snapshot import MonitoringInputs, build_monitoring_snapshot


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    inputs = MonitoringInputs(
        app_config_path=Path(args.config),
        filter_config_path=Path(args.filter_config),
        prompt_config_path=Path(args.prompt_config),
        production_config_path=Path(args.production_config),
        monitoring_config_path=Path(args.monitoring_config),
        decisions_paths=tuple(Path(value) for value in args.decisions),
        retrieval_judgment_paths=tuple(Path(value) for value in args.judgments),
        filter_evaluation_path=(Path(args.filter_evaluation) if args.filter_evaluation else None),
        ann_evaluation_path=(Path(args.ann_evaluation) if args.ann_evaluation else None),
        filter_label_path=Path(args.review_labels),
        review_judgment_path=Path(args.review_judgments),
        review_annotator=args.annotator,
        probe_qdrant=not args.no_qdrant_probe,
    )
    try:
        if args.install_desktop_launcher:
            project_root = load_config(inputs.app_config_path).root_dir
            path = install_desktop_launcher(project_root)
            print(f"Installed ArtSearch desktop launcher: {path}")
            return
        if args.snapshot_json:
            snapshot = build_monitoring_snapshot(inputs)
            destination = Path(args.snapshot_json)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(
                json.dumps(snapshot, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            print(f"Wrote model-health snapshot: {destination.resolve()}")
            return
        if args.host not in {"127.0.0.1", "localhost", "::1"} and not args.allow_remote:
            raise ValueError("non-loopback binding requires --allow-remote")
        if args.allow_remote_review and not args.allow_remote:
            raise ValueError("--allow-remote-review requires --allow-remote")
        serve_monitoring_dashboard(
            inputs,
            host=args.host,
            port=args.port,
            open_browser=not args.no_open,
            allow_remote_review=args.allow_remote_review,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        parser.error(str(exc))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Launch the local ArtSearch model-performance operations dashboard."
    )
    parser.add_argument("--config", default="config/config.yaml")
    parser.add_argument("--filter-config", default="configs/artwork_filter.default.toml")
    parser.add_argument("--prompt-config", default="configs/artwork_filter.prompts.v1.toml")
    parser.add_argument("--production-config", default="configs/production.default.toml")
    parser.add_argument("--monitoring-config", default="configs/model_dashboard.default.toml")
    parser.add_argument("--decisions", action="append", default=[])
    parser.add_argument("--judgments", action="append", default=[])
    parser.add_argument("--filter-evaluation")
    parser.add_argument("--ann-evaluation")
    parser.add_argument(
        "--review-labels",
        default="data/filter/labels/dashboard.jsonl",
        help="Append-only output for artwork-gate labels.",
    )
    parser.add_argument(
        "--review-judgments",
        default="data/retrieval/judgments-dashboard.jsonl",
        help="Append-only output for retrieval relevance judgments.",
    )
    parser.add_argument("--annotator", default="local-reviewer")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--allow-remote", action="store_true")
    parser.add_argument(
        "--allow-remote-review",
        action="store_true",
        help="Enable review writes on a non-loopback bind; only use behind a private port.",
    )
    parser.add_argument("--no-open", action="store_true")
    parser.add_argument("--no-qdrant-probe", action="store_true")
    parser.add_argument("--snapshot-json", help="Write one JSON snapshot and exit.")
    parser.add_argument(
        "--install-desktop-launcher",
        action="store_true",
        help="Install a double-click launcher on the current desktop and exit.",
    )
    return parser


if __name__ == "__main__":
    main()
