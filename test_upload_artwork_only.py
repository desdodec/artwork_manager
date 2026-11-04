from offline_ownership_fixtures import preflight

"""Offline upload-only and desktop progress regression checks; no cloud clients."""
import json
import queue
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import publisher
import test_publish_again as fixtures
from app import App


class UploadOnlyTests(unittest.TestCase):
    setUp = fixtures.PublishAgainTests.setUp
    preview = fixtures.PublishAgainTests.preview

    def fake_api(self, endpoint, token, body, timeout=35):
        # Fail the test at the API boundary if ANY non-read-only mode is used.
        self.assertEqual(body["mode"], "preflight")
        self.api_calls.append(body["mode"])
        return preflight(self.mapped, currently_listed_keys=[])

    def upload(self, plan, stop=None):
        return publisher.upload_artwork_only(
            plan,
            self.r2,
            "fake-bucket",
            "offline.invalid",
            "offline-token",
            self.report,
            lambda _: None,
            stop or threading.Event(),
        )

    def journal(self, plan):
        return json.loads(
            (
                self.report / ("publication_run_" + plan["operation_id"] + ".json")
            ).read_text()
        )

    def test_success_then_fresh_retry_skips_every_file_without_commit(self):
        with patch("publisher._api", side_effect=self.fake_api):
            plan = self.preview()
            self.assertEqual(
                self.upload(plan), dict(uploaded=3, skipped=0, failed=0, verified=3)
            )
            audit = self.journal(plan)
            self.assertTrue(audit["artwork_complete"])
            self.assertFalse(audit["complete"])
            self.assertIsNone(audit["database_result"])
            self.assertEqual(len(audit["backups"]), 3)
            self.r2.uploads.clear()
            self.assertEqual(
                self.upload(self.preview()),
                dict(uploaded=0, skipped=3, failed=0, verified=3),
            )
            self.assertEqual(self.r2.uploads, [])
            self.assertEqual(plan["items"][0]["street_key"], "example-1")

    def test_failed_upload_preserves_recovery_and_fresh_retry_never_commits(self):
        with patch("publisher._api", side_effect=self.fake_api):
            plan = self.preview()
            self.r2.fail_at = 2
            with self.assertRaisesRegex(RuntimeError, "simulated upload failure"):
                self.upload(plan)
            audit = self.journal(plan)
            self.assertFalse(audit["complete"])
            self.assertEqual(len(audit["restore_needed"]), 2)
            self.assertEqual(audit["counts"]["failed"], 1)
            self.r2.fail_at = None
            self.r2.uploads.clear()
            retry = self.preview()
            self.assertNotEqual(plan["operation_id"], retry["operation_id"])
            self.assertEqual(
                self.upload(retry), dict(uploaded=1, skipped=2, failed=0, verified=3)
            )
            self.assertEqual(self.r2.uploads, self.keys[2:3])

    def test_verification_failure_cannot_commit_and_retains_overwrite_intent(self):
        with patch("publisher._api", side_effect=self.fake_api):
            plan = self.preview()
            with patch(
                "publisher._verify_artwork", side_effect=RuntimeError("bad bytes")
            ):
                with self.assertRaisesRegex(RuntimeError, "bad bytes"):
                    self.upload(plan)
            audit = self.journal(plan)
            self.assertEqual(
                audit["counts"], dict(uploaded=1, skipped=0, failed=1, verified=0)
            )
            self.assertEqual(len(audit["restore_needed"]), 1)
            self.assertIsNone(audit["database_result"])

    def test_new_files_and_determinate_progress(self):
        self.r2.objects.clear()
        progress = Mock()
        with patch("publisher._api", side_effect=self.fake_api):
            plan = self.preview()
            result = publisher.upload_artwork_only(
                plan,
                self.r2,
                "fake-bucket",
                "offline.invalid",
                "offline-token",
                self.report,
                lambda _: None,
                threading.Event(),
                progress=progress,
            )
        self.assertEqual(result["uploaded"], 3)
        self.assertEqual(progress.call_args_list[0].args, (0, 3))
        self.assertEqual(progress.call_args_list[-1].args, (3, 3))

    def test_cancelled_upload_preserves_journal_without_commit(self):
        stop = threading.Event()
        stop.set()
        with patch("publisher._api", side_effect=self.fake_api):
            plan = self.preview()
            with self.assertRaisesRegex(RuntimeError, "Stopped during backup"):
                self.upload(plan, stop)
        self.assertEqual(self.r2.uploads, [])
        self.assertFalse(self.journal(plan)["complete"])

    def test_unsafe_preflight_blocks_writes(self):
        with patch("publisher._api", side_effect=self.fake_api):
            plan = self.preview()
        with patch("publisher._api", return_value={"safe_to_upload": False}) as api:
            with self.assertRaisesRegex(RuntimeError, "safety check failed"):
                self.upload(plan)
        self.assertEqual(api.call_args.args[2]["mode"], "preflight")
        self.assertEqual(self.r2.uploads, [])
        self.assertEqual(self.r2.copies, [])


class DesktopProgressTests(unittest.TestCase):
    def app(self):
        app = SimpleNamespace(
            events=queue.Queue(),
            progress=Mock(),
            _log=Mock(),
            after=Mock(),
            release=None,
            busy=True,
        )
        app._poll = Mock()
        app._reset_progress = lambda: App._reset_progress(app)
        for name in (
            "validate",
            "upload",
            "test",
            "print",
            "replace",
            "publish",
            "artwork",
            "preflight",
        ):
            setattr(app, name + "_button", Mock())
        return app

    def test_upload_confirmation_stops_animation_before_dialog_and_cancel(self):
        app = self.app()
        plan = {"summary": {"NEW": 2, "REPLACE": 1, "SAME": 1}}
        app.events.put(("confirm_artwork", (plan, None, "named-bucket", "", "", None)))
        app.events.put(("done", None))

        def dialog(title, text):
            app.progress.stop.assert_called()
            self.assertIn("named-bucket", text)
            self.assertIn("Supabase catalogue records will NOT change", text)
            return False

        with patch("app.messagebox.askyesno", side_effect=dialog):
            App._poll(app)
        self.assertFalse(app.busy)
        self.assertEqual(app.progress.configure.call_args.kwargs["value"], 0)
        # Only the next UI poll is scheduled; no upload is scheduled on cancellation.
        self.assertEqual(app.after.call_count, 1)

    def test_upload_dispatch_never_uses_publication_action(self):
        app = self.app()
        app.cancel = threading.Event()
        app._work = lambda work, **kw: work()
        values = ({}, None, "fake-bucket", "offline.invalid", "offline-token", None)
        with patch(
            "app.upload_artwork_only", return_value={"verified": 4}
        ) as upload, patch(
            "app.apply_publication", side_effect=AssertionError("Publication forbidden")
        ):
            App._apply_publish_after_confirmation(app, values, upload_only=True)
        upload.assert_called_once()

    def test_error_and_done_reset_progress(self):
        app = self.app()
        app.events.put(("progress", (3, 4)))
        app.events.put(("error", "verification failed"))
        app.events.put(("done", None))

        def dialog(*_):
            self.assertEqual(app.progress.configure.call_args.kwargs["value"], 0)

        with patch("app.messagebox.showerror", side_effect=dialog):
            App._poll(app)
        self.assertFalse(app.busy)
        self.assertEqual(app.progress.configure.call_args.kwargs["value"], 0)
