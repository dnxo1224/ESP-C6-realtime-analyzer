"""세션 로딩 / 결측 보간 / 캘리브레이션 통계 / 정규화 / 윈도우 생성.

정규화 ID (PLAN.md 5절):
  N0  raw 진폭
  N1  세션별 z-score (오라클, 배포 불가 — 상한 참고용)
  N2  부재 기반 z-score: (amp - mu_empty) / sigma_empty      [집·노드·서브캐리어별]
  N3  로그진폭 - 부재 로그평균 (채널 비율에 해당, 배포 가능)
  N3z N3 후 부재 로그 sigma로 나눔
  N4  로그진폭 시간 차분 (캘리브레이션 불필요)
  N6  윈도우 내 per-(node,sc) 표준화 (캘리브레이션 불필요)
  조합은 실험 코드에서 순차 적용으로 구성 (예: N3 -> N6)
"""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from common import CACHE, HZ, FALL_ACTIONS, all_sessions, cache_path

EPS = 1e-6


def load_session(place, subj, act, interp=True):
    """캐시에서 세션 로드. 반환 amp: float32 (T,4,246), 결측은 보간(interp=True) 또는 NaN."""
    d = np.load(cache_path(place, subj, act))
    iq = d["iq"].astype(np.float32)
    amp = np.hypot(iq[..., 0], iq[..., 1])  # (T,4,246)
    mask = d["mask"].astype(bool)           # (T,4)
    amp[~mask] = np.nan
    if interp:
        amp = interp_missing(amp, mask)
    return dict(amp=amp, mask=mask, seq=d["seq"], rssi=d["rssi"])


def interp_missing(amp, mask):
    """노드별 프레임 단위 선형 보간 (실시간 구현 가능한 방식).

    한 노드의 결측 슬롯은 그 노드의 이웃 수신 프레임으로 선형 보간.
    시작/끝 결측은 최근접 값으로 채움 (np.interp 기본 동작).
    """
    T = amp.shape[0]
    t = np.arange(T)
    out = amp.copy()
    for n in range(amp.shape[1]):
        m = mask[:, n]
        if m.all():
            continue
        if not m.any():
            out[:, n] = 0.0
            continue
        src_t = t[m]
        # (246,) 차원 일괄 보간: np.interp는 1D라 서브캐리어 루프
        for sc in range(amp.shape[2]):
            out[~m, n, sc] = np.interp(t[~m], src_t, amp[m, n, sc])
    return out


def lamp_of(amp):
    return np.log1p(amp)


def load_amp(place, subj, act):
    """보간 완료 진폭 (T,4,246) float32 + mask. cache/amp 우선, 없으면 계산."""
    p = CACHE / "amp" / f"{place}__{subj}__{act}.npz"
    if p.exists():
        d = np.load(p)
        return d["amp"].astype(np.float32), d["mask"].astype(bool)
    s = load_session(place, subj, act, interp=True)
    return s["amp"], s["mask"]


def empty_calib_stats(place, k_min=10.0, offset_min=0.0):
    """해당 집 empty 세션의 [offset, offset+k_min) 분 구간에서 캘리브레이션 통계.

    반환: dict(mu, sd, lmu, lsd) 각 (4,246) — 진폭/로그진폭의 평균·표준편차.
    """
    a, _ = load_amp(place, "none", "empty")
    i0 = int(offset_min * 60 * HZ)
    i1 = int((offset_min + k_min) * 60 * HZ)
    seg = a[i0:i1]
    lseg = lamp_of(seg)
    return dict(
        mu=seg.mean(0), sd=seg.std(0) + EPS,
        lmu=lseg.mean(0), lsd=lseg.std(0) + EPS,
        i_end=i1,  # 이 인덱스 이후만 empty 평가에 사용해야 함
    )


def normalize(amp, method, calib=None):
    """세션 전체 (T,4,246) -> 정규화된 (T,C,246). N4는 T가 1 줄어들지 않도록 앞을 0으로 패딩."""
    if method == "N0":
        return amp
    if method == "N1":  # 세션별 z (오라클)
        return (amp - amp.mean(0)) / (amp.std(0) + EPS)
    if method == "N2":
        return (amp - calib["mu"]) / calib["sd"]
    if method == "N3":
        return lamp_of(amp) - calib["lmu"]
    if method == "N3z":
        return (lamp_of(amp) - calib["lmu"]) / calib["lsd"]
    if method == "N4":
        l = lamp_of(amp)
        d = np.diff(l, axis=0, prepend=l[:1])
        return d
    if method == "N4z":  # 차분 후 부재 차분 sigma로 스케일
        l = lamp_of(amp)
        d = np.diff(l, axis=0, prepend=l[:1])
        return d / (calib["dsd"] + EPS)
    if method == "N5":  # 노드 간 로그 비율 (C개 노드 -> C*(C-1)/2쌍)
        from itertools import combinations
        l = lamp_of(amp)
        pairs = list(combinations(range(amp.shape[1]), 2))
        return np.stack([l[:, i] - l[:, j] for i, j in pairs], axis=1)
    if method == "N6":  # 윈도우 단계에서 적용해야 의미가 있으므로 여기서는 원본 유지
        return amp
    raise ValueError(method)


def add_diff_sigma(calib, place):
    """N4z용: empty 캘리브 구간의 로그차분 sigma."""
    a, _ = load_amp(place, "none", "empty")
    l = lamp_of(a[:calib["i_end"]])
    d = np.diff(l, axis=0)
    calib["dsd"] = d.std(0) + EPS
    return calib


def window_starts(T, W, stride):
    return np.arange(0, T - W + 1, stride)


def iter_windows(x, W, stride, per_window_norm=False):
    """x: (T,C,S) -> (n,W,C,S) 뷰 스택 (copy). per_window_norm=True면 N6 적용."""
    starts = window_starts(x.shape[0], W, stride)
    out = np.empty((len(starts), W, x.shape[1], x.shape[2]), np.float32)
    for k, s0 in enumerate(starts):
        w = x[s0:s0 + W]
        if per_window_norm:
            w = (w - w.mean(0)) / (w.std(0) + EPS)
        out[k] = w
    return out, starts


def is_fall(action):
    return action in FALL_ACTIONS
