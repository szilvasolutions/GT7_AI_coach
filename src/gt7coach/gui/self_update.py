"""GUI-side hook that downloads the staged update and spawns updater.exe.

This module is import-time safe: it never touches the network or
spawns any process until ``run_update_flow()`` is explicitly called.
The Phase D installer (PyInstaller bundle) ships ``updater.exe``
alongside ``GT7Coach.exe``; for non-frozen (pip) installs the GUI
disables the Download & install button before we ever get here.
"""

from __future__ import annotations

import hashlib
import logging
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal
from PySide6.QtWidgets import (
    QMessageBox,
    QProgressDialog,
    QWidget,
)

from gt7coach.gui.updater import GITHUB_REPO, USER_AGENT, UpdateInfo, can_self_update

log = logging.getLogger(__name__)


def _staging_path(filename: str) -> Path:
    """Where to place the downloaded zip. Lives outside the install dir
    so the swap can move the install dir out from under itself."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home())
        return base / "gt7coach" / "staging" / filename
    return Path(tempfile.gettempdir()) / "gt7coach-staging" / filename


def install_dir() -> Path:
    """Best-guess for the current install directory (the folder
    containing ``GT7Coach.exe`` / the frozen interpreter)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    # In dev (pip install -e .) we shouldn't be calling self-update at all.
    return Path.cwd()


def fetch_sha256sums(release_tag: str, timeout_s: float = 5.0) -> dict[str, str]:
    """Download the SHA256SUMS.txt asset for a given release tag and
    return a ``{filename: hex_digest}`` mapping. Empty dict on failure.

    File format (one line per asset):
        <hex sha-256>  *<asset filename>
    """
    url = f"https://github.com/{GITHUB_REPO}/releases/download/{release_tag}/SHA256SUMS.txt"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            text = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
        log.warning("could not fetch SHA256SUMS.txt for %s: %s", release_tag, exc)
        return {}
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # "hex *filename" or "hex  filename"
        parts = line.replace("*", " ").split()
        if len(parts) >= 2:
            out[parts[-1]] = parts[0].lower()
    return out


class _DownloadWorker(QObject):
    progress = Signal(int, int)  # bytes_read, total
    finished = Signal(Path)  # final staged path
    failed = Signal(str)

    def __init__(self, url: str, dest: Path) -> None:
        super().__init__()
        self._url = url
        self._dest = dest

    def run(self) -> None:
        try:
            self._dest.parent.mkdir(parents=True, exist_ok=True)
            req = urllib.request.Request(self._url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=15.0) as resp:
                total_raw = resp.headers.get("Content-Length")
                total = int(total_raw) if total_raw else 0
                read = 0
                with self._dest.open("wb") as f:
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        f.write(chunk)
                        read += len(chunk)
                        self.progress.emit(read, total)
            self.finished.emit(self._dest)
        except Exception as exc:  # pragma: no cover — network paths
            log.warning("download failed: %s", exc)
            self.failed.emit(str(exc))


def run_update_flow(parent: QWidget, info: UpdateInfo) -> None:
    """Drive the full download → verify → spawn-updater → quit flow.

    Pre-conditions enforced by the caller: we're running from a frozen
    bundle, and ``info.zip_url`` is non-empty.
    """
    if not info.zip_url:
        QMessageBox.critical(parent, "No download URL", "This release has no Windows zip asset.")
        return

    # Check the swap machinery exists BEFORE spending a ~96 MB download on
    # an update we can't apply (the one-file build has no updater.exe).
    if not can_self_update():
        QMessageBox.information(
            parent,
            "Can't update in place",
            "This is the single-file GT7Coach.exe, which can't replace itself "
            "while it's running.\n\nDownload the new version from the release "
            "page, or switch to the win64.zip build — that one updates itself.",
        )
        return

    zip_name = Path(info.zip_url).name
    dest = _staging_path(zip_name)

    progress = QProgressDialog("Downloading update…", "Cancel", 0, 100, parent)
    progress.setWindowTitle("GT7 AI Coach — update")
    progress.setMinimumDuration(0)
    progress.setAutoReset(False)
    progress.setAutoClose(False)
    progress.setValue(0)

    thread = QThread(parent)
    worker = _DownloadWorker(info.zip_url, dest)
    worker.moveToThread(thread)
    thread.started.connect(worker.run)

    def _on_progress(read: int, total: int) -> None:
        if total > 0:
            progress.setMaximum(total)
            progress.setValue(read)
            mb_read = read / (1024 * 1024)
            mb_total = total / (1024 * 1024)
            progress.setLabelText(f"Downloading update… {mb_read:.1f} / {mb_total:.1f} MB")
        else:
            progress.setLabelText(f"Downloading update… {read / 1024:.0f} KB")

    def _on_finished(path: Path) -> None:
        progress.close()
        thread.quit()
        _verify_and_spawn(parent, info, path)

    def _on_failed(msg: str) -> None:
        progress.close()
        thread.quit()
        QMessageBox.critical(parent, "Download failed", msg)

    worker.progress.connect(_on_progress)
    worker.finished.connect(_on_finished)
    worker.failed.connect(_on_failed)
    thread.finished.connect(worker.deleteLater)
    thread.finished.connect(thread.deleteLater)
    thread.start()


def _verify_and_spawn(parent: QWidget, info: UpdateInfo, zip_path: Path) -> None:
    """Check SHA-256, then launch updater.exe and quit the GUI."""
    expected: str | None = None
    sums = fetch_sha256sums(info.tag)
    if sums:
        zip_name = Path(info.zip_url or "").name
        expected = sums.get(zip_name)

    if expected:
        h = hashlib.sha256()
        with zip_path.open("rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        actual = h.hexdigest().lower()
        if actual != expected.lower():
            QMessageBox.critical(
                parent,
                "Checksum mismatch",
                f"The downloaded zip's SHA-256 does not match the release's published sum.\n\n"
                f"Expected: {expected}\nActual:   {actual}\n\n"
                "Aborting update for safety.",
            )
            return

    # Locate updater.exe alongside our exe.
    here = install_dir()
    updater_exe = here / "updater.exe"
    if not updater_exe.is_file():
        QMessageBox.critical(
            parent,
            "Updater missing",
            f"Couldn't find updater.exe alongside the running GT7Coach.exe ({here}).\n"
            "Re-download the latest release manually.",
        )
        return

    pid = os.getpid()
    args = [
        str(updater_exe),
        "--pid",
        str(pid),
        "--zip",
        str(zip_path),
        "--install-dir",
        str(here),
    ]
    if expected:
        args += ["--sha256", expected]

    log.info("spawning updater: %r", args)
    try:
        kwargs: dict = {"close_fds": True}
        if sys.platform == "win32":
            DETACHED_PROCESS = 0x00000008
            kwargs["creationflags"] = DETACHED_PROCESS
        subprocess.Popen(args, **kwargs)
    except Exception as exc:
        QMessageBox.critical(parent, "Could not start updater", str(exc))
        return

    # Tell the user, then quit so the updater can replace files.
    QMessageBox.information(
        parent,
        "Updating",
        "The updater is running in the background.\n"
        "GT7 AI Coach will close now and relaunch automatically once "
        "the new version is installed.",
    )
    # Close the application window. Qt will fire MainWindow.closeEvent
    # which already stops the runner and tail.
    parent.close()
