# Reliability notes

Walked the five failure paths called out in the cleanup brief. Each is
traced to the file + line that handles it; gaps are flagged at the end.

Verdict: nothing currently crashes or pollutes the session log on any
of these paths. There are two soft observability gaps worth knowing
about (no "no packets in N seconds" warning; receiver re-raises
mid-session `OSError` after `start()`).

## a) Track not in the DB (unknown track)

**Path.** `TrackDetector.feed()` runs every packet; until it finds a
match, `track_detector.track` stays `None`. In `main.py:411`:

```python
if track_detector.track is None:
    tr = track_detector.feed(packet)
    if tr is not None:
        advisor.set_track_shape(tr.shape_description)
```

If no track ever matches, `set_track_shape()` is never called.
`AdvisorConfig.track_shape` defaults to `""` (empty string)
(`src/gt7coach/coach/advisor.py:161`). The prompt builder gates on
truthiness:

```python
if context.track_shape:
    lines.append(f"Track context: {context.track_shape}. ...")
```

(`src/gt7coach/coach/prompt.py:213-215`)

So the prompt simply omits the track block, the coach still runs, and
nothing fails. `TrackDetector` also caps probing: after
`max_probes=600` attempts (≈10 s at 60 Hz) without a match, it sets
`_gave_up=True` and returns `None` permanently — no further bbox /
sequence work per packet. The `track detected: ...` log line never
appears; that's the user-visible signal.

**Verdict.** ✅ clean. The only gap is that the user has no way to
*know* the track wasn't matched except by reading the logs. (Not
fixing tonight — it's a feature, not a bug.)

## b) LLM provider returns error or empty response

**Path.** Two LLM call sites:

1. **Per-corner advice** in `Advisor._process_job`
   (`src/gt7coach/coach/advisor.py`). Around the `provider.complete()`
   call:

   ```python
   try:
       advice = self.provider.complete(system_prompt, user_prompt)
       failure_reason = None
   except ProviderError as exc:
       log.warning("provider failed: %s — falling back to canned phrase", exc)
       advice = ""
       failure_reason = f"provider-error: {exc}"
   if advice and len(advice.split()) < 3:
       failure_reason = f"too-short-response: {advice!r}"
       advice = ""
   if not advice:
       advice = (random.choice(_COMPLIMENT_FALLBACKS)
                 if is_compliment else fallback_phrase(job.winner.type))
       if not advice:
           return self._record(None, job.winner, failure_reason or "empty-response", ...)
       failure_reason = f"{failure_reason or 'empty-response'}; spoke fallback"
   ```

   Three layers: catch `ProviderError` → check empty / too-short →
   pick a canned fallback (compliment-aware) → if even the fallback is
   empty, record the suppression and stay silent. Either way the
   `on_result` callback fires and `coach.jsonl` gets the real story.

2. **Lap-end summary** in `LapTracker._on_lap_complete`
   (`src/gt7coach/coach/laps.py:150-162`):

   ```python
   try:
       text = self.provider.complete(LAP_SUMMARY_SYSTEM_PROMPT, user_prompt, ...)
   except ProviderError as exc:
       log.warning("provider failed on lap summary: %s — using fallback", exc)
       text = ""
   text = (text or "").strip().strip("\"'`")
   if not text or len(text.split()) < 3:
       text = _canned_summary(last_lap_ms, self._best_lap_ms, delta_ms, counts)
   ```

   Falls back to `_canned_summary` (PB / near-best / dominant-mistake
   / clean-lap branches). Always something to speak.

**Verdict.** ✅ clean on both sites. Logging stays honest — every
fallback writes a `suppressed_reason` / `failure_reason` so
post-session analysis can tell "the coach spoke a canned line
because Gemini errored" apart from "the coach actually got a fresh
response".

## c) Lap completes with zero detected events

**Path.** `LapTracker._on_lap_complete` always builds a prompt. If
`counts` is empty, `_build_prompt` appends `"No mistakes detected this
lap."` and the LLM is asked to summarise that. If the LLM (or the
canned fallback) is reached with an empty `counts`, `_canned_summary`
falls through to the `random.choice` of three clean-lap compliments
(`Clean lap, 1:23.456.` / `Tidy, 1:23.456.` / `Solid lap, 1:23.456.`).

`Advisor.on_corner` separately handles the *per-corner* zero-events
case: `if not events_list: return self._record(None, None, "no events", ...)`.
The receive loop never burns rate-limit budget on an empty corner.

