#!/usr/bin/env python3
"""token-ledger — contract for the chapter-18 token audit + loop-portfolio ledger.

Hermetic: TOKEN_LEDGER_HOME fakes $HOME (so ~/.claude.json, ~/.claude/,
~/.config/systemd/user/ are all fixtures), TOKEN_LEDGER_REPO fakes the repo
root, TOKEN_LEDGER_NOW pins the clock. No network, no real systemd, no real
transcripts.

The assertions EXECUTE the tool against fixtures and are written to
DISCRIMINATE — each one was designed against a specific way a plausible-looking
implementation reports success while measuring nothing (the C12 lesson):

  A1  every MCP surface is inventoried, not just ~/.claude.json's four:
      claudeAiMcpEverConnected (remote connectors) and
      claudeInChromeDefaultEnabled (Chrome) are surfaces too. A tool that reads
      only `mcpServers` measures 4 of 17 and looks green doing it.
  A2  "called" means a real tool_use, not a string match. A recent transcript
      that merely MENTIONS mcp__ghost__x in assistant prose must NOT count as a
      call, and a call inside a 20-day-old file must not count as recent.
  A3  secrets never leave: an env value in the fixture's server entry must be
      absent from stdout, from --json, and from the written artifact.
  A4  CLAUDE.md is measured in bytes as well as lines — a 3-line, 5000-byte
      file is the real shape of this repo's CLAUDE.md, and `wc -l` alone
      reports it as tiny.
  A5  --write snapshots are IMMUTABLE: two writes the same day leave two files.
      An overwriting --write destroys the before-snapshot the round depends on.
  A6  an unreadable scan degrades to "unknown", never to "not called"
      (fail-closed: absence of evidence is not evidence of absence).

  L1  only ARMED units count as armed (timers.target.wants), not every file
      that happens to sit in ~/.config/systemd/user.
  L2  agent-invoking is resolved through the ExecStart chain — a wrapper that
      calls harness-agent-run is agent-invoking; a plain script is not.
  L3  caps are read from where they actually live, and a unit with no
      discoverable cap lands in `uncapped`.
  L4  a fully-capped fixture yields an EMPTY uncapped list — without this, an
      implementation that always reports everything uncapped passes L3.
  L5  est_basis is honest: "measured" only when round-usage rows exist, and the
      arithmetic is checkable; otherwise a declared non-measured basis.

Red-first is structural: pre-change there is no bin/token-ledger at all, so
every case below fails against `git show HEAD:`.
"""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TOOL = os.path.join(ROOT, "bin", "token-ledger")
FAILS = []
NOW = "2026-08-16T12:00:00Z"
SECRET = "sk-tokenledger-sentinel-9f3a"


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{(' — ' + detail) if detail and not ok else ''}")
    if not ok:
        FAILS.append(f"{name}: {detail}")


def run(verb, home, repo, *args, expect_rc=0):
    env = dict(os.environ, TOKEN_LEDGER_HOME=home, TOKEN_LEDGER_REPO=repo,
               TOKEN_LEDGER_NOW=NOW, CLAUDEMAXXING_HARNESS_CHILD="1")
    r = subprocess.run([sys.executable, TOOL, verb, *args], capture_output=True,
                       text=True, env=env, timeout=60)
    return r


def run_json(verb, home, repo, *args):
    r = run(verb, home, repo, "--json", *args)
    if r.returncode != 0:
        return None, r
    try:
        return json.loads(r.stdout), r
    except json.JSONDecodeError:
        return None, r


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(text)
    return path


def touch_days_ago(path, days):
    """Backdate mtime by `days` relative to the pinned clock."""
    import calendar
    import datetime as _dt
    base = _dt.datetime(2026, 8, 16, 12, 0, 0)
    when = calendar.timegm((base - _dt.timedelta(days=days)).timetuple())
    os.utime(path, (when, when))


def transcript_line(tool_name):
    """One assistant turn that REALLY calls `tool_name` (a tool_use block)."""
    return json.dumps({
        "type": "assistant",
        "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_x", "name": tool_name, "input": {}}]},
    })


