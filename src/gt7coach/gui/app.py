"""``gt7coach-gui`` — PySide6 main window.

The top of the window is a single toolbar: Start / Stop + the four
most-used options (provider, voice, voice-rate, driver style). Below
is a horizontal split: left = live status panel (connection, track,
corners, last advice, lap times); right = live log tail of the
coach subprocess stderr.

Phase B scope only. Config editing, voice testing, lap tables, and
the update banner come in Phase C / C.5.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QSlider,
    QSplitter,
    QStatusBar,
    QTabWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from gt7coach.gui.config_panel import ConfigDialog
from gt7coach.gui.log_tail import StatusEvent, StatusTail
from gt7coach.gui.runner import CoachOptions, CoachRunner
from gt7coach.gui.widgets.advice_history import AdviceHistory
from gt7coach.gui.widgets.lap_table import LapTable
from gt7coach.gui.widgets.live_log import LiveLog
from gt7coach.gui.widgets.status_panel import StatusPanel

log = logging.getLogger("gt7coach.gui")


_PROVIDER_CHOICES = ["auto", "gemini", "anthropic", "openai", "ollama", "mock"]
_VOICE_CHOICES = ["pyttsx3", "null"]
_DRIVER_STYLE_CHOICES = ["smooth", "aggressive", "learning"]


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("GT7 AI Coach")
        self.resize(1100, 680)

        self._runner = CoachRunner(self)
        self._status_tail = StatusTail(self)

        # --- Toolbar ------------------------------------------------------
        toolbar = QToolBar("Main controls")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        self._start_action = QAction("Start", self)
        self._start_action.triggered.connect(self._on_start)
        toolbar.addAction(self._start_action)

        self._stop_action = QAction("Stop", self)
        self._stop_action.triggered.connect(self._on_stop)
        self._stop_action.setEnabled(False)
        toolbar.addAction(self._stop_action)

        toolbar.addSeparator()

        toolbar.addWidget(QLabel(" Provider: "))
        self._provider_combo = QComboBox()
        self._provider_combo.addItems(_PROVIDER_CHOICES)
        self._provider_combo.setCurrentText("auto")
        toolbar.addWidget(self._provider_combo)

        toolbar.addWidget(QLabel(" Voice: "))
        self._voice_combo = QComboBox()
        self._voice_combo.addItems(_VOICE_CHOICES)
        toolbar.addWidget(self._voice_combo)

        toolbar.addWidget(QLabel(" Rate: "))
        self._voice_rate = QSlider(Qt.Orientation.Horizontal)
        self._voice_rate.setRange(150, 280)
        self._voice_rate.setValue(200)
        self._voice_rate.setFixedWidth(140)
        toolbar.addWidget(self._voice_rate)
        self._voice_rate_label = QLabel("200 wpm")
        self._voice_rate.valueChanged.connect(lambda v: self._voice_rate_label.setText(f"{v} wpm"))
        toolbar.addWidget(self._voice_rate_label)

        toolbar.addWidget(QLabel(" Style: "))
        self._style_combo = QComboBox()
        self._style_combo.addItems(_DRIVER_STYLE_CHOICES)
        toolbar.addWidget(self._style_combo)

        toolbar.addWidget(QLabel(" Car class: "))
        self._car_class_edit = QLineEdit()
        self._car_class_edit.setPlaceholderText('e.g. "Gr.3 RWD"')
        self._car_class_edit.setFixedWidth(160)
        toolbar.addWidget(self._car_class_edit)

        # --- Central split (tabbed-left | log) ---------------------------
        self._status_panel = StatusPanel()
        self._lap_table = LapTable()
        self._advice_history = AdviceHistory()
        self._live_log = LiveLog()

        left_tabs = QTabWidget()
        left_tabs.addTab(self._status_panel, "Status")
        left_tabs.addTab(self._lap_table, "Laps")
        left_tabs.addTab(self._advice_history, "Advice")

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(left_tabs)
        splitter.addWidget(self._live_log)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([400, 700])

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(splitter)
        self.setCentralWidget(central)

        # --- Status bar ---------------------------------------------------
        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("Ready. Press Start to launch the coach.")

        # --- File menu ----------------------------------------------------
        file_menu = self.menuBar().addMenu("&File")
        open_log_action = QAction("Open last session folder…", self)
        open_log_action.triggered.connect(self._open_last_session)
        file_menu.addAction(open_log_action)
        file_menu.addSeparator()
        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        # --- Tools menu ---------------------------------------------------
        tools_menu = self.menuBar().addMenu("&Tools")
        configure_action = QAction("Configure…", self)
        configure_action.triggered.connect(self._open_config_dialog)
        tools_menu.addAction(configure_action)
        voice_test_action = QAction("Test voice", self)
        voice_test_action.triggered.connect(self._on_voice_test)
        tools_menu.addAction(voice_test_action)

        # --- Wire signals -------------------------------------------------
        self._runner.stderr_line.connect(self._live_log.append_line)
        self._runner.stdout_line.connect(self._live_log.append_line)
        self._runner.state_changed.connect(self._on_runner_state)
        self._runner.exited.connect(self._on_runner_exit)
        self._status_tail.event.connect(self._on_status_event)

    # ---- handlers ---------------------------------------------------------

    def _build_options(self) -> CoachOptions:
        provider = self._provider_combo.currentText()
        return CoachOptions(
            provider=None if provider == "auto" else provider,
            voice=self._voice_combo.currentText(),
            voice_rate=self._voice_rate.value(),
            driver_style=self._style_combo.currentText(),
            car_class=self._car_class_edit.text().strip(),
        )

    def _on_start(self) -> None:
        if self._runner.is_running():
            return
        self._live_log.clear_log()
        self._status_panel.reset()
        self._lap_table.reset()
        self._advice_history.reset()
        opts = self._build_options()
        try:
            self._runner.start(opts)
        except Exception as exc:  # pragma: no cover — defensive
            QMessageBox.critical(self, "Failed to start", str(exc))
            return
        # Begin tailing the status file the runner just allocated.
        if self._runner.status_file is not None:
            self._status_tail.watch(self._runner.status_file)

    def _on_stop(self) -> None:
        if not self._runner.is_running():
            return
        self._runner.stop()

    def _on_runner_state(self, state: str) -> None:
        self.statusBar().showMessage(f"Runner: {state}")
        running = state in ("starting", "running", "stopping")
        self._start_action.setEnabled(not running)
        self._stop_action.setEnabled(running)

    def _on_runner_exit(self, exit_code: int) -> None:
        self._status_tail.stop()
        if exit_code == 0:
            self.statusBar().showMessage("Stopped cleanly.")
        elif exit_code == 130:
            self.statusBar().showMessage("Force-stopped (second Ctrl+C / second Stop).")
        else:
            self.statusBar().showMessage(f"Subprocess exited with code {exit_code}.")

    def _on_status_event(self, ev: StatusEvent) -> None:
        self._status_panel.on_status_event(ev)
        self._lap_table.on_status_event(ev)
        self._advice_history.on_status_event(ev)

    def _open_config_dialog(self) -> None:
        dlg = ConfigDialog(self)
        dlg.exec()

    def _on_voice_test(self) -> None:
        """Speak a test phrase in-process using the toolbar's current
        voice settings. Runs on a short-lived thread so we don't block
        the Qt event loop while pyttsx3 warms up the audio device."""
        from threading import Thread

        engine = self._voice_combo.currentText()
        rate = self._voice_rate.value()

        def worker() -> None:
            try:
                from gt7coach.voice import make_voice

                voice = make_voice(engine, rate=rate)
                voice.speak("Coach ready. Testing voice.")
                import time as _time

                deadline = _time.monotonic() + 8.0
                while not voice.is_idle() and _time.monotonic() < deadline:
                    _time.sleep(0.05)
                voice.stop()
            except Exception as exc:  # pragma: no cover — defensive
                log.warning("voice test failed: %s", exc)

        Thread(target=worker, name="gt7-gui-voice-test", daemon=True).start()
        self.statusBar().showMessage(f"Speaking test phrase via {engine}…")

    def _open_last_session(self) -> None:
        # The default log dir is ./sessions; offer that as the starting
        # point for the picker so the user lands in the right place.
        start = Path.cwd() / "sessions"
        if not start.exists():
            start = Path.cwd()
        path = QFileDialog.getExistingDirectory(self, "Open session folder", str(start))
        if path:
            # Cross-platform "open folder in file manager":
            from PySide6.QtCore import QUrl
            from PySide6.QtGui import QDesktopServices

            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def closeEvent(self, event) -> None:
        if self._runner.is_running():
            self._runner.stop()
        self._status_tail.stop()
        super().closeEvent(event)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="gt7coach-gui", description="GT7 AI Coach GUI")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    app = QApplication(sys.argv)
    app.setApplicationName("GT7 AI Coach")
    app.setOrganizationName("szilvasolutions")

    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
