"""Normalized data model shared by both scrapers, the storage layer and the API.

A :class:`HarvestRecord` is the single canonical shape the whole application
speaks. Scrapers only ever produce these objects, the storage layer only ever
persists them, and the frontend only ever renders their JSON form.
"""

from __future__ import annotations

import html as html_module
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import PurePosixPath
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlsplit

from .config import ABSTRACT_LIMIT, ALL_RESOURCE_CLASSES, PLACEHOLDER_VALUE

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_SCRIPT_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_DOI_PREFIX_RE = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", re.IGNORECASE)
_KEYWORD_SPLIT_RE = re.compile(r"[,;]")


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------
def strip_html(value: Any) -> str:
    """Remove HTML tags and entities from an API string (Zenodo abstracts)."""
    if value is None:
        return ""
    text = str(value)
    text = _SCRIPT_RE.sub(" ", text)
    text = _TAG_RE.sub(" ", text)
    text = html_module.unescape(text)
    return _WS_RE.sub(" ", text).strip()


def clean_text(value: Any, limit: Optional[int] = None) -> str:
    """Normalize an API string: strip HTML, collapse whitespace, optionally clip."""
    text = strip_html(value)
    if text == PLACEHOLDER_VALUE:
        return ""
    if limit and len(text) > limit:
        text = text[:limit].rstrip() + "..."
    return text


def unique(values: Iterable[Any]) -> List[str]:
    """De-duplicate while preserving order, dropping empty entries."""
    seen: set = set()
    result: List[str] = []
    for raw in values:
        text = clean_text(raw)
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def split_keywords(raw: Any) -> List[str]:
    """Flatten repository keyword metadata into individual, de-duplicated tags.

    Both APIs regularly return one comma- or semicolon-delimited string inside a
    single-element list (``["magnonics, nanomagnetism, spin waves"]``), so every
    part must become its own chip instead of a single merged tag.
    """
    if raw is None:
        return []
    if isinstance(raw, dict):
        return split_keywords(raw.get("subject") or raw.get("value"))
    if isinstance(raw, (list, tuple, set)):
        parts: List[str] = []
        for item in raw:
            parts.extend(split_keywords(item))
        return unique(parts)
    return unique(_KEYWORD_SPLIT_RE.split(clean_text(raw)))


