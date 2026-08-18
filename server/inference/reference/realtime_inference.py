"""새 집 배포용 실시간 추론 파이프라인 (+ 시뮬레이션 데모).

배포 절차:
  1) 새 집에 TX + RX(규약 배치: TX 6시, rx1~4 반시계, rx1·rx4가 TX 양옆) 설치
  2) 부재(빈 집) 데이터 K분 수집 → --calibrate 로 캘리브레이션 파일 생성
     (정규화 통계 + 부재 점수 분위수 기반 자동 임계값)
  3) --simulate <synced.csv> 로 스트림 시뮬레이션 (실제 배포 시 시리얼 수신부로 교체)

사용 예:
  python realtime_inference.py --calibrate path/to/new_home_empty_synced.csv
  python realtime_inference.py --simulate path/to/live_synced.csv

구현 노트:
  - 결측 프레임은 실시간 선형 보간(다음 프레임 도착 시 사이 구간 채움) 후 윈도우 완성.
  - stride(기본 33프레임=1초)마다 특징 추출 + RF 추론. CPU로 윈도우당 수 ms.
  - 경보는 merge_gap(3s) 내 연속 양성을 하나의 에피소드로 병합.
"""
import argparse
import csv
import sys
import time
from pathlib import Path

import joblib
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from common import KEEP, MODELS, HZ
from dataset import lamp_of, EPS
from features import featurize

BUNDLE_PATH = MODELS / "final_bundle.joblib"
CALIB_PATH = MODELS / "new_home_calib.joblib"


# ---------------------------------------------------------------- 스트림 파서
def stream_synced_csv(path):
    """synced.csv를 한 행(=한 seq 슬롯)씩 yield: (seq, amp(4,246) 또는 NaN행, mask(4,))"""
    keep = np.asarray(KEEP)
    with open(path, newline="", encoding="utf-8") as f:
        rd = csv.reader(f)
        next(rd)
        for row in rd:
            amp = np.full((4, len(keep)), np.nan, np.float32)
            mask = np.zeros(4, bool)
            for n in range(4):
                base = 1 + n * 4
                if row[base] == "1":
                    v = np.fromstring(row[base + 3][1:-1], dtype=np.float64, sep=",")
                    iq = v.astype(np.float32).reshape(256, 2)[keep]
                    amp[n] = np.hypot(iq[:, 0], iq[:, 1])
                    mask[n] = True
            yield int(row[0]), amp, mask


class RollingInterp:
    """노드별 실시간 선형 보간 버퍼. 프레임 도착 시 이전 결측 구간을 채워서 반환."""

    def __init__(self):
        self.last = [None] * 4      # (t_idx, amp246)
        self.pending = [[] for _ in range(4)]  # 결측 t_idx 목록

    def push(self, t, amp, mask):
        """t 시점 프레임 → 확정된 (t_idx, node, amp246) 리스트 반환."""
        out = []
        for n in range(4):
            if mask[n]:
                if self.pending[n] and self.last[n] is not None:
                    t0, a0 = self.last[n]
                    for tm in self.pending[n]:
                        w = (tm - t0) / (t - t0)
                        out.append((tm, n, a0 * (1 - w) + amp[n] * w))
                elif self.pending[n]:  # 시작부 결측: 첫 수신값 복제
                    for tm in self.pending[n]:
                        out.append((tm, n, amp[n].copy()))
                self.pending[n] = []
                self.last[n] = (t, amp[n].copy())
                out.append((t, n, amp[n]))
            else:
                self.pending[n].append(t)
        return out


# ---------------------------------------------------------------- 정규화/특징
def make_calib(amp_TxNxS, k_cal_min):
    seg = amp_TxNxS[: int(k_cal_min * 60 * HZ)]
    l = lamp_of(seg)
    d = np.diff(l, axis=0)
    return dict(lmu=l.mean(0), lsd=l.std(0) + EPS, dsd=d.std(0) + EPS)


def transform_window(win, chains, calib):
    """win: (W,C,S) 진폭 → 체인별 변환 후 featurize 결과 concat (1,F)."""
    from itertools import combinations
    feats = []
    l = lamp_of(win)
    for chain in chains:
        if chain == "N5":
            pairs = list(combinations(range(win.shape[1]), 2))
            x = np.stack([l[:, i] - l[:, j] for i, j in pairs], axis=1)
        elif chain == "N4z":
            d = np.diff(l, axis=0, prepend=l[:1])
            x = d / calib["dsd"]
        elif chain == "N4":
            x = np.diff(l, axis=0, prepend=l[:1])
        elif chain == "N3z":
            x = (l - calib["lmu"]) / calib["lsd"]
        else:
            raise ValueError(chain)
        feats.append(featurize(x[None].astype(np.float32)))
    return np.hstack(feats)


