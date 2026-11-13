"""Central configuration for the ENAC-IT4R Dataset & Repository Harvester.

Every upstream constraint in this module was verified against the live APIs
(see ``docs/TECHNICAL_ARCHITECTURE.md`` section 7 and ``docs/recon/``). No
credentials, tokens or environment-specific values are required: both
repositories are queried anonymously.
"""

from __future__ import annotations

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Project layout
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
FRONTEND_DIR = BASE_DIR / "frontend"

DB_PATH = Path(os.environ.get("HARVESTER_DB", DATA_DIR / "harvester.db"))
JSON_MIRROR_PATH = Path(
    os.environ.get("HARVESTER_JSON_MIRROR", DATA_DIR / "dataset_cache.json")
)

APP_NAME = "ENAC-IT4R Dataset & Repository Harvester"
APP_VERSION = "1.5.0"

# ---------------------------------------------------------------------------
# Local web server
# ---------------------------------------------------------------------------
HOST = "127.0.0.1"
PORT = 8080

# ---------------------------------------------------------------------------
# HTTP identity shared by both clients
# ---------------------------------------------------------------------------
USER_AGENT = (
    "ENAC-IT4R-Harvester/1.5 (EPFL ENAC-IT4R; research data & software outreach; "
    "mailto:enacit@epfl.ch)"
)
REQUEST_TIMEOUT = 60
REQUEST_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json",
}

# ---------------------------------------------------------------------------
# Zenodo (InvenioRDM) - EPFL community
# ---------------------------------------------------------------------------
ZENODO_COMMUNITY = "epfl"
ZENODO_API_BASE = "https://zenodo.org/api"
ZENODO_COMMUNITY_RECORDS_URL = (
    f"{ZENODO_API_BASE}/communities/{ZENODO_COMMUNITY}/records"
)
ZENODO_PAGE_SIZE = 25           # anonymous hard cap: size > 25 -> HTTP 400
ZENODO_FIRST_PAGE = 1           # strictly 1-indexed: page=0 -> HTTP 400
ZENODO_MAX_PAGES = 60           # 60 * 25 = 1500 records ceiling per run
ZENODO_DELAY_SECONDS = 2.0      # polite pacing between pages
ZENODO_RATE_LIMIT_FLOOR = 3     # pause when X-RateLimit-Remaining <= 3
ZENODO_RATE_LIMIT_PAUSE = 15.0  # seconds to cool down
ZENODO_SORT = "newest"

# ---------------------------------------------------------------------------
# EPFL Infoscience (DSpace 7 CRIS)
# ---------------------------------------------------------------------------
INFOSCIENCE_BASE = "https://infoscience.epfl.ch"
INFOSCIENCE_API_BASE = f"{INFOSCIENCE_BASE}/server/api"
INFOSCIENCE_SEARCH_URL = f"{INFOSCIENCE_API_BASE}/discover/search/objects"
INFOSCIENCE_CONFIGURATION = "researchoutputs"
INFOSCIENCE_PAGE_SIZE = 100     # never >100: DSpace clamps objects but not offset
INFOSCIENCE_FIRST_PAGE = 0      # strictly 0-indexed
INFOSCIENCE_MAX_PAGES = 3       # CloudFront burst budget: 3 * 100 records / run
INFOSCIENCE_DELAY_SECONDS = 6.5  # host-wide 429 after ~13 calls / 2 minutes
INFOSCIENCE_SORT = "dc.date.accessioned,desc"

# AWS WAF Bot Control: requests may be answered with a CAPTCHA challenge that the
# OS-native TLS stack (curl / Schannel) is allowed to pass.
WAF_ACTION_HEADER = "x-amzn-waf-action"
WAF_CHALLENGE_STATUSES = (202, 405)
CURL_BINARY = os.environ.get("HARVESTER_CURL", "curl")

# DSpace CRIS sentinel that must never reach the UI or the database.
PLACEHOLDER_VALUE = "#PLACEHOLDER_PARENT_METADATA_VALUE#"

