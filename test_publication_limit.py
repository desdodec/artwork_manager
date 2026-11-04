"""Offline publication request boundaries, separate from catalogue inventory size."""
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import publisher
from offline_ownership_fixtures import preflight


class PublicationLimitTests(unittest.TestCase):
    def prepare(self, count, *, upload_only=False, check_plan=None):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'source'
            source.write_bytes(b'offline artwork')
            rows, files, features = [], [], []
            # A full 2,000-design inventory must not count as the selection.
            for n in range(2000):
                row = dict(street_key=f'street-{n}', street_name=f'Street {n}')
                for slot in publisher.LOCAL_SLOTS:
                    directory = 'mockups' if slot.endswith('mockup') else 'print_candidates'
                    ext = 'svg' if slot.endswith('svg') else 'png'
                    row[slot] = f'{publisher.DATASET}/{directory}/{n}-{slot}.{ext}'
                    if n < count:
                        files.append((source, row[slot]))
                rows.append(row)
                if n < count:
                    features.append(dict(properties=dict(street_key=row['street_key']),
                        geometry=dict(type='LineString', coordinates=[[0, 0], [1, 1]])))
            selected = rows[:count]
            release = SimpleNamespace(rows=selected, issues=[], prefix=publisher.DATASET, files=files)
            client = Mock()
            with patch.object(publisher, '_api', return_value=preflight(rows)) as api, \
                    patch.object(publisher, '_r2_head', return_value=None) as head:
                try:
                    plan = publisher.prepare(release, selected, dict(features=features), client,
                        'offline-bucket', 'offline-endpoint', 'x'*64, 'partial', root / 'reports',
                        lambda _: None, threading.Event(), upload_only=upload_only)
                except ValueError:
                    api.assert_not_called()
                    head.assert_not_called()
                    self.assertEqual(client.mock_calls, [])
                    self.assertFalse((root / 'reports').exists())
                    raise
                self.assertEqual(plan['design_count'], count)
                self.assertEqual(plan['object_count'], count * 3)
                self.assertEqual(head.call_count, count * 3)
                self.assertEqual(client.mock_calls, [])
                if check_plan:
                    check_plan(plan, root)
                return plan

    def test_1000_selected_allowed_with_2000_inventory(self):
        self.prepare(1000)

    def test_1001_rejected_before_any_remote_call_or_plan(self):
        with self.assertRaisesRegex(ValueError, 'at most 1,000.*1,001 selected.*Select fewer'):
            self.prepare(1001)

    def test_existing_290_design_workflow(self):
        self.prepare(290)

    def test_upload_only_1001_allowed_without_catalogue_request(self):
        self.prepare(1001, upload_only=True)

    def test_direct_publish_rejects_1001_upload_only_plan_before_remote_operations(self):
        def check(plan, root):
            client = Mock()
            with patch.object(publisher, '_api') as api, \
                    patch.object(publisher, '_r2_head') as head, \
                    patch.object(publisher, '_backup_existing_objects') as backup, \
                    patch('socket.socket', side_effect=AssertionError('Network forbidden')):
                with self.assertRaisesRegex(ValueError, 'Publish Collection allows at most 1,000.*1,001 selected'):
                    publisher.publish(plan, client, 'offline-bucket', 'offline-endpoint',
                                      'x'*64, root / 'reports', lambda _: None, threading.Event())
                api.assert_not_called()
                head.assert_not_called()
                backup.assert_not_called()
                self.assertEqual(client.mock_calls, [])
                self.assertEqual(list((root / 'reports').glob('publication_run_*')), [])
        self.prepare(1001, upload_only=True, check_plan=check)

    def assert_reaches_preflight(self, count, runner):
        def check(plan, root):
            client = Mock()
            # Stop at the first mocked request: no remote service is contacted.
            with patch.object(publisher, '_api', side_effect=RuntimeError('offline preflight reached')) as api:
                with self.assertRaisesRegex(RuntimeError, '^offline preflight reached$'):
                    runner(plan, client, 'offline-bucket', 'offline-endpoint', 'x'*64,
                           root / 'reports', lambda _: None, threading.Event())
                api.assert_called_once_with('offline-endpoint', 'x'*64,
                    {'mode': 'preflight', 'dataset_id': publisher.DATASET})
                self.assertEqual(client.mock_calls, [])
        self.prepare(count, upload_only=True, check_plan=check)

    def test_direct_publish_allows_1000_design_boundary(self):
        self.assert_reaches_preflight(1000, publisher.publish)

    def test_direct_upload_only_preserves_1001_design_exemption(self):
        self.assert_reaches_preflight(1001, publisher.upload_artwork_only)

    def test_edge_and_desktop_share_request_limit(self):
        edge = Path('collection-publish-edge.ts').read_text()
        self.assertIn(f'const MAX_PUBLISH_DESIGNS = {publisher.MAX_PUBLISH_DESIGNS};', edge)
        self.assertIn('items.length>MAX_PUBLISH_DESIGNS', edge)
        self.assertIn('data[k]>2000', edge)
