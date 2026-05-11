# Demo storyboard — 90-second video

Single recording, no edits, two windows visible:

```
+-------------------+-------------------------------+
|                   |                               |
|   gt7coach-coach  |   GT7 driving footage         |
|   terminal        |   (PS5 capture-card or phone) |
|                   |                               |
+-------------------+-------------------------------+
                    audio: TTS + game engine
```

Terminal on the **left** (logs scrolling, advice readable). GT7 footage
on the **right** at full volume so the engine note and the TTS sit side
by side. Coach output is audible because the terminal session itself
runs the TTS.

The goal is to show, in 90 seconds, that the coach catches **a fast
sweeper, a hairpin, a slide / spin moment, and a lap-end summary** —
the four behaviours that prove the system actually works.

---

## Track + car options

Pick whichever of these you can hot-lap. Listed in order of likelihood
that all four behaviours fire inside 90 seconds.

### Option A (recommended) — Deep Forest Raceway, Gr.3 RWD

* Why: dense corner sequence (15 corners in 1:25), final hairpin is a
  classic slide trap on cold tyres, fast esses give clean-corner
  compliments. Already the most-tested layout in this codebase.
* Car: any Gr.3 RWD. Porsche 911 RSR or BMW M6 GT3 are good picks —
  RWD power-on oversteer fires the wheelspin / oversteer detectors
  reliably.
* Tip: start on cold tyres. The first two corners almost always
  produce wheelspin + slide-adjacent events.

### Option B — Trial Mountain, Gr.4 AWD

* Why: long high-speed sweeper out of the tunnel (fast_corner clean
  events) + 90° downhill braking zone (late_brake) + a tight
  switchback (hairpin). Forgiving enough that you can drive smoothly
  and trigger compliments instead of just mistakes.
* Car: any Gr.4 AWD. Subaru BRZ Gr.4 is the user's existing baseline.

### Option C — Brands Hatch Indy, N300 RWD

* Why: shortest layout in the DB (15 corners in 0:50), so a lap fits
  inside the demo. Paddock Hill Bend is a guaranteed late_brake
  trigger; Druids is a textbook hairpin; Clearways is a fast sweeper.
* Car: Mazda MX-5, GR Yaris, or any N300 RWD. Low-power car = clean
  shift between coast and throttle, easier to demo without a wheel.

---

## Shot list — 90 seconds total

| Time | What's on screen | What the coach should say |
|---|---|---|
| 0:00–0:05 | Title card or just the terminal showing `gt7coach-coach --car-class "Gr.3 RWD" --summary` about to be run. | (silent) |
| 0:05–0:10 | Run the command. Show the `track detected: Deep Forest Raceway` log line as the first telemetry packets arrive. | (silent, but log line is the visual hook) |
| 0:10–0:15 | Cross the start/finish line. Pit out of the garage if needed. | "Coach ready." (startup confirmation) |
| 0:15–0:30 | First sector. Show the terminal scrolling — every corner trace logs with peak g + event count. | First **late_brake** or **early_lift** triggers ~corner 2 or 3. Coach speaks one short imperative. |
| 0:30–0:45 | Second sector. Aim for a clean fast sweeper (turn 4-5 at Deep Forest). | **Clean-corner compliment** — "Hooked up nicely through that one." |
| 0:45–1:00 | Approach the final hairpin (turn 14 at Deep Forest). Brake late and let it slide. | **Slide / spin incident** with sarcastic fallback — "Skating, are we?" or similar. |
| 1:00–1:15 | Recover, finish the lap. Cross the line. | **Lap-end summary** spoken via `voice.interrupt` — "1:25.456. Tighten up next lap." or similar. |
| 1:15–1:25 | Stop the coach with Ctrl-C. Show the `=== Session summary ===` block printed in the terminal. | (silent, but the summary is on screen) |
| 1:25–1:30 | Closing card with the GitHub URL: `github.com/szilvasolutions/GT7_AI_coach`. | (silent) |

Total: 1:30. Trim to 90 s in post by cutting dead air around 0:10 if
the lap takes less than 75 s.

---

## Voiceover script (optional)

If you want a narration track on top of the game + TTS audio, this fits
in 75-80 seconds at a normal reading pace:

> "This is GT7 AI Coach — an open-source driving coach that listens to
> the GT7 telemetry stream, detects mistakes with physics, and uses an
> LLM to coach you in real time.
>
> [pause for first coach utterance]
>
> Notice it never sees raw telemetry. The detectors find specific
> events — late braking, wheelspin, understeer — and the LLM only
> rewrites those events into one short imperative sentence.
>
> [pause for clean-corner compliment]
>
> It also catches when you're driving *well*, and says so.
>
> [pause for slide / spin incident]
>
> Spins, slides, and crashes interrupt whatever the coach was about to
> say with a dry one-liner.
>
> [pause for lap-end summary]
>
> And at the start-finish line, you get a one-or-two-sentence verdict on
> the lap.
>
> Everything is local. Your telemetry never leaves your machine; only
> the event summaries go to the LLM you configure. It's Apache-2.0 on
> GitHub — link in the description."

Don't read over the TTS. The point of the demo is to *hear* the coach.
Pause the narration whenever the coach is talking.

---

## Pre-flight checklist

Before hitting record, run these once:

```powershell
gt7coach-list-tracks --filter <your-track>
# Confirms the DB has your track id.

gt7coach-coach --source .\examples\sample_session\telemetry.csv `
               --provider gemini --voice pyttsx3
# Replays the sample session through the real LLM + TTS so you can hear
# what the coach will sound like on your speakers. ~3 minutes.
```

If `--source examples/sample_session/telemetry.csv` produces audible
advice, the live run will too. If it doesn't, fix that first.

Also:

* `.env` populated with the API key you intend to use.
* `config.yaml` has `coach.car_class` set so you don't need a CLI flag.
* `--summary` is on (already in the storyboard command) so the
  end-of-session debrief actually prints.
* Recording software is set to capture **both** the terminal audio and
  the GT7 audio (separate tracks if your tool supports it — easier to
  rebalance in post).

---

## If something breaks during the take

The coach will not crash. Worst case it goes silent for ~5 seconds while
a slow Gemini call completes, or speaks a canned fallback ("Brake
earlier") when the provider errors. Both still look correct on camera
— the canned line is short and fits the cadence. Just keep driving and
the next corner will recover.

If the terminal `track detected: ...` line never appears, you're outside
the bbox of every track in the DB (e.g. a special event circuit not
covered yet). The coach will still work, just without a track-shape
prompt block. Skip the track-detected hook in the storyboard and
proceed.

Recording is one take. Don't try to splice multiple laps — the lap-end
summary is sequence-dependent and the demo loses its rhythm if you cut.
