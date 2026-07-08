from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from artsearch.ingest.cli import register_artists_main


if __name__ == "__main__":
    register_artists_main()