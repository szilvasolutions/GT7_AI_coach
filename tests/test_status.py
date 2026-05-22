"""Tests for ``gt7coach.status`` — the opt-in JSONL status emitter."""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from gt7coach import status


@pytest.fixture(autouse=True)
def reset_module_state(monkeypatch):
    """The status module memoises ``enabled()`` after first call. Reset it
    between tests so each one starts from a clean slate."""
    monkeypatch.delenv("GT7COACH_STATUS_FILE", raising=False)
    importlib.reload(status)
    yield
    importlib.reload(status)


def test_disabled_without_env_var() -> None:
    assert status.enabled() is False
    status.emit("corner", corner_idx=1)  # must not raise


def test_enabled_when_env_set_writes_jsonl(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "status.jsonl"
    monkeypatch.setenv("GT7COACH_STATUS_FILE", str(target))

    assert status.enabled() is True
    assert target.exists(), "file should be created on first enabled() call"

    status.emit("corner", corner_idx=3, duration_s=4.2)
    status.emit("advice", advice="Brake earlier next lap.", event_type="braking.late_brake")

    lines = target.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2

    rec0 = json.loads(lines[0])
    assert rec0["type"] == "corner"
    assert rec0["corner_idx"] == 3
    assert rec0["duration_s"] == 4.2
    assert isinstance(rec0["ts"], (int, float))

    rec1 = json.loads(lines[1])
    assert rec1["type"] == "advice"
    assert rec1["advice"] == "Brake earlier next lap."


def test_emit_skips_unserializable_payload(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "status.jsonl"
    monkeypatch.setenv("GT7COACH_STATUS_FILE", str(target))

    class _Weird:
        pass

    status.enabled()
    status.emit("track", obj=_Weird())  # default=str will stringify the repr

    lines = target.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["type"] == "track"
    assert "obj" in rec  # serialized via default=str


def test_truncates_stale_file(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "status.jsonl"
    target.write_text('{"type":"old","ts":0}\n', encoding="utf-8")
    monkeypatch.setenv("GT7COACH_STATUS_FILE", str(target))

    status.enabled()
    status.emit("rx_stats", hz=60.0)

    lines = target.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["type"] == "rx_stats"