# ---------------------------------------------------------------------------
# Resource type taxonomy
# ---------------------------------------------------------------------------
# ``query`` fragments are injected verbatim into the Lucene (Zenodo) and Solr
# (Infoscience) query strings. Findings behind each entry are documented in
# docs/TECHNICAL_ARCHITECTURE.md (sections 6.1 - 6.3).
RESOURCE_FILTERS = {
    "dataset": {
        "label": "Datasets Only",
        "pill": "Datasets",
        "description": "Research data, including COAR dataset subtypes.",
        "zenodo_query": "resource_type.type:dataset",
        "infoscience_query": "dc.type:dataset",
        "classes": ["dataset"],
    },
    "software": {
        "label": "Software Only",
        "pill": "Software",
        "description": "Research software, source code and software papers.",
        "zenodo_query": "resource_type.type:software",
        "infoscience_query": "dc.type:software",
        "classes": ["software"],
    },
    "model_workflow": {
        "label": "Models & Workflows",
        "pill": "Models & Workflows",
        "description": "Invenio models/workflows; Infoscience models minus patents.",
        "zenodo_query": "(resource_type.type:model OR resource_type.type:workflow)",
        "infoscience_query": (
            "(dc.type:model OR dc.type:workflow) AND NOT dc.type:patent"
        ),
        "classes": ["model", "workflow"],
    },
    "other": {
        "label": "Other Research Products",
        "pill": "Other Products",
        "description": (
            "Posters, presentations, images, videos and non-student 'other' records."
        ),
        "zenodo_query": (
            "(resource_type.type:other OR resource_type.type:image "
            "OR resource_type.type:video OR resource_type.type:poster "
            "OR resource_type.type:presentation)"
        ),
        "infoscience_query": 'dc.type:other AND NOT dc.type:"student work"',
        "classes": ["other"],
    },
    "all": {
        "label": "All Works",
        "pill": "All Works",
        "description": (
            "Every listable research output (datasets, software, models, "
            "workflows, other products) — never journal articles or student work."
        ),
        # Never an unfiltered query: an empty Infoscience clause returns ~197 000
        # journal articles, and the 300-record page cap would then stop before
        # reaching any dataset from an earlier month.
        "zenodo_query": (
            "(resource_type.type:dataset OR resource_type.type:software "
            "OR resource_type.type:model OR resource_type.type:workflow "
            "OR resource_type.type:other OR resource_type.type:image "
            "OR resource_type.type:video OR resource_type.type:poster "
            "OR resource_type.type:presentation)"
        ),
        "infoscience_query": (
            '((dc.type:dataset OR dc.type:software OR dc.type:model '
            "OR dc.type:workflow OR dc.type:other) "
            'AND NOT dc.type:"student work" AND NOT dc.type:patent)'
        ),
        "classes": ["dataset", "software", "model", "workflow", "other"],
    },
}
DEFAULT_RESOURCE_FILTER = "dataset"
ALL_RESOURCE_CLASSES = ["dataset", "software", "model", "workflow", "other"]

# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
SCHEMA_VERSION = "1.5"
LOG_BUFFER_SIZE = 800
ABSTRACT_LIMIT = 1200

# ``harvest_runs`` keeps a bounded audit trail: repeated zero-change runs are
# coalesced into their predecessor and older rows are pruned beyond this cap
# (see ``Storage.record_run``).
MAX_HARVEST_RUNS_HISTORY = 25

# Per query scope ``(resource_type, date_from, date_to)`` only the previous and
# the current run are kept, so re-running one window never piles up rows.
MAX_RUNS_PER_SCOPE = 2

# Fields compared on every upsert: a difference marks the row ``updated``.
TRACKED_FIELDS = (
    "title",
    "abstract",
    "affiliations",
    "unit_managers",
    "doi",
    "publication_date",
    "resource_type",
    "funding",
    "code_repo",
    "keywords",
    "url",
)

# Display labels for the repository origin badge.
SOURCE_LABELS = {
    "zenodo": "Zenodo (EPFL Community)",
    "infoscience": "Infoscience (DSpace 7)",
    "merged": "Infoscience + Zenodo",
}
