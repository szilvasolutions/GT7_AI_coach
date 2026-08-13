"""The update download, driven end to end against a real local HTTP server.

Reported live: on v0.1.5 the progress dialog opened at 0%, never advanced,
never closed, and nothing installed — surviving a restart. The worker was
created with no Qt parent and held only by a local in run_update_flow(); that
function returns as soon as the thread starts, Python dropped the last
reference and the C++ object was destroyed before it could emit anything.

No unit test could catch that, because every piece was individually correct.
This drives the actual flow.
"""

from __future__ import annotations

import gc
import http.server
import os
import threading

import pytest

pytest.importorskip("PySide6", reason="PySide6 not installed; skipping GUI tests")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QObject, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox, QWidget

from gt7coach.gui import self_update
from gt7coach.gui.updater import UpdateInfo


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def server(tmp_path_factory):
    """Serve a payload big enough to need several chunks."""
    root = tmp_path_factory.mktemp("srv")
    (root / "GT7Coach-v9.9.9-win64.zip").write_bytes(b"x" * (400 * 1024))

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(root), **kw)

        def log_message(self, *a):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/GT7Coach-v9.9.9-win64.zip"
    httpd.shutdown()


def _pump(predicate, timeout_ms=15000):
    """Spin the Qt event loop until predicate() or timeout."""
    loop = QEventLoop()
    elapsed = {"ms": 0}

    def tick():
        elapsed["ms"] += 50
        if predicate() or elapsed["ms"] >= timeout_ms:
            loop.quit()

    t = QTimer()
    t.timeout.connect(tick)
    t.start(50)
    loop.exec()
    t.stop()
    return predicate()


def test_download_actually_progresses_and_completes(qapp, server, tmp_path, monkeypatch):
    """The regression: dialog opens, sits at 0%, never finishes."""
    staged = tmp_path / "staged.zip"
    monkeypatch.setattr(self_update, "_staging_path", lambda name: staged)
    monkeypatch.setattr(self_update, "can_self_update", lambda: True)
    # Stop the flow once the file is down; installing is a separate concern.
    spawned: list = []
    monkeypatch.setattr(
        self_update, "_verify_and_spawn", lambda parent, info, path: spawned.append(path)
    )

    parent = QWidget()
    info = UpdateInfo(tag="v9.9.9", name="test", body="", html_url="https://x", zip_url=server)
    self_update.run_update_flow(parent, info)

    # Observe progress through a QObject slot, never a plain function: a
    # plain receiver has no thread affinity, so Qt would invoke it directly
    # on the worker thread and crash the same way the bug did.
    seen: list[int] = []

    class _Spy(QObject):
        def record(self, read: int, total: int) -> None:
            seen.append(read)

    spy = _Spy()
    for transfer in list(self_update._ACTIVE_TRANSFERS):
        transfer.worker.progress.connect(spy.record)

    # The exact conditions of the bug: the function has returned, so anything
    # held only by a local is now collectable.
    gc.collect()

    assert _pump(lambda: bool(spawned)), "download never completed — the 0% hang"
    # The reported symptom was specifically a dialog stuck at 0%, so assert
    # the bar actually moved rather than only that the file arrived.
    assert seen, "no progress was ever reported — the dialog would sit at 0%"
    assert max(seen) > 0
    assert staged.exists()
    assert staged.stat().st_size == 400 * 1024
    assert not self_update.update_in_progress(), "transfer was not released"


def test_a_second_click_does_not_start_a_second_download(qapp, server, tmp_path, monkeypatch):
    staged = tmp_path / "staged2.zip"
    monkeypatch.setattr(self_update, "_staging_path", lambda name: staged)
    monkeypatch.setattr(self_update, "can_self_update", lambda: True)
    monkeypatch.setattr(self_update, "_verify_and_spawn", lambda *a: None)
    shown: list = []
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: shown.append(a)))

    parent = QWidget()
    info = UpdateInfo(tag="v9.9.9", name="test", body="", html_url="https://x", zip_url=server)
    self_update.run_update_flow(parent, info)
    in_flight = self_update.update_in_progress()
    self_update.run_update_flow(parent, info)  # user clicks again
    _pump(lambda: not self_update.update_in_progress())

    if in_flight:
        assert shown, "a second click should say it's already downloading"


def test_a_dead_url_reports_instead_of_hanging(qapp, tmp_path, monkeypatch):
    """A failure must close the dialog, not leave it at 0% forever."""
    monkeypatch.setattr(self_update, "_staging_path", lambda name: tmp_path / "never.zip")
    monkeypatch.setattr(self_update, "can_self_update", lambda: True)
    monkeypatch.setattr(self_update, "_verify_and_spawn", lambda *a: None)
    errors: list = []
    monkeypatch.setattr(QMessageBox, "critical", staticmethod(lambda *a, **k: errors.append(a)))

    parent = QWidget()
    info = UpdateInfo(
        tag="v9.9.9",
        name="test",
        body="",
        html_url="https://x",
        zip_url="http://127.0.0.1:9/nope.zip",
    )
    self_update.run_update_flow(parent, info)
    gc.collect()

    assert _pump(lambda: bool(errors)), "a dead URL must surface an error"
    assert not self_update.update_in_progress()
