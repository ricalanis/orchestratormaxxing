#!/usr/bin/env python3
"""Acceptance contract for bin/cogload.

Runs offline with an injected event source — no X server, no daemon. The one
check that must cross a real boundary (`cogload selftest`) is deliberately NOT
here: a fully-mocked suite is a statement about the mocks, not the system.

C1 is the load-bearing test. Note what it asserts and what it deliberately does
NOT: it CANNOT assert "no key name appears in the store", because the class
labels `backspace` / `delete` / `enter` ARE key names and are legitimately part
of the schema. Asserting their absence would be unsatisfiable. What actually
matters is: (a) the schema is closed, (b) the number of persisted rows scales
with WINDOWS, not with KEYSTROKES — so no per-event record exists to
reconstruct from, and (c) no typed content reaches the store, stdout or stderr.
"""

import importlib.machinery
import importlib.util
import ctypes
import ctypes.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TOOL = REPO / "bin" / "cogload"

FAILURES: list[str] = []
PASSES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASSES if ok else FAILURES).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not ok else ""))


def load(store: Path):
    """Import bin/cogload as a module with COGLOAD_DIR pointed at a temp store."""
    os.environ["COGLOAD_DIR"] = str(store)
    for m in list(sys.modules):
        if m == "cogload":
            del sys.modules[m]
    spec = importlib.util.spec_from_loader(
        "cogload", importlib.machinery.SourceFileLoader("cogload", str(TOOL)))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cogload"] = mod
    spec.loader.exec_module(mod)
    return mod


class FakeKey:
    """Stands in for a pynput key object."""

    def __init__(self, name=None, char=None):
        if name is not None:
            self.name = name
        self.char = char


SENTINEL = "correcthorsebatterystaple"


# --------------------------------------------------------------- C1 no-content

def c1_no_content(store: Path) -> None:
    print("\nC1 — no keystroke content is recoverable")
    cg = load(store)

    # Type the sentinel, with realistic corrections, across 3 windows.
    typed = 0
    for w in range(3):
        win = cg.Window()
        for ch in SENTINEL:
            win.on_key(cg.classify(FakeKey(char=ch)), 1000.0 + typed * 0.1)
            typed += 1
        for _ in range(4):
            win.on_key(cg.classify(FakeKey(name="backspace")), 1000.0 + typed * 0.1)
            typed += 1
        win.on_key(cg.classify(FakeKey(name="enter")), 1000.0 + typed * 0.1)
        typed += 1
        cg.write_record(win.record(cg._now(), "test", "x11", "ok"))

    files = list(store.rglob("keys-*.jsonl"))
    rows = [json.loads(l) for f in files for l in f.read_text().splitlines()]

    check("3 windows -> exactly 3 rows (not 1 row per keystroke)",
          len(rows) == 3, f"got {len(rows)} rows for {typed} keystrokes")
    check("row count is independent of keystroke count",
          len(rows) < typed, f"{len(rows)} rows vs {typed} keys")

    closed = all(set(r) <= cg.RECORD_FIELDS for r in rows)
    check("schema is closed (no field outside the whitelist)", closed)

    blob = "\n".join(f.read_text() for f in files)
    check("sentinel text absent from the store", SENTINEL not in blob)

    # A naive substring search is NOT sufficient and must not be trusted alone:
    # a leak that stores characters individually (["c","o","r",...]) is fully
    # reconstructable yet contains no contiguous sentinel. This was proven
    # empirically — an earlier version of this test passed against a seeded
    # leaky collector. The assertions below are structural instead.
    def all_strings(o):
        if isinstance(o, str):
            yield o
        elif isinstance(o, dict):
            for v in o.values():
                yield from all_strings(v)
        elif isinstance(o, (list, tuple)):
            for v in o:
                yield from all_strings(v)

    flat = "".join(s for r in rows for s in all_strings(r))
    check("sentinel absent after flattening every string value (defeats "
          "character-splitting leaks)", SENTINEL not in flat)

    def seqs(o):
        if isinstance(o, dict):
            for v in o.values():
                yield from seqs(v)
        elif isinstance(o, (list, tuple)):
            yield o
            for v in o:
                yield from seqs(v)

    longest = max((len(s) for r in rows for s in seqs(r)), default=0)
    check("no per-event sequence survives into a row", longest == 0,
          f"found a sequence of length {longest}")

    # The decisive invariant: a record's SIZE must not grow with keystrokes.
    # Any per-event accumulation violates this regardless of field naming.
    small, big = cg.Window(), cg.Window()
    for i in range(10):
        small.on_key(cg.classify(FakeKey(char="a")), 1.0 + i * 0.1)
    for i in range(2000):
        big.on_key(cg.classify(FakeKey(char="a")), 1.0 + i * 0.1)
    s_len = len(json.dumps(small.record(cg._now(), "t", "x11", "ok")))
    b_len = len(json.dumps(big.record(cg._now(), "t", "x11", "ok")))
    check("record size is independent of keystroke volume "
          "(10 keys vs 2000 keys)", abs(b_len - s_len) < 40,
          f"{s_len}B vs {b_len}B — record grows with input, so it retains events")

    # The writer must actively refuse a widened record.
    try:
        bad = rows[0].copy()
        bad["keystrokes"] = ["a", "b"]
        cg.write_record(bad)
        check("writer refuses a non-whitelisted field", False, "it accepted one")
    except ValueError:
        check("writer refuses a non-whitelisted field", True)

    # classify() must return only class names, never identity, for any input.
    got = {cg.classify(FakeKey(char=c)) for c in "abcXYZ019!@#"}
    check("classify() collapses all printable keys to 'other'", got == {"other"},
          f"got {got}")

    # stdout/stderr must not leak content either.
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        cg.main(["show", "-n", "50"])
    check("sentinel absent from `show` output",
          SENTINEL not in out.getvalue() + err.getvalue())


# ------------------------------------------------------- C2 anti-silent-zero

def c2_anti_silent_zero(store: Path) -> None:
    print("\nC2 — silence and blindness are different rows")
    cg = load(store)

    idle = cg.Window()
    rec = idle.record(cg._now(), "test", "x11", "ok")
    check("idle window -> status ok with total 0",
          rec["status"] == "ok" and rec["total"] == 0)

    broken = cg.Window()
    broken.screen_ok = False
    broken.screen_reason = "xlib-read-failed:Test"
    brec = broken.record(cg._now(), "test", "x11", "degraded:listener-died")
    check("broken window -> status degraded, never silent zero",
          brec["status"].startswith("degraded") and brec["total"] == 0)
    check("a degraded row is distinguishable from an idle row",
          rec["status"] != brec["status"])
    check("screen sampler reports unavailable rather than fake zeros",
          brec["screen"]["available"] is False and "reason" in brec["screen"])

    # Regression: _read_records must return CHRONOLOGICAL order. It once built
    # its day list newest-first, so _last_record() (recs[-1]) returned
    # YESTERDAY's final row and a healthy collector reported itself stale by
    # exactly one day. A false DEGRADED is as costly as a false OK.
    y = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    t = datetime.now().strftime("%Y-%m-%d")
    for day, hh in ((y, "23"), (t, "00")):
        p = store / "keys" / day[:7]
        p.mkdir(parents=True, exist_ok=True)
        r = dict(rec)
        r["ts"] = f"{day}T{hh}:00:00+00:00"
        (p / f"keys-{day}.jsonl").write_text(json.dumps(r) + "\n")
    got = cg._read_records(days=2)
    check("_read_records returns oldest-first (chronological)",
          len(got) >= 2 and got[0]["ts"] < got[-1]["ts"],
          f"got {[g['ts'] for g in got]}")
    check("_last_record() is the NEWEST row, not yesterday's",
          cg._last_record()["ts"].startswith(t))

    cg.write_record(rec)
    cg.write_record(brec)
    rc = cg.main(["status", "--gate"])
    check("status --gate is non-zero when the newest row is degraded", rc == 1,
          f"exit {rc}")


