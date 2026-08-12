# -*- coding: utf-8 -*-
"""PyInstaller spec for the single-file GT7Coach.exe.

Same app as ``gt7coach.spec``, packed into ONE executable so a
non-technical user can download `GT7Coach.exe` and double-click it —
no zip, no folder, no `_internal` next to it.

Trade-offs vs the single-folder build (both are shipped every release):
* The bootloader unpacks ~230 MB to ``%TEMP%`` on every launch, so
  startup takes a few seconds and antivirus scans it more eagerly.
* Pressing Start pays that cost a second time — the GUI relaunches
  itself as the coach process (see ``gui/runner.py``).
* The in-app updater is folder-only, so this build just points at the
  releases page instead of self-updating.

Which is why the zip stays the recommended download for regular use.
"""

from pathlib import Path

ROOT = Path(globals().get("SPECPATH", ".")).parent.resolve()
SRC = ROOT / "src"

datas = [
    (str(SRC / "gt7coach" / "tracks" / "data"), "gt7coach/tracks/data"),
    (str(SRC / "gt7coach" / "gui" / "assets"), "gt7coach/gui/assets"),
    (str(ROOT / "config.example.yaml"), "."),
    (str(ROOT / "README.md"), "."),
]

hiddenimports = [
    "pyttsx3.drivers",
    "pyttsx3.drivers.sapi5",
    "comtypes",
    "google.genai",
    "google.api_core",
    "google.auth",
    "anthropic",
    "openai",
]

a = Analysis(
    [str(SRC / "gt7coach" / "gui" / "app.py")],
    pathex=[str(SRC)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tests",
        "tkinter",
        "matplotlib",
        "PIL.ImageQt",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

# Passing binaries + datas into EXE (and omitting COLLECT) is what makes
# this a one-file build.
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="GT7Coach",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
