# Releasing GT7 AI Coach (Windows)

## Build stack (Plan B — no code signing)
- **App** → Nuitka `--standalone` (real machine code; far fewer AV false-positives
  than the old PyInstaller bootloader).
- **Updater** → Nuitka `--onefile` (tiny stub, dropped flat next to `GT7Coach.exe`).
- **Installer** → Inno Setup wizard (`packaging/gt7coach.iss`) → `GT7Coach-Setup-<ver>.exe`.
- The old PyInstaller **one-file** exe is gone (it was the biggest Defender magnet).

## Cut a release
```bash
git tag v0.1.17
git push origin v0.1.17
```
The `Release Windows bundle` workflow builds and publishes a GitHub Release with:
`GT7Coach-Setup-<ver>.exe`, `GT7Coach-v0.1.17-win64.zip`, `SHA256SUMS.txt`.

## Test a build WITHOUT publishing a release
Run the workflow manually (Actions → *Release Windows bundle* → *Run workflow*, or via
API/`workflow_dispatch`). Test runs **do not** publish a Release — they upload the
installer + zip as **workflow artifacts** (14-day retention), so `/releases/latest`
stays untouched.

## After each release — clear AV false-positives
Everything is **unsigned**, so expect:
- **SmartScreen "unknown publisher"** — one-time "More info → Run anyway". Only code
  signing removes this (deferred until the app earns money).
- Occasional **Defender "virus"** false-positive — do the two steps below.

1. **Check the detection rate** (needs a free VirusTotal API key in `$VT_API_KEY`):
   ```bash
   python scripts/av_check.py dist/installer/GT7Coach-Setup-<ver>.exe
   ```
2. **Report false-positives to Microsoft** (clears Defender in ~1–3 days):
   https://www.microsoft.com/en-us/wdsi/filesubmission
   - Product: *Microsoft Defender Antivirus*
   - "This is a false positive / should not be detected"
   - Attach `GT7Coach-Setup-<ver>.exe` (and `GT7Coach.exe` if flagged too).

Reputation builds over downloads, so later versions get flagged less.
