from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

from artsearch.artwork_filter.errors import PersistenceError
from artsearch.artwork_filter.review_export import (
    load_latest_candidates,
    load_latest_decisions,
)
from artsearch.artwork_filter.schemas import FilterResult, ImageCandidate


def write_bluesky_gallery(
    *,
    candidates_path: str | Path,
    decisions_path: str | Path,
    output_path: str | Path,
) -> Path:
    """Write a read-only review view using the latest decision for each candidate."""

    candidate_source = Path(candidates_path)
    decision_source = Path(decisions_path)
    destination = Path(output_path)
    candidates = load_latest_candidates(candidate_source)
    decisions = load_latest_decisions(decision_source)

    items: list[dict[str, Any]] = []
    undecided_candidates = 0
    stale_decisions = 0
    for candidate_id, candidate in candidates.items():
        result = decisions.get(candidate_id)
        if result is None:
            undecided_candidates += 1
            continue
        if (
            result.source_cid is not None
            and candidate.post_cid is not None
            and result.source_cid != candidate.post_cid
        ):
            stale_decisions += 1
            continue
        items.append(_gallery_item(candidate, result, destination))

    items.sort(
        key=lambda item: (
            item["createdAt"] or "",
            item["processedAt"],
            item["candidateId"],
        ),
        reverse=True,
    )
    unmatched_decisions = len(set(decisions) - set(candidates))
    payload = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "sources": {
            "candidates": str(candidate_source),
            "decisions": str(decision_source),
        },
        "summary": {
            "candidateCount": len(candidates),
            "decisionCount": len(decisions),
            "displayedCount": len(items),
            "undecidedCandidateCount": undecided_candidates,
            "staleDecisionCount": stale_decisions,
            "unmatchedDecisionCount": unmatched_decisions,
            "decisions": dict(Counter(item["decision"] for item in items)),
            "classes": dict(Counter(item["predictedClass"] for item in items)),
            "routes": dict(Counter(item["route"] for item in items)),
        },
        "items": items,
    }

    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(_render_bluesky_gallery(payload), encoding="utf-8")
    except OSError as exc:
        raise PersistenceError(f"could not write Bluesky gallery: {exc}") from exc
    return destination


def _gallery_item(
    candidate: ImageCandidate,
    result: FilterResult,
    destination: Path,
) -> dict[str, Any]:
    visual = result.visual_scores
    text = result.text_scores
    rules = result.rule_result
    thumbnail_url = _safe_http_url(candidate.thumbnail_url)
    fullsize_url = _safe_http_url(candidate.fullsize_url)
    local_image = _local_image_src(candidate.local_path, destination)
    image_src = thumbnail_url or fullsize_url or local_image
    detail_image_src = fullsize_url or thumbnail_url or local_image

    class_scores = []
    if visual is not None:
        class_scores = sorted(
            (
                {
                    "contentClass": score.content_class.value,
                    "score": score.score,
                }
                for score in visual.class_scores
            ),
            key=lambda item: item["score"],
            reverse=True,
        )

    return {
        "candidateId": candidate.candidate_id,
        "authorDid": candidate.author_did,
        "authorHandle": candidate.author_handle,
        "authorLabel": candidate.author_handle or candidate.author_did or "Unknown artist",
        "postUri": candidate.post_uri,
        "postUrl": _bluesky_post_url(candidate),
        "postCid": candidate.post_cid,
        "imageIndex": candidate.image_index,
        "imageSrc": image_src,
        "detailImageSrc": detail_image_src,
        "thumbnailUrl": thumbnail_url,
        "fullsizeUrl": fullsize_url,
        "postText": candidate.post_text,
        "altText": candidate.alt_text,
        "createdAt": candidate.created_at.isoformat() if candidate.created_at else None,
        "contentLabels": candidate.content_labels,
        "authorLabels": candidate.author_labels,
        "isRepost": candidate.is_repost,
        "isQuotePost": candidate.is_quote_post,
        "decision": result.decision.value,
        "predictedClass": result.predicted_class.value,
        "route": result.route,
        "acceptedForMainCorpus": result.accepted_for_main_corpus,
        "finalScore": result.final_score,
        "confidence": result.confidence,
        "confidenceMargin": visual.confidence_margin if visual is not None else 0.0,
        "artUtilityScore": visual.art_utility_score if visual is not None else None,
        "noiseScore": visual.noise_score if visual is not None else None,
        "classScores": class_scores,
        "reasonCodes": result.reason_codes,
        "ruleHits": [
            {
                "ruleId": hit.rule_id,
                "disposition": hit.disposition.value,
                "reasonCode": hit.reason_code,
                "message": hit.message,
            }
            for hit in rules.hits
        ]
        if rules is not None
        else [],
        "textSignals": {
            "positiveScore": text.positive_score,
            "negativeScore": text.negative_score,
            "netScore": text.net_score,
            "positiveTerms": text.matched_positive_terms,
            "negativeTerms": text.matched_negative_terms,
            "patterns": text.matched_patterns,
        }
        if text is not None
        else None,
        "imageSha256": result.image_sha256,
        "width": result.width,
        "height": result.height,
        "modelVersion": result.model_version,
        "configVersion": result.config_version,
        "configHash": result.config_hash,
        "promptVersion": result.prompt_version,
        "classifierVersion": result.classifier_version,
        "processedAt": result.processed_at.isoformat(),
        "durationMs": result.duration_ms,
        "errorType": result.error_type,
        "errorMessage": result.error_message,
    }