# --------------------------------------------------------- C3 Wayland degrade

def c3_wayland(store: Path) -> None:
    print("\nC3 — a display-stack switch degrades loudly")
    cg = load(store)
    src = TOOL.read_text()
    check("daemon does NOT read XDG_SESSION_TYPE from its own environ",
          'os.environ.get("XDG_SESSION_TYPE"' not in src and
          "os.environ['XDG_SESSION_TYPE'" not in src,
          "a process environ is fixed at exec; it can never observe a switch")
    check("daemon queries the live login session instead",
          "def live_session_type" in src and "loginctl" in src)
    check("non-x11 session marks the window degraded",
          "xrecord-partial" in src)

    # The fork-free fast path and the loginctl fallback must agree. If they
    # ever diverge, a host without /run/systemd/sessions would silently
    # classify its display server differently — and this check is the one
    # thing standing between XWayland and silently partial capture.
    check("reads logind state files instead of forking loginctl per session",
          "/run/systemd/sessions" in src and "scandir" in src)
    check("never opens the .ref FIFOs (opening a named pipe can block)",
          '"." in entry.name' in src)
    fast = cg.live_session_type()
    real_scandir = os.scandir
    try:
        os.scandir = lambda p: (_ for _ in ()).throw(OSError("no systemd"))
        cg._sess_cache["t"] = 0
        fallback = cg.live_session_type()
    finally:
        os.scandir = real_scandir
        cg._sess_cache["t"] = 0
    check("fast path and loginctl fallback agree",
          fast == fallback, f"fast={fast!r} fallback={fallback!r}")


# ------------------------------------------------------------- C5 governance

def c5_governance(store: Path) -> None:
    print("\nC5 — governance and the kill switch")
    cg = load(store)
    (store / "keys").mkdir(parents=True, exist_ok=True)
    probe = store / "keys" / "canary.jsonl"
    probe.write_text("x")

    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cg.main(["wipe"])
    check("wipe without --confirm exits 3", rc == 3, f"exit {rc}")
    check("wipe without --confirm deletes nothing", probe.exists())

    cg.DISABLED.write_text("off")
    rc = cg.main(["status", "--gate"])
    check("kill-switch sentinel makes status exit 2", rc == 2, f"exit {rc}")
    cg.DISABLED.unlink()

    out = io.StringIO()
    with redirect_stdout(out):
        cg.main(["show"])
    shown = out.getvalue()
    check("`show` self-describes the whitelist", "ts" in shown and "keys" in shown)
    check("`show` states what is NOT recorded", "NOT recorded" in shown)

    # A pynput-needing verb run from PATH must re-exec into the collector's
    # venv. The guard MUST compare sys.prefix, not resolved executable paths:
    # a venv's python3 is a symlink to the system interpreter, so .resolve()
    # makes both sides equal and silently swallows every re-exec.
    src = TOOL.read_text()
    check("re-exec guard compares sys.prefix, not resolved exe paths",
          "Path(sys.prefix) == (STORE" in src
          and "Path(sys.executable).resolve() == VENV_PY.resolve()" not in src)
    check("re-exec is loop-guarded by an env sentinel",
          "COGLOAD_NO_REEXEC" in src)
    check("only pynput-dependent verbs re-exec",
          'NEEDS_PYNPUT = {"daemon", "selftest"}' in src)

    # A globally importable but incompatible pynput must not bypass the venv.
    # This is the exact Mac/Python 3.13 failure: global pynput 1.7.6 imported,
    # then collided with threading.Thread._handle; the venv's 1.8.2 worked.
    fake_py = store / "venv/bin/python3"
    fake_py.parent.mkdir(parents=True, exist_ok=True)
    fake_py.write_text("")
    old_venv_py, old_prefix, old_execv = cg.VENV_PY, cg.sys.prefix, cg.os.execv
    old_sentinel = os.environ.pop("COGLOAD_NO_REEXEC", None)
    called = {}
    try:
        cg.VENV_PY = fake_py
        cg.sys.prefix = "/global-python-with-importable-pynput"
        cg.os.execv = lambda path, argv: called.update(path=path, argv=argv)
        cg._reexec_in_venv_if_needed(["selftest"])
    finally:
        cg.VENV_PY, cg.sys.prefix, cg.os.execv = old_venv_py, old_prefix, old_execv
        if old_sentinel is not None:
            os.environ["COGLOAD_NO_REEXEC"] = old_sentinel
        else:
            os.environ.pop("COGLOAD_NO_REEXEC", None)
    check("importable global pynput cannot bypass governed venv",
          called.get("path") == str(fake_py), repr(called))

    # A real Wayland/XWayland sample can be healthy without WM_CLASS. The
    # explicit third tuple field is authoritative; requiring a non-None app
    # would turn a valid no-properties sample into a false selftest failure.
    recovered = getattr(cg, "_screen_sample_recovered", lambda *_: False)
    check("selftest accepts a healthy sample without WM_CLASS",
          recovered((None, 1, True), True))
    check("selftest still rejects an explicit failed sample",
          not recovered((None, None, False), True))


# --------------------------------------------------------- C4 analyzer golden

def c4_analyzer(store: Path) -> None:
    print("\nC4 — analyzer aggregates without emitting content")
    cg = load(store)
    fake = store / "fixture-projects" / "-home-user-dev-x"
    fake.mkdir(parents=True, exist_ok=True)
    secret = "SECRETPROMPTBODY"
    rows = [
        {"type": "user", "timestamp": "2026-08-10T15:00:00.000Z",
         "cwd": "/home/operator/dev/a", "sessionId": "s1", "lastPrompt": secret},
        {"type": "assistant", "timestamp": "2026-08-10T15:00:05.000Z",
         "cwd": "/home/operator/dev/a", "sessionId": "s1"},
        {"type": "user", "timestamp": "2026-08-10T15:10:00.000Z",
         "cwd": "/home/operator/dev/b", "sessionId": "s2"},
        {"type": "queue-operation", "timestamp": "2026-08-10T15:11:00.000Z",
         "cwd": "/home/operator/dev/b", "sessionId": "s2"},
    ]
    (fake / "t.jsonl").write_text("\n".join(json.dumps(r) for r in rows))

    cg.CLAUDE_PROJECTS = store / "fixture-projects"
    cg.MIRROR = store / "nonexistent-mirror"
    out = io.StringIO()
    with redirect_stdout(out):
        cg.main(["transcripts", "--since", "3650", "--json"])
    text = out.getvalue()
    data = json.loads(text)
    day = next((d for d in data if d["day"] == "2026-08-10"), None)
    check("analyzer found the fixture day", day is not None)
    if day:
        check("counts distinct projects (the load variable)", day["projects"] == 2,
              f"got {day['projects']}")
        check("counts user turns", day["user_turns"] == 2, f"got {day['user_turns']}")
        check("counts queue-ops (queued-while-busy)", day["queue_ops"] == 1)
    check("prompt CONTENT never appears in analyzer output", secret not in text)
    check("prompt length is kept instead of text",
          day is not None and day.get("prompt_chars_mean") == len(secret))

    # Dedup: the same relative file present in both live and mirror must be
    # counted once, or every overlapping event doubles.
    mirror = store / "mirror2" / "-home-user-dev-x"
    mirror.mkdir(parents=True, exist_ok=True)
    (mirror / "t.jsonl").write_text((fake / "t.jsonl").read_text())
    cg.MIRROR = store / "mirror2"
    out2 = io.StringIO()
    with redirect_stdout(out2):
        cg.main(["transcripts", "--since", "3650", "--json"])
    d2 = next(d for d in json.loads(out2.getvalue()) if d["day"] == "2026-08-10")
    check("live+mirror overlap is deduped, not double-counted",
          d2["user_turns"] == 2, f"got {d2['user_turns']} (double-count = 4)")


