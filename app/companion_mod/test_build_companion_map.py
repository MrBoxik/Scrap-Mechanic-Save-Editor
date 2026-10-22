import unittest
import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_companion_map import height_grid, merge_rectangles


class CompanionExporterTests(unittest.TestCase):
    def test_merges_identical_rows_into_one_rectangle(self):
        image = Image.new("RGB", (8, 6), (20, 80, 30))
        rectangles = merge_rectangles(image, [[0] * 8 for _ in range(6)])
        self.assertEqual(len(rectangles), 1)
        self.assertEqual((rectangles[0].width, rectangles[0].height), (8, 6))

    def test_height_change_splits_geometry(self):
        image = Image.new("RGB", (4, 2), (20, 80, 30))
        rectangles = merge_rectangles(image, [[0] * 4, [1] * 4])
        self.assertEqual(len(rectangles), 2)

    def test_height_grid_accepts_editor_stride(self):
        values = list(range(12))
        index = {"heightField": {"width": 3, "height": 2, "elevation": values}}
        result = height_grid(index, 3, 2)
        self.assertEqual((len(result), len(result[0])), (2, 3))
        self.assertTrue(all(0 <= item <= 10 for row in result for item in row))


if __name__ == "__main__":
    unittest.main()
