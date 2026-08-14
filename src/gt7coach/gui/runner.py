"""QProcess wrapper around ``gt7coach-coach``.

Why a subprocess and not in-process: keeping the pipeline in its own
process means a crash in pyttsx3 / SAPI / the LLM client cannot kill
the GUI. The existing signal handler in ``main.py`` (first SIGINT =
drain, second = ``os._exit(130)``) means a second Stop click force-
quits cleanly. The GUI just builds an argv from form state.

This module owns the subprocess lifecycle and exposes Qt signals for:

* :attr:`CoachRunner.stdout_line` — one decoded UTF-8 line at a time
* :attr:`CoachRunner.stderr_line` — same, separate stream
* :attr:`CoachRunner.state_changed` — running / stopped / errored
* :attr:`CoachRunner.exited` — emitted once with the process exit code

The status events (corner / lap / advice / rx_stats / track) are NOT
read here — they go through :class:`StatusTail` in ``log_tail.py``
because the file-based protocol is simpler and survives a GUI restart
mid-run.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, Signal

log = logging.getLogger(__name__)


@dataclass
class CoachOptions:
    """Snapshot of the GUI's form state, translated to gt7coach-coach argv."""

    provider: str | None = None  # None = let CLI auto-pick
    api_key: str | None = None
    voice: str = "pyttsx3"
    voice_rate: int = 200
    driver_style: str = "smooth"
    car_class: str = ""
    track: str = ""
    cooldown: float = 4.0
    no_summary: bool = False
    verbose: bool = False
    extra_args: list[str] = field(default_factory=list)

    def to_argv(self) -> list[str]:
        argv: list[str] = []
        if self.provider:
            argv += ["--provider", self.provider]
        if self.api_key:
            argv += ["--api-key", self.api_key]
        argv += ["--voice", self.voice, "--voice-rate", str(self.voice_rate)]
        argv += ["--driver-style", self.driver_style]
        if self.car_class:
            argv += ["--car-class", self.car_class]
        if self.track:
            argv += ["--track", self.track]
        argv += ["--cooldown", str(self.cooldown)]
        if self.no_summary:
            argv += ["--no-summary"]
        if self.verbose:
            argv += ["-v"]
        argv += list(self.extra_args)
        return argv


