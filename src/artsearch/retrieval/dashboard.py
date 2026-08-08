from __future__ import annotations

import json


def render_gallery_html(payload: dict) -> str:
    data_json = json.dumps(payload).replace("</", "<\\/")
    return _TEMPLATE.replace("__ARTSEARCH_DATA__", data_json)


_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ArtSearch Retrieval Workbench</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f3f5f2;
      --panel: #ffffff;
      --panel-soft: #f8faf7;
      --line: #d7ddd5;
      --line-strong: #aeb8ac;
      --text: #18201a;
      --muted: #5e6a61;
      --accent: #17663a;
      --accent-soft: #e6f3ea;
      --yes: #17663a;
      --yes-soft: #e5f4e9;
      --no: #a43a32;
      --no-soft: #fae9e7;
      --focus: #1557a0;
    }
    * {
      box-sizing: border-box;
    }
    body {
      margin: 0;
      min-height: 100vh;
      font-family: system-ui, sans-serif;
      color: var(--text);
      background: var(--bg);
    }
    button, input, select {
      font: inherit;
    }
    button {
      cursor: pointer;
    }
    img {
      display: block;
      max-width: 100%;
    }
    h1, h2, h3, p {
      margin: 0;
    }
    .app-header {
      position: sticky;
      top: 0;
      z-index: 20;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 20px;
      min-height: 68px;
      padding: 12px 20px;
      border-bottom: 1px solid var(--line);
      background: rgba(255, 255, 255, 0.97);
    }
    .brand h1 {
      font-size: 19px;
      font-weight: 700;
    }
    .brand p {
      margin-top: 3px;
      color: var(--muted);
      font-size: 12px;
    }
    .tabs {
      display: flex;
      gap: 4px;
      padding: 3px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel-soft);
    }
    .tab {
      border: 0;
      border-radius: 6px;
      padding: 8px 13px;
      color: var(--muted);
      background: transparent;
    }
    .tab[aria-selected="true"] {
      color: var(--accent);
      background: var(--panel);
      box-shadow: 0 1px 2px rgba(24, 32, 26, 0.12);
    }
    .tab-panel[hidden] {
      display: none;
    }
    .browse-layout {
      display: grid;
      grid-template-columns: minmax(260px, 32%) 1fr;
      min-height: calc(100vh - 69px);
    }
    .query-panel {
      overflow: auto;
      max-height: calc(100vh - 69px);
      padding: 16px;
      border-right: 1px solid var(--line);
      background: var(--panel-soft);
    }
    .artist {
      margin-bottom: 18px;
    }
    .artist h2 {
      margin-bottom: 8px;
      color: #3f4942;
      font-size: 13px;
    }
    .query-grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(86px, 1fr));
      gap: 8px;
    }
    .thumb {
      width: 100%;
      padding: 2px;
      border: 2px solid transparent;
      border-radius: 8px;
      background: transparent;
    }
    .thumb[aria-pressed="true"] {
      border-color: var(--focus);
    }
    .thumb img, .square-image {
      width: 100%;
      aspect-ratio: 1;
      object-fit: contain;
      border-radius: 5px;
      background: #7f857f;
    }
    .browse-results {
      overflow: auto;
      padding: 18px;
    }
    .selected-query {
      display: grid;
      grid-template-columns: minmax(160px, 260px) 1fr;
      gap: 18px;
      align-items: start;
      padding-bottom: 18px;
      border-bottom: 1px solid var(--line);
    }
    .selected-query img {
      width: 100%;
      max-height: 260px;
      object-fit: contain;
      border-radius: 6px;
      background: #7f857f;
    }
    .meta {
      display: grid;
      gap: 8px;
      color: #3e4941;
      font-size: 13px;
    }
    .result-grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(164px, 1fr));
      gap: 12px;
      margin-top: 18px;
    }
    .result-card {
      margin: 0;
      padding: 8px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
    }
    .result-card figcaption {
      margin-top: 7px;
      color: #3f4942;
      font-size: 12px;
      line-height: 1.4;
      overflow-wrap: anywhere;
    }
    .score, .subtle {
      color: var(--muted);
    }
    .retrieval-evidence {
      display: grid;
      gap: 2px;
      margin-top: 5px;
      padding-top: 5px;
      border-top: 1px solid var(--line);
      color: #465149;
      font-size: 11px;
      line-height: 1.35;
    }
    .retrieval-evidence .final-signal {
      color: var(--accent);
      font-weight: 700;
    }
    .evaluation-shell {
      width: min(1500px, 100%);
      margin: 0 auto;
      padding: 18px 20px 40px;
    }
    .eval-toolbar {
      display: flex;
      flex-wrap: wrap;
      align-items: end;
      justify-content: space-between;
      gap: 12px;
      padding-bottom: 16px;
      border-bottom: 1px solid var(--line);
    }
    .toolbar-group {
      display: flex;
      flex-wrap: wrap;
      align-items: end;
      gap: 8px;
    }
    .field {
      display: grid;
      gap: 4px;
      color: var(--muted);
      font-size: 11px;
      font-weight: 650;
      text-transform: uppercase;
    }
    .field input, .field select {
      min-height: 36px;
      border: 1px solid var(--line-strong);
      border-radius: 6px;
      padding: 6px 9px;
      color: var(--text);
      background: var(--panel);
      text-transform: none;
    }
    .button {
      min-height: 36px;
      border: 1px solid var(--line-strong);
      border-radius: 6px;
      padding: 7px 10px;
      color: var(--text);
      background: var(--panel);
    }
    .button:hover {
      border-color: #788579;
    }
    .button.primary {
      border-color: var(--accent);
      color: #ffffff;
      background: var(--accent);
    }
    .button:disabled {
      cursor: not-allowed;
      border-color: var(--line);
      color: #8b948d;
      background: #eef1ed;
    }
    .button.danger {
      color: var(--no);
    }
    .session-status {
      margin-top: 8px;
      color: var(--muted);
      font-size: 12px;
    }
    .session-status strong {
      color: var(--text);
    }
    .section-heading {
      display: flex;
      align-items: end;
      justify-content: space-between;
      gap: 16px;
      margin: 22px 0 10px;
    }
    .section-heading h2 {
      font-size: 17px;
    }
    .section-heading p {
      color: var(--muted);
      font-size: 12px;
    }
    .metrics-grid {
      display: grid;
      grid-template-columns: repeat(4, minmax(140px, 1fr));
      gap: 10px;
    }
    .metric {
      min-height: 86px;
      padding: 13px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
    }
    .metric-label {
      color: var(--muted);
      font-size: 11px;
      font-weight: 650;
      text-transform: uppercase;
    }
    .metric-value {
      margin-top: 7px;
      font-size: 24px;
      font-weight: 720;
      font-variant-numeric: tabular-nums;
    }
    .metric-note {
      margin-top: 4px;
      color: var(--muted);
      font-size: 11px;
    }
    .table-wrap {
      overflow-x: auto;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 12px;
    }
    th, td {
      padding: 9px 10px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      white-space: nowrap;
      font-variant-numeric: tabular-nums;
    }
    th {
      color: var(--muted);
      background: var(--panel-soft);
      font-size: 10px;
      text-transform: uppercase;
    }
    tbody tr:last-child td {
      border-bottom: 0;
    }
    .model-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
      gap: 10px;
    }
    .model-card {
      padding: 13px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
    }
    .model-card h3 {
      font-size: 14px;
    }
    .model-card dl {
      display: grid;
      grid-template-columns: 84px 1fr;
      gap: 5px 8px;
      margin: 10px 0 0;
      font-size: 11px;
    }
    .model-card dt {
      color: var(--muted);
    }
    .model-card dd {
      margin: 0;
      overflow-wrap: anywhere;
    }
    .review-head {
      display: grid;
      grid-template-columns: auto minmax(220px, 1fr) auto;
      align-items: center;
      gap: 10px;
      margin-top: 22px;
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
    }
    .mode-tabs {
      display: flex;
      flex-wrap: wrap;
      gap: 5px;
    }
    .mode-button {
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 7px 9px;
      color: var(--muted);
      background: var(--panel-soft);
    }
    .mode-button[aria-pressed="true"] {
      border-color: var(--accent);
      color: var(--accent);
      background: var(--accent-soft);
    }
    .question {
      color: #344039;
      font-size: 13px;
      text-align: center;
    }
    .progress {
      color: var(--muted);
      font-size: 12px;
      white-space: nowrap;
    }
    .eval-query {
      display: grid;
      grid-template-columns: 190px 1fr;
      gap: 16px;
      margin-top: 12px;
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
    }
    .eval-query img {
      width: 100%;
      max-height: 220px;
      object-fit: contain;
      border-radius: 5px;
      background: #7f857f;
    }
    .eval-results {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
      gap: 12px;
      margin-top: 12px;
    }
    .eval-card {
      padding: 9px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
    }
    .eval-card[data-judgment="yes"] {
      border-color: #83ac8f;
    }
    .eval-card[data-judgment="no"] {
      border-color: #d7aaa6;
    }
    .eval-card img {
      width: 100%;
      aspect-ratio: 1;
      object-fit: contain;
      border-radius: 5px;
      background: #7f857f;
    }
    .guess-line {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      margin-top: 8px;
      font-size: 12px;
    }
    .guess {
      border-radius: 999px;
      padding: 3px 7px;
      color: #245336;
      background: var(--accent-soft);
      font-size: 10px;
      font-weight: 750;
      letter-spacing: 0.03em;
    }
    .eval-meta {
      display: grid;
      gap: 4px;
      margin-top: 7px;
      color: #3e4941;
      font-size: 11px;
      overflow-wrap: anywhere;
    }
    .judgment-buttons {
      display: grid;
      grid-template-columns: 1fr 1fr auto;
      gap: 6px;
      margin-top: 9px;
    }
    .judgment {
      border: 1px solid var(--line-strong);
      border-radius: 6px;
      padding: 7px 5px;
      background: var(--panel);
    }
    .judgment.yes[aria-pressed="true"] {
      border-color: var(--yes);
      color: var(--yes);
      background: var(--yes-soft);
    }
    .judgment.no[aria-pressed="true"] {
      border-color: var(--no);
      color: var(--no);
      background: var(--no-soft);
    }
    .judgment.clear {
      color: var(--muted);
    }
    details {
      margin-top: 8px;
      border-top: 1px solid var(--line);
      padding-top: 7px;
      color: var(--muted);
      font-size: 11px;
    }
    summary {
      cursor: pointer;
      color: #455148;
      font-weight: 650;
    }
    .evidence {
      display: grid;
      gap: 5px;
      margin-top: 7px;
    }
    .evidence code {
      white-space: normal;
      overflow-wrap: anywhere;
    }
    .empty {
      padding: 30px;
      color: var(--muted);
      text-align: center;
    }
    @media (max-width: 900px) {
      .app-header {
        position: static;
        align-items: flex-start;
        flex-direction: column;
      }
      .browse-layout {
        grid-template-columns: 1fr;
      }
      .query-panel {
        max-height: none;
        border-right: 0;
        border-bottom: 1px solid var(--line);
      }
      .metrics-grid {
        grid-template-columns: repeat(2, 1fr);
      }
      .review-head {
        grid-template-columns: 1fr;
      }
      .question {
        text-align: left;
      }
    }
    @media (max-width: 560px) {
      .selected-query, .eval-query {
        grid-template-columns: 1fr;
      }
      .metrics-grid {
        grid-template-columns: 1fr;
      }
      .evaluation-shell {
        padding-inline: 12px;
      }
    }
  </style>
