import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server" / "inference"))

from c6_pipeline import EpisodeTracker

QUIET, MOVING = 0.1, 1.0      # empty_motion_q95=0.1, quiet_factor=2.0 기준
Q95 = 0.1


def tracker(hold=30.0, wait=60.0, gap=3.0):
    return EpisodeTracker(stillness_hold_s=hold, stillness_wait_s=wait, merge_gap_s=gap)


class EpisodePolicyTest(unittest.TestCase):
    def test_stillness_must_hold_for_the_configured_duration(self):
        t = tracker(hold=30.0)
        current, _ = t.update(ts=0, probability=.9, threshold=.8, motion_energy=MOVING, empty_motion_q95=Q95)
        self.assertEqual(current.status, "PENDING")
        # 정지가 시작돼도 30초를 채우기 전에는 확정되지 않는다
        current, _ = t.update(ts=1, probability=.7, threshold=.8, motion_energy=QUIET, empty_motion_q95=Q95)
        self.assertEqual(current.status, "PENDING")
        current, _ = t.update(ts=29, probability=.7, threshold=.8, motion_energy=QUIET, empty_motion_q95=Q95)
        self.assertEqual(current.status, "PENDING")
        # 30초를 채우는 순간 확정되고, 확률도 이미 내려간 지 오래라 그 자리에서 닫힌다
        current, completed = t.update(ts=31, probability=.7, threshold=.8, motion_energy=QUIET, empty_motion_q95=Q95)
        self.assertIsNone(current)
        self.assertEqual(completed.status, "CONFIRMED")

    def test_movement_restarts_the_stillness_clock(self):
        t = tracker(hold=30.0)
        t.update(ts=0, probability=.9, threshold=.8, motion_energy=MOVING, empty_motion_q95=Q95)
        t.update(ts=1, probability=.7, threshold=.8, motion_energy=QUIET, empty_motion_q95=Q95)
        # 20초 조용하다가 다시 움직이면 시계가 0으로 돌아간다
        t.update(ts=21, probability=.7, threshold=.8, motion_energy=MOVING, empty_motion_q95=Q95)
        current, _ = t.update(ts=45, probability=.7, threshold=.8, motion_energy=QUIET, empty_motion_q95=Q95)
        self.assertEqual(current.status, "PENDING")

    def test_rejects_when_stillness_never_holds_within_the_deadline(self):
        t = tracker(hold=30.0, wait=60.0)
        t.update(ts=0, probability=.9, threshold=.8, motion_energy=MOVING, empty_motion_q95=Q95)
        current, completed = t.update(ts=61, probability=.1, threshold=.8, motion_energy=MOVING, empty_motion_q95=Q95)
        self.assertIsNone(current)
        self.assertEqual(completed.status, "REJECTED")

    def test_episode_stays_open_until_a_verdict_is_reached(self):
        # 확률은 진작 내려갔지만 정지를 기다리는 중이면 PENDING인 채로 닫히면 안 된다
        t = tracker(hold=30.0, wait=60.0, gap=3.0)
        t.update(ts=0, probability=.9, threshold=.8, motion_energy=MOVING, empty_motion_q95=Q95)
        current, completed = t.update(ts=10, probability=.1, threshold=.8, motion_energy=QUIET, empty_motion_q95=Q95)
        self.assertIsNone(completed)
        self.assertEqual(current.status, "PENDING")
        # 30초 정지를 채우면 확정되고, 그때 비로소 닫힌다
        _, completed = t.update(ts=41, probability=.1, threshold=.8, motion_energy=QUIET, empty_motion_q95=Q95)
        self.assertEqual(completed.status, "CONFIRMED")

    def test_hot_windows_merge_into_one_episode(self):
        t = tracker(hold=1.0, wait=10.0, gap=3.0)
        t.update(ts=0, probability=.9, threshold=.8, motion_energy=MOVING, empty_motion_q95=Q95)
        t.update(ts=2, probability=.9, threshold=.8, motion_energy=MOVING, empty_motion_q95=Q95)
        current, _ = t.update(ts=4, probability=.85, threshold=.8, motion_energy=QUIET, empty_motion_q95=Q95)
        self.assertEqual(current.start_ts, 0)          # 새 에피소드가 열리지 않았다


if __name__ == "__main__":
    unittest.main()
