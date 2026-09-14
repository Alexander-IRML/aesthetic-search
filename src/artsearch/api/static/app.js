"use strict";

const form = document.querySelector("#search-form");
const input = document.querySelector("#image-input");
const dropZone = document.querySelector("#drop-zone");
const preview = document.querySelector("#query-preview");
const fileMeta = document.querySelector("#file-meta");
const fileName = document.querySelector("#file-name");
const clearButton = document.querySelector("#clear-file");
const searchButton = document.querySelector("#search-button");
const emptyState = document.querySelector("#empty-state");
const errorState = document.querySelector("#error-state");
const errorTitle = document.querySelector("#error-title");
const errorMessage = document.querySelector("#error-message");
const resultsGrid = document.querySelector("#results-grid");
const resultCount = document.querySelector("#result-count");
const statusMessage = document.querySelector("#status-message");
const queryId = document.querySelector("#query-id");
const indexVersion = document.querySelector("#index-version");
const latency = document.querySelector("#latency");
const resultTemplate = document.querySelector("#result-template");
const assistantPanel = document.querySelector("#assistant-panel");
const assistantTranscript = document.querySelector("#assistant-transcript");
const assistantForm = document.querySelector("#assistant-form");
const assistantInput = document.querySelector("#assistant-input");
const assistantSend = document.querySelector("#assistant-send");
const assistantState = document.querySelector("#assistant-state");
const backendState = document.querySelector("#backend-state");
const backendStateLabel = document.querySelector("#backend-state-label");
const latencyPanel = document.querySelector("#latency-panel");
const requestStartState = document.querySelector("#request-start-state");
const retrievalWeightInputs = [...document.querySelectorAll(".weight-control input")];
const timingValues = new Map(
  [...document.querySelectorAll("[data-timing]")].map((element) => [
    element.dataset.timing,
    element,
  ]),
);

const acceptedTypes = new Set(["image/jpeg", "image/png", "image/webp"]);
const maximumBytes = 10_000_000;
const conservativeWarmWindowMs = 14 * 60 * 1000;
let selectedFile = null;
let previewUrl = null;
let warmingTimer = null;
let backendExpiryTimer = null;
let activeSearchController = null;
let activeResultsPayload = null;
let searchSequence = 0;
let assistantSequence = 0;

function browserSessionKey() {
  const storageKey = "artsearch.public-session.v1";
  let value = localStorage.getItem(storageKey);
  if (!value) {
    const randomValues = new Uint32Array(4);
    crypto.getRandomValues(randomValues);
    value = typeof crypto.randomUUID === "function"
      ? crypto.randomUUID()
      : `web-${Date.now()}-${[...randomValues].map((part) => part.toString(16)).join("")}`;
    localStorage.setItem(storageKey, value);
  }
  return value;
}

function setBackendState(state, detail = "") {
  const labels = {
    checking: "BACKEND CHECKING",
    cold: "BACKEND COLD",
    ready: "BACKEND WARM",
    unknown: "BACKEND UNKNOWN",
    warming: "BACKEND WARMING",
  };
  backendState.dataset.state = state;
  backendStateLabel.textContent = labels[state] || labels.unknown;
  backendState.title = detail;
}

function scheduleBackendExpiry() {
  clearTimeout(backendExpiryTimer);
  backendExpiryTimer = setTimeout(() => {
    setBackendState("cold", "No recent search response. The next request may cold start.");
  }, conservativeWarmWindowMs);
}

async function refreshRuntimeStatus() {
  setBackendState("checking", "Checking container state without starting the model.");
  try {
    const response = await fetch("/api/runtime-status", {
      cache: "no-store",
      headers: { Accept: "application/json" },
    });
    if (!response.ok) throw new Error("runtime status request failed");
    const payload = await response.json();
    const state = ["ready", "warming", "cold"].includes(payload.status)
      ? payload.status
      : "unknown";
    setBackendState(
      state,
      payload.warm
        ? "The model container is ready for a warm search."
        : "The next image search may need to start the model container.",
    );
    if (payload.warm) scheduleBackendExpiry();
  } catch {
    setBackendState("unknown", "Container state could not be verified.");
  }
}

