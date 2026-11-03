"""Offline tests of read-only remote recovery verification. No R2 connections."""
import hashlib
import io
import json
import tempfile
import unittest
import uuid
from pathlib import Path

from botocore.exceptions import ClientError
from publisher import DATASET
from remote_recovery_verification import (
    format_remote_verification, verify_remote_backups,
)


class MemoryReadOnlyR2:
    def __init__(self, objects):
        self.objects = objects
        self.calls = []

    def head_object(self, *, Bucket, Key):
        self.calls.append(("head_object", Bucket, Key))
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "404", "Message": "Not found"}}, "HeadObject")
        return {"ContentLength": len(self.objects[Key])}

    def get_object(self, *, Bucket, Key):
        self.calls.append(("get_object", Bucket, Key))
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "404", "Message": "Not found"}}, "GetObject")
        return {"Body": io.BytesIO(self.objects[Key])}


class RemoteRecoveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "journal.json"
        self.operation = str(uuid.uuid4())
        self.original = DATASET + "/mockups/example.png"
        self.backup = (DATASET + "/_publication_backups/" +
                       self.operation + "/mockups/example.png")
        self.payload = b"old actual image bytes"
        self.audit = {
            "operation_id": self.operation, "complete": False,
            "backup_state": "complete", "backup_total": 1,
            "files": [{"key": self.original, "status": "REPLACE"}],
            "backups": [{
                "original_key": self.original, "backup_key": self.backup,
                "sha256": hashlib.sha256(self.payload).hexdigest(),
                "bytes": len(self.payload),
            }],
        }
        self.client = MemoryReadOnlyR2({self.backup: self.payload})

    def verify(self):
        self.path.write_text(json.dumps(self.audit), encoding="utf-8")
        return verify_remote_backups(self.path, self.client, "test-bucket")

    def test_matching_backup_is_verified_from_real_bytes(self):
        result = self.verify()
        self.assertTrue(result["all_recorded_backups_verified"])
        self.assertEqual(result["verified_count"], 1)
        self.assertEqual(result["checks"][0]["status"], "verified")
        self.assertEqual([name for name, _, _ in self.client.calls],
                         ["head_object", "get_object"])
        self.assertFalse(result["supabase_checked"])
        self.assertFalse(result["original_objects_checked"])
        self.assertIn("NOT AUTHORISED", format_remote_verification(result))

    def test_missing_backup_reports_failure(self):
        self.client.objects.clear()
        result = self.verify()
        self.assertFalse(result["all_recorded_backups_verified"])
        self.assertEqual(result["checks"][0]["status"], "missing")

    def test_checksum_mismatch_reports_failure(self):
        self.client.objects[self.backup] = b"bad" + self.payload[3:]
        result = self.verify()
        self.assertFalse(result["all_recorded_backups_verified"])
        self.assertEqual(result["checks"][0]["status"], "sha256_mismatch")

    def test_size_mismatch_reports_failure(self):
        self.client.objects[self.backup] = b"short"
        result = self.verify()
        self.assertEqual(result["checks"][0]["status"], "size_mismatch")
        self.assertEqual([name for name, _, _ in self.client.calls], ["head_object"])

    def test_tampered_backup_key_fails_before_network(self):
        self.audit["backups"][0]["backup_key"] = "street-face/hebden-bridge/v2/mockups/example.png"
        with self.assertRaisesRegex(ValueError, "Backup key"):
            self.verify()
        self.assertEqual(self.client.calls, [])

    def test_traversal_path_fails_before_network(self):
        self.audit["backups"][0]["original_key"] = DATASET + "/../mockups/example.png"
        with self.assertRaisesRegex(ValueError, "Unsafe original"):
            self.verify()
        self.assertEqual(self.client.calls, [])

    def test_unrecorded_replacement_fails_before_network(self):
        self.audit["files"].append({"key": DATASET + "/extra.png", "status": "REPLACE"})
        with self.assertRaisesRegex(ValueError, "no backup record"):
            self.verify()
        self.assertEqual(self.client.calls, [])

    def test_missing_backup_phase_fails_before_network(self):
        self.audit["backup_state"] = "in_progress"
        with self.assertRaisesRegex(ValueError, "not recorded complete"):
            self.verify()
        self.assertEqual(self.client.calls, [])

    def test_bad_recorded_digest_fails_before_network(self):
        self.audit["backups"][0]["sha256"] = "not-a-sha"
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            self.verify()
        self.assertEqual(self.client.calls, [])

    def test_duplicate_backup_fails_before_network(self):
        self.audit["backups"].append(self.audit["backups"][0].copy())
        self.audit["backup_total"] = 2
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            self.verify()
        self.assertEqual(self.client.calls, [])

    def test_interrupted_backup_mapping_mismatch_fails_before_network(self):
        self.audit["overwrite_in_progress"] = {
            "original_key": self.original, "backup_key": "other/key",
        }
        with self.assertRaisesRegex(ValueError, "unexpected backup"):
            self.verify()
        self.assertEqual(self.client.calls, [])


if __name__ == "__main__":
    unittest.main()
