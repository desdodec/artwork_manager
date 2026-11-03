"""Offline simulated tests for opt-in isolated R2 integration probe."""
import io
import unittest
import uuid

from r2_backup_integration import (
    ORIGINAL, REPLACEMENT, TEST_BUCKET, _sha, execute_probe,
)


class FakeR2:
    def __init__(self, *, occupied=False, corrupt_backup=False):
        self.objects = {}
        self.calls = []
        self.occupied = occupied
        self.corrupt_backup = corrupt_backup

    def _call(self, name, **kwargs):
        self.calls.append((name, kwargs))
        if kwargs["Bucket"] != TEST_BUCKET:
            raise AssertionError("Unexpected bucket")

    def head_bucket(self, *, Bucket):
        self._call("head_bucket", Bucket=Bucket)

    def list_objects_v2(self, *, Bucket, Prefix, MaxKeys):
        self._call("list_objects_v2", Bucket=Bucket, Prefix=Prefix, MaxKeys=MaxKeys)
        return {"Contents": [{"Key": Prefix + "unrelated"}]} if self.occupied else {}

    def put_object(self, *, Bucket, Key, Body):
        self._call("put_object", Bucket=Bucket, Key=Key)
        self.objects[Key] = Body

    def copy_object(self, *, Bucket, Key, CopySource):
        self._call("copy_object", Bucket=Bucket, Key=Key)
        if CopySource["Bucket"] != TEST_BUCKET:
            raise AssertionError("Unexpected copy source bucket")
        data = self.objects[CopySource["Key"]]
        if self.corrupt_backup and "/backups/" in Key:
            data = b"CORRUPTED"
        self.objects[Key] = data

    def get_object(self, *, Bucket, Key):
        self._call("get_object", Bucket=Bucket, Key=Key)
        return {"Body": io.BytesIO(self.objects[Key])}


class IsolatedLiveProbeTests(unittest.TestCase):
    def test_requires_authorization_before_cloud_call(self):
        client = FakeR2()
        with self.assertRaises(PermissionError):
            execute_probe(client, str(uuid.uuid4()))
        self.assertEqual(client.calls, [])

    def test_rejects_invalid_run_id_without_cloud_call(self):
        client = FakeR2()
        with self.assertRaises(ValueError):
            execute_probe(client, "../hebden-bridge", authorized=True)
        self.assertEqual(client.calls, [])

    def test_refuses_nonempty_prefix_without_writes(self):
        client = FakeR2(occupied=True)
        with self.assertRaises(RuntimeError):
            execute_probe(client, str(uuid.uuid4()), authorized=True)
        self.assertNotIn("put_object", [method for method, _ in client.calls])
        self.assertNotIn("copy_object", [method for method, _ in client.calls])

    def test_full_backup_replace_restore_stays_isolated(self):
        client = FakeR2()
        run_id = str(uuid.uuid4())
        result = execute_probe(client, run_id, authorized=True)
        self.assertEqual(result["restored_sha256"], _sha(ORIGINAL))
        self.assertEqual(result["backup_sha256"], _sha(ORIGINAL))
        prefix = result["prefix"]
        self.assertEqual(client.objects[prefix + "live/original.png"], ORIGINAL)
        self.assertEqual(client.objects[prefix + "backups/original.png"], ORIGINAL)
        self.assertNotEqual(ORIGINAL, REPLACEMENT)
        self.assertEqual(
            [name for name, _ in client.calls].count("put_object"), 2
        )
        self.assertEqual(
            [name for name, _ in client.calls].count("copy_object"), 2
        )
        self.assertTrue(all(
            kwargs.get("Key", prefix).startswith(prefix)
            for _, kwargs in client.calls
        ))
        self.assertFalse(any("delete" in name for name, _ in client.calls))

    def test_bad_backup_halts_before_replace(self):
        client = FakeR2(corrupt_backup=True)
        run_id = str(uuid.uuid4())
        with self.assertRaises(RuntimeError):
            execute_probe(client, run_id, authorized=True)
        keys = [key for key in client.objects if key.endswith("/live/original.png")]
        self.assertEqual(len(keys), 1)
        self.assertEqual(client.objects[keys[0]], ORIGINAL)
        self.assertEqual(
            [name for name, _ in client.calls].count("put_object"), 1
        )


if __name__ == "__main__":
    unittest.main()
