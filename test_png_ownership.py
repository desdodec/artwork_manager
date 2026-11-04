"""Offline ownership and three-PNG boundary checks; no network or credentials."""

import copy
import json
import threading
import unittest
from unittest.mock import patch
import publisher
import test_publish_again as fixtures
from offline_ownership_fixtures import preflight


class OwnershipTests(unittest.TestCase):
    setUp = fixtures.PublishAgainTests.setUp
    preview = fixtures.PublishAgainTests.preview
    do_publish = fixtures.PublishAgainTests.do_publish

    def response(self):
        return preflight(self.mapped)

    def test_four_local_sources_three_remote_destinations_and_svg_retained(self):
        with patch(
            "publisher._api",
            side_effect=lambda e, t, b, **kw: (
                self.response()
                if b["mode"] == "preflight"
                else {"db_result": {"status": "published"}}
            ),
        ):
            plan = self.preview()
            self.assertEqual(len(self.release.files), 4)
            self.assertEqual(plan["object_count"], 3)
            self.do_publish(plan)
        self.assertEqual(self.r2.objects[self.keys[3]], self.original[3])
        self.assertTrue(self.release.files[3][0].is_file())
        self.assertNotIn("svg_key", plan["items"][0])
        self.assertTrue(all(key.endswith(".png") for key in self.r2.uploads))
        self.assertTrue(
            all(not source.endswith(".svg") for source, _ in self.r2.copies)
        )

    def test_290_selected_streets_plan_exactly_870_pngs(self):
        base = self.mapped[0]
        sources = {key: path for path, key in self.release.files}
        files = []
        mapped = []
        features = []
        for index in range(290):
            row = dict(base, street_key="permanent-" + str(index))
            for slot in publisher.LOCAL_SLOTS:
                key = base[slot].replace("sample", str(index) + "_sample")
                row[slot] = key
                files.append((sources[base[slot]], key))
            mapped.append(row)
            features.append(
                dict(
                    properties={"street_key": row["street_key"]},
                    geometry=self.geo["features"][0]["geometry"],
                )
            )
        self.mapped = mapped
        self.geo = {"features": features}
        self.release.rows = mapped
        self.release.files = files
        self.r2.objects.clear()
        with patch("publisher._api", return_value=self.response()):
            plan = self.preview()
        self.assertEqual(plan["design_count"], 290)
        self.assertEqual(plan["object_count"], 870)
        self.assertEqual(len(files), 1160)
        self.assertEqual(plan["summary"], {"NEW": 870})
        self.assertFalse(self.r2.uploads)

    def test_missing_local_svg_is_still_rejected(self):
        self.release.files = self.release.files[:3]
        with patch("publisher._api", return_value=self.response()):
            with self.assertRaisesRegex(RuntimeError, "Missing local source"):
                self.preview()

    def test_missing_incomplete_duplicate_and_cross_identity_ownership_block_before_r2(
        self,
    ):
        cases = []
        cases.append({})
        r = self.response()
        r["ownership_complete"] = False
        cases.append(r)
        r = self.response()
        r["png_ownership"].pop()
        cases.append(r)
        r = self.response()
        r["png_ownership"].append(copy.deepcopy(r["png_ownership"][0]))
        cases.append(r)
        for field, value in [
            ("dataset_id", "other"),
            ("street_key", "other"),
            ("role", "rear"),
            ("product_sku", "other"),
            ("version", 2),
            ("status", "live"),
        ]:
            r = self.response()
            r["png_ownership"][0][field] = value
            cases.append(r)
        for response in cases:
            with self.subTest(response=response), patch(
                "publisher._api", return_value=response
            ), patch.object(
                self.r2,
                "head_object",
                side_effect=AssertionError("No R2 reads permitted"),
            ):
                if not response:
                    response["safe_to_upload"] = True
                with self.assertRaises(RuntimeError):
                    self.preview()
        self.assertFalse(self.r2.uploads)
        self.assertFalse(self.r2.copies)

    def test_changed_owner_ids_and_unexplained_plan_keys_block_before_backups(self):
        for action in ("design_id", "asset_id", "extra", "svg"):
            with self.subTest(action=action), patch(
                "publisher._api", return_value=self.response()
            ):
                plan = self.preview()
                response = self.response()
                if action in ("design_id", "asset_id"):
                    response["png_ownership"][0][action] = "changed"
                elif action == "extra":
                    plan["operations"].append(
                        dict(
                            plan["operations"][0],
                            key=publisher.DATASET + "/mockups/unexplained.png",
                        )
                    )
                else:
                    plan["operations"][0]["key"] = self.keys[3]
                with patch("publisher._api", return_value=response):
                    with self.assertRaises(RuntimeError):
                        self.do_publish(plan)
                self.assertFalse(self.r2.copies)
                self.assertFalse(self.r2.uploads)

    def test_new_upload_client_error_records_uncertain_remote_write(self):
        self.r2.objects.clear()
        with patch("publisher._api", return_value=self.response()):
            plan = self.preview()
            self.r2.fail_at = 1
            with self.assertRaisesRegex(RuntimeError, "simulated upload failure"):
                self.do_publish(plan)
        audit = json.loads(
            (
                self.report / ("publication_run_" + plan["operation_id"] + ".json")
            ).read_text()
        )
        self.assertEqual(audit["upload_in_progress"]["key"], self.keys[0])
        self.assertEqual(audit["counts"]["uploaded"], 0)
        self.assertEqual(audit["counts"]["verified"], 0)
        self.assertEqual(self.r2.objects[self.keys[0]], self.updated[0])
        self.assertIsNone(audit["database_result"])


class ReservationTests(unittest.TestCase):
    def app(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        import queue

        app = SimpleNamespace(
            busy=False,
            cancel=threading.Event(),
            events=queue.Queue(),
            progress=Mock(),
            _reset_progress=Mock(),
        )
        for name in (
            "validate",
            "upload",
            "replace",
            "test",
            "print",
            "publish",
            "artwork",
            "preflight",
        ):
            setattr(app, name + "_button", Mock())
        return app

    def test_preview_handoff_keeps_reservation_and_cancellation_for_both_actions(self):
        from app import App
        from unittest.mock import Mock

        class ImmediateThread:
            def __init__(self, target, **kwargs):
                self.target = target

            def start(self):
                self.target()

        for upload_only in (True, False):
            with self.subTest(upload_only=upload_only):
                app = self.app()
                app._work = lambda fn, **kw: App._work(app, fn, **kw)

                def preview():
                    app.cancel.set()
                    return True

                with patch("app.threading.Thread", ImmediateThread):
                    App._work(app, preview)
                    self.assertTrue(app.busy)
                    self.assertTrue(app.events.empty())
                    other = Mock()
                    App._work(app, other)
                    other.assert_not_called()

                    def action(*args, **kw):
                        self.assertTrue(app.cancel.is_set())
                        return {}

                    with patch(
                        "app.upload_artwork_only", side_effect=action
                    ) as upload, patch(
                        "app.apply_publication", side_effect=action
                    ) as publish:
                        App._apply_publish_after_confirmation(
                            app, ({}, None, "bucket", "url", "token", None), upload_only
                        )
                        self.assertEqual(upload.call_count, int(upload_only))
                        self.assertEqual(publish.call_count, int(not upload_only))
                    self.assertEqual(
                        [e for e, _ in list(app.events.queue)].count("done"), 1
                    )