class CoachRunner(QObject):
    """Lifecycle manager for one ``gt7coach-coach`` subprocess."""

    stdout_line = Signal(str)
    stderr_line = Signal(str)
    state_changed = Signal(str)  # "starting" | "running" | "stopping" | "stopped"
    exited = Signal(int)  # exit code (Qt's int — 0 on clean exit)
    # Emitted with a human-readable reason whenever Start cannot reach the
    # "running" state (pre-flight rejected the launch, or QProcess errored
    # out before/during spawn). MainWindow shows a QMessageBox.critical on
    # this so any failure is immediately visible to the operator.
    start_failed = Signal(str)
    # Emitted exactly once per subprocess from the QProcess.started slot
    # (i.e. the OS has spawned the process and it's actually running).
    # Distinct from state_changed("running") so MainWindow can stamp a
    # monotonic clock for the immediate-crash detector.
    process_ready = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._proc: QProcess | None = None
        self._status_file: Path | None = None
        self._stop_file: Path | None = None
        # True from the moment the user asks to stop until the process is
        # gone, so an abnormal exit code isn't reported as a crash.
        self._stopping = False
        self._stdout_buf = ""
        self._stderr_buf = ""

    # ---- lifecycle ---------------------------------------------------------

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.state() != QProcess.ProcessState.NotRunning

    @property
    def status_file(self) -> Path | None:
        """The status-event file the subprocess is writing to (if running)."""
        return self._status_file

    def start(self, options: CoachOptions) -> None:
        if self.is_running():
            log.warning("runner already running; ignoring start()")
            return

        # Pre-flight: confirm the module is importable from sys.executable.
        # Without this, a broken install surfaces as a generic
        # QProcess.FailedToStart with no useful diagnostic on Windows.
        if not python_module_available("gt7coach.main"):
            self.start_failed.emit(
                "gt7coach.main is not importable from this Python interpreter.\n"
                f"  sys.executable: {sys.executable}\n\n"
                'Reinstall with:  pip install -e ".[gui]"'
            )
            return

        # Allocate a fresh status file under the user's temp dir. Phase B's
        # status emitter truncates it on first emit, so stale content is OK.
        tmpdir = Path(tempfile.gettempdir()) / "gt7coach-gui"
        tmpdir.mkdir(parents=True, exist_ok=True)
        self._status_file = tmpdir / f"status-{os.getpid()}.jsonl"
        self._stop_file = tmpdir / f"stop-{os.getpid()}"
        self._stop_file.unlink(missing_ok=True)
        self._stopping = False

        env = QProcessEnvironment.systemEnvironment()
        env.insert("GT7COACH_STATUS_FILE", str(self._status_file))
        env.insert("GT7COACH_STOP_FILE", str(self._stop_file))

        cwd = Path.cwd()

        self._proc = QProcess(self)
        self._proc.setProgram(sys.executable)
        from gt7coach.runtime import is_frozen

        if is_frozen():
            # Packaged build (PyInstaller or Nuitka): sys.executable is
            # GT7Coach.exe itself and
            # there is no -m machinery. gui/app.py:main() recognises
            # --run-coach and dispatches to gt7coach.main before any GUI
            # setup.
            self._proc.setArguments(["--run-coach", *options.to_argv()])
        else:
            # Use the module entry point so the subprocess works no matter
            # how the user installed (`pip install -e .`, wheel, checkout).
            self._proc.setArguments(["-m", "gt7coach.main", *options.to_argv()])
        self._proc.setProcessEnvironment(env)
        # Pin the working directory so .env / config.yaml auto-discovery sees
        # the place the GUI was launched from, no matter what the GUI does
        # later. Also makes the path obvious in the live log.
        self._proc.setWorkingDirectory(str(cwd))
        self._proc.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        self._proc.readyReadStandardOutput.connect(self._on_stdout)
        self._proc.readyReadStandardError.connect(self._on_stderr)
        self._proc.finished.connect(self._on_finished)
        self._proc.errorOccurred.connect(self._on_error)
        self._proc.started.connect(self._on_started)

        log.info(
            "starting gt7coach-coach: cwd=%s argv=%r",
            cwd,
            options.to_argv(),
        )
        self.state_changed.emit("starting")
        # On Windows, CREATE_NEW_PROCESS_GROUP is needed if we want to send
        # CTRL_BREAK_EVENT later. Qt doesn't expose that flag directly; we
        # send SIGINT via QProcess.terminate() which maps to the right OS
        # signal on each platform, and the existing main.py shutdown handler
        # (commit 3937e8d) treats a second SIGINT as force-quit.
        self._proc.start()
        # NB: do NOT emit "running" here. QProcess.start() is async; the
        # state only becomes truly Running once the OS has spawned the
        # process. _on_started below handles that transition.

    def stop(self) -> None:
        """Ask the coach to shut down. Second call force-kills."""
        if not self.is_running():
            return
        assert self._proc is not None
        # First stop: gentle. main.py drains the advisor + voice and writes
        # meta.json on the way out. Second stop: kill.
        if self._proc.property("gt7_stop_count"):
            log.warning("second stop request — killing subprocess")
            self._stopping = True
            self._proc.kill()
            self.state_changed.emit("stopping")
            return
        self._stopping = True
        self._proc.setProperty("gt7_stop_count", 1)
        # Windows: QProcess.terminate() posts WM_CLOSE to the process's
        # top-level windows. The frozen coach runs as GT7Coach.exe
        # --run-coach — windowed subsystem, no window, no message loop — so
        # nothing receives it and the process runs on until it's killed,
        # which the GUI then reports as a crash. The stop file is what it
        # actually watches for; terminate() still covers dev runs, where the
        # coach is a console process and gets a real signal.
        if self._stop_file is not None:
            try:
                self._stop_file.parent.mkdir(parents=True, exist_ok=True)
                self._stop_file.touch()
                log.info("stop requested via %s", self._stop_file)
            except OSError as exc:
                log.warning("could not write stop file %s: %s", self._stop_file, exc)
        log.info("sending terminate to subprocess pid=%s", self._proc.processId())
        self._proc.terminate()
        self.state_changed.emit("stopping")

    # ---- channel handlers --------------------------------------------------

    def _on_stdout(self) -> None:
        if self._proc is None:
            return
        data = bytes(self._proc.readAllStandardOutput()).decode("utf-8", errors="replace")
        self._stdout_buf += data
        while "\n" in self._stdout_buf:
            line, self._stdout_buf = self._stdout_buf.split("\n", 1)
            if line:
                self.stdout_line.emit(line)

    def _on_stderr(self) -> None:
        if self._proc is None:
            return
        data = bytes(self._proc.readAllStandardError()).decode("utf-8", errors="replace")
        self._stderr_buf += data
        while "\n" in self._stderr_buf:
            line, self._stderr_buf = self._stderr_buf.split("\n", 1)
            if line:
                self.stderr_line.emit(line)
                # Also stream to the GUI's own stderr so the operator's
                # terminal has a paper trail even if the GUI window
                # disappears (uncaught slot exception, native crash, etc).
                try:
                    if sys.stderr is not None:
                        sys.stderr.write(f"[coach] {line}\n")
                        sys.stderr.flush()
                except (OSError, ValueError):
                    # sys.stderr may be closed (PyInstaller windowed bundles
                    # set it to None, hence the guard above).
                    pass

    def _on_started(self) -> None:
        """QProcess.started — the OS has confirmed the spawn succeeded."""
        self.state_changed.emit("running")
        self.process_ready.emit()

    def _on_finished(self, exit_code: int, _exit_status) -> None:
        log.info("subprocess finished, exit_code=%d", exit_code)
        if self._stop_file is not None:
            self._stop_file.unlink(missing_ok=True)
        if self._stopping and exit_code != 0:
            # Killed on the user's own Stop click — report it as a clean exit
            # so MainWindow doesn't treat it as a failure.
            log.info("exit code %d after a requested stop; reporting clean", exit_code)
            exit_code = 0
        self._stopping = False
        self.state_changed.emit("stopped")
        self.exited.emit(exit_code)
        if self._status_file is not None:
            # Leave the status file on disk for one cycle so the GUI can
            # render any final events; the next start() truncates it.
            pass
        self._proc = None

    def _on_error(self, error) -> None:
        msg = self._proc.errorString() if self._proc is not None else str(error)
        try:
            error_name = error.name  # PySide6 enum -> human readable
        except AttributeError:
            error_name = str(error)
        if self._stopping:
            # Expected: a killed process reports QProcess.Crashed. The user
            # asked for this, so don't pop "Crashed: Process crashed" at them.
            log.info("QProcess %s during user-requested stop (expected)", error_name)
            if self._proc is not None and self._proc.state() == QProcess.ProcessState.NotRunning:
                self.state_changed.emit("stopped")
                self.exited.emit(0)
                self._proc = None
            return
        log.warning("QProcess error %s: %s", error_name, msg)
        # Tell MainWindow exactly what went wrong. Without this signal the
        # operator gets the status bar flickering from "running" to "stopped"
        # with no popup and no clue what to fix.
        self.start_failed.emit(f"{error_name}: {msg}")
        # On QProcess.FailedToStart we never get a `finished` signal, so
        # emit a synthetic exit so the UI returns to the idle state.
        if self._proc is not None and self._proc.state() == QProcess.ProcessState.NotRunning:
            self.state_changed.emit("stopped")
            self.exited.emit(-1)
            self._proc = None


def python_module_available(module: str) -> bool:
    """True iff ``python -m <module>`` would succeed.

    Used by the GUI before launching to give a clearer error than
    QProcess.FailedToStart when gt7coach isn't on sys.path.
    """
    return shutil.which(sys.executable) is not None and bool(
        __import__("importlib.util", fromlist=["find_spec"]).find_spec(module)
    )
