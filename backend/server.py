"""Threading HTTP server: static frontend assets plus a small JSON API.

Only ``GET /api/harvest`` touches the external repositories. ``GET /api/data``
always answers from SQLite, so a browser refresh never triggers upstream calls.
"""

from __future__ import annotations

import json
import logging
import mimetypes
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List
from urllib.parse import parse_qs, urlparse

from .config import (
    ALL_RESOURCE_CLASSES,
    APP_NAME,
    APP_VERSION,
    DEFAULT_RESOURCE_FILTER,
    FRONTEND_DIR,
    HOST,
    PORT,
    RESOURCE_FILTERS,
)
from .harvester import (
    HarvestBusyError,
    execute_harvest,
)
from .logger import get_logger, log_snapshot
from .models import parse_iso_date, utc_now_iso
from .storage import get_storage

logger = logging.getLogger("enac_it4r.harvester")

TEXT_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


def public_filters() -> List[Dict[str, Any]]:
    """Filter taxonomy for the UI, with backend labels as the source of truth."""
    return [
        {
            "key": key,
            "label": spec["label"],
            "pill": spec["pill"],
            "description": spec["description"],
            "classes": spec["classes"],
        }
        for key, spec in RESOURCE_FILTERS.items()
    ]


def _first_value(query: Dict[str, List[str]], key: str, default: str = "") -> str:
    values = query.get(key) or []
    return values[0].strip() if values and values[0] else default


