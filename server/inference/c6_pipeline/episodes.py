from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Episode:
    start_ts: float
    last_hot_ts: float
    peak_probability: float
    status: str = "PENDING"
    quiet_since: float | None = None
    reason: str = "awaiting post-fall stillness"


class EpisodeTracker:
    """낙상 판정 규칙.

    확률이 임계값을 넘으면 에피소드가 열리고, 그 뒤 '빈방 대비 조용한' 상태가
    stillness_hold_s 동안 끊기지 않고 이어지면 낙상으로 확정한다.
    stillness_wait_s 안에 그 조건을 채우지 못하면 기각한다.

    정지 조건은 샘플 개수가 아니라 **시간**으로 잰다. 추론 주기는 서버 성능에
    따라 달라지므로, 개수로 세면 같은 설정이 기계마다 다른 시간을 뜻하게 된다.

    stillness_wait_s는 stillness_hold_s보다 충분히 길어야 한다. 그렇지 않으면
    정지를 다 채우기 전에 기각 기한이 먼저 도래해 아무것도 확정되지 않는다.

    에피소드는 **판정이 끝난 뒤에만** 닫는다. 정지 요구가 길어지면 확률이 이미
    임계값 아래로 내려간 상태에서 정지를 기다리게 되는데, merge_gap만 보고 닫으면
    판정 전에 PENDING인 채로 기록돼 버린다.
    """

    def __init__(self, *, merge_gap_s: float = 3.0, stillness_wait_s: float = 60.0,
                 stillness_hold_s: float = 30.0, quiet_factor: float = 2.0):
        self.merge_gap_s = merge_gap_s
        self.stillness_wait_s = stillness_wait_s
        self.stillness_hold_s = stillness_hold_s
        self.quiet_factor = quiet_factor
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

        episode = self.current
        if episode is not None:
            if motion_energy < self.quiet_factor * empty_motion_q95:
                if episode.quiet_since is None:
                    episode.quiet_since = ts
            else:
                episode.quiet_since = None          # 다시 움직이면 정지 시간은 처음부터

            if episode.status == "PENDING":
                held = (episode.quiet_since is not None
                        and ts - episode.quiet_since >= self.stillness_hold_s)
                if held:
                    episode.status = "CONFIRMED"
                    episode.reason = f"post-fall stillness held {self.stillness_hold_s:.0f}s"
                elif ts - episode.start_ts > self.stillness_wait_s:
                    episode.status = "REJECTED"
                    episode.reason = (f"no {self.stillness_hold_s:.0f}s stillness "
                                      f"within {self.stillness_wait_s:.0f}s")

            if episode.status != "PENDING" and not hot and ts - episode.last_hot_ts > self.merge_gap_s:
                completed = episode
                self.current = None
        return self.current, completed


def motion_energy(amplitude: "object") -> float:
    import numpy as np

    log_amplitude = np.log1p(amplitude)
    return float(np.abs(np.diff(log_amplitude, axis=0)).mean())