# ------------------------------------------------------------- C6 refusal

def c6_curve_refuses(store: Path) -> None:
    print("\nC6 — the curve refuses to over-claim")
    cg = load(store)
    cg.AGENT_DIR.mkdir(parents=True, exist_ok=True)
    (cg.AGENT_DIR / "daily.jsonl").write_text(
        json.dumps({"day": "2026-08-10", "projects": 3}) + "\n")
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cg.main(["curve"])
    check("curve refuses on thin data", rc == 1, f"exit {rc}")
    check("refusal explains itself", "REFUSING" in out.getvalue())


def c7_light_and_digest(store: Path) -> None:
    print("\nC7 — luminance grid is content-free; digest makes pruning lossless")
    cg = load(store)

    # A luminance grid must collapse to scalars and never be retained.
    w = cg.Window()
    for i in range(6):
        w.on_light([float((i * 7 + j) % 256) for j in range(576)])
    rec = w.record(cg._now(), "t", "x11", "ok")
    light = rec["light"]
    check("light aggregates to scalars only",
          set(light) <= {"available", "n", "mean", "contrast", "churn"})
    check("no grid vector survives into the record",
          not any(isinstance(v, (list, tuple)) for v in light.values()))
    check("light record size is independent of sample count",
          abs(len(json.dumps(rec["light"]))
              - len(json.dumps(cg.Window().record(cg._now(), "t", "x11", "ok")["light"]))) < 90)
    check("grid resolution is below legibility by construction",
          cg.LIGHT_GRID_X * cg.LIGHT_GRID_Y <= 1024,
          f"{cg.LIGHT_GRID_X}x{cg.LIGHT_GRID_Y}")
    check("record schema still closed with light added",
          set(rec) <= cg.RECORD_FIELDS)

    # Digest must produce a day row, and rotate must refuse undigested days.
    cg.write_record(rec)
    out = io.StringIO()
    with redirect_stdout(out):
        cg.main(["digest"])
    digests = list((store / "digest").glob("*.jsonl"))
    check("digest writes a durable day row", len(digests) == 1)
    if digests:
        row = json.loads(digests[0].read_text().splitlines()[0])
        check("digest keeps coverage, so a blind day can't read as calm",
              "windows_ok" in row and "windows_degraded" in row)
        check("digest carries the light measure", "light_mean" in row)
        check("today's digest is flagged partial", row.get("partial") is True)

    # rotate must not delete an undigested day.
    old = (datetime.now() - timedelta(days=9999)).strftime("%Y-%m-%d")
    md = store / "keys" / old[:7]
    md.mkdir(parents=True, exist_ok=True)
    victim = md / f"keys-{old}.jsonl"
    victim.write_text(json.dumps(rec) + "\n")
    out = io.StringIO()
    with redirect_stdout(out):
        cg.main(["rotate"])
    check("rotate REFUSES to prune a day that was never digested",
          victim.exists() or "not digested" in out.getvalue(),
          "an undigested day was deleted — pruning would be amnesia")


class FakeDisplay:
    """Scripted X display: raise on the calls named in `fail_on`, else succeed."""

    def __init__(self, fail_calls=0, always_fail=False):
        self.calls = 0
        self.fail_calls = fail_calls
        self.always_fail = always_fail


def _sampler(cg, fail_first=0, always_fail=False):
    """A ScreenSampler with a scripted display, built without touching X."""
    s = cg.ScreenSampler.__new__(cg.ScreenSampler)
    s.ok = True
    s.reason = ""
    s._geo = (1920, 1080)
    s._geo_at = 1e18          # never re-query geometry
    s._class_cache = {}
    state = {"n": 0}

    class _X:
        AnyPropertyType = 0
        ZPixmap = 2

    s._X = _X()
    s._Req = None
    s._d = None
    s._root = None
    s._fake = state
    s._fake_fail_first = fail_first
    s._fake_always = always_fail
    return s


def c8_transient_recovery(store: Path) -> None:
    print("\nC8 — a transient X error must not kill the sampler forever")
    cg = load(store)
    src = TOOL.read_text()

    check("per-call X errors do not permanently set ok=False",
          "fail_streak" in src or "_screen_fail" in src,
          "sample()/luminance_grid() still latch self.ok=False on any exception")
    check("sampler is down only after a run of consecutive failures",
          "FAIL_STREAK_DOWN" in src or "fail_streak >=" in src)
    check("a down sampler retries re-initialisation",
          "_reinit" in src or "re-init" in src.lower())
    # Screen (2s) and light (10s) must not share one streak: frequent screen
    # successes would perpetually reset a permanently-dead light path.
    check("screen and light track health INDEPENDENTLY",
          ("screen_fail" in src and "light_fail" in src)
          or ("_health['screen']" in src and "_health['light']" in src),
          "a shared counter lets 2s screen successes mask a dead 10s light path")
    check("success is returned explicitly, not inferred from (None, None)",
          "SAMPLE_FAILED" in src or "ok=True," in src or '"ok":' in src,
          "(None, None) is ambiguous: failure vs a real no-properties result")


def c9_status_honesty(store: Path) -> None:
    print("\nC9 — keys OK + screen dead must NOT read as healthy")
    cg = load(store)
    # This fixture models a device that CLAIMS screen+light support. Do not let
    # the host running the contract decide that policy: on macOS both channels
    # are deliberately unsupported, which correctly makes their absence healthy
    # in production but would turn this X11-blind fixture into a false green.
    cg.device = lambda: {
        "id": "fixture-x11",
        "channels": {"screen": True, "light": True},
    }
    cg._unit_active = lambda: True
    w = cg.Window()
    w.screen_ok = False
    w.screen_reason = "xlib-read-failed:BadWindow"
    rec = w.record(cg._now(), "t", "x11", "ok")   # status ok, screen blind
    cg.write_record(rec)

    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cg.main(["status", "--gate"])
    check("status --gate exits non-zero when a subsystem is blind", rc != 0,
          f"exit {rc} — top-level ok while screen/light are dead (observed live)")

    out = io.StringIO()
    with redirect_stdout(out):
        cg.main(["status"])
    text = out.getvalue()
    check("status names the blind subsystem",
          "screen" in text.lower() and ("blind" in text.lower()
                                        or "down" in text.lower()
                                        or "degraded" in text.lower()))
    out, err = io.StringIO(), io.StringIO()
    ok = False
    try:
        with redirect_stdout(out), redirect_stderr(err):
            cg.main(["status", "--json"])
        j = json.loads(out.getvalue())
        ok = isinstance(j.get("subsystems"), dict) and "screen" in j["subsystems"]
    except SystemExit:
        ok = False          # argparse rejects --json: the flag does not exist yet
    except (json.JSONDecodeError, TypeError):
        ok = False
    check("status --json emits a per-subsystem breakdown", ok)