async function registerSiteVisit() {
  try {
    const response = await fetch("/api/site-visit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_key: browserSessionKey() }),
      keepalive: true,
    });
    if (response.ok) {
      await refreshRuntimeStatus();
    }
  } catch {
    // Visitor telemetry is best effort and must never block the search UI.
  }
}

function syncRetrievalWeight(input) {
  const output = document.querySelector(`#${input.id}-value`);
  if (output) output.textContent = `${Number(input.value).toFixed(2)}x`;
}

function selectFile(file) {
  clearError();
  if (!file) {
    resetFile();
    return;
  }
  if (!acceptedTypes.has(file.type)) {
    showError("Unsupported image", "Choose a JPEG, PNG, or WebP image.");
    resetFile();
    return;
  }
  if (file.size > maximumBytes) {
    showError("Image too large", "Choose an image smaller than 10 MB.");
    resetFile();
    return;
  }
  cancelActiveSearch();
  resetAssistant();
  selectedFile = file;
  if (previewUrl) URL.revokeObjectURL(previewUrl);
  previewUrl = URL.createObjectURL(file);
  preview.src = previewUrl;
  preview.hidden = false;
  dropZone.classList.add("has-image");
  fileName.textContent = `${file.name} / ${formatBytes(file.size)}`;
  fileMeta.hidden = false;
  searchButton.disabled = false;
}

function resetFile() {
  cancelActiveSearch();
  resetAssistant();
  selectedFile = null;
  input.value = "";
  preview.hidden = true;
  preview.removeAttribute("src");
  dropZone.classList.remove("has-image");
  fileMeta.hidden = true;
  searchButton.disabled = true;
  if (previewUrl) URL.revokeObjectURL(previewUrl);
  previewUrl = null;
}

function showError(title, message) {
  errorTitle.textContent = title;
  errorMessage.textContent = message;
  errorState.hidden = false;
  emptyState.hidden = true;
  statusMessage.textContent = "ERROR";
  statusMessage.style.color = "var(--red)";
}

function clearError() {
  errorState.hidden = true;
  statusMessage.style.removeProperty("color");
}

function setLoading(active, artistName = null) {
  searchButton.disabled = active || !selectedFile;
  searchButton.textContent = active
    ? (artistName ? `Searching ${artistName}...` : "Starting image search...")
    : "Search similar artwork";
  resultsGrid.setAttribute("aria-busy", String(active));
  if (!active) {
    clearTimeout(warmingTimer);
    warmingTimer = null;
    setAssistantBusy(false);
    return;
  }
  clearError();
  latencyPanel.hidden = true;
  emptyState.hidden = true;
  resultsGrid.replaceChildren();
  for (let index = 0; index < 10; index += 1) {
    const card = document.createElement("article");
    card.className = "result-card skeleton-card";
    card.innerHTML = [
      '<div class="result-image-frame"></div>',
      '<div class="result-details">',
      '<span class="skeleton-line"></span>',
      '<span class="skeleton-line short"></span>',
      '<span class="skeleton-line"></span>',
      "</div>",
    ].join("");
    resultsGrid.append(card);
  }
  resultCount.textContent = "-- / 10";
  statusMessage.textContent = "SEARCHING";
  setAssistantBusy(true, artistName ? "ARTIST TOOL RUNNING" : "SEARCH RUNNING");
  warmingTimer = setTimeout(() => {
    searchButton.textContent = artistName
      ? `Waking model for ${artistName}...`
      : "Waking model and searching...";
    statusMessage.textContent = "MODEL WARMING";
    setBackendState("warming", "The current request is waiting for the model container.");
  }, 10_000);
}

