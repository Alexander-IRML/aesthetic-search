from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from artsearch.artwork_filter.batch import classify_candidate_jsonl
from artsearch.artwork_filter.config import load_artwork_filter_config
from artsearch.artwork_filter.enums import FilterDecision
from artsearch.artwork_filter.errors import ArtworkFilterError
from artsearch.artwork_filter.evaluation import (
    calibrate_thresholds,
    evaluate_filter,
    write_json_report,
    write_threshold_sweep_csv,
)
from artsearch.artwork_filter.factory import build_artwork_filter_service
from artsearch.artwork_filter.labels import import_review_csv
from artsearch.artwork_filter.persistence import JSONLDecisionStore
from artsearch.artwork_filter.prompt_bank import load_prompt_bank
from artsearch.artwork_filter.review_export import export_review_queue
from artsearch.artwork_filter.schemas import FilterResult, ImageCandidate
from artsearch.artwork_filter.smoke import run_model_smoke_test
from artsearch.artwork_filter.zero_shot import PromptMatch


DEFAULT_CONFIG = "configs/artwork_filter.default.toml"
DEFAULT_PROMPTS = "configs/artwork_filter.prompts.v1.toml"


def main() -> None:
    parser = argparse.ArgumentParser(description="ArtSearch artwork-content filter.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    classify = subparsers.add_parser("classify-image", help="Classify one local image.")
    classify.add_argument("--path", required=True)
    classify.add_argument("--post-text", default="")
    classify.add_argument("--alt-text", default="")
    _add_model_args(classify)
    classify.add_argument("--output")
    classify.add_argument("--json", action="store_true")

    classify_jsonl = subparsers.add_parser(
        "classify-jsonl",
        help="Classify a streaming ImageCandidate JSONL file in model-sized batches.",
    )
    classify_jsonl.add_argument("--input", required=True)
    classify_jsonl.add_argument("--output", required=True)
    classify_jsonl.add_argument("--append", action="store_true")
    classify_jsonl.add_argument("--resume", action="store_true")
    _add_model_args(classify_jsonl)

    inspect = subparsers.add_parser(
        "inspect-prompts",
        help="Show the strongest individual SigLIP 2 prompt matches for one image.",
    )
    inspect.add_argument("--path", required=True)
    inspect.add_argument("--top-k", type=int, default=15)
    inspect.add_argument("--json", action="store_true")
    inspect.add_argument("--config", default=DEFAULT_CONFIG)
    inspect.add_argument("--prompt-config", default=DEFAULT_PROMPTS)

    info = subparsers.add_parser("info", help="Print artwork-filter config information.")
    info.add_argument("--config", default=DEFAULT_CONFIG)
    info.add_argument("--prompt-config", default=DEFAULT_PROMPTS)

    review = subparsers.add_parser(
        "export-review",
        help="Export a boundary-prioritized human review CSV.",
    )
    review.add_argument("--candidates", required=True)
    review.add_argument("--decisions", required=True)
    review.add_argument("--output", required=True)
    review.add_argument("--config", default=DEFAULT_CONFIG)
    review.add_argument(
        "--include-decision",
        action="append",
        choices=[decision.value for decision in FilterDecision],
        default=None,
        help="Decision to include; repeat as needed. Defaults to review.",
    )

    labels = subparsers.add_parser(
        "import-labels",
        help="Append completed review CSV annotations to label JSONL.",
    )
    labels.add_argument("--input", required=True)
    labels.add_argument("--output", required=True)
    labels.add_argument("--annotator", required=True)

    evaluate = subparsers.add_parser(
        "evaluate",
        help="Evaluate exact decision snapshots against the latest human labels.",
    )
    _add_evaluation_inputs(evaluate)
    evaluate.add_argument("--output", required=True)

    calibrate = subparsers.add_parser(
        "calibrate",
        help="Sweep thresholds and recommend conservative accept/reject settings.",
    )
    _add_evaluation_inputs(calibrate)
    calibrate.add_argument("--config", default=DEFAULT_CONFIG)
    calibrate.add_argument("--output", required=True)
    calibrate.add_argument("--sweep-output")
    calibrate.add_argument("--target-accept-precision", type=float, default=0.95)
    calibrate.add_argument("--target-reject-precision", type=float, default=0.95)
    calibrate.add_argument("--minimum-decisions", type=int, default=30)

    smoke = subparsers.add_parser(
        "smoke-test-model",
        help="Run real SigLIP image/prompt inference on explicit safe local images.",
    )
    smoke.add_argument("--path", action="append", required=True)
    smoke.add_argument("--config", default=DEFAULT_CONFIG)
    smoke.add_argument("--prompt-config", default=DEFAULT_PROMPTS)
    smoke.add_argument("--consistency-tolerance", type=float, default=1e-4)
    smoke.add_argument("--output")

    args = parser.parse_args()
    if args.command == "classify-image":
        result = asyncio.run(_classify_image(args))
        _print_result(result, as_json=args.json)
        return

    if args.command == "classify-jsonl":
        counts = asyncio.run(_classify_jsonl(args))
        print(json.dumps(counts, sort_keys=True))
        return

    if args.command == "inspect-prompts":
        matches = asyncio.run(_inspect_prompts(args))
        if args.json:
            print(
                json.dumps(
                    [
                        {
                            "content_class": match.content_class.value,
                            "prompt": match.prompt,
                            "score": match.score,
                        }
                        for match in matches
                    ],
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            for match in matches:
                print(f"{match.score:9.4f}  {match.content_class.value:24s}  {match.prompt}")
        return

    if args.command == "info":
        config = load_artwork_filter_config(args.config)
        prompts = load_prompt_bank(args.prompt_config)
        print(f"version: {config.version}")
        print(f"mode: {config.mode.value}")
        print(f"model: {config.model.model_id}")
        print(f"model_revision: {config.model.revision or 'unpinned'}")
        print(f"prompt_version: {prompts.version}")
        print(f"config_hash: {config.config_hash}")
        return

    if args.command == "export-review":
        config = load_artwork_filter_config(args.config)
        included = {
            FilterDecision(value)
            for value in (args.include_decision or [FilterDecision.REVIEW.value])
        }
        counts = export_review_queue(
            args.candidates,
            args.decisions,
            args.output,
            accept_score=config.thresholds.accept_score,
            reject_score=config.thresholds.reject_score,
            decisions=included,
        )
        print(json.dumps(counts, sort_keys=True))
        return

    if args.command == "import-labels":
        counts = import_review_csv(
            args.input,
            args.output,
            annotator=args.annotator,
        )
        print(json.dumps(counts, sort_keys=True))
        return

    if args.command == "evaluate":
        report = evaluate_filter(args.candidates, args.decisions, args.labels)
        write_json_report(report, args.output)
        print(json.dumps(report["dataset"], sort_keys=True))
        return

    if args.command == "calibrate":
        config = load_artwork_filter_config(args.config)
        report = calibrate_thresholds(
            args.candidates,
            args.decisions,
            args.labels,
            config,
            target_accept_precision=args.target_accept_precision,
            target_reject_precision=args.target_reject_precision,
            minimum_decisions=args.minimum_decisions,
        )
        write_json_report(report, args.output)
        if args.sweep_output:
            write_threshold_sweep_csv(report, args.sweep_output)
        print(json.dumps(report["recommendation"], sort_keys=True))
        return

    if args.command == "smoke-test-model":
        try:
            report = asyncio.run(_smoke_test_model(args))
        except (ArtworkFilterError, OSError, RuntimeError, ValueError) as exc:
            parser.error(str(exc))
        if args.output:
            write_json_report(report, args.output)
        print(json.dumps(report, indent=2, sort_keys=True))
        if not report["passed"]:
            raise SystemExit(1)
        return


def _add_model_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--prompt-config", default=DEFAULT_PROMPTS)
    parser.add_argument(
        "--deterministic-only",
        action="store_true",
        help="Run media, provenance, and text rules without loading SigLIP 2.",
    )


def _add_evaluation_inputs(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--candidates", required=True)
    parser.add_argument("--decisions", required=True)
    parser.add_argument("--labels", required=True)


async def _classify_image(args: argparse.Namespace) -> FilterResult:
    config = load_artwork_filter_config(args.config)
    store = JSONLDecisionStore(args.output) if args.output else None
    service = build_artwork_filter_service(
        config,
        prompt_config=args.prompt_config,
        decision_store=store,
        deterministic_only=args.deterministic_only,
    )
    try:
        candidate = ImageCandidate(
            candidate_id=f"local:{Path(args.path).resolve()}",
            local_path=Path(args.path),
            post_text=args.post_text,
            alt_text=args.alt_text,
            source="local",
        )
        return await service.classify(candidate)
    finally:
        await service.aclose()


async def _classify_jsonl(args: argparse.Namespace) -> dict[str, int]:
    config = load_artwork_filter_config(args.config)
    append = args.append or args.resume
    store = JSONLDecisionStore(args.output, append=append)
    service = build_artwork_filter_service(
        config,
        prompt_config=args.prompt_config,
        decision_store=store,
        deterministic_only=args.deterministic_only,
    )
    completed = False
    try:
        counts = await classify_candidate_jsonl(
            service,
            args.input,
            resume_decisions_path=args.output if args.resume else None,
        )
        store.commit(allow_empty=True)
        completed = True
        return counts
    finally:
        if not completed:
            store.abort()
        await service.aclose()


async def _inspect_prompts(args: argparse.Namespace) -> list[PromptMatch]:
    config = load_artwork_filter_config(args.config)
    service = build_artwork_filter_service(config, prompt_config=args.prompt_config)
    try:
        candidate = ImageCandidate(
            candidate_id=f"local:{Path(args.path).resolve()}",
            local_path=Path(args.path),
            source="local",
        )
        loaded = await service.image_loader.load(candidate)
        try:
            classifier = service.visual_classifier
            if classifier is None or not hasattr(classifier, "inspect_embedding"):
                raise RuntimeError("prompt inspection requires the zero-shot visual classifier")
            embedding = classifier.encode_images([loaded.rgb_image])[0]
            return classifier.inspect_embedding(embedding, top_k=args.top_k)
        finally:
            loaded.rgb_image.close()
    finally:
        await service.aclose()


async def _smoke_test_model(args: argparse.Namespace) -> dict[str, object]:
    paths = _validate_smoke_paths(args.path)
    config = load_artwork_filter_config(args.config)
    service = build_artwork_filter_service(config, prompt_config=args.prompt_config)
    try:
        return await run_model_smoke_test(
            service,
            paths,
            consistency_tolerance=args.consistency_tolerance,
        )
    finally:
        await service.aclose()


def _validate_smoke_paths(values: list[str]) -> list[Path]:
    paths = [Path(value) for value in values]
    for path in paths:
        if not path.exists():
            raise ValueError(f"smoke-test image does not exist: {path}")
        if not path.is_file():
            raise ValueError(f"smoke-test image is not a file: {path}")
    return paths


def _print_result(result: FilterResult, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result.model_dump(mode="json"), indent=2, sort_keys=True))
        return
    print(f"decision: {result.decision.value}")
    print(f"class: {result.predicted_class.value}")
    print(f"route: {result.route}")
    print(f"score: {result.final_score:.4f}")
    print(f"reasons: {', '.join(result.reason_codes)}")