def c10_window_silent_zero(store: Path) -> None:
    print("\nC10 — a window with zero successful samples is NOT 'available'")
    cg = load(store)
    w = cg.Window()          # no on-screen sample ever landed
    sc = w.screen()
    check("screen() reports unavailable when no sample landed",
          sc.get("available") is False,
          f"got available={sc.get('available')}, switches={sc.get('switches')} "
          "— a silent zero inside the anti-silent-zero design")
    check("it carries a reason", bool(sc.get("reason")))
    li = w.light()
    check("light() likewise reports unavailable with no samples",
          li.get("available") is False)


def c11_digest_aggregable(store: Path) -> None:
    print("\nC11 — digest must be honestly aggregable and continuity-aware")
    cg = load(store)
    w = cg.Window()
    for i in range(40):
        w.on_key("other", 1.0 + i * 0.05)
    for i in range(6):
        w.on_key("backspace", 3.0 + i * 0.05)
    cg.write_record(w.record(cg._now(), "t", "x11", "ok"))
    out = io.StringIO()
    with redirect_stdout(out):
        cg.main(["digest"])
    dg = list((store / "digest").glob("*.jsonl"))
    row = json.loads(dg[0].read_text().splitlines()[0]) if dg else {}

    for f in ("windows_screen_ok", "windows_light_ok", "keys_active_total",
              "keys_bs_active", "expected_slots", "gap_minutes"):
        check(f"digest carries {f}", f in row)

    # Sol's arithmetic: 60 consecutive rows span 59 minutes, so
    # gap = span - windows would be -1. Expected slots must be inclusive.
    if "gap_minutes" in row:
        check("gap_minutes is never negative", row["gap_minutes"] >= 0,
              f"got {row['gap_minutes']} — off-by-one in the slot count")
    check("weekly ratio is reconstructable from sums, not mean-of-ratios",
          row.get("keys_active_total") is not None
          and row.get("keys_bs_active") is not None)


def c12_behavioral(store: Path) -> None:
    """Behavioral counterparts to C8/C11.

    C8's assertions are source-text greps: they pass on VOCABULARY, not
    behaviour. A delivered version once satisfied every one of them while the
    permanent-death bug survived intact — the streak machinery was present as
    decoration and never ran. These checks exercise the code instead.
    """
    print("\nC12 — behaviour, not vocabulary")
    cg = load(store)
    from datetime import timezone as _tz

    # --- a transient failure must NOT latch the sampler off permanently ------
    s = cg.ScreenSampler.__new__(cg.ScreenSampler)
    # _health FIRST: `ok` is a property backed by it, so assigning ok before
    # _health exists raises AttributeError inside the setter.
    s._health = {
        sub: {"ok": True, "reason": "", "fail_streak": 0,
              "last_ok_at": 0.0, "last_reinit_at": 0.0}
        for sub in ("screen", "light")
    }
    s._geo = (1920, 1080)
    s._geo_at = 1e18
    s._class_cache = {}
    if hasattr(cg.ScreenSampler, "_fail"):
        try:
            s._fail("screen", "simulated")
            latched = (s.ok is False)
            check("one transient failure does not latch the sampler off",
                  not latched,
                  "ok=False after a SINGLE failure: the entry guard then "
                  "short-circuits forever and the streak never reaches its "
                  "threshold — permanent death with streak machinery as decor")
        except Exception as exc:
            check("one transient failure does not latch the sampler off", False,
                  f"_fail raised {type(exc).__name__}")
    else:
        check("sampler exposes a per-subsystem failure path", False,
              "no _fail(); C8 greps cannot prove behaviour")

    # --- a reconnect must not clear a failure it did not fix ----------------
    # Measured live 2026-08-17 on GNOME Wayland 50.1: `screen` works (X property
    # reads on the XWayland root succeed) while `light` fails EVERY sample
    # (GetImage on that root -> BadMatch). _reinit() re-opened the Display and
    # then cleared the health of BOTH subsystems, so light's streak and its
    # specific reason were wiped on a cadence: fail 5x -> down -> reinit
    # "recovers" it -> fail 5x -> forever. The dashboard therefore showed the
    # generic "no-sample-in-window" instead of the actionable BadMatch, and the
    # light channel was permanently blind while never once saying why.
    # The invariant: a reconnect restores the TRANSPORT; only an actual success
    # restores HEALTH.
    if hasattr(cg.ScreenSampler, "_reinit"):
        import sys as _sys
        import types as _types
        s2 = cg.ScreenSampler.__new__(cg.ScreenSampler)
        s2._health = {
            "screen": {"ok": True, "reason": "", "fail_streak": 0,
                       "last_ok_at": 0.0, "last_reinit_at": 0.0},
            "light": {"ok": False, "reason": "xlib-image-failed:BadMatch",
                      "fail_streak": 9, "last_ok_at": 0.0, "last_reinit_at": 0.0},
        }
        s2._geo, s2._geo_at, s2._class_cache = None, 0.0, {}

        class _FakeScreen:
            root = _types.SimpleNamespace(id=1)

        class _FakeDisplay:
            def screen(self):
                return _FakeScreen()

            def intern_atom(self, name):
                return 1
        fake = _types.ModuleType("Xlib")
        fake.display = _types.SimpleNamespace(Display=lambda *a, **k: _FakeDisplay())
        saved = {k: _sys.modules.get(k) for k in ("Xlib", "Xlib.display")}
        _sys.modules["Xlib"] = fake
        _sys.modules["Xlib.display"] = fake.display
        try:
            s2._reinit("light")
        except Exception as exc:
            check("a reconnect does not clear a failure it did not fix", False,
                  f"_reinit raised {type(exc).__name__}: {exc}")
        else:
            check("a reconnect does not clear a failure it did not fix",
                  s2._health["light"]["reason"] == "xlib-image-failed:BadMatch"
                  and s2._health["light"]["ok"] is False,
                  f"light health after reinit={s2._health['light']} — the "
                  "specific cause was wiped by a reconnect that fixed nothing, "
                  "so the channel reports a generic 'no sample' forever")
        finally:
            for k, v in saved.items():
                if v is None:
                    _sys.modules.pop(k, None)
                else:
                    _sys.modules[k] = v

    # --- continuity must see a LEADING/TRAILING outage, not just interior ----
    base = datetime(2026, 8, 10, 12, 0, tzinfo=_tz.utc)
    recs = [cg.Window().record(base + timedelta(minutes=i), "t", "x11", "ok")
            for i in range(720)]
    d = cg._digest_day(recs, "2026-08-10")
    check("digest exposes a DAY-anchored continuity measure",
          "day_coverage_pct" in d and "day_gap_minutes" in d,
          "only interior gaps are measured")
    if "day_gap_minutes" in d:
        check("a 12h outage is NOT reported as perfect continuity",
              (d.get("day_gap_minutes") or 0) > 300,
              f"day_gap_minutes={d.get('day_gap_minutes')} for a half-blind day "
              "— anchoring to surviving rows makes a crash invisible")
    if "expected_slots" in d:
        check("expected_slots counts MINUTES, not hours",
              d["expected_slots"] == 720,
              f"got {d['expected_slots']} for 720 minute-rows (24 => divided by 60 twice)")

    # --- the kill switch must stop OBSERVING, not merely stop writing --------
    src = TOOL.read_text()
    tblock = src[src.index("def cmd_transcripts"):][:600]
    check("cmd_transcripts checks the kill switch before reading the corpus",
          "DISABLED" in tblock,
          "`cogload off && cogload transcripts` would still ingest ~/.claude/projects")

    # --- a dead mouse thread must be detectable -----------------------------
    check("mouse listener liveness is checked",
          "m_listener.running" in src,
          "a dead mouse thread leaves clicks/scrolls at 0 with status ok — "
          "indistinguishable from a genuinely mouse-free hour")

    # --- never signal a PID we have not identified as ours -------------------
    check("daemon signalling verifies process identity before kill()",
          "_signal_daemon" in src and "/proc/" in src,
          "a stale pidfile makes `cogload off` SIGUSR1 a recycled PID; "
          "SIGUSR1's default disposition is TERMINATE")
    check("the pidfile is removed on exit",
          "PIDFILE.unlink" in src)


