#!/usr/bin/env bash
# Contract: bin/model-catalog turns the provider's own published facts into the
# harness's model policy input, and NEVER guesses one.
#
# Why this exists: bin/oll-sync::ctx_for() guessed context by family prefix, so
# deepseek-v4-pro:0813 (really 1M) was registered in OpenCode at 200K and
# glm-5.2 (really 976K) at 200K (lq-17b20b52). Worse, commit a7e7a7b9 made
# deepseek-v4-pro the routine-fanout default calling it "medium usage" when the
# provider says "Extra High Usage" (lq-faefaa8c). Both failures are the same
# shape: a model FACT that nothing measured. A guess that is silently wrong is
# worse than an UNKNOWN that is loudly right, so unknown must stay unknown here.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
FIX="$ROOT/tests/model-catalog/fixtures"

python3 - "$ROOT/bin/model-catalog" "$FIX" <<'PY'
import runpy, sys

mod = runpy.run_path(sys.argv[1])
FIX = sys.argv[2]

for name in ("parse_rows", "row_name_for", "parse_context", "USAGE_ORDER",
             "usage_rank", "facts_from_page"):
    assert name in mod, f"C0 FAIL: bin/model-catalog exposes no {name}"

parse_rows = mod["parse_rows"]
row_name_for = mod["row_name_for"]
parse_context = mod["parse_context"]
usage_rank = mod["usage_rank"]
facts_from_page = mod["facts_from_page"]

# ---- C1: exact facts out of a real saved page, no interpretation ------------
qwen = open(f"{FIX}/qwen3.5.html").read()
rows = parse_rows(qwen)
assert "qwen3.5:397b-cloud" in rows, f"C1 FAIL: cloud row missing: {sorted(rows)[:5]}"
r = rows["qwen3.5:397b-cloud"]
assert r["usage_label"] == "Medium Usage", f"C1 FAIL: {r!r}"
assert r["context"] == 256_000, f"C1 FAIL: context {r['context']!r}"
assert r["modalities"] == ["Text", "Image"], f"C1 FAIL: modalities {r['modalities']!r}"

# A local (downloadable) row carries a SIZE, not a usage class. It must not be
# mistaken for a cloud row -- that is how a 6.6GB laptop build could end up
# quoted as the policy for a 397B cloud worker.
assert rows["qwen3.5:9b"]["usage_label"] is None, "C1 FAIL: local row got a usage class"
assert rows["qwen3.5:9b"]["size"] == "6.6GB", f"C1 FAIL: {rows['qwen3.5:9b']!r}"

# ---- C2: the two live facts this whole session turned on --------------------
pro = parse_rows(open(f"{FIX}/deepseek-v4-pro.html").read())
assert pro["deepseek-v4-pro:0813-cloud"]["usage_label"] == "Extra High Usage", \
    "C2 FAIL: the Extra-High reading that contradicts commit a7e7a7b9 did not survive"
assert pro["deepseek-v4-pro:0813-cloud"]["context"] == 1_000_000, \
    "C2 FAIL: v4-pro context is 1M, not the 200K oll-sync guessed"

flash = parse_rows(open(f"{FIX}/glm-5.3-flash.html").read())
assert flash["glm-5.3-flash:cloud"]["usage_label"] == "Medium Usage"
assert flash["glm-5.3-flash:cloud"]["context"] == 1_000_000

# ---- C3: id -> row-name mapping, both tag shapes ----------------------------
assert row_name_for("qwen3.5:397b") == "qwen3.5:397b-cloud"
assert row_name_for("glm-5.3-flash") == "glm-5.3-flash:cloud"
assert row_name_for("deepseek-v4-pro:0813") == "deepseek-v4-pro:0813-cloud"

# ---- C4: UNKNOWN stays UNKNOWN. This is the anti-ctx_for assertion. ---------
missing = facts_from_page("glm-5.2", flash)          # right shape, wrong page
assert missing is None, f"C4 FAIL: fabricated facts for an absent model: {missing!r}"
assert facts_from_page("qwen3.5:397b", parse_rows("")) is None, \
    "C4 FAIL: an empty page produced facts"
assert facts_from_page("qwen3.5:397b", parse_rows("<html><body>garbage")) is None, \
    "C4 FAIL: a malformed page produced facts"

