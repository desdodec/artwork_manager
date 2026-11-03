"""Offline tests of isolated R2 test credential storage."""
import unittest

from r2_test_credentials import (
    PRODUCTION_SERVICE, TEST_SERVICE, store_test_credentials,
)


class FakeKeyring:
    def __init__(self):
        self.entries = {(PRODUCTION_SERVICE, "account"): "existing-account",
                        (PRODUCTION_SERVICE, "access"): "prod-access",
                        (PRODUCTION_SERVICE, "secret"): "prod-secret"}

    def set_password(self, service, field, value):
        self.entries[(service, field)] = value


class TestCredentialIsolation(unittest.TestCase):
    def test_separate_storage_preserves_all_production_entries(self):
        store = FakeKeyring()
        before = {k: v for k, v in store.entries.items()}
        store_test_credentials(store, "abc123", "test-access", "test-secret")
        for key, value in before.items():
            self.assertEqual(store.entries[key], value)
        self.assertNotEqual(TEST_SERVICE, PRODUCTION_SERVICE)
        self.assertEqual(store.entries[(TEST_SERVICE, "secret")], "test-secret")

    def test_missing_inputs_do_not_store_any_credentials(self):
        for values in (("abc123", "", "secret"), ("", "access", "secret"),
                       ("abc123", "access", "")):
            with self.subTest(values=values):
                store = FakeKeyring()
                before = store.entries.copy()
                with self.assertRaises(ValueError):
                    store_test_credentials(store, *values)
                self.assertEqual(store.entries, before)

    def test_invalid_account_id_does_not_store(self):
        store = FakeKeyring()
        with self.assertRaises(ValueError):
            store_test_credentials(store, "https://example.com", "key", "secret")
        self.assertFalse(any(key[0] == TEST_SERVICE for key in store.entries))


if __name__ == "__main__":
    unittest.main()