def _safe_http_url(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return value


def _local_image_src(local_path: Path | None, destination: Path) -> str | None:
    if local_path is None:
        return None
    image_path = local_path if local_path.is_absolute() else Path.cwd() / local_path
    return Path(os.path.relpath(image_path, destination.parent)).as_posix()


def _bluesky_post_url(candidate: ImageCandidate) -> str | None:
    if not candidate.post_uri or not candidate.post_uri.startswith("at://"):
        return None
    parts = candidate.post_uri.removeprefix("at://").split("/")
    if len(parts) < 3 or parts[1] != "app.bsky.feed.post":
        return None
    actor = candidate.author_handle or candidate.author_did or parts[0]
    return f"https://bsky.app/profile/{quote(actor, safe=':.')}/post/{quote(parts[2], safe='')}"


def _render_bluesky_gallery(payload: dict[str, Any]) -> str:
    data_json = json.dumps(payload, ensure_ascii=True).replace("</", "<\\/")
    return _HTML_TEMPLATE.replace("__ARTSEARCH_DATA__", data_json)


_HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ArtSearch Bluesky Corpus</title>
  <style>
    :root {
      color-scheme: light;
      --ink: #18211e;
      --muted: #5d6965;
      --line: #d6ddda;
      --surface: #ffffff;
      --canvas: #f2f5f3;
      --accent: #006b61;
      --accent-soft: #e5f3f0;
      --accept: #176b42;
      --accept-soft: #e7f4ec;
      --review: #8a5a00;
      --review-soft: #fff3d7;
      --reject: #a33636;
      --reject-soft: #fbeaea;
      --error: #8b1e3f;
      --error-soft: #f9e8ee;
    }
    * {
      box-sizing: border-box;
    }
    body {
      margin: 0;
      min-height: 100vh;
      font-family:
        Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI",
        sans-serif;
      color: var(--ink);
      background: var(--canvas);
    }
    button, input, select {
      font: inherit;
    }
    button, select {
      cursor: pointer;
    }
    h1, h2, h3, p {
      margin: 0;
    }
    .topbar {
      position: sticky;
      z-index: 10;
      top: 0;
      border-bottom: 1px solid var(--line);
      background: rgba(255, 255, 255, 0.97);
    }
    .title-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 20px;
      padding: 14px 20px 12px;
    }
    .title-block {
      min-width: 220px;
    }
    h1 {
      font-size: 20px;
      font-weight: 720;
    }
    .subtitle {
      margin-top: 3px;
      color: var(--muted);
      font-size: 13px;
    }
    .stats {
      display: flex;
      flex-wrap: wrap;
      justify-content: flex-end;
      gap: 6px;
    }
    .stat {
      display: grid;
      min-width: 76px;
      gap: 1px;
      padding: 6px 9px;
      border: 1px solid var(--line);
      border-radius: 6px;
      color: var(--ink);
      background: var(--surface);
      text-align: left;
    }
    .stat:hover {
      border-color: #9aa9a4;
    }
    .stat[aria-pressed="true"] {
      border-color: var(--accent);
      background: var(--accent-soft);
    }
    .stat strong {
      font-size: 16px;
      line-height: 1.1;
    }
    .stat span {
      color: var(--muted);
      font-size: 11px;
      text-transform: uppercase;
    }
    .controls {
      display: grid;
      grid-template-columns: minmax(220px, 1fr) repeat(3, minmax(130px, 180px)) auto;
      gap: 8px;
      padding: 10px 20px;
      border-top: 1px solid #edf0ee;
    }
    .control {
      display: grid;
      gap: 4px;
    }
    .control span {
      color: var(--muted);
      font-size: 11px;
      font-weight: 650;
      text-transform: uppercase;
    }
    input, select, .reset {
      width: 100%;
      min-height: 36px;
      border: 1px solid #bcc7c3;
      border-radius: 6px;
      color: var(--ink);
      background: var(--surface);
    }
    input, select {
      padding: 7px 9px;
    }
    input:focus, select:focus, button:focus-visible {
      outline: 2px solid #6cb4aa;
      outline-offset: 1px;
    }
    .reset {
      align-self: end;
      padding: 7px 11px;
      color: var(--accent);
      font-weight: 650;
    }
    main {
      padding: 16px 20px 32px;
    }
    .result-row {
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 12px;
      color: var(--muted);
      font-size: 13px;
    }
    .result-row strong {
      color: var(--ink);
    }
    .gallery {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(205px, 1fr));
      gap: 12px;
    }
    .art-card {
      display: grid;
      min-width: 0;
      overflow: hidden;
      padding: 0;
      border: 1px solid var(--line);
      border-radius: 7px;
      color: inherit;
      background: var(--surface);
      text-align: left;
    }
    .art-card:hover {
      border-color: #8fa19b;
      box-shadow: 0 3px 12px rgba(24, 33, 30, 0.09);
    }
    .image-wrap {
      position: relative;
      width: 100%;
      aspect-ratio: 1;
      overflow: hidden;
      background: #e5eae8;
    }
    .image-wrap img {
      display: block;
      width: 100%;
      height: 100%;
      object-fit: contain;
    }
    .image-missing {
      display: none;
      place-items: center;
      height: 100%;
      padding: 20px;
      color: var(--muted);
      text-align: center;
    }
    .badge {
      display: inline-flex;
      align-items: center;
      width: max-content;
      min-height: 21px;
      padding: 2px 7px;
      border: 1px solid currentColor;
      border-radius: 999px;
      font-size: 10px;
      font-weight: 750;
      text-transform: uppercase;
    }
    .image-wrap .badge {
      position: absolute;
      top: 8px;
      left: 8px;
      box-shadow: 0 1px 4px rgba(24, 33, 30, 0.2);
    }
    .decision-accept {
      color: var(--accept);
      background: var(--accept-soft);
    }
    .decision-review {
      color: var(--review);
      background: var(--review-soft);
    }
    .decision-reject {
      color: var(--reject);
      background: var(--reject-soft);
    }
    .decision-error {
      color: var(--error);
      background: var(--error-soft);
    }
    .card-body {
      display: grid;
      gap: 7px;
      padding: 10px;
    }
    .card-heading {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
    }
    .author {
      min-width: 0;
      overflow: hidden;
      font-size: 13px;
      font-weight: 700;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .score {
      flex: 0 0 auto;
      color: var(--accent);
      font-size: 12px;
      font-variant-numeric: tabular-nums;
      font-weight: 750;
    }
    .class-label {
      overflow: hidden;
      color: #3f4b47;
      font-size: 12px;
      font-weight: 600;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .description {
      display: -webkit-box;
      min-height: 34px;
      overflow: hidden;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.4;
      -webkit-box-orient: vertical;
      -webkit-line-clamp: 2;
    }
    .empty {
      display: none;
      padding: 64px 20px;
      border: 1px dashed #aebbb6;
      border-radius: 7px;
      color: var(--muted);
      background: var(--surface);
      text-align: center;
    }
    .page-note {
      margin-top: 18px;
      color: var(--muted);
      font-size: 11px;
    }
    dialog {
      width: min(1120px, calc(100vw - 32px));
      max-height: calc(100vh - 32px);
      padding: 0;
      overflow: hidden;
      border: 1px solid #aebbb6;
      border-radius: 8px;
      color: var(--ink);
      background: var(--surface);
      box-shadow: 0 18px 60px rgba(24, 33, 30, 0.28);
    }
    dialog::backdrop {
      background: rgba(22, 31, 28, 0.58);
    }
    .dialog-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 12px 14px;
      border-bottom: 1px solid var(--line);
    }
    .dialog-title {
      min-width: 0;
    }
    .dialog-title h2 {
      overflow: hidden;
      font-size: 16px;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .dialog-title p {
      margin-top: 2px;
      color: var(--muted);
      font-size: 12px;
    }
    .dialog-actions {
      display: flex;
      gap: 6px;
    }
    .icon-button {
      min-width: 34px;
      min-height: 34px;
      padding: 6px 9px;
      border: 1px solid var(--line);
      border-radius: 6px;
      color: var(--ink);
      background: var(--surface);
      font-weight: 700;
    }
    .dialog-body {
      display: grid;
      grid-template-columns: minmax(300px, 48%) 1fr;
      max-height: calc(100vh - 91px);
      overflow: auto;
    }
    .detail-media {
      display: grid;
      align-content: start;
      min-height: 420px;
      padding: 14px;
      border-right: 1px solid var(--line);
      background: #e5eae8;
    }
    .detail-media img {
      display: block;
      width: 100%;
      max-height: calc(100vh - 130px);
      object-fit: contain;
    }
    .detail-content {
      display: grid;
      align-content: start;
      gap: 18px;
      padding: 16px;
    }
    .detail-summary {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 8px;
    }
    .detail-score {
      font-size: 18px;
      font-variant-numeric: tabular-nums;
      font-weight: 750;
    }
    .link-row {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }
    .link-row a {
      padding: 6px 9px;
      border: 1px solid #aebbb6;
      border-radius: 6px;
      color: var(--accent);
      font-size: 12px;
      font-weight: 650;
      text-decoration: none;
    }
    .detail-section {
      display: grid;
      gap: 7px;
    }
    .detail-section h3 {
      color: #33403b;
      font-size: 12px;
      text-transform: uppercase;
    }
    .copy {
      color: #3f4b47;
      font-size: 13px;
      line-height: 1.5;
      overflow-wrap: anywhere;
      white-space: pre-wrap;
    }
    .muted-copy {
      color: var(--muted);
    }
    .chip-row {
      display: flex;
      flex-wrap: wrap;
      gap: 5px;
    }
    .chip {
      padding: 3px 6px;
      border-radius: 4px;
      color: #3f4b47;
      background: #eef2f0;
      font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
      font-size: 10px;
      overflow-wrap: anywhere;
    }
    .score-list {
      display: grid;
      gap: 7px;
    }
    .score-row {
      display: grid;
      grid-template-columns: minmax(120px, 1fr) 2fr 48px;
      align-items: center;
      gap: 8px;
      font-size: 11px;
    }
    .score-name {
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .score-track {
      height: 7px;
      overflow: hidden;
      border-radius: 4px;
      background: #e1e7e4;
    }
    .score-fill {
      height: 100%;
      background: var(--accent);
    }
    .score-value {
      color: var(--muted);
      font-variant-numeric: tabular-nums;
      text-align: right;
    }
    .facts {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 7px 12px;
      margin: 0;
    }
    .facts div {
      min-width: 0;
    }
    .facts dt {
      color: var(--muted);
      font-size: 10px;
      text-transform: uppercase;
    }
    .facts dd {
      margin: 2px 0 0;
      overflow-wrap: anywhere;
      font-size: 12px;
    }
    @media (max-width: 900px) {
      .title-row {
        align-items: flex-start;
        flex-direction: column;
      }
      .stats {
        justify-content: flex-start;
      }
      .controls {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }
      .control:first-child {
        grid-column: 1 / -1;
      }
      .dialog-body {
        grid-template-columns: 1fr;
      }
      .detail-media {
        min-height: 280px;
        border-right: 0;
        border-bottom: 1px solid var(--line);
      }
      .detail-media img {
        max-height: 52vh;
      }
    }
    @media (max-width: 560px) {
      .title-row, .controls, main {
        padding-right: 12px;
        padding-left: 12px;
      }
      .controls {
        grid-template-columns: 1fr;
      }
      .control:first-child {
        grid-column: auto;
      }
      .gallery {
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 8px;
      }
      .description {
        display: none;
      }
      .facts {
        grid-template-columns: 1fr;
      }
      dialog {
        width: 100vw;
        max-width: none;
        max-height: 100vh;
        border-radius: 0;
      }
    }
  </style>
</head>
<body>
  <header class="topbar">
    <div class="title-row">
      <div class="title-block">
        <h1>Bluesky Corpus Review</h1>
        <p id="subtitle" class="subtitle"></p>
      </div>
      <div id="stats" class="stats" aria-label="Decision filters"></div>
    </div>
    <div class="controls">
      <label class="control">
        <span>Search</span>
        <input id="search" type="search" placeholder="Artist, post text, alt text, ID">
      </label>
      <label class="control">
        <span>Content class</span>
        <select id="classFilter"><option value="">All classes</option></select>
      </label>
      <label class="control">
        <span>Route</span>
        <select id="routeFilter"><option value="">All routes</option></select>
      </label>
      <label class="control">
        <span>Sort</span>
        <select id="sort">
          <option value="newest">Newest post</option>
          <option value="score-desc">Highest art score</option>
          <option value="confidence-desc">Highest confidence</option>
          <option value="margin-asc">Smallest margin</option>
          <option value="artist">Artist</option>
        </select>
      </label>
      <button id="reset" class="reset" type="button">Reset</button>
    </div>
  </header>

  <main>
    <div class="result-row">
      <div><strong id="visibleCount">0</strong> images shown</div>
      <div id="activeFilter"></div>
    </div>
    <div id="gallery" class="gallery"></div>
    <div id="empty" class="empty">No images match the current filters.</div>
    <p id="pageNote" class="page-note"></p>
  </main>

  <dialog id="detailDialog">
    <div class="dialog-head">
      <div class="dialog-title">
        <h2 id="detailTitle"></h2>
        <p id="detailSubtitle"></p>
      </div>
      <div class="dialog-actions">
        <button id="previous" class="icon-button" type="button" title="Previous image"
          aria-label="Previous image">&lt;</button>
        <button id="next" class="icon-button" type="button" title="Next image"
          aria-label="Next image">&gt;</button>
        <button id="close" class="icon-button" type="button" title="Close"
          aria-label="Close">X</button>
      </div>
    </div>
    <div class="dialog-body">
      <div id="detailMedia" class="detail-media"></div>
      <div id="detailContent" class="detail-content"></div>
    </div>
  </dialog>

  <script id="artsearchData" type="application/json">__ARTSEARCH_DATA__</script>
  <script>
    "use strict";

    const payload = JSON.parse(document.getElementById("artsearchData").textContent);
    const state = {
      decision: "",
      contentClass: "",
      route: "",
      search: "",
      sort: "newest",
      visibleItems: [],
      activeIndex: -1,
    };
    const elements = {
      stats: document.getElementById("stats"),
      subtitle: document.getElementById("subtitle"),
      search: document.getElementById("search"),
      classFilter: document.getElementById("classFilter"),
      routeFilter: document.getElementById("routeFilter"),
      sort: document.getElementById("sort"),
      reset: document.getElementById("reset"),
      visibleCount: document.getElementById("visibleCount"),
      activeFilter: document.getElementById("activeFilter"),
      gallery: document.getElementById("gallery"),
      empty: document.getElementById("empty"),
      pageNote: document.getElementById("pageNote"),
      dialog: document.getElementById("detailDialog"),
      detailTitle: document.getElementById("detailTitle"),
      detailSubtitle: document.getElementById("detailSubtitle"),
      detailMedia: document.getElementById("detailMedia"),
      detailContent: document.getElementById("detailContent"),
      previous: document.getElementById("previous"),
      next: document.getElementById("next"),
      close: document.getElementById("close"),
    };

    function label(value) {
      if (!value) return "Unknown";
      return value.replaceAll("_", " ").replace(/\b\w/g, (character) => character.toUpperCase());
    }

    function compactNumber(value) {
      return Number(value || 0).toLocaleString();
    }

    function score(value) {
      return typeof value === "number" ? value.toFixed(3) : "n/a";
    }

    function dateLabel(value) {
      if (!value) return "Unknown date";
      const parsed = new Date(value);
      if (Number.isNaN(parsed.getTime())) return value;
      return parsed.toLocaleString();
    }

    function element(tag, className, text) {
      const node = document.createElement(tag);
      if (className) node.className = className;
      if (text !== undefined && text !== null) node.textContent = String(text);
      return node;
    }

    function appendText(parent, tag, className, text) {
      const node = element(tag, className, text);
      parent.append(node);
      return node;
    }

    function badge(decision) {
      return element("span", `badge decision-${decision}`, decision);
    }

    function imageNode(item, detail = false) {
      const wrapper = element("div", detail ? "" : "image-wrap");
      const source = detail ? item.detailImageSrc : item.imageSrc;
      if (!source) {
        wrapper.append(element("div", "image-missing", "Image URL unavailable"));
        wrapper.firstChild.style.display = "grid";
        return wrapper;
      }
      const image = document.createElement("img");
      image.src = source;
      image.alt = item.altText || `${item.authorLabel} Bluesky image`;
      image.loading = detail ? "eager" : "lazy";
      image.referrerPolicy = "no-referrer";
      const missing = element("div", "image-missing", "Image could not be loaded");
      image.addEventListener("error", () => {
        image.style.display = "none";
        missing.style.display = "grid";
      });
      wrapper.append(image, missing);
      if (!detail) wrapper.append(badge(item.decision));
      return wrapper;
    }

    function populateSelect(select, values) {
      [...new Set(values)].sort().forEach((value) => {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = label(value);
        select.append(option);
      });
    }

    function renderStats() {
      const entries = [
        ["", "All", payload.summary.displayedCount],
        ["accept", "Accept", payload.summary.decisions.accept || 0],
        ["review", "Review", payload.summary.decisions.review || 0],
        ["reject", "Reject", payload.summary.decisions.reject || 0],
        ["error", "Error", payload.summary.decisions.error || 0],
      ];
      elements.stats.replaceChildren();
      entries.forEach(([decision, title, count]) => {
        const button = element("button", "stat");
        button.type = "button";
        button.dataset.decision = decision;
        button.setAttribute("aria-pressed", String(state.decision === decision));
        button.append(element("strong", "", compactNumber(count)), element("span", "", title));
        button.addEventListener("click", () => {
          state.decision = decision;
          render();
        });
        elements.stats.append(button);
      });
    }

    function searchBlob(item) {
      return [
        item.authorHandle,
        item.authorDid,
        item.candidateId,
        item.postText,
        item.altText,
        item.predictedClass,
        item.route,
        ...(item.reasonCodes || []),
      ].filter(Boolean).join(" ").toLocaleLowerCase();
    }

    function filteredItems() {
      const query = state.search.trim().toLocaleLowerCase();
      const items = payload.items.filter((item) => {
        if (state.decision && item.decision !== state.decision) return false;
        if (state.contentClass && item.predictedClass !== state.contentClass) return false;
        if (state.route && item.route !== state.route) return false;
        return !query || searchBlob(item).includes(query);
      });
      const comparators = {
        newest: (left, right) =>
          String(right.createdAt || right.processedAt).localeCompare(
            String(left.createdAt || left.processedAt)
          ),
        "score-desc": (left, right) => right.finalScore - left.finalScore,
        "confidence-desc": (left, right) => right.confidence - left.confidence,
        "margin-asc": (left, right) => left.confidenceMargin - right.confidenceMargin,
        artist: (left, right) => left.authorLabel.localeCompare(right.authorLabel),
      };
      return items.sort(comparators[state.sort]);
    }

    function card(item, index) {
      const button = element("button", "art-card");
      button.type = "button";
      button.append(imageNode(item));

      const body = element("div", "card-body");
      const heading = element("div", "card-heading");
      heading.append(
        element("span", "author", item.authorLabel),
        element("span", "score", score(item.finalScore))
      );
      body.append(
        heading,
        element("div", "class-label", label(item.predictedClass)),
        element("div", "description", item.altText || item.postText || "No description")
      );
      button.append(body);
      button.addEventListener("click", () => openDetail(index));
      return button;
    }

    function render() {
      state.visibleItems = filteredItems();
      renderStats();
      elements.gallery.replaceChildren(
        ...state.visibleItems.map((item, index) => card(item, index))
      );
      elements.visibleCount.textContent = compactNumber(state.visibleItems.length);
      elements.empty.style.display = state.visibleItems.length ? "none" : "block";
      const filters = [
        state.decision && label(state.decision),
        state.contentClass && label(state.contentClass),
        state.route && label(state.route),
      ].filter(Boolean);
      elements.activeFilter.textContent = filters.length ? filters.join(" / ") : "All decisions";
    }

    function detailSection(title, text, muted = false) {
      const section = element("section", "detail-section");
      section.append(element("h3", "", title));
      section.append(element("div", muted ? "copy muted-copy" : "copy", text || "None"));
      return section;
    }

    function chipSection(title, values) {
      const section = element("section", "detail-section");
      section.append(element("h3", "", title));
      const row = element("div", "chip-row");
      (values.length ? values : ["None"]).forEach((value) => {
        row.append(element("span", "chip", value));
      });
      section.append(row);
      return section;
    }

    function scoreSection(classScores) {
      const section = element("section", "detail-section");
      section.append(element("h3", "", "Visual class scores"));
      const list = element("div", "score-list");
      classScores.forEach((entry) => {
        const row = element("div", "score-row");
        const track = element("div", "score-track");
        const fill = element("div", "score-fill");
        fill.style.width = `${Math.max(0, Math.min(100, entry.score * 100))}%`;
        track.append(fill);
        row.append(
          element("span", "score-name", label(entry.contentClass)),
          track,
          element("span", "score-value", score(entry.score))
        );
        list.append(row);
      });
      if (!classScores.length) list.append(element("div", "muted-copy", "No visual scores"));
      section.append(list);
      return section;
    }

    function factList(item) {
      const list = element("dl", "facts");
      const facts = [
        ["Route", label(item.route)],
        ["Confidence", score(item.confidence)],
        ["Margin", score(item.confidenceMargin)],
        ["Art utility", score(item.artUtilityScore)],
        ["Noise", score(item.noiseScore)],
        ["Dimensions", item.width && item.height ? `${item.width} x ${item.height}` : "Unknown"],
        ["Image position", item.imageIndex === null ? "Unknown" : String(item.imageIndex + 1)],
        ["Processed", dateLabel(item.processedAt)],
        ["Model", item.modelVersion],
        ["Prompt bank", item.promptVersion || "None"],
        ["Config", `${item.configVersion} / ${item.configHash || "no hash"}`],
        ["Candidate ID", item.candidateId],
      ];
      facts.forEach(([term, value]) => {
        const wrapper = element("div");
        wrapper.append(element("dt", "", term), element("dd", "", value));
        list.append(wrapper);
      });
      return list;
    }

    function externalLink(text, href) {
      const link = element("a", "", text);
      link.href = href;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      return link;
    }

    function openDetail(index) {
      const item = state.visibleItems[index];
      if (!item) return;
      state.activeIndex = index;
      elements.detailTitle.textContent = item.authorLabel;
      elements.detailSubtitle.textContent =
        `${label(item.predictedClass)} / ${dateLabel(item.createdAt)}`;
      elements.detailMedia.replaceChildren(imageNode(item, true));
      elements.detailContent.replaceChildren();

      const summary = element("div", "detail-summary");
      summary.append(badge(item.decision), element("span", "detail-score", score(item.finalScore)));
      elements.detailContent.append(summary);

      const links = element("div", "link-row");
      if (item.postUrl) links.append(externalLink("Open Bluesky post", item.postUrl));
      if (item.fullsizeUrl) links.append(externalLink("Open full-size image", item.fullsizeUrl));
      if (links.childElementCount) elements.detailContent.append(links);

      elements.detailContent.append(
        detailSection("Post text", item.postText, !item.postText),
        detailSection("Alt text", item.altText, !item.altText),
        chipSection(
          "Bluesky labels",
          [...(item.contentLabels || []), ...(item.authorLabels || [])]
        ),
        chipSection("Reason codes", item.reasonCodes || []),
        scoreSection(item.classScores || [])
      );

      if (item.textSignals) {
        elements.detailContent.append(
          chipSection(
            `Text signals / net ${score(item.textSignals.netScore)}`,
            [
              ...(item.textSignals.positiveTerms || []).map((term) => `+ ${term}`),
              ...(item.textSignals.negativeTerms || []).map((term) => `- ${term}`),
              ...(item.textSignals.patterns || []),
            ]
          )
        );
      }
      elements.detailContent.append(factList(item));
      if (item.errorType || item.errorMessage) {
        elements.detailContent.append(
          detailSection("Error", [item.errorType, item.errorMessage].filter(Boolean).join(": "))
        );
      }
      elements.previous.disabled = index <= 0;
      elements.next.disabled = index >= state.visibleItems.length - 1;
      if (!elements.dialog.open) elements.dialog.showModal();
    }

    elements.search.addEventListener("input", () => {
      state.search = elements.search.value;
      render();
    });
    elements.classFilter.addEventListener("change", () => {
      state.contentClass = elements.classFilter.value;
      render();
    });
    elements.routeFilter.addEventListener("change", () => {
      state.route = elements.routeFilter.value;
      render();
    });
    elements.sort.addEventListener("change", () => {
      state.sort = elements.sort.value;
      render();
    });
    elements.reset.addEventListener("click", () => {
      state.decision = "";
      state.contentClass = "";
      state.route = "";
      state.search = "";
      state.sort = "newest";
      elements.search.value = "";
      elements.classFilter.value = "";
      elements.routeFilter.value = "";
      elements.sort.value = "newest";
      render();
    });
    elements.close.addEventListener("click", () => elements.dialog.close());
    elements.previous.addEventListener("click", () => openDetail(state.activeIndex - 1));
    elements.next.addEventListener("click", () => openDetail(state.activeIndex + 1));
    elements.dialog.addEventListener("click", (event) => {
      if (event.target === elements.dialog) elements.dialog.close();
    });
    document.addEventListener("keydown", (event) => {
      if (!elements.dialog.open) return;
      if (event.key === "ArrowLeft") openDetail(state.activeIndex - 1);
      if (event.key === "ArrowRight") openDetail(state.activeIndex + 1);
    });

    populateSelect(elements.classFilter, payload.items.map((item) => item.predictedClass));
    populateSelect(elements.routeFilter, payload.items.map((item) => item.route));
    elements.subtitle.textContent =
      `${compactNumber(payload.summary.displayedCount)} latest decisions from ` +
      `${compactNumber(payload.summary.candidateCount)} candidates`;
    const notes = [
      "Images load directly from Bluesky's CDN.",
      `${payload.summary.undecidedCandidateCount} candidates have no current decision.`,
      `${payload.summary.staleDecisionCount} stale decision joins were excluded.`,
    ];
    elements.pageNote.textContent = notes.join(" ");
    render();
  </script>
</body>
</html>
"""
