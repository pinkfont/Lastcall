"""Where things live on disk.

    <root>/
      *.bat          the commands you double-click
      app/           this code, tests, requirements, Python environment
      config/.env    your settings and secrets (never committed)
      data/          database, saved POLITEMall login, debug logs (never committed)
      docs/          README

DATA_DIR can be overridden with the DATA_DIR environment variable (e.g. on a server).
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP_DIR = ROOT / "app"
CONFIG_DIR = ROOT / "config"
DATA_DIR = Path(os.getenv("DATA_DIR", ROOT / "data"))


def data_path(value: str | os.PathLike) -> Path:
    """Relative paths (like DB_PATH=duetracker.db) are placed inside data/."""
    path = Path(value)
    return path if path.is_absolute() else DATA_DIR / path
