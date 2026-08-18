from __future__ import annotations

import math
import struct
from dataclasses import dataclass


MAGIC = b"\xa5\x5a"
VERSION = 1
CSI_LEN = 512
NULL_SUBCARRIERS = frozenset((0, 1, 2, 3, 4, 128, 252, 253, 254, 255))
KEEP_SUBCARRIERS = tuple(i for i in range(256) if i not in NULL_SUBCARRIERS)
_KEEP_INDEX = list(KEEP_SUBCARRIERS)  # numpy 인덱싱용 — 튜플은 다차원 색인으로 해석된다
META = struct.Struct("<6s6sIIbBBBBBBBBBBbBBBBHBBfH")


class ContractError(ValueError):
    pass


def amplitude_from_storage(raw_csi: bytes, compensate_gain: float):
    """Apply the handoff's per-I/Q truncation before converting to amplitude.

    numpy는 함수 안에서 import한다 — 이 모듈은 numpy가 없는 ingest 이미지에도 복사되며,
    ingest는 프레임 파싱만 하고 이 함수를 호출하지 않는다.
    np.trunc는 파이썬 int()와 같은 0 방향 절삭이라 핸드오프 계약과 값이 일치한다
    (tests/test_contract.py와 test_model_contract.py의 고정 해시가 이를 검증한다).
    """
    if len(raw_csi) != CSI_LEN or not math.isfinite(compensate_gain) or compensate_gain <= 0:
        raise ContractError("stored C6 CSI must be 512 bytes with a positive finite gain")
    import numpy as np

    raw = np.frombuffer(raw_csi, dtype=np.int8).astype(np.float64)
    iq = np.trunc(compensate_gain * raw).reshape(CSI_LEN // 2, 2)[_KEEP_INDEX]
    return np.hypot(iq[:, 0], iq[:, 1])


@dataclass(frozen=True)
class C6Report:
    recv_mac: bytes
    src_mac: bytes
    seq: int
    local_timestamp_us: int
    rssi: int
    compensate_gain: float
    csi_len: int
    raw_csi: tuple[int, ...]
    raw_meta: bytes

    @property
    def rx_id(self) -> int:
        return self.recv_mac[-1]

    @property
    def compensated_iq(self) -> tuple[int, ...]:
        return tuple(int(self.compensate_gain * value) for value in self.raw_csi)

    @property
    def amplitude(self) -> tuple[float, ...]:
        return amplitude_from_storage(struct.pack(f"<{CSI_LEN}b", *self.raw_csi), self.compensate_gain)


def decode_frame(frame: bytes) -> C6Report:
    if len(frame) < 6 or frame[:2] != MAGIC or frame[2] != VERSION:
        raise ContractError("invalid C6 frame header")
    payload_len = struct.unpack_from("<H", frame, 3)[0]
    expected_len = 5 + payload_len + 1
    if len(frame) != expected_len:
        raise ContractError("C6 frame length does not match header")

    payload = frame[5:-1]
    checksum = 0
    for value in payload:
        checksum ^= value
    if checksum != frame[-1]:
        raise ContractError("C6 frame checksum mismatch")
    if len(payload) < META.size:
        raise ContractError("C6 frame metadata is truncated")

    fields = META.unpack(payload[: META.size])
    recv_mac, src_mac, seq, local_timestamp_us = fields[:4]
    rssi = fields[4]
    compensate_gain = fields[23]
    csi_len = fields[24]
    raw_csi_bytes = payload[META.size :]
    if csi_len != CSI_LEN or len(raw_csi_bytes) != CSI_LEN:
        raise ContractError(f"C6 CSI length must be {CSI_LEN} bytes")
    rx_id = recv_mac[-1]
    if rx_id not in (1, 2, 3, 4):
        raise ContractError("C6 RX id must be in 1..4")
    if not math.isfinite(compensate_gain) or compensate_gain <= 0:
        raise ContractError("C6 compensate_gain must be finite and positive")

    raw_csi = struct.unpack(f"<{CSI_LEN}b", raw_csi_bytes)
    return C6Report(
        recv_mac=recv_mac,
        src_mac=src_mac,
        seq=seq,
        local_timestamp_us=local_timestamp_us,
        rssi=rssi,
        compensate_gain=compensate_gain,
        csi_len=csi_len,
        raw_csi=raw_csi,
        raw_meta=payload[: META.size],
    )


class FrameDecoder:
    def __init__(self) -> None:
        self._buffer = bytearray()
        self.rejected_frames = 0

    def feed(self, chunk: bytes) -> list[C6Report]:
        self._buffer.extend(chunk)
        reports: list[C6Report] = []
        while True:
            start = self._buffer.find(MAGIC)
            if start < 0:
                self._buffer[:] = self._buffer[-1:] if self._buffer.endswith(MAGIC[:1]) else b""
                break
            if start:
                del self._buffer[:start]
            if len(self._buffer) < 5:
                break
            if self._buffer[2] != VERSION:
                del self._buffer[:2]
                self.rejected_frames += 1
                continue
            payload_len = struct.unpack_from("<H", self._buffer, 3)[0]
            frame_len = 5 + payload_len + 1
            if payload_len != META.size + CSI_LEN:
                del self._buffer[:2]
                self.rejected_frames += 1
                continue
            if len(self._buffer) < frame_len:
                break
            frame = bytes(self._buffer[:frame_len])
            del self._buffer[:frame_len]
            try:
                reports.append(decode_frame(frame))
            except ContractError:
                self.rejected_frames += 1
        return reports
