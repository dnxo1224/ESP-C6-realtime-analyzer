"""윈도우 -> 고전 ML 특징 벡터 (P2 스크리닝용).

윈도우 (n, W, C, S) -> 서브캐리어 대역 축약 (n, W, C, B) -> 시간/스펙트럼 통계.
"""
import numpy as np

N_BANDS = 24


def band_reduce(win, n_bands=N_BANDS):
    """(n,W,C,S) -> (n,W,C,B): 인접 서브캐리어 평균."""
    n, W, C, S = win.shape
    edges = np.linspace(0, S, n_bands + 1).astype(int)
    out = np.empty((n, W, C, n_bands), np.float32)
    for b in range(n_bands):
        out[..., b] = win[..., edges[b]:edges[b + 1]].mean(-1)
    return out


def featurize(win, hz=33.0):
    """(n,W,C,S) -> (n,F). 시간통계 4 + 스펙트럼 5 per (C,B) + 전역 모션 4."""
    xb = band_reduce(win)                       # (n,W,C,B)
    n, W, C, B = xb.shape
    feats = []
    # 시간 통계
    feats.append(xb.std(1))                     # (n,C,B)
    d = np.abs(np.diff(xb, axis=1))
    feats.append(d.mean(1))
    feats.append(xb.max(1) - xb.min(1))
    feats.append(np.abs(xb - xb.mean(1, keepdims=True)).mean(1))
    # 스펙트럼 (대역 파워)
    xd = xb - xb.mean(1, keepdims=True)
    P = np.abs(np.fft.rfft(xd, axis=1)) ** 2    # (n,F,C,B)
    f = np.fft.rfftfreq(W, 1 / hz)
    for lo, hi in [(0.0, 0.5), (0.5, 2), (2, 5), (5, 10), (10, 16.6)]:
        m = (f >= lo) & (f < hi)
        feats.append(np.log1p(P[:, m].sum(1)))  # (n,C,B)
    F = np.concatenate([x.reshape(n, -1) for x in feats], 1)  # (n, 9*C*B)
    # 전역 모션 에너지 프로파일 통계
    e = d.mean((2, 3))                          # (n, W-1)
    glob = np.stack([e.mean(1), e.max(1), np.percentile(e, 90, 1), e.std(1)], 1)
    return np.concatenate([F, glob], 1).astype(np.float32)
