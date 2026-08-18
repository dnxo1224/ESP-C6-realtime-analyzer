from __future__ import annotations

from itertools import combinations
from dataclasses import dataclass
from pathlib import Path
import hashlib

import numpy as np

from .features import featurize


EPSILON = 1e-6


@dataclass(frozen=True)
class CalibrationProfile:
    dsd: np.ndarray
    threshold: float
    median_score: float
    quantile_score: float
    window_count: int


class C6Model:
    def __init__(self, bundle: dict, sha256: str):
        self.model = bundle["model"]
        self.config = bundle["config"]
        self.sha256 = sha256
        expected = {
            "chains": ["N5", "N4z"],
            "nodes": None,
            "W": 200,
            "stride": 33,
        }
        for key, value in expected.items():
            if self.config.get(key) != value:
                raise ValueError(f"unsupported C6 model config: {key}={self.config.get(key)!r}")
        if getattr(self.model, "n_features_in_", None) != 2168:
            raise ValueError("C6 model must accept exactly 2168 features")

    @classmethod
    def load(cls, path: str | Path) -> "C6Model":
        import joblib

        model_path = Path(path)
        digest = hashlib.sha256(model_path.read_bytes()).hexdigest()
        return cls(joblib.load(model_path), digest)

    def score(self, amplitude: np.ndarray, profile: CalibrationProfile) -> float:
        features = transform_window(amplitude, profile.dsd)
        return float(self.model.predict_proba(features)[0, 1])

    def calibrate(self, amplitude: np.ndarray) -> CalibrationProfile:
        if amplitude.ndim != 3 or amplitude.shape[1:] != (4, 246) or amplitude.shape[0] < 200:
            raise ValueError("calibration requires at least 200 C6 slots with shape (4, 246)")
        log_amplitude = np.log1p(amplitude)
        dsd = np.diff(log_amplitude, axis=0).std(0) + EPSILON
        scores = []
        for start in range(0, amplitude.shape[0] - 200 + 1, 33):
            features = transform_window(amplitude[start : start + 200], dsd)
            scores.append(float(self.model.predict_proba(features)[0, 1]))
        values = np.asarray(scores, dtype=np.float64)
        quantile = float(self.config.get("empty_quantile", 0.999))
        median = float(np.median(values))
        tail = float(np.quantile(values, quantile))
        threshold = float(min(1.0, tail + (tail - median)))
        return CalibrationProfile(dsd, threshold, median, tail, len(scores))


def transform_window(amplitude: np.ndarray, dsd: np.ndarray) -> np.ndarray:
    if amplitude.shape != (200, 4, 246):
        raise ValueError("C6 model window must have shape (200, 4, 246)")
    if dsd.shape != (4, 246):
        raise ValueError("C6 calibration dsd must have shape (4, 246)")

    log_amplitude = np.log1p(amplitude)
    pairs = list(combinations(range(4), 2))
    n5 = np.stack(
        [log_amplitude[:, left] - log_amplitude[:, right] for left, right in pairs],
        axis=1,
    )
    difference = np.diff(log_amplitude, axis=0, prepend=log_amplitude[:1])
    n4z = difference / dsd
    return np.hstack(
        [
            featurize(n5[None].astype(np.float32)),
            featurize(n4z[None].astype(np.float32)),
        ]
    )