def c13_label_day_join(store: Path) -> None:
    """A label must join to the LOCAL day it was recorded, not the UTC date.

    The evening ask fires 21:15 local, which is already TOMORROW in UTC.
    Truncating the UTC timestamp filed every evening label against the following
    day's behaviour — inverting the very association the calibration exists to
    measure. Proven against a real evening timestamp, not a synthetic noon.
    """
    print("\nC13 — labels join to the local day, not the UTC date")
    cg = load(store)
    out = io.StringIO()
    with redirect_stdout(out):
        cg.main(["mark", "--stress", "3", "--eff", "4", "--src", "test"])
    rec = json.loads((store / "labels.jsonl").read_text().splitlines()[0])
    check("label carries an explicit local `day`", "day" in rec)
    check("label carries its tz offset", "tz" in rec)
    if "day" in rec:
        from datetime import datetime as _dt
        local_today = _dt.now().astimezone().strftime("%Y-%m-%d")
        check("`day` is the local calendar day", rec["day"] == local_today,
              f"got {rec['day']}, local today is {local_today}")
        utc_day = rec["ts"][:10]
        check("`day` is authoritative even when UTC has already rolled over",
              rec["day"] == local_today,
              f"utc prefix {utc_day} vs local {local_today}")


def c14_claims_engine(store: Path) -> None:
    """The interpretation layer cannot over-claim, by TYPE not by instruction."""
    print("\nC14 — over-claiming is a type error, not a disobedience")
    cg = load(store)

    # No model anywhere in the outbound path.
    src = TOOL.read_text()
    # Anchor on the catalog block itself, not "everything up to cmd_claims" —
    # unrelated code inserted between them would otherwise be swept in and
    # reported as a claims-path violation (it was: the fleet SSH code).
    seg = src[src.index("CLAIM_CATALOG = {"):src.index("class ClaimError")]
    check("no LLM call inside the claims path",
          not any(t in seg for t in ("oll ", "subprocess", "requests", "urllib")),
          "a model in the truth path defeats the whole design")

    # Unknown id and non-numeric params must RAISE.
    try:
        cg.render_claim("nope.not.real", "day")
        check("unknown claim id is refused", False, "it rendered")
    except cg.ClaimError:
        check("unknown claim id is refused", True)
    try:
        cg.render_claim("capture.coverage", "day", coverage_pct="mucho",
                        covered_minutes=1, expected_minutes=2)
        check("a STRING param is refused (no free-text slot exists)", False,
              "prose reached a template")
    except cg.ClaimError:
        check("a STRING param is refused (no free-text slot exists)", True)
    try:
        cg.render_claim("assoc.metric_label", "day", n_hi=5, n_lo=5,
                        med_hi=1.0, med_lo=2.0, dim="stress", metric="bs")
        check("a T3 claim is refused in DAY scope (anti-anchoring)", False)
    except cg.ClaimError:
        check("a T3 claim is refused in DAY scope (anti-anchoring)", True)

    # Templates that are not calibrated may not assert an internal state.
    bad = []
    for cid, spec in cg.CLAIM_CATALOG.items():
        if spec["construct_source"] in ("none", "self-report"):
            low = spec["template_es"].lower()
            for w in cg.CONSTRUCT_LEXICON:
                if w in low:
                    bad.append((cid, w))
    check("no uncalibrated template asserts an internal state", not bad, str(bad))

    # The statistical gate must reject the noise case that a naive
    # accuracy-margin rule would have blessed.
    noise = cg.ledger_verdict(17, 28, 0.5, (14, 14))
    real = cg.ledger_verdict(22, 28, 0.5, (14, 14))
    lop = cg.ledger_verdict(25, 28, 0.5, (3, 25))
    check("17/28 (p=0.17) is NOT certified predictive",
          noise["verdict"] == "not-better", str(noise))
    check("22/28 (p=0.002) IS certified predictive", real["verdict"] == "better")
    check("a lopsided sample cannot win by predicting the majority",
          lop["verdict"] == "insufficient")

    # Day scope must carry no association/prediction content.
    out = io.StringIO()
    with redirect_stdout(out):
        cg.main(["claims"])
    text = out.getvalue()
    check("day scope emits no asociación content", "[asociación" not in text)
    check("day scope emits no predictivo content", "[predictivo]" not in text)
    check("locked tiers still SAY what is missing", "bloquead" in text)


def c15_one_label_per_day(store: Path) -> None:
    """One label per LOCAL day, correctable, last-writer-wins.

    Undefined duplicate semantics let the store accumulate several labels for a
    day and forced every reader to invent its own reconciliation — a median
    across the day would blend a correction back together with the mistake it
    was correcting.
    """
    print("\nC15 — one label per day; corrections win")
    cg = load(store)
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc1 = cg.main(["mark", "--slot", "evening", "--stress", "3", "--eff", "4"])
    check("first label of the day is accepted", rc1 == 0)

    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc2 = cg.main(["mark", "--slot", "evening", "--stress", "5", "--eff", "1"])
    check("a second label in the SAME slot is REFUSED (exit 3)", rc2 == 3, f"exit {rc2}")
    rows = (store / "labels.jsonl").read_text().splitlines()
    check("the refused label did not reach the store", len(rows) == 1,
          f"{len(rows)} rows")

    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc3 = cg.main(["mark", "--slot", "evening", "--stress", "5", "--eff", "1", "--force"])
    check("--force lets a correction through", rc3 == 0)
    rows = [json.loads(l) for l in (store / "labels.jsonl").read_text().splitlines()]
    check("the store keeps both rows as an audit trail", len(rows) == 2)
    latest = max(rows, key=lambda r: r["ts"])
    check("the CORRECTION is authoritative, not a blend",
          latest["stress"] == 5, f"got {latest['stress']}")
    # Scope the check to t3_eligibility. A bare repo-wide grep for
    # `statistics.median(vals)` is a FALSE POSITIVE: cmd_curve legitimately
    # medians over load BINS, which has nothing to do with a day's labels.
    src = TOOL.read_text()
    seg = src[src.index("def t3_eligibility"):]
    seg = seg[:seg.index("\ndef ", 10)]   # this function only
    # Strip comments before grepping: the segment's own comment EXPLAINS why a
    # median is wrong, so a naive substring match flags the explanation itself.
    code = "\n".join(l.split("#", 1)[0] for l in seg.splitlines())
    check("t3 takes the max-ts label, never a median across the day",
          "max(withdim, key=" in code and "median" not in code)
    # CONCURRENCY — the guard is only true if it holds a lock. There are two
    # live writers (dashboard POST and the Telegram MCP handler); an unlocked
    # check-then-append lets both pass. A sequential "mark twice" test cannot
    # detect this, so spawn real competing processes.
    import subprocess as _sp
    conc = Path(tempfile.mkdtemp(prefix="cogload-race-"))
    env = dict(os.environ, COGLOAD_DIR=str(conc))
    procs = [_sp.Popen([sys.executable, str(TOOL), "mark", "--stress", "3",
                        "--src", f"race{i}"], env=env,
                       stdout=_sp.DEVNULL, stderr=_sp.DEVNULL) for i in range(8)]
    codes = [p.wait() for p in procs]
    rows = (conc / "labels.jsonl").read_text().splitlines() if (conc / "labels.jsonl").exists() else []
    check("8 concurrent marks -> exactly one winner",
          codes.count(0) == 1, f"exit codes {sorted(codes)}")
    check("8 concurrent marks -> exactly one row",
          len(rows) == 1, f"{len(rows)} rows — the check/append is not locked")
    shutil.rmtree(conc, ignore_errors=True)


