"""Dual persistence: incremental SQLite upserts plus a synchronized JSON mirror.

SQLite is the system of record. ``data/dataset_cache.json`` is rewritten after
every upsert so the dataset is always ready for inspection, ``git diff`` review
or a future batch LLM pipeline.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .config import (
    APP_NAME,
    DB_PATH,
    JSON_MIRROR_PATH,
    MAX_HARVEST_RUNS_HISTORY,
    MAX_RUNS_PER_SCOPE,
    SCHEMA_VERSION,
    SOURCE_LABELS,
    TRACKED_FIELDS,
)
from .models import HarvestRecord, record_from_row, utc_now_iso

logger = logging.getLogger("enac_it4r.harvester")

JSON_FIELDS = (
    "source_keys",
    "authors",
    "affiliations",
    "parent_orgs",
    "unit_managers",
    "author_orcids",
    "funding",
    "keywords",
    "file_summary",
)

#: Fields only Infoscience resolves (EPFL hierarchy, funders, handle URL, ingest
#: date). A single-source snapshot must never blank them on a merged row.
INFOSCIENCE_AUTHORITATIVE_FIELDS = (
    "affiliations",
    "parent_orgs",
    "unit_managers",
    "funding",
    "url",
    "created_date",
)

COLUMNS = (
    "canonical_id",
    "source_id",
    "source",
    "source_keys",
    "resource_type",
    "resource_class",
    "title",
    "abstract",
    "authors",
    "affiliations",
    "parent_orgs",
    "unit_managers",
    "author_orcids",
    "funding",
    "code_repo",
    "file_summary",
    "keywords",
    "license",
    "access_rights",
    "doi",
    "url",
    "publication_date",
    "created_date",
    "first_seen",
    "last_seen",
    "sync_status",
)

# Columns identifying the *query scope* of a run: two runs sharing them are
# repeats of the same request, so a zero-change repeat can be coalesced into the
# row it repeats instead of appending a duplicate.
RUN_SCOPE_FIELDS = (
    "date_from",
    "date_to",
    "resource_type",
    "zenodo_status",
    "infoscience_status",
)

#: Retention key of a query scope: one window only ever needs the previous run
#: and the current one (``MAX_RUNS_PER_SCOPE``), whatever the metadata churn.
RUN_SCOPE_KEYS = ("resource_type", "date_from", "date_to")

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    canonical_id      TEXT PRIMARY KEY,
    source_id         TEXT,
    source            TEXT,
    source_keys       TEXT,
    resource_type     TEXT,
    resource_class    TEXT,
    title             TEXT,
    abstract          TEXT,
    authors           TEXT,
    affiliations      TEXT,
    parent_orgs       TEXT,
    unit_managers     TEXT,
    author_orcids     TEXT,
    funding           TEXT,
    code_repo         TEXT,
    file_summary      TEXT,
    keywords          TEXT,
    license           TEXT,
    access_rights     TEXT,
    doi               TEXT,
    url               TEXT,
    publication_date  TEXT,
    created_date      TEXT,
    first_seen        TEXT,
    last_seen         TEXT,
    sync_status       TEXT
);
CREATE INDEX IF NOT EXISTS idx_records_publication
    ON records(publication_date DESC);
CREATE INDEX IF NOT EXISTS idx_records_created
    ON records(created_date DESC);
CREATE INDEX IF NOT EXISTS idx_records_class
    ON records(resource_class);

CREATE TABLE IF NOT EXISTS harvest_runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at              TEXT NOT NULL,
    date_from           TEXT,
    date_to             TEXT,
    resource_type       TEXT,
    zenodo_status       TEXT,
    infoscience_status  TEXT,
    new_count           INTEGER DEFAULT 0,
    updated_count       INTEGER DEFAULT 0,
    unchanged_count     INTEGER DEFAULT 0,
    zenodo_found        INTEGER DEFAULT 0,
    infoscience_found   INTEGER DEFAULT 0,
    merged_count        INTEGER DEFAULT 0,
    total_records       INTEGER DEFAULT 0,
    duration_seconds    REAL DEFAULT 0,
    notes               TEXT
);
"""


