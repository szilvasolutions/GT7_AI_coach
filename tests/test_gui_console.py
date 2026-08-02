"""Console panel: colorized log lines, clear, hide signal, theme sheet."""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6", reason="PySide6 not installed; skipping GUI tests")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QToolButton

from gt7coach.gui.theme import apply_theme, build_stylesheet
from gt7coach.gui.widgets.console_panel import ConsolePanel, LiveLog


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def test_live_log_keeps_text_across_levels(qapp) -> None:
    log = LiveLog()
    lines = [
        "13:41:02 INFO gt7coach.coach: provider: gemini",
        "13:41:14 WARNING gt7coach.coach.advisor: provider slow",
        "13:41:20 ERROR gt7coach.telemetry: socket died",
        "13:41:25 INFO gt7coach.coach: coach -> 'Brake earlier.'",
        "line with <html> & entities",
    ]
    for line in lines:
        log.append_line(line)
    text = log.toPlainText()
    for line in lines:
        assert line in text  # colouring must never eat or mangle content


def test_live_log_still_caps_line_count(qapp) -> None:
    log = LiveLog()
    for i in range(1100):
        log.append_line(f"line {i}")
    assert log.blockCount() <= 1000


def test_console_panel_delegates_and_clears(qapp) -> None:
    panel = ConsolePanel()
    panel.append_line("hello")
    assert "hello" in panel.log.toPlainText()
    panel.clear_log()
    assert panel.log.toPlainText() == ""


def test_console_panel_hide_button_emits_signal(qapp) -> None:
    panel = ConsolePanel()
    fired: list[bool] = []
    panel.hide_requested.connect(lambda: fired.append(True))
    close_buttons = [b for b in panel.findChildren(QToolButton) if b.text() == "✕"]
    assert len(close_buttons) == 1
    close_buttons[0].click()
    assert fired == [True]


def test_theme_applies_and_references_bundled_arrow(qapp) -> None:
    sheet = build_stylesheet()
    assert "arrow_down.svg" in sheet
    # The referenced asset must actually exist next to the module.
    from gt7coach.gui.theme import _ASSETS

    assert (_ASSETS / "arrow_down.svg").is_file()
    apply_theme(qapp)  # must not raise; Fusion + sheet accepted
    assert qapp.styleSheet()
