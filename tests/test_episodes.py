import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server" / "inference"))

from c6_pipeline import EpisodeTracker


class EpisodePolicyTest(unittest.TestCase):
    def test_hot_windows_merge_and_post_fall_stillness_confirms(self):
        tracker = EpisodeTracker()
        current, _ = tracker.update(ts=10, probability=.9, threshold=.8, motion_energy=.1, empty_motion_q95=.1)
        self.assertEqual(current.status, "PENDING")
        current, _ = tracker.update(ts=11.2, probability=.7, threshold=.8, motion_energy=.1, empty_motion_q95=.1)
        self.assertEqual(current.status, "CONFIRMED")
        _, completed = tracker.update(ts=14.5, probability=.2, threshold=.8, motion_energy=.1, empty_motion_q95=.1)
        self.assertEqual(completed.status, "CONFIRMED")

    def test_no_stillness_rejects_after_eight_seconds(self):
        tracker = EpisodeTracker()
        tracker.update(ts=0, probability=.9, threshold=.8, motion_energy=1, empty_motion_q95=.1)
        current, completed = tracker.update(ts=9, probability=.1, threshold=.8, motion_energy=1, empty_motion_q95=.1)
        self.assertEqual(current, None)
        self.assertEqual(completed.status, "REJECTED")


if __name__ == "__main__":
    unittest.main()
