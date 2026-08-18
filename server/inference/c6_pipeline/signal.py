from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class InterpolationResult:
    amplitude: np.ndarray
    quality: str
    max_gap: int


def interpolate_grid(
    amplitude: np.ndarray,
    mask: np.ndarray,
    *,
    warning_gap: int = 3,
    maximum_gap: int = 15,
) -> InterpolationResult:
    if amplitude.ndim != 3 or mask.shape != amplitude.shape[:2]:
        raise ValueError("amplitude and mask shapes do not describe the same slot grid")

    output = amplitude.astype(np.float32, copy=True)
    longest = 0
    degraded = False
    slots = np.arange(amplitude.shape[0])
    for node in range(amplitude.shape[1]):
        present = mask[:, node].astype(bool)
        if not present.any():
            degraded = True
            longest = max(longest, amplitude.shape[0])
            continue
        missing = ~present
        cursor = 0
        while cursor < len(present):
            if present[cursor]:
                cursor += 1
                continue
            end = cursor
            while end < len(present) and not present[end]:
                end += 1
            gap = end - cursor
            longest = max(longest, gap)
            if gap > maximum_gap:
                degraded = True
            else:
                known_slots = slots[present]
                for subcarrier in range(amplitude.shape[2]):
                    output[cursor:end, node, subcarrier] = np.interp(
                        slots[cursor:end], known_slots, amplitude[present, node, subcarrier]
                    )
            cursor = end

    quality = "DEGRADED" if degraded else ("WARNING" if longest > warning_gap else "OK")
    return InterpolationResult(output, quality, longest)
