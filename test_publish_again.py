"""Prove Publish Again resumes an interrupted release using real prepare/publish logic.

Offline only. Uses a four-file, one-design collection, simulated R2 and mocked
Supabase. No production upload, database change, or external network request.
"""
from __future__ import annotations
from offline_ownership_fixtures import preflight

import json
import tempfile
import threading
import unittest
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import test_publisher_backup as backup_tests
from publisher import DATASET, prepare, publish


class PublishAgainTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.keys = [
            DATASET + "/mockups/sample_front.png",
            DATASET + "/mockups/sample_rear.png",
            DATASET + "/print_candidates/sample.png",
            DATASET + "/print_candidates/sample.svg",
        ]
        self.original = [f"previous file {i}".encode() for i in range(4)]
        self.updated = [f"updated file {i}".encode() for i in range(4)]
        local_files = []
        for i, key in enumerate(self.keys):
            source = self.folder / f"sample_{i}" / Path(key).name
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(self.updated[i])
            local_files.append((source, key))
        self.r2 = backup_tests.UploadR2(dict(zip(self.keys, self.original)))
        self.release = SimpleNamespace(
            issues=[], rows=[{"street_key": "example-1"}],
            prefix=DATASET, files=local_files,
        )
        self.mapped = [{
            "street_key": "example-1", "street_name": "Example Street",
            "postcode_district": "HX7",
            "front_mockup": self.keys[0], "rear_mockup": self.keys[1],
            "production_png": self.keys[2], "production_svg": self.keys[3],
        }]
        self.geo = {"features": [{
            "properties": {"street_key": "example-1"},
            "geometry": {"type": "Point", "coordinates": [-2.0, 53.0]},
        }]}
        self.api_calls = []
        self.report = self.folder / "reports"
        self.report.mkdir()

    def fake_api(self, endpoint, token, body, timeout=35):
        self.api_calls.append(body["mode"])
        if body["mode"] == "preflight":
            return preflight(self.mapped, currently_listed_keys=[])
        return {"db_result": {"status": "published"}}

    def preview(self):
        return prepare(
            self.release, self.mapped, self.geo, self.r2, "fake-bucket",
            "offline.invalid", "offline-token-with-at-least-32-characters",
            "complete", self.report, lambda _: None, threading.Event(),
        )

    def do_publish(self, plan):
        return publish(
            plan, self.r2, "fake-bucket", "offline.invalid", "offline-token",
            self.report, lambda _: None, threading.Event(),
        )

    def test_reopen_folder_and_publish_again_finishes_remaining_files(self):
        with patch("publisher._api", side_effect=self.fake_api):
            first_plan = self.preview()
            self.assertEqual(first_plan["summary"], {"REPLACE": 3})
            self.r2.fail_at = 2  # Second remote object mutates before error.
            with self.assertRaisesRegex(RuntimeError, "simulated upload failure"):
                self.do_publish(first_plan)
            self.assertEqual(self.api_calls.count("commit"), 0)
            self.assertEqual(self.r2.objects[self.keys[0]], self.updated[0])
            self.assertEqual(self.r2.objects[self.keys[1]], self.updated[1])
            self.assertEqual(self.r2.objects[self.keys[2]], self.original[2])
            self.assertEqual(self.r2.objects[self.keys[3]], self.original[3])

            # User selects the SAME folder again: fresh actual prepare() call.
            retry_plan = self.preview()
            self.assertNotEqual(first_plan["operation_id"], retry_plan["operation_id"])
            self.assertEqual(Counter(op["action"] for op in retry_plan["operations"]),
                             {"SAME": 2, "REPLACE": 1})
            self.assertEqual(retry_plan["summary"], {"SAME": 2, "REPLACE": 1})

            self.r2.fail_at = None
            self.r2.uploads.clear()
            result = self.do_publish(retry_plan)
            self.assertEqual(result["db_result"]["status"], "published")
            self.assertEqual(self.r2.uploads, self.keys[2:3])
            self.assertEqual(self.api_calls.count("commit"), 1)
            for i, key in enumerate(self.keys[:3]):
                self.assertEqual(self.r2.objects[key], self.updated[i])
            retry_audit = json.loads((self.report /
                ("publication_run_" + retry_plan["operation_id"] + ".json")
            ).read_text(encoding="utf-8"))
            self.assertTrue(retry_audit["complete"])
            self.assertEqual(len(retry_audit["backups"]), 1)

    def test_after_success_a_fresh_preview_skips_all_uploads(self):
        with patch("publisher._api", side_effect=self.fake_api):
            plan = self.preview()
            self.do_publish(plan)
            self.r2.uploads.clear()
            another = self.preview()
            self.assertEqual(another["summary"], {"SAME": 3})
            # Even running Publish again should perform no artwork upload.
            self.do_publish(another)
            self.assertEqual(self.r2.uploads, [])
            self.assertEqual(self.api_calls.count("commit"), 2)


if __name__ == "__main__":
    unittest.main()
