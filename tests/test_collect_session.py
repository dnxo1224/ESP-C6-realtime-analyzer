"""큐 격자가 COLLECTOR_HANDOFF.md의 표와 정확히 일치하는지 검증한다.

시각이 한 칸이라도 어긋나면 라벨이 통째로 밀리므로, 문서의 세션 1 표를
그대로 옮겨 놓고 한 줄씩 대조한다.
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from collect_session import build_cues, cycle_order, SESSION_S


def mmss(sec):
    return f"{int(sec) // 60}:{int(sec) % 60:02d}"


# 문서 1절 "정확한 절 단위 표 (세션 1)"
DOC_SESSION1 = [
    ("0:00", "부재"), ("2:00", "입장하세요"), ("2:10", "가만히 서 계세요"),
    ("2:30", "낙상"), ("2:33", "조금씩 움직이세요"), ("3:00", "일어나세요"),
    ("3:10", "가만히 서서 기지개를 펴세요"), ("3:30", "낙상"), ("4:00", "일어나세요"),
    ("4:10", "가만히 서서 물건을 주우세요"), ("4:30", "낙상"), ("5:00", "일어나세요"),
    ("5:10", "매트에 누워 가만히 계세요"), ("6:00", "일어나세요"),
    ("6:10", "걸어다니세요"), ("6:30", "낙상"), ("7:00", "일어나세요"),
    ("7:10", "걸어다니며 물건을 주우세요"), ("7:30", "낙상"), ("8:00", "일어나세요"),
    ("8:10", "의자로 가서 앉아 기다리세요"), ("8:30", "낙상"), ("9:00", "일어나세요"),
    ("9:10", "의자에 앉아 가만히 계세요"), ("10:00", "일어나세요"),
    ("10:10", "수집 종료"),
]


class CueGridTest(unittest.TestCase):
    def test_session_one_matches_the_document_table(self):
        cues, total, _ = build_cues("event", 1, 0)
        self.assertEqual(total, 610.0)
        self.assertEqual(len(cues), len(DOC_SESSION1))
        for (t, phase, label, text, cycle, hold), (want_t, want_text) in zip(cues, DOC_SESSION1):
            self.assertEqual(mmss(t), want_t, f"시각 불일치: {text}")
            if want_text not in ("부재",):
                self.assertEqual(text, want_text, f"{want_t} 문구 불일치")

    def test_wiggle_cue_only_in_cycle_one(self):
        for s in (1, 2, 3, 4, 5, 6):
            cues, _, _ = build_cues("event", s, 0)
            wig = [c for c in cues if c[1] == "wiggle"]
            self.assertEqual(len(wig), 1, f"세션 {s}: 꿈틀 큐는 정확히 1개여야 한다")
            self.assertEqual(wig[0][4], 1, f"세션 {s}: 꿈틀 큐는 항상 c1")

    def test_rotation_matches_document_matrix(self):
        # 문서 2절 표의 c1 라벨
        want_c1 = ["stand_still", "stretch", "pick_stand", "walk", "walk_pick", "chair_sit"]
        for s in range(1, 7):
            self.assertEqual(cycle_order(s)[0][0], want_c1[s - 1], f"세션 {s} c1")
        # 휴식은 c4·c8, 홀수 세션은 lie→sit, 짝수는 sit→lie
        for s in range(1, 8):
            order = [lb for lb, _ in cycle_order(s)]
            self.assertEqual(len(order), 8)
            rests = [order[3], order[7]]
            self.assertEqual(rests, ["lie_rest", "sit_rest"] if s % 2 else ["sit_rest", "lie_rest"])
        # 세션 7은 세션 1과 같은 순서
        self.assertEqual([lb for lb, _ in cycle_order(7)], [lb for lb, _ in cycle_order(1)])

    def test_every_session_has_six_falls_and_two_rests(self):
        for s in range(1, 7):
            cues, _, labels = build_cues("event", s, 0)
            self.assertEqual(sum(1 for c in cues if c[1] == "fall"), 6, f"세션 {s} 낙상 수")
            self.assertEqual(sum(1 for c in cues if c[1] == "rest"), 2, f"세션 {s} 휴식 수")
            self.assertEqual(sum(1 for c in cues if c[1] == "rise"), 8, f"세션 {s} 기상 수")
            self.assertEqual(len(set(labels)), 8, f"세션 {s}: 8개 라벨이 모두 달라야 한다")

    def test_life_mode_has_no_falls(self):
        cues, total, labels = build_cues("life", 1, 0)
        self.assertEqual(total, 610.0)
        self.assertEqual([c for c in cues if c[1] == "fall"], [])
        self.assertEqual(len(labels), 8)
        self.assertEqual(mmss(cues[2][0]), "2:10")     # 첫 블록

    def test_empty_mode_is_absence_only(self):
        cues, total, labels = build_cues("empty", 1, 15)
        self.assertEqual(total, 900.0)
        self.assertEqual(labels, [])
        self.assertEqual([c[1] for c in cues], ["start", "end"])

    def test_cues_are_sorted_and_within_session(self):
        for mode, s in (("event", 3), ("life", 1)):
            cues, total, _ = build_cues(mode, s, 0)
            times = [c[0] for c in cues]
            self.assertEqual(times, sorted(times))
            self.assertLessEqual(max(times), total)


if __name__ == "__main__":
    unittest.main()
