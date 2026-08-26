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

const acceptedTypes = new Set(["image/jpeg", "image/png", "image/webp"]);
const maximumBytes = 10_000_000;
let selectedFile = null;
let previewUrl = null;

function browserSessionKey() {
  const storageKey = "artsearch.public-session.v1";
  let value = localStorage.getItem(storageKey);
  if (!value) {
    value = typeof crypto.randomUUID === "function"
      ? crypto.randomUUID()
      : `web-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    localStorage.setItem(storageKey, value);
  }
  return value;
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

function setLoading(active) {
  searchButton.disabled = active || !selectedFile;
  searchButton.textContent = active ? "Encoding and searching..." : "Search similar artwork";
  resultsGrid.setAttribute("aria-busy", String(active));
  if (!active) return;
  clearError();
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
}

function renderResults(payload) {
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
  indexVersion.textContent = `INDEX ${payload.model.index_alias.toUpperCase()}`;
  latency.textContent = `${Math.round(payload.timing.total_ms)} MS`;
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

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!selectedFile) return;
  setLoading(true);
  const body = new FormData();
  body.append("image", selectedFile, selectedFile.name);
  body.append("session_key", browserSessionKey());
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 125_000);
  try {
    const response = await fetch("/api/search-by-image", {
      method: "POST",
      body,
      signal: controller.signal,
    });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.error?.message || "Search could not be completed.");
    }
    clearError();
    renderResults(payload);
  } catch (error) {
    resultsGrid.replaceChildren();
    resultCount.textContent = "00 / 10";
    const message = error.name === "AbortError"
      ? "The search took too long. Try the image again."
      : error.message;
    showError("Search unavailable", message);
  } finally {
    clearTimeout(timeout);
    setLoading(false);
  }
});
