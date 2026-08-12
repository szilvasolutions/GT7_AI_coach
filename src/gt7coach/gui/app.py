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

from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QAction, QKeySequence
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
from gt7coach.gui.theme import apply_theme
from gt7coach.gui.updater import UpdateChecker, UpdateInfo
from gt7coach.gui.widgets.advice_history import AdviceHistory
from gt7coach.gui.widgets.console_panel import ConsolePanel
from gt7coach.gui.widgets.lap_table import LapTable
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
        self.setMinimumSize(900, 540)
        self.resize(1160, 700)

        self._settings = QSettings("szilvasolutions", "gt7coach-gui")
        self._runner = CoachRunner(self)
        self._status_tail = StatusTail(self)

        # --- Toolbar ------------------------------------------------------
        toolbar = QToolBar("Main controls")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        self._start_action = QAction("▶  Start", self)
        self._start_action.triggered.connect(self._on_start)
        toolbar.addAction(self._start_action)

        self._stop_action = QAction("■  Stop", self)
        self._stop_action.triggered.connect(self._on_stop)
        self._stop_action.setEnabled(False)
        toolbar.addAction(self._stop_action)

        # Name the auto-created tool buttons so the theme can paint Start
        # green and Stop red.
        start_btn = toolbar.widgetForAction(self._start_action)
        if start_btn is not None:
            start_btn.setObjectName("startButton")
        stop_btn = toolbar.widgetForAction(self._stop_action)
        if stop_btn is not None:
            stop_btn.setObjectName("stopButton")

        toolbar.addSeparator()

        toolbar.addWidget(QLabel("Provider:"))
        self._provider_combo = QComboBox()
        self._provider_combo.addItems(_PROVIDER_CHOICES)
        self._provider_combo.setCurrentText("auto")
        toolbar.addWidget(self._provider_combo)

        toolbar.addWidget(QLabel("Voice:"))
        self._voice_combo = QComboBox()
        self._voice_combo.addItems(_VOICE_CHOICES)
        toolbar.addWidget(self._voice_combo)

        toolbar.addWidget(QLabel("Rate:"))
        self._voice_rate = QSlider(Qt.Orientation.Horizontal)
        self._voice_rate.setRange(150, 280)
        self._voice_rate.setValue(200)
        self._voice_rate.setFixedWidth(140)
        toolbar.addWidget(self._voice_rate)
        self._voice_rate_label = QLabel("200 wpm")
        self._voice_rate.valueChanged.connect(lambda v: self._voice_rate_label.setText(f"{v} wpm"))
        toolbar.addWidget(self._voice_rate_label)

        toolbar.addWidget(QLabel("Style:"))
        self._style_combo = QComboBox()
        self._style_combo.addItems(_DRIVER_STYLE_CHOICES)
        toolbar.addWidget(self._style_combo)

        toolbar.addWidget(QLabel("Car class:"))
        self._car_class_edit = QLineEdit()
        self._car_class_edit.setPlaceholderText('e.g. "Gr.3 RWD"')
        self._car_class_edit.setFixedWidth(150)
        toolbar.addWidget(self._car_class_edit)

        # --- Central split (tabbed-left | log) ---------------------------
        self._status_panel = StatusPanel()
        self._lap_table = LapTable()
        self._advice_history = AdviceHistory()
        self._live_log = ConsolePanel()
        self._live_log.hide_requested.connect(self._hide_console)

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

        # --- View menu ----------------------------------------------------
        view_menu = self.menuBar().addMenu("&View")
        self._console_action = QAction("Console log", self)
        self._console_action.setCheckable(True)
        self._console_action.setShortcut(QKeySequence("Ctrl+L"))
        self._console_action.setStatusTip("Show or hide the live console log")
        self._console_action.toggled.connect(self._on_console_toggled)
        view_menu.addAction(self._console_action)
        # Restore last session's preference (default: visible).
        console_visible = self._settings.value("view/console", True, type=bool)
        self._console_action.setChecked(console_visible)
        self._live_log.setVisible(console_visible)

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
        # Tracks whether the most recent exit was triggered by a Stop click
        # (so we don't pop a "crashed" dialog when the user asked for it).
        self._stop_requested: bool = False

        # Restore persisted window geometry + toolbar options last, so the
        # defaults above only apply on a truly fresh install.
        self._restore_settings()

    # ---- settings persistence ---------------------------------------------

    def _restore_settings(self) -> None:
        geo = self._settings.value("window/geometry")
        if geo is not None:
            self.restoreGeometry(geo)
        self._provider_combo.setCurrentText(
            str(self._settings.value("options/provider", self._provider_combo.currentText()))
        )
        self._voice_combo.setCurrentText(
            str(self._settings.value("options/voice", self._voice_combo.currentText()))
        )
        self._voice_rate.setValue(
            int(self._settings.value("options/voice_rate", self._voice_rate.value()))
        )
        self._style_combo.setCurrentText(
            str(self._settings.value("options/driver_style", self._style_combo.currentText()))
        )
        self._car_class_edit.setText(str(self._settings.value("options/car_class", "")))

    def _save_settings(self) -> None:
        self._settings.setValue("window/geometry", self.saveGeometry())
        self._settings.setValue("options/provider", self._provider_combo.currentText())
        self._settings.setValue("options/voice", self._voice_combo.currentText())
        self._settings.setValue("options/voice_rate", self._voice_rate.value())
        self._settings.setValue("options/driver_style", self._style_combo.currentText())
        self._settings.setValue("options/car_class", self._car_class_edit.text().strip())
        self._settings.setValue("view/console", self._console_action.isChecked())

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
        """Wrap the entire Start handler in an exception logger because
        PySide6 swallows uncaught slot exceptions silently (they reach
        sys.unraisablehook, not sys.excepthook). Without this wrapper a
        plain AttributeError anywhere in the body looks like nothing
        happened."""
        log.info("=== Start button clicked ===")
        try:
            self._do_start()
        except BaseException:
            log.exception("Start: handler crashed with an exception")
            try:
                QMessageBox.critical(
                    self,
                    "Start failed",
                    "An exception was raised inside the Start handler.\n"
                    "Open gui.log for the full traceback:\n"
                    f"  {_gui_log_path()}",
                )
            except Exception:
                pass

    def _do_start(self) -> None:
        log.debug("Start: checking is_running")
        if self._runner.is_running():
            log.info("Start: runner already running, ignoring click")
            return
        log.debug("Start: clearing live log")
        self._live_log.clear_log()
        log.debug("Start: clearing status panel")
        self._status_panel.clear_all()
        log.debug("Start: clearing lap table")
        self._lap_table.clear_all()
        log.debug("Start: clearing advice history")
        self._advice_history.clear_all()
        log.debug("Start: building options")
        opts = self._build_options()
        log.info(
            "Start: built options provider=%r voice=%r voice_rate=%d style=%r car_class=%r",
            opts.provider,
            opts.voice,
            opts.voice_rate,
            opts.driver_style,
            opts.car_class,
        )
        log.info("Start: calling runner.start(opts)")
        self._runner.start(opts)
        log.info("Start: runner.start() returned (subprocess spawn requested)")
        # Begin tailing the status file the runner just allocated.
        if self._runner.status_file is not None:
            log.info("Start: watching status file %s", self._runner.status_file)
            self._status_tail.watch(self._runner.status_file)
        else:
            log.warning("Start: runner has no status_file — subprocess probably refused to start")

    def _on_stop(self) -> None:
        if not self._runner.is_running():
            return
        self._stop_requested = True
        log.info("Stop button clicked — graceful stop requested")
        self._runner.stop()
        self.statusBar().showMessage("Stopping… (will force-quit if not gone in 3 s)")
        # On Windows, QProcess.terminate() sends WM_CLOSE which a console
        # subprocess ignores. Schedule a forced kill if the graceful stop
        # hasn't taken effect within 3 seconds — the operator clicked Stop
        # because they want it gone NOW.
        from PySide6.QtCore import QTimer

        QTimer.singleShot(3000, self._stop_force_kill_if_alive)

    def _stop_force_kill_if_alive(self) -> None:
        if self._runner.is_running():
            log.warning("Stop: subprocess still alive after 3 s grace — force killing")
            self._runner.stop()  # second call escalates to kill() in CoachRunner.stop()

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
        was_user_stop = self._stop_requested
        self._stop_requested = False

        if exit_code == 0:
            self.statusBar().showMessage("Stopped cleanly.")
        elif exit_code == 130:
            self.statusBar().showMessage("Force-stopped (second Ctrl+C / second Stop).")
        else:
            self.statusBar().showMessage(f"Subprocess exited with code {exit_code}.")

        # Detect the most common "first launch" failure: PS5 not on / GT7
        # not running. The subprocess takes ~6 s to give up (3 s broadcast +
        # 3 s subnet scan) so the 5 s immediate-crash window misses it.
        # Scan the captured stderr for the specific error string.
        joined_stderr = "\n".join(self._recent_stderr)
        if "PS5 not discovered" in joined_stderr and not was_user_stop:
            QMessageBox.warning(
                self,
                "Can't find your PS5",
                "Couldn't reach your PS5 on the LAN.\n\n"
                "Most likely cause: Gran Turismo 7 isn't running yet.\n"
                "Telemetry only flows when GT7 is in a race / time trial.\n\n"
                "Steps:\n"
                "  1. Turn the PS5 on (same network as this laptop).\n"
                "  2. Start GT7 and enter a race or time trial.\n"
                "  3. Come back here and click Start again.",
            )
            return

        # Immediate-crash detector. If the subprocess actually reached
        # Running but died within 5 s with a non-zero code, the user
        # almost certainly didn't see anything useful — pop up the last
        # ~20 stderr lines so the actual exception is right in their face.
        if (
            exit_code not in (0, 130)
            and not was_user_stop
            and ran_for is not None
            and ran_for < 5.0
        ):
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

    def _on_console_toggled(self, checked: bool) -> None:
        self._live_log.setVisible(checked)
        # Persist immediately — the preference should survive even a crash.
        self._settings.setValue("view/console", checked)

    def _hide_console(self) -> None:
        """The console's own ✕ button — route through the menu action so
        the checkbox stays in sync."""
        self._console_action.setChecked(False)

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
        log.info(
            "MainWindow.closeEvent — runner_running=%s",
            self._runner.is_running(),
        )
        self._save_settings()
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

    # Exceptions raised inside PySide6 slots do NOT trigger sys.excepthook —
    # they go through sys.unraisablehook (CPython >= 3.8). Without this,
    # a TypeError or AttributeError inside e.g. _on_start vanishes into
    # the void: the slot returns, the GUI stays running, and the operator
    # has no clue why their click did nothing.
    def _unraisablehook(unraisable):
        logging.getLogger("gt7coach.gui").critical(
            "unraisable exception in slot: %s",
            unraisable.err_msg or "(no message)",
            exc_info=(unraisable.exc_type, unraisable.exc_value, unraisable.exc_traceback),
        )
        sys.__unraisablehook__(unraisable)

    sys.unraisablehook = _unraisablehook

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
    argv = list(sys.argv[1:]) if argv is None else argv
    # Frozen-bundle dispatch: inside the PyInstaller bundle there is no
    # python.exe, so CoachRunner re-launches THIS executable with
    # --run-coach to start a session. Route that straight to the coach CLI
    # before any GUI setup happens.
    if argv[:1] == ["--run-coach"]:
        from gt7coach.main import main as coach_main

        return coach_main(argv[1:])

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
        apply_theme(app)
        app.aboutToQuit.connect(lambda: boot_log.info("aboutToQuit signal received"))

        boot_log.debug("constructing MainWindow")
        window = MainWindow()
        boot_log.debug("MainWindow constructed; calling show()")
        window.show()
        # Force-bring the window to the front. On Windows, a Qt window
        # spawned from a PowerShell-launched Python often appears BEHIND
        # the terminal — show() draws it but doesn't push it to the top
        # of the Z-order. raise_() + activateWindow() makes the window
        # the active foreground window the way a user-launched .exe would.
        window.raise_()
        window.activateWindow()

        # Heartbeat: log every 5 s while the GUI is alive. If the operator
        # reports "the GUI exited with nothing", the heartbeats tell us
        # how long the process actually lived and roughly where the death
        # happened.
        from PySide6.QtCore import QTimer

        heartbeat_count = {"n": 0}

        def _heartbeat() -> None:
            heartbeat_count["n"] += 1
            try:
                visible = window.isVisible()
                geo = window.geometry()
                geo_str = f"{geo.width()}x{geo.height()}+{geo.x()},{geo.y()}"
            except Exception:
                visible = "?"
                geo_str = "?"
            boot_log.info(
                "heartbeat #%d (window visible=%s geom=%s)",
                heartbeat_count["n"],
                visible,
                geo_str,
            )

        hb_timer = QTimer()
        hb_timer.timeout.connect(_heartbeat)
        hb_timer.start(5000)

        boot_log.info(
            "entering app.exec() — window visible=%s, isMinimized=%s, geom=%dx%d+%d,%d",
            window.isVisible(),
            window.isMinimized(),
            window.geometry().width(),
            window.geometry().height(),
            window.geometry().x(),
            window.geometry().y(),
        )
        rc = app.exec()
        boot_log.info("app.exec() returned %d — exiting cleanly", rc)
        return rc
    except SystemExit as exc:
        boot_log.warning("SystemExit raised during GUI lifecycle: code=%r", exc.code)
        raise
    except BaseException:
        boot_log.exception("fatal: GUI crashed before/inside app.exec()")
        raise


if __name__ == "__main__":
    raise SystemExit(main())
