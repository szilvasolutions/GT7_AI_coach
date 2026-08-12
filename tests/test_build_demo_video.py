"""Sync-math tests for scripts/build_demo_video.py (fit + alignment only —
the ffmpeg mux itself is exercised manually on real recordings).
"""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "build_demo_video",
    Path(__file__).resolve().parents[1] / "scripts" / "build_demo_video.py",
)
bdv = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(bdv)

WALL0 = 1_800_000_000.0  # arbitrary epoch anchor


def _cues(n: int, spacing: float = 7.0) -> list[float]:
    return [WALL0 + i * spacing for i in range(n)]


def _onsets(cues: list[float], offset: float, drift: float = 1.0) -> list[float]:
    return [offset + drift * (c - cues[0]) for c in cues]


def test_fit_recovers_offset_and_drift():
    cues = _cues(8)
    onsets = _onsets(cues, offset=12.345, drift=1.0001)
    offset, drift, res = bdv.fit_offset_drift(cues, onsets, cues[0])
    assert offset == pytest.approx(12.345, abs=1e-6)
    assert drift == pytest.approx(1.0001, abs=1e-8)
    assert max(abs(r) for r in res) < 1e-3


def test_fit_tolerates_measurement_noise():
    cues = _cues(10)
    noise = [+0.01, -0.012, 0.008, -0.005, 0.011, -0.009, 0.004, -0.01, 0.007, -0.003]
    onsets = [o + n for o, n in zip(_onsets(cues, offset=5.0), noise, strict=True)]
    offset, _drift, res = bdv.fit_offset_drift(cues, onsets, cues[0])
    assert offset == pytest.approx(5.0, abs=0.02)
    assert max(abs(r) for r in res) < 25  # ms


def test_align_skips_leading_spurious_onsets():
    cues = _cues(6)
    real = _onsets(cues, offset=30.0)
    onsets = [1.2, 4.7, *real]  # Audacity clicks before the coach spoke
    matched, offset, _drift, _res = bdv.align(cues, onsets, max_residual_ms=50)
    assert matched == real
    assert offset == pytest.approx(30.0, abs=1e-6)


def test_align_with_merged_onsets_fits_cue_window():
    # silencedetect merged the last two cues -> one onset short.
    cues = _cues(6)
    onsets = _onsets(cues, offset=8.0)[:-1]
    matched, offset, _drift, _res = bdv.align(cues, onsets, max_residual_ms=50)
    assert len(matched) == len(onsets)
    assert offset == pytest.approx(8.0, abs=1e-6)


def test_align_fails_loudly_on_garbage():
    cues = _cues(6)
    onsets = [3.0, 9.5, 11.2, 25.0, 26.1, 40.0]  # unrelated timings
    with pytest.raises(SystemExit):
        bdv.align(cues, onsets, max_residual_ms=50)


def test_parse_timestamp_forms():
    assert bdv.parse_timestamp("83.4") == pytest.approx(83.4)
    assert bdv.parse_timestamp("1:23.4") == pytest.approx(83.4)
    assert bdv.parse_timestamp("1:01:23.4") == pytest.approx(3683.4)
    with pytest.raises(argparse.ArgumentTypeError):
        bdv.parse_timestamp("1:2:3:4")