function renderResults(payload, trace) {
  resultsGrid.replaceChildren();
  payload.results.forEach((result) => {
    const card = resultTemplate.content.firstElementChild.cloneNode(true);
    const image = card.querySelector(".result-image");
    image.src = result.image_url;
    image.alt = `Artwork by ${result.artist_name}`;
    image.addEventListener("error", () => card.classList.add("image-error"), { once: true });
    card.querySelector(".result-rank").textContent = String(result.rank).padStart(2, "0");
    card.querySelector(".artist-name").textContent = result.artist_name;
    card.querySelector(".content-class").textContent = result.content_class.replaceAll("_", " ");
    const source = card.querySelector(".source-link");
    if (result.source_url) {
      source.href = result.source_url;
      source.hidden = false;
    }
    setScore(
      card.querySelector(".subject-score"),
      card.querySelector(".subject-value"),
      result.match.subject_score,
    );
    setScore(
      card.querySelector(".style-score"),
      card.querySelector(".style-value"),
      result.match.style_score,
    );
    bindFeedback(card, payload.query_id, result.artwork_id);
    resultsGrid.append(card);
  });
  emptyState.hidden = payload.results.length > 0;
  resultCount.textContent = `${String(payload.result_count).padStart(2, "0")} / 10`;
  statusMessage.textContent = payload.results.length ? "COMPLETE" : "NO RESULTS";
  queryId.textContent = `QUERY ${payload.query_id.slice(0, 8).toUpperCase()}`;
  const indexLabel = payload.model.index_version || payload.model.index_alias;
  const backendLabel = payload.model.vector_backend
    ? `${payload.model.vector_backend.toUpperCase()} `
    : "";
  indexVersion.textContent = `INDEX ${backendLabel}${indexLabel.toUpperCase()}`;
  latency.textContent = `${Math.round(trace.browserTotalMs)} MS`;
  renderLatencyTrace(payload.timing, trace);
}

function renderLatencyTrace(apiTiming, trace) {
  const edgeTotalMs = trace.edgeTotalMs;
  const values = {
    browser_total_ms: trace.browserTotalMs,
    edge_total_ms: edgeTotalMs,
    container_wait_ms: trace.containerWaitMs,
    container_fetch_ms: trace.containerFetchMs,
    network_browser_ms: edgeTotalMs === null
      ? null
      : Math.max(0, trace.browserTotalMs - edgeTotalMs),
    api_total_ms: apiTiming?.total_ms,
    upload_decode_ms: apiTiming?.upload_decode_ms,
    queue_ms: apiTiming?.queue_ms,
    telemetry_start_ms: apiTiming?.telemetry_start_ms,
    telemetry_wait_ms: apiTiming?.telemetry_wait_ms,
    clip_encode_ms: apiTiming?.clip_encode_ms,
    csd_encode_ms: apiTiming?.csd_encode_ms,
    csd_preprocess_ms: apiTiming?.csd_preprocess_ms,
    csd_tensor_transfer_ms: apiTiming?.csd_tensor_transfer_ms,
    csd_backbone_ms: apiTiming?.csd_backbone_ms,
    csd_projection_ms: apiTiming?.csd_projection_ms,
    csd_output_ms: apiTiming?.csd_output_ms,
    embedding_normalize_ms: apiTiming?.embedding_normalize_ms,
    vector_search_ms: apiTiming?.vector_search_ms,
    catalog_ms: apiTiming?.catalog_ms,
  };
  timingValues.forEach((element, key) => {
    element.textContent = formatDuration(values[key]);
  });

  const initialState = trace.containerState;
  const warmStart = initialState === "healthy";
  requestStartState.dataset.state = warmStart ? "ready" : "cold";
  requestStartState.textContent = warmStart
    ? "WARM START"
    : (initialState === "running" ? "WARMING START" : "COLD START");
  latencyPanel.hidden = false;
}

function responseTrace(response, browserStarted) {
  return {
    browserTotalMs: performance.now() - browserStarted,
    containerState: response.headers.get("X-ArtSearch-Container-State") || "unknown",
    containerWaitMs: numericHeader(response, "X-ArtSearch-Container-Wait-Ms"),
    containerFetchMs: numericHeader(response, "X-ArtSearch-Container-Fetch-Ms"),
    edgeTotalMs: numericHeader(response, "X-ArtSearch-Edge-Total-Ms"),
  };
}

function numericHeader(response, name) {
  const raw = response.headers.get(name);
  if (raw === null) return null;
  const value = Number(raw);
  return Number.isFinite(value) && value >= 0 ? value : null;
}

function formatDuration(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "--";
  if (value > 0 && value < 1) return "<1 ms";
  return `${value < 10 ? value.toFixed(1) : Math.round(value)} ms`;
}

