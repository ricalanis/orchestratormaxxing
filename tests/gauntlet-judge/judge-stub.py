#!/usr/bin/env python3
"""Offline judge stub for tests/gauntlet-judge. Replaces `oll` via GAUNTLET_OLL.

Scans the blinded package on stdin for sentinel strings to decide its answer in
Artifact-1/2 terms — which exercises the tool's unblinding bookkeeping (the
test controls G/R, the stub only ever sees labels). Scenario via
GAUNTLET_TEST_SCENARIO; every invocation appends to GAUNTLET_TEST_MARKER so
tests can assert that policy refusals never reached a judge.
"""
import json, os, re, sys

data = sys.stdin.read()
argv = sys.argv[1:]
model = argv[argv.index("--model") + 1] if "--model" in argv else "?"
marker = os.environ.get("GAUNTLET_TEST_MARKER")
if marker:
    open(marker, "a").write(model + "\n")

scenario = os.environ.get("GAUNTLET_TEST_SCENARIO", "g_better")

body1 = data.split("=== ARTIFACT 1 ===\n", 1)[1].split("=== ARTIFACT 2 ===\n", 1)[0]
body2 = data.split("=== ARTIFACT 2 ===\n", 1)[1].split("=== END ARTIFACTS ===", 1)[0]
ids = [c["id"] for c in json.loads(re.search(r"RUBRIC \(JSON\): (\[.*?\])\n", data).group(1))]

def num_with(sentinel):
    if sentinel in body1:
        return "1"
    if sentinel in body2:
        return "2"
    return None

g_num, verdict = num_with("SENTINEL_G"), "tie"
grades = {cid: {"artifact1": True, "artifact2": True, "evidence": "line 1"} for cid in ids}

if scenario == "invalid":
    print("this is not the JSON you are looking for")
    sys.exit(0)
elif scenario == "position_bias":
    verdict = "1"  # always favors whatever is first — must trip order-stability
elif scenario == "split":
    # one judge family favors G both orders, the other favors R — a 2-2 split
    other = {"1": "2", "2": "1"}.get(g_num)
    verdict = g_num if "qwen" in model else other
elif scenario in ("g_better", "load_bearing_loss") and g_num:
    verdict = g_num
    loser = {"1": "2", "2": "1"}[g_num]
    for cid in ids:
        grades[cid]["artifact" + loser] = False
    if scenario == "load_bearing_loss":  # G loses the load-bearing criterion
        grades["lb1"]["artifact" + g_num] = False
        grades["lb1"]["artifact" + loser] = True
elif scenario == "canary_null_wins" and num_with("N/A") and body1.strip() != body2.strip():
    verdict = num_with("N/A")  # degenerate judge: the null artifact "wins"
elif body1.strip() != body2.strip():
    null_num = "1" if body1.strip() == "N/A" else "2" if body2.strip() == "N/A" else None
    if null_num:  # honest judge: the null artifact loses
        verdict = {"1": "2", "2": "1"}[null_num]

print(json.dumps({
    "grades": grades,
    "verdict": verdict,
    "deciding_criterion": None if verdict == "tie" else ids[0],
    "refutations": {"artifact1": "cannot refute" if verdict == "1" else "misses the flag list",
                    "artifact2": "cannot refute" if verdict == "2" else "misses the flag list"},
}))
