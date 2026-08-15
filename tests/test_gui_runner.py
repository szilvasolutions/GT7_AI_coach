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
import tempfile
import textwrap
from pathlib import Path

import pytest

pytest.importorskip("PySide6", reason="PySide6 not installed; skipping GUI tests")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

from gt7coach.gui.runner import CoachOptions, CoachRunner


@pytest.fixture(scope="module")
def qapp():
    """One QApplication shared across all GUI tests in the session.

    QCoreApplication is not subclass-compatible with QApplication. If a
    QCoreApplication is created first and a later test asks for a
    QApplication, Qt aborts the process. Standardising on QApplication
    here lets the widget-level tests reuse the same instance.
    """
    app = QApplication.instance() or QApplication([])
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


# ---- Phase D.1: failure-visibility regressions ----------------------------


def test_start_failed_emitted_when_module_missing(qapp, monkeypatch) -> None:
    """If python_module_available rejects the launch, start_failed must fire
    with a useful reason and no QProcess must be created."""
    from gt7coach.gui import runner as runner_mod

    monkeypatch.setattr(runner_mod, "python_module_available", lambda _m: False)

    r = CoachRunner()
    reasons: list[str] = []
    r.start_failed.connect(reasons.append)

    r.start(CoachOptions(voice="null"))

    assert reasons, "start_failed should have fired"
    assert "gt7coach.main" in reasons[0]
    assert "pip install" in reasons[0]
    # No QProcess should have been spawned.
    assert r.is_running() is False


def test_state_running_only_after_qprocess_started(qapp, tmp_path, monkeypatch) -> None:
    """state_changed must reach 'running' via the QProcess.started slot,
    not synchronously inside start(). Regression for the bug where the
    GUI claimed 'running' even when QProcess immediately errored out."""
    from PySide6.QtCore import QProcess, QProcessEnvironment

    # A dummy subprocess that does a tiny stderr write then sleeps long
    # enough to confirm "running" has settled before we kill it.
    dummy = tmp_path / "dummy.py"
    dummy.write_text(
        "import sys, time; print('booting', file=sys.stderr, flush=True); time.sleep(0.5)",
        encoding="utf-8",
    )

    r = CoachRunner()
    states: list[str] = []
    ready_count = {"n": 0}
    r.state_changed.connect(states.append)
    r.process_ready.connect(lambda: ready_count.__setitem__("n", ready_count["n"] + 1))

    # Bypass module-availability + replace argv with our dummy script.
    monkeypatch.setattr("gt7coach.gui.runner.python_module_available", lambda _m: True)

    original_start = r.start

    def patched_start(opts):
        sf = tmp_path / "status.jsonl"
        r._status_file = sf
        env = QProcessEnvironment.systemEnvironment()
        env.insert("GT7COACH_STATUS_FILE", str(sf))
        r._proc = QProcess(r)
        r._proc.setProgram(sys.executable)
        r._proc.setArguments([str(dummy)])
        r._proc.setProcessEnvironment(env)
        r._proc.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        r._proc.readyReadStandardOutput.connect(r._on_stdout)
        r._proc.readyReadStandardError.connect(r._on_stderr)
        r._proc.finished.connect(r._on_finished)
        r._proc.errorOccurred.connect(r._on_error)
        r._proc.started.connect(r._on_started)
        r.state_changed.emit("starting")
        r._proc.start()

    monkeypatch.setattr(r, "start", patched_start)
    r.start(CoachOptions(voice="null"))

    # Wait for both 'running' and 'stopped' to land.
    _wait_for(lambda: "stopped" in states, timeout_ms=5000)

    # First two state transitions must be starting -> running (and nothing in
    # between). 'running' must NOT have been emitted from inside start().
    assert states[0] == "starting"
    assert states[1] == "running"
    assert ready_count["n"] == 1, "process_ready should fire exactly once"
    _ = original_start


def test_stderr_mirrored_to_sys_stderr(qapp, tmp_path, monkeypatch, capsys) -> None:
    """Every captured stderr line must also flow to sys.stderr with the
    [coach] prefix so the operator's terminal has a paper trail."""
    from PySide6.QtCore import QProcess, QProcessEnvironment

    dummy = tmp_path / "stderr_emitter.py"
    dummy.write_text(
        'import sys; print("HELLO FROM COACH", file=sys.stderr, flush=True)',
        encoding="utf-8",
    )

    r = CoachRunner()
    seen_signal: list[str] = []
    r.stderr_line.connect(seen_signal.append)

    monkeypatch.setattr("gt7coach.gui.runner.python_module_available", lambda _m: True)

    def patched_start(_opts):
        sf = tmp_path / "status.jsonl"
        r._status_file = sf
        env = QProcessEnvironment.systemEnvironment()
        env.insert("GT7COACH_STATUS_FILE", str(sf))
        r._proc = QProcess(r)
        r._proc.setProgram(sys.executable)
        r._proc.setArguments([str(dummy)])
        r._proc.setProcessEnvironment(env)
        r._proc.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        r._proc.readyReadStandardError.connect(r._on_stderr)
        r._proc.finished.connect(r._on_finished)
        r._proc.errorOccurred.connect(r._on_error)
        r._proc.started.connect(r._on_started)
        r._proc.start()

    monkeypatch.setattr(r, "start", patched_start)
    r.start(CoachOptions(voice="null"))
    _wait_for(lambda: bool(seen_signal), timeout_ms=5000)

    # The signal fired (LiveLog widget would have got it).
    assert any("HELLO FROM COACH" in line for line in seen_signal)
    # And sys.stderr saw a copy with the prefix.
    captured = capsys.readouterr().err
    assert "[coach] HELLO FROM COACH" in captured


