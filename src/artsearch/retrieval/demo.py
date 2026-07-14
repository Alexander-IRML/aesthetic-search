from __future__ import annotations

from collections.abc import Sequence
from html import escape
import json
import os
from pathlib import Path

from artsearch.ingest.config import AppConfig, load_config
from artsearch.ingest.db import connect, init_db
from artsearch.retrieval.search import (
    SUPPORTED_RETRIEVAL_MODES,
    RetrievalMode,
    SearchFilters,
    SearchResult,
    get_artwork_for_demo,
    search_similar,
)


def write_search_demo(
    query_artwork_id: str,
    *,
    config_path: str | Path = "config/config.yaml",
    output_path: str | Path | None = None,
    top_k: int | None = None,
    mode: RetrievalMode | str | None = None,
    include_same_artist: bool = False,
    review_status: str | None = None,
    is_sfw: bool | None = None,
) -> Path:
    config = load_config(config_path)
    destination = (
        Path(output_path)
        if output_path is not None
        else config.retrieval.demo_output_path
    )
    if not destination.is_absolute():
        destination = config.root_dir / destination
    destination.parent.mkdir(parents=True, exist_ok=True)

    filters = SearchFilters(
        top_k=top_k,
        include_same_artist=include_same_artist,
        review_status=review_status,
        is_sfw=is_sfw,
    )
    modes = _mode_list(mode, default=SUPPORTED_RETRIEVAL_MODES)

    with connect(config.database_path) as conn:
        init_db(conn)
        query = get_artwork_for_demo(conn, config, query_artwork_id)
        mode_payloads = []
        for retrieval_mode in modes:
            results = search_similar(
                conn,
                config,
                query_artwork_id,
                mode=retrieval_mode,
                filters=filters,
            )
            mode_payloads.append(
                _mode_payload(config, destination, retrieval_mode, query, results)
            )

    destination.write_text(
        _render_search_html(
            query_artwork_id=query_artwork_id,
            mode_payloads=mode_payloads,
            filters=filters,
        ),
        encoding="utf-8",
    )
    return destination


def write_gallery_demo(
    *,
    config_path: str | Path = "config/config.yaml",
    output_path: str | Path | None = None,
    sample_per_artist: int = 3,
    top_k: int = 10,
    mode: RetrievalMode | str = RetrievalMode.DINO_POOLED,
    include_same_artist: bool = False,
    review_status: str | None = None,
    is_sfw: bool | None = None,
) -> Path:
    config = load_config(config_path)
    destination = (
        Path(output_path)
        if output_path is not None
        else config.retrieval.gallery_output_path
    )
    if not destination.is_absolute():
        destination = config.root_dir / destination
    destination.parent.mkdir(parents=True, exist_ok=True)

    retrieval_mode = RetrievalMode.coerce(mode)
    filters = SearchFilters(
        top_k=top_k,
        include_same_artist=include_same_artist,
        review_status=review_status,
        is_sfw=is_sfw,
    )

    with connect(config.database_path) as conn:
        init_db(conn)
        queries = _sample_gallery_queries(conn, config, sample_per_artist, filters)
        query_payloads = []
        for query in queries:
            results = search_similar(
                conn,
                config,
                query.artwork_id,
                mode=retrieval_mode,
                filters=filters,
            )
            query_payloads.append(
                _gallery_payload(config, destination, retrieval_mode, query, results)
            )

    destination.write_text(
        _render_gallery_html(
            {
                "mode": _mode_info(retrieval_mode),
                "filters": _filters_payload(filters),
                "queries": query_payloads,
            }
        ),
        encoding="utf-8",
    )
    return destination


def _mode_list(
    mode: RetrievalMode | str | None,
    *,
    default: Sequence[RetrievalMode],
) -> list[RetrievalMode]:
    if mode is None or mode == "all":
        return list(default)
    return [RetrievalMode.coerce(mode)]


def _mode_info(mode: RetrievalMode) -> dict:
    return {
        "value": mode.value,
        "label": mode.label,
    }


def _mode_payload(
    config: AppConfig,
    destination: Path,
    mode: RetrievalMode,
    query: SearchResult,
    results: list[SearchResult],
) -> dict:
    return {
        "mode": _mode_info(mode),
        "query": _demo_item(config, destination, query),
        "results": [_demo_item(config, destination, result) for result in results],
    }


def _gallery_payload(
    config: AppConfig,
    destination: Path,
    mode: RetrievalMode,
    query: SearchResult,
    results: list[SearchResult],
) -> dict:
    return {
        "mode": _mode_info(mode),
        "query": _demo_item(config, destination, query),
        "results": [_demo_item(config, destination, result) for result in results],
    }