def prose_line(mention):
    """One assistant turn that only TALKS ABOUT a tool — must not count."""
    return json.dumps({
        "type": "assistant",
        "message": {"role": "assistant", "content": [
            {"type": "text", "text": f"you could call {mention} here but I will not"}]},
    })


# ---------------------------------------------------------------- audit fixture

def audit_home(tmp, *, with_secret=True, unreadable=False):
    home = os.path.join(tmp, "home")
    claude_json = {
        "mcpServers": {
            "alpha": {"command": "alpha-server"},
            "beta": {"command": "beta-server",
                     "env": ({"API_KEY": SECRET} if with_secret else {})},
        },
        "claudeAiMcpEverConnected": ["claude.ai Figma", "claude.ai Gmail"],
        "claudeInChromeDefaultEnabled": True,
    }
    write(os.path.join(home, ".claude.json"), json.dumps(claude_json))
    write(os.path.join(home, ".claude", "CLAUDE.md"), "line\n" * 82)
    write(os.path.join(home, ".claude", "settings.json"),
          json.dumps({"model": "opus[1m]", "effortLevel": "high"}))
    for n in range(3):
        write(os.path.join(home, ".claude", "skills", f"s{n}", "SKILL.md"), "x\n")

    projects = os.path.join(home, ".claude", "projects", "-home-user")
    # recent: alpha genuinely called; ghost only mentioned in prose
    recent = write(os.path.join(projects, "recent.jsonl"),
                   transcript_line("mcp__alpha__do") + "\n"
                   + prose_line("mcp__ghost__x") + "\n"
                   + transcript_line("mcp__claude_ai_Figma__get_metadata") + "\n")
    touch_days_ago(recent, 2)
    # stale: beta called, but 20 days ago — outside the 14-day window
    old = write(os.path.join(projects, "old.jsonl"), transcript_line("mcp__beta__do") + "\n")
    touch_days_ago(old, 20)

    if unreadable:
        bad = write(os.path.join(projects, "locked.jsonl"), transcript_line("mcp__alpha__do") + "\n")
        touch_days_ago(bad, 1)
        os.chmod(bad, 0o000)
    return home


def audit_repo(tmp, *, lines=3, filler=5000):
    repo = os.path.join(tmp, "repo")
    body = "\n".join(["short"] * (lines - 1) + ["x" * filler])
    write(os.path.join(repo, "CLAUDE.md"), body + "\n")
    write(os.path.join(repo, "skills", "one", "SKILL.md"), "x\n")
    return repo


def by_name(rows):
    return {r["name"]: r for r in rows}


def a1_a2_inventory_and_real_calls():
    tmp = tempfile.mkdtemp(prefix="tl-a12-")
    home, repo = audit_home(tmp), audit_repo(tmp)
    d, r = run_json("audit", home, repo)
    if d is None:
        check("A1/A2 audit --json parses", False, f"rc={r.returncode} err={r.stderr[:200]}")
        return
    servers = by_name(d.get("mcp_servers", []))
    scopes = {s.get("scope") for s in servers.values()}
    check("A1 inventories local + remote + chrome surfaces",
          {"local", "remote", "chrome"} <= scopes and len(servers) >= 5,
          f"scopes={scopes} n={len(servers)} names={sorted(servers)}")
    check("A1 remote connectors named from claudeAiMcpEverConnected",
          "claude.ai Figma" in servers and "claude.ai Gmail" in servers,
          f"names={sorted(servers)}")
    check("A2 a real tool_use inside the window counts as called",
          servers.get("alpha", {}).get("called_last_14d") is True,
          f"alpha={servers.get('alpha')}")
    check("A2 a remote connector's real call is attributed to it",
          servers.get("claude.ai Figma", {}).get("called_last_14d") is True,
          f"figma={servers.get('claude.ai Figma')}")
    check("A2 a call older than the window does NOT count as called",
          servers.get("beta", {}).get("called_last_14d") is False,
          f"beta={servers.get('beta')}")
    check("A1 every row names the source its existence was read from",
          all(s.get("evidence_source") for s in servers.values()),
          f"missing={[n for n, v in servers.items() if not v.get('evidence_source')]}")
    check("A1 a remote row declares that its liveness is NOT locally knowable",
          servers.get("claude.ai Figma", {}).get("liveness") == "unknown"
          and servers.get("alpha", {}).get("liveness") == "configured",
          f"figma={servers.get('claude.ai Figma')} alpha={servers.get('alpha')}")
    check("A2 prose that merely mentions a tool is NOT a call",
          not any(n.startswith("ghost") or n == "ghost" for n in servers)
          or servers.get("ghost", {}).get("called_last_14d") is not True,
          f"servers={sorted(servers)}")


