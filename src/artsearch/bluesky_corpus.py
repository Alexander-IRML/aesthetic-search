from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any

from artsearch.artwork_filter.config import ArtworkFilterConfig
from artsearch.artwork_filter.corpus import CorpusRouter
from artsearch.artwork_filter.corpus_store import ArtworkFilterCorpusStore
from artsearch.artwork_filter.ensemble import ACCEPTED_ROUTES
from artsearch.artwork_filter.enums import FilterDecision
from artsearch.artwork_filter.image_io import ImageLoader
from artsearch.artwork_filter.review_export import (
    load_decision_history,
    load_latest_candidates,
)
from artsearch.artwork_filter.schemas import FilterResult, ImageCandidate
from artsearch.ingest.config import AppConfig
from artsearch.ingest.db import connect, init_db


CORPUS_SEED_REASON = "accept.siglip_corpus_seed"


@dataclass(frozen=True)
class SelectedCorpusItem:
    candidate: ImageCandidate
    result: FilterResult


@dataclass(frozen=True)
class CorpusSelection:
    items: list[SelectedCorpusItem]
    counts: dict[str, int]


@dataclass(frozen=True)
class CorpusArchive:
    archive_dir: Path
    manifest_path: Path
    moved: dict[str, str]


def select_siglip_corpus(
    candidates_path: str | Path,
    decisions_path: str | Path,
    *,
    required_config_hash: str | None = None,
    include_provenance_review: bool = False,
    max_items: int | None = None,
) -> CorpusSelection:
    """Select current, artwork-like SigLIP decisions without mutating their evidence."""
    if max_items is not None and max_items <= 0:
        raise ValueError("max_items must be positive")

    candidates = load_latest_candidates(candidates_path)
    histories = load_decision_history(decisions_path)
    counts = {
        "candidates": len(candidates),
        "decision_histories": len(histories),
        "selected": 0,
        "missing_decision": 0,
        "stale_decision": 0,
        "config_mismatch": 0,
        "excluded_source": 0,
        "excluded_outcome": 0,
        "excluded_without_visual_scores": 0,
        "excluded_class": 0,
        "excluded_provenance": 0,
        "limited": 0,
    }
    selected: list[SelectedCorpusItem] = []

    for candidate in candidates.values():
        history = histories.get(candidate.candidate_id)
        if not history:
            counts["missing_decision"] += 1
            continue
        result = _latest_current_result(candidate, history)
        if result is None:
            counts["stale_decision"] += 1
            continue
        if required_config_hash and result.config_hash != required_config_hash:
            counts["config_mismatch"] += 1
            continue
        if candidate.source != "bluesky":
            counts["excluded_source"] += 1
            continue
        if result.decision not in {FilterDecision.ACCEPT, FilterDecision.REVIEW}:
            counts["excluded_outcome"] += 1
            continue
        if result.visual_scores is None:
            counts["excluded_without_visual_scores"] += 1
            continue
        if result.predicted_class not in ACCEPTED_ROUTES:
            counts["excluded_class"] += 1
            continue
        if (candidate.is_repost or candidate.is_quote_post) and not include_provenance_review:
            counts["excluded_provenance"] += 1
            continue
        if max_items is not None and len(selected) >= max_items:
            counts["limited"] += 1
            continue
        selected.append(SelectedCorpusItem(candidate=candidate, result=result))

    counts["selected"] = len(selected)
    return CorpusSelection(items=selected, counts=counts)


def promote_siglip_result(result: FilterResult) -> FilterResult:
    """Create explicit corpus-ingest evidence while preserving the model result fields."""
    if result.visual_scores is None or result.predicted_class not in ACCEPTED_ROUTES:
        raise ValueError("only artwork-like visual results can seed the corpus")
    reason_codes = [*result.reason_codes]
    if CORPUS_SEED_REASON not in reason_codes:
        reason_codes.append(CORPUS_SEED_REASON)
    return result.model_copy(
        update={
            "decision": FilterDecision.ACCEPT,
            "accepted_for_main_corpus": True,
            "route": ACCEPTED_ROUTES[result.predicted_class],
            "reason_codes": reason_codes,
        }
    )


