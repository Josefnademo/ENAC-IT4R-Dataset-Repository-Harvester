"""Bounded, read-only preview of remote record files.

The dashboard lets a researcher glance at a CSV, a GeoJSON or the table of
contents of a multi-gigabyte ZIP without downloading the record. This module
therefore never reads more than a hard byte budget per format, and inspects
remote ZIP archives through HTTP ``Range`` requests so that only the central
directory travels over the wire. A file stored inside such an archive can be
previewed on its own (``inner`` argument): only that member's bytes are read.

Security: only ``https://`` URLs on ``zenodo.org`` or ``infoscience.epfl.ch``
are accepted (SSRF allow-list), and every fetch is bounded.
"""

from __future__ import annotations

import base64
import csv
import io
import json
import logging
import re
import zipfile
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

import requests

from .config import REQUEST_HEADERS
from .models import build_file_summary, file_extension, human_size

logger = logging.getLogger("enac_it4r.harvester")

#: Hosts the preview endpoint may read from (SSRF allow-list).
ALLOWED_HOSTS = ("zenodo.org", "infoscience.epfl.ch")

TABULAR_EXTENSIONS = (".csv", ".tsv")
STRUCTURED_EXTENSIONS = (".geojson", ".json")
TEXT_EXTENSIONS = (".txt", ".md", ".yml", ".yaml")
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".svg")

TABULAR_BYTE_LIMIT = 262144      # 256 KB
STRUCTURED_BYTE_LIMIT = 1048576  # 1 MB
TEXT_BYTE_LIMIT = 262144         # 256 KB
MAX_TABLE_ROWS = 100
MAX_ZIP_ENTRIES = 200

#: Uncompressed ceiling for one ZIP member read into memory (10 MB).
MAX_ZIP_MEMBER_BYTES = 10485760
#: An image extracted from a ZIP is base64-inlined; keep the payload small.
MAX_INNER_IMAGE_BYTES = 4194304

IMAGE_MIME_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
}

#: One Range read coalesces the whole ZIP central directory into a few calls.
#: Reads near the end of the archive are aligned to the last block, so the
#: end-of-central-directory record, the zip64 locator and the central
#: directory usually travel in a single request.
READ_AHEAD_BYTES = 1048576
#: Hard ceiling for an unbounded ``read()``; ``io`` allows any size, zipfile
#: only ever asks for the 22-byte end record that way.
MAX_UNBOUNDED_READ = 1048576
#: ZIP metadata budget: the central directory grows with the entry count, so an
#: archive with millions of members is refused instead of read into memory.
MAX_ZIP_BYTES = 12582912

CHUNK_SIZE = 65536
PROBE_TIMEOUT = 10
#: A slow HEAD is skipped in favour of a one-byte Range probe.
HEAD_TIMEOUT = 6
#: Zenodo can take tens of seconds to start streaming a large object, so the
#: read budget is generous even though the transferred volume is capped.
PREVIEW_TIMEOUT = 120

GEOJSON_TYPES = frozenset(
    {
        "FeatureCollection",
        "Feature",
        "GeometryCollection",
        "Point",
        "MultiPoint",
        "LineString",
        "MultiLineString",
        "Polygon",
        "MultiPolygon",
    }
)

UNSUPPORTED_BINARY_REASON = (
    "Binary scientific format — download record to inspect in Python/QGIS"
)
NO_RANGE_REASON = "Remote server does not support partial ZIP inspection"
BAD_ZIP_REASON = "Cannot read the remote ZIP central directory"


