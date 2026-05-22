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
import collections
import logging
import os
import sys
import time
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
from gt7coach.gui.updater import UpdateChecker, UpdateInfo
from gt7coach.gui.widgets.advice_history import AdviceHistory
from gt7coach.gui.widgets.lap_table import LapTable
from gt7coach.gui.widgets.live_log import LiveLog
from gt7coach.gui.widgets.status_panel import StatusPanel
from gt7coach.gui.widgets.update_banner import UpdateBanner

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

        # Update banner — hidden until UpdateChecker fires.
        self._update_banner = UpdateBanner()
        self._update_banner.download_clicked.connect(self._on_download_update)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(self._update_banner)
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
        tools_menu.addSeparator()
        check_updates_action = QAction("Check for updates", self)
        check_updates_action.triggered.connect(lambda: self._update_checker.check(force=True))
        tools_menu.addAction(check_updates_action)

        # --- Update checker ----------------------------------------------
        self._update_checker = UpdateChecker(self)
        self._update_checker.update_available.connect(self._update_banner.show_for)
        # Kick off a cached / lightweight check at startup. Force=False so
        # we don't hit the GitHub API more than once every 6 hours.
        self._update_checker.check(force=False)

        # --- Wire signals -------------------------------------------------
        self._runner.stderr_line.connect(self._live_log.append_line)
        self._runner.stdout_line.connect(self._live_log.append_line)
        self._runner.state_changed.connect(self._on_runner_state)
        self._runner.exited.connect(self._on_runner_exit)
        self._runner.start_failed.connect(self._on_start_failed)
        self._runner.process_ready.connect(self._on_process_ready)
        self._status_tail.event.connect(self._on_status_event)

        # State for the immediate-crash detector. process_ready stamps
        # the monotonic start time; if exited fires within ~5 s of that
        # stamp with a non-zero code, we surface the captured stderr.
        self._process_started_at: float | None = None
        self._recent_stderr: collections.deque[str] = collections.deque(maxlen=20)
        self._runner.stderr_line.connect(self._recent_stderr.append)

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

    def _on_process_ready(self) -> None:
        """Stamps the moment the subprocess actually entered Running.
        The immediate-crash detector in _on_runner_exit uses this to
        decide whether to pop up the captured stderr."""
        self._process_started_at = time.monotonic()

    def _on_start_failed(self, reason: str) -> None:
        """The runner couldn't reach Running. Show the reason loudly so
        a broken install / bad config can't fail silently."""
        QMessageBox.critical(
            self,
            "Coach failed to start",
            reason + "\n\nIf this keeps happening, try the bare CLI in PowerShell:\n"
            "    gt7coach-coach --provider gemini",
        )

    def _on_runner_exit(self, exit_code: int) -> None:
        self._status_tail.stop()
        ran_for = (
            time.monotonic() - self._process_started_at
            if self._process_started_at is not None
            else None
        )
        self._process_started_at = None

        if exit_code == 0:
            self.statusBar().showMessage("Stopped cleanly.")
        elif exit_code == 130:
            self.statusBar().showMessage("Force-stopped (second Ctrl+C / second Stop).")
        else:
            self.statusBar().showMessage(f"Subprocess exited with code {exit_code}.")

        # Immediate-crash detector. If the subprocess actually reached
        # Running but died within 5 s with a non-zero code, the user
        # almost certainly didn't see anything useful — pop up the last
        # ~20 stderr lines so the actual exception is right in their face.
        if exit_code not in (0, 130) and ran_for is not None and ran_for < 5.0:
            tail = "\n".join(self._recent_stderr) or "(no stderr captured)"
            QMessageBox.warning(
                self,
                f"Coach crashed after {ran_for:.1f}s",
                f"Exit code: {exit_code}\n\nLast log lines:\n\n{tail}",
            )

    def _on_status_event(self, ev: StatusEvent) -> None:
        self._status_panel.on_status_event(ev)
        self._lap_table.on_status_event(ev)
        self._advice_history.on_status_event(ev)

    def _open_config_dialog(self) -> None:
        dlg = ConfigDialog(self)
        dlg.exec()

    def _on_download_update(self, info: UpdateInfo) -> None:
        """Drive the self-update flow: download, verify, spawn updater.exe."""
        from gt7coach.gui.self_update import run_update_flow

        run_update_flow(self, info)

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


def _gui_log_path() -> Path:
    """Return ~/.gt7coach/gui.log (or %LOCALAPPDATA%/gt7coach/gui.log on Windows).

    Picked so the file survives even when the GUI dies before MainWindow is
    drawn — i.e. before any session/run_<ts>/debug.log can exist. The user
    can hand this file over and we see everything from import time through
    crash.
    """
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home())
        return Path(base) / "gt7coach" / "gui.log"
    return Path.home() / ".gt7coach" / "gui.log"


