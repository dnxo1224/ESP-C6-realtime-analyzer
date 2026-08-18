import hashlib
import sys
import unittest
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server" / "inference"))

from c6_pipeline import transform_window


class C6ModelContractTest(unittest.TestCase):
    def test_handoff_feature_vector_is_reproduced_exactly(self):
        time = np.arange(200, dtype=np.float32)[:, None, None]
        nodes = np.arange(4, dtype=np.float32)[None, :, None]
        subcarriers = np.arange(246, dtype=np.float32)[None, None, :]
        amplitude = (
            1.0
            + 0.01 * time
            + 0.1 * nodes
            + 0.001 * subcarriers
            + 0.05 * np.sin(time / 7 + nodes)
        )
        dsd = np.linspace(0.1, 1.0, 4 * 246, dtype=np.float32).reshape(4, 246)

        features = transform_window(amplitude, dsd)

        self.assertEqual(features.shape, (1, 2168))
        digest = hashlib.sha256(features.astype("<f4").tobytes()).hexdigest()
        self.assertEqual(digest, "bde6bedb8e2cbd4310129164986189aa050e8ec754cd7dc70b8ad8f099029299")


if __name__ == "__main__":
    unittest.main()