class PreviewError(Exception):
    """A preview request that must be answered with an HTTP error."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def validate_preview_url(url: Any) -> str:
    """Return the URL when it is an allowed HTTPS target, else raise 400."""
    text = str(url or "").strip()
    if not text:
        raise PreviewError("missing 'url' parameter")
    try:
        parts = urlsplit(text)
    except ValueError:
        raise PreviewError("invalid 'url' parameter") from None
    if parts.scheme.casefold() != "https":
        raise PreviewError("only https:// URLs can be previewed")
    if "@" in parts.netloc:
        raise PreviewError("credential-bearing URLs are not allowed")
    host = (parts.hostname or "").casefold()
    if host not in ALLOWED_HOSTS:
        raise PreviewError(
            "host not allowed: only zenodo.org and infoscience.epfl.ch "
            "can be previewed"
        )
    return text


# ---------------------------------------------------------------------------
# On-demand file inventory
# ---------------------------------------------------------------------------
#: ``source_id`` of a Zenodo record ("zenodo-1234567") and the DOI form
#: ("10.5281/zenodo.1234567") of the same record.
ZENODO_SOURCE_ID_RE = re.compile(r"^zenodo-(\d+)$", re.IGNORECASE)
ZENODO_DOI_RE = re.compile(r"^10\.5281/zenodo\.(\d+)$", re.IGNORECASE)
ZENODO_RECORD_URL = "https://zenodo.org/api/records/{recid}"


def resolve_zenodo_record_id(record: Dict[str, Any]) -> str:
    """Return the numeric Zenodo record ID carried by a harvested record."""
    record = record or {}
    match = ZENODO_SOURCE_ID_RE.match(str(record.get("source_id") or "").strip())
    if match:
        return match.group(1)
    match = ZENODO_DOI_RE.match(str(record.get("doi") or "").strip())
    return match.group(1) if match else ""


def fetch_record_file_summary(record: Dict[str, Any]) -> Dict[str, Any]:
    """Rebuild the file inventory of a record stored without preview entries.

    Rows harvested before the preview feature (or outside the current window)
    only know the file count. One lightweight call to the Zenodo record endpoint
    returns the same ``file_summary`` the scraper would have produced, and the
    caller caches it back into SQLite.
    """
    record = record or {}
    existing = record.get("file_summary") or {}
    if isinstance(existing, dict) and existing.get("entries"):
        return existing

    recid = resolve_zenodo_record_id(record)
    if not recid:
        raise PreviewError(
            "This repository does not expose downloadable file metadata for "
            "this record.",
            404,
        )

    url = ZENODO_RECORD_URL.format(recid=recid)
    try:
        response = requests.get(
            url,
            headers=dict(REQUEST_HEADERS),
            timeout=(PROBE_TIMEOUT, PREVIEW_TIMEOUT),
        )
        if response.status_code >= 400:
            raise PreviewError(
                f"Zenodo cannot refresh the file list (HTTP {response.status_code}).",
                502,
            )
        payload = response.json()
    except PreviewError:
        raise
    except (requests.RequestException, ValueError) as exc:
        raise PreviewError(
            f"Cannot reach Zenodo to refresh the file list: {exc}", 502
        ) from exc

    if not isinstance(payload, dict):
        raise PreviewError("Zenodo returned an unexpected record payload.", 502)
    return build_file_summary(payload.get("files"), recid)


# ---------------------------------------------------------------------------
# Bounded fetching
# ---------------------------------------------------------------------------
def _file_headers(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Browser-like headers: the JSON ``Accept`` of the API is overridden.

    ``identity`` encoding keeps the byte budget honest: the cap counts the
    bytes actually transferred, not a decompressed body.
    """
    headers = dict(REQUEST_HEADERS)
    headers["Accept"] = "*/*"
    headers["Accept-Encoding"] = "identity"
    if extra:
        headers.update(extra)
    return headers


def _fetch_bytes(url: str, limit: int) -> Tuple[bytes, bool]:
    """Read at most ``limit`` bytes and report whether more data exists.

    The request asks for ``limit + 1`` bytes through a Range header, so the
    repository sends only the preview window; a server that ignores Range still
    cannot push more than one extra chunk into memory.
    """
    try:
        response = requests.get(
            url,
            headers=_file_headers({"Range": f"bytes=0-{limit}"}),
            stream=True,
            timeout=(PROBE_TIMEOUT, PREVIEW_TIMEOUT),
            allow_redirects=True,
        )
        if response.status_code == 416:
            # A server that rejects an over-long range still serves the file.
            response.close()
            response = requests.get(
                url,
                headers=_file_headers(),
                stream=True,
                timeout=(PROBE_TIMEOUT, PREVIEW_TIMEOUT),
                allow_redirects=True,
            )
        try:
            response.raise_for_status()
            buffer = bytearray()
            for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                if not chunk:
                    continue
                buffer.extend(chunk)
                if len(buffer) > limit:
                    del buffer[limit:]
                    return bytes(buffer), True
            return bytes(buffer), False
        finally:
            response.close()
    except requests.RequestException as exc:
        raise PreviewError(f"cannot fetch remote file: {exc}", 502) from exc


