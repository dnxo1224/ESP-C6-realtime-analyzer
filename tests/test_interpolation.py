import sys
import unittest
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server" / "inference"))

from c6_pipeline import interpolate_grid


class C6InterpolationTest(unittest.TestCase):
    def test_short_missing_run_is_linear_in_amplitude_domain(self):
        amplitude = np.ones((5, 4, 246), dtype=np.float32)
        mask = np.ones((5, 4), dtype=bool)
        amplitude[0, 0] = 10.0
        amplitude[3, 0] = 40.0
        amplitude[1:3, 0] = np.nan
        mask[1:3, 0] = False

        result = interpolate_grid(amplitude, mask)

        self.assertEqual(result.quality, "OK")
        self.assertAlmostEqual(float(result.amplitude[1, 0, 0]), 20.0)
        self.assertAlmostEqual(float(result.amplitude[2, 0, 0]), 30.0)

    def test_gap_policy_warns_then_degrades(self):
        amplitude = np.ones((30, 4, 246), dtype=np.float32)
        mask = np.ones((30, 4), dtype=bool)
        mask[5:10, 0] = False
        warning = interpolate_grid(amplitude, mask)
        self.assertEqual((warning.quality, warning.max_gap), ("WARNING", 5))

        mask[5:21, 0] = False
        degraded = interpolate_grid(amplitude, mask)
        self.assertEqual((degraded.quality, degraded.max_gap), ("DEGRADED", 16))


if __name__ == "__main__":
    unittest.main()
