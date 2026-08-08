from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

from artsearch.artwork_filter.enums import (
    ContentClass,
    FilterDecision,
    ModelMode,
    RuleDisposition,
)
from artsearch.artwork_filter.gallery import write_bluesky_gallery
from artsearch.artwork_filter.schemas import (
    ClassScore,
    FilterResult,
    ImageCandidate,
    RuleHit,
    RuleResult,
    TextScores,
    VisualScores,
)


def test_bluesky_gallery_renders_latest_decision_and_evidence(tmp_path: Path) -> None:
    candidates_path = tmp_path / "candidates.jsonl"
    decisions_path = tmp_path / "decisions.jsonl"
    output_path = tmp_path / "gallery.html"
    candidate = _candidate()
    candidates_path.write_text(candidate.model_dump_json() + "\n", encoding="utf-8")
    decisions_path.write_text(
        "\n".join(
            [
                _result(
                    decision=FilterDecision.REJECT,
                    processed_at=datetime(2026, 7, 22, tzinfo=timezone.utc),
                ).model_dump_json(),
                _result(
                    decision=FilterDecision.REVIEW,
                    processed_at=datetime(2026, 7, 23, tzinfo=timezone.utc),
                ).model_dump_json(),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    written = write_bluesky_gallery(
        candidates_path=candidates_path,
        decisions_path=decisions_path,
        output_path=output_path,
    )

    assert written == output_path
    html = output_path.read_text(encoding="utf-8")
    payload = _embedded_payload(html)
    assert "Bluesky Corpus Review" in html
    assert payload["summary"]["displayedCount"] == 1
    assert payload["summary"]["decisions"] == {"review": 1}
    assert payload["items"][0]["decision"] == "review"
    assert payload["items"][0]["authorLabel"] == "artist.bsky.social"
    assert payload["items"][0]["postUrl"] == (
        "https://bsky.app/profile/artist.bsky.social/post/3example"
    )
    assert payload["items"][0]["classScores"][0] == {
        "contentClass": "finished_illustration",
        "score": 0.82,
    }
    assert payload["items"][0]["reasonCodes"] == ["review.low_margin"]
    assert payload["items"][0]["contentLabels"] == ["bot"]
    assert payload["items"][0]["authorLabels"] == []


def test_bluesky_gallery_escapes_script_endings_in_source_text(tmp_path: Path) -> None:
    candidates_path = tmp_path / "candidates.jsonl"
    decisions_path = tmp_path / "decisions.jsonl"
    output_path = tmp_path / "gallery.html"
    candidate = _candidate(post_text="hello </script><script>alert(1)</script>")
    candidates_path.write_text(candidate.model_dump_json() + "\n", encoding="utf-8")
    decisions_path.write_text(_result().model_dump_json() + "\n", encoding="utf-8")

    write_bluesky_gallery(
        candidates_path=candidates_path,
        decisions_path=decisions_path,
        output_path=output_path,
    )

    html = output_path.read_text(encoding="utf-8")
    data_block = _embedded_data_block(html)
    assert "</script>" not in data_block
    assert "<\\/script>" in data_block
    assert _embedded_payload(html)["items"][0]["postText"] == (
        "hello </script><script>alert(1)</script>"
    )


def test_bluesky_gallery_excludes_a_decision_for_an_older_post_cid(
    tmp_path: Path,
) -> None:
    candidates_path = tmp_path / "candidates.jsonl"
    decisions_path = tmp_path / "decisions.jsonl"
    output_path = tmp_path / "gallery.html"
    candidates_path.write_text(
        _candidate(post_cid="current-cid").model_dump_json() + "\n",
        encoding="utf-8",
    )
    decisions_path.write_text(
        _result(source_cid="older-cid").model_dump_json() + "\n",
        encoding="utf-8",
    )

    write_bluesky_gallery(
        candidates_path=candidates_path,
        decisions_path=decisions_path,
        output_path=output_path,
    )

    payload = _embedded_payload(output_path.read_text(encoding="utf-8"))
    assert payload["items"] == []
    assert payload["summary"]["staleDecisionCount"] == 1


def _candidate(
    *,
    post_text: str = "Finished artwork",
    post_cid: str = "current-cid",
) -> ImageCandidate:
    return ImageCandidate(
        candidate_id="candidate-1",
        author_did="did:plc:artist",
        author_handle="artist.bsky.social",
        post_uri="at://did:plc:artist/app.bsky.feed.post/3example",
        post_cid=post_cid,
        image_index=0,
        thumbnail_url="https://cdn.bsky.app/img/feed_thumbnail/plain/example",
        fullsize_url="https://cdn.bsky.app/img/feed_fullsize/plain/example",
        post_text=post_text,
        alt_text="A finished character illustration",
        content_labels=["bot"],
        created_at=datetime(2026, 7, 23, 12, tzinfo=timezone.utc),
    )


def _result(
    *,
    decision: FilterDecision = FilterDecision.REVIEW,
    processed_at: datetime | None = None,
    source_cid: str = "current-cid",
) -> FilterResult:
    return FilterResult(
        candidate_id="candidate-1",
        decision=decision,
        predicted_class=ContentClass.FINISHED_ILLUSTRATION,
        accepted_for_main_corpus=False,
        route="review",
        final_score=0.74,
        confidence=0.82,
        reason_codes=["review.low_margin"],
        image_sha256="abc123",
        width=640,
        height=480,
        visual_scores=VisualScores(
            backend="siglip2",
            model_id="google/siglip2-base-patch16-224",
            mode=ModelMode.ZERO_SHOT,
            class_scores=[
                ClassScore(
                    content_class=ContentClass.FINISHED_ILLUSTRATION,
                    score=0.82,
                ),
                ClassScore(content_class=ContentClass.MEME, score=0.18),
            ],
            art_utility_score=0.79,
            noise_score=0.21,
            confidence_margin=0.08,
            embedding_dimension=768,
        ),
        text_scores=TextScores(
            positive_score=0.5,
            negative_score=0.0,
            net_score=0.5,
            matched_positive_terms=["artwork"],
        ),
        rule_result=RuleResult(
            disposition=RuleDisposition.CONTINUE,
            hits=[
                RuleHit(
                    rule_id="metadata.text_heavy",
                    disposition=RuleDisposition.CONTINUE,
                    reason_code="rule.text_neutral",
                    message="No routing override.",
                )
            ],
        ),
        model_version="google/siglip2-base-patch16-224",
        config_version="1.0.0",
        prompt_version="1.0.0",
        config_hash="config-hash",
        source_uri="at://did:plc:artist/app.bsky.feed.post/3example",
        source_cid=source_cid,
        author_did="did:plc:artist",
        image_index=0,
        processed_at=processed_at or datetime(2026, 7, 23, tzinfo=timezone.utc),
        duration_ms=12.5,
    )


def _embedded_payload(html: str) -> dict:
    return json.loads(_embedded_data_block(html))


def _embedded_data_block(html: str) -> str:
    marker = '<script id="artsearchData" type="application/json">'
    return html.split(marker, 1)[1].split("</script>", 1)[0]
