"""Filesystem paths shared by stacks that build container image assets."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Docker build context for every image in this project: both api/ and
# functions/* need shared/, so the context has to be backend/ rather than the
# individual function directory.
BACKEND_DIR = REPO_ROOT / "backend"
