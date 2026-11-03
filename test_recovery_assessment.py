"""Offline tests of recovery report. Run: python -m unittest test_recovery_assessment.py -v"""
import json
import tempfile
import unittest
from pathlib import Path

from publisher import DATASET
from recovery_assessment import assess_recovery, format_assessment


class RecoveryAssessmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "publication_run_demo.json"
        self.operation = "00000000-0000-4000-8000-000000000002"
        self.original = DATASET + "/mockups/example.png"
        self.backup = DATASET + "/_publication_backups/" + self.operation + "/mockups/example.png"
        self.audit = {
            "operation_id": self.operation, "complete": False,
            "backup_state": "complete", "backup_total": 1,
            "backups": [{"original_key": self.original, "backup_key": self.backup, "sha256": "0"*64}],
            "files": [],
        }

    def assess(self):
        self.path.write_text(json.dumps(self.audit), encoding="utf-8")
        return assess_recovery(self.path)

    def test_backup_only_is_not_claimed_to_be_remotely_verified(self):
        r = self.assess()
        self.assertEqual(r["state"], "backups_only")
        self.assertFalse(r["remote_verified_now"])
        self.assertIn("NOT CHECKED", format_assessment(r))

    def test_completed_overwrite_is_recovery_candidate(self):
        self.audit["files"] = [{"key": self.original, "status": "REPLACE"}]
        r = self.assess()
        self.assertEqual(r["potentially_changed_keys"], [self.original])
        self.assertEqual(r["missing_backup_keys"], [])
        self.assertEqual(r["state"], "manual_reconciliation_required")

    def test_uncaught_process_crash_preserves_pending_overwrite(self):
        self.audit["overwrite_in_progress"] = {
            "original_key": self.original, "backup_key": self.backup}
        r = self.assess()
        self.assertEqual(r["interrupted_replacement"], self.original)
        self.assertEqual(r["potentially_changed_keys"], [self.original])

    def test_missing_backup_is_flagged(self):
        self.audit["backups"] = []
        self.audit["files"] = [{"key": self.original, "status": "REPLACE"}]
        r = self.assess()
        self.assertEqual(r["missing_backup_keys"], [self.original])
        self.assertTrue(r["concerns"])

    def test_published_success_does_not_suggest_automatic_restore(self):
        self.audit["complete"] = True
        self.audit["files"] = [{"key": self.original, "status": "REPLACE"}]
        r = self.assess()
        self.assertEqual(r["state"], "published_reported")
        self.assertIn("do not restore", r["advice"])

    def test_rejects_invalid_audit(self):
        self.path.write_text('{"operation_id":"abc"}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "files and backups"):
            assess_recovery(self.path)

    def test_incomplete_backup_phase_with_mutation_is_warning(self):
        self.audit["backup_state"] = "in_progress"
        self.audit["files"] = [{"key": self.original, "status": "REPLACE"}]
        r = self.assess()
        self.assertTrue(any("full backup phase" in c for c in r["concerns"]))


if __name__ == "__main__":
    unittest.main()
