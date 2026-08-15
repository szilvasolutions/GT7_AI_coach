"""GitHub Releases update checker for the GUI.

Behaviour:

* Once per GUI process (and at most once per 6 h wall-clock, tracked
  in a tiny JSON cache in the user's AppData / XDG cache dir), call
  the public GitHub Releases API for the latest release.
* Compare the returned ``tag_name`` against ``gt7coach.__version__``.
* If newer, emit :attr:`UpdateChecker.update_available` with a
  :class:`UpdateInfo` payload. The MainWindow shows a non-blocking
  banner with two buttons:
    - **View release** opens the release page in the default browser.
    - **Download & install** triggers the Phase D self-updater
      (greyed out for pip-installed users — they're told to run
      ``git pull && pip install -e .``).
* All HTTP and file I/O happens on a daemon ``QThread`` so the UI
  is never blocked.

No new runtime dependencies — uses ``urllib`` from stdlib for the
single GET. The cache lives at:

* Windows: ``%LOCALAPPDATA%/gt7coach/update.json``
* macOS:   ``~/Library/Caches/gt7coach/update.json``
* Linux:   ``$XDG_CACHE_HOME/gt7coach/update.json`` (or ``~/.cache``)
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal

from gt7coach import __version__ as CURRENT_VERSION

log = logging.getLogger(__name__)

GITHUB_REPO = "szilvasolutions/GT7_AI_coach"
RELEASES_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
USER_AGENT = f"gt7coach/{CURRENT_VERSION} (PySide6 GUI; +https://github.com/{GITHUB_REPO})"
CACHE_INTERVAL_S = 6 * 60 * 60  # 6 hours


@dataclass(frozen=True)
class UpdateInfo:
    """Result of a successful update check that found a newer release."""

    tag: str  # e.g. "v0.2.0"
    name: str  # human-readable release title
    body: str  # release notes (markdown)
    html_url: str  # web page URL
    zip_url: str | None  # browser_download_url of the .zip asset (None if not found)


def _cache_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return Path(base) / "gt7coach"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "gt7coach"
    xdg = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(xdg) / "gt7coach"


def _cache_path() -> Path:
    return _cache_dir() / "update.json"


def is_frozen_bundle() -> bool:
    """True if running from a PyInstaller bundle (vs. pip install).

    Used to decide whether the 'Download & install' button is enabled
    (Phase D wires the self-updater for bundle installs only).
    """
    from gt7coach.runtime import is_frozen

    return is_frozen()


def can_self_update() -> bool:
    """True if this install can actually apply an update in place.

    Being frozen isn't enough. The swap is performed by ``updater.exe``,
    which only ships in the single-folder bundle — the single-file
    ``GT7Coach.exe`` has nothing beside it, and there is no folder to
    unpack a new build into. Gate the Download button on the machinery
    being present rather than on ``sys.frozen``, otherwise the one-file
    build offers an update, downloads ~96 MB and only then admits it
    can't install it.
    """
    if not is_frozen_bundle():
        return False
    from gt7coach.runtime import app_dir

    return (app_dir() / "updater.exe").is_file()


def _parse_version(s: str) -> tuple[int, ...]:
    """Best-effort version parser. Strips a leading ``v``, splits on
    ``.``-``-``-``+`` and converts the leading numeric prefix of each
    segment to int. ``v0.2.0`` → ``(0, 2, 0)``. ``0.1.0a0`` →
    ``(0, 1, 0)`` (the ``a0`` suffix is dropped from the tuple but
    handled by the post-release comparison)."""
    s = s.strip().lstrip("vV")
    parts = re.split(r"[.\-+]", s)
    nums: list[int] = []
    for p in parts:
        m = re.match(r"^(\d+)", p)
        if not m:
            break
        nums.append(int(m.group(1)))
    return tuple(nums) if nums else (0,)


def is_newer(remote_tag: str, current: str = CURRENT_VERSION) -> bool:
    """Compare ``remote_tag`` vs ``current``. Returns True iff remote
    is strictly newer.

    Tuples are zero-padded to the same length first: ``(0, 2, 0) > (0, 2)``
    is True in plain tuple ordering, so a two-component ``__version__``
    against a three-component tag would report an update forever and the
    app would re-download the build it is already running.
    """
    a = _parse_version(remote_tag)
    b = _parse_version(current)
    width = max(len(a), len(b))
    a += (0,) * (width - len(a))
    b += (0,) * (width - len(b))
    return a > b


def _read_cache() -> dict | None:
    p = _cache_path()
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_cache(payload: dict) -> None:
    p = _cache_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(payload), encoding="utf-8")
    except OSError as exc:
        log.debug("update cache write failed: %s", exc)


def _fetch_latest_release(timeout_s: float = 5.0) -> dict | None:
    """Single GET to the GitHub Releases API. Returns the parsed JSON
    or None on any failure (network, 4xx/5xx, parse error)."""
    req = urllib.request.Request(
        RELEASES_API,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/vnd.github+json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            data = resp.read()
        return json.loads(data)
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, OSError) as exc:
        log.info("update check skipped: %s", exc)
        return None


def _pick_zip_asset(release_json: dict) -> str | None:
    """Find the Windows zip asset URL in a GitHub release payload.
    Returns the browser_download_url of the first asset whose name
    matches ``*win64.zip`` (case-insensitive) or ``None`` if none."""
    assets = release_json.get("assets") or []
    plat = platform.system().lower()
    suffix = "win64.zip" if plat.startswith("win") else None
    if suffix is None:
        return None  # Linux/macOS bundles not shipped yet
    for asset in assets:
        name = (asset.get("name") or "").lower()
        if name.endswith(suffix):
            return asset.get("browser_download_url")
    return None


class _CheckWorker(QObject):
    """Runs the HTTP fetch on a worker thread. Emits exactly one of
    :attr:`finished_available` / :attr:`finished_none` / :attr:`finished_error`,
    then terminates."""

    finished_available = Signal(object)  # UpdateInfo
    finished_none = Signal()
    finished_error = Signal(str)

    def __init__(self, force: bool) -> None:
        super().__init__()
        self._force = force

    def run(self) -> None:
        try:
            if not self._force:
                cached = _read_cache()
                if cached and time.time() - float(cached.get("checked_at", 0)) < CACHE_INTERVAL_S:
                    # Re-evaluate the cached TAG rather than replaying the
                    # cached verdict. The verdict was reached by the previous
                    # build; after a successful update the new build starts
                    # inside the 6 h window, reads "update_available: true"
                    # for the version it is already running, and offers to
                    # install it again — re-downloading and re-swapping the
                    # identical bundle until the cache expires.
                    if cached.get("tag") and is_newer(str(cached["tag"])):
                        info = UpdateInfo(
                            tag=cached["tag"],
                            name=cached.get("name", cached["tag"]),
                            body=cached.get("body", ""),
                            html_url=cached.get("html_url", ""),
                            zip_url=cached.get("zip_url"),
                        )
                        self.finished_available.emit(info)
                    else:
                        self.finished_none.emit()
                    return

            release = _fetch_latest_release()
            if release is None:
                self.finished_error.emit("network or API error")
                return

            tag = str(release.get("tag_name") or "")
            if not tag:
                self.finished_error.emit("release payload missing tag_name")
                return

            available = is_newer(tag)
            info_payload = {
                "checked_at": time.time(),
                "update_available": available,
                "tag": tag,
                "name": release.get("name") or tag,
                "body": (release.get("body") or "")[:2000],  # truncate huge release notes
                "html_url": release.get("html_url") or "",
                "zip_url": _pick_zip_asset(release),
            }
            _write_cache(info_payload)

            if available:
                self.finished_available.emit(
                    UpdateInfo(
                        tag=tag,
                        name=info_payload["name"],
                        body=info_payload["body"],
                        html_url=info_payload["html_url"],
                        zip_url=info_payload["zip_url"],
                    )
                )
            else:
                self.finished_none.emit()
        except Exception as exc:  # pragma: no cover — defensive
            log.warning("update check raised: %s", exc)
            self.finished_error.emit(str(exc))


class UpdateChecker(QObject):
    """Public Qt-friendly facade for the update check.

    Connect to ``update_available`` (emitted with ``UpdateInfo``) to
    show the banner, or ``no_update`` / ``check_failed`` if you want
    to drive UI state.
    """

    update_available = Signal(object)  # UpdateInfo
    no_update = Signal()
    check_failed = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._thread: QThread | None = None
        self._worker: _CheckWorker | None = None

    def check(self, *, force: bool = False) -> None:
        """Kick off a background check. No-op if one is already running."""
        if self._thread is not None and self._thread.isRunning():
            return

        self._thread = QThread(self)
        self._worker = _CheckWorker(force=force)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)

        self._worker.finished_available.connect(self.update_available)
        self._worker.finished_none.connect(self.no_update)
        self._worker.finished_error.connect(self.check_failed)

        # Whichever finishes, tear down.
        for sig in (
            self._worker.finished_available,
            self._worker.finished_none,
            self._worker.finished_error,
        ):
            sig.connect(self._thread.quit)
        self._thread.finished.connect(self._cleanup)
        self._thread.start()

    def shutdown(self, timeout_ms: int = 2000) -> None:
        """Stop any in-flight check and wait, so the window can close safely."""
        thread = self._thread
        if thread is not None and thread.isRunning():
            thread.quit()
            if not thread.wait(timeout_ms):
                log.warning("update-check thread did not finish in %d ms", timeout_ms)

    def _cleanup(self) -> None:
        # Only tear down the run that actually finished. check() guards on
        # isRunning(), which is already False between the worker emitting its
        # result and this queued slot running — so a second check started in
        # that window would otherwise be destroyed here, mid-run, taking its
        # C++ object out from under the thread executing it.
        if self.sender() is not self._thread:
            return
        if self._worker is not None:
            self._worker.deleteLater()
            self._worker = None
        if self._thread is not None:
            self._thread.deleteLater()
            self._thread = None
