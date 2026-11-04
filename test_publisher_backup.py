"""Offline safety tests for publication backups. Never connects to R2 or Supabase.

Run: python -m unittest test_publisher_backup.py -v
"""
import hashlib
import io
import json
import tempfile
from pathlib import Path
from unittest.mock import patch
import threading
import unittest
from botocore.exceptions import ClientError

from publisher import DATASET, _backup_existing_objects, publish


def sha(data):
    return hashlib.sha256(data).hexdigest()


class MemoryR2:
    def __init__(self, objects):
        self.objects = dict(objects)
        self.copies = []
        self.corrupt_copies = False

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "404", "Message": "Missing"}},
                              "HeadObject")
        data = self.objects[Key]
        return {"ETag": '"' + sha(data)[:32] + '"', "ContentLength": len(data)}

    def copy_object(self, Bucket, Key, CopySource):
        original = CopySource["Key"]
        self.copies.append((original, Key))
        value = self.objects[original]
        self.objects[Key] = (b"X" * len(value)) if self.corrupt_copies else value

    def get_object(self, Bucket, Key, IfMatch=None):
        head = self.head_object(Bucket, Key)
        if IfMatch is not None:
            assert IfMatch == head['ETag']
        return {**head, "Body": io.BytesIO(self.objects[Key])}


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.original_key = DATASET + "/mockups/example.png"
        self.old_bytes = b"previous artwork contents"
        self.r2 = MemoryR2({self.original_key: self.old_bytes})
        self.plan = {
            "operation_id": "00000000-0000-4000-8000-000000000001",
            "operations": [{
                "key": self.original_key,
                "action": "REPLACE",
                "old_sha256": sha(self.old_bytes),
                "old_etag": sha(self.old_bytes)[:32],
            }]
        }
        self.audit = {"backups": []}
        self.snapshots = []

    def save(self):
        self.snapshots.append({
            "state": self.audit.get("backup_state"),
            "count": len(self.audit["backups"]),
        })

    def backup(self, stop=None):
        _backup_existing_objects(self.plan, self.r2, "fake-bucket",
                                 self.audit, self.save, lambda _: None,
                                 stop or threading.Event())

    def test_backup_verified_before_any_original_changes(self):
        self.backup()
        self.assertEqual(self.audit["backup_state"], "complete")
        self.assertEqual(self.audit["backup_total"], 1)
        record = self.audit["backups"][0]
        self.assertEqual(self.r2.objects[self.original_key], self.old_bytes)
        self.assertEqual(self.r2.objects[record["backup_key"]], self.old_bytes)
        self.assertEqual(record["sha256"], sha(self.old_bytes))
        self.assertTrue(record["backup_key"].startswith(
            DATASET + "/_publication_backups/" + self.plan["operation_id"] + "/"))
        self.assertEqual(self.snapshots[-1]["state"], "complete")

    def test_changed_original_refuses_to_backup(self):
        self.r2.objects[self.original_key] = b"changed since preview"
        with self.assertRaisesRegex(RuntimeError, "changed before backup"):
            self.backup()
        self.assertEqual(self.r2.copies, [])
        self.assertEqual(self.audit["backups"], [])

    def test_corrupt_backup_is_rejected(self):
        self.r2.corrupt_copies = True
        with self.assertRaisesRegex(RuntimeError, "contents differ"):
            self.backup()
        self.assertEqual(self.audit["backup_state"], "in_progress")
        self.assertEqual(self.audit["backups"], [])
        self.assertEqual(self.r2.objects[self.original_key], self.old_bytes)

    def test_existing_backup_is_never_overwritten(self):
        backup_key = (DATASET + "/_publication_backups/" +
                      self.plan["operation_id"] + "/mockups/example.png")
        self.r2.objects[backup_key] = b"existing backup"
        with self.assertRaisesRegex(RuntimeError, "unexpectedly exists"):
            self.backup()
        self.assertEqual(self.r2.objects[backup_key], b"existing backup")
        self.assertEqual(self.r2.copies, [])

    def test_cancel_before_backup_does_not_copy(self):
        stop = threading.Event()
        stop.set()
        with self.assertRaisesRegex(RuntimeError, "Stopped during backup"):
            self.backup(stop)
        self.assertEqual(self.r2.copies, [])
        self.assertEqual(self.r2.objects[self.original_key], self.old_bytes)


class SimulatedCrash(BaseException):
    """Models a sudden process termination, which Exception does not catch."""


