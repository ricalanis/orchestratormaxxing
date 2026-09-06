#!/usr/bin/env python3
import json
import sys

case = json.load(sys.stdin)
if case["id"] == "exact-ping":
    output = "PONG"
else:
    output = json.dumps({"ok": True, "value": 7, "extra": "allowed"})
json.dump({
    "output": output,
    "usage": {"prompt_tokens": 3, "completion_tokens": 2},
    "events": [{"type": "completed"}],
}, sys.stdout)
