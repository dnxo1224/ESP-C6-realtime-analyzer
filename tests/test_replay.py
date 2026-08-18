import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("replay", ROOT / "scripts" / "replay_c6_sample.py")
REPLAY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REPLAY)


class ReplayContractTest(unittest.TestCase):
    def test_handoff_csv_round_trips_through_c6_wire_contract(self):
        slots = REPLAY.load_frames(ROOT / "samples" / "synced_head.csv", slots=2, start_seq=700)
        self.assertEqual([4, 4], [len(slot) for slot in slots])
        reports = [REPLAY.decode_frame(frame) for slot in slots for frame in slot]
        self.assertEqual({512}, {report.csi_len for report in reports})
        self.assertEqual({1, 2, 3, 4}, {report.rx_id for report in reports})
        self.assertEqual({700, 701}, {report.seq for report in reports})


if __name__ == "__main__":
    unittest.main()