def _encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _json_column(value: Any, default: Any) -> Any:
    """Decode a JSON-encoded column, tolerating blank or legacy values."""
    if isinstance(value, type(default)):
        return value
    try:
        decoded = json.loads(value) if value else default
    except (TypeError, ValueError):
        return default
    return decoded if isinstance(decoded, type(default)) else default


def _scope_value(value: Any) -> str:
    """Comparison key for run scope fields: ``None`` equals the empty string."""
    return "" if value is None else str(value).strip()


class Storage:
    """Thread-safe SQLite store with a derived JSON mirror."""

    def __init__(
        self,
        db_path: Optional[Path] = None,
        mirror_path: Optional[Path] = None,
    ):
        self.db_path = Path(db_path or DB_PATH)
        self.mirror_path = Path(mirror_path or JSON_MIRROR_PATH)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.mirror_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.db_path), check_same_thread=False, timeout=30
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.commit()

    # -- lifecycle --------------------------------------------------------
    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:  # pragma: no cover - defensive
                pass

    def __enter__(self) -> "Storage":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # -- serialization ----------------------------------------------------
    @staticmethod
    def _row_values(record: HarvestRecord) -> List[Any]:
        data = record.to_dict()
        values: List[Any] = []
        for column in COLUMNS:
            value = data.get(column, "")
            values.append(_encode(value) if column in JSON_FIELDS else value)
        return values

    @staticmethod
    def _changed(previous: Dict[str, Any], record: HarvestRecord) -> bool:
        """Compare the tracked fields of a stored row against a fresh record."""
        fresh = record.to_dict()
        for field_name in TRACKED_FIELDS:
            stored = previous.get(field_name)
            incoming = fresh.get(field_name)
            if field_name in JSON_FIELDS:
                if _json_column(stored, []) != list(incoming or []):
                    return True
            elif (stored or "") != (incoming or ""):
                return True
        return False

    @staticmethod
    def _preserve_merged(previous: Dict[str, Any], record: HarvestRecord) -> None:
        """Keep what a merged row knows when a single-source snapshot arrives.

        A partial run — Infoscience stopped by its 300-record page cap, Zenodo
        answering 429, a narrower window — must not wipe the EPFL hierarchy,
        funding or file inventory that the ``Infoscience + Zenodo`` row holds.
        Only a strict superset of the incoming ``source_keys`` triggers this.
        """
        incoming_keys = list(record.source_keys)
        stored_keys = _json_column(previous.get("source_keys"), [])
        if not set(stored_keys) > set(incoming_keys):
            return
        zenodo_only = "infoscience" not in incoming_keys
        record.source = previous.get("source") or record.source
        record.source_keys = stored_keys
        for field_name in INFOSCIENCE_AUTHORITATIVE_FIELDS:
            default = [] if field_name in JSON_FIELDS else ""
            stored_value = _json_column(previous.get(field_name), default)
            if stored_value and (zenodo_only or not getattr(record, field_name)):
                setattr(record, field_name, stored_value)
        if not record.file_summary.get("count"):
            stored_files = _json_column(previous.get("file_summary"), {})
            if stored_files:
                record.file_summary = stored_files

    # -- writes -----------------------------------------------------------
    def upsert_records(self, records: Iterable[HarvestRecord]) -> Dict[str, int]:
        """Insert new rows, update changed rows, keep older history intact."""
        now = utc_now_iso()
        counts = {"new": 0, "updated": 0, "unchanged": 0}
        with self._lock:
            cursor = self._conn.cursor()
            for record in records:
                if not record.canonical_id:
                    continue
                existing = cursor.execute(
                    "SELECT * FROM records WHERE canonical_id = ?",
                    (record.canonical_id,),
                ).fetchone()

                if existing is None:
                    record.first_seen = record.first_seen or now
                    record.last_seen = now
                    record.sync_status = "new"
                    placeholders = ", ".join("?" for _ in COLUMNS)
                    cursor.execute(
                        f"INSERT INTO records ({', '.join(COLUMNS)}) "
                        f"VALUES ({placeholders})",
                        self._row_values(record),
                    )
                    counts["new"] += 1
                    continue

                previous = dict(existing)
                # A merged row outranks a single-source snapshot (see _preserve_merged).
                self._preserve_merged(previous, record)
                record.first_seen = previous.get("first_seen") or now
                record.last_seen = now

                if self._changed(previous, record):
                    record.sync_status = "updated"
                    assignments = ", ".join(
                        f"{column} = ?" for column in COLUMNS
                        if column != "canonical_id"
                    )
                    values = [
                        value
                        for column, value in zip(COLUMNS, self._row_values(record))
                        if column != "canonical_id"
                    ]
                    cursor.execute(
                        f"UPDATE records SET {assignments} WHERE canonical_id = ?",
                        values + [record.canonical_id],
                    )
                    counts["updated"] += 1
                else:
                    record.sync_status = "unchanged"
                    cursor.execute(
                        "UPDATE records SET last_seen = ?, sync_status = ? "
                        "WHERE canonical_id = ?",
                        (now, "unchanged", record.canonical_id),
                    )
                    counts["unchanged"] += 1
            self._conn.commit()
        return counts

    # -- run audit trail --------------------------------------------------
    @staticmethod
    def _is_zero_change(run: Dict[str, Any]) -> bool:
        """True when an execution neither inserted nor updated a record."""
        return (
            int(run.get("new_count") or 0) == 0
            and int(run.get("updated_count") or 0) == 0
        )

    @staticmethod
    def _same_scope(stored: Dict[str, Any], run: Dict[str, Any]) -> bool:
        """True when ``run`` repeats the query scope of a stored row."""
        for field_name in RUN_SCOPE_FIELDS:
            if _scope_value(stored.get(field_name)) != _scope_value(run.get(field_name)):
                return False
        return True

    @classmethod
    def _coalescible(cls, stored: Dict[str, Any], run: Dict[str, Any]) -> bool:
        """True when ``run`` may refresh ``stored`` instead of being inserted.

        Both sides must be zero-change: coalescing into a row that still carries
        ``new_count`` / ``updated_count`` would relabel an older change epoch as
        the latest run and make the Hub Metrics tiles report stale counts.
        """
        return (
            cls._is_zero_change(run)
            and cls._is_zero_change(stored)
            and cls._same_scope(stored, run)
        )

    def _prune_scope_runs(self, run: Dict[str, Any]) -> int:
        """Keep only the newest ``MAX_RUNS_PER_SCOPE`` runs of this query scope."""
        scope = tuple(_scope_value(run.get(field)) for field in RUN_SCOPE_KEYS)
        where = " AND ".join(f"COALESCE({field}, '') = ?" for field in RUN_SCOPE_KEYS)
        return self._conn.execute(
            f"DELETE FROM harvest_runs WHERE {where} AND id NOT IN "
            f"(SELECT id FROM harvest_runs WHERE {where} ORDER BY id DESC LIMIT ?)",
            scope + scope + (MAX_RUNS_PER_SCOPE,),
        ).rowcount

    @staticmethod
    def _log_retention(pruned: int) -> None:
        """Log a retention sweep that actually deleted rows."""
        if pruned > 0:
            logger.info(
                "harvest_runs retention: pruned %d row(s) (newest %d per scope, "
                "%d total)",
                pruned,
                MAX_RUNS_PER_SCOPE,
                MAX_HARVEST_RUNS_HISTORY,
            )

    def record_run(self, run: Dict[str, Any]) -> Dict[str, Any]:
        """Record one harvest execution, coalescing repeated zero-change runs.

        Clicking "Run Scraper" again on an unchanged window must not append a
        duplicate row: a zero-change run whose scope (``RUN_SCOPE_FIELDS``)
        matches an equally zero-change newest row refreshes that row's
        ``run_at``, ``duration_seconds`` and ``total_records`` instead. Every
        write then keeps at most ``MAX_RUNS_PER_SCOPE`` rows for the same
        ``(resource_type, date_from, date_to)`` scope — previous and current —
        and trims the table back to ``MAX_HARVEST_RUNS_HISTORY`` rows overall.

        Returns ``{"id": <row id>, "coalesced": <bool>}``.
        """
        columns = (
            "run_at",
            "date_from",
            "date_to",
            "resource_type",
            "zenodo_status",
            "infoscience_status",
            "new_count",
            "updated_count",
            "unchanged_count",
            "zenodo_found",
            "infoscience_found",
            "merged_count",
            "total_records",
            "duration_seconds",
            "notes",
        )
        values = [run.get(column) for column in columns]
        values[0] = values[0] or utc_now_iso()
        with self._lock:
            latest = self._conn.execute(
                "SELECT * FROM harvest_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()

            if latest is not None and self._coalescible(dict(latest), run):
                row_id = int(latest["id"])
                self._conn.execute(
                    "UPDATE harvest_runs SET run_at = ?, duration_seconds = ?, "
                    "total_records = ? WHERE id = ?",
                    (
                        values[0],
                        run.get("duration_seconds") or 0,
                        run.get("total_records") or 0,
                        row_id,
                    ),
                )
                pruned = self._prune_scope_runs(run)
                self._conn.commit()
                logger.info(
                    "Repeated zero-change harvest coalesced into run #%d "
                    "(window %s -> %s, %s)",
                    row_id,
                    run.get("date_from") or "*",
                    run.get("date_to") or "*",
                    run.get("resource_type") or "all",
                )
                self._log_retention(pruned)
                return {"id": row_id, "coalesced": True}

            cursor = self._conn.execute(
                f"INSERT INTO harvest_runs ({', '.join(columns)}) "
                f"VALUES ({', '.join('?' for _ in columns)})",
                values,
            )
            row_id = int(cursor.lastrowid or 0)
            pruned = self._conn.execute(
                "DELETE FROM harvest_runs WHERE id NOT IN "
                "(SELECT id FROM harvest_runs ORDER BY id DESC LIMIT ?)",
                (MAX_HARVEST_RUNS_HISTORY,),
            ).rowcount + self._prune_scope_runs(run)
            self._conn.commit()

        self._log_retention(pruned)
        return {"id": row_id, "coalesced": False}

    # -- reads ------------------------------------------------------------
    def load_records(self) -> List[Dict[str, Any]]:
        """Return every stored record, newest repository date first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM records ORDER BY "
                "COALESCE(NULLIF(created_date, ''), publication_date) DESC, "
                "title COLLATE NOCASE ASC"
            ).fetchall()
        return [record_from_row(dict(row)) for row in rows]

    def load_runs(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Return the most recent harvest executions, newest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM harvest_runs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def count(self) -> int:
        """Number of records currently stored."""
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS total FROM records").fetchone()
        return int(row["total"]) if row else 0

    def last_run(self) -> Optional[Dict[str, Any]]:
        """Most recent harvest execution, or ``None``."""
        runs = self.load_runs(limit=1)
        return runs[0] if runs else None

    # -- JSON mirror ------------------------------------------------------
    def export_json_mirror(
        self, run: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Rewrite ``data/dataset_cache.json`` from the current SQLite snapshot."""
        items = self.load_records()
        items.sort(
            key=lambda item: (item.get("window_date") or "", item.get("title") or ""),
            reverse=True,
        )

        counts_by_source: Dict[str, int] = {}
        counts_by_class: Dict[str, int] = {}
        for item in items:
            for key in item.get("source_keys") or []:
                counts_by_source[key] = counts_by_source.get(key, 0) + 1
            resource_class = item.get("resource_class") or "other"
            counts_by_class[resource_class] = counts_by_class.get(resource_class, 0) + 1

        payload = {
            "generated_at": utc_now_iso(),
            "schema_version": SCHEMA_VERSION,
            "app": APP_NAME,
            "record_count": len(items),
            "counts_by_source": counts_by_source,
            "counts_by_resource_class": counts_by_class,
            "source_labels": SOURCE_LABELS,
            "last_run": run or self.last_run(),
            "items": items,
        }

        temporary = self.mirror_path.with_name(self.mirror_path.name + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temporary, self.mirror_path)
        logger.info(
            "JSON mirror synchronized: %s (%d records)",
            self.mirror_path.name,
            len(items),
        )
        return {
            "path": str(self.mirror_path),
            "record_count": len(items),
            "counts_by_source": counts_by_source,
        }


# ---------------------------------------------------------------------------
# Process-wide singleton
# ---------------------------------------------------------------------------
_default_storage: Optional[Storage] = None
_singleton_lock = threading.Lock()


def get_storage() -> Storage:
    """Return the process-wide :class:`Storage` instance (created lazily)."""
    global _default_storage
    with _singleton_lock:
        if _default_storage is None:
            _default_storage = Storage()
    return _default_storage
