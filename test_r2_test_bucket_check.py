"""Offline tests for strict read-only test-bucket access check."""
import unittest

from r2_test_bucket_check import TEST_BUCKET, TEST_ROOT, check_test_bucket


class FakeR2:
    def __init__(self, keys=None):
        self.calls = []
        self.keys = keys or []

    def head_bucket(self, **kwargs):
        self.calls.append(("head_bucket", kwargs))
        return {}

    def list_objects_v2(self, **kwargs):
        self.calls.append(("list_objects_v2", kwargs))
        return {"Contents": [{"Key": key} for key in self.keys]}


class TestBucketCheckTests(unittest.TestCase):
    def test_only_expected_read_calls(self):
        client = FakeR2()
        result = check_test_bucket(client)
        self.assertTrue(result["read_only"])
        self.assertFalse(result["test_prefix_has_objects"])
        self.assertEqual([method for method, _ in client.calls],
                         ["head_bucket", "list_objects_v2"])
        self.assertEqual(client.calls[1][1]["Prefix"], TEST_ROOT)
        self.assertEqual(client.calls[1][1]["MaxKeys"], 5)

    def test_production_bucket_rejected_without_calls(self):
        client = FakeR2()
        with self.assertRaises(ValueError):
            check_test_bucket(client, "mug-print-assets")
        self.assertEqual(client.calls, [])

    def test_arbitrary_bucket_rejected_without_calls(self):
        client = FakeR2()
        with self.assertRaises(ValueError):
            check_test_bucket(client, "unrelated-bucket")
        self.assertEqual(client.calls, [])

    def test_existing_test_objects_are_visible(self):
        client = FakeR2([TEST_ROOT + "example.png"])
        result = check_test_bucket(client, TEST_BUCKET)
        self.assertTrue(result["test_prefix_has_objects"])
        self.assertEqual(result["sample_object_count"], 1)


if __name__ == "__main__":
    unittest.main()