</head>
<body>
  <header class="app-header">
    <div class="brand">
      <h1>ArtSearch Retrieval Workbench</h1>
      <p id="headerSummary"></p>
    </div>
    <nav class="tabs" role="tablist" aria-label="Dashboard views">
      <button class="tab" id="browseTab" role="tab" aria-controls="browsePanel"
              aria-selected="true">Browse</button>
      <button class="tab" id="evaluationTab" role="tab" aria-controls="evaluationPanel"
              aria-selected="false">Evaluation</button>
    </nav>
  </header>

  <section id="browsePanel" class="tab-panel" role="tabpanel" aria-labelledby="browseTab">
    <div class="browse-layout">
      <aside id="queryPanel" class="query-panel"></aside>
      <section class="browse-results">
        <div id="selected"></div>
        <div id="results" class="result-grid"></div>
      </section>
    </div>
  </section>

  <section id="evaluationPanel" class="tab-panel" role="tabpanel"
           aria-labelledby="evaluationTab" hidden>
    <div class="evaluation-shell">
      <div class="eval-toolbar">
        <div class="toolbar-group">
          <label class="field">Annotator
            <input id="annotator" value="local-reviewer" maxlength="80">
          </label>
          <label class="field">Review session
            <select id="reviewSessionSelect"></select>
          </label>
          <label class="field">Query
            <select id="evalQuerySelect"></select>
          </label>
        </div>
        <div class="toolbar-group">
          <button id="importButton" class="button" type="button">Import JSONL</button>
          <input id="importInput" type="file" accept=".jsonl,.json" hidden>
          <button id="exportButton" class="button" type="button">Export judgments</button>
          <button id="nextSessionButton" class="button primary" type="button" disabled>
            Next review session
          </button>
          <button id="reportButton" class="button" type="button">Export metrics</button>
          <button id="clearAllButton" class="button danger" type="button">Clear session</button>
        </div>
      </div>
      <p id="sessionStatus" class="session-status"></p>

      <div class="section-heading">
        <div>
          <h2>Evaluation Stats</h2>
          <p>Observed metrics update immediately. Coverage shows how much of the pool is judged.</p>
        </div>
      </div>
      <div id="metricCards" class="metrics-grid"></div>
      <div id="modeMetrics" class="table-wrap" style="margin-top: 10px"></div>

      <div class="section-heading">
        <div>
          <h2>Model Decisions</h2>
          <p>Each retrieval result is a top-k MATCH guess; similarity scores are not probabilities.</p>
        </div>
      </div>
      <div id="modelGrid" class="model-grid"></div>
      <div id="funnelStats" class="table-wrap" style="margin-top: 10px"></div>
      <div id="siglipStats" class="table-wrap" style="margin-top: 10px"></div>

      <div id="reviewHead" class="review-head">
        <div id="modeTabs" class="mode-tabs"></div>
        <div id="taskQuestion" class="question"></div>
        <div id="reviewProgress" class="progress"></div>
      </div>
      <div id="evalQueryCard"></div>
      <div id="evalResults" class="eval-results"></div>

      <div class="section-heading">
        <div>
          <h2>Score Calibration</h2>
          <p>Judged relevance rate by similarity-score band for the selected signal.</p>
        </div>
      </div>
      <div id="scoreCalibration" class="table-wrap"></div>
    </div>
  </section>

  <script id="searchData" type="application/json">__ARTSEARCH_DATA__</script>
  <script>
    const payload = JSON.parse(document.getElementById("searchData").textContent);
    const browseData = payload.queries || [];
    const legacyEvalData = payload.evaluation?.queries || [];
    const reviewSessions = payload.evaluation?.sessions?.length
      ? payload.evaluation.sessions
      : (legacyEvalData.length ? [{
          id: `legacy-${payload.dashboardId}`,
          number: 1,
          queryCount: legacyEvalData.length,
          artistCount: new Set(legacyEvalData.map((entry) => entry.query.artistId)).size,
          queries: legacyEvalData,
          funnelStats: payload.evaluation?.funnelStats || {},
        }] : []);
    const storageKey = `artsearch-retrieval-judgments:${payload.corpusFingerprint}`;
    const sessionStateKey = `artsearch-review-session:${payload.dashboardId}`;
    let browseIndex = 0;
    let sessionState = loadSessionState();
    let reviewSessionIndex = Math.max(
      0,
      reviewSessions.findIndex((session) => session.id === sessionState.activeSessionId),
    );
    let evalData = currentReviewSession()?.queries || [];
    let evalQueryIndex = 0;
    let evalModeIndex = 0;
    let events = loadEvents();

    const byId = (id) => document.getElementById(id);
    const h = (value) => String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
    const pct = (value) => value == null ? "-" : `${(value * 100).toFixed(1)}%`;
    const score = (value) => Number.isFinite(Number(value)) ? Number(value).toFixed(4) : "-";
    const mean = (values) => values.length
      ? values.reduce((sum, value) => sum + value, 0) / values.length
      : null;
    const ratio = (a, b) => b ? a / b : null;

    byId("headerSummary").textContent =
      `${browseData.length} browse queries | ${reviewSessions.length} review sessions | `
      + `${payload.mode.label} | corpus ${payload.corpusFingerprint.slice(0, 12)}`;

    function activateTab(name) {
      const browse = name === "browse";
      byId("browseTab").setAttribute("aria-selected", String(browse));
      byId("evaluationTab").setAttribute("aria-selected", String(!browse));
      byId("browsePanel").hidden = !browse;
      byId("evaluationPanel").hidden = browse;
      history.replaceState(null, "", browse ? "#browse" : "#evaluation");
      if (!browse) renderEvaluation();
    }

    byId("browseTab").addEventListener("click", () => activateTab("browse"));
    byId("evaluationTab").addEventListener("click", () => activateTab("evaluation"));

    function groupByArtist(items) {
      return items.reduce((groups, entry, index) => {
        const name = entry.query.artistName;
        groups[name] ||= [];
        groups[name].push([entry, index]);
        return groups;
      }, {});
    }

    function renderBrowseQueries() {
      const groups = groupByArtist(browseData);
      byId("queryPanel").innerHTML = Object.entries(groups).map(([artist, entries]) => `
        <div class="artist">
          <h2>${h(artist)}</h2>
          <div class="query-grid">
            ${entries.map(([entry, index]) => `
              <button class="thumb" type="button" aria-pressed="${index === browseIndex}"
                      data-index="${index}" title="${h(entry.query.artworkId)}">
                <img src="${h(entry.query.imageSrc)}" alt="">
              </button>
            `).join("")}
          </div>
        </div>
      `).join("");
      byId("queryPanel").querySelectorAll(".thumb").forEach((button) => {
        button.addEventListener("click", () => selectBrowseQuery(Number(button.dataset.index)));
      });
    }

    function browseResultCard(item, rank) {
      return `
        <figure class="result-card">
          <img class="square-image" src="${h(item.imageSrc)}" alt="">
          <figcaption>
            <strong>${rank}. ${h(item.artistName)}</strong><br>
            ${h(item.artworkId)}<br>
            <span class="score">score ${score(item.score)}</span><br>
            <span class="subtle">${h(item.siglip?.predictedClass || "no SigLIP evidence")}</span>
            ${retrievalSummary(item)}
          </figcaption>
        </figure>
      `;
    }

    function selectBrowseQuery(index) {
      browseIndex = index;
      const entry = browseData[index];
      byId("selected").innerHTML = `
        <div class="selected-query">
          <img src="${h(entry.query.imageSrc)}" alt="">
          <div class="meta">
            <h2>Query</h2>
            <div><strong>Artist:</strong> ${h(entry.query.artistName)}</div>
            <div><strong>Artwork:</strong> ${h(entry.query.artworkId)}</div>
            <div><strong>Mode:</strong> ${h(entry.mode.label)}</div>
            <div><strong>Results:</strong> top ${entry.results.length}</div>
            ${siglipSummary(entry.query.siglip)}
          </div>
        </div>
      `;
      byId("results").innerHTML = entry.results.length
        ? entry.results.map((item, resultIndex) => browseResultCard(item, resultIndex + 1)).join("")
        : '<div class="empty">No results matched the current filters.</div>';
      renderBrowseQueries();
    }

    function siglipSummary(evidence) {
      if (!evidence) return '<div class="subtle">No linked SigLIP decision.</div>';
      const promoted = evidence.seedPromoted ? " | corpus-seed promotion" : "";
      return `
        <div><strong>SigLIP:</strong> ${h(evidence.predictedClass)}
          | utility ${score(evidence.artUtilityScore)}
          | confidence ${score(evidence.confidence)}${promoted}</div>
      `;
    }

    function retrievalSummary(item) {
      const retrieval = item.retrieval;
      if (!retrieval) return "";
      const components = retrieval.components || {};
      const patch = components.patch;
      const pooled = components.pooled;
      const clip = components.clip;
      const lines = [];
      if (patch) {
        lines.push(`<div class="${retrieval.orderingSignal === "patch" ? "final-signal" : ""}">
          Patch ${retrieval.orderingSignal === "patch" ? "final" : "signal"}:
          #${patch.rank ?? "-"} | ${score(patch.score)}
          ${retrieval.patchMatchTopN ? `| top-${retrieval.patchMatchTopN}` : ""}
        </div>`);
      }
      if (pooled) {
        lines.push(`<div>Pooled recall: #${pooled.rank ?? "-"} | ${score(pooled.score)}</div>`);
      }
      if (clip) {
        lines.push(`<div>CLIP semantic lens: #${clip.rank ?? "-"} | ${score(clip.score)}</div>`);
      }
      if (retrieval.shortlistSize != null) {
        lines.push(`<div class="subtle">Shortlist ${retrieval.shortlistSize} of ${retrieval.candidateCount}</div>`);
      }
      return `<div class="retrieval-evidence">${lines.join("")}</div>`;
    }

    function loadSessionState() {
      const fallback = {
        activeSessionId: reviewSessions[0]?.id || null,
        unlockedIndex: 0,
        exportedSignatures: {},
      };
      try {
        const parsed = JSON.parse(localStorage.getItem(sessionStateKey) || "null");
        if (!parsed || typeof parsed !== "object") return fallback;
        const unlockedIndex = Math.min(
          Math.max(Number(parsed.unlockedIndex) || 0, 0),
          Math.max(reviewSessions.length - 1, 0),
        );
        const activeIndex = reviewSessions.findIndex(
          (session) => session.id === parsed.activeSessionId,
        );
        return {
          activeSessionId: activeIndex >= 0 && activeIndex <= unlockedIndex
            ? parsed.activeSessionId
            : reviewSessions[unlockedIndex]?.id || fallback.activeSessionId,
          unlockedIndex,
          exportedSignatures: parsed.exportedSignatures
            && typeof parsed.exportedSignatures === "object"
            ? parsed.exportedSignatures
            : {},
        };
      } catch {
        return fallback;
      }
    }

    function persistSessionState() {
      try {
        localStorage.setItem(sessionStateKey, JSON.stringify(sessionState));
      } catch {
        // The dashboard still works for this page load without file-URL storage.
      }
    }

    function currentReviewSession() {
      return reviewSessions[reviewSessionIndex] || null;
    }

    function loadEvents() {
      try {
        const parsed = JSON.parse(localStorage.getItem(storageKey) || "[]");
        return Array.isArray(parsed) ? parsed : [];
      } catch {
        return [];
      }
    }

    function persistEvents() {
      try {
        localStorage.setItem(storageKey, JSON.stringify(events));
      } catch {
        // Export remains available when a browser disables file-URL storage.
      }
    }

    function judgmentKey(item) {
      return [
        item.corpus_fingerprint,
        item.retrieval_mode,
        item.query_artwork_id,
        item.candidate_artwork_id,
      ].join("|");
    }

    function latestJudgments() {
      const latest = new Map();
      events.forEach((event) => latest.set(judgmentKey(event), event));
      return [...latest.values()].filter(
        (event) => event.corpus_fingerprint === payload.corpusFingerprint,
      );
    }

    function sessionJudgmentKeys(session = currentReviewSession()) {
      const keys = new Set();
      (session?.queries || []).forEach((entry) => {
        entry.modes.forEach((modeEntry) => {
          modeEntry.results.forEach((candidate) => {
            keys.add([
              payload.corpusFingerprint,
              modeEntry.mode.value,
              entry.query.artworkId,
              candidate.artworkId,
            ].join("|"));
          });
        });
      });
      return keys;
    }

    function latestSessionJudgments(session = currentReviewSession()) {
      const keys = sessionJudgmentKeys(session);
      return latestJudgments().filter((item) => keys.has(judgmentKey(item)));
    }

    function currentSessionEvents() {
      const keys = sessionJudgmentKeys();
      return events.filter(
        (item) => item.corpus_fingerprint === payload.corpusFingerprint
          && keys.has(judgmentKey(item)),
      );
    }

    function currentSessionSignature() {
      return latestSessionJudgments()
        .map((item) => item.judgment_id)
        .sort()
        .join("|");
    }

    function currentSessionIsExported() {
      const session = currentReviewSession();
      const signature = currentSessionSignature();
      return Boolean(
        session
        && signature
        && sessionState.exportedSignatures[session.id] === signature,
      );
    }

    function currentJudgment(mode, queryId, candidateId) {
      const key = [
        payload.corpusFingerprint,
        mode,
        queryId,
        candidateId,
      ].join("|");
      return latestJudgments().find((item) => judgmentKey(item) === key) || null;
    }

    function modelForMode(mode) {
      return (payload.evaluation.modelInfo || []).find((item) => item.mode === mode) || {};
    }

    function recordJudgment(modeEntry, query, candidate, rank, relevant) {
      const model = modelForMode(modeEntry.mode.value);
      const id = globalThis.crypto?.randomUUID
        ? globalThis.crypto.randomUUID()
        : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
      events.push({
        schema_version: "1.0",
        judgment_id: id,
        dashboard_id: payload.dashboardId,
        review_session_id: currentReviewSession()?.id || null,
        review_seed: payload.evaluation?.sessionPlan?.seed || null,
        corpus_fingerprint: payload.corpusFingerprint,
        query_artwork_id: query.artworkId,
        candidate_artwork_id: candidate.artworkId,
        retrieval_mode: modeEntry.mode.value,
        task: modeEntry.mode.task,
        relevant,
        rank,
        result_count: modeEntry.results.length,
        score: Number(candidate.score),
        pooled_score: candidate.retrieval?.components?.pooled?.score ?? null,
        pooled_rank: candidate.retrieval?.components?.pooled?.rank ?? null,
        patch_score: candidate.retrieval?.components?.patch?.score ?? null,
        patch_rank: candidate.retrieval?.components?.patch?.rank ?? null,
        clip_score: candidate.retrieval?.components?.clip?.score ?? null,
        clip_rank: candidate.retrieval?.components?.clip?.rank ?? null,
        shortlist_size: candidate.retrieval?.shortlistSize ?? null,
        candidate_count: candidate.retrieval?.candidateCount ?? null,
        patch_match_top_n: candidate.retrieval?.patchMatchTopN ?? null,
        query_artist_id: query.artistId,
        candidate_artist_id: candidate.artistId,
        model_id: model.modelId || "unknown",
        model_revision: model.revision || "unknown",
        annotator: byId("annotator").value.trim() || "local-reviewer",
        note: "",
        labeled_at: new Date().toISOString(),
      });
      persistEvents();
      renderEvaluation();
    }

    function clearJudgment(mode, queryId, candidateId) {
      events = events.filter((item) => !(
        item.corpus_fingerprint === payload.corpusFingerprint
        && item.retrieval_mode === mode
        && item.query_artwork_id === queryId
        && item.candidate_artwork_id === candidateId
      ));
      persistEvents();
      renderEvaluation();
    }

    function modeEntries(modeValue) {
      return evalData.map((entry) => ({
        query: entry.query,
        mode: entry.modes.find((item) => item.mode.value === modeValue),
      })).filter((entry) => entry.mode);
    }

    function metricsForMode(modeValue) {
      const entries = modeEntries(modeValue);
      const expectedKeys = sessionJudgmentKeys();
      const judgments = latestJudgments().filter(
        (item) => item.retrieval_mode === modeValue
          && expectedKeys.has(judgmentKey(item)),
      );
      const judgmentMap = new Map(judgments.map((item) => [judgmentKey(item), item]));
      const expected = entries.reduce((sum, entry) => sum + entry.mode.results.length, 0);
      const relevant = judgments.filter((item) => item.relevant).length;
      const queryMetrics = entries.map((entry) => {
        const ranked = entry.mode.results.map((candidate, index) => {
          const key = [
            payload.corpusFingerprint,
            modeValue,
            entry.query.artworkId,
            candidate.artworkId,
          ].join("|");
          return {rank: index + 1, judgment: judgmentMap.get(key) || null};
        });
        const positives = ranked.filter((item) => item.judgment?.relevant);
        const first = positives.length ? Math.min(...positives.map((item) => item.rank)) : null;
        let seen = 0;
        const apSum = ranked.reduce((sum, item) => {
          if (!item.judgment?.relevant) return sum;
          seen += 1;
          return sum + seen / item.rank;
        }, 0);
        const ap = positives.length ? apSum / positives.length : 0;
        const dcg = positives.reduce((sum, item) => sum + 1 / Math.log2(item.rank + 1), 0);
        const ideal = positives.reduce(
          (sum, _, index) => sum + 1 / Math.log2(index + 2),
          0,
        );
        return {
          ranked,
          first,
          ap,
          ndcg: ideal ? dcg / ideal : 0,
          judgedCount: ranked.filter((item) => item.judgment).length,
          complete: ranked.every((item) => item.judgment),
        };
      });
      const atK = {};
      [1, 5, 10].forEach((k) => {
        const prefix = queryMetrics.flatMap((query) => query.ranked.filter((item) => item.rank <= k));
        const judged = prefix.filter((item) => item.judgment);
        const positive = judged.filter((item) => item.judgment.relevant).length;
        const expectedAtK = entries.reduce(
          (sum, entry) => sum + Math.min(k, entry.mode.results.length),
          0,
        );
        const judgedQueries = queryMetrics.filter(
          (query) => query.ranked.some((item) => item.rank <= k && item.judgment),
        );
        const hits = judgedQueries.filter(
          (query) => query.ranked.some((item) => item.rank <= k && item.judgment?.relevant),
        ).length;
        const poolRecall = queryMetrics
          .map((query) => {
            const allPositive = query.ranked.filter((item) => item.judgment?.relevant).length;
            const prefixPositive = query.ranked.filter(
              (item) => item.rank <= k && item.judgment?.relevant,
            ).length;
            return allPositive ? prefixPositive / allPositive : null;
          })
          .filter((value) => value != null);
        atK[k] = {
          coverage: ratio(judged.length, expectedAtK),
          precision: ratio(positive, judged.length),
          hitRate: ratio(hits, judgedQueries.length),
          poolRecall: mean(poolRecall),
        };
      });
      const assessedQueries = queryMetrics.filter((item) => item.judgedCount);
      return {
        modeValue,
        queryCount: entries.length,
        expected,
        judgments: judgments.length,
        relevant,
        notRelevant: judgments.length - relevant,
        coverage: ratio(judgments.length, expected),
        precision: ratio(relevant, judgments.length),
        completeQueries: queryMetrics.filter((item) => item.complete).length,
        mrr: mean(assessedQueries.map((item) => item.first ? 1 / item.first : 0)),
        map: mean(assessedQueries.map((item) => item.ap)),
        ndcg: mean(assessedQueries.map((item) => item.ndcg)),
        atK,
        scores: judgments,
      };
    }

    function allModeMetrics() {
      const modes = evalData[0]?.modes || [];
      return modes.map((entry) => ({
        info: entry.mode,
        metrics: metricsForMode(entry.mode.value),
      }));
    }

    function renderMetrics() {
      const rows = allModeMetrics();
      const totalExpected = rows.reduce((sum, row) => sum + row.metrics.expected, 0);
      const totalJudged = rows.reduce((sum, row) => sum + row.metrics.judgments, 0);
      const totalRelevant = rows.reduce((sum, row) => sum + row.metrics.relevant, 0);
      const complete = rows.reduce((sum, row) => sum + row.metrics.completeQueries, 0);
      const queryTasks = rows.reduce((sum, row) => sum + row.metrics.queryCount, 0);
      byId("metricCards").innerHTML = [
        ["Judgments", totalJudged, `${totalExpected} ranked guesses in pool`],
        ["Coverage", pct(ratio(totalJudged, totalExpected)), `${totalExpected - totalJudged} remaining`],
        ["Judged precision", pct(ratio(totalRelevant, totalJudged)), `${totalRelevant} useful matches`],
        ["Complete query-tasks", `${complete}/${queryTasks}`, "all displayed ranks judged"],
      ].map(([label, value, note]) => `
        <div class="metric">
          <div class="metric-label">${h(label)}</div>
          <div class="metric-value">${h(value)}</div>
          <div class="metric-note">${h(note)}</div>
        </div>
      `).join("");

      byId("modeMetrics").innerHTML = `
        <table>
          <thead><tr>
            <th>Signal / judgment task</th>
            <th>Coverage</th>
            <th>Judged precision</th>
            <th>P@1</th>
            <th>P@5</th>
            <th>P@10</th>
            <th>Hit@10</th>
            <th>Pool recall@10</th>
            <th>MRR</th>
            <th>MAP</th>
            <th>nDCG</th>
            <th>TP / FP</th>
          </tr></thead>
          <tbody>
            ${rows.map(({info, metrics}) => `
              <tr>
                <td><strong>${h(info.label)}</strong><br><span class="subtle">${h(info.task)}</span></td>
                <td>${pct(metrics.coverage)}</td>
                <td>${pct(metrics.precision)}</td>
                <td>${pct(metrics.atK[1].precision)}</td>
                <td>${pct(metrics.atK[5].precision)}</td>
                <td>${pct(metrics.atK[10].precision)}</td>
                <td>${pct(metrics.atK[10].hitRate)}</td>
                <td>${pct(metrics.atK[10].poolRecall)}</td>
                <td>${metrics.mrr?.toFixed(3) ?? "-"}</td>
                <td>${metrics.map?.toFixed(3) ?? "-"}</td>
                <td>${metrics.ndcg?.toFixed(3) ?? "-"}</td>
                <td>${metrics.relevant} / ${metrics.notRelevant}</td>
              </tr>
            `).join("")}
          </tbody>
        </table>
      `;
    }

    function renderModels() {
      byId("modelGrid").innerHTML = (payload.evaluation.modelInfo || []).map((model) => `
        <article class="model-card">
          <h3>${h(model.signal)}</h3>
          <dl>
            <dt>Stage</dt><dd>${h(model.stage)}</dd>
            <dt>Model</dt><dd>${h(model.modelId)}</dd>
            <dt>Revision</dt><dd><code>${h(model.revision || "unavailable")}</code></dd>
            <dt>Features</dt><dd>${h(model.representation)}</dd>
            <dt>Guess</dt><dd>${h(model.decision)}</dd>
          </dl>
        </article>
      `).join("");
      const funnel = currentReviewSession()?.funnelStats
        || payload.evaluation.funnelStats
        || {};
      byId("funnelStats").innerHTML = `
        <table>
          <thead><tr>
            <th>Two-stage funnel diagnostics</th>
            <th>Observed value</th>
          </tr></thead>
          <tbody>
            <tr><td>Queries compared with full-corpus patch baseline</td>
              <td>${funnel.queryCount ?? 0}</td></tr>
            <tr><td>Full-patch top-k retained in ensemble top-k</td>
              <td>${pct(funnel.meanPatchTopKAgreement)}</td></tr>
            <tr><td>Exact final order matches full-patch order</td>
              <td>${funnel.exactPatchOrderMatches ?? 0} / ${funnel.queryCount ?? 0}</td></tr>
            <tr><td>Mean shortlist / eligible candidates</td>
              <td>${score(funnel.meanShortlistSize)} / ${score(funnel.meanCandidateCount)}
                (${pct(funnel.meanShortlistFraction)})</td></tr>
            <tr><td>Mean absolute pooled-rank to final-rank movement</td>
              <td>${score(funnel.meanPooledToFinalMovement)}</td></tr>
            <tr><td>Mean absolute CLIP-rank to final-rank disagreement</td>
              <td>${score(funnel.meanClipToFinalMovement)}</td></tr>
          </tbody>
        </table>
      `;
      const stats = payload.evaluation.filterStats || {};
      const classes = Object.entries(stats.classCounts || {});
      byId("siglipStats").innerHTML = `
        <table>
          <thead><tr>
            <th>SigLIP corpus-gate summary</th>
            <th>Value</th>
          </tr></thead>
          <tbody>
            <tr><td>Linked active artworks</td><td>${stats.artworkCount ?? 0}</td></tr>
            <tr><td>Corpus-seed promotions</td><td>${stats.seedPromoted ?? 0}</td></tr>
            <tr><td>Persisted decisions</td><td>${h(JSON.stringify(stats.decisionCounts || {}))}</td></tr>
            <tr><td>Mean final score</td><td>${score(stats.meanFinalScore)}</td></tr>
            <tr><td>Mean art utility</td><td>${score(stats.meanArtUtility)}</td></tr>
            <tr><td>Mean class confidence</td><td>${score(stats.meanConfidence)}</td></tr>
            <tr><td>Mean confidence margin</td><td>${score(stats.meanMargin)}</td></tr>
            <tr><td>Predicted classes</td><td>
              ${classes.map(([name, count]) => `${h(name)}: ${count}`).join(" | ")}
            </td></tr>
          </tbody>
        </table>
      `;
    }

    function renderModeTabs(modeEntriesForQuery) {
      byId("modeTabs").innerHTML = modeEntriesForQuery.map((entry, index) => `
        <button class="mode-button" type="button" data-index="${index}"
                aria-pressed="${index === evalModeIndex}">
          ${h(entry.mode.label)}
        </button>
      `).join("");
      byId("modeTabs").querySelectorAll("button").forEach((button) => {
        button.addEventListener("click", () => {
          evalModeIndex = Number(button.dataset.index);
          renderEvaluation();
        });
      });
    }

    function siglipDetails(evidence) {
      if (!evidence) {
        return "<details><summary>SigLIP gate evidence</summary><div class='evidence'>Unavailable</div></details>";
      }
      const topScores = (evidence.classScores || []).map((item) => `
        <tr><td>${h(item.content_class)}</td><td>${score(item.score)}</td></tr>
      `).join("");
      return `
        <details>
          <summary>SigLIP gate evidence | ${h(evidence.predictedClass)}</summary>
          <div class="evidence">
            <div>Corpus decision: <strong>${h(evidence.decision)}</strong>
              ${evidence.seedPromoted ? "(promoted from review for pilot seeding)" : ""}</div>
            <div>Final ${score(evidence.finalScore)} | confidence ${score(evidence.confidence)}
              | margin ${score(evidence.confidenceMargin)}</div>
            <div>Art utility ${score(evidence.artUtilityScore)}
              | noise ${score(evidence.noiseScore)}</div>
            <div>Reasons: <code>${h((evidence.reasonCodes || []).join(", "))}</code></div>
            ${evidence.postText ? `<div>Post: ${h(evidence.postText)}</div>` : ""}
            ${evidence.altText ? `<div>Alt: ${h(evidence.altText)}</div>` : ""}
            <div class="table-wrap"><table>
              <thead><tr><th>Prompt class</th><th>Score</th></tr></thead>
              <tbody>${topScores}</tbody>
            </table></div>
          </div>
        </details>
      `;
    }

    function renderEvalQuery(entry) {
      byId("evalQueryCard").innerHTML = `
        <div class="eval-query">
          <img src="${h(entry.query.imageSrc)}" alt="">
          <div class="meta">
            <h2>Query | ${h(entry.query.artistName)}</h2>
            <div>${h(entry.query.artworkId)}</div>
            ${siglipSummary(entry.query.siglip)}
            ${siglipDetails(entry.query.siglip)}
          </div>
        </div>
      `;
    }

    function renderEvalResults(entry, modeEntry) {
      const mode = modeEntry.mode.value;
      const queryId = entry.query.artworkId;
      byId("evalResults").innerHTML = modeEntry.results.map((item, index) => {
        const rank = index + 1;
        const judgment = currentJudgment(mode, queryId, item.artworkId);
        const state = judgment ? (judgment.relevant ? "yes" : "no") : "unjudged";
        return `
          <article class="eval-card" data-judgment="${state}">
            <img src="${h(item.imageSrc)}" alt="">
            <div class="guess-line">
              <span class="guess">MATCH guess</span>
              <strong>#${rank} | ${score(item.score)}</strong>
            </div>
            <div class="eval-meta">
              <strong>${h(item.artistName)}</strong>
              <span>${h(item.artworkId)}</span>
              <span>${h(item.siglip?.predictedClass || "No SigLIP evidence")}</span>
              ${retrievalSummary(item)}
            </div>
            <div class="judgment-buttons">
              <button class="judgment yes" type="button" data-action="yes" data-rank="${rank}"
                      aria-pressed="${judgment?.relevant === true}">Yes</button>
              <button class="judgment no" type="button" data-action="no" data-rank="${rank}"
                      aria-pressed="${judgment?.relevant === false}">No</button>
              <button class="judgment clear" type="button" data-action="clear"
                      title="Clear judgment">&times;</button>
            </div>
            ${siglipDetails(item.siglip)}
          </article>
        `;
      }).join("");
      byId("evalResults").querySelectorAll(".eval-card").forEach((card, index) => {
        const item = modeEntry.results[index];
        card.querySelectorAll(".judgment").forEach((button) => {
          button.addEventListener("click", () => {
            if (button.dataset.action === "clear") {
              clearJudgment(mode, queryId, item.artworkId);
            } else {
              recordJudgment(
                modeEntry,
                entry.query,
                item,
                Number(button.dataset.rank),
                button.dataset.action === "yes",
              );
            }
          });
        });
      });
    }

    function renderCalibration(modeValue) {
      const metrics = metricsForMode(modeValue);
      const bins = [
        [-1.0, -0.6],
        [-0.6, -0.2],
        [-0.2, 0.2],
        [0.2, 0.6],
        [0.6, 1.000001],
      ];
      byId("scoreCalibration").innerHTML = `
        <table>
          <thead><tr><th>Score band</th><th>Judgments</th><th>Relevant</th><th>Relevance rate</th></tr></thead>
          <tbody>
            ${bins.map(([lower, upper], index) => {
              const items = metrics.scores.filter(
                (item) => item.score >= lower && (item.score < upper || index === bins.length - 1),
              );
              const relevant = items.filter((item) => item.relevant).length;
              return `<tr>
                <td>${lower.toFixed(1)} to ${Math.min(upper, 1).toFixed(1)}</td>
                <td>${items.length}</td>
                <td>${relevant}</td>
                <td>${pct(ratio(relevant, items.length))}</td>
              </tr>`;
            }).join("")}
          </tbody>
        </table>
      `;
    }

    function renderEvaluation() {
      renderMetrics();
      renderModels();
      renderSessionControls();
      if (!evalData.length) {
        byId("evalResults").innerHTML = '<div class="empty">No evaluation queries available.</div>';
        return;
      }
      evalQueryIndex = Math.min(evalQueryIndex, evalData.length - 1);
      const entry = evalData[evalQueryIndex];
      evalModeIndex = Math.min(evalModeIndex, entry.modes.length - 1);
      const modeEntry = entry.modes[evalModeIndex];
      byId("evalQuerySelect").value = String(evalQueryIndex);
      renderModeTabs(entry.modes);
      byId("taskQuestion").textContent = modeEntry.mode.question;
      const judged = modeEntry.results.filter(
        (item) => currentJudgment(modeEntry.mode.value, entry.query.artworkId, item.artworkId),
      ).length;
      byId("reviewProgress").textContent = `${judged}/${modeEntry.results.length} judged`;
      renderEvalQuery(entry);
      renderEvalResults(entry, modeEntry);
      renderCalibration(modeEntry.mode.value);
    }

    function refreshEvaluationQueryOptions() {
      byId("evalQuerySelect").innerHTML = evalData.map((entry, index) => `
        <option value="${index}">${index + 1}. ${h(entry.query.artistName)} | ${h(entry.query.artworkId)}</option>
      `).join("");
      byId("evalQuerySelect").value = String(evalQueryIndex);
    }

    function renderSessionControls() {
      const session = currentReviewSession();
      const latest = latestSessionJudgments();
      const expected = sessionJudgmentKeys().size;
      const exported = currentSessionIsExported();
      const finalSession = reviewSessionIndex >= reviewSessions.length - 1;
      byId("reviewSessionSelect").innerHTML = reviewSessions.map((item, index) => `
        <option value="${index}" ${index > sessionState.unlockedIndex ? "disabled" : ""}>
          Session ${item.number} | ${item.queryCount} queries
        </option>
      `).join("");
      byId("reviewSessionSelect").value = String(reviewSessionIndex);
      byId("exportButton").disabled = latest.length === 0;
      byId("clearAllButton").disabled = latest.length === 0;
      byId("nextSessionButton").disabled = !exported || finalSession;
      byId("nextSessionButton").textContent = finalSession
        ? "All sessions opened"
        : "Next review session";
      const exportState = latest.length === 0
        ? "No judgments in this session yet."
        : exported
          ? "Current judgments exported; the next session is unlocked."
          : "Current judgments have changes that still need export.";
      byId("sessionStatus").innerHTML = session
        ? `<strong>Session ${session.number} of ${reviewSessions.length}</strong>
          | ${session.queryCount} queries from ${session.artistCount} artists
          | ${latest.length}/${expected} guesses judged
          | seed <code>${h(payload.evaluation?.sessionPlan?.seed || "legacy")}</code>
          | ${h(exportState)}`
        : "No review sessions are embedded in this dashboard.";
    }

    function switchReviewSession(index) {
      if (index === reviewSessionIndex) return;
      if (index < 0 || index >= reviewSessions.length || index > sessionState.unlockedIndex) {
        byId("reviewSessionSelect").value = String(reviewSessionIndex);
        return;
      }
      if (latestSessionJudgments().length && !currentSessionIsExported()) {
        alert("Export the current session after its latest judgment changes before leaving it.");
        byId("reviewSessionSelect").value = String(reviewSessionIndex);
        return;
      }
      reviewSessionIndex = index;
      evalData = currentReviewSession()?.queries || [];
      evalQueryIndex = 0;
      evalModeIndex = 0;
      sessionState.activeSessionId = currentReviewSession()?.id || null;
      persistSessionState();
      refreshEvaluationQueryOptions();
      renderEvaluation();
      window.scrollTo({top: 0, behavior: "smooth"});
    }

    function initializeEvaluationControls() {
      refreshEvaluationQueryOptions();
      byId("evalQuerySelect").addEventListener("change", (event) => {
        evalQueryIndex = Number(event.target.value);
        renderEvaluation();
      });
      byId("reviewSessionSelect").addEventListener("change", (event) => {
        switchReviewSession(Number(event.target.value));
      });
    }

    function download(filename, contents, type) {
      const blob = new Blob([contents], {type});
      const link = document.createElement("a");
      link.href = URL.createObjectURL(blob);
      link.download = filename;
      link.click();
      setTimeout(() => URL.revokeObjectURL(link.href), 1000);
    }

    function evaluationReport() {
      const session = currentReviewSession();
      return {
        schema_version: "1.0",
        generated_at: new Date().toISOString(),
        dashboard_id: payload.dashboardId,
        corpus_fingerprint: payload.corpusFingerprint,
        review_session: {
          id: session?.id || null,
          number: session?.number || null,
          available_sessions: reviewSessions.length,
          seed: payload.evaluation?.sessionPlan?.seed || null,
        },
        dataset: {
          queries: evalData.length,
          judgment_events: currentSessionEvents().length,
          latest_judgments: latestSessionJudgments().length,
        },
        metric_note: payload.evaluation.metricSemantics,
        per_mode: Object.fromEntries(
          allModeMetrics().map(({info, metrics}) => [info.value, metrics]),
        ),
      };
    }

    byId("exportButton").addEventListener("click", () => {
      const session = currentReviewSession();
      const judgments = latestSessionJudgments();
      if (!session || !judgments.length) return;
      const lines = events
        .filter((item) => item.corpus_fingerprint === payload.corpusFingerprint)
        .map((item) => JSON.stringify(item))
        .join("\\n");
      const sessionNumber = String(session.number).padStart(2, "0");
      download(
        `retrieval-judgments-${payload.corpusFingerprint.slice(0, 12)}-through-session-${sessionNumber}.jsonl`,
        lines ? `${lines}\\n` : "",
        "application/x-ndjson",
      );
      sessionState.exportedSignatures[session.id] = currentSessionSignature();
      persistSessionState();
      renderSessionControls();
    });

    byId("nextSessionButton").addEventListener("click", () => {
      if (!currentSessionIsExported()) return;
      const nextIndex = reviewSessionIndex + 1;
      if (nextIndex >= reviewSessions.length) return;
      sessionState.unlockedIndex = Math.max(sessionState.unlockedIndex, nextIndex);
      persistSessionState();
      switchReviewSession(nextIndex);
    });

    byId("reportButton").addEventListener("click", () => {
      const sessionNumber = String(currentReviewSession()?.number || 1).padStart(2, "0");
      download(
        `retrieval-metrics-${payload.corpusFingerprint.slice(0, 12)}-session-${sessionNumber}.json`,
        `${JSON.stringify(evaluationReport(), null, 2)}\\n`,
        "application/json",
      );
    });

    byId("importButton").addEventListener("click", () => byId("importInput").click());
    byId("importInput").addEventListener("change", async (event) => {
      const file = event.target.files?.[0];
      if (!file) return;
      const parsed = (await file.text()).split(/\\r?\\n/).filter(Boolean).map((line) => JSON.parse(line));
      const current = parsed.filter(
        (item) => item.corpus_fingerprint === payload.corpusFingerprint,
      );
      const known = new Set(events.map((item) => item.judgment_id));
      events.push(...current.filter((item) => !known.has(item.judgment_id)));
      persistEvents();
      event.target.value = "";
      renderEvaluation();
      alert(`Imported ${current.length} judgment events for this corpus.`);
    });

    byId("clearAllButton").addEventListener("click", () => {
      const session = currentReviewSession();
      if (!session) return;
      if (!confirm("Clear locally stored judgments for this review session? Export first if needed.")) {
        return;
      }
      const keys = sessionJudgmentKeys(session);
      events = events.filter((item) => !(
        item.corpus_fingerprint === payload.corpusFingerprint
        && keys.has(judgmentKey(item))
      ));
      delete sessionState.exportedSignatures[session.id];
      persistEvents();
      persistSessionState();
      renderEvaluation();
    });

    initializeEvaluationControls();
    if (browseData.length) {
      selectBrowseQuery(0);
    } else {
      byId("selected").innerHTML = '<div class="empty">No embedded artworks are available.</div>';
    }
    if (location.hash === "#evaluation") {
      activateTab("evaluation");
    }
  </script>
</body>
</html>
"""