# ---------------------------------------------------------------- 캘리브레이션
def calibrate(empty_csv, bundle):
    cfg = bundle["config"]
    print(f"[calibrate] {empty_csv} 파싱 중...", flush=True)
    rows = []
    ri = RollingInterp()
    frames = {}
    t_max = -1
    for i, (seq, amp, mask) in enumerate(stream_synced_csv(empty_csv)):
        for (t, n, a) in ri.push(i, amp, mask):
            frames.setdefault(t, {})[n] = a
            t_max = max(t_max, t)
    T = t_max + 1
    S = len(KEEP)
    A = np.zeros((T, 4, S), np.float32)
    for t in range(T):
        for n in range(4):
            if t in frames and n in frames[t]:
                A[t, n] = frames[t][n]
            elif t > 0:
                A[t, n] = A[t - 1, n]  # 끝부분 잔여 결측
    nodes = cfg["nodes"]
    if nodes is not None:
        A = A[:, nodes]
    # 배포 충실도: 캘리브에 쓸 수 있는 데이터는 앞 k_cal분뿐 (긴 파일이 와도 절단)
    A = A[: int(cfg["k_cal_min"] * 60 * HZ)]
    calib = make_calib(A, cfg["k_cal_min"])
    # 부재 점수 분포로 임계값
    W, stride = cfg["W"], cfg["stride"]
    ps = []
    model = bundle["model"]
    for s0 in range(0, A.shape[0] - W + 1, stride):
        F = transform_window(A[s0:s0 + W], cfg["chains"], calib)
        ps.append(model.predict_proba(F)[0, 1])
    ps = np.array(ps)
    q = cfg["empty_quantile"]
    qv = np.quantile(ps, q)
    thr = float(min(1.0, qv + (qv - np.median(ps))))
    calib_out = dict(calib=calib, threshold=thr,
                     empty_score_stats=dict(median=float(np.median(ps)),
                                            q=q, qv=float(qv), n=len(ps)))
    joblib.dump(calib_out, CALIB_PATH)
    print(f"[calibrate] 완료: 윈도우 {len(ps)}개, 부재점수 med={np.median(ps):.3f} "
          f"q{q}={qv:.3f} → threshold={thr:.3f}\n저장: {CALIB_PATH}", flush=True)


# ---------------------------------------------------------------- 시뮬레이션
def simulate(live_csv, bundle, calib_out, merge_gap_s=3.0):
    cfg = bundle["config"]
    model = bundle["model"]
    calib, thr = calib_out["calib"], calib_out["threshold"]
    W, stride = cfg["W"], cfg["stride"]
    nodes = cfg["nodes"]
    S = len(KEEP)
    buf = np.zeros((0, 4, S), np.float32)
    ri = RollingInterp()
    grid = {}
    n_win, alarms = 0, []
    cur_ep = None
    t_wall0 = time.time()
    lat = []
    filled_to = 0
    A = []
    for i, (seq, amp, mask) in enumerate(stream_synced_csv(live_csv)):
        for (t, n, a) in ri.push(i, amp, mask):
            grid.setdefault(t, {})[n] = a
        # 모든 노드가 확정된 프레임까지 배열로 편입
        while filled_to in grid and len(grid[filled_to]) == 4:
            fr = grid.pop(filled_to)
            A.append(np.stack([fr[n] for n in range(4)]))
            filled_to += 1
        # 윈도우 완성 시 추론 (k번째 윈도우 = [k*stride, k*stride+W))
        while len(A) >= W + n_win * stride:
            t0 = time.time()
            s0 = n_win * stride
            win = np.stack(A[s0:s0 + W])
            x = win[:, nodes] if nodes is not None else win
            F = transform_window(x, cfg["chains"], calib)
            p = model.predict_proba(F)[0, 1]
            lat.append(time.time() - t0)
            t_end = (s0 + W) / HZ
            if p >= thr:
                if cur_ep is None:
                    cur_ep = [t_end, t_end, p]
                    print(f"  ⚠ ALARM 시작 t={t_end:.1f}s p={p:.3f}", flush=True)
                else:
                    cur_ep[1], cur_ep[2] = t_end, max(cur_ep[2], p)
            elif cur_ep and t_end - cur_ep[1] > merge_gap_s:
                alarms.append(tuple(cur_ep)); cur_ep = None
            n_win += 1
    if cur_ep:
        alarms.append(tuple(cur_ep))
    dur = len(A) / HZ
    print(f"\n[simulate] {live_csv}\n  스트림 {dur:.0f}s, 윈도우 {n_win}개, "
          f"추론 지연 median {np.median(lat)*1000:.1f}ms\n  경보 에피소드 {len(alarms)}건 "
          f"({len(alarms)/(dur/3600):.1f}건/h): "
          + ", ".join(f"[{a:.0f}~{b:.0f}s p={p:.2f}]" for a, b, p in alarms), flush=True)
    return alarms


def main():
    global CALIB_PATH
    ap_ = argparse.ArgumentParser()
    ap_.add_argument("--calibrate", help="새 집 empty synced.csv로 캘리브레이션")
    ap_.add_argument("--simulate", help="synced.csv 스트림 시뮬레이션")
    ap_.add_argument("--bundle", default=str(BUNDLE_PATH))
    ap_.add_argument("--calib-file", default=str(CALIB_PATH))
    args = ap_.parse_args()
    CALIB_PATH = Path(args.calib_file)
    bundle = joblib.load(args.bundle)
    if args.calibrate:
        calibrate(args.calibrate, bundle)
    if args.simulate:
        calib_out = joblib.load(CALIB_PATH)
        simulate(args.simulate, bundle, calib_out)


if __name__ == "__main__":
    main()
