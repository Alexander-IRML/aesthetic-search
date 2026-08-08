from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
import hashlib
from html import escape
import json
import os
from pathlib import Path
import random
import secrets
import sqlite3

from artsearch.ingest.config import AppConfig, load_config
from artsearch.ingest.db import connect, init_db
from artsearch.retrieval.dashboard import render_gallery_html
from artsearch.retrieval.search import (
    SUPPORTED_RETRIEVAL_MODES,
    RetrievalMode,
    SearchFilters,
    SearchResult,
    get_artwork_for_demo,
    search_similar,
)


def write_search_demo(
    query_artwork_id: str,
    *,
    config_path: str | Path = "config/config.yaml",
    output_path: str | Path | None = None,
    top_k: int | None = None,
    mode: RetrievalMode | str | None = None,
    include_same_artist: bool = False,
    review_status: str | None = None,
    is_sfw: bool | None = None,
) -> Path:
    config = load_config(config_path)
    destination = (
        Path(output_path)
        if output_path is not None
        else config.retrieval.demo_output_path
    )
    if not destination.is_absolute():
        destination = config.root_dir / destination
    destination.parent.mkdir(parents=True, exist_ok=True)

    filters = SearchFilters(
        top_k=top_k,
        include_same_artist=include_same_artist,
        review_status=review_status,
        is_sfw=is_sfw,
    )
    modes = _mode_list(mode, default=SUPPORTED_RETRIEVAL_MODES)

    with connect(config.database_path) as conn:
        init_db(conn)
        query = get_artwork_for_demo(conn, config, query_artwork_id)
        mode_payloads = []
        for retrieval_mode in modes:
            results = search_similar(
                conn,
                config,
                query_artwork_id,
                mode=retrieval_mode,
                filters=filters,
            )
            mode_payloads.append(
                _mode_payload(config, destination, retrieval_mode, query, results)
            )

    destination.write_text(
        _render_search_html(
            query_artwork_id=query_artwork_id,
            mode_payloads=mode_payloads,
            filters=filters,
        ),
        encoding="utf-8",
    )
    return destination


