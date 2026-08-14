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
    from gt7coach.runtime import app_dir, is_frozen

    if is_frozen():
        return app_dir()
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


# Everything driving an in-flight download is held here for the duration.
# Without this the worker is owned only by a local in run_update_flow: that
# function returns the moment the thread starts, Python drops the last
# reference, and the C++ object is destroyed before it can emit anything. The
# dialog then sits at 0% forever — no progress, no finish, no error.
_ACTIVE_TRANSFERS: set[_Transfer] = set()


class _DownloadWorker(QObject):
    progress = Signal(int, int)  # bytes_read, total
    finished = Signal(Path)  # final staged path
    failed = Signal(str)

    def __init__(self, url: str, dest: Path) -> None:
        super().__init__()
        self._url = url
        self._dest = dest
        self._aborted = False

    def abort(self) -> None:
        """Ask the read loop to stop. Cancel previously did nothing at all —
        it hid the dialog and left the download running."""
        self._aborted = True

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
                        if self._aborted:
                            log.info("update download cancelled by the user")
                            return
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


class _Transfer(QObject):
    """Owns one in-flight download and updates the UI for it.

    A QObject created on the GUI thread, so Qt's automatic connection type
    sees a worker-thread sender and a GUI-thread receiver and queues the
    calls. The previous version connected the worker's signals to plain
    closures, which have no thread affinity — Qt therefore invoked them
    directly on the worker thread, touching QProgressDialog from outside the
    GUI thread. That is undefined behaviour: a segfault under X11 and, on
    Windows, a dialog frozen at 0% that never advances or closes.
    """

    def __init__(
        self,
        parent: QWidget,
        info: UpdateInfo,
        thread: QThread,
        worker: _DownloadWorker,
        dialog: QProgressDialog,
    ) -> None:
        super().__init__(parent)
        self._parent = parent
        self._info = info
        self.thread = thread
        self.worker = worker
        self.dialog = dialog
        _ACTIVE_TRANSFERS.add(self)

    def on_progress(self, read: int, total: int) -> None:
        if total > 0:
            self.dialog.setMaximum(total)
            self.dialog.setValue(read)
            self.dialog.setLabelText(
                f"Downloading update… {read / (1024 * 1024):.1f} / {total / (1024 * 1024):.1f} MB"
            )
        else:
            self.dialog.setLabelText(f"Downloading update… {read / 1024:.0f} KB")

    def on_finished(self, path: Path) -> None:
        self.release()
        _verify_and_spawn(self._parent, self._info, path)

    def on_failed(self, msg: str) -> None:
        self.release()
        QMessageBox.critical(
            self._parent,
            "Download failed",
            f"{msg}\n\nYou can download the new version by hand from the release page instead.",
        )

    def on_cancelled(self) -> None:
        # Cancel was never connected at all: it hid the dialog and left the
        # download running with nothing left to close it.
        self.worker.abort()
        self.release()

    def release(self) -> None:
        self.dialog.close()
        self.thread.quit()
        self.thread.wait(3000)
        _ACTIVE_TRANSFERS.discard(self)


def update_in_progress() -> bool:
    """True while a download is running — a second click must not start another."""
    return bool(_ACTIVE_TRANSFERS)


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

    if update_in_progress():
        QMessageBox.information(parent, "Update in progress", "The update is already downloading.")
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

    transfer = _Transfer(parent, info, thread, worker, progress)
    worker.progress.connect(transfer.on_progress)
    worker.finished.connect(transfer.on_finished)
    worker.failed.connect(transfer.on_failed)
    progress.canceled.connect(transfer.on_cancelled)
    thread.finished.connect(worker.deleteLater)
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
            # CREATE_NEW_CONSOLE, not DETACHED_PROCESS. The updater is built
            # with console=True so it can show progress and errors, but
            # DETACHED_PROCESS gives it no console at all — so when the swap
            # failed there was nothing on screen and the update just silently
            # did not happen.
            CREATE_NEW_CONSOLE = 0x00000010
            kwargs["creationflags"] = CREATE_NEW_CONSOLE
        subprocess.Popen(args, **kwargs)
    except Exception as exc:
        QMessageBox.critical(parent, "Could not start updater", str(exc))
        return

    # Tell the user, then quit so the updater can replace files.
    QMessageBox.information(
        parent,
        "Updating",
        "GT7 AI Coach will close now. A small updater window will swap in "
        "the new version and start it again.\n\n"
        "If nothing reopens within a minute, the updater log is at\n"
        "%TEMP%\\gt7coach-updater.log",
    )
    # Close the application window. Qt will fire MainWindow.closeEvent
    # which already stops the runner and tail.
    parent.close()