def _demo_item(config: AppConfig, destination: Path, result: SearchResult) -> dict:
    return {
        "artworkId": result.artwork_id,
        "artistId": result.artist_id,
        "artistName": result.artist_display_name,
        "imageSrc": _relative_image_src(config, destination, result.processed_path),
        "score": result.score,
        "mode": result.mode.value,
        "reviewStatus": result.review_status,
        "isSfw": result.is_sfw,
    }


def _filters_payload(filters: SearchFilters) -> dict:
    return {
        "topK": filters.top_k,
        "includeSameArtist": filters.include_same_artist,
        "reviewStatus": filters.review_status,
        "isSfw": filters.is_sfw,
    }


def _relative_image_src(config: AppConfig, destination: Path, processed_path: str) -> str:
    image_path = Path(processed_path)
    if not image_path.is_absolute():
        image_path = config.root_dir / image_path
    return Path(os.path.relpath(image_path, destination.parent)).as_posix()


def _sample_gallery_queries(
    conn,
    config: AppConfig,
    sample_per_artist: int,
    filters: SearchFilters,
) -> list[SearchResult]:
    rows = conn.execute(
        """
        SELECT
            artworks.artwork_id,
            artworks.artist_id,
            artists.display_name AS artist_display_name,
            artworks.processed_path,
            artworks.review_status,
            artworks.is_sfw
          FROM artworks
          JOIN artists ON artists.artist_id = artworks.artist_id
          JOIN embeddings ON embeddings.artwork_id = artworks.artwork_id
         WHERE artworks.validated = 1
           AND artworks.processed_path IS NOT NULL
           AND embeddings.model_name_dino = ?
           AND embeddings.model_version_dino = ?
           AND embeddings.model_name_clip = ?
           AND embeddings.model_version_clip = ?
         ORDER BY artists.display_name, artworks.artwork_id
        """,
        (
            config.models.dino_model_name,
            config.models.dino_model_version,
            config.models.clip_model_name,
            config.models.clip_model_version,
        ),
    ).fetchall()

    by_artist = {}
    for row in rows:
        if filters.review_status is not None and row["review_status"] != filters.review_status:
            continue
        is_sfw = _bool_or_none(row["is_sfw"])
        if filters.is_sfw is not None and is_sfw != filters.is_sfw:
            continue
        by_artist.setdefault(row["artist_id"], []).append(row)

    queries = []
    for artist_id in sorted(by_artist):
        for row in by_artist[artist_id][:sample_per_artist]:
            queries.append(
                SearchResult(
                    artwork_id=row["artwork_id"],
                    artist_id=row["artist_id"],
                    artist_display_name=row["artist_display_name"],
                    processed_path=row["processed_path"],
                    score=1.0,
                    review_status=row["review_status"],
                    is_sfw=_bool_or_none(row["is_sfw"]),
                )
            )
    return queries


def _render_search_html(
    *,
    query_artwork_id: str,
    mode_payloads: list[dict],
    filters: SearchFilters,
) -> str:
    data_json = _safe_json(
        {
            "queryArtworkId": query_artwork_id,
            "modes": mode_payloads,
            "filters": _filters_payload(filters),
        }
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>ArtSearch Demo - {escape(query_artwork_id)}</title>
  <style>
    * {{
      box-sizing: border-box;
    }}
    body {{
      font-family: system-ui, sans-serif;
      margin: 24px;
      color: #1f2933;
      background: #f7f7f5;
    }}
    h1, h2, h3, p {{
      margin: 0;
    }}
    header {{
      display: grid;
      gap: 12px;
      margin-bottom: 22px;
    }}
    .modebar {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }}
    .modebar button {{
      border: 1px solid #b8beb6;
      border-radius: 8px;
      padding: 7px 10px;
      background: #ffffff;
      cursor: pointer;
      font: inherit;
    }}
    .modebar button[aria-pressed="true"] {{
      border-color: #2458a6;
      color: #123f7c;
      background: #edf4ff;
    }}
    .query {{
      margin-bottom: 28px;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
      gap: 16px;
    }}
    figure {{
      margin: 0;
      padding: 10px;
      background: #ffffff;
      border: 1px solid #d8d8d2;
      border-radius: 8px;
    }}
    img {{
      display: block;
      width: 100%;
      aspect-ratio: 1;
      object-fit: contain;
      background: #808080;
      border-radius: 4px;
    }}
    figcaption {{
      margin-top: 8px;
      font-size: 13px;
      line-height: 1.35;
      overflow-wrap: anywhere;
    }}
    .score, .subtle {{
      color: #52606d;
    }}
  </style>