async def seed_siglip_corpus(
    selection: CorpusSelection,
    *,
    app_config: AppConfig,
    filter_config: ArtworkFilterConfig,
    image_loader: ImageLoader | None = None,
) -> dict[str, int]:
    """Download selected full-size images and import canonical files into fresh SQLite."""
    with connect(app_config.database_path) as conn:
        init_db(conn)
        conn.commit()

    totals = {
        "selected": len(selection.items),
        "routed": 0,
        "decisions": 0,
        "imported": 0,
        "unchanged": 0,
        "duplicates": 0,
        "review_stored": 0,
        "errors": 0,
    }
    router = CorpusRouter(
        filter_config,
        raw_dir=app_config.raw_dir,
        image_loader=image_loader,
        download_review_images=False,
    )
    store = ArtworkFilterCorpusStore(app_config, filter_config)
    try:
        for batch in _chunks(selection.items, filter_config.model.batch_size):
            candidates = [item.candidate for item in batch]
            promoted_results = [promote_siglip_result(item.result) for item in batch]
            routed = await router.route_many(candidates, promoted_results)
            totals["routed"] += len(routed)
            batch_counts = store.persist_batch(candidates, promoted_results, routed)
            for key, value in batch_counts.items():
                totals[key] += value
    finally:
        await router.aclose()
    return totals


def archive_active_corpus(
    app_config: AppConfig,
    *,
    archive_root: str | Path | None = None,
    now: datetime | None = None,
) -> CorpusArchive:
    """Move the active corpus aside so replacing it remains reversible."""
    created_at = now or datetime.now(timezone.utc)
    root = (
        Path(archive_root)
        if archive_root is not None
        else app_config.root_dir / "data" / "corpus_archive"
    )
    archive_dir = _available_archive_dir(
        root,
        created_at.strftime("%Y%m%dT%H%M%SZ"),
    )
    archive_dir.mkdir(parents=True, exist_ok=False)

    sources = _archive_sources(app_config)
    moved: dict[str, str] = {}
    try:
        for source in sources:
            if not source.exists():
                continue
            destination = archive_dir / _archive_relative_path(app_config.root_dir, source)
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, destination)
            moved[str(source)] = str(destination)
    finally:
        manifest_path = archive_dir / "manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "created_at": created_at.isoformat(),
                    "root_dir": str(app_config.root_dir),
                    "database_path": str(app_config.database_path),
                    "raw_dir": str(app_config.raw_dir),
                    "processed_dir": str(app_config.processed_dir),
                    "moved": moved,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    return CorpusArchive(
        archive_dir=archive_dir,
        manifest_path=manifest_path,
        moved=moved,
    )


def selection_report(selection: CorpusSelection) -> dict[str, Any]:
    class_counts: dict[str, int] = {}
    artist_counts: dict[str, int] = {}
    for item in selection.items:
        class_name = item.result.predicted_class.value
        class_counts[class_name] = class_counts.get(class_name, 0) + 1
        artist = item.candidate.author_did or item.candidate.author_handle or "unknown"
        artist_counts[artist] = artist_counts.get(artist, 0) + 1
    return {
        **selection.counts,
        "class_counts": dict(sorted(class_counts.items())),
        "artist_count": len(artist_counts),
    }


def _latest_current_result(
    candidate: ImageCandidate,
    history: Sequence[FilterResult],
) -> FilterResult | None:
    matches = [result for result in history if _matches_current_source(candidate, result)]
    return matches[-1] if matches else None


def _matches_current_source(
    candidate: ImageCandidate,
    result: FilterResult,
) -> bool:
    if candidate.post_cid is not None and result.source_cid != candidate.post_cid:
        return False
    if candidate.post_cid is None and result.source_cid is not None:
        return False
    if candidate.post_uri is not None and result.source_uri != candidate.post_uri:
        return False
    if candidate.image_index != result.image_index:
        return False
    if candidate.author_did is not None and result.author_did != candidate.author_did:
        return False
    return True


def _archive_sources(app_config: AppConfig) -> list[Path]:
    database = app_config.database_path
    sources = [
        Path(f"{database}-wal"),
        Path(f"{database}-shm"),
        database,
        app_config.raw_dir,
        app_config.processed_dir,
        app_config.retrieval.demo_output_path,
        app_config.retrieval.gallery_output_path,
    ]
    unique: list[Path] = []
    seen: set[Path] = set()
    for source in sources:
        normalized = source.resolve()
        if normalized not in seen:
            unique.append(source)
            seen.add(normalized)
    return unique


def _archive_relative_path(root_dir: Path, source: Path) -> Path:
    try:
        return source.resolve().relative_to(root_dir.resolve())
    except ValueError:
        return Path("external") / source.name


def _available_archive_dir(root: Path, name: str) -> Path:
    candidate = root / name
    suffix = 1
    while candidate.exists():
        candidate = root / f"{name}-{suffix}"
        suffix += 1
    return candidate


def _chunks(
    items: Sequence[SelectedCorpusItem],
    batch_size: int,
) -> list[Sequence[SelectedCorpusItem]]:
    return [items[index : index + batch_size] for index in range(0, len(items), batch_size)]