def c16_psychologist_instrument(store: Path) -> None:
    """the psychologist's instrument (2026-08-12): anxiety at BOTH ends of the day.

    A one-label-per-DAY guard silently refused the evening reading, discarding
    half the instrument the psychologist specified. Uniqueness is per
    (day, slot). Momentary anxiety and recalled whole-day anxiety are separate
    measurements — the remembered day is reconstructed, not averaged — so they
    are stored as distinct dimensions and never folded together.
    """
    print("\nC16 — two measurement moments per day")
    cg = load(store)

    def run(*a):
        o, e = io.StringIO(), io.StringIO()
        with redirect_stdout(o), redirect_stderr(e):
            return cg.main(["mark", *a])

    check("morning anxiety is accepted", run("--slot", "morning", "--anx", "2") == 0)
    check("evening reading is NOT blocked by the morning one",
          run("--slot", "evening", "--anx", "4", "--anx-day", "3",
              "--stress", "4") == 0,
          "the per-day guard would discard half the instrument")
    check("a repeat of the SAME slot is still refused",
          run("--slot", "morning", "--anx", "5") == 3)
    check("whole-day recall is refused in the morning slot",
          run("--slot", "morning", "--anx-day", "3") == 3,
          "a recall of a day that has not happened is not a measurement")
    check("a label with no measure at all is refused",
          run("--slot", "evening", "--force") == 3)

    rows = [json.loads(l) for l in (store / "labels.jsonl").read_text().splitlines()]
    slots = {r["slot"] for r in rows}
    check("both moments persist", slots == {"morning", "evening"}, str(slots))
    ev = [r for r in rows if r["slot"] == "evening"][0]
    check("momentary and recalled anxiety are stored separately",
          ev["anx"] == 4 and ev["anx_day"] == 3 and ev["anx"] != ev["anx_day"])
    check("anx is a first-class dimension in the schema",
          all(k in rows[0] for k in ("anx", "anx_day", "slot")))


def c17_fleet_reconciliation(store: Path) -> None:
    """N devices must reconcile to PERSON-days, and egress is payload-validated.

    Two blockers, both measured before the fix:
      * counting rows instead of person-days reported 7 real days as 14, so a
        14-day gate would unlock on half the evidence;
      * a filename allowlist proves which PATHS move, never what is inside
        them — a digest field carrying free text would sail through it.
    """
    print("\nC17 — fleet reconciles to person-days; egress is field-level")
    cg = load(store)
    from datetime import date as _d, timedelta as _td

    base = {"windows": 1400, "windows_ok": 1400, "windows_screen_ok": 1380,
            "windows_light_ok": 1370, "active_minutes": 300, "keys_total": 40000,
            "keys_enter": 900, "keys_active_total": 38000, "keys_bs_active": 3800,
            "gap_minutes": 0, "partial": False, "day_coverage_pct": 0.97}
    rows = []
    today = _d.today()
    for i in range(7):
        day = (today - _td(days=i)).isoformat()
        rows.append({**base, "day": day, "host": "ubuntu-aaa"})
        rows.append({**base, "day": day, "host": "macbook-bbb"})

    pd = cg.person_days(rows)
    check("14 device-days reduce to 7 person-days", len(pd) == 7, f"got {len(pd)}")
    one = pd[max(pd)]
    check("both hosts are retained, not collapsed", len(one["hosts"]) == 2)
    check("event counts SUM across devices (two keyboards, one human)",
          one["keys_total"] == 80000, f"got {one['keys_total']}")
    check("coverage stays STRATIFIED per device, never pooled",
          isinstance(one["per_device"], dict) and len(one["per_device"]) == 2
          and "coverage" not in one)
    check("a person-day is valid if ANY device was valid", one["valid"] is True)

    # A silent device must not invalidate the day, nor fabricate calm.
    quiet = cg.person_days([{**base, "day": "2026-07-01", "host": "ubuntu-aaa"}])
    check("a device that reported nothing neither invalidates nor fabricates",
          quiet["2026-07-01"]["n_devices"] == 1)

    # Digestion refuses to fuse two machines.
    try:
        cg._digest_day([{"ts": "2026-08-10T09:00:00+00:00", "host": "a", "status": "ok",
                         "total": 1, "keys": {}, "iki": {}, "mouse": {}, "screen": {},
                         "light": {}},
                        {"ts": "2026-08-10T09:01:00+00:00", "host": "b", "status": "ok",
                         "total": 1, "keys": {}, "iki": {}, "mouse": {}, "screen": {},
                         "light": {}}], "2026-08-10")
        check("digest REFUSES rows from two hosts", False, "it fused them")
    except (ValueError, KeyError, TypeError) as e:
        check("digest REFUSES rows from two hosts", isinstance(e, ValueError), str(e)[:60])

    # Egress is validated field by field, on send AND on receipt.
    ok, _ = cg._egress_safe({"day": "2026-08-12", "host": "u-1", "windows": 10})
    bad_note, why1 = cg._egress_safe({"day": "x", "host": "u-1", "note": "personal"})
    bad_txt, why2 = cg._egress_safe({"day": "x", "host": "u-1", "app": "Signal — Ana"})
    check("a clean digest row may leave", ok)
    check("a smuggled note is BLOCKED at the payload, not the filename", not bad_note)
    check("a free-form string field is BLOCKED", not bad_txt)
    check("the egress whitelist carries no note/app/title field",
          not ({"note", "app", "title", "wm_name"} & cg.DIGEST_EGRESS_FIELDS))

    src = TOOL.read_text()
    check("the kill switch gates EGRESS, not just collection",
          "DISABLED.exists()" in src[src.index("if args.action == \"push\""):][:400])


