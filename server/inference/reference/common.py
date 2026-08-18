"""공통 상수/유틸 — 모든 스크립트가 import."""
from pathlib import Path

PROJ = Path(r"C:\Users\researcher\R&D_Realtime_CSI\proj-csi_home-Fable5_auto")
DATA = PROJ / "csi-home-dataset-v1.0"
CACHE = PROJ / "cache"
RESULTS = PROJ / "results"
MODELS = PROJ / "models"

PLACES = ["home_B", "home_A", "home_C"]
SUBJECTS = ["P4", "P1", "P2", "P3"]
ACTIONS = ["handsup", "liedown", "sitdown", "walk"]
FALL_ACTIONS = {"liedown", "sitdown"}          # 낙상 클래스
NONFALL_ACTIONS = {"handsup", "walk", "empty"}  # 비낙상 클래스

NULL_SC = {0, 1, 2, 3, 4, 128, 252, 253, 254, 255}
KEEP = [sc for sc in range(256) if sc not in NULL_SC]  # 246개
N_SC = len(KEEP)
N_RX = 4
HZ = 33.0

def all_sessions():
    """(place, subject, action, dir_path) 51개."""
    out = []
    for place in PLACES:
        for subj in SUBJECTS:
            for act in ACTIONS:
                p = DATA / place / subj / act / "trial_01"
                if p.exists():
                    out.append((place, subj, act, p))
        p = DATA / place / "none" / "empty" / "trial_01"
        if p.exists():
            out.append((place, "none", "empty", p))
    return out

def session_key(place, subject, action):
    return f"{place}__{subject}__{action}"

def cache_path(place, subject, action):
    return CACHE / "sessions" / f"{session_key(place, subject, action)}.npz"