def write_gallery_demo(
    *,
    config_path: str | Path = "config/config.yaml",
    output_path: str | Path | None = None,
    sample_per_artist: int = 3,
    review_session_count: int | None = None,
    review_seed: str | None = None,
    top_k: int = 10,
    mode: RetrievalMode | str = RetrievalMode.ENSEMBLE,
    include_same_artist: bool = False,
    review_status: str | None = None,
    is_sfw: bool | None = None,
) -> Path:
    config = load_config(config_path)
    if sample_per_artist <= 0:
        raise ValueError("sample_per_artist must be positive")
    session_count = (
        config.retrieval.review_session_count
        if review_session_count is None
        else review_session_count
    )
    if session_count <= 0:
        raise ValueError("review_session_count must be positive")
    resolved_review_seed = review_seed or secrets.token_hex(8)
    destination = (
        Path(output_path)
        if output_path is not None
        else config.retrieval.gallery_output_path
    )
    if not destination.is_absolute():
        destination = config.root_dir / destination
    destination.parent.mkdir(parents=True, exist_ok=True)

    retrieval_mode = RetrievalMode.coerce(mode)
    filters = SearchFilters(
        top_k=top_k,
        include_same_artist=include_same_artist,
        review_status=review_status,
        is_sfw=is_sfw,
    )
    generated_at = datetime.now(timezone.utc)

    with connect(config.database_path) as conn:
        init_db(conn)
        corpus_fingerprint = _corpus_fingerprint(conn)
        dashboard_seed = hashlib.sha256(resolved_review_seed.encode("utf-8")).hexdigest()[:8]
        dashboard_id = (
            f"{generated_at.strftime('%Y%m%dT%H%M%S%fZ')}-"
            f"{corpus_fingerprint[:12]}-{dashboard_seed}"
        )
        query_pool = _sample_gallery_queries(
            conn,
            config,
            max(sample_per_artist, session_count),
            filters,
            seed=resolved_review_seed,
        )
        queries = _take_queries_per_artist(query_pool, sample_per_artist)
        review_query_sessions = _review_query_sessions(
            query_pool,
            session_count,
            seed=resolved_review_seed,
        )
        evidence = _load_siglip_evidence(conn)
        result_cache: dict[tuple[str, RetrievalMode], list[SearchResult]] = {}

        def ranked(query: SearchResult, retrieval_mode: RetrievalMode) -> list[SearchResult]:
            key = (query.artwork_id, retrieval_mode)
            if key not in result_cache:
                result_cache[key] = search_similar(
                    conn,
                    config,
                    query.artwork_id,
                    mode=retrieval_mode,
                    filters=filters,
                )
            return result_cache[key]

        query_payloads = []
        for query in queries:
            query_payloads.append(
                _gallery_payload(
                    config,
                    destination,
                    retrieval_mode,
                    query,
                    ranked(query, retrieval_mode),
                    evidence,
                )
            )

        review_sessions = []
        all_evaluation_payloads = []
        for session_index, session_queries in enumerate(review_query_sessions):
            evaluation_payloads = [
                {
                    "query": _demo_item(config, destination, query, evidence),
                    "modes": [
                        _gallery_payload(
                            config,
                            destination,
                            mode,
                            query,
                            ranked(query, mode),
                            evidence,
                        )
                        for mode in SUPPORTED_RETRIEVAL_MODES
                    ],
                }
                for query in session_queries
            ]
            all_evaluation_payloads.extend(evaluation_payloads)
            review_sessions.append(
                {
                    "id": _review_session_id(
                        corpus_fingerprint,
                        resolved_review_seed,
                        session_index,
                        session_queries,
                    ),
                    "number": session_index + 1,
                    "queryCount": len(evaluation_payloads),
                    "artistCount": len({query.artist_id for query in session_queries}),
                    "queries": evaluation_payloads,
                    "funnelStats": _funnel_stats(evaluation_payloads),
                }
            )

        model_info = _model_info(conn, config, evidence)

    destination.write_text(
        render_gallery_html(
            {
                "schemaVersion": "3.0",
                "dashboardId": dashboard_id,
                "generatedAt": generated_at.isoformat(),
                "corpusFingerprint": corpus_fingerprint,
                "mode": _mode_info(retrieval_mode),
                "filters": _filters_payload(filters),
                "queries": query_payloads,
                "evaluation": {
                    "sessions": review_sessions,
                    "sessionPlan": {
                        "seed": resolved_review_seed,
                        "requestedSessions": session_count,
                        "generatedSessions": len(review_sessions),
                        "selection": "seeded_artist_balanced_without_replacement",
                    },
                    "modelInfo": model_info,
                    "funnelStats": _funnel_stats(all_evaluation_payloads),
                    "filterStats": _filter_stats(evidence),
                    "metricSemantics": {
                        "recall": (
                            "Recall is measured only within the displayed judged pool; "
                            "full-corpus recall requires exhaustive relevance labels."
                        ),
                        "prediction": (
                            "Every displayed top-k result is a model MATCH guess. "
                            "Scores are similarities, not calibrated probabilities."
                        ),
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    return destination


def _mode_list(
    mode: RetrievalMode | str | None,
    *,
    default: Sequence[RetrievalMode],
) -> list[RetrievalMode]:
    if mode is None or mode == "all":
        return list(default)
    return [RetrievalMode.coerce(mode)]


def _mode_info(mode: RetrievalMode) -> dict:
    task, question, role = {
        RetrievalMode.ENSEMBLE: (
            "ensemble",
            "Is this a useful overall visual match after global recall and local reranking?",
            "DINO pooled shortlist, DINO patch rerank, CLIP semantic evidence",
        ),
        RetrievalMode.CLIP_SUBJECT: (
            "subject",
            "Is this a useful subject or semantic match?",
            "Global semantic image vector",
        ),
        RetrievalMode.DINO_POOLED: (
            "style",
            "Is this a useful style or global visual-feel match?",
            "Global visual/style baseline",
        ),
        RetrievalMode.DINO_PATCH_MAXSIM: (
            "local_detail",
            "Does this share a useful local item, form, or structural detail?",
            "Patch-token late interaction",
        ),
    }[mode]
    return {
        "value": mode.value,
        "label": mode.label,
        "task": task,
        "question": question,
        "role": role,
    }


def _mode_payload(
    config: AppConfig,
    destination: Path,
    mode: RetrievalMode,
    query: SearchResult,
    results: list[SearchResult],
) -> dict:
    return {
        "mode": _mode_info(mode),
        "query": _demo_item(config, destination, query),
        "results": [_demo_item(config, destination, result) for result in results],
    }


def _gallery_payload(
    config: AppConfig,
    destination: Path,
    mode: RetrievalMode,
    query: SearchResult,
    results: list[SearchResult],
    evidence: dict[str, dict] | None = None,
) -> dict:
    return {
        "mode": _mode_info(mode),
        "query": _demo_item(config, destination, query, evidence),
        "results": [
            _demo_item(config, destination, result, evidence)
            for result in results
        ],
    }


def _demo_item(
    config: AppConfig,
    destination: Path,
    result: SearchResult,
    evidence: dict[str, dict] | None = None,
) -> dict:
    return {
        "artworkId": result.artwork_id,
        "artistId": result.artist_id,
        "artistName": result.artist_display_name,
        "imageSrc": _relative_image_src(config, destination, result.processed_path),
        "score": result.score,
        "mode": result.mode.value,
        "reviewStatus": result.review_status,
        "isSfw": result.is_sfw,
        "retrieval": _retrieval_evidence(result),
        "siglip": (evidence or {}).get(result.artwork_id),
    }


def _retrieval_evidence(result: SearchResult) -> dict | None:
    components = {
        "pooled": {
            "score": result.pooled_score,
            "rank": result.pooled_rank,
            "role": "stage_1_recall",
        },
        "patch": {
            "score": result.patch_score,
            "rank": result.patch_rank,
            "role": "stage_2_rerank",
        },
        "clip": {
            "score": result.clip_score,
            "rank": result.clip_rank,
            "role": "parallel_semantic_evidence",
        },
    }
    available = {
        name: component
        for name, component in components.items()
        if component["score"] is not None
    }
    if not available:
        return None
    return {
        "orderingSignal": (
            "patch" if result.mode == RetrievalMode.ENSEMBLE else result.mode.value
        ),
        "components": available,
        "shortlistSize": result.shortlist_size,
        "candidateCount": result.candidate_count,
        "patchMatchTopN": result.patch_match_top_n,
    }


def _filters_payload(filters: SearchFilters) -> dict:
    return {
        "topK": filters.top_k,
        "includeSameArtist": filters.include_same_artist,
        "reviewStatus": filters.review_status,
        "isSfw": filters.is_sfw,
    }


def _relative_image_src(config: AppConfig, destination: Path, processed_path: str) -> str:
    image_path = Path(processed_path)
    if not image_path.is_absolute():
        image_path = config.root_dir / image_path
    return Path(os.path.relpath(image_path, destination.parent)).as_posix()


def _sample_gallery_queries(
    conn,
    config: AppConfig,
    sample_per_artist: int,
    filters: SearchFilters,
    *,
    seed: str,
) -> list[SearchResult]:
    rows = conn.execute(
        """
        SELECT
            artworks.artwork_id,
            artworks.artist_id,
            artists.display_name AS artist_display_name,
            artworks.processed_path,
            artworks.review_status,
            artworks.is_sfw
          FROM artworks
          JOIN artists ON artists.artist_id = artworks.artist_id
          JOIN embeddings ON embeddings.artwork_id = artworks.artwork_id
         WHERE artworks.validated = 1
           AND artworks.processed_path IS NOT NULL
           AND embeddings.model_name_dino = ?
           AND embeddings.model_version_dino = ?
           AND embeddings.model_name_clip = ?
           AND embeddings.model_version_clip = ?
         ORDER BY artists.display_name, artworks.artwork_id
        """,
        (
            config.models.dino_model_name,
            config.models.dino_model_version,
            config.models.clip_model_name,
            config.models.clip_model_version,
        ),
    ).fetchall()

    by_artist = {}
    for row in rows:
        if filters.review_status is not None and row["review_status"] != filters.review_status:
            continue
        is_sfw = _bool_or_none(row["is_sfw"])
        if filters.is_sfw is not None and is_sfw != filters.is_sfw:
            continue
        by_artist.setdefault(row["artist_id"], []).append(row)

    queries = []
    for artist_id in sorted(by_artist):
        artist_rows = list(by_artist[artist_id])
        random.Random(f"{seed}:{artist_id}").shuffle(artist_rows)
        for row in artist_rows[:sample_per_artist]:
            queries.append(
                SearchResult(
                    artwork_id=row["artwork_id"],
                    artist_id=row["artist_id"],
                    artist_display_name=row["artist_display_name"],
                    processed_path=row["processed_path"],
                    score=1.0,
                    review_status=row["review_status"],
                    is_sfw=_bool_or_none(row["is_sfw"]),
                )
            )
    return queries


def _take_queries_per_artist(
    queries: Sequence[SearchResult],
    count: int,
) -> list[SearchResult]:
    selected: dict[str, list[SearchResult]] = {}
    for query in queries:
        artist_queries = selected.setdefault(query.artist_id, [])
        if len(artist_queries) < count:
            artist_queries.append(query)
    return [query for artist_queries in selected.values() for query in artist_queries]


def _review_query_sessions(
    queries: Sequence[SearchResult],
    count: int,
    *,
    seed: str,
) -> list[list[SearchResult]]:
    by_artist: dict[str, list[SearchResult]] = {}
    for query in queries:
        by_artist.setdefault(query.artist_id, []).append(query)

    sessions = []
    for session_index in range(count):
        session_queries = [
            artist_queries[session_index]
            for artist_queries in by_artist.values()
            if session_index < len(artist_queries)
        ]
        if not session_queries:
            break
        random.Random(f"{seed}:review-session:{session_index}").shuffle(session_queries)
        sessions.append(session_queries)
    return sessions


def _review_session_id(
    corpus_fingerprint: str,
    seed: str,
    session_index: int,
    queries: Sequence[SearchResult],
) -> str:
    query_ids = "|".join(query.artwork_id for query in queries)
    digest = hashlib.sha256(
        f"{corpus_fingerprint}|{seed}|{session_index}|{query_ids}".encode("utf-8")
    ).hexdigest()[:12]
    return f"review-{session_index + 1:02d}-{digest}"


def _load_siglip_evidence(conn: sqlite3.Connection) -> dict[str, dict]:
    rows = conn.execute(
        """
        SELECT
            routes.artwork_id,
            decisions.decision,
            decisions.predicted_class,
            decisions.route,
            decisions.final_score,
            decisions.confidence,
            decisions.reason_codes_json,
            decisions.evidence_json,
            decisions.model_id,
            decisions.model_revision,
            decisions.prompt_version,
            decisions.config_version,
            decisions.processed_at
          FROM artwork_filter_routes AS routes
          JOIN artwork_filter_decisions AS decisions
            ON decisions.decision_key = routes.decision_key
         WHERE routes.artwork_id IS NOT NULL
           AND routes.status IN ('stored', 'duplicate')
         ORDER BY decisions.processed_at
        """
    ).fetchall()
    evidence_by_artwork: dict[str, dict] = {}
    for row in rows:
        try:
            evidence = json.loads(row["evidence_json"])
            reason_codes = json.loads(row["reason_codes_json"])
        except (json.JSONDecodeError, TypeError):
            continue
        visual = evidence.get("visual_scores") or {}
        candidate = _candidate_from_evidence(conn, evidence.get("candidate_id"))
        class_scores = sorted(
            visual.get("class_scores") or [],
            key=lambda item: float(item.get("score", 0.0)),
            reverse=True,
        )
        evidence_by_artwork[row["artwork_id"]] = {
            "decision": row["decision"],
            "seedPromoted": "accept.siglip_corpus_seed" in reason_codes,
            "predictedClass": row["predicted_class"],
            "route": row["route"],
            "finalScore": row["final_score"],
            "confidence": row["confidence"],
            "artUtilityScore": visual.get("art_utility_score"),
            "noiseScore": visual.get("noise_score"),
            "confidenceMargin": visual.get("confidence_margin"),
            "classScores": class_scores,
            "reasonCodes": reason_codes,
            "modelId": row["model_id"],
            "modelRevision": row["model_revision"],
            "promptVersion": row["prompt_version"],
            "configVersion": row["config_version"],
            "processedAt": row["processed_at"],
            "postText": candidate.get("post_text", ""),
            "altText": candidate.get("alt_text", ""),
        }
    return evidence_by_artwork


def _candidate_from_evidence(conn: sqlite3.Connection, candidate_id: str | None) -> dict:
    if not candidate_id:
        return {}
    row = conn.execute(
        """
        SELECT candidate_json
          FROM artwork_filter_decisions
         WHERE candidate_id = ?
         ORDER BY processed_at DESC
         LIMIT 1
        """,
        (candidate_id,),
    ).fetchone()
    if row is None:
        return {}
    try:
        return json.loads(row["candidate_json"])
    except (json.JSONDecodeError, TypeError):
        return {}


def _corpus_fingerprint(conn: sqlite3.Connection) -> str:
    rows = conn.execute(
        """
        SELECT
            artworks.artwork_id,
            artworks.file_hash,
            embeddings.model_name_clip,
            embeddings.model_version_clip,
            embeddings.model_name_dino,
            embeddings.model_version_dino
          FROM artworks
          JOIN embeddings ON embeddings.artwork_id = artworks.artwork_id
         WHERE artworks.validated = 1
         ORDER BY artworks.artwork_id
        """
    ).fetchall()
    payload = [dict(row) for row in rows]
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _model_info(
    conn: sqlite3.Connection,
    config: AppConfig,
    evidence: dict[str, dict],
) -> list[dict]:
    shape = conn.execute(
        """
        SELECT clip_dim, dino_pooled_dim, dino_patch_grid_size, dino_patch_dim
          FROM embeddings
         WHERE clip_vector IS NOT NULL
           AND dino_pooled IS NOT NULL
           AND dino_patches IS NOT NULL
         LIMIT 1
        """
    ).fetchone()
    siglip = next(iter(evidence.values()), {})
    return [
        {
            "stage": "Corpus gate",
            "signal": "SigLIP 2 zero-shot",
            "mode": "artwork_filter",
            "modelId": siglip.get("modelId", "google/siglip2-base-patch16-224"),
            "revision": siglip.get("modelRevision"),
            "representation": "768D image/text embedding and prompt-bank class scores",
            "decision": "artwork class plus accept/review/reject routing",
        },
        {
            "stage": "Retrieval orchestrator",
            "signal": "Two-stage DINO ensemble",
            "mode": RetrievalMode.ENSEMBLE.value,
            "modelId": config.models.dino_model_name,
            "revision": config.models.dino_model_version,
            "representation": (
                f"pooled full-corpus top-{config.retrieval.shortlist_size} shortlist, "
                f"then patch top-{config.retrieval.patch_match_top_n} late interaction"
            ),
            "decision": (
                "final order comes from patch reranking; CLIP is reported separately "
                "and does not alter rank"
            ),
        },
        {
            "stage": "Retrieval",
            "signal": "CLIP subject",
            "mode": RetrievalMode.CLIP_SUBJECT.value,
            "modelId": config.models.clip_model_name,
            "revision": config.models.clip_model_version,
            "representation": (
                f"{shape['clip_dim']}D global vector" if shape is not None else "global vector"
            ),
            "decision": "top-k semantic/subject MATCH guesses",
        },
        {
            "stage": "Retrieval",
            "signal": "DINO style / global",
            "mode": RetrievalMode.DINO_POOLED.value,
            "modelId": config.models.dino_model_name,
            "revision": config.models.dino_model_version,
            "representation": (
                f"{shape['dino_pooled_dim']}D pooled global vector"
                if shape is not None
                else "pooled global vector"
            ),
            "decision": "top-k style/global-visual MATCH guesses",
        },
        {
            "stage": "Retrieval",
            "signal": "DINO local detail",
            "mode": RetrievalMode.DINO_PATCH_MAXSIM.value,
            "modelId": config.models.dino_model_name,
            "revision": config.models.dino_model_version,
            "representation": (
                f"{shape['dino_patch_grid_size']}x{shape['dino_patch_grid_size']} "
                f"tokens x {shape['dino_patch_dim']}D, MaxSim late interaction"
                if shape is not None
                else "patch-token MaxSim late interaction"
            ),
            "decision": "top-k local-item/detail MATCH guesses",
        },
    ]


def _filter_stats(evidence: dict[str, dict]) -> dict:
    class_counts: dict[str, int] = {}
    decision_counts: dict[str, int] = {}
    for item in evidence.values():
        predicted_class = item.get("predictedClass") or "unknown"
        decision = item.get("decision") or "unknown"
        class_counts[predicted_class] = class_counts.get(predicted_class, 0) + 1
        decision_counts[decision] = decision_counts.get(decision, 0) + 1
    return {
        "artworkCount": len(evidence),
        "seedPromoted": sum(bool(item.get("seedPromoted")) for item in evidence.values()),
        "decisionCounts": decision_counts,
        "classCounts": dict(
            sorted(class_counts.items(), key=lambda item: (-item[1], item[0]))
        ),
        "meanFinalScore": _mean_available(evidence.values(), "finalScore"),
        "meanConfidence": _mean_available(evidence.values(), "confidence"),
        "meanArtUtility": _mean_available(evidence.values(), "artUtilityScore"),
        "meanMargin": _mean_available(evidence.values(), "confidenceMargin"),
    }


def _funnel_stats(evaluation_payloads: Sequence[dict]) -> dict:
    top_k_agreements = []
    pooled_movements = []
    clip_movements = []
    shortlist_sizes = []
    candidate_counts = []
    exact_order_matches = 0
    compared_queries = 0
    for entry in evaluation_payloads:
        modes = {mode["mode"]["value"]: mode for mode in entry.get("modes", [])}
        ensemble = modes.get(RetrievalMode.ENSEMBLE.value)
        patch = modes.get(RetrievalMode.DINO_PATCH_MAXSIM.value)
        if ensemble is None or patch is None:
            continue
        compared_queries += 1
        ensemble_ids = [item["artworkId"] for item in ensemble["results"]]
        patch_ids = [item["artworkId"] for item in patch["results"]]
        if patch_ids:
            top_k_agreements.append(len(set(ensemble_ids) & set(patch_ids)) / len(patch_ids))
        exact_order_matches += ensemble_ids == patch_ids
        first_retrieval = (
            (ensemble["results"][0].get("retrieval") or {})
            if ensemble["results"]
            else {}
        )
        if first_retrieval.get("shortlistSize") is not None:
            shortlist_sizes.append(int(first_retrieval["shortlistSize"]))
        if first_retrieval.get("candidateCount") is not None:
            candidate_counts.append(int(first_retrieval["candidateCount"]))
        for final_rank, item in enumerate(ensemble["results"], start=1):
            retrieval = item.get("retrieval") or {}
            components = retrieval.get("components") or {}
            pooled_rank = (components.get("pooled") or {}).get("rank")
            clip_rank = (components.get("clip") or {}).get("rank")
            if pooled_rank is not None:
                pooled_movements.append(abs(int(pooled_rank) - final_rank))
            if clip_rank is not None:
                clip_movements.append(abs(int(clip_rank) - final_rank))
    mean_shortlist = _mean_numbers(shortlist_sizes)
    mean_candidates = _mean_numbers(candidate_counts)
    return {
        "queryCount": compared_queries,
        "meanPatchTopKAgreement": _mean_numbers(top_k_agreements),
        "exactPatchOrderMatches": exact_order_matches,
        "meanPooledToFinalMovement": _mean_numbers(pooled_movements),
        "meanClipToFinalMovement": _mean_numbers(clip_movements),
        "meanShortlistSize": mean_shortlist,
        "meanCandidateCount": mean_candidates,
        "meanShortlistFraction": (
            mean_shortlist / mean_candidates
            if mean_shortlist is not None and mean_candidates
            else None
        ),
    }


def _mean_available(items: Iterable[dict], key: str) -> float | None:
    values = [float(item[key]) for item in items if item.get(key) is not None]
    return sum(values) / len(values) if values else None


def _mean_numbers(values: Sequence[float | int]) -> float | None:
    return sum(values) / len(values) if values else None


def _render_search_html(
    *,
    query_artwork_id: str,
    mode_payloads: list[dict],
    filters: SearchFilters,
) -> str:
    data_json = _safe_json(
        {
            "queryArtworkId": query_artwork_id,
            "modes": mode_payloads,
            "filters": _filters_payload(filters),
        }
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>ArtSearch Demo - {escape(query_artwork_id)}</title>
  <style>
    * {{
      box-sizing: border-box;
    }}
    body {{
      font-family: system-ui, sans-serif;
      margin: 24px;
      color: #1f2933;
      background: #f7f7f5;
    }}
    h1, h2, h3, p {{
      margin: 0;
    }}
    header {{
      display: grid;
      gap: 12px;
      margin-bottom: 22px;
    }}
    .modebar {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }}
    .modebar button {{
      border: 1px solid #b8beb6;
      border-radius: 8px;
      padding: 7px 10px;
      background: #ffffff;
      cursor: pointer;
      font: inherit;
    }}
    .modebar button[aria-pressed="true"] {{
      border-color: #2458a6;
      color: #123f7c;
      background: #edf4ff;
    }}
    .query {{
      margin-bottom: 28px;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
      gap: 16px;
    }}
    figure {{
      margin: 0;
      padding: 10px;
      background: #ffffff;
      border: 1px solid #d8d8d2;
      border-radius: 8px;
    }}
    img {{
      display: block;
      width: 100%;
      aspect-ratio: 1;
      object-fit: contain;
      background: #808080;
      border-radius: 4px;
    }}
    figcaption {{
      margin-top: 8px;
      font-size: 13px;
      line-height: 1.35;
      overflow-wrap: anywhere;
    }}
    .score, .subtle {{
      color: #52606d;
    }}
  </style>
</head>
<body>
  <header>
    <h1>ArtSearch Search Demo</h1>
    <p class="subtle">
      Compare retrieval modes for one query image. MaxSim is experimental local-detail search.
    </p>
    <div id="modebar" class="modebar"></div>
  </header>
  <section class="query">
    <h2>Query</h2>
    <div id="query" class="grid"></div>
  </section>
  <section>
    <h2 id="resultsTitle">Top Results</h2>
    <div id="results" class="grid"></div>
  </section>
  <script id="searchData" type="application/json">{data_json}</script>
  <script>
    const payload = JSON.parse(document.getElementById("searchData").textContent);
    const modebar = document.getElementById("modebar");
    const query = document.getElementById("query");
    const results = document.getElementById("results");
    const resultsTitle = document.getElementById("resultsTitle");

    function card(item, rank) {{
      const score = item.score.toFixed(4);
      return `
        <figure>
          <img src="${{item.imageSrc}}" alt="${{item.artworkId}}">
          <figcaption>
            <strong>${{rank ? `${{rank}}. ` : ""}}${{item.artistName}}</strong><br>
            ${{item.artworkId}}<br>
            <span class="score">score ${{score}}</span><br>
            <span class="subtle">${{item.reviewStatus}} · SFW ${{item.isSfw}}</span>
          </figcaption>
        </figure>
      `;
    }}

    function selectMode(index) {{
      const entry = payload.modes[index];
      modebar.querySelectorAll("button").forEach((button, buttonIndex) => {{
        button.setAttribute("aria-pressed", String(buttonIndex === index));
      }});
      query.innerHTML = card(entry.query, null);
      resultsTitle.textContent = `Top Results · ${{entry.mode.label}}`;
      results.innerHTML = entry.results.length
        ? entry.results.map((item, resultIndex) => card(item, resultIndex + 1)).join("")
        : "<p>No results matched the current filters.</p>";
    }}

    modebar.innerHTML = payload.modes.map((entry, index) => `
      <button type="button" data-index="${{index}}" aria-pressed="false">
        ${{entry.mode.label}}
      </button>
    `).join("");
    modebar.querySelectorAll("button").forEach((button) => {{
      button.addEventListener("click", () => selectMode(Number(button.dataset.index)));
    }});

    if (payload.modes.length) {{
      selectMode(0);
    }} else {{
      results.innerHTML = "<p>No retrieval modes were rendered.</p>";
    }}
  </script>
</body>
</html>
"""


def _safe_json(payload: dict) -> str:
    return json.dumps(payload).replace("</", "<\\/")


def _bool_or_none(value: object) -> bool | None:
    if value is None:
        return None
    return bool(value)