</head>
<body>
  <header>
    <h1>ArtSearch Search Demo</h1>
    <p class="subtle">
      Compare retrieval modes for one query image. MaxSim is experimental local-detail search.
    </p>
    <div id="modebar" class="modebar"></div>
  </header>
  <section class="query">
    <h2>Query</h2>
    <div id="query" class="grid"></div>
  </section>
  <section>
    <h2 id="resultsTitle">Top Results</h2>
    <div id="results" class="grid"></div>
  </section>
  <script id="searchData" type="application/json">{data_json}</script>
  <script>
    const payload = JSON.parse(document.getElementById("searchData").textContent);
    const modebar = document.getElementById("modebar");
    const query = document.getElementById("query");
    const results = document.getElementById("results");
    const resultsTitle = document.getElementById("resultsTitle");

    function card(item, rank) {{
      const score = item.score.toFixed(4);
      return `
        <figure>
          <img src="${{item.imageSrc}}" alt="${{item.artworkId}}">
          <figcaption>
            <strong>${{rank ? `${{rank}}. ` : ""}}${{item.artistName}}</strong><br>
            ${{item.artworkId}}<br>
            <span class="score">score ${{score}}</span><br>
            <span class="subtle">${{item.reviewStatus}} · SFW ${{item.isSfw}}</span>
          </figcaption>
        </figure>
      `;
    }}

    function selectMode(index) {{
      const entry = payload.modes[index];
      modebar.querySelectorAll("button").forEach((button, buttonIndex) => {{
        button.setAttribute("aria-pressed", String(buttonIndex === index));
      }});
      query.innerHTML = card(entry.query, null);
      resultsTitle.textContent = `Top Results · ${{entry.mode.label}}`;
      results.innerHTML = entry.results.length
        ? entry.results.map((item, resultIndex) => card(item, resultIndex + 1)).join("")
        : "<p>No results matched the current filters.</p>";
    }}

    modebar.innerHTML = payload.modes.map((entry, index) => `
      <button type="button" data-index="${{index}}" aria-pressed="false">
        ${{entry.mode.label}}
      </button>
    `).join("");
    modebar.querySelectorAll("button").forEach((button) => {{
      button.addEventListener("click", () => selectMode(Number(button.dataset.index)));
    }});

    if (payload.modes.length) {{
      selectMode(0);
    }} else {{
      results.innerHTML = "<p>No retrieval modes were rendered.</p>";
    }}
  </script>