def _decode(raw: bytes) -> str:
    """Decode UTF-8, falling back to latin-1 like the repository metadata."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1", errors="replace")


def _drop_incomplete_line(raw: bytes) -> bytes:
    """Cut a truncated stream back to its last complete line."""
    cut = raw.rfind(b"\n")
    return raw[: cut + 1] if cut >= 0 else raw


# ---------------------------------------------------------------------------
# Per-format previews
# ---------------------------------------------------------------------------
def _parse_table_bytes(raw: bytes, truncated: bool, ext: str) -> Dict[str, Any]:
    """Header row plus at most 100 rows from an already bounded CSV/TSV buffer."""
    if truncated:
        raw = _drop_incomplete_line(raw)
    text = _decode(raw)

    delimiter = "\t" if ext == ".tsv" else ","
    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|")
        delimiter = dialect.delimiter
    except csv.Error:
        pass

    columns: List[str] = []
    rows: List[List[str]] = []
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    for record in reader:
        if not record:
            continue
        if not columns:
            columns = [cell.strip() for cell in record]
            continue
        rows.append(record)
        if len(rows) >= MAX_TABLE_ROWS:
            truncated = True
            break

    return {
        "kind": "table",
        "truncated": truncated,
        "columns": columns,
        "rows": rows,
        "row_count_shown": len(rows),
    }


def _parse_structured_bytes(raw: bytes, truncated: bool) -> Dict[str, Any]:
    """JSON/GeoJSON from a bounded buffer; unparsable input degrades to text."""
    text = _decode(raw)
    try:
        parsed = json.loads(text)
    except ValueError:
        return {"kind": "text", "truncated": truncated, "content": text}
    if isinstance(parsed, dict) and parsed.get("type") in GEOJSON_TYPES:
        return {"kind": "geojson", "geojson": parsed}
    return {
        "kind": "json",
        "content": json.dumps(parsed, indent=2, ensure_ascii=False),
    }


def _parse_text_bytes(raw: bytes, truncated: bool) -> Dict[str, Any]:
    """Plain text, Markdown or YAML from an already bounded buffer."""
    return {"kind": "text", "truncated": truncated, "content": _decode(raw)}


def _preview_table(url: str, ext: str) -> Dict[str, Any]:
    """First 256 KB of a CSV/TSV as a header row plus at most 100 rows."""
    raw, truncated = _fetch_bytes(url, TABULAR_BYTE_LIMIT)
    return _parse_table_bytes(raw, truncated, ext)


def _preview_structured(url: str) -> Dict[str, Any]:
    """Up to 1 MB of JSON: GeoJSON is handed over raw, anything else pretty."""
    raw, truncated = _fetch_bytes(url, STRUCTURED_BYTE_LIMIT)
    return _parse_structured_bytes(raw, truncated)


def _preview_text(url: str) -> Dict[str, Any]:
    """First 256 KB of a text, Markdown or YAML file."""
    raw, truncated = _fetch_bytes(url, TEXT_BYTE_LIMIT)
    return _parse_text_bytes(raw, truncated)


class _HttpRangeFile(io.RawIOBase):
    """Read-only, seekable file object backed by HTTP ``Range`` requests.

    ``zipfile.ZipFile`` only needs the end-of-central-directory record and the
    central directory itself. A single read-ahead block turns the dozens of
    tiny sequential reads of the central directory into a handful of Range
    requests, so a multi-gigabyte archive is never downloaded.
    """

    def __init__(self, url: str, size: int, session: requests.Session) -> None:
        super().__init__()
        self._url = url
        self._size = int(size)
        self._session = session
        self._pos = 0
        self._buffer = b""
        self._buffer_start = 0
        self._transferred = 0

    # -- io.RawIOBase contract -------------------------------------------
    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            target = offset
        elif whence == io.SEEK_CUR:
            target = self._pos + offset
        elif whence == io.SEEK_END:
            target = self._size + offset
        else:
            raise ValueError(f"invalid whence: {whence}")
        if target < 0:
            raise ValueError("negative seek position")
        self._pos = target
        return self._pos

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = min(self._size - self._pos, MAX_UNBOUNDED_READ)
        if size <= 0 or self._pos >= self._size:
            return b""
        size = min(size, self._size - self._pos)
        offset = self._pos - self._buffer_start
        if not self._buffer or offset < 0 or offset + size > len(self._buffer):
            span = max(size, min(READ_AHEAD_BYTES, self._size))
            start = self._pos
            if self._size - self._pos < span:
                start = max(0, self._size - span)
            end = min(start + span, self._size) - 1
            self._buffer = self._read_range(start, end)
            self._buffer_start = start
            offset = self._pos - start
        data = self._buffer[offset : offset + size]
        self._pos += len(data)
        return data

    def readinto(self, buffer: Any) -> int:
        data = self.read(len(buffer))
        buffer[: len(data)] = data
        return len(data)

    def close(self) -> None:
        self._buffer = b""
        super().close()

    # -- HTTP -------------------------------------------------------------
    def _read_range(self, start: int, end: int) -> bytes:
        headers = _file_headers({"Range": f"bytes={start}-{end}"})
        try:
            response = self._session.get(
                self._url,
                headers=headers,
                stream=True,
                timeout=(PROBE_TIMEOUT, PREVIEW_TIMEOUT),
            )
            try:
                expected = end - start + 1
                if response.status_code == 206:
                    body = response.content
                elif response.status_code == 200 and start == 0:
                    # Some CDNs answer a 0..(size-1) range that spans a small
                    # whole file with 200 instead of 206; that is still exactly
                    # the block, provided this really is the complete file.
                    length = response.headers.get("Content-Length") or ""
                    if length != str(self._size):
                        raise OSError("server ignored the Range request")
                    body = response.content
                else:
                    raise OSError(
                        f"range request failed with HTTP {response.status_code}"
                    )
                if len(body) != expected:
                    raise OSError(
                        f"range request returned {len(body)} bytes, expected {expected}"
                    )
                self._transferred += len(body)
                if self._transferred > MAX_ZIP_BYTES:
                    raise PreviewError(
                        "archive has too many entries to list in the browser "
                        f"({MAX_ZIP_BYTES // 1048576} MB of metadata budget)",
                        413,
                    )
                return body
            finally:
                response.close()
        except requests.RequestException as exc:
            raise OSError(f"range request failed: {exc}") from exc


_RANGE_TOTAL_RE = re.compile(r"bytes\s+\d+-\d+/(\d+)", re.IGNORECASE)


def _remote_zip_size(url: str, session: requests.Session) -> Optional[int]:
    """Return the remote size when Range requests are honoured, else ``None``."""
    try:
        head = session.head(
            url,
            headers=_file_headers(),
            allow_redirects=True,
            timeout=(PROBE_TIMEOUT, HEAD_TIMEOUT),
        )
        length = head.headers.get("Content-Length") or ""
        accepts = "bytes" in (head.headers.get("Accept-Ranges") or "").casefold()
        if accepts and length.isdigit():
            return int(length)
    except requests.RequestException:
        pass

    # Zenodo's CDN omits ``Accept-Ranges`` on HEAD and answers it slowly, so a
    # one-byte Range GET is the authoritative (and fast) capability probe.
    try:
        probe = session.get(
            url,
            headers=_file_headers({"Range": "bytes=0-0"}),
            stream=True,
            timeout=(PROBE_TIMEOUT, PREVIEW_TIMEOUT),
        )
        try:
            if probe.status_code != 206:
                return None
            match = _RANGE_TOTAL_RE.match(probe.headers.get("Content-Range") or "")
            return int(match.group(1)) if match else None
        finally:
            probe.close()
    except requests.RequestException:
        return None


def _preview_zip(url: str) -> Dict[str, Any]:
    """List up to 200 entries of a remote ZIP, reading only its directory."""
    with requests.Session() as session:
        session.headers.update(_file_headers())
        size = _remote_zip_size(url, session)
        if not size:
            return {"kind": "unsupported", "reason": NO_RANGE_REASON}
        try:
            with zipfile.ZipFile(_HttpRangeFile(url, size, session)) as archive:
                infos = archive.infolist()
        except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
            logger.info("ZIP preview failed for %s: %s", url, exc)
            return {"kind": "unsupported", "reason": BAD_ZIP_REASON}

    entries = [
        {
            "name": info.filename,
            "size": int(info.file_size),
            "size_human": human_size(int(info.file_size)),
            "ext": file_extension(info.filename),
        }
        for info in infos[:MAX_ZIP_ENTRIES]
    ]
    return {"kind": "zip", "entry_count": len(infos), "entries": entries}


def _preview_image(url: str) -> Dict[str, Any]:
    """Images are hot-linked from the repository, never proxied."""
    return {"kind": "image", "image_url": url}


def _preview_zip_member(url: str, inner: str) -> Dict[str, Any]:
    """Render one file stored inside a remote ZIP, reading only that member.

    The central directory is reached through ``_HttpRangeFile``; only the member's
    own compressed bytes travel over the wire, under the same per-format budgets
    as a direct download. Unsupported extensions never open the archive.
    """
    ext = file_extension(inner)
    if inner.endswith("/"):
        raise PreviewError("Archive entry not found or is a directory.", 404)
    if ext in TABULAR_EXTENSIONS:
        limit = TABULAR_BYTE_LIMIT
    elif ext in STRUCTURED_EXTENSIONS:
        limit = STRUCTURED_BYTE_LIMIT
    elif ext in TEXT_EXTENSIONS:
        limit = TEXT_BYTE_LIMIT
    elif ext in IMAGE_EXTENSIONS:
        limit = MAX_INNER_IMAGE_BYTES
    else:
        return {
            "kind": "unsupported",
            "reason": UNSUPPORTED_BINARY_REASON,
            "inner_path": inner,
        }

    with requests.Session() as session:
        session.headers.update(_file_headers())
        size = _remote_zip_size(url, session)
        if not size:
            return {
                "kind": "unsupported",
                "reason": NO_RANGE_REASON,
                "inner_path": inner,
            }
        try:
            with zipfile.ZipFile(_HttpRangeFile(url, size, session)) as archive:
                info = archive.getinfo(inner)
                if info.is_dir():
                    raise PreviewError(
                        "Archive entry not found or is a directory.", 404
                    )
                if (
                    info.file_size > MAX_ZIP_MEMBER_BYTES
                    or info.compress_size > MAX_ZIP_BYTES
                ):
                    raise PreviewError(
                        "File inside archive is too large to preview in the browser.",
                        413,
                    )
                with archive.open(info) as handle:
                    raw = handle.read(limit + 1)
        except KeyError as exc:
            raise PreviewError(
                "Archive entry not found or is a directory.", 404
            ) from exc
        except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
            logger.info("ZIP member preview failed for %s (%s): %s", url, inner, exc)
            return {
                "kind": "unsupported",
                "reason": BAD_ZIP_REASON,
                "inner_path": inner,
            }

    truncated = len(raw) > limit
    raw = raw[:limit]
    if ext in TABULAR_EXTENSIONS:
        payload = _parse_table_bytes(raw, truncated, ext)
    elif ext in STRUCTURED_EXTENSIONS:
        payload = _parse_structured_bytes(raw, truncated)
    elif ext in TEXT_EXTENSIONS:
        payload = _parse_text_bytes(raw, truncated)
    else:
        if truncated:
            return {
                "kind": "unsupported",
                "reason": "Image inside archive is too large to preview.",
                "inner_path": inner,
            }
        mime = IMAGE_MIME_TYPES.get(ext, "application/octet-stream")
        payload = {
            "kind": "image",
            "image_url": f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}",
            "from_zip": True,
        }
    payload["inner_path"] = inner
    return payload


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def build_preview(url: Any, name: Any = "", inner: Any = "") -> Dict[str, Any]:
    """Classify one remote file by extension and return its bounded preview.

    ``inner`` names a file stored inside a ``.zip`` archive: the archive URL is
    the one validated against the allow-list, and only that member is read.
    """
    target = validate_preview_url(url)
    filename = str(name or "").strip() or urlsplit(target).path.rsplit("/", 1)[-1]
    inner_path = str(inner or "").strip()
    if inner_path:
        logger.debug("Preview member %s of %s", inner_path, filename)
        return _preview_zip_member(target, inner_path)

    ext = file_extension(filename)
    logger.debug("Preview %s (%s)", filename, ext or "no extension")

    if ext in TABULAR_EXTENSIONS:
        return _preview_table(target, ext)
    if ext in STRUCTURED_EXTENSIONS:
        return _preview_structured(target)
    if ext in TEXT_EXTENSIONS:
        return _preview_text(target)
    if ext == ".zip":
        return _preview_zip(target)
    if ext in IMAGE_EXTENSIONS:
        return _preview_image(target)
    return {"kind": "unsupported", "reason": UNSUPPORTED_BINARY_REASON}


