# ArtSearch

Data preparation and baseline visual search pipeline for artwork.

The current MVP ingests explicitly registered artist folders from `data/raw/`,
standardizes images into 448x448 JPEGs in `data/processed/`, tracks metadata in
SQLite, stores CLIP and DINOv2 embeddings, and can generate local HTML search
demos over CLIP subject, DINO pooled, and DINO patch MaxSim retrieval modes.

For a more detailed technical summary, see
[docs/mvp_report.md](docs/mvp_report.md).

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
python scripts/open_gallery.py --mode dino_pooled
python scripts/search_gallery.py --mode dino_pooled
python scripts/search_gallery.py --mode dino_patch_maxsim
python scripts/patch_diagnostics.py <query_artwork_id> <candidate_artwork_id>
```

Generated image data, processed outputs, the SQLite DB, local demo HTML, and the
private local artist manifest are intentionally ignored by git.