def a7_boundaries():
    """The two constants the whole audit's meaning rests on, pinned AT the edge.

    Added after a mutation run: with fixtures at 2d/20d and 82/3 lines, both
    `WINDOW_DAYS = 14` and `THRESHOLD_LINES = 200` could be moved by one without
    a single test noticing — the tool would have kept reporting confidently
    against a window and a ceiling nobody was checking.
    """
    tmp = tempfile.mkdtemp(prefix="tl-a7-")
    home = audit_home(tmp)
    projects = os.path.join(home, ".claude", "projects", "-home-user")
    # exactly 13 days old → inside a 14-day window; 15 days → outside it.
    inside = write(os.path.join(projects, "edge-in.jsonl"),
                   transcript_line("mcp__alpha__edge") + "\n")
    touch_days_ago(inside, 13)
    outside = write(os.path.join(projects, "edge-out.jsonl"),
                    transcript_line("mcp__beta__edge") + "\n")
    touch_days_ago(outside, 15)
    # drop the original recent/old files so each server has exactly one witness
    os.remove(os.path.join(projects, "recent.jsonl"))
    os.remove(os.path.join(projects, "old.jsonl"))

    d, r = run_json("audit", home, audit_repo(tmp))
    if d is None:
        check("A7 audit --json parses at the window edge", False,
              f"rc={r.returncode} err={r.stderr[:200]}")
        return
    servers = by_name(d.get("mcp_servers", []))
    check("A7 a call 13 days old is inside the 14-day window",
          servers.get("alpha", {}).get("called_last_14d") is True,
          f"alpha={servers.get('alpha')}")
    check("A7 a call 15 days old is outside the 14-day window",
          servers.get("beta", {}).get("called_last_14d") is False,
          f"beta={servers.get('beta')}")

    # 200 lines is AT the ceiling (not over); 201 is over.
    at = audit_repo(tempfile.mkdtemp(prefix="tl-a7at-"), lines=200, filler=1)
    over = audit_repo(tempfile.mkdtemp(prefix="tl-a7ov-"), lines=201, filler=1)
    d_at, _ = run_json("audit", home, at)
    d_ov, _ = run_json("audit", home, over)
    check("A7 exactly 200 lines is NOT over the threshold",
          (d_at or {}).get("claude_md", {}).get("repo_over_threshold") is False
          and (d_at or {}).get("claude_md", {}).get("repo_lines") == 200,
          f"claude_md={(d_at or {}).get('claude_md')}")
    check("A7 201 lines IS over the threshold",
          (d_ov or {}).get("claude_md", {}).get("repo_over_threshold") is True
          and (d_ov or {}).get("claude_md", {}).get("repo_lines") == 201,
          f"claude_md={(d_ov or {}).get('claude_md')}")


def a3_no_secret_leaks():
    tmp = tempfile.mkdtemp(prefix="tl-a3-")
    home, repo = audit_home(tmp), audit_repo(tmp)
    r_txt = run("audit", home, repo)
    d, r_js = run_json("audit", home, repo, )
    r_w = run("audit", home, repo, "--json", "--write")
    written = ""
    snap_dir = os.path.join(repo, "knowledge", "token-ledger")
    if os.path.isdir(snap_dir):
        for f in os.listdir(snap_dir):
            written += open(os.path.join(snap_dir, f)).read()
    leaked = [where for where, blob in
              (("stdout", r_txt.stdout), ("stderr", r_txt.stderr),
               ("json", r_js.stdout), ("artifact", written))
              if SECRET in blob]
    check("A3 no credential from .claude.json reaches output or artifact",
          not leaked and r_w.returncode == 0, f"leaked_in={leaked}")


