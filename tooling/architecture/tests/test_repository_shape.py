from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tooling" / "architecture"))
SPEC = importlib.util.spec_from_file_location("shape", ROOT / "tooling" / "architecture" / "validate-repository-shape.py")
SHAPE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(SHAPE)


class ShapeTests(unittest.TestCase):
    def test_current_repository_shape(self):
        self.assertEqual([], SHAPE.errors_for(ROOT))

    def test_unknown_top_level_path_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "mystery").mkdir()
            self.assertTrue(SHAPE.errors_for(root))


if __name__ == "__main__":
    unittest.main()
