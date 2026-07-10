from __future__ import annotations

from html import escape
import os
import json
from pathlib import Path

from artsearch.ingest.config import AppConfig, load_config
from artsearch.ingest.db import connect, init_db
from artsearch.retrieval.search import SearchResult, get_artwork_for_demo, search_similar


def write_search_demo(
    query_artwork_id: str,
    *,
    config_path: str | Path = "config/config.yaml",
    output_path: str | Path | None = None,
    top_k: int | None = None,
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

    with connect(config.database_path) as conn:
        init_db(conn)
        query = get_artwork_for_demo(conn, config, query_artwork_id)
        results = search_similar(conn, config, query_artwork_id, top_k=top_k)

    destination.write_text(
        _render_html(config, destination, query, results),
        encoding="utf-8",
    )
    return destination


def write_gallery_demo(
    *,
    config_path: str | Path = "config/config.yaml",
    output_path: str | Path | None = None,
    sample_per_artist: int = 3,
    top_k: int = 10,
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

    with connect(config.database_path) as conn:
        init_db(conn)
        queries = _sample_gallery_queries(conn, config, sample_per_artist)
        payload = []
        for query in queries:
            results = search_similar(conn, config, query.artwork_id, top_k=top_k)
            payload.append(_gallery_payload(config, destination, query, results))

    destination.write_text(_render_gallery_html(payload), encoding="utf-8")
    return destination


def _render_html(
    config: AppConfig,
    destination: Path,
    query: SearchResult,
    results: list[SearchResult],
) -> str:
    result_cards = "\n".join(
        _render_card(config, destination, result, heading=f"{index}. {result.artist_display_name}")
        for index, result in enumerate(results, start=1)
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>ArtSearch Demo - {escape(query.artwork_id)}</title>
  <style>
    body {{
      font-family: system-ui, sans-serif;
      margin: 24px;
      color: #1f2933;
      background: #f7f7f5;
    }}
    h1, h2 {{
      margin: 0 0 16px;
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
    }}
    figcaption {{
      margin-top: 8px;
      font-size: 13px;
      line-height: 1.35;
      overflow-wrap: anywhere;
    }}
    .score {{
      color: #52606d;
    }}
  </style>
</head>
<body>
  <h1>ArtSearch Baseline Demo</h1>
  <section class="query">
    <h2>Query</h2>
    <div class="grid">
      {_render_card(config, destination, query, heading=query.artist_display_name)}
    </div>
  </section>
  <section>
    <h2>Top Results</h2>
    <div class="grid">
      {result_cards}
    </div>
  </section>
</body>
</html>
"""


def _render_card(
    config: AppConfig,
    destination: Path,
    result: SearchResult,
    *,
    heading: str,
) -> str:
    image_src = _relative_image_src(config, destination, result.processed_path)
    return f"""<figure>
  <img src="{escape(image_src)}" alt="{escape(result.artwork_id)}">
  <figcaption>
    <strong>{escape(heading)}</strong><br>
    {escape(result.artwork_id)}<br>
    <span class="score">score {result.score:.4f}</span>
  </figcaption>
</figure>"""


def _relative_image_src(config: AppConfig, destination: Path, processed_path: str) -> str:
    image_path = Path(processed_path)
    if not image_path.is_absolute():
        image_path = config.root_dir / image_path
    return Path(os.path.relpath(image_path, destination.parent)).as_posix()


def _sample_gallery_queries(
    conn,
    config: AppConfig,
    sample_per_artist: int,
) -> list[SearchResult]:
    rows = conn.execute(
        """
        SELECT
            artworks.artwork_id,
            artworks.artist_id,
            artists.display_name AS artist_display_name,
            artworks.processed_path
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
                )
            )
    return queries


def _gallery_payload(
    config: AppConfig,
    destination: Path,
    query: SearchResult,
    results: list[SearchResult],
) -> dict:
    return {
        "query": _gallery_item(config, destination, query),
        "results": [_gallery_item(config, destination, result) for result in results],
    }


def _gallery_item(config: AppConfig, destination: Path, result: SearchResult) -> dict:
    return {
        "artworkId": result.artwork_id,
        "artistId": result.artist_id,
        "artistName": result.artist_display_name,
        "imageSrc": _relative_image_src(config, destination, result.processed_path),
        "score": result.score,
    }


def _render_gallery_html(payload: list[dict]) -> str:
    data_json = json.dumps(payload).replace("</", "<\\/")
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
    .score {{
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
    <p>
      Choose a query image. Results use DINO pooled-vector similarity with same-artist filtering.
    </p>
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
    const data = JSON.parse(document.getElementById("searchData").textContent);
    const queryPanel = document.getElementById("queryPanel");
    const selected = document.getElementById("selected");
    const results = document.getElementById("results");

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
            <span class="score">score ${{score}}</span>
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
            <div><strong>Results:</strong> top ${{entry.results.length}} cross-artist matches</div>
          </div>
        </div>
      `;
      results.innerHTML = entry.results
        .map((item, resultIndex) => card(item, resultIndex + 1))
        .join("");
      renderQueries(index);
    }}

    if (data.length) {{
      selectQuery(0);
    }} else {{
      selected.innerHTML = `
        <p>No embedded artworks are available for the configured model versions.</p>
      `;
    }}
  </script>
</body>
</html>
"""
