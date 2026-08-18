"""운영 수준 평가: 경보 에피소드 병합, FA/h, 낙상 세션 커버리지, 부재 기반 자동 임계값."""
import numpy as np

from common import HZ


def alarm_episodes(starts, p, thr, merge_gap_s=3.0):
    """윈도우 점수 -> 경보 에피소드 [(t0,t1)] (초). starts: 윈도우 시작 프레임."""
    hot = p >= thr
    if not hot.any():
        return []
    t = starts / HZ
    eps = []
    cur0 = None
    last = None
    for ti, h in zip(t, hot):
        if h:
            if cur0 is None:
                cur0 = ti
            last = ti
        elif cur0 is not None and ti - last > merge_gap_s:
            eps.append((cur0, last)); cur0 = None
    if cur0 is not None:
        eps.append((cur0, last))
    return eps


def fa_per_hour(starts, p, thr, duration_s, merge_gap_s=3.0):
    eps = alarm_episodes(starts, p, thr, merge_gap_s)
    return len(eps) / (duration_s / 3600.0)


def fall_burst_times(amp, height_pct=75, min_dist_s=2.5, smooth_s=0.5):
    """모션 에너지 피크 = 낙상 사이클의 pseudo-GT 이벤트 시각(초)."""
    from scipy.signal import find_peaks
    l = np.log1p(amp)
    d = np.abs(np.diff(l, axis=0)).mean(axis=(1, 2))
    k = max(1, int(smooth_s * HZ))
    e = np.convolve(d, np.ones(k) / k, mode="same")
    pk, _ = find_peaks(e, height=np.percentile(e, height_pct), distance=int(min_dist_s * HZ))
    return pk / HZ


def event_recall(burst_times, episodes, tol_s=5.0):
    """pseudo-GT 버스트 중 경보 에피소드(±tol)로 커버된 비율."""
    if len(burst_times) == 0:
        return np.nan
    hit = 0
    for bt in burst_times:
        for (a, b) in episodes:
            if a - tol_s <= bt <= b + tol_s:
                hit += 1
                break
    return hit / len(burst_times)


def empty_quantile_threshold(p_empty_cal, q=0.999, margin_mult=1.0):
    """부재 캘리브 데이터 점수 분포로 라벨 없이 임계값 설정.

    thr = Q_q(p_empty) + margin_mult * (Q_q - median)  — 분포 꼬리 위에 마진.
    """
    qv = np.quantile(p_empty_cal, q)
    med = np.median(p_empty_cal)
    return float(min(1.0, qv + margin_mult * (qv - med)))
