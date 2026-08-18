import struct
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server" / "inference"))

from c6_pipeline import FrameDecoder, decode_frame


META = struct.Struct("<6s6sIIbBBBBBBBBBBbBBBBHBBfH")


def make_c6_frame(*, csi_len: int = 512, gain: float = 2.5) -> bytes:
    values = [-3, 3, -101] + [0] * (csi_len - 3)
    if csi_len >= 12:
        values[10:12] = [-3, 3]
    csi = struct.pack(f"<{csi_len}b", *values)
    meta = META.pack(
        bytes.fromhex("1e0000000002"),
        bytes.fromhex("1a0000000001"),
        123,
        987654,
        -45,
        11,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        -99,
        0,
        11,
        0,
        0,
        47,
        0,
        0,
        gain,
        csi_len,
    )
    payload = meta + csi
    checksum = 0
    for value in payload:
        checksum ^= value
    return b"\xa5\x5a\x01" + struct.pack("<H", len(payload)) + payload + bytes([checksum])


class C6WireContractTest(unittest.TestCase):
    def test_valid_frame_exposes_training_scale_iq(self):
        report = decode_frame(make_c6_frame())

        self.assertEqual(report.seq, 123)
        self.assertEqual(report.rx_id, 2)
        self.assertEqual(report.csi_len, 512)
        self.assertEqual(report.compensated_iq[:3], (-7, 7, -252))

    def test_amplitude_excludes_c6_null_subcarriers(self):
        report = decode_frame(make_c6_frame())

        amplitude = report.amplitude

        self.assertEqual(len(amplitude), 246)
        self.assertAlmostEqual(amplitude[0], 9.899494936611665)

    def test_stream_decoder_survives_fragmentation_and_noise(self):
        frame = make_c6_frame()
        decoder = FrameDecoder()

        reports = []
        for chunk in (b"noise" + frame[:4], frame[4:119], frame[119:]):
            reports.extend(decoder.feed(chunk))

        self.assertEqual([(item.seq, item.rx_id) for item in reports], [(123, 2)])
        self.assertEqual(decoder.rejected_frames, 0)

    def test_c5_490_byte_frame_is_rejected(self):
        decoder = FrameDecoder()
        self.assertEqual(decoder.feed(make_c6_frame(csi_len=490)), [])
        self.assertEqual(decoder.rejected_frames, 1)


if __name__ == "__main__":
    unittest.main()
