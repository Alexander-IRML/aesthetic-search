# ArtSearch

Data preparation pipeline for a visual similarity search system over artwork.

This first phase ingests explicitly registered artist folders from `data/raw/`,
standardizes images into 448x448 JPEGs in `data/processed/`, and tracks metadata
in SQLite. Embedding generation and retrieval are intentionally out of scope for
this phase.
