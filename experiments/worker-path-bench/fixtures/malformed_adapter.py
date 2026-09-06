#!/usr/bin/env python3
import json
import sys

case = json.load(sys.stdin)
mode = case.get("fixture_mode")
if mode == "nonzero":
    raise SystemExit(7)
if mode == "bad_usage":
    json.dump({"output": "PONG", "usage": [], "events": []}, sys.stdout)
elif mode == "bad_events":
    json.dump({"output": "PONG", "usage": {}, "events": {}}, sys.stdout)
else:
    sys.stdout.write("not-json")