function resetAssistant() {
  assistantSequence += 1;
  activeResultsPayload = null;
  assistantPanel.hidden = true;
  assistantTranscript.replaceChildren();
  assistantInput.value = "";
  assistantInput.disabled = true;
  assistantSend.disabled = true;
  assistantState.textContent = "GROUNDED RESULTS";
}

function setAssistantBusy(active, state = "READING METADATA") {
  const available = activeResultsPayload !== null;
  assistantInput.disabled = active || !available;
  assistantSend.disabled = active || !available;
  if (active && !assistantPanel.hidden) {
    assistantState.textContent = state;
  }
}

function appendAssistantMessage(role, message, extraClass = "") {
  const item = document.createElement("p");
  item.className = `assistant-message ${role} ${extraClass}`.trim();
  item.textContent = message;
  assistantTranscript.append(item);
  assistantTranscript.scrollTop = assistantTranscript.scrollHeight;
  return item;
}

function compactAssistantResults(results) {
  return results.map((result) => ({
    artwork_id: result.artwork_id,
    artist_id: result.artist_id,
    artist_name: result.artist_name,
    rank: result.rank,
    content_class: result.content_class,
    source_metadata: {
      post_text: result.source_metadata?.post_text ?? null,
      alt_text: result.source_metadata?.alt_text ?? null,
    },
  }));
}

async function requestAssistant(mode, message = null) {
  const payload = activeResultsPayload;
  if (!payload || payload.results.length === 0) return;

  const requestNumber = ++assistantSequence;
  setAssistantBusy(true);
  const pending = appendAssistantMessage(
    "assistant",
    mode === "summary" ? "Reading returned source metadata..." : "Checking the result set...",
    "pending",
  );
  try {
    const response = await fetch("/api/retrieval-assistant", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        mode,
        message,
        query_id: payload.query_id,
        results: compactAssistantResults(payload.results),
      }),
    });
    const assistantPayload = await response.json();
    if (!response.ok) {
      throw new Error(assistantPayload.error?.message || "The assistant is unavailable.");
    }
    if (requestNumber !== assistantSequence) return;
    pending.remove();
    appendAssistantMessage("assistant", assistantPayload.reply);
    assistantState.textContent = assistantPayload.route === "tool"
      ? "AGENT TOOL"
      : (assistantPayload.status === "fallback" ? "SAFE FALLBACK" : "WORKERS AI / GROUNDED");
    if (assistantPayload.action?.type === "artist_search") {
      await runSearch({
        artistId: assistantPayload.action.artist_id,
        artistName: assistantPayload.action.artist_name,
        preserveAssistant: true,
      });
    }
  } catch (error) {
    if (requestNumber !== assistantSequence) return;
    pending.remove();
    appendAssistantMessage("assistant", error.message, "is-error");
    assistantState.textContent = "ASSISTANT OFFLINE";
  } finally {
    if (requestNumber === assistantSequence) {
      setAssistantBusy(false);
    }
  }
}

function cancelActiveSearch() {
  searchSequence += 1;
  if (activeSearchController !== null) {
    activeSearchController.abort();
    activeSearchController = null;
  }
}

