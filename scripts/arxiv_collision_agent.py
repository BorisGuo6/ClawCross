#!/usr/bin/env python3
"""CLI wrapper for the arXiv Robotics collision monitor."""

from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from services.arxiv_collision_service import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
