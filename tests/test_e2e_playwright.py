"""Optional end-to-end test driven by Playwright (skipped when absent)."""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from backend import preview
from backend import storage as storage_module
from backend.models import HarvestRecord
from backend.server import create_server
from backend.storage import Storage

try:  # Playwright is an optional developer dependency, never a requirement.
    from playwright.sync_api import sync_playwright

    PLAYWRIGHT_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised whenever Playwright is absent
    sync_playwright = None
    PLAYWRIGHT_AVAILABLE = False

CSV_URL = "https://zenodo.org/api/records/1/files/a.csv"
CSV_PAYLOAD = b"name,value\nalpha,1\nbeta,2\n"


def _record() -> HarvestRecord:
    """The single record rendered by the dashboard during the test."""
    return HarvestRecord(
        canonical_id="10.5281/zenodo.1",
        source_id="zenodo-1",
        source="zenodo",
        source_keys=["zenodo"],
        resource_type="dataset",
        title="Coastal sensor measurements",
        abstract="A small dataset used by the end-to-end test.",
        doi="10.5281/zenodo.1",
        publication_date="2026-01-02",
        keywords=["eeg", "sensors"],
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


@unittest.skipUnless(PLAYWRIGHT_AVAILABLE, "playwright not installed")
class DashboardE2ETests(unittest.TestCase):
    """Browser-level smoke test: cards, filtering, preview modal."""

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
        # The preview of a.csv renders from these bytes instead of the network.
        self.fetch_patch = mock.patch.object(
            preview, "_fetch_bytes", return_value=(CSV_PAYLOAD, False)
        )
        self.fetch_patch.start()
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
        self.fetch_patch.stop()
        self.storage_patch.stop()
        self.storage.close()
        self.tempdir.cleanup()

    def test_cards_search_badges_and_preview_modal(self) -> None:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page()
            try:
                page.goto(self.base + "/", wait_until="load")
                page.wait_for_selector(".dataset-card")
                self.assertEqual(page.locator(".dataset-card").count(), 1)

                # Text search narrows the list and restores it when cleared.
                page.fill("#search-input", "no-such-record")
                page.wait_for_timeout(150)
                self.assertEqual(page.locator(".dataset-card").count(), 0)
                self.assertTrue(page.is_visible("#empty-state"))

                page.fill("#search-input", "")
                page.wait_for_timeout(150)
                self.assertEqual(page.locator(".dataset-card").count(), 1)

                # Badge filtering: clicking a keyword chip drives the search box.
                page.click(".keyword-chip")
                page.wait_for_timeout(150)
                self.assertEqual(page.locator("#search-input").input_value(), "eeg")
                self.assertEqual(page.locator(".dataset-card").count(), 1)

                # Preview overlay renders the CSV table from the mocked bytes.
                page.click("[data-preview-id]")
                page.wait_for_selector("#preview-modal:not([hidden])")
                page.wait_for_selector("#preview-body .preview-table")
                self.assertEqual(
                    page.locator("#preview-body th").nth(1).inner_text(), "name"
                )

                page.keyboard.press("Escape")
                page.wait_for_selector("#preview-modal[hidden]")
                self.assertTrue(page.is_hidden("#preview-modal"))
            finally:
                browser.close()


if __name__ == "__main__":
    unittest.main()

