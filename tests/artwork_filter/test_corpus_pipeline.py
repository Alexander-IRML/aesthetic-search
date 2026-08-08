import asyncio
from datetime import datetime, timezone
import sqlite3

import imagehash
from PIL import Image

from artsearch.artwork_filter.config import load_artwork_filter_config
from artsearch.artwork_filter.corpus import CorpusRouter, RoutedImage
from artsearch.artwork_filter.corpus_store import ArtworkFilterCorpusStore
from artsearch.artwork_filter.enums import ContentClass, FilterDecision
from artsearch.artwork_filter.hashing import sha256_bytes
from artsearch.artwork_filter.schemas import FilterResult, ImageCandidate, LoadedImage
from artsearch.ingest.config import (
    AppConfig,
    DuplicateConfig,
    EmbeddingConfig,
    ImageConfig,
    ModelConfig,
    RetrievalConfig,
)
from artsearch.ingest.db import SCHEMA_PATH, connect


class FakeImageLoader:
    async def load(self, candidate):
        image = Image.new("RGB", (640, 480), (20, 40, 60))
        return LoadedImage(
            candidate_id=candidate.candidate_id,
            rgb_image=image,
            width=image.width,
            height=image.height,
            format="JPEG",
            mime_type="image/jpeg",
            byte_size=100,
            sha256="a" * 64,
            perceptual_hash=str(imagehash.phash(image)),
            source_url=candidate.fullsize_url,
        )


def test_router_materializes_accept_and_review_but_not_reject(tmp_path):
    config = load_artwork_filter_config()
    config.storage.review_image_dir = tmp_path / "review"
    config.storage.download_review_images = True
    candidates = [_candidate(f"candidate-{index}") for index in range(3)]
    results = [
        _result("candidate-0", FilterDecision.ACCEPT, "main_art"),
        _result("candidate-1", FilterDecision.REVIEW, "review"),
        _result("candidate-2", FilterDecision.REJECT, "rejected"),
    ]
    router = CorpusRouter(
        config,
        raw_dir=tmp_path / "raw",
        image_loader=FakeImageLoader(),
    )

    routed = asyncio.run(router.route_many(candidates, results))

    assert [(item.candidate_id, item.target) for item in routed] == [
        ("candidate-0", "corpus"),
        ("candidate-1", "review"),
    ]
    assert all(item.local_path and item.local_path.exists() for item in routed)
    assert not any(item.candidate_id == "candidate-2" for item in routed)


def test_corpus_store_persists_evidence_and_idempotent_accepted_artwork(tmp_path):
    app_config = _app_config(tmp_path)
    legacy_schema = SCHEMA_PATH.read_text(encoding="utf-8").replace(
        "is_sfw          INTEGER,",
        "is_sfw          INTEGER DEFAULT 1,",
    )
    with sqlite3.connect(app_config.database_path) as legacy_conn:
        legacy_conn.executescript(legacy_schema)
    filter_config = load_artwork_filter_config()
    candidate = _candidate("candidate-accepted")
    result = _result(candidate.candidate_id, FilterDecision.ACCEPT, "main_art")
    result.config_hash = filter_config.config_hash
    raw_path = app_config.raw_dir / "source" / "candidate-accepted.jpg"
    raw_path.parent.mkdir(parents=True)
    image = Image.new("RGB", (640, 480), (20, 40, 60))
    image.save(raw_path)
    payload = raw_path.read_bytes()
    routed = RoutedImage(
        candidate_id=candidate.candidate_id,
        target="corpus",
        status="stored",
        local_path=raw_path,
        image_sha256=sha256_bytes(payload),
        perceptual_hash=str(imagehash.phash(image)),
        width=640,
        height=480,
    )
    store = ArtworkFilterCorpusStore(app_config, filter_config)

    first = store.persist_batch([candidate], [result], [routed])
    second = store.persist_batch([candidate], [result], [routed])

    with connect(app_config.database_path) as conn:
        artwork = conn.execute("SELECT * FROM artworks").fetchall()
        decisions = conn.execute("SELECT * FROM artwork_filter_decisions").fetchall()
        routes = conn.execute("SELECT * FROM artwork_filter_routes").fetchall()
    assert first["imported"] == 1
    assert second["unchanged"] == 1
    assert len(artwork) == 1
    assert artwork[0]["source_platform"] == "bluesky"
    assert artwork[0]["source_id"] == candidate.candidate_id
    assert artwork[0]["validated"] == 1
    assert artwork[0]["is_sfw"] is None
    assert len(decisions) == 1
    assert len(routes) == 1
    assert routes[0]["artwork_id"] == artwork[0]["artwork_id"]


