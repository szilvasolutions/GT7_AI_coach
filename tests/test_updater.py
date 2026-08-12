"""Tests for ``gt7coach.gui.updater``: pure functions only.

We don't hit the GitHub API in tests — that's flaky and rate-limited
(and would couple CI to upstream availability). The Qt-thread plumbing
in ``UpdateChecker`` is exercised by a hand-driven worker test that
patches ``urllib.request.urlopen`` to return canned bytes.
"""

from __future__ import annotations

import io
import json
import os
import sys
import time

import pytest

pytest.importorskip("PySide6", reason="PySide6 not installed; skipping GUI tests")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from gt7coach.gui import updater as up


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


# ---- pure version comparison -----------------------------------------------


@pytest.mark.parametrize(
    "remote, current, expected_newer",
    [
        ("v0.2.0", "0.1.0a0", True),
        ("0.2.0", "0.1.0a0", True),
        ("v0.1.1", "0.1.0a0", True),
        ("v0.1.0", "0.1.0a0", False),  # post-release equal — not newer
        ("v0.0.9", "0.1.0a0", False),
        ("v1.0.0", "0.99.99", True),
        ("v0.1.0a1", "v0.1.0a0", False),  # parser drops alpha suffix in tuple
        ("nonsense", "0.1.0", False),  # unparseable -> (0,) -> not newer
    ],
)
def test_is_newer(remote: str, current: str, expected_newer: bool) -> None:
    assert up.is_newer(remote, current) is expected_newer


# ---- cache file location ---------------------------------------------------