**Verdict.** ✅ clean. There's a dedicated branch for "nothing
detected" at both granularities.

## d) Heartbeat to PS5 starts failing mid-session

**Path.** `_heartbeat_loop` swallows `OSError` and logs a warning
(`src/gt7coach/telemetry/receiver.py:240-243`):

```python
try:
    beacon_sock.sendto(heartbeat, (self._target_ip, self.cfg.port_tx))
except OSError as exc:
    log.warning("heartbeat send failed: %s", exc)
```

The loop continues. The PS5 stops sending packets after 16 s of
heartbeat silence; the receive loop's `recvfrom()` sees `TimeoutError`
every second and `continue`s. No crash, no log spam beyond one
warning per failed sendto.

**The gap.** The coach goes silent because there are no corners, but
nothing tells the user "no packets in the last 30 s, your PS5 isn't
talking to us". If the network drops mid-stint, the user might keep
driving for minutes wondering why the coach died. Worth adding a
"no packets in N seconds" stderr warning in a later phase.

**Verdict.** ✅ no crash, but observability is thin. Logged as a soft
gap.

## e) PS5 sends a packet of unexpected size or format

**Path.** `Receiver.frames()` (`src/gt7coach/telemetry/receiver.py:206-225`):

```python
if len(data) < expected:
    log.debug("ignoring short packet (%d bytes)", len(data))
    continue
try:
    decrypted = decrypt_packet(data, fmt=self.cfg.packet_format)
    pkt = parse_packet(decrypted, recv_time=monotonic())
except (ValueError, IndexError) as exc:
    log.warning("packet parse failed: %s", exc)
    continue
```

Two layers: short packets are debug-logged and dropped. Decrypt /
parse errors (bad magic, unexpected size after decrypt, struct unpack
out of range) are caught as `ValueError` / `IndexError`, warning-logged
and dropped. The receive loop continues.

`parse_packet` validates the magic word (`GT7_MAGIC = 0x47375330`,
`packet.py:181`) so a packet from the wrong game / source raises
`ValueError` which is caught.

**Verdict.** ✅ clean. Garbage packets degrade silently to "fewer
corners detected".

## Soft gaps (not fixing tonight)

These are observability holes, not crashes. The coach keeps running;
the user just doesn't know they're in a degraded state.

1. **No "no packets in N seconds" warning** when the PS5 stops sending
   mid-session. Receive loop just sits in `recvfrom` timing out
   silently. Could add a watchdog in `Receiver.frames()` that logs an
   error if `monotonic() - last_packet_t > 30` seconds.
2. **`Receiver.frames` re-raises `OSError`** after start. If the
   socket fails after `start()` for a reason other than `_stop` being
   set (e.g. NIC down), the exception propagates out of the
   generator. `main.py`'s `try/finally` cleans up logger / voice /
   advisor, but the process exits non-zero with a traceback. Not
   observed in practice; flagged for awareness.
3. **No "no track matched after N seconds" warning** to the user.
   `TrackDetector` logs at INFO when it gives up; without `-v` on the
   coach, that doesn't surface. The behaviour itself is fine (coach
   runs without track context), but the user has no warning that the
   track-context prompt block will be missing for this session.
4. **No "no API key in env" early check.** Currently the coach builds
   the `provider` lazily and any auth failure surfaces as a
   `ProviderError` on the first corner's LLM call. The fallback path
   handles it (canned phrase + warning), but the user only finds out
   they typo'd the env var when their first corner gets a canned
   reply. Would be nicer to fail fast at startup.

None of these are correctness bugs. All four are "the coach should be
slightly more talkative about what it can't do". Worth a future
phase if the user wants tighter feedback.

## How to re-verify

```bash
pytest tests/test_phase7.py tests/test_phase6.py tests/test_coach.py -v
```

The existing test suite already covers:

* `test_no_track_match` paths (give-up after `max_probes` outside).
* `test_lap_tracker_uses_canned_fallback_when_provider_fails`.
* `test_canned_summary_*` branches.
* `test_clean_corner_quiet_*` for the no-events corner path.
* `test_advisor_falls_back_to_canned_phrase_on_provider_error` (in
  `test_coach.py`).

For (d) and (e), the receive-loop paths are covered in `test_telemetry.py`
and `test_capture.py` against synthetic / replayed streams. Live
network-failure simulation isn't in the suite — those paths are small
enough to read end-to-end.
