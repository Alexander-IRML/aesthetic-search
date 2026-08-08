from __future__ import annotations

from collections.abc import AsyncIterable
from dataclasses import asdict, dataclass
from pathlib import Path

from artsearch.artwork_filter.batch import completed_candidate_keys, iter_candidate_batches
from artsearch.artwork_filter.corpus import CorpusRouter
from artsearch.artwork_filter.corpus_store import ArtworkFilterCorpusStore
from artsearch.artwork_filter.enums import FilterDecision
from artsearch.artwork_filter.schemas import ImageCandidate
from artsearch.artwork_filter.service import ArtworkFilterService
from artsearch.bluesky.io import JSONLCandidateStore


@dataclass
class PipelineSummary:
    actors_started: int = 0
    actors_completed: int = 0
    actors_skipped: int = 0
    actor_errors: int = 0
    candidates: int = 0
    skipped: int = 0
    accepted: int = 0
    review: int = 0
    rejected: int = 0
    classification_errors: int = 0
    routed: int = 0
    imported: int = 0
    unchanged: int = 0
    duplicates: int = 0
    review_stored: int = 0
    route_errors: int = 0

    def to_dict(self) -> dict[str, int]:
        return asdict(self)

    def merge(self, other: "PipelineSummary") -> None:
        for field_name, value in asdict(other).items():
            setattr(self, field_name, getattr(self, field_name) + value)

    @property
    def has_errors(self) -> bool:
        return bool(self.actor_errors or self.classification_errors or self.route_errors)


class BlueskyArtworkPipeline:
    """Own integration flow while keeping collection, filtering, and ingest independent."""

    def __init__(
        self,
        service: ArtworkFilterService,
        router: CorpusRouter,
        corpus_store: ArtworkFilterCorpusStore,
        *,
        candidate_store: JSONLCandidateStore | None = None,
    ) -> None:
        self.service = service
        self.router = router
        self.corpus_store = corpus_store
        self.candidate_store = candidate_store

    async def process_stream(
        self,
        candidates: AsyncIterable[ImageCandidate],
        *,
        summary: PipelineSummary | None = None,
        skip_candidate_keys: set[tuple[str, str | None]] | None = None,
    ) -> PipelineSummary:
        summary = summary or PipelineSummary()
        skip_keys = skip_candidate_keys if skip_candidate_keys is not None else set()
        batch: list[ImageCandidate] = []
        try:
            async for candidate in candidates:
                candidate_key = (candidate.candidate_id, candidate.post_cid)
                if candidate_key in skip_keys:
                    summary.skipped += 1
                    continue
                skip_keys.add(candidate_key)
                batch.append(candidate)
                if len(batch) == self.service.config.model.batch_size:
                    ready, batch = batch, []
                    await self._process_batch(ready, summary)
            if batch:
                ready, batch = batch, []
                await self._process_batch(ready, summary)
        except Exception:
            if batch:
                ready, batch = batch, []
                await self._process_batch(ready, summary)
            if self.candidate_store is not None:
                self.candidate_store.commit()
            raise
        if self.candidate_store is not None:
            self.candidate_store.commit(allow_empty=True)
        return summary

    async def process_jsonl(
        self,
        path: str | Path,
        *,
        resume_decisions_path: str | Path | None = None,
    ) -> PipelineSummary:
        summary = PipelineSummary()
        skip_keys = self.completed_candidate_keys(resume_decisions_path)

        def record_skip(candidate: ImageCandidate) -> None:
            summary.skipped += 1

        for batch in iter_candidate_batches(
            path,
            batch_size=self.service.config.model.batch_size,
            skip_candidate_keys=skip_keys,
            on_skip=record_skip,
        ):
            await self._process_batch(batch, summary, record_candidates=False)
        return summary

    def completed_candidate_keys(
        self,
        path: str | Path | None,
    ) -> set[tuple[str, str | None]]:
        if path is None:
            return set()
        classifier = self.service.visual_classifier
        return completed_candidate_keys(
            path,
            config_hash=self.service.config.config_hash,
            model_id=self.service.config.model.model_id,
            model_revision=classifier.model_revision if classifier is not None else None,
            prompt_version=classifier.prompt_version if classifier is not None else None,
        )

    async def _process_batch(
        self,
        candidates: list[ImageCandidate],
        summary: PipelineSummary,
        *,
        record_candidates: bool = True,
    ) -> None:
        if record_candidates and self.candidate_store is not None:
            self.candidate_store.append_many(candidates)
        results = await self.service.classify_many(candidates)
        routed = await self.router.route_many(candidates, results)
        stored = self.corpus_store.persist_batch(candidates, results, routed)

        summary.candidates += len(candidates)
        summary.accepted += sum(result.decision == FilterDecision.ACCEPT for result in results)
        summary.review += sum(result.decision == FilterDecision.REVIEW for result in results)
        summary.rejected += sum(result.decision == FilterDecision.REJECT for result in results)
        summary.classification_errors += sum(
            result.decision == FilterDecision.ERROR for result in results
        )
        summary.routed += sum(item.status == "stored" for item in routed)
        summary.imported += stored["imported"]
        summary.unchanged += stored["unchanged"]
        summary.duplicates += stored["duplicates"]
        summary.review_stored += stored["review_stored"]
        summary.route_errors += stored["errors"]
