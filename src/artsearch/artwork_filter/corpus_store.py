from __future__ import annotations

import json
from pathlib import Path
import sqlite3

from artsearch.artwork_filter.config import ArtworkFilterConfig
from artsearch.artwork_filter.corpus import (
    RoutedImage,
    bluesky_artist_folder,
    bluesky_artist_id,
    safe_candidate_filename,
)
from artsearch.artwork_filter.enums import FilterDecision
from artsearch.artwork_filter.errors import PersistenceError
from artsearch.artwork_filter.hashing import sha256_bytes, stable_json_hash
from artsearch.artwork_filter.schemas import FilterResult, ImageCandidate
from artsearch.ingest.artists import ArtistRecord, register_artist
from artsearch.ingest.config import AppConfig
from artsearch.ingest.db import connect, init_db
from artsearch.ingest.standardize import standardize_image


class ArtworkFilterCorpusStore:
    """Persist filter evidence and import only routed ACCEPT images into the corpus."""

    def __init__(self, app_config: AppConfig, filter_config: ArtworkFilterConfig) -> None:
        self.app_config = app_config
        self.filter_config = filter_config

    def persist_batch(
        self,
        candidates: list[ImageCandidate],
        results: list[FilterResult],
        routed_images: list[RoutedImage],
    ) -> dict[str, int]:
        if len(candidates) != len(results):
            raise ValueError("candidates and results must have the same length")
        routed_by_source = {(item.candidate_id, item.source_cid): item for item in routed_images}
        summary = {
            "decisions": 0,
            "imported": 0,
            "unchanged": 0,
            "duplicates": 0,
            "review_stored": 0,
            "errors": 0,
        }

        try:
            with connect(self.app_config.database_path) as conn:
                init_db(conn)
                for candidate, result in zip(candidates, results, strict=True):
                    if result.config_hash and result.config_hash != self.filter_config.config_hash:
                        raise ValueError(
                            "result configuration hash does not match the corpus store"
                        )
                    decision_key = decision_cache_key(result, candidate)
                    self._insert_decision(conn, decision_key, candidate, result)
                    summary["decisions"] += 1
                    routed = routed_by_source.get((candidate.candidate_id, candidate.post_cid))
                    if routed is None:
                        continue
                    if routed.status == "error":
                        self._insert_route(conn, decision_key, routed, artwork_id=None)
                        summary["errors"] += 1
                        continue
                    if routed.target == "review":
                        self._insert_route(conn, decision_key, routed, artwork_id=None)
                        summary["review_stored"] += 1
                        continue
                    if result.decision != FilterDecision.ACCEPT:
                        continue
                    try:
                        import_status, artwork_id = self._import_accepted(
                            conn,
                            candidate,
                            result,
                            routed,
                        )
                        routed.status = "duplicate" if import_status == "duplicates" else "stored"
                        self._insert_route(conn, decision_key, routed, artwork_id=artwork_id)
                        summary[import_status] += 1
                    except (OSError, RuntimeError, ValueError, sqlite3.DatabaseError) as exc:
                        routed.status = "error"
                        routed.error_type = type(exc).__name__
                        routed.error_message = str(exc)
                        self._insert_route(conn, decision_key, routed, artwork_id=None)
                        summary["errors"] += 1
                conn.commit()
        except sqlite3.DatabaseError as exc:
            raise PersistenceError(f"could not persist artwork-filter batch: {exc}") from exc
        return summary

    def _insert_decision(
        self,
        conn: sqlite3.Connection,
        decision_key: str,
        candidate: ImageCandidate,
        result: FilterResult,
    ) -> None:
        visual = result.visual_scores
        conn.execute(
            """
            INSERT OR IGNORE INTO artwork_filter_decisions (
                decision_key, candidate_id, author_did, post_uri, image_index,
                image_sha256, decision, predicted_class, accepted_for_main_corpus,
                route, final_score, confidence, reason_codes_json, candidate_json,
                evidence_json, model_id, model_revision, config_version, config_hash,
                prompt_version, classifier_version, software_version, processed_at,
                duration_ms, error_type, error_message
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                decision_key,
                result.candidate_id,
                candidate.author_did,
                candidate.post_uri,
                candidate.image_index,
                result.image_sha256,
                result.decision.value,
                result.predicted_class.value,
                int(result.accepted_for_main_corpus),
                result.route,
                result.final_score,
                result.confidence,
                json.dumps(result.reason_codes),
                candidate.model_dump_json(),
                result.model_dump_json(),
                result.model_version,
                visual.model_revision if visual is not None else None,
                result.config_version,
                result.config_hash,
                result.prompt_version,
                result.classifier_version,
                result.software_version,
                result.processed_at.isoformat(),
                result.duration_ms,
                result.error_type,
                result.error_message,
            ),
        )

    def _insert_route(
        self,
        conn: sqlite3.Connection,
        decision_key: str,
        routed: RoutedImage,
        *,
        artwork_id: str | None,
    ) -> None:
        route_key = stable_json_hash(
            {
                "decision_key": decision_key,
                "target": routed.target,
                "candidate_id": routed.candidate_id,
                "source_cid": routed.source_cid,
            }
        )
        conn.execute(
            """
            INSERT INTO artwork_filter_routes (
                route_key, decision_key, candidate_id, target, status, local_path,
                image_sha256, perceptual_hash, width, height, artwork_id,
                error_type, error_message
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(route_key) DO UPDATE SET
                status = excluded.status,
                local_path = excluded.local_path,
                image_sha256 = excluded.image_sha256,
                perceptual_hash = excluded.perceptual_hash,
                width = excluded.width,
                height = excluded.height,
                artwork_id = excluded.artwork_id,
                error_type = excluded.error_type,
                error_message = excluded.error_message,
                routed_at = CURRENT_TIMESTAMP
            """,
            (
                route_key,
                decision_key,
                routed.candidate_id,
                routed.target,
                routed.status,
                self._path_for_db(routed.local_path) if routed.local_path is not None else None,
                routed.image_sha256,
                routed.perceptual_hash,
                routed.width,
                routed.height,
                artwork_id,
                routed.error_type,
                routed.error_message,
            ),
        )

    def _import_accepted(
        self,
        conn: sqlite3.Connection,
        candidate: ImageCandidate,
        result: FilterResult,
        routed: RoutedImage,
    ) -> tuple[str, str | None]:
        if routed.local_path is None or routed.image_sha256 is None:
            raise ValueError("stored corpus routes require a local path and content hash")

        existing = conn.execute(
            """
            SELECT * FROM artworks
             WHERE source_platform = 'bluesky' AND source_id = ?
             LIMIT 1
            """,
            (candidate.candidate_id,),
        ).fetchone()
        if (
            existing is not None
            and existing["validated"]
            and existing["file_hash"] == routed.image_sha256
            and self._processed_file_exists(existing["processed_path"])
        ):
            return "unchanged", str(existing["artwork_id"])

        duplicate = conn.execute(
            """
            SELECT artwork_id FROM artworks
             WHERE file_hash = ? AND validated = 1
               AND (? IS NULL OR artwork_id != ?)
             LIMIT 1
            """,
            (
                routed.image_sha256,
                str(existing["artwork_id"]) if existing is not None else None,
                str(existing["artwork_id"]) if existing is not None else None,
            ),
        ).fetchone()
        if duplicate is not None:
            return "duplicates", str(duplicate["artwork_id"])

        artwork_id = (
            str(existing["artwork_id"])
            if existing is not None
            else f"art_{sha256_bytes(f'bluesky|{candidate.candidate_id}'.encode())[:32]}"
        )
        processed_path = (
            self.app_config.processed_dir
            / bluesky_artist_folder(candidate)
            / f"{safe_candidate_filename(candidate.candidate_id)}.jpg"
        )
        if existing is not None:
            conn.execute(
                "UPDATE artworks SET validated = 0 WHERE artwork_id = ?",
                (artwork_id,),
            )
            conn.execute("DELETE FROM embeddings WHERE artwork_id = ?", (artwork_id,))
            conn.commit()
        standardized = standardize_image(
            routed.local_path,
            processed_path,
            self.app_config.images,
        )
        artist = _artist_record(candidate)
        register_artist(conn, artist)
        transform = standardized.transform
        values = {
            "artist_id": artist.artist_id,
            "raw_path": self._path_for_db(routed.local_path),
            "processed_path": self._path_for_db(processed_path),
            "source_platform": "bluesky",
            "source_id": candidate.candidate_id,
            "orig_width": standardized.orig_width,
            "orig_height": standardized.orig_height,
            "file_hash": routed.image_sha256,
            "phash": routed.perceptual_hash,
            "is_sfw": None,
            "validated": 1,
            "review_status": "unreviewed",
            "duplicate_of": None,
            "scale_factor": transform.scale_factor,
            "pad_left": transform.pad_left,
            "pad_top": transform.pad_top,
            "pad_right": transform.pad_right,
            "pad_bottom": transform.pad_bottom,
            "crop_left": transform.crop_left,
            "crop_top": transform.crop_top,
            "crop_right": transform.crop_right,
            "crop_bottom": transform.crop_bottom,
            "notes": f"artwork_filter_route={result.route}",
        }
        if existing is None:
            columns = ["artwork_id", *values]
            conn.execute(
                f"INSERT INTO artworks ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
                (artwork_id, *values.values()),
            )
        else:
            assignments = ", ".join(f"{column} = ?" for column in values)
            conn.execute(
                f"UPDATE artworks SET {assignments} WHERE artwork_id = ?",
                (*values.values(), artwork_id),
            )
        return "imported", artwork_id

    def _path_for_db(self, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(self.app_config.root_dir.resolve()))
        except ValueError:
            return str(path.resolve())

    def _processed_file_exists(self, value: str | None) -> bool:
        if not value:
            return False
        path = Path(value)
        if not path.is_absolute():
            path = self.app_config.root_dir / path
        return path.is_file()


def decision_cache_key(result: FilterResult, candidate: ImageCandidate) -> str:
    model_revision = (
        result.visual_scores.model_revision if result.visual_scores is not None else None
    )
    return stable_json_hash(
        {
            "candidate_id": result.candidate_id,
            "image_sha256": result.image_sha256,
            "model_id": result.model_version,
            "model_revision": model_revision,
            "config_hash": result.config_hash,
            "prompt_version": result.prompt_version,
            "classifier_version": result.classifier_version,
            "software_version": result.software_version,
            "source_uri": candidate.post_uri,
            "source_cid": candidate.post_cid,
            "post_text": candidate.post_text,
            "alt_text": candidate.alt_text,
            "content_labels": candidate.content_labels,
            "author_labels": candidate.author_labels,
            "is_repost": candidate.is_repost,
            "is_quote_post": candidate.is_quote_post,
            "quoted_author_did": candidate.quoted_author_did,
            "text_scores": (
                result.text_scores.model_dump(mode="json")
                if result.text_scores is not None
                else None
            ),
            "rule_result": (
                result.rule_result.model_dump(mode="json")
                if result.rule_result is not None
                else None
            ),
        }
    )


def _artist_record(candidate: ImageCandidate) -> ArtistRecord:
    artist_id = bluesky_artist_id(candidate)
    identity = candidate.author_did or candidate.author_handle or artist_id
    display_name = candidate.author_handle or f"Bluesky artist {artist_id[-8:]}"
    return ArtistRecord(
        artist_id=artist_id,
        display_name=display_name,
        folder_name=bluesky_artist_folder(candidate),
        source_platform="bluesky",
        source_url=f"https://bsky.app/profile/{identity}",
        notes="Imported through the ArtSearch artwork-content filter.",
    )
