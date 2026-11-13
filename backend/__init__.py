"""ENAC-IT4R Dataset & Repository Harvester backend package.

Modules
-------
config      verified API constraints, paths and the resource-type taxonomy
logger      thread-safe ring-buffer logger streamed to the dashboard
models      normalized ``HarvestRecord`` plus HTML/date/DOI helpers
storage     SQLite upserts with a synchronized JSON mirror
harvester   parallel orchestration and cross-repository DOI merging
server      threaded HTTP server (frontend assets + JSON API)
scrapers    isolated Zenodo and Infoscience clients
"""

from __future__ import annotations

from .config import APP_NAME, APP_VERSION

__all__ = ["APP_NAME", "APP_VERSION"]