</body>
</html>
"""


def _render_gallery_html(payload: dict) -> str:
    data_json = _safe_json(payload)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>ArtSearch Gallery Demo</title>
  <style>
    * {{
      box-sizing: border-box;
    }}
    body {{
      margin: 0;
      min-height: 100vh;
      font-family: system-ui, sans-serif;
      color: #202124;
      background: #f4f4f1;
    }}
    header {{
      padding: 16px 20px;
      border-bottom: 1px solid #d7d7d0;
      background: #ffffff;
    }}
    h1, h2, h3, p {{
      margin: 0;
    }}
    h1 {{
      font-size: 20px;
    }}
    p {{
      color: #5f666d;
      font-size: 14px;
      margin-top: 4px;
    }}
    main {{
      display: grid;
      grid-template-columns: minmax(260px, 34%) 1fr;
      min-height: calc(100vh - 73px);
    }}
    aside {{
      overflow: auto;
      padding: 16px;
      border-right: 1px solid #d7d7d0;
      background: #fbfbf8;
    }}
    section {{
      padding: 18px;
      overflow: auto;
    }}
    .artist {{
      margin-bottom: 18px;
    }}
    .artist h2 {{
      font-size: 14px;
      margin-bottom: 8px;
      color: #3f454b;
    }}
    .query-grid, .result-grid {{
      display: grid;
      gap: 10px;
    }}
    .query-grid {{
      grid-template-columns: repeat(auto-fill, minmax(92px, 1fr));
    }}
    .result-grid {{
      grid-template-columns: repeat(auto-fill, minmax(156px, 1fr));
      margin-top: 16px;
    }}
    button.thumb {{
      display: block;
      width: 100%;
      padding: 0;
      border: 2px solid transparent;
      border-radius: 8px;
      background: transparent;
      cursor: pointer;
    }}
    button.thumb[aria-pressed="true"] {{
      border-color: #2458a6;
    }}
    figure {{
      margin: 0;
      padding: 8px;
      border: 1px solid #d6d8d2;
      border-radius: 8px;
      background: #ffffff;
    }}
    img {{
      display: block;
      width: 100%;
      aspect-ratio: 1;
      object-fit: contain;
      background: #808080;
      border-radius: 4px;
    }}
    figcaption {{
      margin-top: 7px;
      font-size: 12px;
      line-height: 1.35;
      overflow-wrap: anywhere;
      color: #3f454b;
    }}
    .selected {{
      display: grid;
      grid-template-columns: minmax(180px, 280px) 1fr;
      gap: 16px;
      align-items: start;
      padding: 12px;
      border: 1px solid #d6d8d2;
      border-radius: 8px;
      background: #ffffff;
    }}
    .selected img {{
      max-height: 280px;
    }}
    .meta {{
      display: grid;
      gap: 8px;
      font-size: 14px;
      color: #3f454b;
    }}
    .score, .subtle {{
      color: #68707a;
    }}
    @media (max-width: 820px) {{
      main {{
        grid-template-columns: 1fr;
      }}
      aside {{
        border-right: 0;
        border-bottom: 1px solid #d7d7d0;
      }}
      .selected {{
        grid-template-columns: 1fr;
      }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>ArtSearch Gallery Demo</h1>
    <p id="summary"></p>
  </header>
  <main>
    <aside id="queryPanel"></aside>
    <section>
      <div id="selected"></div>
      <div id="results" class="result-grid"></div>
    </section>
  </main>
  <script id="searchData" type="application/json">{data_json}</script>
  <script>
    const payload = JSON.parse(document.getElementById("searchData").textContent);
    const data = payload.queries;
    const queryPanel = document.getElementById("queryPanel");
    const selected = document.getElementById("selected");
    const results = document.getElementById("results");
    const summary = document.getElementById("summary");

    summary.textContent = `Mode: ${{payload.mode.label}}. Choose a query image.`;

    function groupByArtist(items) {{
      return items.reduce((groups, item, index) => {{
        const name = item.query.artistName;
        groups[name] = groups[name] || [];
        groups[name].push([item, index]);
        return groups;
      }}, {{}});
    }}

    function renderQueries(activeIndex) {{
      const groups = groupByArtist(data);
      queryPanel.innerHTML = Object.entries(groups).map(([artist, entries]) => `
        <div class="artist">
          <h2>${{artist}}</h2>
          <div class="query-grid">
            ${{entries.map(([entry, index]) => `
              <button
                class="thumb"
                aria-pressed="${{index === activeIndex}}"
                data-index="${{index}}"
              >
                <img src="${{entry.query.imageSrc}}" alt="${{entry.query.artworkId}}">
              </button>
            `).join("")}}
          </div>
        </div>
      `).join("");
      queryPanel.querySelectorAll("button").forEach((button) => {{
        button.addEventListener("click", () => selectQuery(Number(button.dataset.index)));
      }});
    }}

    function card(item, rank) {{
      const score = item.score.toFixed(4);
      return `
        <figure>
          <img src="${{item.imageSrc}}" alt="${{item.artworkId}}">
          <figcaption>
            <strong>${{rank}}. ${{item.artistName}}</strong><br>
            ${{item.artworkId}}<br>
            <span class="score">score ${{score}}</span><br>
            <span class="subtle">${{item.reviewStatus}} · SFW ${{item.isSfw}}</span>
          </figcaption>
        </figure>
      `;
    }}

    function selectQuery(index) {{
      const entry = data[index];
      selected.innerHTML = `
        <div class="selected">
          <img src="${{entry.query.imageSrc}}" alt="${{entry.query.artworkId}}">
          <div class="meta">
            <h2>Query</h2>
            <div><strong>Artist:</strong> ${{entry.query.artistName}}</div>
            <div><strong>Artwork:</strong> ${{entry.query.artworkId}}</div>
            <div><strong>Mode:</strong> ${{entry.mode.label}}</div>
            <div><strong>Results:</strong> top ${{entry.results.length}} matches</div>
          </div>
        </div>
      `;
      results.innerHTML = entry.results.length
        ? entry.results.map((item, resultIndex) => card(item, resultIndex + 1)).join("")
        : "<p>No results matched the current filters.</p>";
      renderQueries(index);
    }}

    if (data.length) {{
      selectQuery(0);
    }} else {{
      selected.innerHTML = `
        <p>No embedded artworks are available for the configured filters and model versions.</p>
      `;
    }}
  </script>
</body>
</html>
"""


def _safe_json(payload: dict) -> str:
    return json.dumps(payload).replace("</", "<\\/")


def _bool_or_none(value: object) -> bool | None:
    if value is None:
        return None
    return bool(value)
