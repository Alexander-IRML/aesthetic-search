import asyncio
from datetime import datetime, timedelta, timezone
import json

import imagehash
from PIL import Image

from artsearch.artwork_filter.config import load_artwork_filter_config
from artsearch.artwork_filter.enums import (
    ContentClass,
    FilterDecision,
    ModelMode,
)
from artsearch.artwork_filter.schemas import (
    ClassScore,
    FilterResult,
    ImageCandidate,
    LoadedImage,
    VisualScores,
)
from artsearch.bluesky_corpus import (
    CORPUS_SEED_REASON,
    CorpusSelection,
    SelectedCorpusItem,
    archive_active_corpus,
    promote_siglip_result,
    seed_siglip_corpus,
    select_siglip_corpus,
)
from artsearch.ingest.config import (
    AppConfig,
    DuplicateConfig,
    EmbeddingConfig,
    ImageConfig,
    ModelConfig,
    RetrievalConfig,
)
from artsearch.ingest.db import connect


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


def test_selection_uses_current_source_and_excludes_provenance(tmp_path):
    config = load_artwork_filter_config()
    now = datetime.now(timezone.utc)
    selected_candidate = _candidate("selected", post_cid="cid-current")
    quote_candidate = _candidate("quote", post_cid="cid-quote", is_quote_post=True)
    stale_candidate = _candidate("stale", post_cid="cid-new")
    noise_candidate = _candidate("noise", post_cid="cid-noise")
    candidates = [selected_candidate, quote_candidate, stale_candidate, noise_candidate]
    decisions = [
        _result(
            selected_candidate,
            ContentClass.FINISHED_ILLUSTRATION,
            source_cid="cid-old",
            processed_at=now + timedelta(minutes=2),
            config_hash=config.config_hash,
        ),
        _result(
            selected_candidate,
            ContentClass.FINISHED_ILLUSTRATION,
            source_cid="cid-current",
            processed_at=now,
            config_hash=config.config_hash,
        ),
        _result(
            quote_candidate,
            ContentClass.COMIC,
            source_cid="cid-quote",
            processed_at=now,
            config_hash=config.config_hash,
        ),
        _result(
            stale_candidate,
            ContentClass.SKETCH_OR_WIP,
            source_cid="cid-old",
            processed_at=now,
            config_hash=config.config_hash,
        ),
        _result(
            noise_candidate,
            ContentClass.MEME,
            source_cid="cid-noise",
            processed_at=now,
            config_hash=config.config_hash,
            decision=FilterDecision.REJECT,
        ),
    ]
    candidates_path = tmp_path / "candidates.jsonl"
    decisions_path = tmp_path / "decisions.jsonl"
    _write_jsonl(candidates_path, candidates)
    _write_jsonl(decisions_path, decisions)

    selection = select_siglip_corpus(
        candidates_path,
        decisions_path,
        required_config_hash=config.config_hash,
    )

    assert [item.candidate.candidate_id for item in selection.items] == ["selected"]
    assert selection.items[0].result.source_cid == "cid-current"
    assert selection.counts["stale_decision"] == 1
    assert selection.counts["excluded_provenance"] == 1
    assert selection.counts["excluded_outcome"] == 1


def test_promote_siglip_result_records_explicit_seed_reason():
    candidate = _candidate("selected", post_cid="cid-current")
    result = _result(
        candidate,
        ContentClass.SKETCH_OR_WIP,
        source_cid="cid-current",
        processed_at=datetime.now(timezone.utc),
        config_hash="config-hash",
    )

    promoted = promote_siglip_result(result)

    assert promoted.decision == FilterDecision.ACCEPT
    assert promoted.accepted_for_main_corpus
    assert promoted.route == "sketches"
    assert CORPUS_SEED_REASON in promoted.reason_codes
    assert result.decision == FilterDecision.REVIEW


