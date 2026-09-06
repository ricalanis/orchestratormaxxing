#!/usr/bin/env python3
"""Deterministic fixture exposing context telemetry for the benchmark contract."""
import json
import re
import sys


case = json.load(sys.stdin)
match = re.search(r"PROTECTED FACT:\s*(\S+)", case["prompt"])
if not match:
    raise SystemExit("missing protected fact")
json.dump({
    "output": match.group(1),
    "usage": {"prompt_tokens": 1234, "completion_tokens": 2},
    "events": [
        {"type": "compaction", "cache_read_tokens": 300},
        {"type": "completed", "cache_read_tokens": 200},
        {"type": "usage", "cache_read_tokens": 100},
    ],
}, sys.stdout)
