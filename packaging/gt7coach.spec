# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the GT7 AI Coach GUI bundle.

Produces a single-folder distribution under ``dist/GT7Coach/``. The
release workflow then zips that folder into the GitHub release
artifact ``GT7Coach-vX.Y.Z-win64.zip``.

Why single-folder over single-file:
* ``--onefile`` extracts to ``%TEMP%`` every launch (slow + AV-flagged)
* Single-folder lets the in-app updater swap files atomically without
  the running exe being locked inside a self-extracting archive.
"""

import sys
from pathlib import Path

# pyinstaller injects SPECPATH; fall back to cwd for editor linters
ROOT = Path(globals().get("SPECPATH", ".")).parent.resolve()
SRC = ROOT / "src"

# Bundle the 84-track database and the example config as data files
# (the PySide6 + pyttsx3 binaries are picked up automatically).
datas = [
    (str(SRC / "gt7coach" / "tracks" / "data"), "gt7coach/tracks/data"),
    (str(ROOT / "config.example.yaml"), "."),
    (str(ROOT / "README.md"), "."),
]

hiddenimports = [
    # pyttsx3's SAPI5 driver is loaded dynamically on Windows.
    "pyttsx3.drivers",
    "pyttsx3.drivers.sapi5",
    # comtypes generated modules — names aren't statically determinable.
    "comtypes",
    # provider SDKs the user might pick at runtime.
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
        "tkinter",  # we use PySide6
        "matplotlib",
        "PIL.ImageQt",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="GT7Coach",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # no console window for the GUI
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="GT7Coach",
)