class HarvesterRequestHandler(SimpleHTTPRequestHandler):
    """Serve ``frontend/`` and the ``/api/*`` endpoints."""

    server_version = f"ENAC-IT4R-Harvester/{APP_VERSION}"
    protocol_version = "HTTP/1.1"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(FRONTEND_DIR), **kwargs)

    # -- logging ----------------------------------------------------------
    def log_message(self, fmt: str, *args: Any) -> None:
        logger.debug("%s - %s", self.address_string(), fmt % args)

    # -- routing ----------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 - http.server API
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path.startswith("/api/"):
            self._handle_api(path.rstrip("/"), query)
            return
        self._serve_static(path)

    def do_HEAD(self) -> None:  # noqa: N802 - http.server API
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        self._send_json(
            {"error": "method not allowed", "hint": "use GET /api/harvest"},
            HTTPStatus.METHOD_NOT_ALLOWED,
        )

    # -- helpers ----------------------------------------------------------
    def _send_bytes(
        self, body: bytes, content_type: str, status: int = 200
    ) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            logger.debug("Client disconnected while streaming %s", self.path)

    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_bytes(body, "application/json; charset=utf-8", status)

    def _serve_static(self, path: str) -> None:
        if path in ("", "/"):
            relative = "index.html"
        elif path == "/favicon.ico":
            self._send_bytes(b"", "image/x-icon", HTTPStatus.NO_CONTENT)
            return
        else:
            relative = path.lstrip("/")

        frontend_root = FRONTEND_DIR.resolve()
        target = (frontend_root / relative).resolve()
        try:
            target.relative_to(frontend_root)
        except ValueError:
            self._send_json({"error": "forbidden path"}, HTTPStatus.FORBIDDEN)
            return

        if not target.is_file():
            self._send_json(
                {"error": "not found", "path": path}, HTTPStatus.NOT_FOUND
            )
            return

        suffix = target.suffix.lower()
        content_type = TEXT_CONTENT_TYPES.get(suffix)
        if content_type is None:
            content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        try:
            body = target.read_bytes()
        except OSError as exc:
            logger.error("Cannot read %s: %s", target, exc)
            self._send_json({"error": "cannot read asset"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self._send_bytes(body, content_type)

    # -- API --------------------------------------------------------------
    def _handle_api(self, path: str, query: Dict[str, List[str]]) -> None:
        try:
            if path == "/api/logs":
                self._api_logs(query)
            elif path == "/api/data":
                self._api_data()
            elif path == "/api/harvest":
                self._api_harvest(query)
            elif path == "/api/status":
                self._api_status()
            elif path == "/api/filters":
                self._send_json(
                    {
                        "filters": public_filters(),
                        "default": DEFAULT_RESOURCE_FILTER,
                        "resource_classes": ALL_RESOURCE_CLASSES,
                    }
                )
            else:
                self._send_json(
                    {"error": "unknown endpoint", "path": path},
                    HTTPStatus.NOT_FOUND,
                )
        except (BrokenPipeError, ConnectionResetError):
            logger.debug("Client disconnected from %s", path)
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("Unhandled API error on %s", path)
            self._send_json(
                {"error": "internal server error", "detail": str(exc)},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def _api_logs(self, query: Dict[str, List[str]]) -> None:
        raw_since = _first_value(query, "since", "0")
        try:
            since = max(int(raw_since), 0)
        except (TypeError, ValueError):
            since = 0
        self._send_json(log_snapshot(since))

    def _api_data(self) -> None:
        storage = get_storage()
        items = storage.load_records()
        self._send_json(
            {
                "generated_at": utc_now_iso(),
                "count": len(items),
                "items": items,
                "runs": storage.load_runs(10),
                "last_run": storage.last_run(),
                "filters": public_filters(),
                "default_filter": DEFAULT_RESOURCE_FILTER,
                "resource_classes": ALL_RESOURCE_CLASSES,
                "source": "sqlite",
            }
        )

    def _api_status(self) -> None:
        storage = get_storage()
        self._send_json(
            {
                "app": APP_NAME,
                "version": APP_VERSION,
                "records": storage.count(),
                "last_run": storage.last_run(),
                "mirror": str(storage.mirror_path),
                "database": str(storage.db_path),
            }
        )

    def _api_harvest(self, query: Dict[str, List[str]]) -> None:
        raw_from = _first_value(query, "from")
        raw_to = _first_value(query, "to")
        resource_filter = (
            _first_value(query, "type")
            or _first_value(query, "resource_filter")
            or DEFAULT_RESOURCE_FILTER
        )

        for label, value in (("from", raw_from), ("to", raw_to)):
            if value and parse_iso_date(value) is None:
                self._send_json(
                    {
                        "error": "invalid date",
                        "detail": f"'{label}' must be YYYY-MM-DD, received '{value}'",
                    },
                    HTTPStatus.BAD_REQUEST,
                )
                return

        if resource_filter not in RESOURCE_FILTERS:
            self._send_json(
                {
                    "error": "unknown resource filter",
                    "detail": resource_filter,
                    "allowed": list(RESOURCE_FILTERS.keys()),
                },
                HTTPStatus.BAD_REQUEST,
            )
            return

        try:
            payload = execute_harvest(raw_from or None, raw_to or None, resource_filter)
        except HarvestBusyError as exc:
            self._send_json({"error": "busy", "detail": str(exc)}, HTTPStatus.CONFLICT)
            return
        except ValueError as exc:
            self._send_json(
                {"error": "invalid window", "detail": str(exc)},
                HTTPStatus.BAD_REQUEST,
            )
            return
        self._send_json(payload)


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------
def create_server(
    host: str = HOST, port: int = PORT
) -> ThreadingHTTPServer:
    """Build (but do not start) the threaded HTTP server."""
    server = ThreadingHTTPServer((host, port), HarvesterRequestHandler)
    server.daemon_threads = True
    return server


def serve(host: str = HOST, port: int = PORT) -> None:
    """Run the dashboard until interrupted."""
    log = get_logger()
    storage = get_storage()
    server = create_server(host, port)
    bound_host, bound_port = server.server_address[0], server.server_address[1]

    log.info("%s v%s", APP_NAME, APP_VERSION)
    log.info("SQLite database: %s (%d records)", storage.db_path, storage.count())
    log.info("JSON mirror: %s", storage.mirror_path)
    log.info("Dashboard ready: http://%s:%d/", bound_host, bound_port)
    log.info("Live harvest endpoint: /api/harvest?from=YYYY-MM-DD&to=YYYY-MM-DD&type=dataset")
    log.info("Press Ctrl+C to stop.")

    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        log.info("Shutdown signal received - closing server")
    finally:
        server.server_close()
        log.info("Server stopped")
