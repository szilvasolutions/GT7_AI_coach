"""Tests for ``packaging/updater_stub.py`` — pure-stdlib self-updater.

We exercise the swap / verify / rollback paths in a tmp_path that
plays the role of the install dir. The relaunch step is mocked
because spawning subprocesses in tests is fragile.
"""

from __future__ import annotations

import hashlib
import importlib.util
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STUB_PATH = ROOT / "packaging" / "updater_stub.py"


@pytest.fixture(scope="module")
def stub():
    """Import the stub as a module without it being on sys.path."""
    spec = importlib.util.spec_from_file_location("updater_stub", STUB_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["updater_stub"] = mod
    spec.loader.exec_module(mod)
    yield mod
    sys.modules.pop("updater_stub", None)


def _make_install_dir(parent: Path, name: str = "GT7Coach") -> Path:
    d = parent / name
    d.mkdir()
    (d / "GT7Coach.exe").write_bytes(b"old-fake-exe")
    (d / "README.txt").write_text("old README\n", encoding="utf-8")
    (d / "tracks_data").mkdir()
    (d / "tracks_data" / "tracks.json").write_text("[]", encoding="utf-8")
    return d


def _make_release_zip(parent: Path, name: str = "release.zip") -> Path:
    """Build a tiny 'new release' zip with a fake exe + a marker file."""
    # Unique stage dir per zip so multiple invocations in one test work.
    src = parent / f"stage-{name}"
    src.mkdir(parents=True, exist_ok=True)
    (src / "GT7Coach").mkdir(exist_ok=True)
    (src / "GT7Coach" / "GT7Coach.exe").write_bytes(b"new-fake-exe")
    (src / "GT7Coach" / "README.txt").write_text("new README\n", encoding="utf-8")

    zip_path = parent / name
    with zipfile.ZipFile(zip_path, "w") as zf:
        for f in (src / "GT7Coach").iterdir():
            zf.write(f, arcname=f"GT7Coach/{f.name}")
    return zip_path


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def test_sha256_of_matches_hashlib(stub, tmp_path: Path) -> None:
    f = tmp_path / "x.bin"
    f.write_bytes(b"some bytes" * 100)
    assert stub.sha256_of(f) == _sha256(f)


def test_swap_install_replaces_contents(stub, tmp_path: Path) -> None:
    install = _make_install_dir(tmp_path)
    zip_path = _make_release_zip(tmp_path)

    new_install = stub.swap_install(zip_path, install)

    assert new_install == install
    # The new exe should be in place.
    # The wrapper folder is stripped: the exe lands at the install root.
    new_exe = install / "GT7Coach.exe"
    assert new_exe.is_file()
    assert new_exe.read_bytes() == b"new-fake-exe"
    # Backup should exist alongside, with the old contents.
    backups = list(tmp_path.glob("GT7Coach.bak.*"))
    assert len(backups) == 1
    assert (backups[0] / "GT7Coach.exe").read_bytes() == b"old-fake-exe"


def test_swap_install_verifies_sha256_when_provided(stub, tmp_path: Path) -> None:
    install = _make_install_dir(tmp_path)
    zip_path = _make_release_zip(tmp_path)
    good = _sha256(zip_path)

    # Right hash — proceeds normally.
    stub.swap_install(zip_path, install, expected_sha256=good)

    # Wrong hash — raises, install dir untouched (must still exist).
    install2 = _make_install_dir(tmp_path, name="GT7Coach2")
    zip2 = _make_release_zip(tmp_path, name="r2.zip")
    with pytest.raises(ValueError, match="sha256 mismatch"):
        stub.swap_install(zip2, install2, expected_sha256="0" * 64)
    assert install2.is_dir(), "failed sha verify must not touch the install"
    assert not any(tmp_path.glob("GT7Coach2.bak.*")), "no backup on sha failure"


def test_swap_install_rolls_back_on_corrupt_zip(stub, tmp_path: Path, monkeypatch) -> None:
    install = _make_install_dir(tmp_path)
    # Build a "zip" that's actually a text file — ZipFile will refuse it.
    bad_zip = tmp_path / "not-a-zip.zip"
    bad_zip.write_text("this is not a zip", encoding="utf-8")

    with pytest.raises(zipfile.BadZipFile):
        stub.swap_install(bad_zip, install)

    # Install dir must have been restored from the backup.
    assert (install / "GT7Coach.exe").read_bytes() == b"old-fake-exe"
    assert (install / "README.txt").read_text(encoding="utf-8") == "old README\n"


def test_relaunch_invokes_exe(stub, tmp_path: Path, monkeypatch) -> None:
    install = tmp_path / "GT7Coach"
    install.mkdir()
    exe = install / "GT7Coach.exe"
    exe.write_bytes(b"fake")

    called: list[list[str]] = []

    def fake_popen(args, **kwargs):
        called.append(args)

        class _P:
            pass

        return _P()

    monkeypatch.setattr(stub.subprocess, "Popen", fake_popen)
    stub.relaunch(install)

    assert called and called[0][0].endswith("GT7Coach.exe")


def test_relaunch_searches_nested_layout(stub, tmp_path: Path, monkeypatch) -> None:
    """Releases zipped with a nested folder (GT7Coach-v0.2.0/GT7Coach/GT7Coach.exe)
    should still be findable."""
    install = tmp_path / "GT7Coach"
    nested = install / "GT7Coach-v0.2.0" / "GT7Coach"
    nested.mkdir(parents=True)
    (nested / "GT7Coach.exe").write_bytes(b"deep")

    called: list[list[str]] = []
    monkeypatch.setattr(
        stub.subprocess, "Popen", lambda args, **kw: called.append(args) or type("P", (), {})()
    )
    stub.relaunch(install)
    assert called and "GT7Coach.exe" in called[0][0]


def test_wait_for_pid_exit_returns_quickly_for_dead_pid(stub) -> None:
    # PID 0 is never a real process on any sane system.
    assert stub.wait_for_pid_exit(0, timeout_s=1.0) is True


def test_main_end_to_end(stub, tmp_path: Path, monkeypatch) -> None:
    install = _make_install_dir(tmp_path)
    zip_path = _make_release_zip(tmp_path)

    # Skip the wait_for_pid_exit poll.
    monkeypatch.setattr(stub, "wait_for_pid_exit", lambda pid, timeout_s=10.0: True)

    rc = stub.main(
        [
            "--pid",
            "1",
            "--zip",
            str(zip_path),
            "--install-dir",
            str(install),
            "--no-restart",
        ]
    )
    assert rc == 0
    assert (install / "GT7Coach.exe").read_bytes() == b"new-fake-exe"


# --- the release zip wraps everything in GT7Coach/ --------------------------


def test_extraction_strips_the_wrapper_folder(tmp_path):
    """The zip is GT7Coach/GT7Coach.exe, and install_dir IS the GT7Coach
    folder — extracting straight in produced <install>/GT7Coach/GT7Coach.exe,
    nested one level too deep."""
    import zipfile

    from updater_stub import _extract_root, _extract_stripping_root

    src = tmp_path / "rel.zip"
    with zipfile.ZipFile(src, "w") as zf:
        zf.writestr("GT7Coach/GT7Coach.exe", "exe")
        zf.writestr("GT7Coach/updater.exe", "upd")
        zf.writestr("GT7Coach/_internal/base_library.zip", "lib")

    with zipfile.ZipFile(src) as zf:
        assert _extract_root(zf) == "GT7Coach"
        dest = tmp_path / "install"
        dest.mkdir()
        _extract_stripping_root(zf, dest)

    assert (dest / "GT7Coach.exe").is_file(), "exe must land at the install root"
    assert (dest / "_internal" / "base_library.zip").is_file()
    assert not (dest / "GT7Coach").exists(), "the wrapper folder must not survive"


def test_extraction_leaves_a_flat_zip_alone(tmp_path):
    import zipfile

    from updater_stub import _extract_root, _extract_stripping_root

    src = tmp_path / "flat.zip"
    with zipfile.ZipFile(src, "w") as zf:
        zf.writestr("GT7Coach.exe", "exe")
        zf.writestr("README.txt", "hi")

    with zipfile.ZipFile(src) as zf:
        assert _extract_root(zf) == ""
        dest = tmp_path / "install2"
        dest.mkdir()
        _extract_stripping_root(zf, dest)
    assert (dest / "GT7Coach.exe").is_file()


def test_swap_install_produces_a_runnable_layout(tmp_path):
    """End to end: the swap must leave GT7Coach.exe directly in the install
    dir, which is what relaunch() looks for first."""
    import zipfile

    from updater_stub import swap_install

    install = tmp_path / "GT7Coach"
    install.mkdir()
    (install / "GT7Coach.exe").write_text("old")
    zip_path = tmp_path / "new.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("GT7Coach/GT7Coach.exe", "new")
        zf.writestr("GT7Coach/_internal/x", "y")

    swap_install(zip_path, install)
    assert (install / "GT7Coach.exe").read_text() == "new"
    assert (install / "_internal" / "x").is_file()
    assert list(tmp_path.glob("GT7Coach.bak.*")), "the old install must be kept as a backup"


def test_swap_preserves_the_inno_uninstaller(tmp_path):
    """Inno Setup registers unins000.exe in Add/Remove Programs. The release
    zip has no such file, so a wholesale folder swap would delete it and leave
    Windows with an uninstall entry pointing at nothing."""
    import zipfile

    from updater_stub import swap_install

    install = tmp_path / "GT7Coach"
    install.mkdir()
    (install / "GT7Coach.exe").write_text("old")
    (install / "unins000.exe").write_text("uninstaller")
    (install / "unins000.dat").write_text("uninstall-data")

    zip_path = tmp_path / "new.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("GT7Coach/GT7Coach.exe", "new")

    swap_install(zip_path, install)

    assert (install / "GT7Coach.exe").read_text() == "new", "the app must be updated"
    assert (install / "unins000.exe").read_text() == "uninstaller", "uninstaller must survive"
    assert (install / "unins000.dat").is_file()


def test_swap_without_an_uninstaller_is_unaffected(tmp_path):
    """A portable-zip install has no unins* files; the swap must not care."""
    import zipfile

    from updater_stub import swap_install

    install = tmp_path / "GT7Coach"
    install.mkdir()
    (install / "GT7Coach.exe").write_text("old")
    zip_path = tmp_path / "new.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("GT7Coach/GT7Coach.exe", "new")

    swap_install(zip_path, install)
    assert (install / "GT7Coach.exe").read_text() == "new"
    assert not list(install.glob("unins*"))
