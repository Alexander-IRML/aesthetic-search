from __future__ import annotations

import re
import unicodedata

from artsearch.artwork_filter.schemas import ImageCandidate, TextScores


POSITIVE_TERMS = (
    "character design",
    "reference sheet",
    "ref sheet",
    "model sheet",
    "work in progress",
    "art trade",
    "speedpaint",
    "timelapse",
    "commission",
    "sketch",
    "drawing",
    "illustration",
    "painting",
    "comic",
    "panel",
    "page",
    "render",
    "wip",
    "request",
    "adoptable",
    "process",
)
NEGATIVE_TERMS = (
    "reaction image",
    "package arrived",
    "merch arrived",
    "convention photo",
    "my cat",
    "my dog",
    "lunch",
    "dinner",
    "breakfast",
    "burger",
    "restaurant",
    "coffee",
    "selfie",
    "vacation",
    "hotel",
    "screenshot",
    "meme",
)
ROUTING_PATTERNS = (
    "commissions open",
    "commission prices",
    "price sheet",
    "terms of service",
    "tos",
    "adopt auction",
    "bidding",
    "ych",
    "merch",
    "print shop",
    "sticker",
    "keychain",
)


def score_text(candidate: ImageCandidate, *, max_text_length: int = 4000) -> TextScores:
    text = normalize_text(f"{candidate.post_text}\n{candidate.alt_text}")[:max_text_length]
    if not text:
        return TextScores(
            positive_score=0.0,
            negative_score=0.0,
            net_score=0.0,
            matched_positive_terms=[],
            matched_negative_terms=[],
            matched_patterns=[],
        )

    positives = _match_terms(text, POSITIVE_TERMS)
    negatives = _match_terms(text, NEGATIVE_TERMS)
    routing = _match_terms(text, ROUTING_PATTERNS)
    positive_score = _score_matches(positives)
    negative_score = _score_matches([*negatives, *routing])
    return TextScores(
        positive_score=positive_score,
        negative_score=negative_score,
        net_score=max(-1.0, min(1.0, positive_score - negative_score)),
        matched_positive_terms=positives,
        matched_negative_terms=negatives,
        matched_patterns=routing,
    )


def normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).lower()
    return re.sub(r"\s+", " ", normalized).strip()


def _match_terms(text: str, terms: tuple[str, ...]) -> list[str]:
    matches = []
    for term in sorted(terms, key=len, reverse=True):
        pattern = rf"(?<!\w){re.escape(term.lower())}(?!\w)"
        if re.search(pattern, text):
            matches.append(term)
    return matches


def _score_matches(matches: list[str]) -> float:
    return min(1.0, 0.25 * len(matches))
