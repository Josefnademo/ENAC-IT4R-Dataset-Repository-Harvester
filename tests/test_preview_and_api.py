"""Regression tests for backend/preview.py and the HTTP API."""

from __future__ import annotations

import base64
import io
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from unittest import mock

import requests

from backend import preview
from backend import storage as storage_module
from backend.models import HarvestRecord
from backend.preview import (
    MAX_INNER_PDF_BYTES,
    STRUCTURED_BYTE_LIMIT,
    TABULAR_BYTE_LIMIT,
    TEXT_BYTE_LIMIT,
    PreviewError,
    _parse_structured_bytes,
    _parse_table_bytes,
    build_preview,
    resolve_preview_spec,
    supported_preview_extensions,
    validate_preview_url,
)
from backend.server import create_server
from backend.storage import Storage

ZIP_URL = "https://zenodo.org/api/records/1/files/FigureData.zip"
CSV_URL = "https://zenodo.org/api/records/1/files/a.csv"


def _zip_bytes(files):
    """Build a real in-memory ZIP whose directory and members can be read."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def _local_zip(payload):
    """Serve an in-memory ZIP through the module's HTTP Range plumbing."""

    def fake_range_file(url, size):
        return io.BytesIO(payload)

    return mock.patch.multiple(
        preview,
        _remote_zip_size=mock.Mock(return_value=len(payload)),
        _HttpRangeFile=fake_range_file,
    )


def _record() -> HarvestRecord:
    """One stored record whose file inventory is already cached."""
    return HarvestRecord(
        canonical_id="10.5281/zenodo.1",
        source_id="zenodo-1",
        source="zenodo",
        source_keys=["zenodo"],
        resource_type="dataset",
        title="Dataset with a previewable file",
        doi="10.5281/zenodo.1",
        publication_date="2026-01-02",
        file_summary={
            "count": 1,
            "total_bytes": 12,
            "total_human": "12 B",
            "extensions": [".csv"],
            "entries": [
                {
                    "key": "a.csv",
                    "size": 12,
                    "size_human": "12 B",
                    "ext": ".csv",
                    "url": CSV_URL,
                }
            ],
        },
    )