def c18_slot_is_part_of_the_series(store: Path) -> None:
    """A measure taken at TWO moments is two series, never one overwritten.

    The morning check-in is only worth asking if its answer survives to the
    association layer. It did not: `t3_eligibility` took the newest row per day
    carrying a dimension, so the 18:45 reading silently replaced the 07:15 one
    for the same field — the morning number was collected, stored, and then
    dropped by every consumer. the psychologist asked for both ends of the day precisely
    so they can be COMPARED; folding them is the one thing that makes the
    instrument useless.
    """
    print("\nC18 — morning and evening are separate series, not last-writer-wins")
    cg = load(store)

    # 14 valid days, each carrying a LOW morning reading and a HIGH evening one.
    # If the reader is slot-blind, the morning series reads as high (or the
    # evening one as low) and one of the two groups is empty.
    cg.DIGEST_DIR.mkdir(parents=True, exist_ok=True)
    rows, labels = [], []
    for i in range(14):
        day = (datetime.now() - timedelta(days=i + 1)).strftime("%Y-%m-%d")
        rows.append(json.dumps({
            "day": day, "host": "test", "windows": 600, "windows_ok": 600,
            "windows_screen_ok": 600, "windows_light_ok": 600,
            "gap_minutes": 0, "partial": False, "active_minutes": 300,
            "keys_total": 5000, "backspace_ratio": 0.05,
        }))
        labels.append(json.dumps({"ts": f"{day}T07:15:00-06:00", "day": day,
                                  "slot": "morning", "anx": 2, "stress": 2}))
        labels.append(json.dumps({"ts": f"{day}T18:45:00-06:00", "day": day,
                                  "slot": "evening", "anx": 5, "stress": 5,
                                  "anx_day": 4}))
    (cg.DIGEST_DIR / "2026-08.jsonl").write_text("\n".join(rows) + "\n")
    cg.LABELS.write_text("\n".join(labels) + "\n")

    am = cg.t3_eligibility("anx", "morning")
    pm = cg.t3_eligibility("anx", "evening")
    check("the morning series sees its OWN 14 readings", am["pairs"] == 14,
          f"got {am['pairs']}")
    check("the evening series sees its OWN 14 readings", pm["pairs"] == 14,
          f"got {pm['pairs']}")
    check("a low morning reading is not overwritten by a high evening one",
          am["n_lo"] == 14 and am["n_hi"] == 0,
          f"lo={am['n_lo']} hi={am['n_hi']} — the 18:45 row replaced the 07:15 one")
    check("the evening reading stays high", pm["n_hi"] == 14 and pm["n_lo"] == 0,
          f"lo={pm['n_lo']} hi={pm['n_hi']}")
    check("stress is slot-segmented too, not just anxiety",
          cg.t3_eligibility("stress", "morning")["n_lo"] == 14,
          "the morning check-in asks estrés; it must survive the same way")
    check("whole-day recall stays a single evening-only series",
          cg.t3_eligibility("anx_day", "evening")["pairs"] == 14)

    # Each series is named, so a refusal says WHICH reading is missing.
    ids = {cg.series_id(f, s) for f, s in cg.LABEL_SERIES}
    check("morning and evening are separately addressable",
          {"anx_morning", "anx_evening", "stress_morning", "stress_evening"} <= ids,
          str(sorted(ids)))
    out = io.StringIO()
    with redirect_stdout(out):
        cg.main(["claims"])
    text = out.getvalue()
    check("a locked morning series names itself in the refusal",
          "anx_morning" in text, text[-300:])


def c19_macos_tcc(store: Path) -> None:
    print("\nC19 — macOS TCC preflight")
    cg = load(store)
    src = TOOL.read_text()
    # macOS capture APIs fail closed but silently: without a preflight, denied
    # TCC access is indistinguishable from an honestly idle keyboard. Exercise
    # the real helper against deterministic stand-ins for the system symbols.
    has_tcc = hasattr(cg, "mac_tcc_status") and hasattr(cg, "mac_tcc_reason")
    check("macOS TCC preflight exists", has_tcc)
    if has_tcc:
        class BoolCall:
            def __init__(self, value):
                self.value = value
                self.restype = None

            def __call__(self):
                return self.value

        class TccLib:
            def __init__(self, im, ax):
                self.CGPreflightListenEventAccess = BoolCall(im)
                self.AXIsProcessTrusted = BoolCall(ax)

        old_is_mac = cg._is_mac
        old_find = ctypes.util.find_library
        old_load = ctypes.cdll.LoadLibrary
        try:
            cg._is_mac = lambda: True
            ctypes.util.find_library = lambda _name: "/fake/framework"

            ctypes.cdll.LoadLibrary = lambda _path: TccLib(False, False)
            check("denied Input Monitoring degrades explicitly",
                  cg.mac_tcc_reason() == "tcc-input-monitoring-denied")

            ctypes.cdll.LoadLibrary = lambda _path: TccLib(True, False)
            check("denied Accessibility degrades explicitly",
                  cg.mac_tcc_reason() == "tcc-accessibility-denied")

            ctypes.cdll.LoadLibrary = lambda _path: TccLib(True, True)
            check("both macOS capture grants clear the preflight",
                  cg.mac_tcc_reason() == "")

            ctypes.cdll.LoadLibrary = lambda _path: object()
            check("unmeasurable TCC state is not assumed healthy",
                  cg.mac_tcc_reason() == "tcc-unknown")
        finally:
            cg._is_mac = old_is_mac
            ctypes.util.find_library = old_find
            ctypes.cdll.LoadLibrary = old_load

    check("daemon turns denied TCC into degraded rows",
          "_tcc_reason = mac_tcc_reason()" in src
          and "elif _tcc_reason:" in src
          and 'state["degraded"] = f"degraded:{_tcc_reason}"' in src)


def c20_evdev_privacy(store: Path) -> None:
    print("\nC20 — evdev classification and privacy")
    cg = load(store)

    expectations = {
        14: "backspace", 111: "delete", 28: "enter", 96: "enter",
        15: "nav", 103: "nav", 108: "nav", 105: "nav", 106: "nav",
        42: "mod", 54: "mod", 29: "mod", 97: "mod", 56: "mod",
        100: "mod", 125: "mod", 126: "mod", 58: "mod",
        30: "other", 57: "other",
    }
    bad = []
    for code, expected in expectations.items():
        got = cg.classify_evdev(code)
        if got != expected:
            bad.append((code, expected, got))
    check("classify_evdev maps the documented codes correctly",
          not bad, str(bad))

    classes = {cg.classify_evdev(c) for c in range(600)}
    check("classify_evdev returns only the six class names for 0..599",
          classes <= set(cg.KEY_CLASSES), str(classes))

    small, big = cg.Window(), cg.Window()
    for i in range(10):
        small.on_key(cg.classify_evdev(30), 1.0 + i * 0.1)
    for i in range(2000):
        big.on_key(cg.classify_evdev(30), 1.0 + i * 0.1)
    s_len = len(json.dumps(small.record(cg._now(), "t", "wayland", "ok")))
    b_len = len(json.dumps(big.record(cg._now(), "t", "wayland", "ok")))
    check("evdev path record size is independent of keystroke volume",
          abs(b_len - s_len) < 40,
          f"{s_len}B vs {b_len}B — record grows with input, so it retains events")

    def all_strings(o):
        if isinstance(o, str):
            yield o
        elif isinstance(o, dict):
            for v in o.values():
                yield from all_strings(v)
        elif isinstance(o, (list, tuple)):
            for v in o:
                yield from all_strings(v)

    ts = cg._now()
    ts_str = ts.isoformat()
    # The host field is derived from device_id(), which can contain random hex
    # that overlaps with test keycodes. Record once and strip it.
    rec0 = cg.Window().record(ts, "t", "wayland", "ok")
    host = rec0.get("host", "")
    candidate_codes = [14, 111, 28, 96, 15, 103, 108, 105, 106,
                       42, 54, 29, 97, 56, 100, 125, 126, 58, 30, 57]
    fed_codes = [c for c in candidate_codes
                 if str(c) not in ts_str and str(c) not in host]
    if len(fed_codes) < 5:
        fed_codes = [c for c in range(3, 600)
                     if str(c) not in ts_str and str(c) not in host
                     and cg.classify_evdev(c) in cg.KEY_CLASSES][:20]
    win = cg.Window()
    for i in range(2000):
        win.on_key(cg.classify_evdev(fed_codes[i % len(fed_codes)]),
                   1.0 + i * 0.1)
    rec = win.record(ts, "t", "wayland", "ok")
    flat = "".join(s for s in all_strings(rec))
    flat_no_ts = flat.replace(ts_str, "")
    leaked = [str(c) for c in fed_codes if str(c) in flat_no_ts]
    check("no raw keycode digits leak into the flattened record",
          not leaked, f"leaked: {leaked[:10]}")

    power_caps = {cg._EV_TYPE_KEY:
                  [116, 142, 143, 152, 153, 154, 155, 156, 157, 158,
                   159, 160, 161, 162, 163, 164, 165, 166, 167]}
    check("_is_typing_keyboard rejects a power/sleep-only device",
          not cg._is_typing_keyboard(power_caps))
    typing_caps = {cg._EV_TYPE_KEY: list(range(2, 54))}
    check("_is_typing_keyboard accepts the main typing block",
          cg._is_typing_keyboard(typing_caps))