async function runSearch({
  artistId = null,
  artistName = null,
  preserveAssistant = false,
} = {}) {
  if (!selectedFile) return;
  cancelActiveSearch();
  const requestNumber = searchSequence;
  if (!preserveAssistant) {
    resetAssistant();
  } else {
    assistantSequence += 1;
  }

  if (backendState.dataset.state !== "ready") {
    setBackendState("warming", "This search may need to start the model container.");
  }
  setLoading(true, artistName);
  const body = new FormData();
  body.append("image", selectedFile, selectedFile.name);
  body.append("session_key", browserSessionKey());
  retrievalWeightInputs.forEach((weight) => body.append(weight.name, weight.value));
  if (artistId) body.append("artist_id", artistId);
  const controller = new AbortController();
  activeSearchController = controller;
  const timeout = setTimeout(() => controller.abort(), 200_000);
  const browserStarted = performance.now();
  let completedPayload = null;
  try {
    const response = await fetch("/api/search-by-image", {
      method: "POST",
      body,
      signal: controller.signal,
    });
    const payload = await response.json();
    const trace = responseTrace(response, browserStarted);
    if (!response.ok) {
      if (response.status === 503) {
        setBackendState("cold", "The model container did not become ready yet.");
      }
      throw new Error(payload.error?.message || "Search could not be completed.");
    }
    if (requestNumber !== searchSequence) return;
    clearError();
    setBackendState("ready", "The model container responded and is warm.");
    scheduleBackendExpiry();
    renderResults(payload, trace);
    activeResultsPayload = payload;
    completedPayload = payload;
  } catch (error) {
    if (requestNumber !== searchSequence) return;
    resultsGrid.replaceChildren();
    resultCount.textContent = "00 / 10";
    const message = error.name === "AbortError"
      ? "The model did not finish warming in time. Try the image again."
      : error.message;
    showError("Search unavailable", message);
  } finally {
    clearTimeout(timeout);
    if (requestNumber === searchSequence) {
      activeSearchController = null;
      setLoading(false);
    }
  }

  if (completedPayload !== null && requestNumber === searchSequence) {
    assistantPanel.hidden = false;
    if (artistName) {
      appendAssistantMessage(
        "assistant",
        `Artist-scoped retrieval completed for ${artistName}. Summarizing the new set now.`,
      );
    }
    void requestAssistant("summary");
  }
}

function bindFeedback(card, activeQueryId, artworkId) {
  const buttons = [...card.querySelectorAll(".feedback-buttons button")];
  const feedbackStatus = card.querySelector(".feedback-status");
  buttons.forEach((button) => {
    button.addEventListener("click", async () => {
      buttons.forEach((item) => { item.disabled = true; });
      feedbackStatus.textContent = "SAVING";
      feedbackStatus.classList.remove("is-error");
      try {
        const response = await fetch("/api/result-feedback", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            query_id: activeQueryId,
            artwork_id: artworkId,
            session_key: browserSessionKey(),
            grade: button.dataset.grade,
          }),
        });
        const payload = await response.json();
        if (!response.ok) {
          throw new Error(payload.error?.message || "Feedback could not be recorded.");
        }
        buttons.forEach((item) => {
          item.setAttribute("aria-pressed", String(item === button));
        });
        feedbackStatus.textContent = "RECORDED";
      } catch (error) {
        feedbackStatus.textContent = "NOT RECORDED";
        feedbackStatus.classList.add("is-error");
        feedbackStatus.title = error.message;
      } finally {
        buttons.forEach((item) => { item.disabled = false; });
      }
    });
  });
}

function setScore(track, label, value) {
  if (typeof value !== "number") {
    track.style.width = "0%";
    label.textContent = "--";
    return;
  }
  track.style.width = `${Math.max(0, Math.min(1, value)) * 100}%`;
  label.textContent = value.toFixed(2);
}

function formatBytes(value) {
  if (value < 1000) return `${value} B`;
  if (value < 1_000_000) return `${(value / 1000).toFixed(1)} KB`;
  return `${(value / 1_000_000).toFixed(1)} MB`;
}

input.addEventListener("change", () => selectFile(input.files[0]));
clearButton.addEventListener("click", resetFile);
retrievalWeightInputs.forEach((weight) => {
  syncRetrievalWeight(weight);
  weight.addEventListener("input", () => syncRetrievalWeight(weight));
});

["dragenter", "dragover"].forEach((eventName) => {
  dropZone.addEventListener(eventName, (event) => {
    event.preventDefault();
    dropZone.classList.add("is-dragging");
  });
});

["dragleave", "drop"].forEach((eventName) => {
  dropZone.addEventListener(eventName, (event) => {
    event.preventDefault();
    dropZone.classList.remove("is-dragging");
  });
});

dropZone.addEventListener("drop", (event) => selectFile(event.dataTransfer.files[0]));
dropZone.addEventListener("keydown", (event) => {
  if (event.key === "Enter" || event.key === " ") {
    event.preventDefault();
    input.click();
  }
});

form.addEventListener("submit", (event) => {
  event.preventDefault();
  void runSearch();
});

assistantForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const message = assistantInput.value.trim();
  if (!message || activeResultsPayload === null) return;
  assistantInput.value = "";
  appendAssistantMessage("user", message);
  void requestAssistant("message", message);
});

void refreshRuntimeStatus();
void registerSiteVisit();