def _init_gui_logging(verbose: bool) -> Path:
    """Configure root logging to write everything to gui.log and stderr.

    Runs as the very first thing in main() so we capture failures during
    QApplication construction or MainWindow.__init__ — both of which were
    previously invisible when the GUI died at launch.

    Returns the resolved path of the log file so we can print it.
    """
    log_path = _gui_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = logging.Formatter(
        "%(asctime)s.%(msecs)03d %(levelname)s %(threadName)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # File handler — always DEBUG, always on, opened in append mode so a
    # restart-after-crash sequence stays in one file the user can paste.
    fh = logging.FileHandler(log_path, mode="a", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    root.addHandler(fh)

    # Stderr handler — INFO by default, DEBUG with -v.
    sh = logging.StreamHandler(stream=sys.stderr)
    sh.setLevel(logging.DEBUG if verbose else logging.INFO)
    sh.setFormatter(fmt)
    root.addHandler(sh)

    # Filter out noisy third-party loggers from the console (they still go
    # to the file). Same prefix set the receive-loop code uses.
    noisy = ("comtypes", "httpx", "httpcore", "google_genai", "urllib3", "asyncio")

    class _Noise(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            return not record.name.startswith(noisy)

    sh.addFilter(_Noise())

    # Capture ANY uncaught exception in the GUI process to the log file
    # before the process dies. Previously these went to a stderr the user
    # couldn't see (the PowerShell scrollback was too short / the window
    # closed too fast).
    def _excepthook(exc_type, exc_value, exc_tb):
        logging.getLogger("gt7coach.gui").critical(
            "uncaught exception", exc_info=(exc_type, exc_value, exc_tb)
        )
        # Still let Python's default behaviour run too.
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = _excepthook

    # Capture Qt-side warnings + errors. Qt writes these to stderr by
    # default, which the user can't see when the console scrolls past.
    try:
        from PySide6.QtCore import QtMsgType, qInstallMessageHandler

        def _qt_msg(mode, _ctx, msg):
            level = {
                QtMsgType.QtDebugMsg: logging.DEBUG,
                QtMsgType.QtInfoMsg: logging.INFO,
                QtMsgType.QtWarningMsg: logging.WARNING,
                QtMsgType.QtCriticalMsg: logging.ERROR,
                QtMsgType.QtFatalMsg: logging.CRITICAL,
            }.get(mode, logging.INFO)
            logging.getLogger("Qt").log(level, "%s", msg)

        qInstallMessageHandler(_qt_msg)
    except Exception:  # pragma: no cover — defensive
        pass

    return log_path


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="gt7coach-gui", description="GT7 AI Coach GUI")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    log_path = _init_gui_logging(args.verbose)
    boot_log = logging.getLogger("gt7coach.gui.boot")

    # Print the log path to the operator's terminal up front so they can
    # find it even if the GUI dies in the next millisecond.
    print(f"[gt7coach-gui] writing detailed log to: {log_path}", file=sys.stderr)
    boot_log.info("=" * 70)
    boot_log.info("gt7coach-gui starting")
    try:
        import platform as _platform

        from PySide6 import __version__ as pyside_version

        from gt7coach import __version__ as gt7_version

        boot_log.info(
            "versions: gt7coach=%s python=%s PySide6=%s platform=%s",
            gt7_version,
            _platform.python_version(),
            pyside_version,
            _platform.platform(),
        )
    except Exception:
        boot_log.exception("could not log version info")
    boot_log.info("cwd=%s", Path.cwd())
    boot_log.info("sys.executable=%s", sys.executable)
    boot_log.info("argv=%r", argv if argv is not None else sys.argv)

    try:
        boot_log.debug("constructing QApplication")
        app = QApplication(sys.argv)
        app.setApplicationName("GT7 AI Coach")
        app.setOrganizationName("szilvasolutions")

        boot_log.debug("constructing MainWindow")
        window = MainWindow()
        boot_log.debug("MainWindow constructed; calling show()")
        window.show()
        boot_log.info("entering app.exec() — GUI is up")
        rc = app.exec()
        boot_log.info("app.exec() returned %d — exiting cleanly", rc)
        return rc
    except SystemExit:
        raise
    except BaseException:
        boot_log.exception("fatal: GUI crashed before/inside app.exec()")
        raise


if __name__ == "__main__":
    raise SystemExit(main())