def a4_bytes_not_only_lines():
    tmp = tempfile.mkdtemp(prefix="tl-a4-")
    home = audit_home(tmp)
    repo = audit_repo(tmp, lines=3, filler=5000)
    d, r = run_json("audit", home, repo)
    if d is None:
        check("A4 audit --json parses", False, f"rc={r.returncode} err={r.stderr[:200]}")
        return
    cm = d.get("claude_md", {})
    check("A4 repo CLAUDE.md reports lines AND bytes",
          cm.get("repo_lines") == 3 and cm.get("repo_bytes", 0) >= 5000,
          f"claude_md={cm}")
    check("A4 global CLAUDE.md line count is read from the fixture home",
          cm.get("global_lines") == 82, f"claude_md={cm}")
    check("A4 model and effort are read from settings.json",
          d.get("settings", {}).get("model") == "opus[1m]"
          and d.get("settings", {}).get("effort") == "high",
          f"settings={d.get('settings')}")


def a5_snapshots_immutable():
    tmp = tempfile.mkdtemp(prefix="tl-a5-")
    home, repo = audit_home(tmp), audit_repo(tmp)
    r1 = run("audit", home, repo, "--write")
    r2 = run("audit", home, repo, "--write")
    snap_dir = os.path.join(repo, "knowledge", "token-ledger")
    files = sorted(os.listdir(snap_dir)) if os.path.isdir(snap_dir) else []
    check("A5 two same-day --write calls leave two immutable snapshots",
          r1.returncode == 0 and r2.returncode == 0 and len(files) == 2
          and all(f.startswith("audit-2026-08-16") for f in files),
          f"rc={r1.returncode}/{r2.returncode} files={files}")


def a6_unreadable_is_unknown():
    if os.geteuid() == 0:
        print("  SKIP  A6 (running as root: chmod 000 is not enforced)")
        return
    tmp = tempfile.mkdtemp(prefix="tl-a6-")
    home = audit_home(tmp, unreadable=True)
    repo = audit_repo(tmp)
    d, r = run_json("audit", home, repo)
    if d is None:
        check("A6 audit --json parses with an unreadable transcript", False,
              f"rc={r.returncode} err={r.stderr[:200]}")
        return
    scan = d.get("scan", {})
    check("A6 an unreadable transcript makes the scan incomplete, not silently clean",
          scan.get("complete") is False and scan.get("unreadable", 0) >= 1,
          f"scan={scan}")
    check("A6 servers with no evidence under an incomplete scan read 'unknown'",
          any(s.get("called_last_14d") == "unknown" for s in d.get("mcp_servers", [])),
          f"servers={[(s['name'], s.get('called_last_14d')) for s in d.get('mcp_servers', [])]}")


# ---------------------------------------------------------------- loops fixture