def test_corpus_store_invalidates_embedding_when_existing_artwork_changes(tmp_path):
    app_config = _app_config(tmp_path)
    filter_config = load_artwork_filter_config()
    candidate = _candidate("candidate-changing")
    result = _result(candidate.candidate_id, FilterDecision.ACCEPT, "main_art")
    result.config_hash = filter_config.config_hash
    raw_path = app_config.raw_dir / "source" / "candidate-changing.jpg"
    raw_path.parent.mkdir(parents=True)
    first_image = Image.new("RGB", (640, 480), (20, 40, 60))
    first_image.save(raw_path)
    first_payload = raw_path.read_bytes()
    first_route = RoutedImage(
        candidate_id=candidate.candidate_id,
        target="corpus",
        status="stored",
        local_path=raw_path,
        image_sha256=sha256_bytes(first_payload),
        perceptual_hash=str(imagehash.phash(first_image)),
        width=640,
        height=480,
    )
    store = ArtworkFilterCorpusStore(app_config, filter_config)
    store.persist_batch([candidate], [result], [first_route])

    with connect(app_config.database_path) as conn:
        artwork_id = conn.execute("SELECT artwork_id FROM artworks").fetchone()["artwork_id"]
        conn.execute(
            "INSERT INTO embeddings (artwork_id, model_name_clip, model_version_clip, "
            "model_name_dino, model_version_dino) VALUES (?, 'clip', 'v1', 'dino', 'v1')",
            (artwork_id,),
        )
        conn.commit()

    second_image = Image.new("RGB", (640, 480), (90, 20, 10))
    second_image.save(raw_path)
    second_payload = raw_path.read_bytes()
    second_route = RoutedImage(
        candidate_id=candidate.candidate_id,
        target="corpus",
        status="stored",
        local_path=raw_path,
        image_sha256=sha256_bytes(second_payload),
        perceptual_hash=str(imagehash.phash(second_image)),
        width=640,
        height=480,
    )

    summary = store.persist_batch([candidate], [result], [second_route])

    with connect(app_config.database_path) as conn:
        artwork = conn.execute("SELECT file_hash, validated FROM artworks").fetchone()
        embedding_count = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
    assert summary["imported"] == 1
    assert artwork["file_hash"] == second_route.image_sha256
    assert artwork["validated"] == 1
    assert embedding_count == 0


def _candidate(candidate_id: str) -> ImageCandidate:
    return ImageCandidate(
        candidate_id=candidate_id,
        author_did="did:plc:test-artist",
        author_handle="artist.example",
        post_uri=f"at://did:plc:test-artist/app.bsky.feed.post/{candidate_id}",
        image_index=0,
        fullsize_url="https://cdn.example/fullsize.jpg",
        source="bluesky",
    )


def _result(
    candidate_id: str,
    decision: FilterDecision,
    route: str,
) -> FilterResult:
    return FilterResult(
        candidate_id=candidate_id,
        decision=decision,
        predicted_class=(
            ContentClass.FINISHED_ILLUSTRATION
            if decision == FilterDecision.ACCEPT
            else ContentClass.UNKNOWN
        ),
        accepted_for_main_corpus=decision == FilterDecision.ACCEPT,
        route=route,
        final_score=0.95 if decision == FilterDecision.ACCEPT else 0.2,
        confidence=0.95,
        reason_codes=[f"{decision.value}.test"],
        image_sha256="b" * 64,
        width=640,
        height=480,
        visual_scores=None,
        text_scores=None,
        rule_result=None,
        model_version="fake-model",
        config_version="test",
        prompt_version="test",
        classifier_version=None,
        processed_at=datetime.now(timezone.utc),
        duration_ms=1.0,
        source_uri=f"at://post/{candidate_id}",
        author_did="did:plc:test-artist",
        image_index=0,
        config_hash="config-hash",
        software_version="test",
    )


def _app_config(tmp_path) -> AppConfig:
    return AppConfig(
        root_dir=tmp_path,
        raw_dir=tmp_path / "raw",
        processed_dir=tmp_path / "processed",
        database_path=tmp_path / "artsearch.db",
        images=ImageConfig(
            canonical_size=448,
            crop_threshold=2.5,
            output_format="jpeg",
            jpeg_quality=95,
            padding_fill_strategy="neutral_gray",
            neutral_gray_value=128,
        ),
        duplicates=DuplicateConfig(phash_distance_threshold=6),
        models=ModelConfig(
            clip_model_name="clip",
            clip_model_version="v1",
            dino_model_name="dino",
            dino_model_version="v1",
        ),
        embeddings=EmbeddingConfig(batch_size=2, device="cpu"),
        retrieval=RetrievalConfig(
            default_top_k=5,
            demo_output_path=tmp_path / "demo.html",
            gallery_output_path=tmp_path / "gallery.html",
        ),
    )
