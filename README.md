# ArtSearch

Data preparation and baseline visual search pipeline for artwork.

The current MVP ingests explicitly registered artist folders from `data/raw/`,
standardizes images into 448x448 JPEGs in `data/processed/`, tracks metadata in
SQLite, stores CLIP and DINOv2 embeddings, and can generate a minimal local HTML
search demo over DINO pooled-vector similarity.

## Local Workflow

For a private corpus, keep real artist registrations in the ignored local
manifest:

```bash
cp config/artists.yaml config/artists.local.yaml
# Edit config/artists.local.yaml with local folder names.
```

```bash
source .venv/bin/activate
python -m pytest -q
python -m ruff check .
python scripts/standardize.py
```

Embedding generation uses optional ML dependencies:

```bash
python -m pip install -e '.[embed]'
python scripts/embed.py
python scripts/search_demo.py <artwork_id>
python scripts/search_gallery.py
```

Generated image data, processed outputs, the SQLite DB, local demo HTML, and the
private local artist manifest are intentionally ignored by git.
