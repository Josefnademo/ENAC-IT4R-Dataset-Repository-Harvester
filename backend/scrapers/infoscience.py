"""EPFL Infoscience (DSpace 7 CRIS) client.

Verified protocol rules (see docs/TECHNICAL_ARCHITECTURE.md section 5):
  * pagination is 0-indexed and ``size`` must never exceed 100, otherwise DSpace
    clamps the payload but not the offset and silently skips records;
  * Solr syntax in ``query=`` with ``configuration=researchoutputs``;
  * CloudFront bans the whole ``/server/api`` host for ~15 minutes after a burst
    of ~13 calls in two minutes -> 6.5 s pacing plus a fail-fast circuit breaker;
  * AWS WAF Bot Control may answer with a CAPTCHA challenge that is replayed
    through the OS-native TLS stack (curl).
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import requests

from ..config import (
    CURL_BINARY,
    INFOSCIENCE_BASE,
    INFOSCIENCE_CONFIGURATION,
    INFOSCIENCE_DELAY_SECONDS,
    INFOSCIENCE_FIRST_PAGE,
    INFOSCIENCE_MAX_PAGES,
    INFOSCIENCE_PAGE_SIZE,
    INFOSCIENCE_SEARCH_URL,
    INFOSCIENCE_SORT,
    REQUEST_HEADERS,
    REQUEST_TIMEOUT,
    RESOURCE_FILTERS,
    WAF_ACTION_HEADER,
    WAF_CHALLENGE_STATUSES,
)
from ..models import (
    HarvestRecord,
    classify_resource_type,
    clean_text,
    extract_code_repo_url,
    normalize_code_repo_url,
    normalize_doi,
    split_keywords,
    to_date_string,
    unique,
)
from . import STATUS_ERROR, STATUS_OK, STATUS_RATE_LIMITED, FetchResult

logger = logging.getLogger("enac_it4r.harvester")

SOURCE_KEY = "infoscience"
SOURCE_LABEL = "Infoscience (DSpace 7)"

#: Browser UA used only for the curl replay (the WAF expects a real browser).
CURL_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# Query building
# ---------------------------------------------------------------------------
def build_query(date_from: str, date_to: str, resource_filter: str) -> str:
    """Compose the Solr query: type clause AND the accessioned-date window."""
    spec = RESOURCE_FILTERS.get(resource_filter) or RESOURCE_FILTERS["dataset"]
    clauses: List[str] = []
    type_query = (spec.get("infoscience_query") or "").strip()
    if type_query:
        clauses.append(type_query)
    clauses.append(
        f"dc.date.accessioned:[{date_from}T00:00:00Z TO {date_to}T23:59:59Z]"
    )
    return " AND ".join(clauses)


# ---------------------------------------------------------------------------
# Transport: requests first, curl replay on an AWS WAF challenge
# ---------------------------------------------------------------------------
def _is_waf_challenge(response: requests.Response) -> bool:
    action = (response.headers.get(WAF_ACTION_HEADER) or "").strip().lower()
    if action:
        return True
    if response.status_code in WAF_CHALLENGE_STATUSES:
        body = (response.text or "").lower()
        return "captcha" in body or "awswaf" in body or "waf" in body
    return False


def _curl_get(url: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Replay a request through the OS curl binary (native TLS stack)."""
    binary = shutil.which(CURL_BINARY)
    if not binary and Path(CURL_BINARY).exists():
        binary = CURL_BINARY
    if not binary:
        logger.error(
            "Infoscience: curl fallback unavailable ('%s' not found)", CURL_BINARY
        )
        return None

    full_url = f"{url}?{urlencode(params)}"
    command = [
        binary,
        "-sS",
        "--compressed",
        "--max-time",
        str(REQUEST_TIMEOUT),
        "-H",
        "Accept: application/json",
        "-H",
        f"User-Agent: {CURL_USER_AGENT}",
        full_url,
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=REQUEST_TIMEOUT + 20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.error("Infoscience: curl fallback failed to start: %s", exc)
        return None

    if completed.returncode != 0:
        logger.error(
            "Infoscience: curl fallback exit %s: %s",
            completed.returncode,
            (completed.stderr or "").strip()[:200],
        )
        return None

    try:
        return json.loads(completed.stdout)
    except ValueError as exc:
        logger.error("Infoscience: curl fallback returned invalid JSON: %s", exc)
        return None


def _retry_after(response: requests.Response) -> int:
    raw = response.headers.get("Retry-After")
    try:
        return int(float(raw)) if raw is not None else 0
    except (TypeError, ValueError):
        return 0


def _request_page(
    session: requests.Session, params: Dict[str, Any], page: int
) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    """Fetch one page and return ``(payload, transport info)``."""
    info: Dict[str, Any] = {
        "status": 0,
        "retry_after": 0,
        "error": "",
        "transport": "requests",
    }
    try:
        response = session.get(
            INFOSCIENCE_SEARCH_URL, params=params, timeout=REQUEST_TIMEOUT
        )
    except requests.RequestException as exc:
        info["error"] = f"transport error on page {page}: {exc}"
        logger.error("Infoscience: %s", info["error"])
        return None, info

    info["status"] = response.status_code

    if _is_waf_challenge(response):
        info["transport"] = "curl"
        logger.warning(
            "Infoscience: AWS WAF challenge on page %d (HTTP %d, %s=%s) - "
            "replaying via curl",
            page,
            response.status_code,
            WAF_ACTION_HEADER,
            response.headers.get(WAF_ACTION_HEADER),
        )
        payload = _curl_get(INFOSCIENCE_SEARCH_URL, params)
        if payload is not None:
            info["status"] = 200
            logger.info("Infoscience: curl replay succeeded on page %d", page)
            return payload, info
        info["error"] = "WAF CAPTCHA challenge and curl replay both failed"
        return None, info

    if response.status_code == 429:
        info["retry_after"] = _retry_after(response)
        info["error"] = (
            f"HTTP 429 CloudFront burst ban on page {page} "
            f"(Retry-After={info['retry_after']}s)"
        )
        return None, info

    if response.status_code >= 400:
        info["error"] = (
            f"HTTP {response.status_code} on page {page}: {response.text[:200]}"
        )
        return None, info

    try:
        return response.json(), info
    except ValueError as exc:
        info["error"] = f"invalid JSON on page {page}: {exc}"
        return None, info


# ---------------------------------------------------------------------------
# Metadata helpers
# ---------------------------------------------------------------------------
def _entries(metadata: Dict[str, Any], key: str) -> List[Any]:
    """Return the raw DSpace metadata entries for one field key."""
    raw = metadata.get(key)
    if raw is None:
        return []
    if isinstance(raw, dict):
        return [raw]
    if isinstance(raw, list):
        return raw
    return [raw]


def _values(metadata: Dict[str, Any], key: str) -> List[str]:
    """Return cleaned, de-duplicated values; CRIS placeholders are dropped."""
    values: List[Any] = []
    for entry in _entries(metadata, key):
        if isinstance(entry, dict):
            values.append(entry.get("value"))
        else:
            values.append(entry)
    return unique(values)


def _first(metadata: Dict[str, Any], key: str) -> str:
    values = _values(metadata, key)
    return values[0] if values else ""


def _code_repository(metadata: Dict[str, Any]) -> str:
    """Resolve ``code_repo``: ``epfl.url`` first, then ``datacite.relatedIdentifier``.

    ``epfl.url`` is authoritative, but the CRIS only fills it for some record
    types; projects such as Solidipes publish the repository in the DataCite
    related identifiers instead. Non-forge links (DOI, Zenodo, publishers) are
    rejected by :func:`normalize_code_repo_url`.
    """
    urls = _entries(metadata, "epfl.url")
    descriptions = _entries(metadata, "epfl.url.description")
    for index, entry in enumerate(urls):
        url = clean_text(entry.get("value") if isinstance(entry, dict) else entry)
        if not url:
            continue
        code_url = normalize_code_repo_url(url)
        if code_url:
            return code_url
        # Any other URL only counts as a repository when the CRIS says it is one.
        description = descriptions[index] if index < len(descriptions) else ""
        descriptor = clean_text(
            description.get("value") if isinstance(description, dict) else description
        ).casefold()
        if any(word in descriptor for word in ("code", "repository", "source")):
            return url.rstrip("/")
    return extract_code_repo_url(_values(metadata, "datacite.relatedIdentifier"))


def _fallback_id(metadata: Dict[str, Any], handle: str, uuid: str) -> str:
    """Stable identifier when a record has neither DOI nor handle/uuid."""
    if handle:
        return f"infoscience-{handle}"
    if uuid:
        return f"infoscience-{uuid}"
    seed = "|".join(
        (
            _first(metadata, "dc.title"),
            _first(metadata, "dc.date.accessioned"),
            _first(metadata, "dc.contributor.author"),
        )
    )
    digest = hashlib.md5(seed.encode("utf-8")).hexdigest()[:12]
    return f"infoscience-{digest}"


def _record_from_object(indexable: Dict[str, Any]) -> HarvestRecord:
    """Map one DSpace ``indexableObject`` onto the canonical record shape."""
    metadata = indexable.get("metadata") or {}
    handle = clean_text(indexable.get("handle"))
    uuid = clean_text(indexable.get("uuid")) or clean_text(indexable.get("id"))
    doi = normalize_doi(_first(metadata, "dc.identifier.doi"))
    resource_type = _first(metadata, "dc.type") or "other"

    url = ""
    if handle:
        url = f"{INFOSCIENCE_BASE}/handle/{handle}"
    if not url:
        url = _first(metadata, "dc.identifier.uri")
    if not url:
        url = INFOSCIENCE_BASE

    license_value = _first(metadata, "ctb.oaireXXlicenseCondition") or _first(
        metadata, "oaire.licenseCondition"
    )
    publication_date = _first(metadata, "dc.date.issued") or _first(
        metadata, "dc.date.accessioned"
    )

    source_id = _fallback_id(metadata, handle, uuid)
    return HarvestRecord(
        canonical_id=doi or source_id,
        source_id=source_id,
        source=SOURCE_LABEL,
        source_keys=[SOURCE_KEY],
        resource_type=resource_type,
        resource_class=classify_resource_type(resource_type),
        title=_first(metadata, "dc.title"),
        abstract=_first(metadata, "dc.description.abstract"),
        authors=_values(metadata, "dc.contributor.author"),
        affiliations=_values(metadata, "cris.virtual.department"),
        parent_orgs=_values(metadata, "cris.virtual.parent-organization"),
        unit_managers=_values(metadata, "cris.virtual.unitManager"),
        author_orcids=_values(metadata, "epfl.author.orcid"),
        funding=_values(metadata, "dc.relation.funding"),
        code_repo=_code_repository(metadata),
        file_summary={},
        keywords=split_keywords(_values(metadata, "dc.subject")),
        license=license_value,
        access_rights=_first(metadata, "datacite.rights"),
        doi=doi,
        url=url,
        publication_date=to_date_string(publication_date),
        created_date=to_date_string(_first(metadata, "dc.date.accessioned")),
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def _page_parts(payload: Dict[str, Any]) -> Tuple[Dict[str, Any], List[Any]]:
    """Extract the page metadata and object list from a discover response."""
    search_result = (payload.get("_embedded") or {}).get("searchResult") or {}
    page_info = search_result.get("page") or {}
    objects = (search_result.get("_embedded") or {}).get("objects") or []
    return page_info, objects


def fetch_infoscience_records(
    date_from: str,
    date_to: str,
    resource_filter: str = "dataset",
    max_pages: Optional[int] = None,
) -> FetchResult:
    """Harvest EPFL Infoscience for one date window and type filter."""
    query = build_query(date_from, date_to, resource_filter)
    page_limit = max_pages or INFOSCIENCE_MAX_PAGES
    result = FetchResult(source=SOURCE_KEY)
    logger.info(
        "Infoscience: start (window %s -> %s, configuration=%s, query=%s, "
        "size=%d, sort=%s)",
        date_from,
        date_to,
        INFOSCIENCE_CONFIGURATION,
        query,
        INFOSCIENCE_PAGE_SIZE,
        INFOSCIENCE_SORT,
    )
    logger.info(
        "Infoscience: CloudFront guard armed - %.1fs between pages, max %d "
        "page(s), fail-fast on the first HTTP 429",
        INFOSCIENCE_DELAY_SECONDS,
        page_limit,
    )

    try:
        with requests.Session() as session:
            session.headers.update(REQUEST_HEADERS)
            page = INFOSCIENCE_FIRST_PAGE
            while page < page_limit:
                params = {
                    "query": query,
                    "configuration": INFOSCIENCE_CONFIGURATION,
                    "size": INFOSCIENCE_PAGE_SIZE,
                    "page": page,
                    "sort": INFOSCIENCE_SORT,
                }
                payload, info = _request_page(session, params, page)

                if payload is None:
                    if info.get("status") == 429:
                        result.status = STATUS_RATE_LIMITED
                        result.retry_after = int(info.get("retry_after") or 0)
                        result.message = (
                            f"{info.get('error')} - circuit breaker tripped: "
                            "Infoscience aborted, Zenodo results retained"
                        )
                        logger.error("Infoscience: %s", result.message)
                    else:
                        result.status = STATUS_ERROR
                        result.message = info.get("error") or "unknown failure"
                        logger.error("Infoscience: %s", result.message)
                    break

                page_info, objects = _page_parts(payload)
                total_elements = page_info.get("totalElements")
                total_pages = page_info.get("totalPages")

                parsed = 0
                for entry in objects:
                    if not isinstance(entry, dict):
                        continue
                    indexable = (entry.get("_embedded") or {}).get("indexableObject")
                    if not isinstance(indexable, dict):
                        continue
                    result.records.append(_record_from_object(indexable))
                    parsed += 1

                result.pages_fetched += 1
                if isinstance(total_elements, int):
                    result.total_available = total_elements

                logger.info(
                    "Infoscience: page %d -> %d records (window total: %s, "
                    "pages: %s, transport: %s)",
                    page,
                    parsed,
                    total_elements if total_elements is not None else "n/a",
                    total_pages if total_pages is not None else "n/a",
                    info.get("transport", "requests"),
                )

                if not objects or parsed == 0:
                    break

                next_page = page + 1
                if isinstance(total_pages, int) and next_page >= total_pages:
                    break
                if (
                    isinstance(total_elements, int)
                    and total_elements > 0
                    and result.found >= total_elements
                ):
                    break

                time.sleep(INFOSCIENCE_DELAY_SECONDS)
                page = next_page
    except Exception as exc:  # pragma: no cover - last-resort isolation
        result.status = STATUS_ERROR
        result.message = f"unexpected error: {exc}"
        logger.exception("Infoscience: unexpected failure")

    if result.status == STATUS_OK:
        logger.info(
            "Infoscience: done - %d records over %d page(s), %d available in window",
            result.found,
            result.pages_fetched,
            result.total_available,
        )
    return result
