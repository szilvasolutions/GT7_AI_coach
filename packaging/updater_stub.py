"""Tiny self-updater for the PyInstaller-bundled GT7 AI Coach.

The GUI shells out to this stub to swap files because Windows won't
let an .exe overwrite itself while it's running. Flow:

    1. Wait for the GUI's PID to terminate (bounded; force-kill after timeout).
    2. Verify the staged zip's SHA-256 (already validated by the GUI but
       checked again here so a manual command-line invocation is safe).
    3. Rename the current install directory to ``GT7Coach.bak.<ts>``.
    4. Extract the staged zip in its place.
    5. Launch the new ``GT7Coach.exe`` from the new install.
    6. On any failure between (3) and (4), restore from the .bak.

This file is shipped as its own tiny PyInstaller exe (~10 MB) alongside
the main bundle. It depends only on the stdlib.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

log = logging.getLogger("gt7coach.updater")


def wait_for_pid_exit(pid: int, timeout_s: float = 10.0) -> bool:
    """Poll until the GUI process disappears or we time out."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.25)
    return not _pid_alive(pid)


def _pid_alive(pid: int) -> bool:
    """Cross-platform 'is this PID still running'."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        # On Windows we can't send signal 0 reliably; use OpenProcess via ctypes.
        import ctypes

        SYNCHRONIZE = 0x00100000
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not handle:
            return False
        exit_code = ctypes.c_ulong()
        STILL_ACTIVE = 259
        ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return bool(ok) and exit_code.value == STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def relocate_self_to_temp() -> Path | None:
    """Copy this updater out of the install directory and re-exec from there.

    Windows will not rename or move a directory that contains a running
    executable, and updater.exe ships *inside* the install folder it is meant
    to replace. The rename therefore failed with a sharing violation every
    time, the updater exited quietly, and the app simply never updated.

    Returns the temp path if a re-exec was launched (caller should exit), or
    None if relocation is unnecessary or impossible.
    """
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return None
    me = Path(sys.executable).resolve()
    if os.environ.get("GT7COACH_UPDATER_RELOCATED") == "1":
        return None  # already running from the copy

    tmp_dir = Path(tempfile.mkdtemp(prefix="gt7coach-updater-"))
    copy = tmp_dir / me.name
    try:
        shutil.copy2(me, copy)
    except OSError as exc:
        log.warning("could not copy updater to %s: %s", copy, exc)
        return None

    env = dict(os.environ, GT7COACH_UPDATER_RELOCATED="1")
    log.info("re-executing from %s so the install folder can be replaced", copy)
    subprocess.Popen(
        [str(copy), *sys.argv[1:]],
        env=env,
        creationflags=0x00000010,  # CREATE_NEW_CONSOLE — keep errors visible
        close_fds=True,
    )
    return copy


def _extract_root(zf: zipfile.ZipFile) -> str:
    """Return the single top-level folder in the zip, or '' if there isn't one.

    The release zip wraps everything in ``GT7Coach/``. Extracting it straight
    into the install directory produced ``<install>/GT7Coach/GT7Coach.exe`` —
    nested one level too deep — so the contents must be lifted out of it.
    """
    tops = {n.split("/")[0] for n in zf.namelist() if n.strip()}
    if len(tops) != 1:
        return ""
    only = tops.pop()
    return only if any(n.startswith(only + "/") for n in zf.namelist()) else ""


def _extract_stripping_root(zf: zipfile.ZipFile, dest: Path) -> None:
    root = _extract_root(zf)
    if not root:
        zf.extractall(dest)
        return
    prefix = root + "/"
    for member in zf.infolist():
        if not member.filename.startswith(prefix):
            continue
        rel = member.filename[len(prefix) :]
        if not rel:
            continue
        target = dest / rel
        if member.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(member) as src, target.open("wb") as out:
            shutil.copyfileobj(src, out)


def swap_install(
    zip_path: Path,
    install_dir: Path,
    expected_sha256: str | None = None,
) -> Path:
    """Backup, extract, and return the path to the new install dir.

    Raises on any error. The caller (main / GUI) is responsible for
    rolling back from the .bak directory if subsequent steps fail.
    """
    if not zip_path.is_file():
        raise FileNotFoundError(f"zip not found: {zip_path}")
    if not install_dir.exists():
        raise FileNotFoundError(f"install dir not found: {install_dir}")
    if not install_dir.is_dir():
        raise NotADirectoryError(f"install dir is not a directory: {install_dir}")

    if expected_sha256:
        actual = sha256_of(zip_path)
        if actual.lower() != expected_sha256.lower():
            raise ValueError(f"sha256 mismatch: expected {expected_sha256}, got {actual}")

    ts = time.strftime("%Y%m%d_%H%M%S")
    backup = install_dir.with_name(install_dir.name + f".bak.{ts}")
    log.info("renaming current install %s -> %s", install_dir, backup)
    install_dir.rename(backup)

    try:
        install_dir.mkdir(parents=True, exist_ok=False)
        log.info("extracting %s -> %s", zip_path, install_dir)
        with zipfile.ZipFile(zip_path, "r") as zf:
            _extract_stripping_root(zf, install_dir)
    except Exception:
        log.exception("extract failed; rolling back from %s", backup)
        # Best-effort rollback. Remove the half-extracted dir, restore backup.
        if install_dir.exists():
            shutil.rmtree(install_dir, ignore_errors=True)
        backup.rename(install_dir)
        raise

    log.info("update extracted OK; backup retained at %s", backup)
    return install_dir


def relaunch(install_dir: Path, exe_name: str = "GT7Coach.exe") -> None:
    """Spawn the freshly-installed exe and detach so we can exit."""
    exe = install_dir / exe_name
    if not exe.is_file():
        # Fall back to scanning for the right file (in case the zip uses a
        # nested layout like GT7Coach-vX.Y.Z/GT7Coach.exe).
        candidates = list(install_dir.rglob(exe_name))
        if not candidates:
            raise FileNotFoundError(f"{exe_name} not found under {install_dir}")
        exe = candidates[0]
    log.info("launching %s", exe)
    # Detach: no wait, no inherited handles. On Windows use CREATE_NEW_PROCESS_GROUP
    # so the new process isn't tied to our updater's console.
    kwargs: dict = {"cwd": str(exe.parent)}
    if sys.platform == "win32":
        DETACHED_PROCESS = 0x00000008
        kwargs["creationflags"] = DETACHED_PROCESS
        kwargs["close_fds"] = True
    subprocess.Popen([str(exe)], **kwargs)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="gt7coach-updater",
        description="Atomic self-updater for the GT7 AI Coach Windows bundle.",
    )
    p.add_argument("--pid", type=int, required=True, help="PID of the GUI to wait for")
    p.add_argument("--zip", type=Path, required=True, help="Path to the staged release zip")
    p.add_argument("--install-dir", type=Path, required=True, help="Existing install directory")
    p.add_argument("--sha256", default=None, help="Expected hex SHA-256 of the zip")
    p.add_argument(
        "--no-restart",
        action="store_true",
        help="Skip launching the new build (testing / scripted use).",
    )
    p.add_argument(
        "--wait-timeout",
        type=float,
        default=10.0,
        help="Seconds to wait for the GUI PID to exit (default 10).",
    )
    args = p.parse_args(argv)

    # Log to a file as well as the console. The GUI spawns this detached, so
    # when the swap failed there was nothing on screen and nothing on disk to
    # explain why -- the update simply never happened.
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    log_path = Path(tempfile.gettempdir()) / "gt7coach-updater.log"
    try:
        handlers.append(logging.FileHandler(log_path, mode="a", encoding="utf-8"))
    except OSError:
        pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )
    log.info("updater log: %s", log_path)

    # Get out of the directory we are about to replace before touching it.
    if relocate_self_to_temp() is not None:
        log.info("handed over to the relocated copy; exiting")
        return 0

    log.info("updater starting (pid=%d, zip=%s, install=%s)", args.pid, args.zip, args.install_dir)
    if not wait_for_pid_exit(args.pid, timeout_s=args.wait_timeout):
        log.warning(
            "GUI pid %d still alive after %.1fs — proceeding anyway", args.pid, args.wait_timeout
        )

    try:
        new_install = swap_install(args.zip, args.install_dir, args.sha256)
    except Exception as exc:
        log.exception("update failed: %s", exc)
        return 2

    if not args.no_restart:
        try:
            relaunch(new_install)
        except Exception as exc:
            log.error("relaunch failed: %s", exc)
            return 3

    log.info("update complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