def test_cache_dir_uses_localappdata_on_windows(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(up.sys, "platform", "win32", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    expected = tmp_path / "gt7coach"
    assert up._cache_dir() == expected


def test_cache_dir_uses_xdg_on_linux(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(up.sys, "platform", "linux", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert up._cache_dir() == tmp_path / "gt7coach"


# ---- worker / cache flow ---------------------------------------------------


def _fake_release(tag: str = "v9.9.9", with_win_asset: bool = True) -> dict:
    assets = []
    if with_win_asset:
        assets.append(
            {
                "name": f"GT7Coach-{tag}-win64.zip",
                "browser_download_url": f"https://example.com/dl/{tag}/GT7Coach-{tag}-win64.zip",
            }
        )
    return {
        "tag_name": tag,
        "name": f"Release {tag}",
        "body": "## What's new\n- Speedier coaching.",
        "html_url": f"https://github.com/szilvasolutions/GT7_AI_coach/releases/tag/{tag}",
        "assets": assets,
    }


def test_worker_emits_available_on_new_release(qapp, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(up, "_cache_dir", lambda: tmp_path / "gt7coach")
    monkeypatch.setattr(up.platform, "system", lambda: "Windows")

    payload = _fake_release(tag="v9.9.9")
    fake_resp = io.BytesIO(json.dumps(payload).encode())

    def fake_urlopen(req, timeout=None):
        class _Ctx:
            def __enter__(self):
                return fake_resp

            def __exit__(self, *_a):
                return None

        return _Ctx()

    monkeypatch.setattr(up.urllib.request, "urlopen", fake_urlopen)

    worker = up._CheckWorker(force=True)
    received: list = []
    worker.finished_available.connect(received.append)
    worker.finished_none.connect(lambda: received.append("NONE"))
    worker.finished_error.connect(lambda msg: received.append(("ERR", msg)))

    worker.run()

    assert len(received) == 1
    info = received[0]
    assert info.tag == "v9.9.9"
    assert info.zip_url and info.zip_url.endswith("win64.zip")
    # Cache populated
    cache = json.loads((tmp_path / "gt7coach" / "update.json").read_text())
    assert cache["tag"] == "v9.9.9"
    assert cache["update_available"] is True


def test_worker_uses_cache_within_interval(qapp, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(up, "_cache_dir", lambda: tmp_path / "gt7coach")
    cache_dir = tmp_path / "gt7coach"
    cache_dir.mkdir()
    (cache_dir / "update.json").write_text(
        json.dumps(
            {
                "checked_at": time.time() - 60,  # 1 minute ago — well within 6h window
                "update_available": True,
                "tag": "v7.7.7",
                "name": "Cached release",
                "body": "",
                "html_url": "https://example.com",
                "zip_url": "https://example.com/x.zip",
            }
        )
    )

    def boom(*_a, **_kw):
        raise AssertionError("network must not be hit when cache is fresh")

    monkeypatch.setattr(up.urllib.request, "urlopen", boom)

    worker = up._CheckWorker(force=False)
    received: list = []
    worker.finished_available.connect(received.append)
    worker.finished_none.connect(lambda: received.append("NONE"))
    worker.run()
    assert len(received) == 1
    assert received[0].tag == "v7.7.7"


def test_worker_emits_none_when_remote_not_newer(qapp, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(up, "_cache_dir", lambda: tmp_path / "gt7coach")
    monkeypatch.setattr(up.platform, "system", lambda: "Windows")

    # Remote tag matches current version exactly.
    payload = _fake_release(tag=f"v{up.CURRENT_VERSION}", with_win_asset=True)
    fake_resp = io.BytesIO(json.dumps(payload).encode())

    def fake_urlopen(req, timeout=None):
        class _Ctx:
            def __enter__(self):
                return fake_resp

            def __exit__(self, *_a):
                return None

        return _Ctx()

    monkeypatch.setattr(up.urllib.request, "urlopen", fake_urlopen)

    worker = up._CheckWorker(force=True)
    received: list = []
    worker.finished_available.connect(lambda info: received.append(("AVAIL", info)))
    worker.finished_none.connect(lambda: received.append("NONE"))
    worker.run()
    assert received == ["NONE"]


def test_worker_emits_error_on_network_failure(qapp, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(up, "_cache_dir", lambda: tmp_path / "gt7coach")

    def fail_urlopen(*_a, **_kw):
        raise up.urllib.error.URLError("offline")

    monkeypatch.setattr(up.urllib.request, "urlopen", fail_urlopen)

    worker = up._CheckWorker(force=True)
    received: list = []
    worker.finished_error.connect(received.append)
    worker.finished_available.connect(lambda _i: received.append("AVAIL"))
    worker.run()
    assert len(received) == 1
    # The worker swallows the URLError and emits a generic "network or API error"
    # rather than the raw exception. Either way it must NOT have raised.
    assert isinstance(received[0], str)


# --- can_self_update: folder bundle vs single-file exe ----------------------


def test_can_self_update_false_when_not_frozen(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    assert up.can_self_update() is False


def test_can_self_update_true_for_folder_bundle(monkeypatch, tmp_path):
    """The win64.zip layout ships updater.exe next to GT7Coach.exe."""
    (tmp_path / "GT7Coach.exe").write_bytes(b"stub")
    (tmp_path / "updater.exe").write_bytes(b"stub")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "GT7Coach.exe"))
    assert up.can_self_update() is True


def test_can_self_update_false_for_single_file_exe(monkeypatch, tmp_path):
    """Regression: the one-file build is frozen but has no updater.exe, and
    used to offer an update that failed only after a ~96 MB download."""
    (tmp_path / "GT7Coach.exe").write_bytes(b"stub")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", "/tmp/_MEI123456", raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "GT7Coach.exe"))
    assert up.is_frozen_bundle() is True
    assert up.can_self_update() is False


def test_release_with_both_assets_still_picks_the_zip(monkeypatch):
    """Since v0.1.1 a release carries GT7Coach.exe as well as the zip; the
    updater must keep choosing the zip."""
    release = {
        "assets": [
            {"name": "GT7Coach.exe", "browser_download_url": "https://x/GT7Coach.exe"},
            {
                "name": "GT7Coach-v9.9.9-win64.zip",
                "browser_download_url": "https://x/GT7Coach-v9.9.9-win64.zip",
            },
            {"name": "SHA256SUMS.txt", "browser_download_url": "https://x/SHA256SUMS.txt"},
        ]
    }
    monkeypatch.setattr(up.platform, "system", lambda: "Windows")
    assert up._pick_zip_asset(release) == "https://x/GT7Coach-v9.9.9-win64.zip"