class UploadR2(MemoryR2):
    def __init__(self, objects):
        super().__init__(objects)
        self.uploads = []
        self.fail_at = None
        self.crash_at = None

    def upload_file(self, Filename, Bucket, Key, ExtraArgs):
        self.objects[Key] = Path(Filename).read_bytes()
        self.uploads.append(Key)
        if len(self.uploads) == self.fail_at:
            raise RuntimeError("simulated upload failure after remote mutation")
        if len(self.uploads) == self.crash_at:
            raise SimulatedCrash("simulated unexpected process termination")


class PublishRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.old = [b"old artwork A", b"old artwork B"]
        self.new = [b"new artwork A!", b"new artwork B!"]
        self.keys = [
            DATASET + "/mockups/recovery_a.png",
            DATASET + "/mockups/recovery_b.png",
        ]
        self.r2 = UploadR2(dict(zip(self.keys, self.old)))
        self.operations = []
        for index, key in enumerate(self.keys):
            source = self.folder / ("artwork_%d.png" % index)
            source.write_bytes(self.new[index])
            self.operations.append({
                "source": str(source), "key": key, "action": "REPLACE",
                "local_sha256": sha(self.new[index]),
                "local_bytes": len(self.new[index]),
                "old_sha256": sha(self.old[index]),
                "old_etag": sha(self.old[index])[:32],
            })
        self.plan = {
            "operation_id": "00000000-0000-4000-8000-000000000002",
            "mode": "complete", "operations": self.operations, "items": [],
        }
        self.plan['ownership'] = {key: {} for key in self.keys}
        ownership_patch = patch('publisher.ownership_for', return_value=self.plan['ownership'])
        ownership_patch.start()
        self.addCleanup(ownership_patch.stop)
        self.api_calls = []

    def fake_api(self, endpoint, token, body, timeout=35):
        self.api_calls.append(body["mode"])
        if body["mode"] == "preflight":
            return {"safe_to_upload": True}
        return {"db_result": {"status": "published"}}

    def run_publish(self):
        return publish(self.plan, self.r2, "fake-bucket", "offline.invalid",
                       "fake-token", self.folder, lambda _: None,
                       threading.Event())

    def audit(self):
        path = self.folder / (
            "publication_run_" + self.plan["operation_id"] + ".json")
        return json.loads(path.read_text(encoding="utf-8"))

    def assert_backups_intact(self):
        a = self.audit()
        self.assertEqual(a["backup_state"], "complete")
        self.assertEqual(len(a["backups"]), 2)
        for index, key in enumerate(self.keys):
            backup = next(b for b in a["backups"]
                          if b["original_key"] == key)
            self.assertEqual(self.r2.objects[backup["backup_key"]],
                             self.old[index])
        return a

    def test_upload_failure_after_second_mutation_records_both(self):
        self.r2.fail_at = 2
        with patch("publisher._api", side_effect=self.fake_api):
            with self.assertRaisesRegex(RuntimeError, "simulated upload failure"):
                self.run_publish()
        a = self.assert_backups_intact()
        self.assertTrue(self.api_calls and set(self.api_calls) == {"preflight"})
        self.assertFalse(a["complete"])
        self.assertEqual({r["original_key"] for r in a["restore_needed"]},
                         set(self.keys))
        self.assertEqual(len(a["files"]), 1)
        self.assertEqual(a["overwrite_in_progress"]["original_key"], self.keys[1])

    def test_unexpected_crash_preserves_prewrite_intent(self):
        self.r2.crash_at = 1
        with patch("publisher._api", side_effect=self.fake_api):
            with self.assertRaises(SimulatedCrash):
                self.run_publish()
        a = self.assert_backups_intact()
        self.assertEqual(a["overwrite_in_progress"]["original_key"], self.keys[0])
        self.assertEqual(a["files"], [])
        self.assertTrue(self.api_calls and set(self.api_calls) == {"preflight"})
        # Crash recovery must inspect overwrite_in_progress even without restore_needed.
        self.assertNotIn("restore_needed", a)

    def test_database_rejection_after_uploads_records_recovery(self):
        def reject_commit(endpoint, token, body, timeout=35):
            self.api_calls.append(body["mode"])
            if body["mode"] == "preflight":
                return {"safe_to_upload": True}
            raise RuntimeError("simulated Supabase transaction rejected")
        with patch("publisher._api", side_effect=reject_commit):
            with self.assertRaisesRegex(RuntimeError, "transaction rejected"):
                self.run_publish()
        a = self.assert_backups_intact()
        self.assertEqual(self.api_calls, ["preflight"] * 6 + ["commit"])
        self.assertFalse(a["complete"])
        self.assertEqual(len(a["files"]), 2)
        self.assertEqual({r["original_key"] for r in a["restore_needed"]},
                         set(self.keys))
        self.assertNotIn("overwrite_in_progress", a)


if __name__ == "__main__":
    unittest.main()
