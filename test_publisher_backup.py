"""Offline safety tests for publication backups. Never connects to R2 or Supabase.

Run: python -m unittest test_publisher_backup.py -v
"""
import hashlib
import io
import threading
import unittest
from botocore.exceptions import ClientError

from publisher import DATASET, _backup_existing_objects


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

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[Key])}


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


if __name__ == "__main__":
    unittest.main()
