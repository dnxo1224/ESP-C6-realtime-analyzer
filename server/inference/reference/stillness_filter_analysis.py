"""P6+ '낙상 후 정지 확인' 필터 분석 — handsup 혼동 억제 실험.

가설: 낙상은 넘어진 직후 ~1.5초 정지가 있지만 handsup/walk는 계속 움직인다.
경보 에피소드 시작 후 G초 안에 '정지 구간'(모션 에너지 < θ_still, 지속 d초)이
없으면 경보를 기각한다. θ_still은 그 집 empty 캘리브 구간의 에너지 분포에서 유도(배포 가능).

출력: results/07_demo/stillness_filter.md + png — factor×duration 스윕의
      (낙상 이벤트 커버리지, 스트림별 FA/h) 표.
"""
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from common import HZ, PLACES, SUBJECTS, RESULTS
from dataset import load_amp, lamp_of
from eval_events import alarm_episodes, fall_burst_times, event_recall, empty_quantile_threshold

PRED = RESULTS / "06_final_A_union_all"
OUT = RESULTS / "07_demo"
OUT.mkdir(exist_ok=True)
K_CAL_FRAMES = int(10 * 60 * HZ)
G_AFTER = 8.0   # 경보 시작 후 정지 탐색 구간(초)


def motion_energy(amp, smooth_s=0.4):
    d = np.abs(np.diff(lamp_of(amp), axis=0)).mean(axis=(1, 2))
    k = max(1, int(smooth_s * HZ))
    e = np.convolve(d, np.ones(k) / k, mode="same")
    return np.concatenate([[e[0]], e])  # 길이 T로


def has_stillness(e, t0_s, factor_thr, dur_s, theta_base):
    """[t0, t0+G] 안에 e < factor*theta_base 가 dur_s 이상 지속되는 구간 존재?"""
    i0, i1 = int(t0_s * HZ), int((t0_s + G_AFTER) * HZ)
    seg = e[max(0, i0):min(len(e), i1)] < factor_thr * theta_base
    need = int(dur_s * HZ)
    run = 0
    for v in seg:
        run = run + 1 if v else 0
        if run >= need:
            return True
    return False


def main():
    # 집별 empty 에너지 기준 + 에너지 캐시
    theta = {}
    energies = {}
    for place in PLACES:
        amp, _ = load_amp(place, "none", "empty")
        e = motion_energy(amp)
        theta[place] = np.quantile(e[:K_CAL_FRAMES], 0.95)
        energies[(place, "none", "empty")] = e
    bursts = {}
    for place in PLACES:
        for subj in SUBJECTS:
            for act in ["liedown", "sitdown", "walk", "handsup"]:
                amp, _ = load_amp(place, subj, act)
                energies[(place, subj, act)] = motion_energy(amp)
                if act in ("liedown", "sitdown"):
                    bursts[(place, subj, act)] = fall_burst_times(amp)

    rows = []
    for factor in [1.5, 2.0, 3.0, 5.0, np.inf]:  # inf = 필터 없음
        for dur in [0.9, 1.2]:
            if np.isinf(factor) and dur != 0.9:
                continue
            ers, fa = [], {"walk": [0, 0.0], "handsup": [0, 0.0], "empty": [0, 0.0]}
            for place in PLACES:
                for subj in SUBJECTS:
                    f = PRED / f"pred_{place}_{subj}.npz"
                    if not f.exists():
                        continue
                    d = np.load(f, allow_pickle=True)
                    p, ac, sj, st = d["p"], d["ac"], d["sj"], d["st"]
                    thr = empty_quantile_threshold(d["p_cal"], q=0.999)
                    for act in ["liedown", "sitdown", "walk", "handsup", "empty"]:
                        sjv = "none" if act == "empty" else subj
                        m = (ac == act) & (sj == sjv)
                        if not m.any():
                            continue
                        eps = alarm_episodes(st[m], p[m], thr)
                        e = energies[(place, sjv, act)]
                        if not np.isinf(factor):
                            eps = [ep for ep in eps
                                   if has_stillness(e, ep[0], factor, dur, theta[place])]
                        if act in ("liedown", "sitdown"):
                            ers.append(event_recall(bursts[(place, subj, act)], eps))
                        else:
                            dur_h = ((st[m].max() - st[m].min()) / HZ + 200 / HZ) / 3600
                            fa[act][0] += len(eps); fa[act][1] += dur_h
            rows.append(dict(
                factor=("없음" if np.isinf(factor) else factor), dur_s=dur,
                event_recall=round(float(np.nanmean(ers)), 4),
                fa_walk=round(fa["walk"][0] / fa["walk"][1], 1),
                fa_handsup=round(fa["handsup"][0] / fa["handsup"][1], 1),
                fa_empty=round(fa["empty"][0] / fa["empty"][1], 2),
            ))
            print(rows[-1], flush=True)

    df = pd.DataFrame(rows)
    df.to_csv(OUT / "stillness_filter.csv", index=False)
    with open(OUT / "stillness_filter.md", "w", encoding="utf-8") as f:
        f.write("# '낙상 후 정지 확인' 필터 스윕 (P6-A 예측, empty_q 임계값, dual-unseen 12조합)\n\n")
        f.write("경보 시작 후 8초 내에 [모션에너지 < factor × empty-q95]가 dur_s 이상 지속되지 "
                "않으면 경보 기각.\n\n")
        f.write(df.to_markdown(index=False) + "\n")
    plt.rcParams["font.family"] = "Malgun Gothic"
    plt.rcParams["axes.unicode_minus"] = False
    fig, ax = plt.subplots(figsize=(8, 5))
    base = df[df.factor == "없음"].iloc[0]
    for dur, sub in df[df.factor != "없음"].groupby("dur_s"):
        ax.plot(sub.fa_handsup, sub.event_recall, "o-",
                label=f"정지 {dur}s (factor 1.5→5)")
    ax.plot(base.fa_handsup, base.event_recall, "r*", ms=15, label="필터 없음")
    ax.set_xlabel("handsup FA/h"); ax.set_ylabel("낙상 이벤트 커버리지")
    ax.set_title("정지 확인 필터: handsup 혼동 억제 vs 낙상 커버리지")
    ax.grid(alpha=0.3); ax.legend()
    fig.tight_layout(); fig.savefig(OUT / "stillness_filter.png", dpi=130)
    print("STILLNESS DONE")


if __name__ == "__main__":
    main()
