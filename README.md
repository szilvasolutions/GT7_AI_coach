<div align="center">

# GT7 AI Coach

**A real-time AI driving coach that talks to you while you drive Gran Turismo 7.**

### [![Download the installer](https://img.shields.io/badge/⬇%20DOWNLOAD-GT7Coach%20installer-2ea043?style=for-the-badge&logo=windows&logoColor=white&labelColor=1f6feb)](https://github.com/szilvasolutions/GT7_AI_coach/releases/latest/download/GT7Coach-Setup.exe)

**Download it. Run it. Click Next → Install. Done.**

No Python, no unzipping, nothing to set up first.

</div>

---

The coach reads Gran Turismo 7's telemetry stream from your PS4/PS5, detects
driving events with real physics — late braking, lockups, understeer,
wheelspin, clean corners — and speaks short, timed coaching lines through an
AI voice while you drive. Per-corner audit logs, lap summaries, a live
dashboard with track map and traction circle.

## What you need

* Gran Turismo 7 on PS4 or PS5
* A Windows PC on the same network as the console
* An API key for one supported AI provider (Gemini has a free tier), or a
  local [Ollama](https://ollama.com) model — the setup wizard walks you
  through it

## Installing

1. Hit the download button above and run `GT7Coach-Setup.exe`
2. Windows will show *"unknown publisher"* → click **More info** → **Run
   anyway** (the app is not code-signed; the SHA-256 sums of every release
   asset are published alongside it)
3. First launch takes ~10 seconds; the setup wizard does the rest

**Updating:** the app checks for updates itself — one click in the status bar
installs new versions. Nothing to re-download manually, ever.

**Prefer a portable copy?** Grab `GT7Coach-vX.Y.Z-win64.zip` from the
[releases page](https://github.com/szilvasolutions/GT7_AI_coach/releases/latest)
— the same app in a folder, no installer.

## About

Built by [Szilva Solutions](https://szilvasolutions.com). The application is
free to download and use. The source code is proprietary and no longer
published; earlier open-source snapshots remain under their original license.

Issues and feature requests are welcome in this repository's
[issue tracker](https://github.com/szilvasolutions/GT7_AI_coach/issues).