# ---- C5: context units ------------------------------------------------------
assert parse_context("1M context window") == 1_000_000
assert parse_context("976K context window") == 976_000
assert parse_context("256K context window") == 256_000
assert parse_context("no number here") is None, "C5 FAIL: invented a context"

# ---- C6: usage classes are ORDERED, so a policy ceiling is expressible ------
assert mod["USAGE_ORDER"] == ("Low Usage", "Medium Usage", "High Usage", "Extra High Usage")
assert usage_rank("Medium Usage") < usage_rank("Extra High Usage")
assert usage_rank("Low Usage") < usage_rank("Medium Usage") < usage_rank("High Usage")
# An unrankable label must not silently sort as "cheapest" -- that would let an
# unknown model slip under a ceiling it was never measured against.
assert usage_rank(None) is None and usage_rank("Bogus Usage") is None, \
    "C6 FAIL: an unknown usage label was given a rank"
print("parse checks pass")
PY

# ---- C7: reading the cache must never touch the network ---------------------
python3 - "$ROOT/bin/model-catalog" <<'PY'
import json, os, runpy, sys, tempfile

mod = runpy.run_path(sys.argv[1])
main = mod["main"]
g = main.__globals__

def explode(*a, **k):
    raise AssertionError("C7 FAIL: a read path opened the network")
g["urllib"].request.urlopen = explode

with tempfile.TemporaryDirectory() as d:
    cache = os.path.join(d, "catalog.json")
    json.dump({"fetched_at": "2026-08-26T00:00:00Z", "models": {
        "glm-5.3-flash": {"usage_label": "Medium Usage", "context": 1000000,
                          "modalities": ["Text", "Image"]}}}, open(cache, "w"))
    old = sys.argv
    sys.argv = ["model-catalog", "show", "--cache", cache, "--json"]
    try:
        rc = main()
    finally:
        sys.argv = old
    assert rc == 0, f"C7 FAIL: show returned {rc}"
print("offline checks pass")
PY

# ---- C8/C9: the refresh path + every verb through main(), fully offline -----
# cmd_refresh / live_model_ids / fetch / cmd_show / cmd_check / cmd_limits /
# main had (almost) ZERO automated coverage (lq-166f10d2: a --scope all
# mutation sweep left ~104 survivors, every one in this region); the only
# validation was a manual real-path run. These blocks drive the real CLI
# surface offline: urlopen is stubbed to serve the SAME saved fixture pages
# C1/C2 parse, and load_key runs its real code path against a temp auth store
# (never the machine's).
python3 - "$ROOT/bin/model-catalog" "$FIX" <<'PY'
import contextlib, io, json, os, runpy, sys, tempfile, urllib.error

mod = runpy.run_path(sys.argv[1])
FIX = sys.argv[2]
g = mod["cmd_refresh"].__globals__   # the LIVE namespace (mod itself is a copy)

pages = {
    "https://ollama.com/library/qwen3.5": open(f"{FIX}/qwen3.5.html").read(),
    "https://ollama.com/library/deepseek-v4-pro": open(f"{FIX}/deepseek-v4-pro.html").read(),
    "https://ollama.com/library/glm-5.3-flash": open(f"{FIX}/glm-5.3-flash.html").read(),
}

class Resp:
    def __init__(self, body): self.body = body.encode("utf-8")
    def read(self): return self.body
    def __enter__(self): return self
    def __exit__(self, *a): return False

def make_stub(live_ids):
    def stub(req, timeout=None, context=None):
        url = req.full_url
        if url == "https://ollama.com/v1/models":
            auth = req.get_header("Authorization")
            assert auth == "Bearer test-key-c8", \
                f"C8 FAIL: catalog fetch lost its auth header: {auth!r}"
            return Resp(json.dumps({"data": [{"id": i} for i in live_ids]}))
        if "broken-model" in url:
            raise urllib.error.URLError("stubbed connection failure")
        assert url in pages, f"C8 FAIL: unexpected URL fetched: {url}"
        return Resp(pages[url])
    return stub

def run(argv):
    """Drive the real CLI surface — argparse dispatch included, not bare cmd_*."""
    old = sys.argv
    sys.argv = ["model-catalog"] + argv
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            rc = g["main"]()
    finally:
        sys.argv = old
    return rc, out.getvalue()