def test_archive_moves_active_corpus_and_seed_imports_fresh_sqlite(tmp_path):
    app_config = _app_config(tmp_path)
    filter_config = load_artwork_filter_config()
    app_config.database_path.write_text("old database", encoding="utf-8")
    app_config.raw_dir.mkdir(parents=True)
    (app_config.raw_dir / "old.jpg").write_bytes(b"old")
    app_config.processed_dir.mkdir(parents=True)
    (app_config.processed_dir / "old.jpg").write_bytes(b"old")
    app_config.retrieval.gallery_output_path.write_text("old gallery", encoding="utf-8")

    archive = archive_active_corpus(
        app_config,
        archive_root=tmp_path / "archives",
        now=datetime(2026, 7, 24, tzinfo=timezone.utc),
    )

    assert archive.manifest_path.is_file()
    assert not app_config.database_path.exists()
    assert not app_config.raw_dir.exists()
    assert not app_config.processed_dir.exists()
    manifest = json.loads(archive.manifest_path.read_text(encoding="utf-8"))
    assert len(manifest["moved"]) == 4

    candidate = _candidate("selected", post_cid="cid-current")
    result = _result(
        candidate,
        ContentClass.FINISHED_ILLUSTRATION,
        source_cid="cid-current",
        processed_at=datetime.now(timezone.utc),
        config_hash=filter_config.config_hash,
    )
    selection = CorpusSelection(
        items=[SelectedCorpusItem(candidate=candidate, result=result)],
        counts={"selected": 1},
    )

    summary = asyncio.run(
        seed_siglip_corpus(
            selection,
            app_config=app_config,
            filter_config=filter_config,
            image_loader=FakeImageLoader(),
        )
    )

    assert summary["imported"] == 1
    assert summary["errors"] == 0
    with connect(app_config.database_path) as conn:
        artwork = conn.execute(
            "SELECT source_platform, source_id, validated FROM artworks"
        ).fetchone()
        evidence = conn.execute(
            "SELECT decision, route, evidence_json FROM artwork_filter_decisions"
        ).fetchone()
    assert tuple(artwork) == ("bluesky", candidate.candidate_id, 1)
    assert evidence["decision"] == "accept"
    assert evidence["route"] == "main_art"
    assert CORPUS_SEED_REASON in evidence["evidence_json"]


def _candidate(
    candidate_id: str,
    *,
    post_cid: str,
    is_quote_post: bool = False,
) -> ImageCandidate:
    return ImageCandidate(
        candidate_id=candidate_id,
        author_did="did:plc:test-artist",
        author_handle="artist.example",
        post_uri=f"at://did:plc:test-artist/app.bsky.feed.post/{candidate_id}",
        post_cid=post_cid,
        image_index=0,
        thumbnail_url="https://cdn.bsky.app/img/feed_thumbnail/plain/test",
        fullsize_url="https://cdn.bsky.app/img/feed_fullsize/plain/test",
        is_quote_post=is_quote_post,
        source="bluesky",
    )


def _result(
    candidate: ImageCandidate,
    content_class: ContentClass,
    *,
    source_cid: str,
    processed_at: datetime,
    config_hash: str,
    decision: FilterDecision = FilterDecision.REVIEW,
) -> FilterResult:
    visual = VisualScores(
        backend="fake",
        model_id="fake-siglip",
        model_revision="revision",
        mode=ModelMode.ZERO_SHOT,
        class_scores=[ClassScore(content_class=content_class, score=0.9)],
        art_utility_score=0.9,
        noise_score=0.1,
        confidence_margin=0.8,
        embedding_dimension=4,
    )
    return FilterResult(
        candidate_id=candidate.candidate_id,
        decision=decision,
        predicted_class=content_class,
        accepted_for_main_corpus=decision == FilterDecision.ACCEPT,
        route="review" if decision == FilterDecision.REVIEW else "rejected",
        final_score=0.85,
        confidence=0.9,
        reason_codes=[f"{decision.value}.test"],
        image_sha256="b" * 64,
        width=640,
        height=480,
        visual_scores=visual,
        text_scores=None,
        rule_result=None,
        model_version="fake-siglip",
        config_version="test",
        prompt_version="prompts-v1",
        processed_at=processed_at,
        duration_ms=1.0,
        source_uri=candidate.post_uri,
        source_cid=source_cid,
        author_did=candidate.author_did,
        image_index=candidate.image_index,
        config_hash=config_hash,
        software_version="test",
    )


def _write_jsonl(path, models):
    path.write_text(
        "\n".join(model.model_dump_json() for model in models) + "\n",
        encoding="utf-8",
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
