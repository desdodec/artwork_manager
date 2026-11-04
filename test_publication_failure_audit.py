"""End-to-end offline publication failure -> saved audit -> recovery assessment.

Exercises the REAL publisher.publish and recovery_assessment.assess_recovery.
All R2 operations and the Supabase API are simulated. No cloud calls.
Run: python -m unittest test_publication_failure_audit.py -v
"""
from __future__ import annotations

import json
from unittest.mock import patch

from recovery_assessment import assess_recovery
import test_publisher_backup as backup_tests


class PublicationFailureAuditTests(backup_tests.PublishRecoveryTests):
    """Reuse the tested two-object R2 simulator and real publisher."""

    # Prevent inherited test duplication when both suites are invoked.
    test_upload_failure_after_second_mutation_records_both = None
    test_unexpected_crash_preserves_prewrite_intent = None
    test_database_rejection_after_uploads_records_recovery = None

    def _assessment(self):
        path = self.folder / ("publication_run_" + self.plan["operation_id"] + ".json")
        return assess_recovery(path)

    def test_journal_is_persisted_before_each_remote_overwrite(self):
        observed = []
        original_upload = self.r2.upload_file
        audit_path = self.folder / ("publication_run_" + self.plan["operation_id"] + ".json")

        def guarded_upload(Filename, Bucket, Key, ExtraArgs):
            # This callback runs before remote mutation; inspect *on-disk* JSON.
            saved = json.loads(audit_path.read_text(encoding="utf-8"))
            pending = saved.get("overwrite_in_progress")
            self.assertIsInstance(pending, dict)
            self.assertEqual(pending["original_key"], Key)
            self.assertEqual(saved["backup_state"], "complete")
            self.assertEqual(len(saved["backups"]), 2)
            self.assertTrue(any(b["backup_key"] == pending["backup_key"]
                                for b in saved["backups"]))
            observed.append(Key)
            return original_upload(Filename, Bucket, Key, ExtraArgs)

        self.r2.fail_at = 2
        self.r2.upload_file = guarded_upload
        with patch("publisher._api", side_effect=self.fake_api):
            with self.assertRaisesRegex(RuntimeError, "simulated upload failure"):
                self.run_publish()
        self.assertEqual(observed, self.keys)

    def test_failed_second_upload_is_recoverable_in_journal(self):
        self.r2.fail_at = 2
        with patch("publisher._api", side_effect=self.fake_api):
            with self.assertRaisesRegex(RuntimeError, "simulated upload failure"):
                self.run_publish()
        assessment = self._assessment()
        self.assertEqual(assessment["state"], "manual_reconciliation_required")
        self.assertEqual(assessment["completed_replacements"], 1)
        self.assertEqual(assessment["interrupted_replacement"], self.keys[1])
        self.assertEqual(set(assessment["potentially_changed_keys"]), set(self.keys))
        self.assertEqual(assessment["missing_backup_keys"], [])
        self.assertFalse(assessment["remote_verified_now"])
        self.assertTrue(self.api_calls and set(self.api_calls) == {"preflight"})

    def test_abrupt_crash_is_detected_even_without_restore_needed(self):
        self.r2.crash_at = 1
        with patch("publisher._api", side_effect=self.fake_api):
            with self.assertRaises(backup_tests.SimulatedCrash):
                self.run_publish()
        assessment = self._assessment()
        self.assertEqual(assessment["state"], "manual_reconciliation_required")
        self.assertEqual(assessment["interrupted_replacement"], self.keys[0])
        self.assertEqual(assessment["completed_replacements"], 0)
        self.assertEqual(assessment["potentially_changed_keys"], [self.keys[0]])
        self.assertEqual(assessment["missing_backup_keys"], [])

    def test_supabase_commit_rejection_marks_both_for_reconciliation(self):
        def reject_commit(endpoint, token, body, timeout=35):
            self.api_calls.append(body["mode"])
            if body["mode"] == "preflight":
                return {"safe_to_upload": True}
            raise RuntimeError("simulated commit rejection")

        with patch("publisher._api", side_effect=reject_commit):
            with self.assertRaisesRegex(RuntimeError, "simulated commit rejection"):
                self.run_publish()
        assessment = self._assessment()
        self.assertEqual(set(assessment["potentially_changed_keys"]), set(self.keys))
        self.assertEqual(assessment["completed_replacements"], 2)
        self.assertIsNone(assessment["interrupted_replacement"])
        self.assertEqual(assessment["missing_backup_keys"], [])
        self.assertEqual(self.api_calls, ["preflight"] * 6 + ["commit"])

    def test_corrupt_backup_prevents_any_original_upload(self):
        self.r2.corrupt_copies = True
        with patch("publisher._api", side_effect=self.fake_api):
            with self.assertRaisesRegex(RuntimeError, "backup contents differ"):
                self.run_publish()
        assessment = self._assessment()
        self.assertEqual(self.r2.uploads, [])
        self.assertEqual(self.r2.objects[self.keys[0]], self.old[0])
        self.assertEqual(self.r2.objects[self.keys[1]], self.old[1])
        self.assertEqual(assessment["state"], "no_recorded_overwrites")
        self.assertEqual(assessment["completed_replacements"], 0)


if __name__ == "__main__":
    import unittest
    unittest.main()
