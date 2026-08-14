# Plan B — Reduce AV false-positives (free) + newbie-friendly install

**Goal:** make Windows Defender/AV far less likely to flag `GT7Coach.exe`, and give a
non-technical user a simple "download → run → done" install — **without paid code
signing.**

**Accepted limitation:** everything here is still *unsigned*, so the one-time
SmartScreen "unknown publisher → More info → Run anyway" prompt remains. Removing that
requires code signing (deferred; revisit if/when the app earns money).

**Backup taken (2026-08-14):** `/root/backups/gt7/GT7_AI_coach-20260814-2011.bundle`
(full git history, verified) + `…-src-20260814-2011.tar.gz`. Restore with
`git clone <bundle>`.

---

## Phase 1 — Stop shipping the worst offender  *(quick win)*
- Remove the **one-file** PyInstaller build (`gt7coach-onefile.spec`, its build/smoke
  steps, and `dist-onefile/GT7Coach.exe` from the release assets + `SHA256SUMS.txt`).
- The single-file exe is the biggest Defender magnet; the folder build already flags less.

## Phase 2 — Rebuild the app with **Nuitka** instead of PyInstaller  *(main win)*
Nuitka compiles Python to real C/machine code, so AV heuristics that fingerprint the
PyInstaller bootloader stop matching.
- Add `nuitka` to the build deps; drop `pyinstaller` for the app.
- Build command (GUI entry = `src/gt7coach/gui/app.py`), `--standalone` (folder, not onefile):
  ```
  python -m nuitka --standalone --assume-yes-for-downloads \
    --enable-plugin=pyside6 --windows-console-mode=disable \
    --windows-icon-from-ico=packaging/gt7coach.ico \
    --include-data-dir=src/gt7coach/tracks/data=gt7coach/tracks/data \
    --include-data-dir=src/gt7coach/gui/assets=gt7coach/gui/assets \
    --include-data-files=config.example.yaml=config.example.yaml \
    --include-package=pyttsx3 --include-package=comtypes \
    --include-package=anthropic --include-package=openai --include-package=google \
    --output-dir=dist-nuitka src/gt7coach/gui/app.py
  ```
  then rename `dist-nuitka/app.dist` → `dist/GT7Coach/`, `app.exe` → `GT7Coach.exe`.
- **Known risk — dynamic imports** (the part likely to need 1–3 CI iterations):
  - `pyttsx3.drivers.sapi5` + `comtypes` (Windows voice) — comtypes generates modules at
    runtime; may need extra `--include-module` / a comtypes gen dir.
  - `google.genai` / `google.api_core` (namespace packages) — Nuitka is picky here.
  - Verify the folder build still lets the **in-app updater swap files atomically**
    (Nuitka `--standalone` keeps a normal folder, so this should hold).

## Phase 3 — Convert `updater.exe` to Nuitka too
Same treatment so the updater isn't independently flagged.

## Phase 4 — **Inno Setup** wizard installer  *(newbie flow)*
- Add `packaging/gt7coach.iss`; build it on the `windows-latest` runner (Inno Setup ships
  via choco / an action).
- Produces `GT7Coach-Setup-vX.Y.Z.exe`: a familiar Next→Next→Install wizard that unpacks
  the Nuitka folder to `%LOCALAPPDATA%\Programs\GT7Coach`, adds Start-menu + desktop
  shortcuts, and registers an uninstaller. No manual "extract the zip" step.
- Inno installers are themselves far less AV-flagged than packed Python exes.
- This becomes the **primary download** for beginners; the folder zip stays as the
  "advanced / portable" option.

## Phase 5 — False-positive handling  *(per release)*
- Add `docs/RELEASING.md` step: after a release, check the exe on **VirusTotal** to see how
  many engines flag it, and submit any Defender flag to Microsoft's false-positive portal
  (https://www.microsoft.com/en-us/wdsi/filesubmission) — clears within ~1–3 days.
- Optional: a small `scripts/av_check.py` that uploads the artifact to the VirusTotal API
  (needs a free VT key) and prints the detection ratio, so we can watch it improve.

## Phase 6 — Update downloader + docs
- `install.ps1`: offer the **Setup .exe** as the default (download + launch the wizard),
  keep the zip path as fallback.
- `README.md`: point the big "Download" link straight at `GT7Coach-Setup-….exe`; document
  the one-time "unknown publisher → More info → Run anyway" click with a screenshot.

---

## Testing / iteration
1. All changes on a branch; trigger the release workflow via `workflow_dispatch` with a
   throwaway tag (e.g. `vdev-nuitka`) — no real release published.
2. **You test the built `GT7Coach-Setup-….exe` on a real Windows PC**: confirm the app runs
   (telemetry, voice, provider calls) and note what Defender does.
3. Compare VirusTotal detections: PyInstaller one-file (before) vs Nuitka folder + Inno (after).
4. Iterate on Nuitka dynamic-import flags until the app runs clean, then cut a real tag.

## Split of work
- **Me:** all workflow / spec / installer-script / README changes; drive the CI iterations.
- **You:** run the artifact on Windows to confirm functionality + observe AV behavior; do the
  one-click Microsoft false-positive submission when a release goes out (I'll give the exact link).

## Realistic outcome
- Defender "virus/trojan" false-positive: **greatly reduced**, and easily cleared via the
  submission when it does happen.
- SmartScreen "unknown publisher": **still present** (signing-only) — one extra click for the user.
- Newbie install: **download one Setup .exe → wizard → done** (plus the one SmartScreen click).
