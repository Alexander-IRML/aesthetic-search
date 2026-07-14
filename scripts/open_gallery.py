from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from artsearch.retrieval.cli import open_gallery_main


if __name__ == "__main__":
    open_gallery_main()
