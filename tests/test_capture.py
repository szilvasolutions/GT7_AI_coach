"""Unit tests for the capture tool.

Only the bits we can exercise without a live PS5:
    * CLI argparse — defaults and overrides.
    * .bin round-trip writer/reader.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from gt7coach.capture import (
    _FRAME_HEADER,
    BIN_HEADER_MAGIC,
    iter_bin_capture,
    parse_args,
)


def test_parse_args_defaults() -> None:
    ns = parse_args([])
    assert ns.ip == "auto"
    assert ns.format == "B"
    assert ns.duration == 0.0
    assert ns.port_rx == 33740
    assert ns.port_tx == 33739


def test_parse_args_overrides() -> None:
    ns = parse_args(
        ["--ip", "192.168.1.120", "--format", "A", "--duration", "30", "--out", "/tmp/x"]
    )
    assert ns.ip == "192.168.1.120"
    assert ns.format == "A"
    assert ns.duration == 30.0
    assert ns.out == Path("/tmp/x")


def test_iter_bin_capture_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "capture_test.bin"
    fake_packets = [
        (0.000, b"\x00" * 296),
        (0.016, b"\x11" * 296),
        (0.033, b"\xff" * 316),
    ]
    with path.open("wb") as fh:
        fh.write(BIN_HEADER_MAGIC)
        fh.write(b"B")
        for t_rel, payload in fake_packets:
            fh.write(_FRAME_HEADER.pack(t_rel, len(payload)))
            fh.write(payload)

    recovered = list(iter_bin_capture(path))
    assert len(recovered) == len(fake_packets)
    for (t_orig, p_orig), (t_back, p_back) in zip(fake_packets, recovered, strict=True):
        assert t_back == pytest.approx(t_orig)
        assert p_back == p_orig


def test_iter_bin_capture_rejects_bad_magic(tmp_path: Path) -> None:
    bad = tmp_path / "bad.bin"
    bad.write_bytes(b"NOPE0001" + b"B" + b"\x00" * 10)
    with pytest.raises(ValueError, match="not a GT7 capture"):
        list(iter_bin_capture(bad))


def test_iter_bin_capture_detects_truncation(tmp_path: Path) -> None:
    path = tmp_path / "truncated.bin"
    with path.open("wb") as fh:
        fh.write(BIN_HEADER_MAGIC)
        fh.write(b"B")
        fh.write(_FRAME_HEADER.pack(0.0, 296))
        fh.write(b"\x00" * 100)  # promised 296, wrote 100
    with pytest.raises(ValueError, match="truncated"):
        list(iter_bin_capture(path))


def test_frame_header_struct_size() -> None:
    """A small invariant: 8 bytes time + 2 bytes size = 10."""
    assert _FRAME_HEADER.size == 10
    assert _FRAME_HEADER.format == "<dH"
    # Spot-check pack/unpack
    packed = _FRAME_HEADER.pack(1.5, 296)
    assert len(packed) == 10
    assert _FRAME_HEADER.unpack(packed) == (1.5, 296)
    assert struct.calcsize("<dH") == 10
