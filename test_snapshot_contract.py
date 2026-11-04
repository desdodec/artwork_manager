"""Offline snapshot boundary regressions; no services or credentials."""
import unittest
from publisher import ownership_for
from offline_ownership_fixtures import preflight

class SnapshotContractTests(unittest.TestCase):
    def setUp(self):
        self.row = dict(street_key='street', front_mockup='street-face/hebden-bridge/v2/mockups/front.png', rear_mockup='street-face/hebden-bridge/v2/mockups/rear.png', production_png='street-face/hebden-bridge/v2/print_candidates/production.png')
        self.items = [dict(street_key='street', front_key=self.row['front_mockup'], rear_key=self.row['rear_mockup'], production_key=self.row['production_png'])]
    def test_unrelated_conflicts_do_not_block_selection(self):
        response=preflight([self.row])
        response['png_ownership'] += [dict(key='other.png'),dict(key='other.png')]
        self.assertEqual(len(ownership_for(self.items,response)),3)
    def test_malformed_selected_assignments_fail_closed(self):
        for value in ['', [], None, 123]:
            response=preflight([self.row]);response['png_ownership'][0]['design_id']=value
            with self.subTest(value=value), self.assertRaises(RuntimeError): ownership_for(self.items,response)
    def test_inconsistent_selected_asset_identity_fails_closed(self):
        response=preflight([self.row]);response['png_ownership'][0]['asset_id']='different'
        with self.assertRaises(RuntimeError): ownership_for(self.items,response)
    def test_oversized_inventory_fails_closed(self):
        response=preflight([self.row]);response['png_ownership'] *= 2001
        with self.assertRaisesRegex(RuntimeError,'limit exceeded'): ownership_for(self.items,response)

    def test_transport_byte_ceiling_blocks_oversized_response(self):
        from unittest.mock import patch, MagicMock
        from publisher import _api
        response=MagicMock()
        response.__enter__.return_value.read.return_value=b' ' * 8388609
        with patch('publisher.urlopen', return_value=response), self.assertRaisesRegex(RuntimeError,'limit exceeded'):
            _api('https://offline.invalid','offline',{})
