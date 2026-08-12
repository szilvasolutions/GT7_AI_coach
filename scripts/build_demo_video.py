#!/usr/bin/env python3
"""Build a synced, ducked demo video from a PS5 screen recording + coach audio.

The demo capture setup has no capture card, so the two recordings share no
clock and no common audio:

* **video** — PS5 Create-button recording of the race (game video + game audio).
* **coach WAV** — Audacity WASAPI-loopback recording on the gaming PC of
  everything the coach spoke (nothing else may be audible on the PC).
* **session dir** — the coach's own ``sessions/run_<ts>/`` from the same run.
  Its ``debug.log`` carries millisecond wall-clock times for every
  ``utterance start`` line and for the ``race start`` anchor.

Sync strategy (no waveform alignment — the recordings share no audio):

1. Speech onsets in the coach WAV via ``ffmpeg silencedetect``.
2. Least-squares fit of those onsets against the ``utterance start`` wall
   times from debug.log -> offset + clock drift (wall time -> WAV time).
   Residuals are the correctness check: anything above ``--max-residual-ms``
   (default 50) aborts loudly rather than emitting a subtly desynced video.
3. One manual number: ``--race-start-video``, the video timestamp where the
   lap timer starts (scrub the PS5 clip once). debug.log's ``race start``
   line pins the same moment on the wall clock, which the fit converts to
   WAV time -> video/WAV offset known.
4. Mux: delay/trim the coach track, sidechain-duck the game audio under the
   voice, re-encode audio only (video stream is stream-copied).

Usage::

    python scripts/build_demo_video.py \
        --video ps5_clip.mp4 --wav coach.wav \
        --session sessions/run_20260812_183000 \
        --race-start-video 1:23.400 -o demo.mp4

``--dry-run`` prints the fit (offset, drift, per-cue residuals) and exits.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

UTTERANCE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}) .*utterance start(?: #\d+)?: "
)
RACE_START_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}) .*race start \(lap 1 began\)"
)
SILENCE_END_RE = re.compile(r"silence_end: (?P<t>[0-9.]+)")
LOG_TS_FMT = "%Y-%m-%d %H:%M:%S.%f"


def die(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(1)


def parse_timestamp(value: str) -> float:
    """``ss``, ``ss.mmm``, ``mm:ss.mmm`` or ``hh:mm:ss.mmm`` -> seconds."""
    parts = value.split(":")
    if not 1 <= len(parts) <= 3:
        raise argparse.ArgumentTypeError(f"bad timestamp: {value!r}")
    try:
        seconds = float(parts[-1])
        for factor, part in zip((60, 3600), reversed(parts[:-1]), strict=False):
            seconds += factor * int(part)
    except ValueError:
        raise argparse.ArgumentTypeError(f"bad timestamp: {value!r}") from None
    return seconds


def read_debug_log(session_dir: Path) -> tuple[list[float], float]:
    """Return (utterance wall times, race-start wall time), seconds since epoch."""
    log_path = session_dir / "debug.log"
    if not log_path.is_file():
        die(f"{log_path} not found — pass the session run directory, not its parent")
    cues: list[float] = []
    race_start: float | None = None
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = UTTERANCE_RE.match(line)
        if m:
            cues.append(datetime.strptime(m.group("ts"), LOG_TS_FMT).timestamp())
            continue
        m = RACE_START_RE.match(line)
        if m and race_start is None:  # first race start; restarts re-log it
            race_start = datetime.strptime(m.group("ts"), LOG_TS_FMT).timestamp()
    if not cues:
        die(
            "no 'utterance start' lines in debug.log — coach never spoke, or the "
            "session predates the utterance-start logging (v0.1.0)"
        )
    if race_start is None:
        die(
            "no 'race start' line in debug.log — was the coach already running "
            "when the race began? Re-run with the coach started before the race."
        )
    return cues, race_start


def detect_speech_onsets(wav: Path, *, noise_db: float, min_silence_s: float) -> list[float]:
    """Speech onset times (s) in the WAV = ffmpeg silencedetect silence_end marks."""
    proc = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-i",
            str(wav),
            "-af",
            f"silencedetect=noise={noise_db}dB:d={min_silence_s}",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        die(f"ffmpeg silencedetect failed:\n{proc.stderr[-2000:]}")
    onsets = [float(m.group("t")) for m in SILENCE_END_RE.finditer(proc.stderr)]
    # The recording usually starts mid-silence, so the first silence_end IS the
    # first onset. But if Audacity caught a click at t=0, silencedetect emits no
    # leading silence_end for it — nothing to do about that here; the alignment
    # step tolerates it by scanning contiguous windows.
    if not onsets:
        die(
            f"no speech onsets found in {wav} (noise={noise_db}dB). Is it the "
            "right file? Try a higher --noise-db (e.g. -30)."
        )
    return onsets


def fit_offset_drift(
    wall_times: list[float], onsets: list[float], anchor: float
) -> tuple[float, float, list[float]]:
    """Least-squares fit onsets ~= offset + drift * (wall - anchor).

    Returns (offset, drift, residuals_ms). The anchor keeps x small (wall
    times are epoch seconds — fitting them raw loses precision) and must be
    the same one used later to evaluate the fit.
    """
    x = [w - anchor for w in wall_times]
    y = onsets
    n = len(x)
    if n == 1:
        return y[0], 1.0, [0.0]
    sx, sy = sum(x), sum(y)
    sxx = sum(v * v for v in x)
    sxy = sum(a * b for a, b in zip(x, y, strict=True))
    denom = n * sxx - sx * sx
    if denom == 0:
        return y[0], 1.0, [0.0] * n
    drift = (n * sxy - sx * sy) / denom
    offset = (sy - drift * sx) / n
    residuals_ms = [(y[i] - (offset + drift * x[i])) * 1000 for i in range(n)]
    return offset, drift, residuals_ms


def align(
    cues: list[float], onsets: list[float], max_residual_ms: float
) -> tuple[list[float], float, float, list[float]]:
    """Pair logged cues with detected onsets; return (onsets_used, offset, drift, residuals).

    Counts can differ: silencedetect merges cues spoken < min-silence apart and
    may pick up stray clicks. Try every contiguous window of the longer list
    against the full shorter list and keep the best fit. Anything cleverer
    (gap-tolerant DP) is not worth it for a 90-second demo — if no window fits,
    we abort with diagnostics instead of guessing.
    """
    anchor = cues[0]
    best = None
    if len(onsets) >= len(cues):
        for start in range(len(onsets) - len(cues) + 1):
            window = onsets[start : start + len(cues)]
            offset, drift, res = fit_offset_drift(cues, window, anchor)
            worst = max(abs(r) for r in res)
            if best is None or worst < best[0]:
                best = (worst, window, offset, drift, res)
    else:
        for start in range(len(cues) - len(onsets) + 1):
            window = cues[start : start + len(onsets)]
            offset, drift, res = fit_offset_drift(window, onsets, anchor)
            worst = max(abs(r) for r in res)
            if best is None or worst < best[0]:
                best = (worst, onsets, offset, drift, res)
    worst, matched, offset, drift, res = best
    if worst <= max_residual_ms:
        return matched, offset, drift, res
    die(
        f"cue/onset alignment failed: {len(cues)} logged cues vs {len(onsets)} "
        f"detected onsets, best window still has {worst:.0f} ms worst residual "
        f"(limit {max_residual_ms:.0f}). Something else was audible on the PC, "
        "or the WAV is from a different run. Inspect with --dry-run, adjust "
        "--noise-db / --min-silence, or re-record."
    )


def build_ffmpeg_cmd(
    video: Path,
    wav: Path,
    out: Path,
    voice_delay_s: float,
    *,
    duck_ratio: float,
    voice_gain_db: float,
) -> list[str]:
    if voice_delay_s >= 0:
        ms = round(voice_delay_s * 1000)
        head = f"adelay={ms}:all=1"
    else:
        head = f"atrim=start={-voice_delay_s:.3f},asetpts=PTS-STARTPTS"
    filter_complex = (
        f"[1:a]{head},volume={voice_gain_db}dB,asplit=2[sc][mix];"
        f"[0:a][sc]sidechaincompress=threshold=0.03:ratio={duck_ratio}"
        f":attack=20:release=400[ducked];"
        f"[ducked][mix]amix=inputs=2:duration=first:dropout_transition=0,"
        f"alimiter=limit=0.95[aout]"
    )
    return [
        "ffmpeg",
        "-hide_banner",
        "-y",
        "-i",
        str(video),
        "-i",
        str(wav),
        "-filter_complex",
        filter_complex,
        "-map",
        "0:v",
        "-map",
        "[aout]",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        str(out),
    ]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--video", type=Path, required=True, help="PS5 recording (mp4)")
    ap.add_argument("--wav", type=Path, required=True, help="coach voice WAV (WASAPI loopback)")
    ap.add_argument("--session", type=Path, required=True, help="sessions/run_<ts> directory")
    ap.add_argument(
        "--race-start-video",
        type=parse_timestamp,
        required=True,
        help="video timestamp where the lap timer starts (e.g. 1:23.400)",
    )
    ap.add_argument("-o", "--out", type=Path, default=Path("demo.mp4"))
    ap.add_argument("--noise-db", type=float, default=-40.0, help="silencedetect threshold")
    ap.add_argument("--min-silence", type=float, default=0.4, help="min silence gap (s)")
    ap.add_argument("--max-residual-ms", type=float, default=50.0)
    ap.add_argument("--duck-ratio", type=float, default=8.0)
    ap.add_argument("--voice-gain-db", type=float, default=0.0)
    ap.add_argument("--dry-run", action="store_true", help="print the fit and exit")
    args = ap.parse_args()

    if shutil.which("ffmpeg") is None:
        die("ffmpeg not on PATH")
    for p in (args.video, args.wav):
        if not p.is_file():
            die(f"{p} not found")

    cues, race_start_wall = read_debug_log(args.session)
    onsets = detect_speech_onsets(args.wav, noise_db=args.noise_db, min_silence_s=args.min_silence)
    print(f"{len(cues)} logged cues, {len(onsets)} detected onsets")

    _used, offset, drift, residuals = align(cues, onsets, args.max_residual_ms)
    worst = max(abs(r) for r in residuals)
    print(
        f"fit: wav_time = {offset:.3f}s + {drift:.6f} * wall_elapsed  "
        f"(drift {1000 * 60 * (drift - 1):+.1f} ms/min, worst residual {worst:.0f} ms)"
    )
    for i, r in enumerate(residuals):
        print(f"  cue {i + 1:2d}: residual {r:+6.1f} ms")
    if abs(drift - 1) > 1e-4:
        print(
            "warning: clock drift above 6 ms/min — fine for a short demo, "
            "audible on recordings > ~10 min"
        )

    race_start_wav = offset + drift * (race_start_wall - cues[0])
    voice_delay = args.race_start_video - race_start_wav
    print(
        f"race start: wall {datetime.fromtimestamp(race_start_wall):%H:%M:%S.%f} "
        f"-> wav {race_start_wav:.3f}s -> video {args.race_start_video:.3f}s "
        f"(voice delay {voice_delay:+.3f}s)"
    )

    cmd = build_ffmpeg_cmd(
        args.video,
        args.wav,
        args.out,
        voice_delay,
        duck_ratio=args.duck_ratio,
        voice_gain_db=args.voice_gain_db,
    )
    if args.dry_run:
        print("dry run — would execute:")
        print("  " + " ".join(cmd))
        return
    proc = subprocess.run(cmd)
    if proc.returncode != 0:
        die("ffmpeg mux failed (see output above)")
    meta = {
        "video": str(args.video),
        "wav": str(args.wav),
        "session": str(args.session),
        "offset_s": offset,
        "drift": drift,
        "worst_residual_ms": worst,
        "race_start_video_s": args.race_start_video,
        "voice_delay_s": voice_delay,
    }
    args.out.with_suffix(".sync.json").write_text(json.dumps(meta, indent=2))
    print(f"wrote {args.out} (+ {args.out.with_suffix('.sync.json').name})")


if __name__ == "__main__":
    main()
