#!/usr/bin/env python3
import json
import os
import sys
import time


def emit(kind, message_id, part_type, **part):
    session_id = part.pop("sessionID", "ses_test")
    payload = {
        "type": kind,
        "timestamp": 1,
        "sessionID": session_id,
        "part": {
            "id": f"prt_{kind}_{message_id}",
            "messageID": message_id,
            "sessionID": session_id,
            "type": part_type,
            **part,
        },
    }
    print(json.dumps(payload, separators=(",", ":")), flush=True)


args = sys.argv[1:]
record = os.environ.get("OCC_FAKE_ARGS")
if record:
    with open(record, "w", encoding="utf-8") as destination:
        destination.write("\n".join(args) + "\n")
mode = args[-1] if args else ""

if mode == "default-noisy":
    print("\033[32mworker progress\033[0m")
    for index in range(1, 9):
        print(json.dumps({"id": f"A{index}"}))
    print("{}")
    raise SystemExit(0)

if "--pure" not in args or "--format" not in args or "json" not in args:
    print("fake-opencode: structured flags missing", file=sys.stderr)
    raise SystemExit(9)

if mode == "nonzero":
    print("model failed", file=sys.stderr)
    raise SystemExit(7)
if mode == "timeout":
    time.sleep(30)
    raise SystemExit(0)
if mode == "malformed":
    print("{not-json")
    raise SystemExit(0)
if mode == "unknown-event":
    print("{}")
    raise SystemExit(0)

emit("step_start", "msg_1", "step-start")
if mode == "mixed-session":
    emit("text", "msg_1", "text", sessionID="ses_other", text="WRONG")
    emit("step_finish", "msg_1", "step-finish", reason="stop")
    raise SystemExit(0)
if mode == "error-event":
    emit("error", "msg_1", "error", error="fixture")
    raise SystemExit(0)
if mode == "missing-text":
    emit("step_finish", "msg_1", "step-finish", reason="stop")
    raise SystemExit(0)
if mode == "ambiguous":
    emit("text", "msg_1", "text", text="ONE")
    emit("text", "msg_1", "text", text="TWO")
    emit("step_finish", "msg_1", "step-finish", reason="stop")
    raise SystemExit(0)
if mode == "assistant-bad-jsonl":
    emit("text", "msg_1", "text", text='{"id":1}\n{}')
    emit("step_finish", "msg_1", "step-finish", reason="stop")
    raise SystemExit(0)
if mode == "oversized-final":
    emit("text", "msg_1", "text", text="x" * (1024 * 1024 + 1))
    emit("step_finish", "msg_1", "step-finish", reason="stop")
    raise SystemExit(0)
if mode == "oversized-stream":
    for _ in range(10):
        emit("reasoning", "msg_1", "reasoning", text="x" * (900 * 1024))
    emit("text", "msg_1", "text", text="NEVER_EMIT")
    emit("step_finish", "msg_1", "step-finish", reason="stop")
    raise SystemExit(0)

emit("text", "msg_1", "text", text="EARLY_PROGRESS")
emit("tool_use", "msg_1", "tool", tool="read", state={"status": "completed"})
emit("step_finish", "msg_1", "step-finish", reason="tool-calls")
emit("step_start", "msg_2", "step-start")
emit("reasoning", "msg_2", "reasoning", text="HIDDEN_REASONING")
emit("text", "msg_2", "text", text="FINAL_ONE\nFINAL_TWO\r\n")
emit("step_finish", "msg_2", "step-finish", reason="stop")
