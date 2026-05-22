"""Tests for ``gt7coach.gui.runner.CoachRunner``.

These tests exercise the QProcess lifecycle without needing PS5
hardware or a real ``gt7coach-coach`` invocation — we substitute a
tiny throwaway Python script that prints a few lines, then exits.

Qt headless: we set ``QT_QPA_PLATFORM=offscreen`` so no display is
required (works on CI / dev sandboxes). PySide6 must be installed
(via the project's ``[gui]`` extra) for these tests to run.
"""

from __future__ import annotations

import os
import sys
import textwrap
from pathlib import Path

import pytest

pytest.importorskip("PySide6", reason="PySide6 not installed; skipping GUI tests")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer

from gt7coach.gui.runner import CoachOptions, CoachRunner


@pytest.fixture(scope="module")
def qapp():
    """One QCoreApplication for the whole test module."""
    app = QCoreApplication.instance() or QCoreApplication([])
    yield app


def _spin(ms: int) -> None:
    """Run the Qt event loop for ``ms`` milliseconds, then return."""
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def _wait_for(predicate, timeout_ms: int = 3000, poll_ms: int = 50) -> bool:
    """Spin the event loop until ``predicate()`` is truthy or timeout."""
    elapsed = 0
    while elapsed < timeout_ms:
        if predicate():
            return True
        _spin(poll_ms)
        elapsed += poll_ms
    return predicate()


def test_options_to_argv_minimal() -> None:
    opts = CoachOptions(voice="null", voice_rate=200, driver_style="smooth")
    argv = opts.to_argv()
    assert "--voice" in argv and "null" in argv
    assert "--voice-rate" in argv and "200" in argv
    assert "--driver-style" in argv and "smooth" in argv
    # provider/api-key/car-class/track are omitted when blank
    assert "--provider" not in argv
    assert "--api-key" not in argv
    assert "--car-class" not in argv
    assert "--track" not in argv


def test_options_to_argv_full() -> None:
    opts = CoachOptions(
        provider="gemini",
        api_key="k123",
        voice="pyttsx3",
        voice_rate=230,
        driver_style="aggressive",
        car_class="Gr.3 RWD",
        track="deep_forest",
        cooldown=2.5,
        no_summary=True,
        verbose=True,
        extra_args=["--no-log"],
    )
    argv = opts.to_argv()
    assert argv.count("--provider") == 1
    assert "gemini" in argv
    assert "--api-key" in argv and "k123" in argv
    assert "--car-class" in argv and "Gr.3 RWD" in argv
    assert "--track" in argv and "deep_forest" in argv
    assert "--cooldown" in argv and "2.5" in argv
    assert "--no-summary" in argv
    assert "-v" in argv
    assert "--no-log" in argv


def test_runner_lifecycle_with_dummy_subprocess(qapp, tmp_path: Path, monkeypatch) -> None:
    """Replace the gt7coach.main entry point with a tiny script that
    writes one status event + a stderr line, then exits cleanly."""
    dummy = tmp_path / "fake_main.py"
    dummy.write_text(
        textwrap.dedent(
            """
            import os, sys, json, time
            sf = os.environ.get("GT7COACH_STATUS_FILE")
            if sf:
                with open(sf, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"ts": time.time(), "type": "track", "name": "Deep Forest"}) + "\\n")
            print("hello stdout", flush=True)
            print("INFO gt7coach.coach: corner #1 detected", file=sys.stderr, flush=True)
            sys.exit(0)
            """
        ),
        encoding="utf-8",
    )

    # Patch the subprocess to point at our dummy script. Easiest way is to
    # subclass CoachRunner and override start() — but we can also just call
    # start() with a CoachOptions whose extra_args swap the module. We do it
    # the explicit way for clarity:
    runner = CoachRunner()
    stderr_lines: list[str] = []
    stdout_lines: list[str] = []
    states: list[str] = []
    exit_codes: list[int] = []

    runner.stderr_line.connect(stderr_lines.append)
    runner.stdout_line.connect(stdout_lines.append)
    runner.state_changed.connect(states.append)
    runner.exited.connect(exit_codes.append)

    # Replace start()'s argv with the dummy script. Smallest patch is to
    # intercept the QProcess.setArguments call via monkeypatching CoachRunner
    # at the instance level. We do that by overriding the internal method.
    original_start = runner.start

    def patched_start(opts):
        # Bypass module-based invocation; spawn the dummy script directly.
        from PySide6.QtCore import QProcess, QProcessEnvironment

        runner._stdout_buf = ""
        runner._stderr_buf = ""
        sf = tmp_path / "status.jsonl"
        runner._status_file = sf
        env = QProcessEnvironment.systemEnvironment()
        env.insert("GT7COACH_STATUS_FILE", str(sf))
        runner._proc = QProcess(runner)
        runner._proc.setProgram(sys.executable)
        runner._proc.setArguments([str(dummy)])
        runner._proc.setProcessEnvironment(env)
        runner._proc.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        runner._proc.readyReadStandardOutput.connect(runner._on_stdout)
        runner._proc.readyReadStandardError.connect(runner._on_stderr)
        runner._proc.finished.connect(runner._on_finished)
        runner._proc.errorOccurred.connect(runner._on_error)
        runner.state_changed.emit("starting")
        runner._proc.start()
        runner.state_changed.emit("running")

    monkeypatch.setattr(runner, "start", patched_start)

    runner.start(CoachOptions(voice="null"))
    assert _wait_for(lambda: exit_codes, timeout_ms=5000), "subprocess never exited"

    assert exit_codes[0] == 0
    assert "stopped" in states
    # stdout / stderr came through
    assert any("hello stdout" in line for line in stdout_lines)
    assert any("corner #1 detected" in line for line in stderr_lines)
    # Status file got the track event
    status_text = (tmp_path / "status.jsonl").read_text(encoding="utf-8").strip()
    assert "Deep Forest" in status_text

    _ = original_start  # silence unused-var lint
