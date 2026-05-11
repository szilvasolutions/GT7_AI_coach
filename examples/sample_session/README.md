# Sample session

A real `gt7coach-coach --summary` run from 2026-05-11, captured for the
file-format documentation. 3 minutes 36 seconds, 25 corners, 51 events,
Deep Forest Raceway in a Gr.3 RWD car.

> Note: this session was recorded against the Phase 7 codebase before the
> async-result logging fix landed, so `totals.advice_spoken: 0` is wrong —
> the coach actually spoke during the run, but only the synchronous
> "queued" stubs got written to `coach.jsonl`. The fix in
> [`207ab23`](../../../../commit/207ab23) addresses this; future captures
> have the correct count.

## Files

| File | Bytes | What's in it |
|---|---|---|
| `meta.json` | ~1 KB | Host info, full CLI args, totals (`packets`, `corners`, `events`, `advice_spoken`, `incidents`). |
| `events.jsonl` | ~15 KB | One JSON object per detected event, with the corner trace summary that produced it. |
| `coach.jsonl` | ~11 KB | One JSON object per advisor turn: verbatim system + user prompt and the LLM response (the AI audit log). |
| `telemetry.csv` | ~4.3 MB | Every packet, replay-compatible. Feed it back to the coach with `--source ./examples/sample_session/telemetry.csv`. |

## Replay it yourself

```powershell
gt7coach-coach --source .\examples\sample_session\telemetry.csv `
               --provider mock --voice null
```

That dry-runs all 9 detectors + the clean-corner positive-feedback path
without making a single LLM call. Swap in `--provider gemini` to hear what
the coach would have said.
