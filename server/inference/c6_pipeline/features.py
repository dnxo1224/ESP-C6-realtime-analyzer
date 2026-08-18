from __future__ import annotations

import numpy as np


N_BANDS = 24


def band_reduce(window: np.ndarray, n_bands: int = N_BANDS) -> np.ndarray:
    n, width, channels, subcarriers = window.shape
    edges = np.linspace(0, subcarriers, n_bands + 1).astype(int)
    reduced = np.empty((n, width, channels, n_bands), np.float32)
    for band in range(n_bands):
        reduced[..., band] = window[..., edges[band] : edges[band + 1]].mean(-1)
    return reduced


def featurize(window: np.ndarray, hz: float = 33.0) -> np.ndarray:
    reduced = band_reduce(window)
    n, width, channels, bands = reduced.shape
    differences = np.abs(np.diff(reduced, axis=1))
    features = [
        reduced.std(1),
        differences.mean(1),
        reduced.max(1) - reduced.min(1),
        np.abs(reduced - reduced.mean(1, keepdims=True)).mean(1),
    ]

    centered = reduced - reduced.mean(1, keepdims=True)
    power = np.abs(np.fft.rfft(centered, axis=1)) ** 2
    frequencies = np.fft.rfftfreq(width, 1 / hz)
    for low, high in ((0.0, 0.5), (0.5, 2), (2, 5), (5, 10), (10, 16.6)):
        selected = (frequencies >= low) & (frequencies < high)
        features.append(np.log1p(power[:, selected].sum(1)))

    flattened = np.concatenate([item.reshape(n, -1) for item in features], axis=1)
    motion = differences.mean((2, 3))
    global_motion = np.stack(
        [motion.mean(1), motion.max(1), np.percentile(motion, 90, axis=1), motion.std(1)],
        axis=1,
    )
    return np.concatenate([flattened, global_motion], axis=1).astype(np.float32)