def loops_home(tmp, *, all_capped=False):
    home = os.path.join(tmp, "home")
    units = os.path.join(home, ".config", "systemd", "user")
    wants = os.path.join(units, "timers.target.wants")
    os.makedirs(wants, exist_ok=True)

    wrapper = write(os.path.join(home, ".config", "claudemaxxing", "loop-cron.sh"),
                    "#!/usr/bin/env bash\nMAX_TURNS=60\nharness-agent-run self-improve\n")
    os.chmod(wrapper, os.stat(wrapper).st_mode | stat.S_IEXEC)

    write(os.path.join(units, "armed-agent.service"),
          f"[Service]\nExecStart={wrapper}\n")
    write(os.path.join(units, "armed-agent.timer"),
          "[Timer]\nOnCalendar=*-*-* 07:00:00\n")
    os.symlink(os.path.join(units, "armed-agent.timer"),
               os.path.join(wants, "armed-agent.timer"))

    plain = write(os.path.join(home, ".config", "claudemaxxing", "plain.sh"),
                  "#!/usr/bin/env bash\necho deterministic\n")
    os.chmod(plain, os.stat(plain).st_mode | stat.S_IEXEC)
    write(os.path.join(units, "armed-plain.service"), f"[Service]\nExecStart={plain}\n")
    write(os.path.join(units, "armed-plain.timer"), "[Timer]\nOnCalendar=daily\n")
    os.symlink(os.path.join(units, "armed-plain.timer"),
               os.path.join(wants, "armed-plain.timer"))

    # installed but NOT linked into timers.target.wants → not armed
    write(os.path.join(units, "installed-only.service"), f"[Service]\nExecStart={wrapper}\n")
    write(os.path.join(units, "installed-only.timer"), "[Timer]\nOnCalendar=weekly\n")

    # The shape every REAL unit on this host actually uses: the interpreter is
    # the first token and the script is the second. Resolving the first token
    # finds /bin/bash, reads a binary, and silently reports "no cap" for a loop
    # that is in fact capped — the exact reading that would have let the ledger
    # call the daily self-improve loop uncapped.
    write(os.path.join(units, "armed-wrapped.service"),
          f"[Service]\nExecStart=/bin/bash {wrapper}\n")
    write(os.path.join(units, "armed-wrapped.timer"), "[Timer]\nOnCalendar=daily\n")
    os.symlink(os.path.join(units, "armed-wrapped.timer"),
               os.path.join(wants, "armed-wrapped.timer"))

    # A deterministic script that merely CONTAINS the harness's name. `claude` is
    # a substring of `claudemaxxing`, so a naive marker scan calls every one of
    # this host's hermes-* and cogload-* timers an agent loop and inflates both
    # the token estimate and the uncapped list.
    # It also READS ~/.claude/projects — a path component, not an invocation. On
    # the real host this was the last phantom: cogload-nightly digests
    # transcripts, spends zero model tokens, and was the only row left in the
    # uncapped list because of it.
    namesake = write(os.path.join(home, ".config", "claudemaxxing", "namesake.sh"),
                     "#!/usr/bin/env bash\n"
                     "# claudemaxxing backup: rsync the claudemaxxing state dir\n"
                     'PROJECTS_DIR="$HOME/.claude/projects"\n'
                     'rsync -a "$PROJECTS_DIR" /backup/ && cat ~/.claude/settings.json\n')
    os.chmod(namesake, os.stat(namesake).st_mode | stat.S_IEXEC)
    write(os.path.join(units, "armed-namesake.service"),
          f"[Service]\nExecStart=/bin/bash {namesake}\n")
    write(os.path.join(units, "armed-namesake.timer"), "[Timer]\nOnCalendar=daily\n")
    os.symlink(os.path.join(units, "armed-namesake.timer"),
               os.path.join(wants, "armed-namesake.timer"))

    if not all_capped:
        # an agent-invoking unit whose wrapper declares no turn/timeout cap
        nocap = write(os.path.join(home, ".config", "claudemaxxing", "nocap.sh"),
                      "#!/usr/bin/env bash\nharness-agent-run self-improve\n")
        os.chmod(nocap, os.stat(nocap).st_mode | stat.S_IEXEC)
        write(os.path.join(units, "armed-nocap.service"), f"[Service]\nExecStart={nocap}\n")
        write(os.path.join(units, "armed-nocap.timer"), "[Timer]\nOnCalendar=hourly\n")
        os.symlink(os.path.join(units, "armed-nocap.timer"),
                   os.path.join(wants, "armed-nocap.timer"))
    return home


def loops_repo(tmp, *, usage_rows=True):
    repo = os.path.join(tmp, "lrepo")
    os.makedirs(os.path.join(repo, ".results"), exist_ok=True)
    if usage_rows:
        rows = [{"input": 100, "output": 200, "cache_read": 1000,
                 "cache_creation": 10, "rc": 0},
                {"input": 300, "output": 400, "cache_read": 3000,
                 "cache_creation": 30, "rc": 0}]
        with open(os.path.join(repo, ".results", "round-usage.jsonl"), "w") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")
    write(os.path.join(repo, "CLAUDE.md"), "x\n")
    return repo


