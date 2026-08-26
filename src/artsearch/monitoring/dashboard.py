from __future__ import annotations

import json


def render_monitoring_html(*, review_token: str = "") -> str:
    return _TEMPLATE.replace("__ARTSEARCH_REVIEW_TOKEN__", json.dumps(review_token))


_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ArtSearch Model Operations</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f4f6f3;
      --panel: #ffffff;
      --panel-soft: #f8faf7;
      --line: #d6ddd5;
      --line-strong: #aab5aa;
      --text: #19221b;
      --muted: #5f6d62;
      --green: #17663a;
      --green-soft: #e6f3ea;
      --blue: #245f9c;
      --blue-soft: #e8f0f8;
      --amber: #946200;
      --amber-soft: #fff3d5;
      --red: #a33831;
      --red-soft: #fae9e7;
      --gray-soft: #edf0ec;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      color: var(--text);
      background: var(--bg);
      font-family: system-ui, sans-serif;
      font-size: 14px;
    }
    button, input, select { font: inherit; }
    button { cursor: pointer; }
    h1, h2, h3, p { margin: 0; }
    .app-header {
      position: sticky;
      top: 0;
      z-index: 20;
      display: grid;
      grid-template-columns: minmax(250px, 1fr) auto auto;
      gap: 18px;
      align-items: center;
      min-height: 72px;
      padding: 11px 20px;
      border-bottom: 1px solid var(--line);
      background: rgba(255, 255, 255, 0.98);
    }
    .brand h1 { font-size: 18px; font-weight: 750; }
    .brand p { margin-top: 3px; color: var(--muted); font-size: 12px; }
    .tabs {
      display: flex;
      gap: 3px;
      padding: 3px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel-soft);
    }
    .tab {
      border: 0;
      border-radius: 6px;
      padding: 8px 11px;
      color: var(--muted);
      background: transparent;
    }
    .tab[aria-selected="true"] {
      color: var(--green);
      background: var(--panel);
      box-shadow: 0 1px 2px rgba(25, 34, 27, 0.12);
    }
    .header-actions { display: flex; align-items: center; gap: 8px; }
    .button {
      min-height: 34px;
      border: 1px solid var(--line-strong);
      border-radius: 6px;
      padding: 6px 10px;
      color: var(--text);
      background: var(--panel);
      text-decoration: none;
    }
    .button:hover { border-color: #6f7c70; }
    .button.primary { border-color: var(--green); color: #fff; background: var(--green); }
    .status-pill, .tag {
      display: inline-flex;
      align-items: center;
      gap: 5px;
      border-radius: 999px;
      padding: 4px 8px;
      font-size: 11px;
      font-weight: 700;
      text-transform: uppercase;
    }
    .critical { color: var(--red); background: var(--red-soft); }
    .warning, .attention { color: var(--amber); background: var(--amber-soft); }
    .healthy { color: var(--green); background: var(--green-soft); }
    .info, .unmeasured { color: var(--blue); background: var(--blue-soft); }
    main { width: min(1600px, 100%); margin: 0 auto; padding: 18px 20px 42px; }
    .tab-panel[hidden] { display: none; }
    .summary-band {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 14px;
      padding: 13px 15px;
      border: 1px solid var(--line);
      border-left-width: 4px;
      background: var(--panel);
    }
    .summary-band[data-level="critical"] { border-left-color: var(--red); }
    .summary-band[data-level="attention"] { border-left-color: var(--amber); }
    .summary-band[data-level="healthy"] { border-left-color: var(--green); }
    .summary-band h2 { font-size: 16px; }
    .summary-band p { margin-top: 3px; color: var(--muted); font-size: 12px; }
    .metrics-grid {
      display: grid;
      grid-template-columns: repeat(6, minmax(130px, 1fr));
      gap: 9px;
    }
    .metric {
      min-height: 91px;
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: 7px;
      background: var(--panel);
    }
    .metric-label { color: var(--muted); font-size: 10px; font-weight: 700; text-transform: uppercase; }
    .metric-value { margin-top: 7px; font-size: 23px; font-weight: 750; font-variant-numeric: tabular-nums; }
    .metric-note { margin-top: 3px; color: var(--muted); font-size: 11px; line-height: 1.35; }
    .section-heading {
      display: flex;
      align-items: end;
      justify-content: space-between;
      gap: 16px;
      margin: 22px 0 9px;
    }
    .section-heading h2 { font-size: 16px; }
    .section-heading p { color: var(--muted); font-size: 11px; }
    .table-wrap {
      overflow-x: auto;
      border: 1px solid var(--line);
      border-radius: 7px;
      background: var(--panel);
    }
    table { width: 100%; border-collapse: collapse; font-size: 12px; }
    th, td {
      padding: 9px 10px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      vertical-align: top;
      font-variant-numeric: tabular-nums;
    }
    th {
      color: var(--muted);
      background: var(--panel-soft);
      font-size: 10px;
      text-transform: uppercase;
      white-space: nowrap;
    }
    tr:last-child td { border-bottom: 0; }
    .alert-title { font-weight: 700; }
    .alert-detail { margin-top: 3px; color: var(--muted); line-height: 1.4; }
    .alert-action { margin-top: 4px; color: #35463a; line-height: 1.4; }
    .funnel {
      display: grid;
      grid-template-columns: repeat(6, minmax(120px, 1fr));
      gap: 1px;
      border: 1px solid var(--line);
      border-radius: 7px;
      overflow: hidden;
      background: var(--line);
    }
    .funnel-step { min-height: 96px; padding: 13px; background: var(--panel); }
    .funnel-step strong { display: block; margin-top: 6px; font-size: 22px; }
    .funnel-step span { color: var(--muted); font-size: 10px; text-transform: uppercase; }
    .funnel-step small { display: block; margin-top: 4px; color: var(--muted); }
    .split {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
    }
    .panel {
      padding: 13px;
      border: 1px solid var(--line);
      border-radius: 7px;
      background: var(--panel);
    }
    .panel h3 { margin-bottom: 11px; font-size: 13px; }
    .bar-row { display: grid; grid-template-columns: minmax(120px, 0.8fr) 2fr 56px; gap: 8px; align-items: center; margin: 7px 0; }
    .bar-label { overflow: hidden; color: #3d4940; font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }
    .bar-track { height: 8px; background: var(--gray-soft); }
    .bar-fill { height: 100%; min-width: 1px; background: var(--green); }
    .bar-count { color: var(--muted); font-size: 11px; text-align: right; }
    .model-grid { display: grid; grid-template-columns: repeat(3, minmax(240px, 1fr)); gap: 10px; }
    .model-card { padding: 13px; border: 1px solid var(--line); border-radius: 7px; background: var(--panel); }
    .model-card h3 { font-size: 13px; }
    dl { display: grid; grid-template-columns: 92px 1fr; gap: 6px 9px; margin: 11px 0 0; font-size: 11px; }
    dt { color: var(--muted); }
    dd { margin: 0; overflow-wrap: anywhere; }
    .empty {
      padding: 24px;
      border: 1px dashed var(--line-strong);
      color: var(--muted);
      background: var(--panel-soft);
      text-align: center;
    }
    .loading { padding: 60px 20px; color: var(--muted); text-align: center; }
    .mono { font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: 11px; }
    .nowrap { white-space: nowrap; }
    .review-switcher, .review-controls, .review-actions, .rating-actions {
      display: flex;
      flex-wrap: wrap;
      gap: 7px;
      align-items: center;
    }
    .review-switcher { margin: 14px 0; }
    .review-switcher .button[aria-pressed="true"] {
      border-color: var(--green);
      color: #fff;
      background: var(--green);
    }
    .review-controls {
      justify-content: space-between;
      margin-bottom: 10px;
      padding: 10px;
      border: 1px solid var(--line);
      background: var(--panel);
    }
    .review-controls label, .field label {
      display: grid;
      gap: 4px;
      color: var(--muted);
      font-size: 10px;
      font-weight: 700;
      text-transform: uppercase;
    }
    select, textarea {
      border: 1px solid var(--line-strong);
      border-radius: 5px;
      color: var(--text);
      background: var(--panel);
    }
    select { min-height: 34px; padding: 5px 28px 5px 8px; }
    textarea { width: 100%; min-height: 72px; padding: 8px; resize: vertical; }
    .review-layout {
      display: grid;
      grid-template-columns: minmax(320px, 1fr) minmax(380px, 1fr);
      gap: 12px;
      align-items: start;
    }
    .review-media-shell {
      position: relative;
      display: grid;
      place-items: center;
      min-height: 430px;
      overflow: hidden;
      border: 1px solid var(--line);
      background: #e9ede8;
    }
    .review-media-shell.compact { min-height: 250px; }
    .review-media-shell img {
      display: block;
      width: 100%;
      max-height: 72vh;
      object-fit: contain;
      background: #e9ede8;
    }
    .media-cover {
      display: grid;
      place-items: center;
      min-height: inherit;
      width: 100%;
      padding: 24px;
      color: var(--muted);
      text-align: center;
    }
    .media-cover .button { margin-top: 10px; }
    .decision-header {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 12px;
    }
    .decision-header h2 { font-size: 17px; }
    .decision-header p { margin-top: 4px; color: var(--muted); font-size: 11px; }
    .evidence-list { display: grid; grid-template-columns: 125px 1fr; gap: 7px 10px; font-size: 12px; }
    .evidence-list dt { color: var(--muted); }
    .evidence-list dd { min-width: 0; }
    .reason-list { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 8px; }
    .field-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 9px; }
    .review-form { margin-top: 12px; padding-top: 12px; border-top: 1px solid var(--line); }
    .review-form .field { margin-bottom: 9px; }
    .review-message { min-height: 18px; color: var(--muted); font-size: 11px; }
    .retrieval-query {
      display: grid;
      grid-template-columns: 210px 1fr;
      gap: 12px;
      margin-bottom: 12px;
      padding: 11px;
      border: 1px solid var(--line);
      background: var(--panel);
    }
    .retrieval-grid { display: grid; grid-template-columns: repeat(4, minmax(180px, 1fr)); gap: 10px; }
    .result-card { overflow: hidden; border: 1px solid var(--line); background: var(--panel); }
    .result-card[data-rated="yes"] { border-color: var(--green); }
    .result-card[data-rated="no"] { border-color: var(--red); }
    .result-body { padding: 10px; }
    .result-body h3 { font-size: 12px; }
    .result-body p { margin-top: 3px; color: var(--muted); font-size: 10px; }
    .result-score { margin: 8px 0; font-size: 19px; font-weight: 750; }
    .rating-actions .button { flex: 1; }
    .button.good[aria-pressed="true"] { border-color: var(--green); color: #fff; background: var(--green); }
    .button.bad[aria-pressed="true"] { border-color: var(--red); color: #fff; background: var(--red); }
    .text-evidence {
      max-height: 110px;
      overflow: auto;
      margin-top: 8px;
      padding: 8px;
      border-left: 3px solid var(--line);
      color: #3d4940;
      background: var(--panel-soft);
      font-size: 11px;
      white-space: pre-wrap;
    }
    @media (max-width: 1100px) {
      .app-header { grid-template-columns: 1fr auto; }
      .tabs { grid-column: 1 / -1; order: 3; overflow-x: auto; }
      .metrics-grid { grid-template-columns: repeat(3, 1fr); }
      .funnel { grid-template-columns: repeat(3, 1fr); }
      .model-grid { grid-template-columns: 1fr; }
      .retrieval-grid { grid-template-columns: repeat(2, minmax(180px, 1fr)); }
    }
    @media (max-width: 700px) {
      .app-header { grid-template-columns: minmax(0, 1fr); gap: 8px; padding: 10px 12px; }
      .brand { min-width: 0; }
      .brand h1 { font-size: 16px; }
      .header-actions { justify-content: flex-start; }
      .header-actions .button:not(.primary) { display: none; }
      .header-actions .button.primary { width: 36px; padding: 0; font-size: 0; }
      .header-actions .button.primary::after { content: "↻"; font-size: 19px; }
      main { padding: 13px 10px 30px; }
      .metrics-grid { grid-template-columns: repeat(2, 1fr); }
      .funnel { grid-template-columns: repeat(2, 1fr); }
      .split { grid-template-columns: 1fr; }
      .review-layout, .retrieval-query { grid-template-columns: 1fr; }
      .review-media-shell { min-height: 320px; }
      .field-grid, .retrieval-grid { grid-template-columns: 1fr; }
      .section-heading { align-items: flex-start; flex-direction: column; gap: 3px; }
      .bar-row { grid-template-columns: minmax(100px, 0.8fr) 2fr 42px; gap: 6px; }
    }
  </style>
</head>
<body>
  <header class="app-header">
    <div class="brand">
      <h1>ArtSearch Model Operations</h1>
      <p id="generated-at">Loading current evidence...</p>
    </div>
    <nav class="tabs" aria-label="Dashboard sections">
      <button class="tab" data-tab="overview" aria-selected="true">Overview</button>
      <button class="tab" data-tab="quality" aria-selected="false">Model quality</button>
      <button class="tab" data-tab="data" aria-selected="false">Data &amp; drift</button>
      <button class="tab" data-tab="operations" aria-selected="false">Operations</button>
      <button class="tab" data-tab="review" aria-selected="false">Review &amp; feedback</button>
    </nav>
    <div class="header-actions">
      <span id="header-status" class="status-pill info">Loading</span>
      <a class="button" href="/api/snapshot?download=1">Export JSON</a>
      <button id="refresh" class="button primary" aria-label="Refresh snapshot" title="Refresh snapshot">Refresh</button>
    </div>
  </header>
  <main id="app"><div class="loading">Reading canonical metrics and evaluation evidence...</div></main>
  <script>
    "use strict";
    let snapshot = null;
    const reviewToken = __ARTSEARCH_REVIEW_TOKEN__;
    const validTabs = new Set(["overview", "quality", "data", "operations", "review"]);
    let activeTab = validTabs.has(location.hash.slice(1)) ? location.hash.slice(1) : "overview";
    let refreshTimer = null;
    let reviewSummary = null;
    let filterReview = null;
    let retrievalReview = null;
    let reviewWorkspace = new URLSearchParams(location.search).get("workspace") === "retrieval" ? "retrieval" : "filter";
    let filterDecision = "all";
    let filterLabelStatus = "unlabeled";
    let filterOffset = 0;
    let retrievalMode = "ensemble";
    let reviewMessage = "";
    let reviewLoading = false;
    const revealedMedia = new Set();
    const retrievalRatings = new Map();

    const esc = value => String(value ?? "").replace(/[&<>"']/g, char => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;"
    })[char]);
    const number = value => value == null ? "Not measured" : Number(value).toLocaleString();
    const pct = value => value == null ? "Not measured" : `${(Number(value) * 100).toFixed(1)}%`;
    const decimal = (value, digits=3) => value == null ? "Not measured" : Number(value).toFixed(digits);
    const signed = (value, digits=3) => value == null ? "Not measured" : `${Number(value) >= 0 ? "+" : ""}${Number(value).toFixed(digits)}`;
    const ratio = value => value == null ? "Not measured" : `${Number(value).toFixed(2)}x`;
    const measure = value => {
      if (value == null) return "-";
      if (typeof value !== "number") return esc(value);
      if (Number.isInteger(value)) return number(value);
      return Math.abs(value) <= 1 ? value.toFixed(3) : value.toFixed(2);
    };
    const ms = value => value == null ? "Not measured" : `${Number(value).toFixed(0)} ms`;
    const bytes = value => {
      if (value == null) return "Not measured";
      const units = ["B", "KB", "MB", "GB", "TB"];
      let amount = Number(value), index = 0;
      while (amount >= 1024 && index < units.length - 1) { amount /= 1024; index += 1; }
      return `${amount.toFixed(index ? 1 : 0)} ${units[index]}`;
    };
    const short = value => value ? esc(String(value).slice(0, 12)) : "Not set";
    const metric = (label, value, note="") => `
      <div class="metric"><div class="metric-label">${esc(label)}</div>
      <div class="metric-value">${value}</div><div class="metric-note">${esc(note)}</div></div>`;
    const tag = (text, level="info") => `<span class="tag ${esc(level)}">${esc(text)}</span>`;
    const rows = (items, empty, render) => items?.length ? items.map(render).join("") :
      `<tr><td colspan="12" class="empty">${esc(empty)}</td></tr>`;
    const section = (title, note="") => `<div class="section-heading"><h2>${esc(title)}</h2><p>${esc(note)}</p></div>`;
    const formatDate = value => value ? new Date(value).toLocaleString() : "Not available";

    function bars(items, color="var(--green)") {
      if (!items?.length) return `<div class="empty">No distribution data available.</div>`;
      const max = Math.max(...items.map(item => Number(item.count || 0)), 1);
      return items.slice(0, 16).map(item => `
        <div class="bar-row"><div class="bar-label" title="${esc(item.label)}">${esc(item.label)}</div>
        <div class="bar-track"><div class="bar-fill" style="width:${100 * item.count / max}%;background:${color}"></div></div>
        <div class="bar-count">${number(item.count)}</div></div>`).join("");
    }

    function evidenceTag(measured) { return measured ? tag("Measured", "healthy") : tag("Not measured", "unmeasured"); }

    function renderOverview(s) {
      const level = s.monitoring.overall_level;
      const critical = s.alerts.filter(item => item.severity === "critical").length;
      const warnings = s.alerts.filter(item => item.severity === "warning").length;
      const evidence = s.monitoring.evidence_coverage;
      const search = s.product?.search || {};
      const feedback = s.product?.feedback || {};
      return `
        <section class="summary-band" data-level="${esc(level)}">
          <div><h2>${level === "healthy" ? "All configured checks pass" : "Maintenance attention is required"}</h2>
          <p>${critical} critical, ${warnings} warning. Missing evaluations are shown as unknown, never as passing.</p></div>
          ${tag(level, level)}
        </section>
        <div class="metrics-grid">
          ${metric("Searchable corpus", number(s.corpus.searchable), `${number(s.corpus.artists)} artists`)}
          ${metric("Searches executed", number(search.total_searches), `${number(search.searches_last_24h)} in the last 24h`)}
          ${metric("Search p95", ms(search.latency_p95_ms), `${number(s.product?.window_days)} day window`)}
          ${metric("Online P@10", pct(feedback.precision_at_10), `${number(feedback.fully_judged_queries)} complete queries`)}
          ${metric("Global embeddings", pct(s.embeddings.global_coverage), `${number(s.embeddings.current_complete)} current rows`)}
          ${metric("Latest decisions", number(s.filtering.latest_decisions), `${number(s.filtering.artists)} artists`)}
          ${metric("Filter p95", ms(s.filtering.latency_ms.p95), `target <= ${ms(s.monitoring.targets.filter_p95_latency_ms)}`)}
          ${metric("Serving eligible", number(s.corpus.serving_eligible), `${number(s.corpus.demo_eligible)} demo approved`)}
          ${metric("Evaluation evidence", `${Object.values(evidence).filter(Boolean).length}/3`, "filter, retrieval, ANN")}
        </div>
        ${section("Pipeline readiness", "Canonical counts at each durable handoff")}
        <div class="funnel">
          <div class="funnel-step"><span>Filter decisions</span><strong>${number(s.filtering.latest_decisions)}</strong><small>latest source versions</small></div>
          <div class="funnel-step"><span>Validated artwork</span><strong>${number(s.corpus.validated)}</strong><small>canonical SQLite</small></div>
          <div class="funnel-step"><span>Searchable rows</span><strong>${number(s.corpus.searchable)}</strong><small>processed, nonduplicate</small></div>
          <div class="funnel-step"><span>Current vectors</span><strong>${number(s.embeddings.current_complete)}</strong><small>CLIP + CSD style</small></div>
          <div class="funnel-step"><span>Safe + approved</span><strong>${number(s.corpus.serving_eligible)}</strong><small>explicit alpha policy</small></div>
          <div class="funnel-step"><span>Qdrant points</span><strong>${number(s.qdrant.remote_points)}</strong><small>${esc(s.qdrant.remote_status)}</small></div>
        </div>
        ${section("Action queue", "Highest-severity checks first")}
        <div class="table-wrap"><table><thead><tr><th>Severity</th><th>Area</th><th>Finding</th><th>Recommended action</th><th>Observed / target</th></tr></thead><tbody>
          ${rows(s.alerts, "No active alerts.", item => `<tr><td>${tag(item.severity, item.severity)}</td><td>${esc(item.area)}</td>
          <td><div class="alert-title">${esc(item.title)}</div><div class="alert-detail">${esc(item.detail)}</div></td>
          <td class="alert-action">${esc(item.action)}</td><td class="nowrap">${measure(item.observed)} / ${measure(item.target)}</td></tr>`)}
        </tbody></table></div>`;
    }

    function filterQuality(s) {
      const report = s.filter_evaluation;
      if (!report) return `<div class="empty">No human-labeled filter evaluation report is available. Automatic ACCEPT precision, recall, class F1, and false-acceptance rates are unknown.</div>`;
      const d = report.decision_metrics || {}, m = report.multiclass_metrics || {}, dataset = report.dataset || {};
      const interval = d.automatic_accept_precision_95ci || [];
      return `<div class="metrics-grid">
        ${metric("Matched labels", number(dataset.matched), `${number(dataset.artist_groups)} artist groups`)}
        ${metric("Accept precision", pct(d.automatic_accept_precision), `95% low ${pct(interval[0])}`)}
        ${metric("Accept recall", pct(d.automatic_accept_recall), "useful-art truth")}
        ${metric("Accept coverage", pct(d.automatic_accept_coverage), "automatic fraction")}
        ${metric("Macro F1", pct(m.macro_f1), "content classes")}
        ${metric("Top-1 class accuracy", pct(m.top1_accuracy), "human labels")}
      </div>`;
    }

    function retrievalQuality(s) {
      const modes = s.retrieval.per_mode || {};
      return `<div class="table-wrap"><table><thead><tr><th>Mode</th><th>Judgments</th><th>Queries</th><th>P@1</th><th>P@5</th><th>P@10</th><th>MRR</th><th>MAP</th><th>nDCG</th><th>Coverage</th></tr></thead><tbody>
        ${rows(Object.entries(modes), "No retrieval judgments have been imported.", ([name, item]) => `<tr><td>${esc(name)}</td><td>${number(item.judgments)}</td><td>${number(item.query_count)}</td>
        <td>${pct(item.at_k?.["1"]?.judged_precision)}</td><td>${pct(item.at_k?.["5"]?.judged_precision)}</td><td>${pct(item.at_k?.["10"]?.judged_precision)}</td>
        <td>${pct(item.mrr)}</td><td>${pct(item.map)}</td><td>${pct(item.ndcg)}</td><td>${pct(item.annotation_coverage)}</td></tr>`)}
      </tbody></table></div>`;
    }

    function annQuality(s) {
      const metrics = s.ann_evaluation?.metrics || [];
      return `<div class="table-wrap"><table><thead><tr><th>Named vector</th><th>Queries</th><th>K</th><th>Mean recall</th><th>Minimum recall</th><th>ANN p50</th><th>ANN p95</th><th>Exact p50</th><th>Exact p95</th></tr></thead><tbody>
        ${rows(metrics, "No exact-versus-HNSW evaluation report is available.", item => `<tr><td>${esc(item.vector_name)}</td><td>${number(item.query_count)}</td><td>${number(item.top_k)}</td>
        <td>${pct(item.mean_recall)}</td><td>${pct(item.minimum_recall)}</td><td>${ms(item.approximate_latency_p50_ms)}</td><td>${ms(item.approximate_latency_p95_ms)}</td>
        <td>${ms(item.exact_latency_p50_ms)}</td><td>${ms(item.exact_latency_p95_ms)}</td></tr>`)}
      </tbody></table></div>`;
    }

    function onlineFeedbackQuality(s) {
      const product = s.product || {};
      const feedback = product.feedback || {};
      const recall = product.recall_at_100 || {};
      return `<div class="metrics-grid">
        ${metric("Great", number(feedback.great), "graded relevance 2")}
        ${metric("Good", number(feedback.good), "graded relevance 1")}
        ${metric("Bad", number(feedback.bad), "miss / relevance 0")}
        ${metric("Judged hit rate", pct(feedback.judged_hit_rate), "partial labels, not P@10")}
        ${metric("Precision@10", pct(feedback.precision_at_10), `${number(feedback.fully_judged_queries)} fully judged searches`)}
        ${metric("nDCG@10", pct(feedback.ndcg_at_10), "Great ranks above Good")}
        ${metric("Recall@100", pct(recall.mean_recall), recall.measured ? `${number(recall.query_count)} exact comparisons` : "requires K=100 ANN report")}
        ${metric("Label coverage", pct(feedback.annotation_coverage_at_10), `${number(feedback.top_ten_judgments)} / ${number(feedback.top_ten_impressions)} impressions`)}
      </div>`;
    }

    function renderQuality(s) {
      const smoke = s.siglip_smoke;
      const evidence = s.monitoring.evidence_coverage;
      return `
        <div class="metrics-grid">
          ${metric("Filter quality", evidenceTag(evidence.filter_quality), "human-labeled decisions")}
          ${metric("Retrieval quality", evidenceTag(evidence.retrieval_quality), `${number(s.retrieval.dataset.latest_judgments)} judgments`)}
          ${metric("ANN quality", evidenceTag(evidence.ann_quality), "exact comparison")}
          ${metric("Online feedback", number(s.product?.feedback?.latest_judgments), `${number(s.product?.feedback?.fully_judged_queries)} complete searches`)}
          ${metric("SigLIP smoke", smoke ? tag(smoke.passed ? "Passed" : "Failed", smoke.passed ? "healthy" : "critical") : tag("Not found", "unmeasured"), smoke ? `${number(smoke.image_count)} images` : "no report")}
          ${metric("Corpus fingerprints", number(s.retrieval.dataset.corpus_fingerprints?.length), s.retrieval.dataset.mixed_corpora ? "mixed: invalid comparison" : "single corpus")}
          ${metric("Review queries", number(s.retrieval.dataset.query_count), `target >= ${number(s.monitoring.targets.minimum_retrieval_queries)}`)}
        </div>
        ${section("Artwork gate evaluation", "Precision on automatic ACCEPT is the primary corpus-quality metric")}
        ${filterQuality(s)}
        ${section("Retrieval relevance", "Judged results only; displayed-pool recall is not full-corpus recall")}
        ${retrievalQuality(s)}
        ${section("Live alpha feedback", "Great and Good are hits; Bad is a miss. P@10 and nDCG@10 require every displayed result to be judged")}
        ${onlineFeedbackQuality(s)}
        ${section("Qdrant ANN fidelity", "Approximate HNSW compared with exact search for each named vector")}
        ${annQuality(s)}
        ${section("Score and policy diagnostics")}
        <div class="split">
          <div class="panel"><h3>Filter decision distribution</h3>${bars(s.filtering.outcomes)}</div>
          <div class="panel"><h3>Current thresholds</h3><dl>
            <dt>Auto ACCEPT</dt><dd>${s.filtering.policy.automatic_accept_enabled ? "enabled" : "disabled"}</dd>
            <dt>Accept score</dt><dd>${esc(s.filtering.policy.accept_score)}</dd><dt>Reject score</dt><dd>${esc(s.filtering.policy.reject_score)}</dd>
            <dt>Min confidence</dt><dd>${esc(s.filtering.policy.minimum_confidence)}</dd><dt>Min margin</dt><dd>${esc(s.filtering.policy.minimum_margin)}</dd>
            <dt>Low confidence</dt><dd>${pct(s.filtering.low_confidence_rate)}</dd><dt>Low margin</dt><dd>${pct(s.filtering.low_margin_rate)}</dd>
          </dl></div>
        </div>`;
    }

    function renderData(s) {
      const e = s.embeddings;
      const drift = s.filtering.drift;
      const concentration = s.filtering.artist_concentration;
      return `
        <div class="metrics-grid">
          ${metric("CLIP current", number(e.clip_current), pct(e.searchable_denominator ? e.clip_current / e.searchable_denominator : null))}
          ${metric("CSD style current", number(e.csd_current), pct(e.searchable_denominator ? e.csd_current / e.searchable_denominator : null))}
          ${metric("Vector storage", bytes(e.clip_bytes + e.csd_bytes), "SQLite BLOBs")}
          ${metric("Safety unknown", number(s.corpus.safety_unknown), `${number(s.corpus.safe)} explicitly safe`)}
        </div>
        ${section("Decision cohorts", "Latest decision per candidate and source CID")}
        <div class="split">
          <div class="panel"><h3>Predicted content classes</h3>${bars(s.filtering.predicted_classes, "var(--blue)")}</div>
          <div class="panel"><h3>Corpus routes</h3>${bars(s.filtering.routes, "var(--amber)")}</div>
        </div>
        ${section("Recent-vs-baseline drift", `${number(drift.recent_count)} recent decisions (${number(drift.recent_days)}d) vs ${number(drift.baseline_count)} baseline (${number(drift.baseline_days)}d)`)}
        <div class="metrics-grid">
          ${metric("Drift evidence", evidenceTag(drift.measured), `minimum ${number(drift.minimum_cohort)} per cohort`)}
          ${metric("Class JS divergence", decimal(drift.class_js_divergence), `warn ${decimal(s.monitoring.targets.class_distribution_js_warning)}`)}
          ${metric("Decision JS divergence", decimal(drift.decision_js_divergence), "0 stable, 1 disjoint")}
          ${metric("Confidence mean shift", signed(drift.confidence_mean_delta), "recent minus baseline")}
          ${metric("Final-score mean shift", signed(drift.final_score_mean_delta), "recent minus baseline")}
          ${metric("Latency p95 ratio", ratio(drift.latency_p95_ratio), "recent / baseline")}
        </div>
        <div class="split" style="margin-top:12px">
          <div class="panel"><h3>Recent class cohort</h3>${bars(drift.recent_classes, "var(--blue)")}</div>
          <div class="panel"><h3>Baseline class cohort</h3>${bars(drift.baseline_classes, "var(--line-strong)")}</div>
        </div>
        ${section("Source diversity", "Anonymous aggregate concentration; artist identities are not exposed")}
        <div class="metrics-grid">
          ${metric("Artists represented", number(concentration.artists), `${number(concentration.labeled_decisions)} attributed decisions`)}
          ${metric("Largest artist share", pct(concentration.top_artist_share), `target <= ${pct(s.monitoring.targets.maximum_top_artist_share)}`)}
          ${metric("Top-five artist share", pct(concentration.top_five_share), "intake concentration")}
          ${metric("Artist HHI", decimal(concentration.herfindahl_index), "lower is more diverse")}
          ${metric("Corpus sources", number(s.corpus.by_source.length), "source-platform cohorts")}
          ${metric("Corpus artists", number(s.corpus.artists), "canonical SQLite")}
        </div>
        ${section("Latency and score distributions")}
        <div class="metrics-grid">
          ${metric("Latency mean", ms(s.filtering.latency_ms.mean), `${number(s.filtering.latency_ms.count)} samples`)}
          ${metric("Latency p50", ms(s.filtering.latency_ms.p50), "median")}
          ${metric("Latency p95", ms(s.filtering.latency_ms.p95), "tail")}
          ${metric("Confidence p50", s.filtering.confidence.p50?.toFixed(3) ?? "Not measured", `p95 ${s.filtering.confidence.p95?.toFixed(3) ?? "-"}`)}
          ${metric("Final score p50", s.filtering.final_score.p50?.toFixed(3) ?? "Not measured", `p95 ${s.filtering.final_score.p95?.toFixed(3) ?? "-"}`)}
          ${metric("Margin p50", s.filtering.margin.p50?.toFixed(3) ?? "Not measured", `p95 ${s.filtering.margin.p95?.toFixed(3) ?? "-"}`)}
        </div>
        ${section("Recent intake throughput", "Latest 30 observed decision dates")}
        <div class="table-wrap"><table><thead><tr><th>Date</th><th>Total</th><th>Accept</th><th>Review</th><th>Reject</th><th>Error</th><th>p95 latency</th></tr></thead><tbody>
          ${rows(s.operations.daily_intake, "No intake history available.", item => `<tr><td>${esc(item.date)}</td><td>${number(item.count)}</td><td>${number(item.accept)}</td><td>${number(item.review)}</td><td>${number(item.reject)}</td><td>${number(item.error)}</td><td>${ms(item.p95_duration_ms)}</td></tr>`)}
        </tbody></table></div>
        ${section("Embedding version cohorts", "Mixed rows indicate an incomplete model migration")}
        <div class="table-wrap"><table><thead><tr><th>CLIP model</th><th>CLIP revision</th><th>CSD model</th><th>CSD revision</th><th>Rows</th></tr></thead><tbody>
          ${rows(e.versions, "No embedding rows available.", item => `<tr><td>${esc(item.clip_model)}</td><td class="mono">${esc(item.clip_revision)}</td><td>${esc(item.csd_model)}</td><td class="mono">${esc(item.csd_revision)}</td><td>${number(item.count)}</td></tr>`)}
        </tbody></table></div>`;
    }

    function modelCard(name, item) {
      return `<div class="model-card"><h3>${esc(name)}</h3><dl>${Object.entries(item).map(([key, value]) => `<dt>${esc(key.replaceAll("_", " "))}</dt><dd class="${String(value).length > 24 ? "mono" : ""}">${esc(value)}</dd>`).join("")}</dl></div>`;
    }

    function renderOperations(s) {
      const scheduler = s.operations.scheduler || {};
      const sourceState = s.operations.source_state || {};
      const search = s.product?.search || {};
      const pipeline = s.operations.pipeline_success || {};
      const postgresPipeline = pipeline.postgres || {};
      return `
        <div class="metrics-grid">
          ${metric("Qdrant status", tag(s.qdrant.remote_status, s.qdrant.remote_status === "green" ? "healthy" : "info"), s.qdrant.endpoint || "no endpoint")}
          ${metric("Qdrant remote", number(s.qdrant.remote_points), s.qdrant.collection)}
          ${metric("Index ledger", number(s.qdrant.ledger_points), "SQLite checkpoints")}
          ${metric("Published originals", number(s.operations.published_objects), bytes(s.operations.published_bytes))}
          ${metric("Run errors", number(s.operations.recent_runs.reduce((sum, item) => sum + item.errors_count, 0)), `${number(s.operations.recent_runs.length)} recent runs`)}
          ${metric("Decision freshness", s.operations.decision_age_days == null ? "Not measured" : `${s.operations.decision_age_days.toFixed(1)} d`, formatDate(s.operations.latest_decision_at))}
        </div>
        ${section("Online search service", `Aggregate PostgreSQL telemetry over ${number(s.product?.window_days)} days`)}
        <div class="metrics-grid">
          ${metric("Telemetry", tag(s.product?.status || "disabled", s.product?.status === "online" ? "healthy" : "warning"), s.product?.error || "aggregate-only")}
          ${metric("Total searches", number(search.total_searches), `${number(search.searches_last_24h)} in 24h`)}
          ${metric("Search success", pct(search.success_rate), `${number(search.window_completed)} completed`)}
          ${metric("Search errors", pct(search.error_rate), `${number(search.window_errors)} failed`)}
          ${metric("Search p50", ms(search.latency_p50_ms), "total request latency")}
          ${metric("Search p95", ms(search.latency_p95_ms), `target <= ${ms(s.monitoring.targets.search_p95_latency_ms)}`)}
        </div>
        ${section("Pipeline success", "Rolling durable-stage outcomes; missing stages remain unmeasured")}
        <div class="metrics-grid">
          ${metric("Filter decisions", pct(pipeline.filter_decision_success_rate), `${number(pipeline.filter_decisions)} latest decisions`)}
          ${metric("Corpus routing", pct(pipeline.routing_success_rate), `${number(postgresPipeline.routes)} PostgreSQL routes`)}
          ${metric("Embedding build", pct(pipeline.embedding_coverage), "current CLIP + CSD")}
          ${metric("Object publication", pct(pipeline.object_publication_coverage), `${number(s.operations.published_objects)} objects`)}
          ${metric("Qdrant indexing", pct(pipeline.qdrant_index_coverage), `${number(s.qdrant.remote_points)} remote points`)}
          ${metric("Recent clean runs", pct(pipeline.recent_run_success_rate), `${number(postgresPipeline.finished_runs)} PostgreSQL runs`)}
        </div>
        ${section("Daily intake scheduler", "Windows trigger, Airflow run state, and incremental Bluesky source progress")}
        <div class="metrics-grid">
          ${metric("Daily target", number(scheduler.candidate_target), `${esc(scheduler.daily_time || "not scheduled")} ${esc(scheduler.timezone || "")}`)}
          ${metric("Roster", number(scheduler.actor_count), `${number(sourceState.tracked_actors)} actors checkpointed`)}
          ${metric("Completed scans", number(sourceState.completed_actors), formatDate(sourceState.latest_attempt_at))}
          ${metric("Backlogged scans", number(sourceState.backlogged_actors), `${number(sourceState.resumable_cursors)} resumable cursors`)}
          ${metric("Actor errors", number(sourceState.actors_with_errors), "latest persistent source state")}
          ${metric("Last scheduler event", tag(scheduler.last_status || "not_run", scheduler.last_status === "success" ? "healthy" : scheduler.last_status === "failed" ? "critical" : "info"), formatDate(scheduler.last_event_at))}
        </div>
        ${section("Model and index lineage", "Exact revisions used to determine compatibility")}
        <div class="model-grid">
          ${modelCard("CLIP subject", s.lineage.clip)}${modelCard("CSD style", s.lineage.csd)}${modelCard("SigLIP artwork gate", s.lineage.siglip)}
        </div>
        ${section("Recent pipeline runs")}
        <div class="table-wrap"><table><thead><tr><th>Run</th><th>Script</th><th>Started</th><th>Finished</th><th>Processed</th><th>Skipped</th><th>Errors</th></tr></thead><tbody>
          ${rows(s.operations.recent_runs, "No run records available.", item => `<tr><td>${number(item.run_id)}</td><td>${esc(item.script_name)}</td><td>${esc(item.started_at)}</td><td>${esc(item.finished_at || "running")}</td><td>${number(item.images_processed)}</td><td>${number(item.images_skipped)}</td><td>${number(item.errors_count)}</td></tr>`)}
        </tbody></table></div>
        ${section("Monitoring targets", "Initial alpha guardrails; tune only from representative evidence")}
        <div class="table-wrap"><table><thead><tr><th>Target</th><th>Configured value</th></tr></thead><tbody>
          ${Object.entries(s.monitoring.targets).map(([key, value]) => `<tr><td>${esc(key.replaceAll("_", " "))}</td><td>${esc(value)}</td></tr>`).join("")}
        </tbody></table></div>
        ${section("Evidence sources", "Paths are project-relative; credentials and image paths are excluded")}
        <div class="table-wrap"><table><thead><tr><th>Source</th><th>Artifacts</th></tr></thead><tbody>
          ${Object.entries(s.sources).map(([key, value]) => `<tr><td>${esc(key)}</td><td class="mono">${esc(Array.isArray(value) ? value.join(", ") || "not found" : value || "not found")}</td></tr>`).join("")}
        </tbody></table></div>`;
    }

    const humanize = value => String(value || "").replaceAll("_", " ").replace(/\b\w/g, char => char.toUpperCase());
    const reviewMediaUrl = (kind, id) => `/api/review/media/${kind}?${new URLSearchParams({
      token: reviewToken, [kind === "filter" ? "candidate_id" : "artwork_id"]: id
    })}`;
    const reviewSelect = (name, values, selected, placeholder) => `
      <select name="${esc(name)}" required>
        <option value="">${esc(placeholder)}</option>
        ${(values || []).map(value => `<option value="${esc(value)}" ${value === selected ? "selected" : ""}>${esc(humanize(value))}</option>`).join("")}
      </select>`;

    function concealedMedia(kind, id, key, compact=false) {
      const revealed = reviewSummary?.conceal_media === false || revealedMedia.has(key);
      return `<div class="review-media-shell ${compact ? "compact" : ""}">
        ${revealed ? `<img src="${esc(reviewMediaUrl(kind, id))}" alt="Review image">` : `
          <div class="media-cover"><div><strong>Media concealed</strong><br>
          Safety has not necessarily been reviewed for this item.<br>
          <button class="button" data-review-action="reveal" data-media-key="${esc(key)}">Reveal image</button></div></div>`}
      </div>`;
    }

    function renderFilterWorkspace() {
      const summary = reviewSummary?.filter || {};
      const controls = `<div class="review-controls"><div class="review-actions">
        <label>Decision<select id="filter-decision">
          ${["all", "accept", "review", "reject", "error"].map(value => `<option value="${value}" ${value === filterDecision ? "selected" : ""}>${humanize(value)}</option>`).join("")}
        </select></label>
        <label>Labels<select id="filter-label-status">
          ${[["unlabeled", "Needs review"], ["labeled", "Already labeled"], ["all", "All"]].map(([value, label]) => `<option value="${value}" ${value === filterLabelStatus ? "selected" : ""}>${label}</option>`).join("")}
        </select></label>
      </div><div class="review-actions">
        <button class="button" data-review-action="filter-prev" ${!filterReview?.offset ? "disabled" : ""}>Previous</button>
        <span class="mono">${filterReview?.total ? `${number(filterReview.offset + 1)} / ${number(filterReview.total)}` : "0 / 0"}</span>
        <button class="button" data-review-action="filter-next" ${!filterReview?.total || filterReview.offset >= filterReview.total - 1 ? "disabled" : ""}>Next</button>
      </div></div>`;
      if (reviewLoading && !filterReview) return controls + `<div class="loading">Loading decision evidence...</div>`;
      const item = filterReview?.item;
      if (!item) return controls + `<div class="empty">No decisions match this queue. ${number(summary.pending || 0)} decisions remain unlabeled overall.</div>`;
      const previous = item.previous_label || {};
      const topScores = (item.class_scores || []).slice(0, 8);
      const positiveTerms = item.text_scores?.matched_positive_terms || [];
      const negativeTerms = item.text_scores?.matched_negative_terms || [];
      const message = reviewMessage ? `<div class="review-message">${esc(reviewMessage)}</div>` : `<div class="review-message"></div>`;
      return `${controls}<div class="review-layout">
        <div>${concealedMedia("filter", item.candidate_id, `filter:${item.candidate_id}`)}</div>
        <div class="panel">
          <div class="decision-header"><div><h2>${esc(humanize(item.predicted_class))}</h2>
            <p>${esc(item.author_handle || "Unknown artist")} · ${esc(formatDate(item.created_at))}</p></div>
            ${tag(item.decision, item.decision === "accept" ? "healthy" : item.decision === "reject" ? "critical" : "warning")}
          </div>
          <dl class="evidence-list">
            <dt>Final score</dt><dd>${decimal(item.final_score)} · utility ${decimal(item.art_utility_score)} · noise ${decimal(item.noise_score)}</dd>
            <dt>Confidence</dt><dd>${decimal(item.confidence)} · margin ${decimal(item.confidence_margin)}</dd>
            <dt>Route</dt><dd>${esc(item.route)}</dd>
            <dt>Model</dt><dd class="mono">${esc(item.model_version)} @ ${esc(item.model_revision || "unpinned")}</dd>
            <dt>Candidate</dt><dd class="mono">${esc(item.candidate_id)}</dd>
          </dl>
          <div class="reason-list">${(item.reason_codes || []).map(code => tag(code, "info")).join("")}</div>
          ${item.post_text ? `<div class="text-evidence"><strong>Post</strong>\n${esc(item.post_text)}</div>` : ""}
          ${item.alt_text ? `<div class="text-evidence"><strong>Alt text</strong>\n${esc(item.alt_text)}</div>` : ""}
          ${section("Visual class scores", "SigLIP prompt-bank output")}
          <div class="table-wrap"><table><thead><tr><th>Class</th><th>Score</th></tr></thead><tbody>
            ${rows(topScores, "No visual scores.", score => `<tr><td>${esc(humanize(score.content_class))}</td><td>${decimal(score.score)}</td></tr>`)}
          </tbody></table></div>
          ${(positiveTerms.length || negativeTerms.length) ? `<p class="metric-note" style="margin-top:8px">Text signals: + ${esc(positiveTerms.join(", ") || "none")} · - ${esc(negativeTerms.join(", ") || "none")}</p>` : ""}
          <form id="filter-label-form" class="review-form">
            <input type="hidden" name="candidate_id" value="${esc(item.candidate_id)}">
            <div class="field-grid">
              <div class="field"><label>Actual content class${reviewSelect("content_class", reviewSummary?.content_labels, previous.content_class || "", "Choose class")}</label></div>
              <div class="field"><label>Artist's own work${reviewSelect("is_original_artist_work", reviewSummary?.original_work_labels, previous.is_original_artist_work || "", "Choose provenance")}</label></div>
              <div class="field"><label>Include in main corpus${reviewSelect("include_in_main_corpus", reviewSummary?.corpus_inclusion_labels, previous.include_in_main_corpus || "", "Choose action")}</label></div>
            </div>
            <div class="field"><label>Reviewer note<textarea name="annotator_note" placeholder="Optional boundary-case note">${esc(previous.annotator_note || "")}</textarea></label></div>
            <div class="review-actions">
              <button type="button" class="button" data-review-action="use-model-class" data-model-class="${esc(item.predicted_class)}">Use model class</button>
              <button type="submit" class="button primary">Save label &amp; continue</button>
              ${previous.content_class ? tag("Previously labeled", "healthy") : ""}
            </div>${message}
          </form>
        </div>
      </div>`;
    }

    function componentLine(item) {
      const values = [];
      if (item.fusion_score != null) values.push(`fusion ${decimal(item.fusion_score)} (#${item.fusion_rank})`);
      if (item.clip_score != null) values.push(`CLIP ${decimal(item.clip_score)} (#${item.clip_rank})`);
      if (item.style_score != null) values.push(`CSD ${decimal(item.style_score)} (#${item.style_rank})`);
      return values.join(" · ") || "single-vector ordering";
    }

    function renderRetrievalWorkspace() {
      const modeOptions = reviewSummary?.retrieval_modes || [];
      const controls = `<div class="review-controls"><div class="review-actions">
        <label>Retrieval task<select id="retrieval-mode">
          ${modeOptions.map(item => `<option value="${esc(item.value)}" ${item.value === retrievalMode ? "selected" : ""}>${esc(item.label)}</option>`).join("")}
        </select></label>
      </div><div class="review-actions">
        <button class="button" data-review-action="new-retrieval">New random query</button>
        ${retrievalReview ? `<button class="button" data-review-action="reveal" data-media-key="retrieval:${esc(retrievalReview.session_id)}">Reveal this set</button>` : ""}
      </div></div>`;
      if (reviewLoading && !retrievalReview) return controls + `<div class="loading">Running exact retrieval for a review sample...</div>`;
      if (!retrievalReview) return controls + `<div class="empty">Generate a random query to begin retrieval review.</div>`;
      const session = retrievalReview;
      const revealKey = `retrieval:${session.session_id}`;
      const resultCards = (session.results || []).map(item => {
        const ratingKey = `${session.session_id}:${item.artwork_id}`;
        const rating = retrievalRatings.get(ratingKey);
        return `<article class="result-card" data-rated="${rating === true ? "yes" : rating === false ? "no" : ""}">
          ${concealedMedia("artwork", item.artwork_id, revealKey, true)}
          <div class="result-body"><h3>#${number(item.rank)} · ${esc(item.artist_name)}</h3>
            <p class="mono">${esc(item.artwork_id)}</p><div class="result-score">${decimal(item.score)}</div>
            <p>${esc(componentLine(item))}</p>
            <div class="rating-actions" style="margin-top:9px">
              <button class="button good" aria-pressed="${rating === true}" data-review-action="rate" data-relevant="true" data-candidate="${esc(item.artwork_id)}">Useful match</button>
              <button class="button bad" aria-pressed="${rating === false}" data-review-action="rate" data-relevant="false" data-candidate="${esc(item.artwork_id)}">Not useful</button>
            </div>
          </div></article>`;
      }).join("");
      return `${controls}
        <section class="summary-band" data-level="healthy"><div><h2>${esc(session.mode.question)}</h2>
          <p>${esc(session.mode.label)} · ${esc(session.model_id)} @ ${esc(session.model_revision)}</p></div>
          ${tag(`${number(session.results.length)} results`, "info")}</section>
        <div class="retrieval-query">
          ${concealedMedia("artwork", session.query.artwork_id, revealKey, true)}
          <div><div class="metric-label">Query artwork</div><h2 style="margin-top:5px">${esc(session.query.artist_name)}</h2>
          <p class="mono" style="margin-top:5px">${esc(session.query.artwork_id)}</p>
          <p class="metric-note" style="margin-top:10px">Judge each result against this query for the task above. A rating is written immediately as an immutable event.</p></div>
        </div>
        <div class="retrieval-grid">${resultCards || `<div class="empty">No candidates were returned.</div>`}</div>
        <div class="review-message" style="margin-top:9px">${esc(reviewMessage)}</div>`;
    }

    function renderReview() {
      if (!reviewToken) return `<div class="empty">Human review is available only when this dashboard is bound to localhost.</div>`;
      const filter = reviewSummary?.filter || {};
      const retrieval = reviewSummary?.retrieval || {};
      return `
        <div class="metrics-grid">
          ${metric("Filter decisions", number(filter.decisions), `${number(filter.pending)} need a label`)}
          ${metric("Latest filter labels", number(filter.labeled), `${number(filter.history_events)} immutable events`)}
          ${metric("Retrieval judgments", number(retrieval.latest_judgments), `${number(retrieval.history_events)} immutable events`)}
          ${metric("Reviewer", esc(reviewSummary?.annotator || "local-reviewer"), "stored with every event")}
          ${metric("Label output", `<span class="mono">${esc(filter.output || "loading")}</span>`, "local and Git-ignored")}
          ${metric("Judgment output", `<span class="mono">${esc(retrieval.output || "loading")}</span>`, "local and Git-ignored")}
        </div>
        <div class="review-switcher">
          <button class="button" aria-pressed="${reviewWorkspace === "filter"}" data-review-action="workspace" data-workspace="filter">Artwork gate decisions</button>
          <button class="button" aria-pressed="${reviewWorkspace === "retrieval"}" data-review-action="workspace" data-workspace="retrieval">Retrieval feedback</button>
        </div>
        <div id="review-workspace">${reviewWorkspace === "filter" ? renderFilterWorkspace() : renderRetrievalWorkspace()}</div>`;
    }

    function paintReview() {
      const panel = document.querySelector('[data-panel="review"]');
      if (panel) panel.innerHTML = renderReview();
    }

    async function reviewGet(path, params={}) {
      const query = new URLSearchParams({...params, token: reviewToken});
      const response = await fetch(`${path}?${query}`, {cache: "no-store"});
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.message || payload.error || `HTTP ${response.status}`);
      return payload;
    }

    async function reviewPost(path, payload) {
      const response = await fetch(path, {
        method: "POST",
        headers: {"Content-Type": "application/json", "X-ArtSearch-Review-Token": reviewToken},
        body: JSON.stringify(payload)
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.message || body.error || `HTTP ${response.status}`);
      return body;
    }

    async function loadReviewSummary() {
      if (!reviewToken) return;
      reviewSummary = await reviewGet("/api/review/summary");
      paintReview();
    }

    async function loadFilterReview() {
      reviewLoading = true; filterReview = null; paintReview();
      try {
        filterReview = await reviewGet("/api/review/filter", {
          decision: filterDecision, status: filterLabelStatus, offset: filterOffset
        });
        filterOffset = filterReview.offset;
      } finally { reviewLoading = false; paintReview(); }
    }

    async function loadRetrievalReview() {
      reviewLoading = true; retrievalReview = null; reviewMessage = ""; paintReview();
      try { retrievalReview = await reviewGet("/api/review/retrieval", {mode: retrievalMode}); }
      finally { reviewLoading = false; paintReview(); }
    }

    async function ensureReview() {
      if (!reviewToken) return;
      try {
        if (!reviewSummary) await loadReviewSummary();
        if (reviewWorkspace === "filter" && !filterReview) await loadFilterReview();
        if (reviewWorkspace === "retrieval" && !retrievalReview) await loadRetrievalReview();
      } catch (error) {
        reviewLoading = false; reviewMessage = error.message;
        paintReview();
      }
    }

    function render() {
      if (!snapshot) return;
      document.getElementById("header-status").className = `status-pill ${snapshot.monitoring.overall_level}`;
      document.getElementById("header-status").textContent = snapshot.monitoring.overall_level;
      document.getElementById("generated-at").textContent = `v${snapshot.application_version} · refreshed ${formatDate(snapshot.generated_at)}`;
      const panels = {
        overview: renderOverview(snapshot), quality: renderQuality(snapshot),
        data: renderData(snapshot), operations: renderOperations(snapshot),
        review: renderReview()
      };
      document.getElementById("app").innerHTML = Object.entries(panels).map(([name, html]) =>
        `<section class="tab-panel" data-panel="${name}" ${name === activeTab ? "" : "hidden"}>${html}</section>`
      ).join("");
    }

    async function load(force=false) {
      const button = document.getElementById("refresh");
      button.disabled = true; button.textContent = "Refreshing...";
      try {
        const response = await fetch(`/api/snapshot${force ? "?refresh=1" : ""}`, {cache: "no-store"});
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        snapshot = await response.json();
        const preserveReview = activeTab === "review" && document.querySelector('[data-panel="review"]') && !force;
        if (!preserveReview) render();
        else {
          document.getElementById("header-status").className = `status-pill ${snapshot.monitoring.overall_level}`;
          document.getElementById("header-status").textContent = snapshot.monitoring.overall_level;
          document.getElementById("generated-at").textContent = `v${snapshot.application_version} · refreshed ${formatDate(snapshot.generated_at)}`;
        }
        clearTimeout(refreshTimer);
        refreshTimer = setTimeout(() => load(false), snapshot.monitoring.refresh_seconds * 1000);
      } catch (error) {
        document.getElementById("app").innerHTML = `<div class="summary-band" data-level="critical"><div><h2>Dashboard refresh failed</h2><p>${esc(error.message)}</p></div></div>`;
      } finally { button.disabled = false; button.textContent = "Refresh"; }
    }

    function activateTab(name, updateHash=true) {
      if (!validTabs.has(name)) return;
      activeTab = name;
      document.querySelectorAll(".tab").forEach(item => item.setAttribute("aria-selected", item.dataset.tab === activeTab ? "true" : "false"));
      document.querySelectorAll(".tab-panel").forEach(panel => panel.hidden = panel.dataset.panel !== activeTab);
      if (updateHash) history.replaceState(null, "", `#${activeTab}`);
      if (activeTab === "review") ensureReview();
    }
    document.querySelectorAll(".tab").forEach(button => button.addEventListener("click", () => activateTab(button.dataset.tab)));
    window.addEventListener("hashchange", () => activateTab(location.hash.slice(1), false));
    activateTab(activeTab, false);
    document.getElementById("refresh").addEventListener("click", () => load(true));
    document.addEventListener("change", event => {
      if (event.target.id === "filter-decision") {
        filterDecision = event.target.value; filterOffset = 0; reviewMessage = "";
        loadFilterReview().catch(error => { reviewMessage = error.message; paintReview(); });
      }
      if (event.target.id === "filter-label-status") {
        filterLabelStatus = event.target.value; filterOffset = 0; reviewMessage = "";
        loadFilterReview().catch(error => { reviewMessage = error.message; paintReview(); });
      }
      if (event.target.id === "retrieval-mode") {
        retrievalMode = event.target.value; retrievalReview = null; reviewMessage = "";
        loadRetrievalReview().catch(error => { reviewMessage = error.message; paintReview(); });
      }
    });
    document.addEventListener("submit", async event => {
      if (event.target.id !== "filter-label-form") return;
      event.preventDefault();
      const form = new FormData(event.target);
      reviewMessage = "Saving label..."; paintReview();
      try {
        const payload = Object.fromEntries(form.entries());
        const response = await reviewPost("/api/review/filter-label", payload);
        reviewSummary = response.summary;
        reviewMessage = response.appended ? "Label saved." : "This exact label was already saved.";
        filterReview = null;
        await loadFilterReview();
        load(true);
      } catch (error) { reviewMessage = error.message; paintReview(); }
    });
    document.addEventListener("click", async event => {
      const button = event.target.closest("[data-review-action]");
      if (!button) return;
      const action = button.dataset.reviewAction;
      try {
        if (action === "workspace") {
          reviewWorkspace = button.dataset.workspace; reviewMessage = ""; paintReview();
          await ensureReview();
        } else if (action === "reveal") {
          revealedMedia.add(button.dataset.mediaKey); paintReview();
        } else if (action === "filter-prev") {
          filterOffset = Math.max(0, filterOffset - 1); await loadFilterReview();
        } else if (action === "filter-next") {
          filterOffset += 1; await loadFilterReview();
        } else if (action === "use-model-class") {
          const select = document.querySelector('#filter-label-form [name="content_class"]');
          if (select) select.value = button.dataset.modelClass;
        } else if (action === "new-retrieval") {
          await loadRetrievalReview();
        } else if (action === "rate") {
          button.disabled = true;
          const relevant = button.dataset.relevant === "true";
          const response = await reviewPost("/api/review/retrieval-judgment", {
            session_id: retrievalReview.session_id,
            candidate_artwork_id: button.dataset.candidate,
            relevant
          });
          retrievalRatings.set(`${retrievalReview.session_id}:${button.dataset.candidate}`, relevant);
          reviewSummary = response.summary;
          reviewMessage = `Saved ${relevant ? "useful" : "not useful"} judgment for rank ${button.closest(".result-card")?.querySelector("h3")?.textContent || "result"}.`;
          paintReview();
          load(true);
        }
      } catch (error) { reviewMessage = error.message; paintReview(); }
    });
    load(false);
  </script>
</body>
</html>
"""
