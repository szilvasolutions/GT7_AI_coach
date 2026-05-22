"""Tests for the Phase C left-tab widgets (lap table + advice history).

These don't need a real subprocess — we instantiate the widgets in
offscreen mode and feed them StatusEvent objects directly.
"""

from __future__ import annotations

import os
import time

import pytest

pytest.importorskip("PySide6", reason="PySide6 not installed; skipping GUI tests")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from gt7coach.gui.log_tail import StatusEvent
from gt7coach.gui.widgets.advice_history import AdviceHistory
from gt7coach.gui.widgets.lap_table import LapTable


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _lap(lap_no: int, last_ms: int, best_ms: int) -> StatusEvent:
    return StatusEvent(
        ts=time.time(),
        type="lap",
        payload={"lap": lap_no, "last_lap_ms": last_ms, "best_lap_ms": best_ms},
    )


def _advice(text: str, event_type: str = "braking.late_brake") -> StatusEvent:
    return StatusEvent(
        ts=time.time(),
        type="advice",
        payload={"advice": text, "event_type": event_type},
    )


def test_lap_table_appends_rows_and_marks_best(qapp) -> None:
    t = LapTable()
    t.on_status_event(_lap(1, 60_922, 60_922))  # first lap = best
    t.on_status_event(_lap(2, 52_784, 52_784))  # new best
    t.on_status_event(_lap(3, 53_500, 52_784))  # slower

    assert t.rowCount() == 3
    assert t.item(0, 0).text() == "1"
    assert t.item(1, 0).text() == "2"
    assert t.item(2, 0).text() == "3"
    # Best lap is now row 1 (the 0:52.784).
    best_item = t.item(1, 1)
    assert best_item.font().bold(), "best lap time must be bold"
    # Delta column for non-best row 2: '+0:00.716'
    delta_text = t.item(2, 2).text()
    assert delta_text.startswith("+0:00")


def test_lap_table_ignores_invalid_lap_time(qapp) -> None:
    t = LapTable()
    t.on_status_event(_lap(0, 0, 0))  # GT7's -1 / 0 sentinel
    t.on_status_event(_lap(1, -1, 0))
    assert t.rowCount() == 0


def test_lap_table_reset_clears(qapp) -> None:
    t = LapTable()
    t.on_status_event(_lap(1, 90_000, 90_000))
    t.reset()
    assert t.rowCount() == 0


def test_advice_history_appends(qapp) -> None:
    h = AdviceHistory()
    h.on_status_event(_advice("Brake earlier into that turn next lap."))
    h.on_status_event(
        _advice("Carry throttle deeper through that turn.", event_type="throttle.early_lift")
    )
    h.on_status_event(_advice("", event_type="ignored"))  # empty advice -> skipped

    assert h.count() == 2
    first = h.item(0).text()
    assert "Brake earlier" in first
    assert "braking.late_brake" in first
    second = h.item(1).text()
    assert "throttle.early_lift" in second


def test_advice_history_ignores_other_event_types(qapp) -> None:
    h = AdviceHistory()
    h.on_status_event(StatusEvent(ts=time.time(), type="corner", payload={"corner_idx": 1}))
    h.on_status_event(StatusEvent(ts=time.time(), type="rx_stats", payload={"hz": 60.0}))
    assert h.count() == 0