def l1_l3_rows():
    tmp = tempfile.mkdtemp(prefix="tl-l13-")
    home, repo = loops_home(tmp), loops_repo(tmp)
    d, r = run_json("loops", home, repo)
    if d is None:
        check("L1 loops --json parses", False, f"rc={r.returncode} err={r.stderr[:200]}")
        return
    rows = by_name(d.get("rows", []))
    required = {"trigger", "cap", "gate", "monthly_estimate", "est_basis", "armed",
                "agent_invoking"}
    check("L1 every row carries the ledger's five columns",
          rows and all(required <= set(v) for v in rows.values()),
          f"missing={[n for n, v in rows.items() if not required <= set(v)]}")
    armed = {n for n, v in rows.items() if v.get("armed")}
    check("L1 only units linked into timers.target.wants are armed",
          "armed-agent.timer" in armed and "installed-only.timer" not in armed,
          f"armed={sorted(armed)}")
    check("L2 ExecStart chain reaching harness-agent-run marks agent_invoking",
          rows.get("armed-agent.timer", {}).get("agent_invoking") is True
          and rows.get("armed-plain.timer", {}).get("agent_invoking") is False,
          f"agent={rows.get('armed-agent.timer')} plain={rows.get('armed-plain.timer')}")
    cap = rows.get("armed-agent.timer", {}).get("cap") or {}
    check("L3 the real cap is read from the wrapper (MAX_TURNS=60)",
          cap.get("max_turns") == 60, f"cap={cap}")
    check("L3 an agent loop with no discoverable cap is named in `uncapped`",
          "armed-nocap.timer" in (d.get("uncapped") or []),
          f"uncapped={d.get('uncapped')}")
    wrapped = rows.get("armed-wrapped.timer", {})
    check("L3 `ExecStart=/bin/bash <script>` resolves the SCRIPT, not the interpreter",
          (wrapped.get("cap") or {}).get("max_turns") == 60
          and wrapped.get("agent_invoking") is True,
          f"wrapped={wrapped}")
    check("L2 a script that only CONTAINS 'claudemaxxing' is not an agent loop",
          rows.get("armed-namesake.timer", {}).get("agent_invoking") is False,
          f"namesake={rows.get('armed-namesake.timer')}")
    check("L3 a non-agent unit is never listed as uncapped",
          "armed-namesake.timer" not in (d.get("uncapped") or [])
          and "armed-plain.timer" not in (d.get("uncapped") or []),
          f"uncapped={d.get('uncapped')}")


def l6_binary_execstart():
    """A service whose ExecStart is a real BINARY must not crash the ledger.

    Found on the real machine, not by this suite: every fixture wrapper here is
    UTF-8 shell script, but this host has units whose ExecStart points straight
    at a compiled binary, and reading one as text raised UnicodeDecodeError and
    took the whole `loops` verb down. Tier-1c in one line — a fully-fixtured
    contract is a statement about the fixtures.
    """
    tmp = tempfile.mkdtemp(prefix="tl-l6-")
    home = loops_home(tmp)
    units = os.path.join(home, ".config", "systemd", "user")
    blob = os.path.join(home, ".config", "claudemaxxing", "binary-exec")
    with open(blob, "wb") as fh:
        fh.write(b"\x7fELF\x02\x01\x01\x00" + bytes(range(200, 256)) * 4)
    os.chmod(blob, 0o755)
    write(os.path.join(units, "armed-binary.service"), f"[Service]\nExecStart={blob}\n")
    write(os.path.join(units, "armed-binary.timer"), "[Timer]\nOnCalendar=daily\n")
    os.symlink(os.path.join(units, "armed-binary.timer"),
               os.path.join(units, "timers.target.wants", "armed-binary.timer"))

    d, r = run_json("loops", home, loops_repo(tmp))
    check("L6 a binary ExecStart is survived, not crashed on",
          d is not None and r.returncode == 0,
          f"rc={r.returncode} err={r.stderr.strip()[-200:]}")
    if d:
        check("L6 the binary-backed unit still appears as a row",
              "armed-binary.timer" in by_name(d.get("rows", [])),
              f"rows={sorted(by_name(d.get('rows', [])))}")


