"""Deterministic offline interruptions against the real runner and journal parser."""
import threading
import unittest
from unittest.mock import patch

import publisher
from offline_ownership_fixtures import preflight
from recovery_assessment import assess_recovery
import test_upload_artwork_only as fixtures


class SafetyRegressions(unittest.TestCase):
    setUp = fixtures.UploadOnlyTests.setUp
    preview = fixtures.UploadOnlyTests.preview
    fake_api = fixtures.UploadOnlyTests.fake_api
    upload = fixtures.UploadOnlyTests.upload
    def assess(self, plan):
        return assess_recovery(self.report / ('publication_run_' + plan['operation_id'] + '.json'))

    def test_cancellation_at_each_blocking_boundary_prevents_next_write(self):
        for boundary in ('backup_head', 'upload_head', 'hash', 'journal', 'verification'):
            with self.subTest(boundary=boundary):
                self.setUp()
                stop = threading.Event()
                with patch('publisher._api', side_effect=self.fake_api):
                    plan = self.preview()
                    head = publisher._r2_head
                    digest = publisher._digest
                    replace = publisher.os.replace
                    verify = publisher._verify_artwork

                    def on_head(client, bucket, key):
                        result = head(client, bucket, key)
                        if ((boundary == 'backup_head' and '/_publication_backups/' in key) or
                                (boundary == 'upload_head' and len(self.r2.copies) == 3)):
                            stop.set()
                        return result

                    def on_digest(path):
                        result = digest(path)
                        if boundary == 'hash':
                            stop.set()
                        return result

                    def on_save(source, destination):
                        replace(source, destination)
                        if boundary == 'journal' and 'upload_in_progress' in destination.read_text():
                            stop.set()

                    def on_verify(*args):
                        result = verify(*args)
                        if boundary == 'verification':
                            stop.set()
                        return result

                    with patch('publisher._r2_head', side_effect=on_head), \
                            patch('publisher._digest', side_effect=on_digest), \
                            patch('publisher.os.replace', side_effect=on_save), \
                            patch('publisher._verify_artwork', side_effect=on_verify):
                        with self.assertRaisesRegex(RuntimeError, 'Stopped'):
                            self.upload(plan, stop)
                self.assertEqual(len(self.r2.uploads), 1 if boundary == 'verification' else 0)
                if boundary == 'backup_head':
                    self.assertEqual(self.r2.copies, [])

    def test_changed_safety_or_owner_at_each_phase_aborts_next_write(self):
        # Initial check, first backup, post-backup, first upload, later upload.
        for check, copies, uploads in ((1, 0, 0), (2, 0, 0), (5, 3, 0), (6, 3, 0), (7, 3, 1)):
            for change in ('safe_to_upload', 'design_id', 'asset_id', 'role', 'street_key', 'status'):
                with self.subTest(check=check, change=change):
                    self.setUp()
                    with patch('publisher._api', side_effect=self.fake_api):
                        plan = self.preview()
                    calls = 0

                    def changed(*args, **kwargs):
                        nonlocal calls
                        calls += 1
                        result = preflight(self.mapped)
                        if calls == check:
                            if change == 'safe_to_upload':
                                result[change] = False
                            else:
                                result['png_ownership'][0][change] = 'changed'
                        return result

                    with patch('publisher._api', side_effect=changed):
                        with self.assertRaises(RuntimeError):
                            self.upload(plan)
                    self.assertEqual(len(self.r2.copies), copies)
                    self.assertEqual(len(self.r2.uploads), uploads)

    def test_actual_uncertain_new_and_replacement_journals(self):
        for new in (False, True):
            with self.subTest(new=new):
                self.setUp()
                if new:
                    self.r2.objects.clear()
                with patch('publisher._api', side_effect=self.fake_api):
                    plan = self.preview()
                    self.r2.fail_at = 1
                    with self.assertRaises(RuntimeError):
                        self.upload(plan)
                result = self.assess(plan)
                self.assertEqual(result['state'], 'manual_reconciliation_required')
                field = 'uncertain_new_keys' if new else 'uncertain_replacement_keys'
                self.assertEqual(result[field], self.keys[:1])
                self.assertFalse(result['publication_complete_reported'])

    def test_actual_success_journal_is_artwork_only(self):
        with patch('publisher._api', side_effect=self.fake_api):
            plan = self.preview()
            self.upload(plan)
        result = self.assess(plan)
        self.assertEqual(result['state'], 'artwork_upload_reported')
        self.assertTrue(result['artwork_upload_complete_reported'])
        self.assertFalse(result['publication_complete_reported'])

    def test_old_preflight_without_snapshot_contract_is_blocked(self):
        with patch('publisher._api', side_effect=self.fake_api):
            plan = self.preview()
        response = preflight(self.mapped)
        del response['ownership_snapshot_version']
        with patch('publisher._api', return_value=response):
            with self.assertRaisesRegex(RuntimeError, 'incomplete'):
                self.upload(plan)
        self.assertEqual(self.r2.copies, [])
        self.assertEqual(self.r2.uploads, [])
