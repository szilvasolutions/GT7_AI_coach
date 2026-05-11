# Track database attribution

`tracks.json` is built from two upstream community datasets:

## zetetos/gt-telemetry — polylines (MIT)

Per-track racing-line polylines were captured from real GT7 laps by the
project maintainers and published under the MIT licence.

- Source: <https://github.com/zetetos/gt-telemetry>
- File path in upstream: `pkg/circuits/inventory/*.json`
- Copyright: 2024 vwhitteron
- Licence: MIT — copied verbatim below.

## ddm999/gt7info — track metadata (community attribution)

Polyphony Digital course IDs, corner counts, lengths, oval flag, and other
metadata are sourced from ddm999's gt7info community dataset.

- Source: <https://github.com/ddm999/gt7info>
- File path in upstream: `_data/db/course.csv`
- No formal licence file; used with community attribution.

## Rebuild

Re-run `scripts/build_track_db.py` to fetch fresh data and rewrite
`tracks.json`. The shape descriptions are templated from the joined
statistics and never hand-edited per-track.

---

## MIT licence (zetetos/gt-telemetry)

```
MIT License

Copyright (c) 2024 vwhitteron

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