def c21_redeclare_identity(store: Path) -> None:
    print("\nC21 — channel redeclaration preserves identity")
    cg = load(store)

    saved_platform = sys.platform
    saved_live = cg.live_session_type
    try:
        cases = [
            (("darwin", lambda: "x11"),
             {"screen": False, "light": False}),
            (("linux", lambda: "x11"),
             {"screen": True, "light": True}),
            (("linux", lambda: "wayland"),
             {"screen": False, "light": False}),
            (("linux", lambda: "tty"), None),
            (("linux", lambda: "unknown"), None),
        ]
        bad = []
        for (plat, st_fn), expected in cases:
            sys.platform = plat
            cg.live_session_type = st_fn
            cg._sess_cache["t"] = 0
            got = cg.detect_channels()
            if expected is None:
                if got is not None:
                    bad.append((plat, st_fn(), "expected None", got))
            else:
                if got is None:
                    bad.append((plat, st_fn(), "got None", expected))
                else:
                    for k, v in expected.items():
                        if got.get(k) != v:
                            bad.append((plat, st_fn(), k, got.get(k), v))
        check("detect_channels() matrix is correct", not bad, str(bad))
    finally:
        sys.platform = saved_platform
        cg.live_session_type = saved_live
        cg._sess_cache["t"] = 0

    base_dev = {
        "id": "fixture-abc123",
        "uuid": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "slug": "fixture",
        "platform": "linux",
        "created": "2026-08-01T00:00:00+00:00",
        "role": "solo",
        "hub": None,
        "channels": {"keys": True, "mouse": True, "screen": True,
                     "light": True, "agents": True},
        "decoy": "preserve-me",
    }
    cg.DEVICE_FILE.write_text(
        json.dumps(base_dev, indent=2, sort_keys=True), encoding="utf-8")
    cg._DEVICE_CACHE.clear()

    sys.platform = "linux"
    cg.live_session_type = lambda: "wayland"
    cg._sess_cache["t"] = 0
    try:
        rc = cg.main(["channels", "--redeclare"])
        check("redeclare succeeds when channels differ", rc == 0, f"exit {rc}")
        new_dev = json.loads(cg.DEVICE_FILE.read_text(encoding="utf-8"))
        changed = [k for k in base_dev if base_dev[k] != new_dev.get(k)]
        disallowed = [k for k in changed if k not in {"channels", "channels_updated"}]
        check("redeclare leaves identity fields byte-for-byte identical",
              not disallowed, f"changed: {disallowed}")
        check("redeclare updates channels",
              new_dev.get("channels") == {"keys": True, "mouse": True,
                                          "screen": False, "light": False,
                                          "agents": True})
        check("redeclare adds channels_updated", "channels_updated" in new_dev)
    finally:
        sys.platform = saved_platform
        cg.live_session_type = saved_live
        cg._sess_cache["t"] = 0

    base_dev2 = {
        "id": "fixture-xyz789",
        "uuid": "11111111-2222-3333-4444-555555555555",
        "slug": "fixture2",
        "platform": "linux",
        "created": "2026-08-01T00:00:00+00:00",
        "role": "solo",
        "hub": None,
        "channels": {"keys": True, "mouse": True, "screen": True,
                     "light": True, "agents": True},
    }
    cg.DEVICE_FILE.write_text(
        json.dumps(base_dev2, indent=2, sort_keys=True), encoding="utf-8")
    cg._DEVICE_CACHE.clear()
    sys.platform = "linux"
    cg.live_session_type = lambda: "tty"
    cg._sess_cache["t"] = 0
    try:
        before = cg.DEVICE_FILE.read_text(encoding="utf-8")
        rc = cg.main(["channels", "--redeclare"])
        check("redeclare refuses an indeterminate session",
              rc != 0, f"exit {rc}")
        after = cg.DEVICE_FILE.read_text(encoding="utf-8")
        check("device.json is unchanged on refusal", before == after)
    finally:
        sys.platform = saved_platform
        cg.live_session_type = saved_live
        cg._sess_cache["t"] = 0


def c22_selftest_wayland(store: Path) -> None:
    print("\nC22 — selftest is honest under Wayland")
    cg = load(store)

    src = TOOL.read_text()
    st_start = src.index("def cmd_selftest")
    st_end = src.find("\ndef main", st_start)
    selftest_src = src[st_start:st_end if st_end != -1 else len(src)]
    check("cmd_selftest consults live_session_type",
          "live_session_type()" in selftest_src)
    check("cmd_selftest has a non-zero Wayland+no-evdev branch",
          'sess == "wayland"' in selftest_src
          and "return 1" in selftest_src)
    check("xrecord-partial string still appears in source",
          "xrecord-partial" in src)

    old_available = cg.EvdevListener.available
    old_live = cg.live_session_type
    cg.EvdevListener.available = staticmethod(
        lambda: (False, "evdev-no-keyboard"))
    cg.live_session_type = lambda: "wayland"
    cg._sess_cache["t"] = 0
    try:
        rc = cg.main(["selftest"])
        check("cmd_selftest returns non-zero under wayland+no-evdev",
              rc != 0, f"exit {rc}")
    finally:
        cg.EvdevListener.available = staticmethod(old_available)
        cg.live_session_type = old_live
        cg._sess_cache["t"] = 0


def main() -> int:
    print("cogload acceptance contract")
    print("=" * 46)
    for fn in (c1_no_content, c2_anti_silent_zero, c3_wayland,
               c4_analyzer, c5_governance, c6_curve_refuses, c7_light_and_digest,
               c8_transient_recovery, c9_status_honesty, c10_window_silent_zero,
               c11_digest_aggregable, c12_behavioral, c13_label_day_join,
               c14_claims_engine, c15_one_label_per_day,
               c16_psychologist_instrument,
               c17_fleet_reconciliation,
               c18_slot_is_part_of_the_series,
               c19_macos_tcc,
               c20_evdev_privacy, c21_redeclare_identity, c22_selftest_wayland):
        d = Path(tempfile.mkdtemp(prefix="cogload-test-"))
        try:
            fn(d)
        finally:
            shutil.rmtree(d, ignore_errors=True)
    print("\n" + "=" * 46)
    print(f"{len(PASSES)} passed, {len(FAILURES)} failed")
    for f in FAILURES:
        print(f"  FAILED: {f}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