with tempfile.TemporaryDirectory() as d:
    auth = os.path.join(d, "auth.json")
    json.dump({"ollama-cloud": {"key": "test-key-c8"}}, open(auth, "w"))
    # the tool's default key source is OpenCode's auth store — the single-key-
    # source rule — so pin it before this contract redirects to a temp store
    assert g["AUTH_JSON"].endswith("/.local/share/opencode/auth.json"), \
        f"C8 FAIL: default auth path is not OpenCode's store: {g['AUTH_JSON']!r}"
    g["AUTH_JSON"] = auth               # load_key's REAL path, temp store

    # ---- C8: partial coverage -> exit 3, BOTH unknown branches, atomic write
    cache = os.path.join(d, "sub", "catalog.json")   # dir must be CREATED by refresh
    ids = ["qwen3.5:397b", "deepseek-v4-pro:0813", "glm-5.3-flash",
           "qwen3.5:398b",      # page loads, row absent -> UNKNOWN (no-row branch)
           "broken-model"]      # library fetch URLError  -> UNKNOWN (except branch)
    g["urllib"].request.urlopen = make_stub(ids)
    rc, out = run(["refresh", "--cache", cache])
    assert rc == 3, f"C8a FAIL: unknowns must exit 3, got {rc}"
    # substance, not prose: the resolve COUNTS and the unknown ID LIST are the
    # required behavior; the sentence wording is an implementation accident.
    assert "3/5" in out, f"C8a FAIL: resolve count lost: {out!r}"
    assert "broken-model, qwen3.5:398b" in out, \
        f"C8a FAIL: unknowns not announced on stdout: {out!r}"
    assert os.path.exists(cache), "C8b FAIL: cache not written"
    assert not os.path.exists(cache + ".tmp"), "C8b FAIL: leftover .tmp — write not atomic"
    raw = open(cache).read()
    assert raw.endswith("\n"), "C8b FAIL: cache must end in a newline"

    p = json.loads(raw)
    for k in ("fetched_at", "source", "live_ids", "unknown", "models"):
        assert k in p, f"C8c FAIL: payload missing {k!r}"
    assert p["live_ids"] == sorted(ids), f"C8c FAIL: live_ids {p['live_ids']!r}"
    assert p["unknown"] == ["broken-model", "qwen3.5:398b"], \
        f"C8d FAIL: unknown list {p['unknown']!r}"
    assert not set(p["unknown"]) & set(p["models"]), \
        "C8d FAIL: an UNKNOWN id also has facts — unknown must stay unknown"

    q = p["models"]["qwen3.5:397b"]
    assert (q["usage_label"], q["context"]) == ("Medium Usage", 256_000), f"C8e FAIL: {q!r}"
    assert q["modalities"] == ["Text", "Image"] and q["vision"] is True, f"C8e FAIL: {q!r}"
    assert q["usage_rank"] == 1 and q["output"] is None, f"C8e FAIL: {q!r}"
    dv = p["models"]["deepseek-v4-pro:0813"]
    assert (dv["usage_label"], dv["context"]) == ("Extra High Usage", 1_000_000), f"C8e FAIL: {dv!r}"
    # a text-only model must read vision False — with only vision-True models
    # in the fixture set, an inverted `in ("image","video")` stays green
    assert dv["modalities"] == ["Text"] and dv["vision"] is False, f"C8e FAIL: {dv!r}"
    gl = p["models"]["glm-5.3-flash"]
    assert (gl["usage_label"], gl["context"]) == ("Medium Usage", 1_000_000), f"C8e FAIL: {gl!r}"

    # check: sees the SAME gap refresh reported; a missing cache is 2, not a crash
    assert run(["check", "--cache", cache])[0] == 3, \
        "C8f FAIL: check must exit 3 on an uncovered live id"
    assert run(["check", "--cache", cache + ".nope"])[0] == 2, \
        "C8f FAIL: check on a missing cache must exit 2"

    # limits: known model prints its context; unknown 3; missing cache 2
    rc, out = run(["limits", "glm-5.3-flash", "--cache", cache])
    assert rc == 0, "C8g FAIL: limits(known) must exit 0"
    assert out.strip() == "1000000", f"C8g FAIL: limits printed {out!r}"
    assert run(["limits", "broken-model", "--cache", cache])[0] == 3, \
        "C8g FAIL: limits(unknown) must exit 3"
    assert run(["limits", "glm-5.3-flash", "--cache", cache + ".nope"])[0] == 2, \
        "C8g FAIL: limits on a missing cache must exit 2"

    # show: table mode renders facts cheapest-first and names the unknowns
    rc, out = run(["show", "--cache", cache])
    assert rc == 0, f"C8h FAIL: show returned {rc}"
    for needle in ("deepseek-v4-pro:0813", "Extra High Usage",
                   "broken-model, qwen3.5:398b",
                   "Text, Image"):   # a row's modalities column renders the list, not '-'
        assert needle in out, f"C8h FAIL: table lost {needle!r}: {out!r}"
    assert out.index("glm-5.3-flash") < out.index("qwen3.5:397b") \
        < out.index("deepseek-v4-pro:0813"), "C8h FAIL: not sorted cheapest-first"
    # --model narrows to exactly one model; an UNKNOWN id is 3, not empty facts
    rc, out = run(["show", "--cache", cache, "--model", "qwen3.5:397b", "--json"])
    assert rc == 0 and set(json.loads(out)) == {"qwen3.5:397b"}, \
        f"C8h FAIL: filtered show wrong: {out!r}"
    assert run(["show", "--cache", cache, "--model", "broken-model"])[0] == 3, \
        "C8h FAIL: show of an UNKNOWN model must exit 3"
    assert run(["show", "--cache", cache + ".nope"])[0] == 2, \
        "C8h FAIL: show on a missing cache must exit 2"

    # a row with no modality field parses to [] (never an unbound variable)
    mini = ('<a href="/library/x"><span><p>x:cloud</p></span>'
            '<p class="flex text-neutral-500">Medium Usage · 1M context window</p></a>')
    assert g["parse_rows"](mini)["x:cloud"]["modalities"] == [], \
        "C8i FAIL: a modality-less row did not parse to []"
    # the modality scan is positional-tolerant BY DESIGN (skip context/age parts
    # wherever they sit) — a fixed-index read must fail this reordered row
    mini2 = ('<a href="/library/y"><span><p>y:cloud</p></span>'
             '<p class="flex text-neutral-500">Medium Usage · Text · 1M context window</p></a>')
    assert g["parse_rows"](mini2)["y:cloud"]["modalities"] == ["Text"], \
        "C8i FAIL: a modality part before the context part was not scanned"

    # a verbless invocation is an argparse usage error (exit 2), never a crash
    try:
        run([])
        raise AssertionError("C8k FAIL: a verbless invocation must be rejected")
    except SystemExit as ex:
        assert ex.code == 2, f"C8k FAIL: argparse must exit 2, got {ex.code!r}"

    # ---- C9: full coverage -> exit 0 and check agrees
    cache2 = os.path.join(d, "catalog2.json")
    g["urllib"].request.urlopen = make_stub(
        ["qwen3.5:397b", "deepseek-v4-pro:0813", "glm-5.3-flash"])
    rc, out = run(["refresh", "--cache", cache2])
    assert rc == 0, f"C9a FAIL: full coverage must exit 0, got {rc}"
    assert "3/3" in out, f"C9a FAIL: resolve count lost: {out!r}"
    assert not os.path.exists(cache2 + ".tmp"), "C9a FAIL: leftover .tmp"
    p2 = json.load(open(cache2))
    assert p2["unknown"] == [] and set(p2["models"]) == set(p2["live_ids"]), \
        f"C9b FAIL: full coverage payload wrong: unknown={p2['unknown']!r}"
    assert run(["check", "--cache", cache2])[0] == 0, \
        "C9c FAIL: check must exit 0 on full coverage"

    # a keyless auth store must ABORT refresh — never fetch anonymously
    json.dump({"ollama-cloud": {}}, open(auth, "w"))
    try:
        run(["refresh", "--cache", cache2])
        raise AssertionError("C9d FAIL: a keyless auth store did not abort refresh")
    except SystemExit as ex:
        assert "ollama-cloud" in str(ex), f"C9d FAIL: wrong abort: {ex!r}"

    # an UNREADABLE auth store aborts just as loudly (never a NameError later)
    open(auth, "w").write("not json{")
    try:
        run(["refresh", "--cache", cache2])
        raise AssertionError("C9e FAIL: an unreadable auth store did not abort refresh")
    except SystemExit as ex:
        assert "ERROR reading" in str(ex), f"C9e FAIL: wrong abort: {ex!r}"

print("refresh-path offline checks pass")
PY

echo "model-catalog contract: PASS"
