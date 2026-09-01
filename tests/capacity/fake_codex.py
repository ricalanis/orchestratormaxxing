#!/usr/bin/env python3
"""Hermetic Codex app-server fixture for the capacity CLI contract."""

import json
import os
import sys


def emit(value):
    sys.stdout.write(json.dumps(value, separators=(",", ":")) + "\n")
    sys.stdout.flush()


mode = os.environ.get("CAPACITY_FAKE_CODEX_MODE", "ok")
log_path = os.environ.get("CAPACITY_FAKE_CODEX_LOG")

for line in sys.stdin:
    try:
        message = json.loads(line)
    except json.JSONDecodeError:
        continue
    if log_path:
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(message, sort_keys=True) + "\n")
    request_id = message.get("id")
    if message.get("method") == "initialize":
        emit({"id": request_id, "result": {
            "userAgent": "capacity-fixture/1", "codexHome": "/fixture",
            "platformFamily": "unix", "platformOs": "linux",
        }})
    elif message.get("method") == "account/rateLimits/read":
        if mode == "malformed":
            emit({"id": request_id, "result": {"rateLimits": "wrong"}})
            continue
        emit({"id": request_id, "result": {
            "rateLimits": {
                "limitId": "codex", "limitName": None,
                "primary": {
                    "usedPercent": 69,
                    "windowDurationMins": 10080,
                    "resetsAt": 1787014200,
                },
                "secondary": None,
                "credits": {
                    "hasCredits": True, "unlimited": False, "balance": "2500",
                },
                "planType": "pro",
            },
            "rateLimitsByLimitId": {},
            "rateLimitResetCredits": {"availableCount": 2, "credits": []},
        }})

