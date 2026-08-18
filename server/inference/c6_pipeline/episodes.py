from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Episode:
    start_ts: float
    last_hot_ts: float
    peak_probability: float
    status: str = "PENDING"
    quiet_samples: int = 0
    reason: str = "awaiting post-fall stillness"


class EpisodeTracker:
    def __init__(self, *, merge_gap_s: float = 3.0, stillness_wait_s: float = 8.0):
        self.merge_gap_s = merge_gap_s
        self.stillness_wait_s = stillness_wait_s
        self.current: Episode | None = None

    def update(
        self,
        *,
        ts: float,
        probability: float,
        threshold: float,
        motion_energy: float,
        empty_motion_q95: float,
    ) -> tuple[Episode | None, Episode | None]:
        completed = None
        hot = probability >= threshold
        if hot and self.current is None:
            self.current = Episode(ts, ts, probability)
        elif hot and self.current is not None:
            self.current.last_hot_ts = ts
            self.current.peak_probability = max(self.current.peak_probability, probability)

        if self.current is not None:
            if motion_energy < 2.0 * empty_motion_q95:
                self.current.quiet_samples += 1
            else:
                self.current.quiet_samples = 0
            if self.current.status == "PENDING" and self.current.quiet_samples >= 2:
                self.current.status = "CONFIRMED"
                self.current.reason = "post-fall stillness observed"
            elif self.current.status == "PENDING" and ts - self.current.start_ts > self.stillness_wait_s:
                self.current.status = "REJECTED"
                self.current.reason = "post-fall stillness not observed"

            if not hot and ts - self.current.last_hot_ts > self.merge_gap_s:
                completed = self.current
                self.current = None
        return self.current, completed


def motion_energy(amplitude: "object") -> float:
    import numpy as np

    log_amplitude = np.log1p(amplitude)
    return float(np.abs(np.diff(log_amplitude, axis=0)).mean())
