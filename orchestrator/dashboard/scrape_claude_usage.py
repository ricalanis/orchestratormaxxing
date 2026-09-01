#!/usr/bin/env python3
"""
Scrape Claude Max usage by running `claude -p "/usage"`.

Parses session and weekly usage percentages, then writes to the same store
the dashboard reads: ~/.local/share/orchestratormaxxing/claude-usage.json

Exit codes: 0 = OK · 3 = parse error · 4 = claude CLI not found
"""
import json
import re
import subprocess
import sys
import time
from pathlib import Path

STORE = Path.home() / ".local" / "share" / "orchestratormaxxing" / "claude-usage.json"


def _write(payload: dict) -> None:
    STORE.parent.mkdir(parents=True, exist_ok=True)
    STORE.write_text(json.dumps(payload, indent=2))


def main() -> int:
    claude_bin = subprocess.run(["which", "claude"], capture_output=True, text=True)
    if claude_bin.returncode != 0:
        print("claude CLI not found", file=sys.stderr)
        return 4

    result = subprocess.run(
        ["claude", "-p", "/usage"],
        capture_output=True, text=True, timeout=30
    )
    output = result.stdout

    if not output:
        print(f"empty output. stderr: {result.stderr[:200]}", file=sys.stderr)
        return 3

    # Parse: "Current session: 13% used · resets Jul 4, 5:10pm (America/Monterrey)"
    session_pct = None
    session_reset = None
    m = re.search(r"Current session:\s*(\d+(?:\.\d+)?)\s*%\s*used", output, re.I)
    if m:
        session_pct = float(m.group(1))
    sr = re.search(r"Current session:.*?resets\s+(.+?)(?:\n|$)", output, re.I)
    if sr:
        session_reset = sr.group(1).strip()

    # Parse: "Current week (all models): 6% used · resets Jul 10, 10pm"
    weekly_pct = None
    weekly_reset = None
    m = re.search(r"Current week\s*\(all models\):\s*(\d+(?:\.\d+)?)\s*%\s*used", output, re.I)
    if m:
        weekly_pct = float(m.group(1))
    wr = re.search(r"Current week\s*\(all models\):.*?resets\s+(.+?)(?:\n|$)", output, re.I)
    if wr:
        weekly_reset = wr.group(1).strip()

    # Parse Fable weekly separately
    fable_pct = None
    m = re.search(r"Current week\s*\(Fable\):\s*(\d+(?:\.\d+)?)\s*%\s*used", output, re.I)
    if m:
        fable_pct = float(m.group(1))

    ok = session_pct is not None or weekly_pct is not None

    payload = {
        "ok": ok,
        "scraped_at": int(time.time()),
        "session_pct": session_pct,
        "session_resets_at": session_reset,
        "weekly_pct": weekly_pct,
        "weekly_resets_at": weekly_reset,
        "fable_weekly_pct": fable_pct,
        "raw_text": output[:4000],
    }
    _write(payload)

    if not ok:
        print(f"couldn't parse usage — inspect {STORE}", file=sys.stderr)
        return 3

    print(f"claude: session={session_pct}% weekly={weekly_pct}% "
          f"fable_weekly={fable_pct}% → {STORE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())