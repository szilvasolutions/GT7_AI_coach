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

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._proc: QProcess | None = None
        self._status_file: Path | None = None
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

        # Allocate a fresh status file under the user's temp dir. Phase B's
        # status emitter truncates it on first emit, so stale content is OK.
        tmpdir = Path(tempfile.gettempdir()) / "gt7coach-gui"
        tmpdir.mkdir(parents=True, exist_ok=True)
        self._status_file = tmpdir / f"status-{os.getpid()}.jsonl"

        env = QProcessEnvironment.systemEnvironment()
        env.insert("GT7COACH_STATUS_FILE", str(self._status_file))

        self._proc = QProcess(self)
        self._proc.setProgram(sys.executable)
        # Use the module entry point so the subprocess works whether the user
        # installed via `pip install -e .` or from a frozen bundle that
        # already exposes the gt7coach package on sys.path.
        self._proc.setArguments(["-m", "gt7coach.main", *options.to_argv()])
        self._proc.setProcessEnvironment(env)
        self._proc.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        self._proc.readyReadStandardOutput.connect(self._on_stdout)
        self._proc.readyReadStandardError.connect(self._on_stderr)
        self._proc.finished.connect(self._on_finished)
        self._proc.errorOccurred.connect(self._on_error)

        log.info("starting gt7coach-coach with argv=%r", options.to_argv())
        self.state_changed.emit("starting")
        # On Windows, CREATE_NEW_PROCESS_GROUP is needed if we want to send
        # CTRL_BREAK_EVENT later. Qt doesn't expose that flag directly; we
        # send SIGINT via QProcess.terminate() which maps to the right OS
        # signal on each platform, and the existing main.py shutdown handler
        # (commit 3937e8d) treats a second SIGINT as force-quit.
        self._proc.start()
        self.state_changed.emit("running")

    def stop(self) -> None:
        """Send a graceful stop signal. Second call force-kills."""
        if not self.is_running():
            return
        assert self._proc is not None
        # First stop: gentle. main.py's signal handler drains advisor +
        # voice in the finally block. Second stop: kill.
        if self._proc.property("gt7_stop_count"):
            log.warning("second stop request — killing subprocess")
            self._proc.kill()
            self.state_changed.emit("stopping")
            return
        log.info("sending terminate to subprocess pid=%s", self._proc.processId())
        self._proc.setProperty("gt7_stop_count", 1)
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

    def _on_finished(self, exit_code: int, _exit_status) -> None:
        log.info("subprocess finished, exit_code=%d", exit_code)
        self.state_changed.emit("stopped")
        self.exited.emit(exit_code)
        if self._status_file is not None:
            # Leave the status file on disk for one cycle so the GUI can
            # render any final events; the next start() truncates it.
            pass
        self._proc = None

    def _on_error(self, error) -> None:
        log.warning("subprocess error: %s", error)
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
