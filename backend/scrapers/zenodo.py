"""Zenodo (InvenioRDM) client for the EPFL community.

Verified protocol rules (see docs/TECHNICAL_ARCHITECTURE.md section 4):
  * pagination is 1-indexed (``page=0`` -> HTTP 400);
  * anonymous page size is capped at 25 (``size>25`` -> HTTP 400);
  * Lucene syntax in ``q=`` and ``sort=newest``;
  * 30 requests / 60 s, advertised through ``X-RateLimit-Remaining``.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, List, Optional

import requests

from ..config import (
    REQUEST_HEADERS,
    REQUEST_TIMEOUT,
    RESOURCE_FILTERS,
    ZENODO_COMMUNITY_RECORDS_URL,
    ZENODO_DELAY_SECONDS,
    ZENODO_FIRST_PAGE,
    ZENODO_MAX_PAGES,
    ZENODO_PAGE_SIZE,
    ZENODO_RATE_LIMIT_FLOOR,
    ZENODO_RATE_LIMIT_PAUSE,
    ZENODO_SORT,
)
from ..models import (
    HarvestRecord,
    build_file_summary,
    classify_resource_type,
    clean_text,
    extract_code_repo_from_text,
    extract_code_repo_url,
    normalize_code_repo_url,
    normalize_doi,
    split_keywords,
    to_date_string,
    unique,
)
from . import STATUS_ERROR, STATUS_OK, STATUS_RATE_LIMITED, FetchResult

logger = logging.getLogger("enac_it4r.harvester")

SOURCE_KEY = "zenodo"
SOURCE_LABEL = "Zenodo (EPFL Community)"

#: InvenioRDM relations that most often point at the code of a record, in
#: decreasing order of usefulness. Any other relation is still accepted when its
#: identifier resolves to a known code forge.
CODE_RELATION_PRIORITY = (
    "isversionof",
    "issupplementto",
    "isderivedfrom",
    "isidenticalto",
    "isdocumentedby",
)


# ---------------------------------------------------------------------------
# Query building
# ---------------------------------------------------------------------------
def build_query(date_from: str, date_to: str, resource_filter: str) -> str:
    """Compose the Lucene query: resource type AND creation window."""
    spec = RESOURCE_FILTERS.get(resource_filter) or RESOURCE_FILTERS["dataset"]
    clauses: List[str] = []
    type_query = (spec.get("zenodo_query") or "").strip()
    if type_query:
        clauses.append(type_query)
    clauses.append(f"created:[{date_from} TO {date_to}]")
    return " AND ".join(clauses)


# ---------------------------------------------------------------------------
# Payload helpers
# ---------------------------------------------------------------------------
def _as_text(value: Any) -> str:
    """Read a Zenodo custom field that may be a string, list or dict."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("url", "value", "id", "title"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        return ""
    if isinstance(value, list):
        for entry in value:
            text = _as_text(entry)
            if text:
                return text
    return ""


def _creator_fields(metadata: Dict[str, Any]) -> Dict[str, List[str]]:
    authors: List[str] = []
    affiliations: List[str] = []
    orcids: List[str] = []
    for creator in metadata.get("creators") or []:
        if not isinstance(creator, dict):
            continue
        name = clean_text(creator.get("name"))
        if name:
            authors.append(name)
        affiliation = creator.get("affiliation")
        if isinstance(affiliation, list):
            for entry in affiliation:
                text = clean_text(entry)
                if text:
                    affiliations.append(text)
        else:
            text = clean_text(affiliation)
            if text:
                affiliations.append(text)
        orcid = clean_text(creator.get("orcid"))
        if orcid:
            orcids.append(orcid)
    return {"authors": authors, "affiliations": affiliations, "author_orcids": orcids}


#: Funder grant identifiers (``200021-227992``, ``2024-A0160502224``) are codes,
#: not programme names, so they never become funding labels.
_GRANT_CODE_RE = re.compile(r"\d{4,}")


def _grant_labels(metadata: Dict[str, Any]) -> List[str]:
    """Funding badges from ``metadata.grants``: grant title and/or acronym.

    ``grant.code`` is the funder's internal number and is deliberately never
    emitted; an acronym is only kept when it names a programme (``NCCR SPIN``,
    ``EoCoE-III``), never when it looks like a grant ID.
    """
    labels: List[str] = []
    for grant in metadata.get("grants") or []:
        if not isinstance(grant, dict):
            text = clean_text(grant)
            if text:
                labels.append(text)
            continue
        title = clean_text(grant.get("title"))
        acronym = clean_text(grant.get("acronym"))
        # A grant ID, or an acronym the title already carries, adds nothing.
        if _GRANT_CODE_RE.search(acronym) or acronym.casefold() in title.casefold():
            acronym = ""
        label = f"{title} ({acronym})" if title and acronym else title or acronym
        if label:
            labels.append(label)
    return unique(labels)