class PreviewValidationTests(unittest.TestCase):
    """SSRF guard and the declarative extension registry."""

    def test_ssrf_allow_list(self) -> None:
        for bad in (
            "http://zenodo.org/api/records/1/files/a.csv",
            "https://user:pass@zenodo.org/api/records/1/files/a.csv",
            "https://evil.com/a.csv",
            "",
        ):
            with self.subTest(url=bad):
                with self.assertRaises(PreviewError) as ctx:
                    validate_preview_url(bad)
                self.assertEqual(ctx.exception.status, 400)

        self.assertEqual(
            validate_preview_url("https://zenodo.org/records/1"),
            "https://zenodo.org/records/1",
        )
        self.assertEqual(
            validate_preview_url("https://infoscience.epfl.ch/entities/publication/1"),
            "https://infoscience.epfl.ch/entities/publication/1",
        )

    def test_resolve_preview_spec(self) -> None:
        cases = {
            "analysis.py": ("text", TEXT_BYTE_LIMIT),
            "pyproject.toml": ("text", TEXT_BYTE_LIMIT),
            "README.rst": ("text", TEXT_BYTE_LIMIT),
            "paper.pdf": ("pdf", MAX_INNER_PDF_BYTES),
            "data.csv": ("table", TABULAR_BYTE_LIMIT),
            "meta.json": ("structured", STRUCTURED_BYTE_LIMIT),
            "map.geojson": ("structured", STRUCTURED_BYTE_LIMIT),
            "archive.zip": ("zip", 0),
            "Makefile": ("text", TEXT_BYTE_LIMIT),
            "docs/LICENSE": ("text", TEXT_BYTE_LIMIT),
            "Dockerfile": ("text", TEXT_BYTE_LIMIT),
            ".gitignore": ("text", TEXT_BYTE_LIMIT),
            "model.nc": ("unsupported", 0),
            "": ("unsupported", 0),
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(resolve_preview_spec(name), expected)

    def test_supported_preview_extensions(self) -> None:
        extensions = supported_preview_extensions()
        for token in (".py", ".pdf", ".csv", ".zip", "makefile", "license"):
            self.assertIn(token, extensions)
        self.assertNotIn(".nc", extensions)
        self.assertEqual(extensions, sorted(extensions))


class ByteParserTests(unittest.TestCase):
    """The shared parsers used by direct files and extracted ZIP members."""

    def test_table_bytes(self) -> None:
        payload = _parse_table_bytes(
            b"name,value\nalpha,1\nbeta,2\n", False, ".csv"
        )
        self.assertEqual(payload["kind"], "table")
        self.assertEqual(payload["columns"], ["name", "value"])
        self.assertEqual(payload["rows"], [["alpha", "1"], ["beta", "2"]])
        self.assertEqual(payload["row_count_shown"], 2)

    def test_tsv_bytes(self) -> None:
        payload = _parse_table_bytes(b"a\tb\n1\t2\n", False, ".tsv")
        self.assertEqual(payload["columns"], ["a", "b"])
        self.assertEqual(payload["rows"], [["1", "2"]])

    def test_truncated_table_drops_incomplete_line(self) -> None:
        payload = _parse_table_bytes(b"a,b\n1,2\n9", True, ".csv")
        self.assertEqual(payload["rows"], [["1", "2"]])
        self.assertTrue(payload["truncated"])

    def test_structured_json_and_geojson(self) -> None:
        json_payload = _parse_structured_bytes(b'{"a": 1}', False)
        self.assertEqual(json_payload["kind"], "json")
        self.assertIn('"a": 1', json_payload["content"])

        geojson = b'{"type": "FeatureCollection", "features": []}'
        self.assertEqual(
            _parse_structured_bytes(geojson, False)["kind"], "geojson"
        )

        text = _parse_structured_bytes(b"{not json", False)
        self.assertEqual(text["kind"], "text")


class ZipMemberTests(unittest.TestCase):
    """Central-directory listing and single-member extraction, all local."""

    FILES = {
        "FigureData/Figure3/a_pH11.csv": b"time,pH\n0,7.1\n1,7.0\n",
        "code/plot.py": b"print('hello')\n",
        "docs/report.pdf": b"%PDF-1.4 fake pdf bytes",
        "img/logo.png": b"\x89PNG\r\n\x1a\nfake",
    }

    def setUp(self) -> None:
        self.payload = _zip_bytes(self.FILES)

    def test_central_directory_listing(self) -> None:
        with _local_zip(self.payload):
            payload = build_preview(ZIP_URL, "FigureData.zip")
        self.assertEqual(payload["kind"], "zip")
        self.assertEqual(payload["entry_count"], len(self.FILES))
        names = {entry["name"] for entry in payload["entries"]}
        self.assertIn("FigureData/Figure3/a_pH11.csv", names)
        self.assertIn("docs/report.pdf", names)
        by_name = {entry["name"]: entry for entry in payload["entries"]}
        self.assertEqual(by_name["code/plot.py"]["ext"], ".py")
        self.assertTrue(by_name["code/plot.py"]["size_human"])

    def test_csv_member_is_parsed_as_table(self) -> None:
        with _local_zip(self.payload):
            payload = build_preview(
                ZIP_URL, "FigureData.zip", inner="FigureData/Figure3/a_pH11.csv"
            )
        self.assertEqual(payload["kind"], "table")
        self.assertEqual(payload["columns"], ["time", "pH"])
        self.assertEqual(payload["rows"], [["0", "7.1"], ["1", "7.0"]])
        self.assertEqual(payload["inner_path"], "FigureData/Figure3/a_pH11.csv")

    def test_code_member_is_rendered_as_text(self) -> None:
        with _local_zip(self.payload):
            payload = build_preview(ZIP_URL, "FigureData.zip", inner="code/plot.py")
        self.assertEqual(payload["kind"], "text")
        self.assertIn("print('hello')", payload["content"])
        self.assertEqual(payload["inner_path"], "code/plot.py")

    def test_pdf_member_is_base64_inlined(self) -> None:
        with _local_zip(self.payload):
            payload = build_preview(ZIP_URL, "FigureData.zip", inner="docs/report.pdf")
        self.assertEqual(payload["kind"], "pdf")
        self.assertTrue(payload["from_zip"])
        self.assertEqual(payload["inner_path"], "docs/report.pdf")
        self.assertEqual(
            base64.b64decode(payload["pdf_base64"]),
            self.FILES["docs/report.pdf"],
        )

    def test_image_member_becomes_data_uri(self) -> None:
        with _local_zip(self.payload):
            payload = build_preview(ZIP_URL, "FigureData.zip", inner="img/logo.png")
        self.assertEqual(payload["kind"], "image")
        self.assertTrue(payload["image_url"].startswith("data:image/png;base64,"))
        self.assertTrue(payload["from_zip"])


    def test_missing_member_is_404(self) -> None:
        with _local_zip(self.payload):
            with self.assertRaises(PreviewError) as ctx:
                build_preview(ZIP_URL, "FigureData.zip", inner="data/missing.csv")
        self.assertEqual(ctx.exception.status, 404)

    def test_directory_entry_is_404(self) -> None:
        with self.assertRaises(PreviewError) as ctx:
            build_preview(ZIP_URL, "FigureData.zip", inner="FigureData/")
        self.assertEqual(ctx.exception.status, 404)

    def test_unsupported_member_never_opens_the_archive(self) -> None:
        with _local_zip(self.payload):
            payload = build_preview(ZIP_URL, "FigureData.zip", inner="model.nc")
            preview._remote_zip_size.assert_not_called()
        self.assertEqual(payload["kind"], "unsupported")
        self.assertEqual(payload["inner_path"], "model.nc")

    def test_oversized_member_is_413(self) -> None:
        with _local_zip(self.payload), mock.patch.object(
            preview, "MAX_ZIP_MEMBER_BYTES", 4
        ):
            with self.assertRaises(PreviewError) as ctx:
                build_preview(ZIP_URL, "FigureData.zip", inner="code/plot.py")
        self.assertEqual(ctx.exception.status, 413)


class DirectPreviewTests(unittest.TestCase):
    """Direct URLs dispatch through the same registry, without extra fetches."""

    def test_pdf_and_image_are_not_proxied(self) -> None:
        pdf_url = "https://zenodo.org/api/records/1/files/paper.pdf"
        self.assertEqual(
            build_preview(pdf_url, "paper.pdf"),
            {"kind": "pdf", "pdf_url": pdf_url},
        )
        self.assertEqual(
            build_preview("https://zenodo.org/api/records/1/files/p.png", "p.png"),
            {
                "kind": "image",
                "image_url": "https://zenodo.org/api/records/1/files/p.png",
            },
        )

    def test_text_and_table_dispatch(self) -> None:
        code_url = "https://zenodo.org/api/records/1/files/code.py"
        with mock.patch.object(
            preview, "_fetch_bytes", return_value=(b"print(1)\n", False)
        ):
            payload = build_preview(code_url, "code.py")
        self.assertEqual(payload["kind"], "text")
        self.assertIn("print(1)", payload["content"])

        with mock.patch.object(
            preview, "_fetch_bytes", return_value=(b"a,b\n1,2\n", False)
        ):
            table = build_preview(CSV_URL, "a.csv")
        self.assertEqual(table["kind"], "table")
        self.assertEqual(table["columns"], ["a", "b"])

    def test_unsupported_extension(self) -> None:
        payload = build_preview(
            "https://zenodo.org/api/records/1/files/model.nc", "model.nc"
        )
        self.assertEqual(payload["kind"], "unsupported")
        self.assertIn("Binary scientific format", payload["reason"])



class FetchBytesTests(unittest.TestCase):
    """`_fetch_bytes` sends its Range budget through the pooled session."""

    class _FakeResponse:
        status_code = 206

        def __init__(self, chunks):
            self._chunks = chunks
            self.headers = {"Content-Range": "bytes 0-4/10"}
            self.closed = False

        def iter_content(self, chunk_size=65536):
            for chunk in self._chunks:
                yield chunk

        def raise_for_status(self):
            return None

        def close(self):
            self.closed = True

    def test_range_budget_truncates_through_shared_session(self) -> None:
        response = self._FakeResponse([b"abc", b"de"])
        with mock.patch.object(
            preview.HTTP_SESSION, "get", return_value=response
        ) as session_get:
            raw, truncated = preview._fetch_bytes(CSV_URL, 4)

        self.assertEqual(raw, b"abcd")
        self.assertTrue(truncated)
        self.assertTrue(response.closed)
        self.assertEqual(session_get.call_args.args[0], CSV_URL)
        headers = session_get.call_args.kwargs["headers"]
        self.assertEqual(headers["Range"], "bytes=0-4")
        self.assertEqual(headers["Accept-Encoding"], "identity")

    def test_stale_keep_alive_socket_is_retried_once(self) -> None:
        """A dead pooled socket must not surface as HTTP 502."""
        response = self._FakeResponse([b"xy", b"z"])
        stale = requests.ConnectionError(
            "('Connection aborted.', "
            "RemoteDisconnected('Remote end closed connection without response'))"
        )
        with mock.patch.object(
            preview.HTTP_SESSION, "get", side_effect=[stale, response]
        ) as session_get, mock.patch.object(
            preview.HTTP_SESSION, "close"
        ) as session_close:
            raw, truncated = preview._fetch_bytes(CSV_URL, 8)

        self.assertEqual(raw, b"xyz")
        self.assertFalse(truncated)
        self.assertEqual(session_get.call_count, 2)
        session_close.assert_called_once()
        # The retry keeps the exact same request (Range budget included).
        self.assertEqual(
            session_get.call_args_list[0], session_get.call_args_list[1]
        )


class ApiServerTests(unittest.TestCase):
    """Live HTTP round-trips against an ephemeral server and a temporary DB."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.storage = Storage(
            db_path=root / "harvester.db", mirror_path=root / "dataset_cache.json"
        )
        self.storage.upsert_records([_record()])
        self.storage_patch = mock.patch.object(
            storage_module, "_default_storage", self.storage
        )
        self.storage_patch.start()
        self.server = create_server("127.0.0.1", 0)
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        self.storage_patch.stop()
        self.storage.close()
        self.tempdir.cleanup()

    def _get(self, path: str):
        try:
            with urllib.request.urlopen(self.base + path, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8")
            return error.code, json.loads(body) if body else {}

    def test_data_filters_logs_and_status(self) -> None:
        status, data = self._get("/api/data")
        self.assertEqual(status, 200)
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["items"][0]["title"], _record().title)
        self.assertIn(".pdf", data["preview_extensions"])
        self.assertIn("makefile", data["preview_extensions"])

        status, filters = self._get("/api/filters")
        self.assertEqual(status, 200)
        self.assertTrue(filters["filters"])
        self.assertIn(".py", filters["preview_extensions"])
        self.assertEqual(filters["default"], "dataset")

        status, logs = self._get("/api/logs?since=0")
        self.assertEqual(status, 200)
        self.assertIn("cursor", logs)
        self.assertIsInstance(logs["entries"], list)

        status, info = self._get("/api/status")
        self.assertEqual(status, 200)
        self.assertEqual(info["records"], 1)
        self.assertTrue(info["version"])

    def test_unknown_endpoint_is_404(self) -> None:
        status, payload = self._get("/api/nope")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"], "unknown endpoint")

    def test_preview_rejects_invalid_requests(self) -> None:
        status, payload = self._get("/api/preview")
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "preview rejected")

        rejected = urllib.parse.quote("https://evil.com/a.csv")
        status, _ = self._get("/api/preview?url=" + rejected)
        self.assertEqual(status, 400)

    def test_preview_serves_zip_member_and_reports_missing(self) -> None:
        payload = _zip_bytes({"data/a.csv": b"x,y\n1,2\n3,4\n"})
        query = urllib.parse.urlencode(
            {"url": ZIP_URL, "name": "FigureData.zip", "inner": "data/a.csv"}
        )
        missing = urllib.parse.urlencode(
            {
                "url": ZIP_URL,
                "name": "FigureData.zip",
                "inner": "data/missing.csv",
            }
        )
        with _local_zip(payload):
            status, body = self._get("/api/preview?" + query)
            self.assertEqual(status, 200)
            self.assertEqual(body["kind"], "table")
            self.assertEqual(body["inner_path"], "data/a.csv")
            self.assertEqual(body["columns"], ["x", "y"])
            self.assertEqual(body["rows"], [["1", "2"], ["3", "4"]])

            status, body = self._get("/api/preview?" + missing)
        self.assertEqual(status, 404)
        self.assertIn("not found", body["detail"])

    def test_record_files_validation_and_cached_inventory(self) -> None:
        status, _ = self._get("/api/record-files")
        self.assertEqual(status, 400)

        status, _ = self._get("/api/record-files?id=unknown")
        self.assertEqual(status, 404)

        record_id = urllib.parse.quote("10.5281/zenodo.1")
        status, payload = self._get("/api/record-files?id=" + record_id)
        self.assertEqual(status, 200)
        self.assertEqual(payload["canonical_id"], "10.5281/zenodo.1")
        self.assertEqual(payload["file_summary"]["entries"][0]["key"], "a.csv")


if __name__ == "__main__":
    unittest.main()

