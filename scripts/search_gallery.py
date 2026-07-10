from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from artsearch.retrieval.cli import gallery_demo_main


if __name__ == "__main__":
    gallery_demo_main()
