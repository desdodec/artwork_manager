"""Compare the owner's supplied shop source; never import or contact the shop.

Reference: desdodec/shop commit 1dc8ba49ac9916f1ce2b95a5276b6cf022487974,
catalogue_import/catalogue_import_validator.py. These helpers are reference-only;
publication must preserve Artwork Manager's current destinations.
"""
import re
import tempfile
import unittest
from pathlib import Path

from app import validate
from publisher import DATASET


def safe_name(name: str) -> str:
    value = name.strip().rstrip(".")
    value = re.sub(r"\s+", "_", value)
    return re.sub(r"[^A-Za-z0-9_'-]", "", value)


def keys(dataset_id: str, street_id: str, street_name: str):
    base = f"{dataset_id}_{street_id}_{safe_name(street_name)}"
    return (
        f"front/{base}_front.png",
        f"rear/{base}_rear.png",
        f"prodigi_h_mug_w/{base}.png",
    )


class ShopKeyCompatibilityTests(unittest.TestCase):
    def test_all_three_destinations_differ_and_manager_preserves_source_names(self):
        name = "St. George's Road."
        street_id = 'permanent-street-93'
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            for child in ('mockups', 'print_candidates'):
                (folder / child).mkdir()
            for child, suffix in (('mockups', '_front.png'), ('mockups', '_rear.png'),
                                  ('print_candidates', '.png'), ('print_candidates', '.svg')):
                (folder / child / f'0007_{name}_wrap_print{suffix}').write_bytes(b'local fixture')
            release = validate(folder, 'hebden-bridge', 'v2')
        self.assertEqual(release.issues, [])
        row = release.rows[0]
        manager = tuple(row[slot] for slot in ('front_mockup', 'rear_mockup', 'production_png'))
        expected_manager = (
            f"{DATASET}/mockups/0007_{name}_wrap_print_front.png",
            f"{DATASET}/mockups/0007_{name}_wrap_print_rear.png",
            f"{DATASET}/print_candidates/0007_{name}_wrap_print.png",
        )
        self.assertEqual(manager, expected_manager)
        shop = keys(DATASET, street_id, name)
        base = f"{DATASET}_{street_id}_St_George's_Road"
        self.assertEqual(shop, (f'front/{base}_front.png', f'rear/{base}_rear.png',
                                f'prodigi_h_mug_w/{base}.png'))
        self.assertTrue(all(actual != imported for actual, imported in zip(manager, shop)))
        self.assertEqual(len(release.files), 4)

    def test_supplied_shop_normalization_and_permanent_identity(self):
        for name, expected in (("  Bridge   Road.  ", 'Bridge_Road'),
                               ("Café & Lane", 'Caf__Lane'),
                               ("King's-Road", "King's-Road")):
            with self.subTest(name=name):
                self.assertEqual(safe_name(name), expected)
        self.assertNotEqual(keys(DATASET, 'permanent-a', 'Same Road'),
                            keys(DATASET, 'permanent-b', 'Same Road'))
