"""Offline guardrail tests for plan-only R2 integration harness."""
import unittest
import uuid
from pathlib import Path

from r2_isolated_test import ROOT, PRODUCTION, build_plan


class IsolatedR2PlanTests(unittest.TestCase):
    def test_all_test_keys_are_distinct_and_isolated(self):
        run_id = str(uuid.uuid4())
        plan = build_plan(run_id)
        self.assertEqual(plan["mode"], "PLAN_ONLY")
        self.assertEqual(plan["run_id"], run_id)
        self.assertTrue(plan["prefix"].startswith(ROOT + "/"))
        self.assertEqual(len(plan["objects"]), len(set(plan["objects"].values())))
        for key in plan["objects"].values():
            self.assertTrue(key.startswith(plan["prefix"]))
            self.assertFalse(key.startswith(PRODUCTION))

    def test_cloud_writes_and_deletes_forbidden(self):
        plan = build_plan(str(uuid.uuid4()))
        self.assertFalse(plan["writes_allowed"])
        self.assertFalse(plan["deletes_allowed"])
        self.assertEqual(plan["cloud_calls_performed"], 0)

    def test_rejects_bad_or_path_like_run_ids(self):
        for invalid in ("../production", "abc", "", "a/b", str(uuid.uuid1()), "0"*36):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    build_plan(invalid)

    def test_repeatable_plan_has_stable_keys(self):
        run_id = str(uuid.uuid4())
        self.assertEqual(build_plan(run_id), build_plan(run_id))

    def test_no_network_or_storage_client_dependencies(self):
        # A deliberately simple structural check: the plan file must stay offline.
        source = (Path(__file__).parent / "r2_isolated_test.py").read_text(encoding="utf-8")
        for forbidden in ("import boto3", "import requests", "urlopen(",
                          "upload_file(", "copy_object(", "put_object(",
                          "delete_object(", "S3Client("):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
