#!/usr/bin/env python3
"""Upload a built artifact to VirusTotal and print the detection ratio.

Handy after a release to see how many AV engines flag the Nuitka/Inno build
(and to watch that number shrink as reputation builds). Purely informational —
it does not touch the app or the release.

Usage:
    VT_API_KEY=<your free virustotal key> python scripts/av_check.py <file>

Needs only the stdlib + `requests`. A free VirusTotal key allows a handful of
lookups per minute, which is plenty for release checks.
"""

from __future__ import annotations

import hashlib
import os
import sys
import time

try:
    import requests
except ImportError:
    sys.exit("Please `pip install requests` to run this check.")

API = "https://www.virustotal.com/api/v3"


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    if len(sys.argv) != 2:
        sys.exit("usage: av_check.py <file>")
    path = sys.argv[1]
    key = os.environ.get("VT_API_KEY")
    if not key:
        sys.exit("Set VT_API_KEY (free key from https://www.virustotal.com/).")
    if not os.path.isfile(path):
        sys.exit(f"no such file: {path}")

    headers = {"x-apikey": key}
    digest = sha256(path)
    print(f"sha256: {digest}")

    # Look up an existing report first (free, instant); else upload.
    r = requests.get(f"{API}/files/{digest}", headers=headers, timeout=60)
    if r.status_code == 404:
        print("not seen before — uploading (large files may take a minute) ...")
        with open(path, "rb") as fh:
            up = requests.post(
                f"{API}/files",
                headers=headers,
                files={"file": (os.path.basename(path), fh)},
                timeout=600,
            )
        up.raise_for_status()
        analysis_id = up.json()["data"]["id"]
        while True:
            time.sleep(15)
            a = requests.get(f"{API}/analyses/{analysis_id}", headers=headers, timeout=60).json()
            if a["data"]["attributes"]["status"] == "completed":
                break
            print("  ... scanning")
        r = requests.get(f"{API}/files/{digest}", headers=headers, timeout=60)

    r.raise_for_status()
    stats = r.json()["data"]["attributes"]["last_analysis_stats"]
    total = sum(stats.values())
    flagged = stats.get("malicious", 0) + stats.get("suspicious", 0)
    print(f"detections: {flagged}/{total}  ({stats})")
    print(f"report: https://www.virustotal.com/gui/file/{digest}")
    # Non-zero exit if a lot of engines flag it, so CI could gate on it later.
    return 1 if flagged >= 10 else 0


if __name__ == "__main__":
    raise SystemExit(main())