# ---------------------------------------------------------------------------
# Date / identifier helpers
# ---------------------------------------------------------------------------
def to_date_string(value: Any) -> str:
    """Return a ``YYYY-MM-DD`` string from DSpace/Zenodo date values (or '')."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    if not text:
        return ""
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})", text)
    if match:
        return match.group(0)
    match = re.match(r"^(\d{4})-(\d{2})$", text)
    if match:
        return f"{match.group(0)}-01"
    match = re.match(r"^(\d{4})$", text)
    if match:
        return f"{match.group(0)}-01-01"
    return ""


def parse_iso_date(value: Any) -> Optional[date]:
    """Parse a user supplied date (``YYYY-MM-DD``, date or datetime)."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def utc_now_iso() -> str:
    """Current UTC timestamp, second precision, ISO-8601."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_doi(value: Any) -> str:
    """Normalize a DOI for cross-repository comparison and primary keys."""
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    text = _DOI_PREFIX_RE.sub("", text)
    text = text.strip().rstrip(".")
    if not text or " " in text or "/" not in text:
        return ""
    return text.casefold()


# ---------------------------------------------------------------------------
# Code repository URLs
# ---------------------------------------------------------------------------
#: Code-forge hosts; any ``gitlab.*`` instance (``gitlab.com``,
#: ``gitlab.epfl.ch``, ``gitlab.example.org``) is accepted on top of these.
CODE_REPO_HOSTS = (
    "github.com",
    "bitbucket.org",
    "codeocean.com",
    "sourceforge.net",
)

#: Code-forge links inside free text (markdown ``[label](url)`` included).
_CODE_URL_IN_TEXT_RE = re.compile(
    r"(?<![a-z0-9.\-])(?:https?://|www\.)?"
    r"(?:github\.com|gitlab\.[a-z0-9.\-]+|bitbucket\.org|codeocean\.com|sourceforge\.net)"
    r"(?:/[^\s<>\"'`\]\)}]*)?",
    re.IGNORECASE,
)


def normalize_code_repo_url(value: Any) -> str:
    """Return the canonical repository URL held by ``value``, else ``""``.

    Only allow-listed forges pass, and Pages sites never do. ``http`` becomes
    ``https``; ``www.``, trailing slashes, ``.git``, query/fragment and
    in-repository pages (``/tree/...``, ``/-/tree/...``, ``/blob/...``,
    ``/issues``, ...) are dropped, so the same repository compares equal.
    """
    text = str(value or "").strip().strip("<>()[]{}\"'`").rstrip(".,;:!?")
    if not text:
        return ""
    if "://" not in text:
        text = "https://" + text.lstrip("/")
    try:
        parts = urlsplit(text)
    except ValueError:
        return ""
    host = (parts.hostname or "").casefold().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    is_gitlab = host.startswith("gitlab.") or ".gitlab." in host
    if not (host in CODE_REPO_HOSTS or is_gitlab):
        return ""
    if host.endswith((".github.io", ".gitlab.io")):
        return ""
    segments = [segment for segment in parts.path.split("/") if segment]
    if is_gitlab:
        # GitLab keeps subgroups; in-repository pages are marked with "/-/".
        segments = segments[: segments.index("-")] if "-" in segments else segments
    else:
        segments = segments[:2]  # <owner>/<repo>: drops /tree/..., /issues, ...
    if len(segments) < 2:
        return ""
    if segments[-1].casefold().endswith(".git"):
        segments[-1] = segments[-1][:-4]
    if not segments[-1]:
        return ""
    return f"https://{host}/" + "/".join(segments)


def extract_code_repo_url(urls: Iterable[str]) -> str:
    """Return the first URL in ``urls`` that points at a code repository."""
    if isinstance(urls, str):
        urls = [urls]
    for candidate in urls or ():
        url = normalize_code_repo_url(candidate)
        if url:
            return url
    return ""


def extract_code_repo_from_text(value: Any) -> str:
    """Scan free text (a Zenodo description) for the first code-repository URL.

    The raw value is scanned before the HTML-stripped one so ``href`` targets and
    ``<https://...>`` links survive :func:`strip_html`.
    """
    for haystack in (str(value or ""), clean_text(value)):
        for match in _CODE_URL_IN_TEXT_RE.finditer(haystack):
            url = normalize_code_repo_url(match.group(0))
            if url:
                return url
    return ""


# ---------------------------------------------------------------------------
# Resource typing
# ---------------------------------------------------------------------------
#: Class for publications. Deliberately *outside* ``ALL_RESOURCE_CLASSES``: the
#: harvesters drop those records instead of filing them under ``other``.
PUBLICATION_CLASS = "publication"

#: Raw type families that describe a publication, not a research output.
_PUBLICATION_MARKERS = (
    "text::",
    "publication",
    "journal",
    "thesis",
    "patent",
    "student work",
)


def classify_resource_type(raw_type: Any) -> str:
    """Map a raw repository type onto a UI resource class.

    Datasets, software, models and workflows keep their exact class; a genuine
    publication — Infoscience ``text::journal::…``, a thesis, a patent, student
    work or Zenodo ``publication`` — returns :data:`PUBLICATION_CLASS` so the
    harvester can discard it instead of storing it as ``other``.
    """
    text = (raw_type or "").strip().casefold()
    if not text:
        return "other"
    root = text.split("::")[0].strip()
    for resource_class in ("dataset", "software", "model", "workflow"):
        if root.startswith(resource_class):
            return resource_class
    if root in ALL_RESOURCE_CLASSES:
        return root
    # More specific than the publication markers: a "software paper" stays software.
    if "software" in text:
        return "software"
    if "dataset" in text:
        return "dataset"
    if any(marker in text for marker in _PUBLICATION_MARKERS):
        return PUBLICATION_CLASS
    return "other"


def format_resource_type(raw_type: Any) -> str:
    """Return the exact resource type string, defaulting to ``other``."""
    text = clean_text(raw_type)
    return text or "other"


# ---------------------------------------------------------------------------
# File summaries
# ---------------------------------------------------------------------------
def human_size(num_bytes: Optional[int]) -> str:
    """Render a byte count as ``812 KB`` / ``1.4 GB`` (empty when unknown)."""
    if num_bytes is None:
        return ""
    try:
        size = float(num_bytes)
    except (TypeError, ValueError):
        return ""
    if size < 0:
        return ""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def file_extension(key: Any) -> str:
    """Lowercase extension of a file key, including ``.tar.gz`` when present."""
    text = str(key or "").strip()
    if not text:
        return ""
    lower = text.casefold()
    if lower.endswith(".tar.gz"):
        return ".tar.gz"
    suffix = PurePosixPath(lower).suffix
    return suffix if suffix and len(suffix) <= 12 else ""


def build_file_summary(files: Any) -> Dict[str, Any]:
    """Summarize a Zenodo ``files`` list into count, size and extensions."""
    entries: List[Dict[str, Any]] = []
    if isinstance(files, list):
        entries = [f for f in files if isinstance(f, dict)]
    elif isinstance(files, dict):
        raw_entries = files.get("entries")
        if isinstance(raw_entries, list):
            entries = [f for f in raw_entries if isinstance(f, dict)]

    extensions: List[str] = []
    total_bytes = 0
    counted = 0
    for entry in entries:
        ext = file_extension(entry.get("key") or entry.get("filename"))
        if ext and ext not in extensions:
            extensions.append(ext)
        size = entry.get("size")
        if isinstance(size, (int, float)):
            total_bytes += int(size)
            counted += 1

    return {
        "count": len(entries),
        "total_bytes": total_bytes if counted else 0,
        "total_human": human_size(total_bytes) if counted else "",
        "extensions": sorted(extensions),
    }


# ---------------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------------
@dataclass
class HarvestRecord:
    """One normalized research output, whichever repository it came from."""

    canonical_id: str
    source_id: str = ""
    source: str = ""
    source_keys: List[str] = field(default_factory=list)
    resource_type: str = "other"
    resource_class: str = "other"
    title: str = ""
    abstract: str = ""
    authors: List[str] = field(default_factory=list)
    affiliations: List[str] = field(default_factory=list)
    parent_orgs: List[str] = field(default_factory=list)
    unit_managers: List[str] = field(default_factory=list)
    author_orcids: List[str] = field(default_factory=list)
    funding: List[str] = field(default_factory=list)
    code_repo: str = ""
    file_summary: Dict[str, Any] = field(default_factory=dict)
    keywords: List[str] = field(default_factory=list)
    license: str = ""
    access_rights: str = ""
    doi: str = ""
    url: str = ""
    publication_date: str = ""
    created_date: str = ""
    first_seen: str = ""
    last_seen: str = ""
    sync_status: str = "new"

    def __post_init__(self) -> None:
        self.resource_type = format_resource_type(self.resource_type)
        # A default "other" is re-derived too: a journal-article type is a
        # publication, not an "other research product", and must be dropped.
        if (
            self.resource_class == "other"
            or self.resource_class not in ALL_RESOURCE_CLASSES
        ):
            self.resource_class = classify_resource_type(self.resource_type)
        self.title = clean_text(self.title, 600) or "Untitled record"
        self.abstract = clean_text(self.abstract, ABSTRACT_LIMIT)
        self.doi = normalize_doi(self.doi)
        self.authors = unique(self.authors)
        self.affiliations = unique(self.affiliations)
        self.parent_orgs = unique(self.parent_orgs)
        self.unit_managers = unique(self.unit_managers)
        self.author_orcids = unique(self.author_orcids)
        self.funding = unique(self.funding)
        # Idempotent: even a delimited blob from a future scraper is split here.
        self.keywords = split_keywords(self.keywords)
        self.source_keys = unique(self.source_keys)
        self.code_repo = clean_text(self.code_repo)
        self.license = clean_text(self.license)
        self.access_rights = clean_text(self.access_rights)
        self.url = clean_text(self.url)
        self.publication_date = to_date_string(self.publication_date)
        self.created_date = to_date_string(self.created_date)
        if not isinstance(self.file_summary, dict):
            self.file_summary = {}
        self.canonical_id = (self.canonical_id or self.source_id or self.url).strip()

    @property
    def window_date(self) -> str:
        """Date used for windowing: repository ingest date, else publication."""
        return self.created_date or self.publication_date

    def to_dict(self) -> Dict[str, Any]:
        """Plain JSON-serializable representation (what the API returns)."""
        data = asdict(self)
        data["window_date"] = self.window_date
        return data


def record_from_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """Turn a SQLite row (JSON encoded columns) back into an API dictionary."""
    import json

    list_fields = (
        "source_keys",
        "authors",
        "affiliations",
        "parent_orgs",
        "unit_managers",
        "author_orcids",
        "funding",
        "keywords",
    )
    record: Dict[str, Any] = {}
    for key, value in row.items():
        if key in list_fields:
            try:
                record[key] = json.loads(value) if value else []
            except (TypeError, ValueError):
                record[key] = []
        elif key == "file_summary":
            try:
                record[key] = json.loads(value) if value else {}
            except (TypeError, ValueError):
                record[key] = {}
        elif value is None:
            record[key] = ""
        else:
            record[key] = value
    record["window_date"] = (
        record.get("created_date") or record.get("publication_date", "")
    )
    return record