def _resource_type(metadata: Dict[str, Any]) -> str:
    resource = metadata.get("resource_type") or {}
    if not isinstance(resource, dict):
        return clean_text(resource) or "other"
    root = clean_text(resource.get("type"))
    subtype = clean_text(resource.get("subtype"))
    if root and subtype and subtype.casefold() not in ("other", root.casefold()):
        return f"{root}::{subtype}"
    return root or "other"


def _license_id(metadata: Dict[str, Any]) -> str:
    value = metadata.get("license")
    if isinstance(value, dict):
        return clean_text(value.get("id") or value.get("title"))
    return clean_text(value)


def _access_rights(metadata: Dict[str, Any]) -> str:
    text = clean_text(metadata.get("access_right"))
    if text:
        return text
    access = metadata.get("access")
    if isinstance(access, dict):
        for key in ("right", "record", "files"):
            candidate = clean_text(access.get(key))
            if candidate:
                return candidate
    return ""


def _relation_key(value: Any) -> str:
    """Normalize a ``related_identifiers[].relation`` for comparison."""
    return re.sub(r"[^a-z0-9]", "", clean_text(value).casefold())


def _related_code_repo(metadata: Dict[str, Any]) -> str:
    """Pick a code-forge URL out of ``metadata.related_identifiers``.

    Entries are grouped by relation so the documented priority
    (``isVersionOf`` -> ``isSupplementTo`` -> ``isDerivedFrom`` ->
    ``isIdenticalTo`` -> ``isDocumentedBy``) wins over array order; every other
    relation is only consulted afterwards, and only when its identifier is a
    URL (DOI identifiers are ignored).
    """
    entries = metadata.get("related_identifiers")
    if isinstance(entries, dict):
        entries = [entries]
    if not isinstance(entries, list):
        return ""

    grouped: Dict[str, List[str]] = {}
    remaining: List[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        identifier = clean_text(entry.get("identifier"))
        if not identifier:
            continue
        scheme = clean_text(entry.get("scheme")).casefold()
        is_url = "://" in identifier or identifier.casefold().startswith("www.")
        if scheme not in ("url", "") and not is_url:
            continue
        relation = _relation_key(entry.get("relation"))
        if relation in CODE_RELATION_PRIORITY:
            grouped.setdefault(relation, []).append(identifier)
        elif is_url:
            remaining.append(identifier)

    for relation in CODE_RELATION_PRIORITY:
        url = extract_code_repo_url(grouped.get(relation, ()))
        if url:
            return url
    return extract_code_repo_url(remaining)


def _code_repo(metadata: Dict[str, Any], custom: Dict[str, Any]) -> str:
    """Resolve ``code_repo``: custom field -> related identifiers -> description.

    ``metadata.custom["code:codeRepository"]`` is only populated for records
    created through Zenodo's GitHub webhook; GitLab (and other self-hosted)
    projects advertise their repository in ``related_identifiers`` or, as a last
    resort, in the free-text description.
    """
    declared = _as_text(custom.get("code:codeRepository"))
    if declared:
        return normalize_code_repo_url(declared) or declared.rstrip("/")
    related = _related_code_repo(metadata)
    if related:
        return related
    return extract_code_repo_from_text(metadata.get("description"))


def _record_from_hit(hit: Dict[str, Any]) -> HarvestRecord:
    """Map one InvenioRDM hit onto the canonical record shape."""
    metadata = hit.get("metadata") or {}
    record_id = hit.get("id") or hit.get("recid") or ""
    source_id = f"zenodo-{record_id}"
    creators = _creator_fields(metadata)
    resource_type = _resource_type(metadata)
    doi = normalize_doi(hit.get("doi") or metadata.get("doi"))
    custom = metadata.get("custom") if isinstance(metadata.get("custom"), dict) else {}
    links = hit.get("links") or {}
    url = clean_text(links.get("self_html")) or (
        f"https://zenodo.org/records/{record_id}" if record_id else ""
    )
    if not url and doi:
        url = f"https://doi.org/{doi}"

    return HarvestRecord(
        canonical_id=doi or source_id,
        source_id=source_id,
        source=SOURCE_LABEL,
        source_keys=[SOURCE_KEY],
        resource_type=resource_type,
        resource_class=classify_resource_type(resource_type),
        title=clean_text(metadata.get("title")),
        abstract=clean_text(metadata.get("description")),
        authors=creators["authors"],
        affiliations=creators["affiliations"],
        parent_orgs=[],
        unit_managers=[],
        author_orcids=creators["author_orcids"],
        funding=_grant_labels(metadata),
        code_repo=_code_repo(metadata, custom),
        file_summary=build_file_summary(hit.get("files")),
        keywords=split_keywords(metadata.get("keywords")),
        license=_license_id(metadata),
        access_rights=_access_rights(metadata),
        doi=doi,
        url=url,
        publication_date=to_date_string(metadata.get("publication_date")),
        created_date=to_date_string(hit.get("created")),
    )


def _rate_limit_remaining(response: requests.Response) -> Optional[int]:
    raw = response.headers.get("X-RateLimit-Remaining")
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _retry_after(response: requests.Response) -> int:
    raw = response.headers.get("Retry-After")
    try:
        return int(float(raw)) if raw is not None else 0
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def fetch_zenodo_records(
    date_from: str,
    date_to: str,
    resource_filter: str = "dataset",
    max_pages: Optional[int] = None,
) -> FetchResult:
    """Harvest the EPFL Zenodo community for one date window and type filter."""
    query = build_query(date_from, date_to, resource_filter)
    page_limit = max_pages or ZENODO_MAX_PAGES
    result = FetchResult(source=SOURCE_KEY)
    logger.info(
        "Zenodo: start (window %s -> %s, q=%s, size=%d, sort=%s)",
        date_from,
        date_to,
        query,
        ZENODO_PAGE_SIZE,
        ZENODO_SORT,
    )

    try:
        with requests.Session() as session:
            session.headers.update(REQUEST_HEADERS)
            page = ZENODO_FIRST_PAGE
            while page <= page_limit:
                params = {
                    "q": query,
                    "size": ZENODO_PAGE_SIZE,
                    "sort": ZENODO_SORT,
                    "page": page,
                }
                try:
                    response = session.get(
                        ZENODO_COMMUNITY_RECORDS_URL,
                        params=params,
                        timeout=REQUEST_TIMEOUT,
                    )
                except requests.RequestException as exc:
                    result.status = STATUS_ERROR
                    result.message = f"transport error on page {page}: {exc}"
                    logger.error("Zenodo: %s", result.message)
                    break

                remaining = _rate_limit_remaining(response)

                if response.status_code == 429:
                    retry_after = _retry_after(response)
                    result.status = STATUS_RATE_LIMITED
                    result.retry_after = retry_after
                    result.message = (
                        f"HTTP 429 rate limit (Retry-After={retry_after}s) on page {page}"
                    )
                    logger.warning("Zenodo: %s - pagination stopped", result.message)
                    break

                if response.status_code >= 400:
                    result.status = STATUS_ERROR
                    result.message = (
                        f"HTTP {response.status_code} on page {page}: "
                        f"{response.text[:180]}"
                    )
                    logger.error("Zenodo: %s", result.message)
                    break

                try:
                    payload = response.json()
                except ValueError as exc:
                    result.status = STATUS_ERROR
                    result.message = f"invalid JSON on page {page}: {exc}"
                    logger.error("Zenodo: %s", result.message)
                    break

                hits_block = payload.get("hits") or {}
                hits = hits_block.get("hits") or []
                try:
                    result.total_available = int(hits_block.get("total") or 0)
                except (TypeError, ValueError):
                    result.total_available = 0

                parsed = 0
                for hit in hits:
                    if not isinstance(hit, dict):
                        continue
                    result.records.append(_record_from_hit(hit))
                    parsed += 1

                result.pages_fetched += 1
                logger.info(
                    "Zenodo: page %d -> %d records (community total: %d, "
                    "rate-limit remaining: %s)",
                    page,
                    parsed,
                    result.total_available,
                    remaining if remaining is not None else "n/a",
                )

                has_next = bool((payload.get("links") or {}).get("next"))
                if not has_next or not hits or parsed == 0:
                    break
                if (
                    result.total_available
                    and result.pages_fetched * ZENODO_PAGE_SIZE >= result.total_available
                ):
                    break

                if remaining is not None and remaining <= ZENODO_RATE_LIMIT_FLOOR:
                    logger.warning(
                        "Zenodo: only %d requests left in the 60 s window - "
                        "pausing %.0fs",
                        remaining,
                        ZENODO_RATE_LIMIT_PAUSE,
                    )
                    time.sleep(ZENODO_RATE_LIMIT_PAUSE)
                else:
                    time.sleep(ZENODO_DELAY_SECONDS)

                page += 1
    except Exception as exc:  # pragma: no cover - last-resort isolation
        result.status = STATUS_ERROR
        result.message = f"unexpected error: {exc}"
        logger.exception("Zenodo: unexpected failure")

    if result.status == STATUS_OK:
        logger.info(
            "Zenodo: done - %d records over %d page(s), %d available in window",
            result.found,
            result.pages_fetched,
            result.total_available,
        )
    return result
