"""Regression tests for the data model and the SQLite persistence layer."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.config import MAX_RUNS_PER_SCOPE
from backend.models import (
    PUBLICATION_CLASS,
    HarvestRecord,
    build_file_summary,
    classify_resource_type,
    human_size,
    normalize_code_repo_url,
    normalize_doi,
    split_keywords,
)
from backend.storage import Storage


class ModelsTestCase(unittest.TestCase):
    """Pure functions shared by both scrapers and the storage layer."""

    def test_normalize_doi(self) -> None:
        self.assertEqual(
            normalize_doi("https://doi.org/10.5281/ZENODO.123"),
            "10.5281/zenodo.123",
        )
        self.assertEqual(
            normalize_doi("doi: 10.5281/zenodo.456."), "10.5281/zenodo.456"
        )
        self.assertEqual(
            normalize_doi("10.5075/epfl-thesis-1234"), "10.5075/epfl-thesis-1234"
        )
        self.assertEqual(normalize_doi("not a doi"), "")
        self.assertEqual(normalize_doi(None), "")

    def test_normalize_code_repo_url(self) -> None:
        self.assertEqual(
            normalize_code_repo_url("https://github.com/EPFL-ENAC/Harvester.git"),
            "https://github.com/EPFL-ENAC/Harvester",
        )
        self.assertEqual(
            normalize_code_repo_url("github.com/epfl-enac/harvester/tree/main/src"),
            "https://github.com/epfl-enac/harvester",
        )
        self.assertEqual(
            normalize_code_repo_url("www.github.com/epfl-enac/harvester/"),
            "https://github.com/epfl-enac/harvester",
        )
        self.assertEqual(
            normalize_code_repo_url(
                "https://gitlab.com/epfl-enac/group/sub/repo/-/tree/main"
            ),
            "https://gitlab.com/epfl-enac/group/sub/repo",
        )
        self.assertEqual(
            normalize_code_repo_url("https://gitlab.epfl.ch/enac/it4r/tools/repo.git"),
            "https://gitlab.epfl.ch/enac/it4r/tools/repo",
        )
        self.assertEqual(
            normalize_code_repo_url("https://epfl-enac.github.io/demo"), ""
        )
        self.assertEqual(
            normalize_code_repo_url("https://doi.org/10.5281/zenodo.1"), ""
        )
        self.assertEqual(
            normalize_code_repo_url("https://example.com/owner/repo"), ""
        )

    def test_split_keywords(self) -> None:
        self.assertEqual(
            split_keywords("EEG, BCI; neuroimaging"),
            ["EEG", "BCI", "neuroimaging"],
        )
        self.assertEqual(
            split_keywords({"subject": "spin waves, magnonics"}),
            ["spin waves", "magnonics"],
        )
        self.assertEqual(
            split_keywords([{"value": "a; b"}, "c, a", "", None]),
            ["a", "b", "c"],
        )
        self.assertEqual(split_keywords(None), [])

    def test_classify_resource_type(self) -> None:
        self.assertEqual(classify_resource_type("dataset"), "dataset")
        self.assertEqual(classify_resource_type("software::simulation"), "software")
        self.assertEqual(classify_resource_type("model"), "model")
        self.assertEqual(classify_resource_type("software paper"), "software")
        self.assertEqual(
            classify_resource_type("text::journal::journal-article"),
            PUBLICATION_CLASS,
        )
        self.assertEqual(classify_resource_type("student work"), PUBLICATION_CLASS)
        self.assertEqual(classify_resource_type("journal article"), PUBLICATION_CLASS)
        self.assertEqual(classify_resource_type("image"), "other")
        self.assertEqual(classify_resource_type(""), "other")

    def test_human_size(self) -> None:
        self.assertEqual(human_size(0), "0 B")
        self.assertEqual(human_size(3072), "3.0 KB")
        self.assertEqual(human_size(None), "")
        self.assertEqual(human_size("nope"), "")

    def test_build_file_summary_keeps_entries_and_urls(self) -> None:
        files = [
            {
                "key": "measurements.csv",
                "size": 2048,
                "links": {
                    "self": (
                        "https://zenodo.org/api/records/7/files/"
                        "measurements.csv/content"
                    )
                },
            },
            {"key": "archive.zip", "size": 1024, "links": {}},
        ]
        summary = build_file_summary(files, record_id=7)

        self.assertEqual(summary["count"], 2)
        self.assertEqual(summary["total_bytes"], 3072)
        self.assertEqual(summary["total_human"], "3.0 KB")
        self.assertEqual(summary["extensions"], [".csv", ".zip"])
        self.assertEqual(len(summary["entries"]), 2)
        self.assertEqual(
            summary["entries"][0]["url"],
            "https://zenodo.org/api/records/7/files/measurements.csv/content",
        )
        self.assertEqual(
            summary["entries"][1]["url"],
            "https://zenodo.org/api/records/7/files/archive.zip/content",
        )
        # The dict form returned by the Zenodo API behaves identically.
        self.assertEqual(build_file_summary({"entries": files}, 7), summary)

def _record(**overrides) -> HarvestRecord:
    """One deterministic record whose fields can be overridden per test."""
    values = {
        "canonical_id": "10.5281/zenodo.1",
        "source_id": "zenodo-1",
        "source": "zenodo",
        "source_keys": ["zenodo"],
        "resource_type": "dataset",
        "title": "A dataset with files",
        "abstract": "Short abstract.",
        "doi": "10.5281/zenodo.1",
        "publication_date": "2026-01-02",
        "keywords": ["EEG", "BCI"],
        "file_summary": {
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
                    "url": "https://zenodo.org/api/records/1/files/a.csv/content",
                }
            ],
        },
    }
    values.update(overrides)
    return HarvestRecord(**values)


class StorageTestCase(unittest.TestCase):
    """Delta detection, merge preservation and run retention on a temp DB."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.storage = Storage(
            db_path=root / "harvester.db", mirror_path=root / "dataset_cache.json"
        )

    def tearDown(self) -> None:
        self.storage.close()
        self.tempdir.cleanup()

    def test_upsert_detects_new_updated_unchanged(self) -> None:
        self.assertEqual(
            self.storage.upsert_records([_record()]),
            {"new": 1, "updated": 0, "unchanged": 0},
        )
        # A byte-identical record only refreshes last_seen.
        self.assertEqual(
            self.storage.upsert_records([_record()]),
            {"new": 0, "updated": 0, "unchanged": 1},
        )
        self.assertEqual(
            self.storage.upsert_records([_record(title="A renamed dataset")]),
            {"new": 0, "updated": 1, "unchanged": 0},
        )
        stored = self.storage.get_record("10.5281/zenodo.1")
        self.assertIsNotNone(stored)
        self.assertEqual(stored["title"], "A renamed dataset")
        self.assertEqual(stored["sync_status"], "updated")

    def test_file_summary_dict_difference_marks_updated(self) -> None:
        self.storage.upsert_records([_record()])
        changed = _record()
        changed.file_summary["entries"].append(
            {
                "key": "b.zip",
                "size": 99,
                "size_human": "99 B",
                "ext": ".zip",
                "url": "https://zenodo.org/api/records/1/files/b.zip/content",
            }
        )
        counts = self.storage.upsert_records([changed])
        self.assertEqual(counts["updated"], 1)
        stored = self.storage.get_record("10.5281/zenodo.1")
        self.assertEqual(len(stored["file_summary"]["entries"]), 2)


    def test_update_file_summary_persists_and_mirrors(self) -> None:
        self.storage.upsert_records([_record()])
        summary = build_file_summary(
            [{"key": "fresh.csv", "size": 5, "links": {}}], record_id=1
        )
        self.storage.update_file_summary("10.5281/zenodo.1", summary)

        stored = self.storage.get_record("10.5281/zenodo.1")
        self.assertEqual(stored["file_summary"]["entries"][0]["key"], "fresh.csv")
        mirror = self.storage.mirror_path.read_text(encoding="utf-8")
        self.assertIn("fresh.csv", mirror)

    def test_superset_merge_is_preserved_on_partial_runs(self) -> None:
        merged = _record(
            source="merged",
            source_keys=["infoscience", "zenodo"],
            affiliations=["ENAC-IT4R"],
            parent_orgs=["ENAC"],
            unit_managers=["Prof. Example"],
            funding=["Open Research Data"],
            url="https://infoscience.epfl.ch/entities/publication/abc",
            created_date="2026-01-03",
        )
        self.storage.upsert_records([merged])

        # A later Zenodo-only snapshot carries no EPFL hierarchy and no files.
        zenodo_only = _record(
            source="zenodo",
            source_keys=["zenodo"],
            affiliations=[],
            parent_orgs=[],
            unit_managers=[],
            funding=[],
            url="",
            created_date="",
            file_summary={},
        )
        counts = self.storage.upsert_records([zenodo_only])
        self.assertEqual(counts["new"], 0)

        stored = self.storage.get_record("10.5281/zenodo.1")
        self.assertEqual(stored["source"], "merged")
        self.assertEqual(sorted(stored["source_keys"]), ["infoscience", "zenodo"])
        self.assertEqual(stored["affiliations"], ["ENAC-IT4R"])
        self.assertEqual(stored["parent_orgs"], ["ENAC"])
        self.assertEqual(stored["unit_managers"], ["Prof. Example"])
        self.assertEqual(stored["funding"], ["Open Research Data"])
        self.assertEqual(stored["created_date"], "2026-01-03")
        self.assertEqual(stored["file_summary"]["entries"][0]["key"], "a.csv")

    def test_harvest_runs_retention_per_scope(self) -> None:
        base = {
            "resource_type": "dataset",
            "date_from": "2026-01-01",
            "date_to": "2026-01-07",
            "new_count": 1,
        }
        for index in range(1, 4):
            self.storage.record_run(
                dict(base, run_at=f"2026-01-0{index}T00:00:00+00:00")
            )
        self.storage.record_run(
            {
                "run_at": "2026-01-04T00:00:00+00:00",
                "resource_type": "software",
                "date_from": "2026-01-01",
                "date_to": "2026-01-07",
                "new_count": 1,
            }
        )

        runs = self.storage.load_runs(limit=20)
        dataset_runs = [run for run in runs if run["resource_type"] == "dataset"]
        software_runs = [run for run in runs if run["resource_type"] == "software"]
        self.assertEqual(len(dataset_runs), MAX_RUNS_PER_SCOPE)
        self.assertEqual(len(software_runs), 1)
        # The two newest dataset runs survive, the oldest is pruned.
        self.assertEqual(
            sorted(run["run_at"] for run in dataset_runs),
            ["2026-01-02T00:00:00+00:00", "2026-01-03T00:00:00+00:00"],
        )


if __name__ == "__main__":
    unittest.main()

