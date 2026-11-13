"""Harvest orchestration: parallel fetch, DOI de-duplication and persistence.

The two repositories are queried concurrently in isolated workers. Whatever
happens upstream, the orchestrator always returns a usable payload: a 429 on
Infoscience keeps the Zenodo results, and vice versa.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from typing import Any, Dict, Iterable, List, Tuple

from .config import (
    ALL_RESOURCE_CLASSES,
    DEFAULT_RESOURCE_FILTER,
    RESOURCE_FILTERS,
    SOURCE_LABELS,
)
from .models import HarvestRecord, parse_iso_date, unique, utc_now_iso
from .scrapers import (
    STATUS_ERROR,
    FetchResult,
)
from .scrapers.infoscience import fetch_infoscience_records
from .scrapers.zenodo import fetch_zenodo_records
from .storage import get_storage

logger = logging.getLogger("enac_it4r.harvester")

_run_lock = threading.Lock()


class HarvestBusyError(RuntimeError):
    """Raised when a second harvest is requested while one is running."""


# ---------------------------------------------------------------------------
# Window helpers
# ---------------------------------------------------------------------------
def resolve_window(
    date_from: Any = None, date_to: Any = None, default_days: int = 7
) -> Tuple[date, date]:
    """Validate the requested window and return an ordered ``(from, to)`` pair."""
    end = parse_iso_date(date_to) or date.today()
    start = parse_iso_date(date_from)
    if start is None:
        start = end - timedelta(days=max(default_days - 1, 0))
    if start > end:
        start, end = end, start
    return start, end


# ---------------------------------------------------------------------------
# Cross-repository merging
# ---------------------------------------------------------------------------
def _preferred_text(primary: str, secondary: str) -> str:
    if primary and primary != "Untitled record":
        return primary
    return secondary or primary


_PAREN_SUFFIX_RE = re.compile(r"\s*\([^()]*\)\s*$")


def _funding_key(label: str) -> str:
    """Comparison key: case-folded label without a trailing ``(ACRONYM)``."""
    return _PAREN_SUFFIX_RE.sub("", label).strip().casefold()


def _merge_funding(infoscience: List[str], zenodo: List[str]) -> List[str]:
    """Infoscience funding wins; Zenodo only adds grants it has not listed yet.

    Zenodo appends the programme acronym (``... Exascale (EoCoE-III)``) where
    Infoscience holds the plain title, so labels are compared on their
    acronym-stripped, case-folded base and either substring counts as the same
    grant.
    """
    if not infoscience:
        return unique(zenodo)
    labels = unique(infoscience)
    keys = [key for key in map(_funding_key, labels) if key]
    for label in zenodo:
        key = _funding_key(label)
        if key and not any(key in known or known in key for known in keys):
            labels.append(label)
            keys.append(key)
    return labels


def _merge_pair(
    infoscience: HarvestRecord, zenodo: HarvestRecord
) -> HarvestRecord:
    """Merge one Infoscience record with its Zenodo twin (same DOI)."""
    resource_type = infoscience.resource_type
    if resource_type == "other" and zenodo.resource_type != "other":
        resource_type = zenodo.resource_type

    source_id = infoscience.source_id or zenodo.source_id
    return HarvestRecord(
        canonical_id=infoscience.canonical_id,
        source_id=source_id,
        source=SOURCE_LABELS["merged"],
        source_keys=["infoscience", "zenodo"],
        resource_type=resource_type,
        resource_class=infoscience.resource_class
        if infoscience.resource_class != "other"
        else zenodo.resource_class,
        title=_preferred_text(infoscience.title, zenodo.title),
        abstract=_preferred_text(infoscience.abstract, zenodo.abstract),
        authors=infoscience.authors or zenodo.authors,
        affiliations=infoscience.affiliations or zenodo.affiliations,
        parent_orgs=infoscience.parent_orgs,
        unit_managers=infoscience.unit_managers,
        author_orcids=unique(
            list(infoscience.author_orcids) + list(zenodo.author_orcids)
        ),
        funding=_merge_funding(infoscience.funding, zenodo.funding),
        code_repo=zenodo.code_repo or infoscience.code_repo,
        file_summary=zenodo.file_summary
        if zenodo.file_summary.get("count")
        else infoscience.file_summary,
        keywords=unique(list(infoscience.keywords) + list(zenodo.keywords)),
        license=infoscience.license or zenodo.license,
        access_rights=infoscience.access_rights or zenodo.access_rights,
        doi=infoscience.doi or zenodo.doi,
        url=infoscience.url or zenodo.url,
        # Zenodo always exposes a full YYYY-MM-DD publication date.
        publication_date=zenodo.publication_date or infoscience.publication_date,
        created_date=infoscience.created_date or zenodo.created_date,
    )


def _listable(records: Iterable[HarvestRecord]) -> List[HarvestRecord]:
    """Keep only the classes the UI can list, dropping publications.

    ``classify_resource_type`` marks journal articles, theses, patents and
    student work as ``publication``, which is outside ``ALL_RESOURCE_CLASSES``:
    such a record must never create or refresh a row.
    """
    kept: List[HarvestRecord] = []
    dropped: Dict[str, int] = {}
    for record in records:
        if record.resource_class in ALL_RESOURCE_CLASSES:
            kept.append(record)
        else:
            dropped[record.resource_class] = dropped.get(record.resource_class, 0) + 1
    if dropped:
        logger.info(
            "Dropped %d record(s) outside the UI taxonomy: %s",
            sum(dropped.values()),
            ", ".join(f"{name}={count}" for name, count in sorted(dropped.items())),
        )
    return kept


def merge_records(
    zenodo_records: Iterable[HarvestRecord],
    infoscience_records: Iterable[HarvestRecord],
) -> Tuple[List[HarvestRecord], int]:
    """Drop publications, de-duplicate by canonical id, merge DOI twins."""
    zenodo_index: Dict[str, HarvestRecord] = {}
    zenodo_order: List[str] = []
    for record in _listable(zenodo_records):
        key = record.canonical_id
        existing = zenodo_index.get(key)
        if existing is None:
            zenodo_index[key] = record
            zenodo_order.append(key)
        elif record.created_date > existing.created_date:
            zenodo_index[key] = record

    combined: List[HarvestRecord] = []
    consumed: set = set()
    merged_count = 0

    for record in _listable(infoscience_records):
        key = record.canonical_id
        if key in consumed:
            continue
        twin = zenodo_index.get(key)
        if twin is not None:
            combined.append(_merge_pair(record, twin))
            merged_count += 1
        else:
            combined.append(record)
        consumed.add(key)

    for key in zenodo_order:
        if key not in consumed:
            combined.append(zenodo_index[key])
    return combined, merged_count


# ---------------------------------------------------------------------------
# Isolated worker wrappers
# ---------------------------------------------------------------------------
def _safe_fetch(fetcher: Any, source_label: str, *args: Any) -> FetchResult:
    """Never let one repository's exception reach the orchestrator."""
    try:
        return fetcher(*args)
    except Exception as exc:  # pragma: no cover - defensive isolation
        logger.exception("%s scraper crashed", source_label)
        return FetchResult(
            source=source_label,
            status=STATUS_ERROR,
            message=f"unhandled scraper error: {exc}",
        )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def execute_harvest(
    date_from: Any = None,
    date_to: Any = None,
    resource_filter: str = DEFAULT_RESOURCE_FILTER,
    default_days: int = 7,
) -> Dict[str, Any]:
    """Run a full harvest, persist it and return the refreshed dataset payload."""
    if resource_filter not in RESOURCE_FILTERS:
        resource_filter = DEFAULT_RESOURCE_FILTER

    start_date, end_date = resolve_window(date_from, date_to, default_days)
    window_from = start_date.isoformat()
    window_to = end_date.isoformat()
    days = (end_date - start_date).days + 1
    spec = RESOURCE_FILTERS[resource_filter]

    if not _run_lock.acquire(blocking=False):
        raise HarvestBusyError("A harvest is already running - please wait for it to finish.")

    started = time.perf_counter()
    try:
        logger.info(
            "Harvest requested: %s | window %s -> %s (%d day(s))",
            spec["label"],
            window_from,
            window_to,
            days,
        )

        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="harvester") as pool:
            zenodo_future = pool.submit(
                _safe_fetch,
                fetch_zenodo_records,
                "zenodo",
                window_from,
                window_to,
                resource_filter,
            )
            infoscience_future = pool.submit(
                _safe_fetch,
                fetch_infoscience_records,
                "infoscience",
                window_from,
                window_to,
                resource_filter,
            )
            zenodo_result = zenodo_future.result()
            infoscience_result = infoscience_future.result()

        logger.info(
            "Fetch phase finished: Zenodo=%s (%d), Infoscience=%s (%d)",
            zenodo_result.status,
            zenodo_result.found,
            infoscience_result.status,
            infoscience_result.found,
        )

        records, merged_count = merge_records(
            zenodo_result.records, infoscience_result.records
        )
        if merged_count:
            logger.info(
                "DOI de-duplication: %d record(s) merged across Infoscience and Zenodo",
                merged_count,
            )

        storage = get_storage()
        counts = storage.upsert_records(records)
        duration = round(time.perf_counter() - started, 2)
        total_records = storage.count()

        run = {
            "run_at": utc_now_iso(),
            "date_from": window_from,
            "date_to": window_to,
            "resource_type": resource_filter,
            "zenodo_status": zenodo_result.status,
            "infoscience_status": infoscience_result.status,
            "new_count": counts["new"],
            "updated_count": counts["updated"],
            "unchanged_count": counts["unchanged"],
            "zenodo_found": zenodo_result.found,
            "infoscience_found": infoscience_result.found,
            "merged_count": merged_count,
            "total_records": total_records,
            "duration_seconds": duration,
            "notes": "; ".join(
                message
                for message in (zenodo_result.message, infoscience_result.message)
                if message
            )[:500],
        }
        storage.record_run(run)
        mirror = storage.export_json_mirror(run)

        logger.info(
            "Harvest complete in %.2fs: +%d new, ~%d updated, =%d unchanged | "
            "database total: %d | mirror: %s",
            duration,
            counts["new"],
            counts["updated"],
            counts["unchanged"],
            total_records,
            mirror.get("path", ""),
        )

        return {
            "run": run,
            "run_at": run["run_at"],
            "date_from": window_from,
            "date_to": window_to,
            "days": days,
            "resource_filter": resource_filter,
            "resource_label": spec["label"],
            "duration_seconds": duration,
            "sources": {
                "zenodo": zenodo_result.summary(),
                "infoscience": infoscience_result.summary(),
            },
            "counts": {
                "fetched": len(records),
                "new": counts["new"],
                "updated": counts["updated"],
                "unchanged": counts["unchanged"],
                "merged": merged_count,
                "stored": total_records,
            },
            "mirror": mirror,
            "items": storage.load_records(),
        }
    finally:
        _run_lock.release()
