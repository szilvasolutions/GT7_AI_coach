"""How the app was packaged, and where it lives on disk.

``sys.frozen`` and ``sys._MEIPASS`` are PyInstaller's markers. Plan B swaps
the build to Nuitka to stop Windows Defender matching on the PyInstaller
bootloader, and Nuitka does not set either of them — it injects
``__compiled__`` into every compiled module instead.

Three separate places were testing for PyInstaller specifically, so under a
Nuitka build every one of them would have silently taken the "running from
source" branch:

* ``gui/runner.py`` would launch ``GT7Coach.exe -m gt7coach.main``, which the
  GUI's own argparse rejects — the exact failure that made every Start click
  die in v0.1.0, reintroduced by a different packager.
* ``gui/updater.py`` would report the app cannot self-update at all.
* ``gui/self_update.py`` would look for the install directory in the current
  working directory rather than next to the executable.

Detect both packagers in one place instead.
"""

from __future__ import annotations

import sys
from pathlib import Path


def is_frozen() -> bool:
    """True when running from a packaged build, PyInstaller or Nuitka."""
    if getattr(sys, "frozen", False) or hasattr(sys, "_MEIPASS"):
        return True
    # Nuitka defines __compiled__ in the globals of every module it compiles.
    return "__compiled__" in globals()


def packager() -> str:
    """Which packager produced this build — for logs and bug reports."""
    if "__compiled__" in globals():
        return "nuitka"
    if getattr(sys, "frozen", False) or hasattr(sys, "_MEIPASS"):
        return "pyinstaller"
    return "source"


def app_dir() -> Path:
    """Directory holding the executable, or the working directory in dev."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path.cwd()
