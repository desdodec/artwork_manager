import json
import tempfile
import unittest
from pathlib import Path

from production_report import ProductionReportError, read_production_report, exclusion_preview


def item(key):
    return {
        "street": key,
        "status": "warning",
        "location": {"street_key": key},
        "png": "print.png",
        "svg": "print.svg",
        "mockup_front": "front.png",
        "mockup_rear": "rear.png",
    }


class ProductionReportTests(unittest.TestCase):
    def load(self, data):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / "print_report.json"
            p.write_text(json.dumps(data), encoding="utf-8")
            return read_production_report(p)

    def test_complete_release_excludes_previously_listed(self):
        r = self.load({"processed": 3, "written": 2,
                       "skipped": [{"item": "Bridge", "status": "not_for_production"}],
                       "streets": [item("road-a"), item("road-b")]})
        preview = exclusion_preview(r, {"road-a", "road-b", "bridge"}, complete=True)
        self.assertEqual(preview["to_unlist"], ["bridge"])
        self.assertEqual(preview["explicit_generator_exclusions"], 1)

    def test_first_release_zero_unlist_is_expected(self):
        r = self.load({"processed": 2, "written": 1,
                       "skipped": [{"item": "Bridge", "status": "not_for_production"}],
                       "streets": [item("road-a")]})
        self.assertEqual(exclusion_preview(r, set(), complete=True)["to_unlist"], [])

    def test_partial_release_does_not_unlist(self):
        r = self.load({"processed": 1, "written": 1,
                       "skipped": [], "streets": [item("road-a")]})
        self.assertEqual(exclusion_preview(r, {"road-a", "bridge"}, complete=False)["to_unlist"], [])

    def test_duplicate_key_rejected(self):
        with self.assertRaises(ProductionReportError):
            self.load({"processed": 2, "written": 2, "skipped": [],
                       "streets": [item("duplicate"), item("duplicate")]})

    def test_bad_count_rejected(self):
        with self.assertRaises(ProductionReportError):
            self.load({"processed": 311, "written": 290,
                       "skipped": [], "streets": []})


if __name__ == "__main__":
    unittest.main()