def l4_uncapped_discriminates():
    tmp = tempfile.mkdtemp(prefix="tl-l4-")
    home, repo = loops_home(tmp, all_capped=True), loops_repo(tmp)
    d, r = run_json("loops", home, repo)
    if d is None:
        check("L4 loops --json parses", False, f"rc={r.returncode} err={r.stderr[:200]}")
        return
    check("L4 a fully-capped fixture yields an EMPTY uncapped list",
          d.get("uncapped") == [], f"uncapped={d.get('uncapped')}")


def l5_est_basis_honest():
    tmp = tempfile.mkdtemp(prefix="tl-l5-")
    home = loops_home(tmp)
    with_rows = loops_repo(tmp, usage_rows=True)
    d1, r1 = run_json("loops", home, with_rows)
    tmp2 = tempfile.mkdtemp(prefix="tl-l5b-")
    without = loops_repo(tmp2, usage_rows=False)
    d2, r2 = run_json("loops", home, without)
    if d1 is None or d2 is None:
        check("L5 loops --json parses in both fixtures", False,
              f"rc={r1.returncode}/{r2.returncode}")
        return
    agent_rows = [v for v in d1.get("rows", [])
                  if v.get("agent_invoking") and v.get("armed")]
    check("L5 with round-usage rows present, an agent row is basis='measured'",
          any(v.get("est_basis") == "measured" for v in agent_rows),
          f"bases={[(v['name'], v.get('est_basis')) for v in agent_rows]}")
    measured = next((v for v in agent_rows if v.get("est_basis") == "measured"), None)
    if measured:
        # (100+200+1000+10) + (300+400+3000+30) = 1310 + 3730; mean = 2520.0
        check("L5 the measured estimate is arithmetic over the fixture rows, not a guess",
              isinstance(measured.get("monthly_estimate"), (int, float))
              and measured["monthly_estimate"] > 0
              and measured.get("mean_round_tokens") in (2520, 2520.0),
              f"row={measured}")
    bases2 = {v.get("est_basis") for v in d2.get("rows", [])}
    check("L5 with no usage rows, nothing claims to be 'measured'",
          "measured" not in bases2 and bases2 <= {"replay-tax-model", "unmeasured", "n/a"},
          f"bases={bases2}")


def z_exit_codes():
    tmp = tempfile.mkdtemp(prefix="tl-z-")
    home, repo = audit_home(tmp), audit_repo(tmp)
    ok = run("audit", home, repo)
    bad = run("nonsense-verb", home, repo)
    check("Z audit exits 0 and an unknown verb exits non-zero",
          ok.returncode == 0 and bad.returncode != 0,
          f"audit_rc={ok.returncode} bad_rc={bad.returncode}")


if __name__ == "__main__":
    if not os.path.isfile(TOOL):
        print(f"token-ledger: MISSING {TOOL}")
        print("\n1 FAILURE(S)")
        sys.exit(1)
    print("token-ledger audit (chapter 18, exercise 1):")
    a1_a2_inventory_and_real_calls()
    a3_no_secret_leaks()
    a4_bytes_not_only_lines()
    a5_snapshots_immutable()
    a6_unreadable_is_unknown()
    a7_boundaries()
    print("token-ledger loops (chapter 18, exercise 3):")
    l1_l3_rows()
    l6_binary_execstart()
    l4_uncapped_discriminates()
    l5_est_basis_honest()
    z_exit_codes()
    if FAILS:
        print(f"\n{len(FAILS)} FAILURE(S)")
        for f in FAILS:
            print(f"  - {f}")
        sys.exit(1)
    print("\nall token-ledger contracts PASS")