def test_frozen_bundle_start_uses_run_coach_flag(qapp, monkeypatch) -> None:
    """In a PyInstaller bundle sys.executable IS the GUI exe, so start()
    must pass --run-coach instead of -m gt7coach.main. Regression for the
    v0.1.0 blocker where every Start in the shipped exe relaunched the GUI
    and died on its argparse."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)

    r = CoachRunner()
    r.start(CoachOptions(voice="null"))
    try:
        assert r._proc is not None
        args = list(r._proc.arguments())
        assert args[0] == "--run-coach"
        assert "-m" not in args
    finally:
        if r._proc is not None:
            r._proc.kill()
            r._proc.waitForFinished(2000)


def test_gui_main_dispatches_run_coach(monkeypatch) -> None:
    """gui.app.main(['--run-coach', ...]) must hand off to gt7coach.main
    before any Qt setup, returning its exit code verbatim."""
    import gt7coach.main as coach_mod
    from gt7coach.gui import app as app_mod

    seen: dict[str, list[str]] = {}

    def fake_coach_main(argv):
        seen["argv"] = argv
        return 42

    monkeypatch.setattr(coach_mod, "main", fake_coach_main)
    rc = app_mod.main(["--run-coach", "--voice", "null", "-v"])
    assert rc == 42
    assert seen["argv"] == ["--voice", "null", "-v"]


def test_stop_writes_the_stop_file(qapp, tmp_path, monkeypatch) -> None:
    """The frozen coach is a windowed process with no message loop, so
    QProcess.terminate() can't reach it. Stop must leave a stop file the
    coach polls for."""
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))

    r = CoachRunner()
    r.start(CoachOptions(voice="null"))
    try:
        assert r._stop_file is not None
        assert not r._stop_file.exists()
        r.stop()
        assert r._stop_file.exists(), "stop() must create the stop file"
    finally:
        if r._proc is not None:
            r._proc.kill()
            r._proc.waitForFinished(2000)


def test_user_stop_is_not_reported_as_a_crash(qapp, tmp_path, monkeypatch) -> None:
    """Killing the process makes QProcess report Crashed. After a Stop click
    that's expected, and used to pop 'Crashed: Process crashed' at the user."""
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))

    r = CoachRunner()
    failures: list[str] = []
    exits: list[int] = []
    r.start_failed.connect(failures.append)
    r.exited.connect(exits.append)

    r.start(CoachOptions(voice="null"))
    assert r._proc is not None
    r._proc.waitForStarted(3000)
    r.stop()  # arms _stopping
    r._proc.kill()  # what the second Stop / a stubborn process leads to
    r._proc.waitForFinished(3000)
    qapp.processEvents()

    assert not failures, f"a requested stop must not raise a failure dialog: {failures}"
    assert exits and exits[-1] == 0, f"expected a clean exit code, got {exits}"


def test_frozen_detection_covers_nuitka_not_just_pyinstaller(monkeypatch):
    """Plan B swaps PyInstaller for Nuitka, and Nuitka sets neither
    sys.frozen nor sys._MEIPASS — it injects __compiled__ instead. Testing
    for PyInstaller alone would have made every Start click relaunch the GUI
    with '-m gt7coach.main', which is exactly how v0.1.0 was broken."""
    import gt7coach.runtime as rt

    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    assert rt.is_frozen() is False
    assert rt.packager() == "source"

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert rt.is_frozen() is True
    assert rt.packager() == "pyinstaller"

    # Nuitka's marker lives in the module globals, not on sys.
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setitem(rt.__dict__, "__compiled__", object())
    assert rt.is_frozen() is True, "a Nuitka build must count as frozen"
    assert rt.packager() == "nuitka"


def test_a_nuitka_build_passes_run_coach_not_dash_m(qapp, monkeypatch, tmp_path):
    """The regression this guards: under Nuitka the runner must still use
    --run-coach, or the GUI relaunches itself and dies on its own argparse."""
    import gt7coach.runtime as rt

    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    monkeypatch.setitem(rt.__dict__, "__compiled__", object())

    r = CoachRunner()
    r.start(CoachOptions(voice="null"))
    try:
        assert r._proc is not None
        args = list(r._proc.arguments())
        assert args[0] == "--run-coach", f"Nuitka build must dispatch in-process, got {args[:2]}"
        assert "-m" not in args
    finally:
        if r._proc is not None:
            r._proc.kill()
            r._proc.waitForFinished(2000)


def test_unset_options_are_left_to_the_config_file() -> None:
    """The GUI must not send settings it has no widget for.

    CoachOptions used to default to the CLI's own defaults and pass them on
    every launch, so config.yaml's cooldown was overridden by --cooldown 4.0
    on every single Start and the tuned value never took effect.
    """
    argv = CoachOptions().to_argv()
    assert "--cooldown" not in argv
    assert "--voice" not in argv
    assert "--voice-rate" not in argv
    assert "--driver-style" not in argv


def test_options_the_user_actually_set_are_still_passed() -> None:
    argv = CoachOptions(
        voice="null", voice_rate=210, driver_style="learning", cooldown=3.3
    ).to_argv()
    assert argv[argv.index("--cooldown") + 1] == "3.3"
    assert argv[argv.index("--voice") + 1] == "null"
    assert argv[argv.index("--voice-rate") + 1] == "210"
    assert argv[argv.index("--driver-style") + 1] == "learning"
