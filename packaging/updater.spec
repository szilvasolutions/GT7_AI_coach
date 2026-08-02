# -*- mode: python ; coding: utf-8 -*-
"""Tiny PyInstaller spec for the self-updater stub.

The updater is its own .exe so it can run while the main GUI is being
replaced (Windows can't overwrite a running .exe). It's shipped inside
the main bundle's folder as ``updater.exe``.
"""

from pathlib import Path

ROOT = Path(globals().get("SPECPATH", ".")).parent.resolve()

a = Analysis(
    [str(ROOT / "packaging" / "updater_stub.py")],
    pathex=[str(ROOT / "packaging")],
    binaries=[],
    datas=[],
    hiddenimports=[],
    excludes=[
        "tests",
        "PySide6",
        "google",
        "anthropic",
        "openai",
        "pyttsx3",
        "comtypes",
        "tkinter",
        "matplotlib",
        "PIL",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="updater",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,  # show a small console with progress / errors
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="updater",
)
