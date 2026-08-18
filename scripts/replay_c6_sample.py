"""Replay the handoff C6 CSV through the real 512-byte gateway wire contract."""
from __future__ import annotations

import argparse
import ast
import csv
import socket
import struct
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server" / "inference"))
from c6_pipeline.contract import META, decode_frame  # noqa: E402


def quantize(values: list[int]) -> tuple[float, bytes]:
    if len(values) != 512:
        raise ValueError(f"C6 CSV row must contain 512 I/Q integers, got {len(values)}")
    gain = max(1.0, max(abs(value) for value in values) / 127.0)
    raw = [max(-128, min(127, round(value / gain))) for value in values]
    return gain, struct.pack("<512b", *raw)


def make_frame(*, seq: int, rx_id: int, rssi: int, timestamp_us: int, values: list[int]) -> bytes:
    gain, raw = quantize(values)
    meta = META.pack(
        bytes((0x1E, 0, 0, 0, 0, rx_id)), bytes((0x1A, 0, 0, 0, 0, 1)),
        seq & 0xFFFFFFFF, timestamp_us & 0xFFFFFFFF, rssi,
        *([0] * 18), gain, 512,
    )
    payload = meta + raw
    checksum = 0
    for value in payload:
        checksum ^= value
    frame = b"\xA5\x5A\x01" + struct.pack("<H", len(payload)) + payload + bytes((checksum,))
    decode_frame(frame)
    return frame


def load_frames(csv_path: Path, *, slots: int, start_seq: int) -> list[list[bytes]]:
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise ValueError("sample CSV is empty")
    output = []
    for index in range(slots):
        row, seq = rows[index % len(rows)], start_seq + index
        reports = []
        for rx in range(1, 5):
            if int(row[f"rx{rx}"]) != 1:
                continue
            reports.append(make_frame(
                seq=seq, rx_id=rx, rssi=int(row[f"rx{rx}_rssi"]),
                timestamp_us=int(row[f"rx{rx}_ts_us"]) + index * 30303,
                values=ast.literal_eval(row[f"rx{rx}_data"]),
            ))
        output.append(reports)
    return output


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", type=Path, default=ROOT / "samples" / "synced_head.csv")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9600)
    parser.add_argument("--slots", type=int, default=240)
    parser.add_argument("--start-seq", type=int, default=100000)
    parser.add_argument("--delay", type=float, default=0.0, help="seconds per 33 Hz slot; 0 is fast replay")
    parser.add_argument("--token", default="")
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    frames = load_frames(args.csv, slots=args.slots, start_seq=args.start_seq)
    if args.validate_only:
        print(f"valid C6 sample: slots={len(frames)} reports={sum(map(len, frames))} csi_len=512")
        return
    with socket.create_connection((args.host, args.port), timeout=10) as sock:
        if args.token:
            sock.sendall(("CSI-TOKEN " + args.token + "\n").encode())
        for reports in frames:
            sock.sendall(b"".join(reports))
            if args.delay:
                time.sleep(args.delay)
    print(f"replayed C6 sample: slots={len(frames)} reports={sum(map(len, frames))}")


if __name__ == "__main__":
    main()
