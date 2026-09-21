"""popcount compat: _popcount_sum matches np.bitwise_count semantics
on any NumPy >= 1.24 (the floor), so binary-codec scoring works with
or without the NumPy 2.0 API."""
from __future__ import annotations

import numpy as np

from cortexm.vsa.codecs import _popcount_sum


def test_popcount_known_values() -> None:
    x = np.array([[0xFF, 0x00, 0xF0]], dtype=np.uint8)
    assert list(_popcount_sum(x, axis=1)) == [8 + 0 + 4]
    assert int(_popcount_sum(np.array([0xFF], dtype=np.uint8))) == 8
    assert int(_popcount_sum(np.array([0x00], dtype=np.uint8))) == 0


def test_popcount_matches_bitwise_count_when_present() -> None:
    if not hasattr(np, "bitwise_count"):
        return
    rng = np.random.default_rng(7)
    x = rng.integers(0, 256, size=(16, 32)).astype(np.uint8)
    assert (_popcount_sum(x, axis=1) == np.bitwise_count(x).sum(axis=1)).all()
    assert int(_popcount_sum(x)) == int(np.bitwise_count(x).sum())