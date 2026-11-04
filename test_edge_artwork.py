"""Run the actual Edge handler offline with fake R2/Supabase and Node's TS stripper."""
import subprocess
import unittest
from pathlib import Path


class EdgeArtworkTests(unittest.TestCase):
    def test_offline_edge_handler(self):
        result = subprocess.run(
            ['node', '--test', 'tests/edge_artwork.test.mjs', 'tests/edge_package.test.mjs'],
            cwd=Path(__file__).resolve().parent, capture_output=True, text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
