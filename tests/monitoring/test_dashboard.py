from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from threading import Event, Thread
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import numpy as np
from PIL import Image
import pytest

from artsearch.artwork_filter.enums import ContentClass, FilterDecision, ModelMode
from artsearch.artwork_filter.labels import latest_labels
from artsearch.artwork_filter.schemas import (
    ClassScore,
    FilterResult,
    ImageCandidate,
    VisualScores,
)
from artsearch.embed.storage import ImageEmbeddings, upsert_embedding
from artsearch.ingest.config import load_config
from artsearch.ingest.db import connect, init_db
from artsearch.monitoring.config import load_monitoring_config
from artsearch.monitoring.corpus_control import (
    CorpusControlConflictError,
    CorpusControlService,
)
from artsearch.monitoring.cli import _default_production_config, _load_private_environment
from artsearch.monitoring.dashboard import render_monitoring_html
from artsearch.monitoring.launcher import _windows_batch_content, install_desktop_launcher
from artsearch.monitoring.review import HumanReviewService
from artsearch.monitoring.server import create_monitoring_server
from artsearch.monitoring.snapshot import MonitoringInputs, build_monitoring_snapshot
from artsearch.retrieval.evaluation import RetrievalJudgment
from artsearch.retrieval.evaluation import load_retrieval_judgments
from artsearch.retrieval.search import RetrievalMode


PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def monitoring_inputs(tmp_path: Path) -> MonitoringInputs:
    root = tmp_path / "project"
    config_dir = root / "config"
    configs_dir = root / "configs"
    data_dir = root / "data"
    config_dir.mkdir(parents=True)
    configs_dir.mkdir()
    data_dir.mkdir()

    app_config = config_dir / "config.yaml"
    app_config.write_text(
        """
paths:
  database: data/artsearch.db
models:
  clip_model_name: test/clip
  clip_model_version: clip-rev
  csd_model_name: test/csd
  csd_model_version: csd-rev
  csd_model_filename: model.pt
  csd_model_sha256: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
  csd_processor_name: test/clip-processor
  csd_processor_version: processor-rev
embeddings:
  batch_size: 2
  device: cpu
""".strip()
        + "\n",
        encoding="utf-8",
    )
    production_config = configs_dir / "production.toml"
    production_config.write_text(
        """
version = "test"

[qdrant]
enabled = false
collection_name = "test_collection"
alias_name = "test_active"
clip_dimension = 2
csd_dimension = 3
require_sfw = true
require_bluesky_accept = true
""".strip()
        + "\n",
        encoding="utf-8",
    )
    monitoring_config = configs_dir / "monitoring.toml"
    monitoring_config.write_text(
        """
version = "test"
refresh_seconds = 5
snapshot_cache_seconds = 0
qdrant_probe_timeout_seconds = 0.1
drift_recent_days = 7
drift_baseline_days = 30

[targets]
embedding_coverage_warning = 0.90
embedding_coverage_critical = 0.50
filter_error_rate_warning = 0.20
filter_error_rate_critical = 0.50
filter_accept_precision = 0.90
minimum_filter_labels = 2
minimum_retrieval_judgments = 2
minimum_retrieval_queries = 1
retrieval_precision_at_5 = 0.50
retrieval_ndcg = 0.50
ann_mean_recall_at_k = 0.90
ann_minimum_recall_at_k = 0.80
filter_p95_latency_ms = 5000
decision_freshness_days = 365
minimum_drift_cohort = 2
class_distribution_js_warning = 0.10
class_distribution_js_critical = 0.20
maximum_top_artist_share = 0.90
""".strip()
        + "\n",
        encoding="utf-8",
    )

    app = load_config(app_config)
    processed_dir = root / "data/processed"
    processed_dir.mkdir(parents=True)
    Image.new("RGB", (320, 320), "#c55f4a").save(processed_dir / "art-0.jpg")
    Image.new("RGB", (320, 320), "#4c78a8").save(processed_dir / "art-1.jpg")
    with connect(app.database_path) as connection:
        init_db(connection)
        connection.execute(
            """
            INSERT INTO artists (
                artist_id, display_name, folder_name, source_platform, source_url
            ) VALUES (
                'artist-a', 'Artist A', 'artist-a', 'bluesky',
                'https://bsky.app/profile/artist-a.test'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO artists (
                artist_id, display_name, folder_name, source_platform
            ) VALUES ('artist-b', 'Artist B', 'artist-b', 'manual')
            """
        )
        for index in range(2):
            connection.execute(
                """
                INSERT INTO artworks (
                    artwork_id, artist_id, raw_path, processed_path,
                    source_platform, validated, review_status, is_sfw, demo_eligible
                ) VALUES (?, ?, ?, ?, 'manual', 1, 'confirmed_unique', 1, 1)
                """,
                (
                    f"art-{index}",
                    f"artist-{'a' if index == 0 else 'b'}",
                    f"data/raw/art-{index}.jpg",
                    f"data/processed/art-{index}.jpg",
                ),
            )
            upsert_embedding(
                connection,
                f"art-{index}",
                ImageEmbeddings(
                    clip_vector=np.array([1.0, float(index)], dtype=np.float32),
                    csd_style=np.array([1.0, 0.5, float(index)], dtype=np.float32),
                ),
                app.models,
            )
        _insert_review_decision(connection)
        connection.execute(
            """
            INSERT INTO runs (
                script_name, started_at, finished_at,
                images_processed, images_skipped, errors_count
            ) VALUES ('test-run', '2026-08-10T10:00:00+00:00',
                      '2026-08-10T10:01:00+00:00', 2, 0, 0)
            """
        )
        connection.commit()

    decisions = data_dir / "decisions.jsonl"
    rows = [
        _decision("base-1", "artist-a", "casual_photo", "reject", "2026-07-20", 800),
        _decision("base-2", "artist-b", "casual_photo", "reject", "2026-07-21", 900),
        _decision("recent-1", "artist-a", "finished_illustration", "review", "2026-08-09", 1200),
        _decision("recent-2", "artist-b", "finished_illustration", "review", "2026-08-10", 1400),
    ]
    decisions.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )

    judgments = data_dir / "judgments.jsonl"
    judgments.write_text(
        "".join(
            _judgment(candidate, rank=rank, relevant=relevant).model_dump_json() + "\n"
            for candidate, rank, relevant in (("art-0", 1, True), ("art-1", 2, False))
        ),
        encoding="utf-8",
    )
    filter_evaluation = data_dir / "filter-evaluation.json"
    filter_evaluation.write_text(
        json.dumps(
            {
                "dataset": {"matched": 2, "artist_groups": 2},
                "decision_metrics": {
                    "automatic_accept_precision": 1.0,
                    "automatic_accept_precision_95ci": [0.95, 1.0],
                    "automatic_accept_recall": 0.8,
                    "automatic_accept_coverage": 0.5,
                },
                "multiclass_metrics": {"macro_f1": 0.8, "top1_accuracy": 0.9},
            }
        ),
        encoding="utf-8",
    )
    ann_evaluation = data_dir / "ann-evaluation.json"
    ann_evaluation.write_text(
        json.dumps(
            {
                "metrics": [
                    {
                        "vector_name": "clip_subject",
                        "query_count": 2,
                        "top_k": 100,
                        "mean_recall": 1.0,
                        "minimum_recall": 1.0,
                        "approximate_latency_p50_ms": 2.0,
                        "approximate_latency_p95_ms": 3.0,
                        "exact_latency_p50_ms": 4.0,
                        "exact_latency_p95_ms": 5.0,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    smoke = data_dir / "siglip-smoke.json"
    smoke.write_text(
        json.dumps({"passed": True, "image_count": 2, "all_finite": True}),
        encoding="utf-8",
    )

    return MonitoringInputs(
        app_config_path=app_config,
        filter_config_path=PROJECT_ROOT / "configs/artwork_filter.default.toml",
        prompt_config_path=PROJECT_ROOT / "configs/artwork_filter.prompts.v1.toml",
        production_config_path=production_config,
        monitoring_config_path=monitoring_config,
        decisions_paths=(decisions,),
        retrieval_judgment_paths=(judgments,),
        filter_evaluation_path=filter_evaluation,
        ann_evaluation_path=ann_evaluation,
        probe_qdrant=False,
    )


def _decision(
    candidate_id: str,
    artist: str,
    predicted_class: str,
    decision: str,
    day: str,
    duration_ms: float,
) -> dict[str, object]:
    return {
        "candidate_id": candidate_id,
        "source_cid": f"cid-{candidate_id}",
        "author_did": f"did:plc:{artist}",
        "decision": decision,
        "predicted_class": predicted_class,
        "route": "review" if decision == "review" else "rejected",
        "duration_ms": duration_ms,
        "confidence": 0.8 if decision == "review" else 0.6,
        "final_score": 0.75 if decision == "review" else 0.2,
        "visual_scores": {"confidence_margin": 0.2},
        "processed_at": f"{day}T12:00:00+00:00",
        "model_version": "siglip-test",
        "model_revision": "revision",
    }


def _insert_review_decision(connection) -> None:
    candidate = ImageCandidate(
        candidate_id="gate-1",
        author_did="did:plc:artist-a",
        author_handle="artist-a.test",
        post_uri="at://did:plc:artist-a/app.bsky.feed.post/test",
        image_index=0,
        local_path=Path("data/processed/art-0.jpg"),
        post_text="Finished illustration",
        alt_text="A painted character portrait",
        source="local",
    )
    result = FilterResult(
        candidate_id=candidate.candidate_id,
        decision=FilterDecision.ACCEPT,
        predicted_class=ContentClass.FINISHED_ILLUSTRATION,
        accepted_for_main_corpus=True,
        route="main_art",
        final_score=0.92,
        confidence=0.88,
        reason_codes=["accept.high_confidence_finished_illustration"],
        image_sha256="a" * 64,
        width=320,
        height=320,
        visual_scores=VisualScores(
            backend="test",
            model_id="test/siglip",
            model_revision="siglip-rev",
            mode=ModelMode.ZERO_SHOT,
            class_scores=[
                ClassScore(
                    content_class=ContentClass.FINISHED_ILLUSTRATION,
                    score=0.88,
                ),
                ClassScore(content_class=ContentClass.CASUAL_PHOTO, score=0.12),
            ],
            art_utility_score=0.92,
            noise_score=0.08,
            confidence_margin=0.76,
            embedding_dimension=2,
        ),
        model_version="test/siglip",
        config_version="test",
        prompt_version="test-prompts",
        processed_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
        duration_ms=12.0,
        source_uri=candidate.post_uri,
        author_did=candidate.author_did,
        image_index=0,
        config_hash="filter-config-hash",
        software_version="test",
    )
    connection.execute(
        """
        INSERT INTO artwork_filter_decisions (
            decision_key, candidate_id, author_did, post_uri, image_index,
            image_sha256, decision, predicted_class, accepted_for_main_corpus,
            route, final_score, confidence, reason_codes_json, candidate_json,
            evidence_json, model_id, model_revision, config_version, config_hash,
            prompt_version, software_version, processed_at, duration_ms
        ) VALUES (
            'decision-gate-1', ?, ?, ?, 0, ?, 'accept', 'finished_illustration', 1,
            'main_art', 0.92, 0.88, ?, ?, ?, 'test/siglip', 'siglip-rev', 'test',
            'filter-config-hash', 'test-prompts', 'test', ?, 12.0
        )
        """,
        (
            candidate.candidate_id,
            candidate.author_did,
            candidate.post_uri,
            result.image_sha256,
            json.dumps(result.reason_codes),
            candidate.model_dump_json(),
            result.model_dump_json(),
            result.processed_at.isoformat(),
        ),
    )
    connection.execute(
        """
        INSERT INTO artwork_filter_routes (
            route_key, decision_key, candidate_id, target, status,
            local_path, image_sha256, width, height, artwork_id
        ) VALUES (
            'route-gate-1', 'decision-gate-1', ?, 'corpus', 'stored',
            'data/processed/art-0.jpg', ?, 320, 320, 'art-0'
        )
        """,
        (candidate.candidate_id, result.image_sha256),
    )


def _judgment(candidate: str, *, rank: int, relevant: bool) -> RetrievalJudgment:
    return RetrievalJudgment(
        judgment_id=f"judgment-{candidate}",
        dashboard_id="dashboard-test",
        corpus_fingerprint="corpus-test",
        query_artwork_id="query",
        candidate_artwork_id=candidate,
        retrieval_mode=RetrievalMode.ENSEMBLE,
        task="ensemble",
        relevant=relevant,
        rank=rank,
        result_count=2,
        score=1.0 - rank / 10,
        model_id="ensemble-test",
        model_revision="revision",
        labeled_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
    )


def test_snapshot_aggregates_quality_drift_and_operational_health(
    monitoring_inputs: MonitoringInputs,
) -> None:
    snapshot = build_monitoring_snapshot(monitoring_inputs)

    assert snapshot["corpus"]["searchable"] == 2
    assert snapshot["corpus"]["serving_eligible"] == 2
    assert snapshot["embeddings"]["global_coverage"] == 1.0
    assert snapshot["filtering"]["latest_decisions"] == 4
    assert snapshot["filtering"]["drift"]["measured"] is True
    assert snapshot["filtering"]["drift"]["class_js_divergence"] == pytest.approx(1.0)
    assert snapshot["retrieval"]["measured"] is True
    assert snapshot["retrieval"]["per_mode"]["ensemble"]["mrr"] == 1.0
    assert snapshot["product"]["status"] == "disabled"
    assert snapshot["product"]["search"]["total_searches"] == 0
    assert snapshot["product"]["visitors"]["total_unique_ips"] == 0
    assert snapshot["product"]["recall_at_100"]["measured"] is True
    assert snapshot["product"]["recall_at_100"]["mean_recall"] == 1.0
    assert "pipeline_success" in snapshot["operations"]
    assert snapshot["monitoring"]["evidence_coverage"] == {
        "filter_quality": True,
        "retrieval_quality": True,
        "ann_quality": True,
    }
    assert "drift.class_distribution.critical" in {item["id"] for item in snapshot["alerts"]}
    serialized = json.dumps(snapshot)
    assert "did:plc:" not in serialized
    assert "post text" not in serialized
    assert "visitor_ip_hash" not in serialized


def test_dashboard_server_exposes_ui_snapshot_and_health(
    monitoring_inputs: MonitoringInputs,
) -> None:
    try:
        server = create_monitoring_server(monitoring_inputs, port=0)
    except PermissionError:
        pytest.skip("execution environment blocks loopback sockets")
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with urlopen(f"{base}/", timeout=2) as response:
            html = response.read().decode("utf-8")
            assert response.headers["X-Frame-Options"] == "DENY"
        with urlopen(f"{base}/api/snapshot", timeout=2) as response:
            snapshot = json.load(response)
        with urlopen(f"{base}/api/health", timeout=2) as response:
            health = json.load(response)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert "ArtSearch Model Operations" in html
    assert "Data &amp; drift" in html
    assert snapshot["corpus"]["artworks"] == 2
    assert snapshot["discovery"]["status"] == "disabled"
    assert health["status"] == "critical"


def test_dashboard_creator_review_api_is_token_protected(
    monitoring_inputs: MonitoringInputs,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = {
        "item": {
            "did": "did:plc:discovery-test",
            "handle": "discovery.test",
            "state": "needs_review",
            "creator_score": 0.71,
            "score_components": {"art_relevance": 0.8},
            "observations": [{"source_type": "starter_pack"}],
            "media": [],
        },
        "offset": 0,
        "total": 1,
    }
    monkeypatch.setattr(
        HumanReviewService,
        "discovery_item",
        lambda self, **kwargs: {**item, "requested": kwargs},
    )
    monkeypatch.setattr(
        HumanReviewService,
        "submit_discovery_review",
        lambda self, payload: {"did": payload["did"], "state": "qualified"},
    )
    try:
        server = create_monitoring_server(monitoring_inputs, port=0)
    except PermissionError:
        pytest.skip("execution environment blocks loopback sockets")
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    token = server.review_token
    try:
        queue = _review_get(
            base,
            "/api/review/discovery",
            token,
            state="needs_review",
            offset="0",
        )
        saved = _review_post(
            base,
            "/api/review/discovery-decision",
            token,
            {"did": "did:plc:discovery-test", "decision": "approve"},
        )
        with pytest.raises(HTTPError) as unauthorized:
            _review_get(base, "/api/review/discovery", "wrong-token")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert queue["item"]["handle"] == "discovery.test"
    assert queue["requested"] == {"state": "needs_review", "offset": 0}
    assert saved == {"did": "did:plc:discovery-test", "state": "qualified"}
    assert unauthorized.value.code == 403


def test_review_service_records_auditable_human_evidence(
    monitoring_inputs: MonitoringInputs,
) -> None:
    monitoring = load_monitoring_config(monitoring_inputs.monitoring_config_path)
    service = HumanReviewService(
        app_config_path=monitoring_inputs.app_config_path,
        filter_config_path=monitoring_inputs.filter_config_path,
        review_config=monitoring.review,
        filter_label_path=monitoring_inputs.filter_label_path,
        retrieval_judgment_path=monitoring_inputs.review_judgment_path,
        retrieval_judgment_sources=monitoring_inputs.retrieval_judgment_paths,
        annotator="tester",
    )

    queue = service.filter_item()
    assert queue["total"] == 1
    assert queue["item"]["candidate_id"] == "gate-1"
    assert service.filter_media("gate-1").data.startswith(b"\xff\xd8")

    saved = service.submit_filter_label(
        {
            "candidate_id": "gate-1",
            "content_class": "finished_illustration",
            "is_original_artist_work": "yes",
            "include_in_main_corpus": "yes",
            "annotator_note": "Correct gate decision.",
        }
    )
    assert saved["appended"] is True
    assert service.filter_item()["total"] == 0

    retrieval = service.new_retrieval_session("clip_subject")
    assert len(retrieval["results"]) == 1
    rated = service.submit_retrieval_judgment(
        {
            "session_id": retrieval["session_id"],
            "candidate_artwork_id": retrieval["results"][0]["artwork_id"],
            "relevant": True,
        }
    )
    assert rated["judgment"]["model_id"] == "test/clip"

    app = load_config(monitoring_inputs.app_config_path)
    labels = latest_labels(app.root_dir / monitoring_inputs.filter_label_path)
    judgments = load_retrieval_judgments(app.root_dir / monitoring_inputs.review_judgment_path)
    snapshot = build_monitoring_snapshot(monitoring_inputs)
    assert labels["gate-1"].annotator == "tester"
    assert len(judgments) == 1
    assert snapshot["filter_evaluation"]["dataset"]["matched"] == 1
    assert snapshot["retrieval"]["dataset"]["latest_judgments"] == 3


def test_dashboard_review_api_persists_filter_and_retrieval_feedback(
    monitoring_inputs: MonitoringInputs,
) -> None:
    try:
        server = create_monitoring_server(monitoring_inputs, port=0)
    except PermissionError:
        pytest.skip("execution environment blocks loopback sockets")
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    token = server.review_token
    try:
        summary = _review_get(base, "/api/review/summary", token)
        queue = _review_get(base, "/api/review/filter", token)
        graph = _review_get(base, "/api/control/graph", token)
        job = _review_get(base, "/api/control/job", token)
        with urlopen(
            f"{base}/api/review/media/filter?{urlencode({'token': token, 'candidate_id': 'gate-1'})}",
            timeout=2,
        ) as response:
            media = response.read()

        assert summary["filter"]["pending"] == 1
        assert queue["item"]["candidate_id"] == "gate-1"
        assert queue["item"]["predicted_class"] == "finished_illustration"
        assert graph["counts"]["corpus_artists"] == 1
        assert graph["posts"][0][1].startswith("p:")
        assert job["active"] is False
        assert response.headers["Content-Type"] == "image/jpeg"
        assert media.startswith(b"\xff\xd8")

        with pytest.raises(HTTPError) as unauthorized:
            _review_post(
                base,
                "/api/review/filter-label",
                "wrong-token",
                {"candidate_id": "gate-1"},
            )
        assert unauthorized.value.code == 403

        with pytest.raises(HTTPError) as invalid_control:
            _review_post(
                base,
                "/api/control/refresh",
                token,
                {"max_new_images": 0},
            )
        assert invalid_control.value.code == 400

        saved = _review_post(
            base,
            "/api/review/filter-label",
            token,
            {
                "candidate_id": "gate-1",
                "content_class": "finished_illustration",
                "is_original_artist_work": "yes",
                "include_in_main_corpus": "yes",
                "annotator_note": "Looks correct.",
            },
        )
        assert saved["appended"] is True
        assert saved["summary"]["filter"]["pending"] == 0

        retrieval = _review_get(
            base,
            "/api/review/retrieval",
            token,
            mode="clip_subject",
        )
        assert retrieval["results"]
        candidate = retrieval["results"][0]
        rating = _review_post(
            base,
            "/api/review/retrieval-judgment",
            token,
            {
                "session_id": retrieval["session_id"],
                "candidate_artwork_id": candidate["artwork_id"],
                "relevant": True,
            },
        )
        assert rating["judgment"]["rank"] == 1
        assert rating["judgment"]["retrieval_mode"] == "clip_subject"

        with urlopen(f"{base}/api/snapshot?refresh=1", timeout=2) as response:
            refreshed = json.load(response)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    app = load_config(monitoring_inputs.app_config_path)
    labels = latest_labels(app.root_dir / monitoring_inputs.filter_label_path)
    judgments = load_retrieval_judgments(app.root_dir / monitoring_inputs.review_judgment_path)
    assert labels["gate-1"].annotator_note == "Looks correct."
    assert len(judgments) == 1
    assert refreshed["filter_evaluation"]["dataset"]["matched"] == 1
    assert refreshed["retrieval"]["dataset"]["latest_judgments"] == 3


def test_embedding_coverage_requires_clip_and_csd_on_the_same_rows(
    monitoring_inputs: MonitoringInputs,
) -> None:
    app = load_config(monitoring_inputs.app_config_path)
    with connect(app.database_path) as connection:
        connection.execute("UPDATE embeddings SET clip_vector = NULL WHERE artwork_id = 'art-0'")
        connection.execute("UPDATE embeddings SET csd_style = NULL WHERE artwork_id = 'art-1'")
        connection.commit()

    snapshot = build_monitoring_snapshot(monitoring_inputs)

    assert snapshot["embeddings"]["clip_current"] == 1
    assert snapshot["embeddings"]["csd_current"] == 1
    assert snapshot["embeddings"]["current_complete"] == 0
    assert snapshot["embeddings"]["global_coverage"] == 0.0


def test_dashboard_html_is_self_contained() -> None:
    html = render_monitoring_html()

    assert "Model quality" in html
    assert "Recent-vs-baseline drift" in html
    assert "Review &amp; feedback" in html
    assert "Live alpha feedback" in html
    assert "Search p95" in html
    assert "Unique demo IPs" in html
    assert "Pipeline success" in html
    assert "Artwork gate decisions" in html
    assert 'data-tab="discovery"' in html
    assert "Corpus discovery control plane" in html
    assert "Duplicate creator IDs" in html
    assert "25-account cap" in html
    assert "Pull missing posts" in html
    assert "Expand similar accounts" in html
    assert 'id="corpus-graph"' in html
    assert "/api/control/refresh" in html
    assert "/api/control/expand" in html
    assert "Creator discovery" in html
    assert "/api/review/discovery-decision" in html
    assert 'data-operator-action="run-intake"' in html
    assert 'new CustomEvent("artsearch:operator-action"' in html
    assert "reviewSummary?.conceal_media === false" in html
    assert 'src="http' not in html
    assert 'href="http' not in html


def test_corpus_control_builds_compact_graph_and_bounded_refresh_job(
    monitoring_inputs: MonitoringInputs,
) -> None:
    commands: list[list[str]] = []

    def runner(command, cwd, stdout_path, stderr_path):
        commands.append(list(command))
        assert cwd == load_config(monitoring_inputs.app_config_path).root_dir
        stdout_path.write_text('{"candidates":2,"accepted":1}\n', encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        return 0

    service = CorpusControlService(
        app_config_path=monitoring_inputs.app_config_path,
        filter_config_path=monitoring_inputs.filter_config_path,
        prompt_config_path=monitoring_inputs.prompt_config_path,
        production_config_path=monitoring_inputs.production_config_path,
        command_runner=runner,
    )

    graph = service.graph()
    assert graph["counts"]["corpus_artists"] == 1
    assert graph["counts"]["posts"] == 1
    assert graph["artists"][0]["key"].startswith("a:")
    assert graph["posts"][0][1].startswith("p:")
    assert "at://" not in json.dumps(graph)
    assert graph["controls"]["can_refresh"] is True
    assert graph["controls"]["can_expand"] is False

    started = service.start_refresh(
        {
            "max_new_images": 7,
            "max_pages_per_artist": 1,
            "max_images_per_artist": 3,
            "artist_limit": 1,
        }
    )
    assert started["job"]["kind"] == "refresh"
    assert service.wait_for_idle()
    status = service.status()
    assert status["job"]["state"] == "succeeded"
    assert status["job"]["summary"] == {"candidates": 2, "accepted": 1}
    assert commands
    command = commands[0]
    assert "--resume" in command
    assert command[command.index("--max-candidates") + 1] == "7"

    app = load_config(monitoring_inputs.app_config_path)
    with connect(app.database_path) as connection:
        state = connection.execute(
            "SELECT high_water_post_uri FROM bluesky_actor_intake_state"
        ).fetchone()
    assert state["high_water_post_uri"] == "at://did:plc:artist-a/app.bsky.feed.post/test"


def test_corpus_control_expansion_uses_trusted_rotating_seeds_and_request_cap(
    monitoring_inputs: MonitoringInputs,
) -> None:
    production_config = monitoring_inputs.production_config_path.with_name(
        "production-enabled.toml"
    )
    production_config.write_text(
        """
version = "test-control"
[postgres]
enabled = true
require_tls = false
""".strip()
        + "\n",
        encoding="utf-8",
    )
    commands: list[list[str]] = []

    def runner(command, cwd, stdout_path, stderr_path):
        commands.append(list(command))
        stdout_path.write_text('{"api_requests":9,"budget_reached":1}\n', encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        return 0

    service = CorpusControlService(
        app_config_path=monitoring_inputs.app_config_path,
        filter_config_path=monitoring_inputs.filter_config_path,
        prompt_config_path=monitoring_inputs.prompt_config_path,
        production_config_path=production_config,
        command_runner=runner,
    )

    started = service.start_expand({"depth": 2, "request_budget": 9})
    assert started["job"]["parameters"]["seed_artists"] == 1
    assert service.wait_for_idle()
    command = commands[0]
    assert "--trusted-seeds" in command
    assert "--manual-seeds-only" in command
    assert command[command.index("--max-depth") + 1] == "2"
    assert command[command.index("--request-budget") + 1] == "9"


def test_corpus_control_allows_only_one_active_background_job(
    monitoring_inputs: MonitoringInputs,
) -> None:
    entered = Event()
    release = Event()

    def runner(command, cwd, stdout_path, stderr_path):
        entered.set()
        release.wait(timeout=2)
        stdout_path.write_text('{"candidates":0}\n', encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        return 0

    service = CorpusControlService(
        app_config_path=monitoring_inputs.app_config_path,
        filter_config_path=monitoring_inputs.filter_config_path,
        prompt_config_path=monitoring_inputs.prompt_config_path,
        production_config_path=monitoring_inputs.production_config_path,
        command_runner=runner,
    )

    try:
        service.start_refresh({"artist_limit": 1})
        assert entered.wait(timeout=1)
        with pytest.raises(CorpusControlConflictError):
            service.start_refresh({"artist_limit": 1})
        assert len(list(service.jobs_root.iterdir())) == 1
    finally:
        release.set()
    assert service.wait_for_idle()


def test_freedesktop_launcher_runs_installed_project_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    executable = project / ".venv/bin/artsearch-model-dashboard"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("artsearch.monitoring.launcher._running_under_wsl", lambda: False)

    destination = install_desktop_launcher(project)
    content = destination.read_text(encoding="utf-8")

    assert destination == home / "Desktop/artsearch-model-dashboard.desktop"
    assert "artsearch-model-dashboard" in content
    assert destination.stat().st_mode & 0o100


def test_windows_launcher_uses_native_wsl_working_directory(tmp_path: Path) -> None:
    project = tmp_path / "project"

    content = _windows_batch_content(project)

    assert f'wsl.exe --cd "{project}" --exec ' in content
    assert f'"{project}/.venv/bin/artsearch-model-dashboard"' in content
    assert "&&" not in content
    assert "bash -lc" not in content
    assert "Starting ArtSearch Model Operations" in content
    assert "could not start" in content


def test_dashboard_loads_ignored_environment_without_overwriting_process_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / ".env"
    path.write_text(
        "# private\nDATABASE_URL='postgresql://private.example/db'\nQDRANT_URL=https://q.test\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DATABASE_URL", "postgresql://already-set.example/db")
    monkeypatch.delenv("QDRANT_URL", raising=False)

    _load_private_environment(path)

    assert os.environ["DATABASE_URL"] == "postgresql://already-set.example/db"
    assert os.environ["QDRANT_URL"] == "https://q.test"


def test_dashboard_prefers_private_cloud_config_when_present(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs/production.alpha-cloud.local.toml").write_text(
        "version = 'test'\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    assert _default_production_config() == "configs/production.alpha-cloud.local.toml"


def test_monitoring_config_rejects_inverted_drift_thresholds(tmp_path: Path) -> None:
    path = tmp_path / "monitoring.toml"
    path.write_text(
        """
version = "bad"
[targets]
class_distribution_js_warning = 0.3
class_distribution_js_critical = 0.2
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="warning class drift"):
        load_monitoring_config(path)


def _review_get(base: str, path: str, token: str, **params: str) -> dict:
    query = urlencode({"token": token, **params})
    with urlopen(f"{base}{path}?{query}", timeout=3) as response:
        return json.load(response)


def _review_post(base: str, path: str, token: str, payload: dict) -> dict:
    request = Request(
        f"{base}{path}",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-ArtSearch-Review-Token": token,
        },
    )
    with urlopen(request, timeout=3) as response:
        return json.load(response)
