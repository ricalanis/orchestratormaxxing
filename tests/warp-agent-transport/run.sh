#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TOOL="$ROOT/bin/warp-agent-event"
TMUX_CONF="$ROOT/shell/tmux.conf"

fail() { printf 'warp-agent-transport contract: %s\n' "$*" >&2; exit 1; }
. "$ROOT/tests/lib/precondition.sh"
harness_need_cmd tmux "warp-agent-transport: tmux"
[[ -x "$TOOL" ]] || fail 'helper missing or not executable'
grep -Fq 'set-option -g allow-passthrough on' "$TMUX_CONF" || \
  fail 'tmux passthrough is not narrowly enabled'
grep -Fq 'set-option -s set-clipboard external' "$TMUX_CONF" || \
  fail 'tmux external clipboard writes are not enabled'
if grep -Eq 'allow-passthrough[[:space:]]+all' "$TMUX_CONF"; then
  fail 'broad passthrough mode is forbidden'
fi

python3 - "$TOOL" "$TMUX_CONF" <<'PY'
import os
import pathlib
import pty
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time

tool, tmux_conf = sys.argv[1:]
tmux = shutil.which("tmux")
assert tmux

# Keep the scratch prefix and socket name SHORT. tmux binds a unix socket at
# $TMUX_TMPDIR/tmux-<uid>/<socket>, and sun_path caps that path at 104 bytes on
# macOS (108 on Linux). macOS TMPDIR is already a ~48-byte /var/folders/... path,
# so descriptive names overflow it and tmux fails with a bare "File name too
# long" that check=True then hid behind a CalledProcessError (lq-5750f48f).
with tempfile.TemporaryDirectory(prefix="wat-") as td:
    root = pathlib.Path(td)
    socket = f"wat-{os.getpid()}"
    trigger = root / "go"
    env = os.environ.copy()
    env["TMUX_TMPDIR"] = str(root)
    env["TERM"] = "xterm-256color"

    # Fail loudly and specifically if the platform limit is still exceeded,
    # rather than letting tmux surface an opaque connect error.
    sock_path = root / f"tmux-{os.getuid()}" / socket
    limit = 104 if sys.platform == "darwin" else 108
    assert len(os.path.realpath(sock_path)) < limit, (
        f"tmux socket path is {len(os.path.realpath(sock_path))} bytes, over the "
        f"{limit}-byte sun_path limit: {sock_path}"
    )
    command = (
        f'while [ ! -f "{trigger}" ]; do sleep 0.05; done; '
        f'env WARP_IS_LOCAL_SHELL_SESSION=1 WARP_CLI_AGENT_PROTOCOL_VERSION=1 '
        f'"{tool}" claude session_start; sleep 0.3'
    )
    # Do NOT use check=True here: CalledProcessError renders only the argv, so
    # tmux's actual diagnosis is lost and the contract fails unreadably.
    started = subprocess.run(
        [tmux, "-L", socket, "-f", tmux_conf, "new-session", "-d", "-s", "claude-wire", command],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    if started.returncode != 0:
        raise AssertionError(
            f"tmux new-session failed (rc={started.returncode}): "
            f"{started.stderr.strip() or '<no stderr>'}"
        )
    try:
        pid, master = pty.fork()
        if pid == 0:
            os.execvpe(tmux, [tmux, "-L", socket, "attach-session", "-t", "claude-wire"], env)

        time.sleep(0.25)
        trigger.touch()
        data = bytearray()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.1)
            if ready:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                data.extend(chunk)
            done, _ = os.waitpid(pid, os.WNOHANG)
            if done == pid:
                break
        else:
            os.kill(pid, signal.SIGTERM)
            os.waitpid(pid, 0)
            raise AssertionError("attached tmux client did not exit")
        os.close(master)

        wire = bytes(data)
        sentinel = b'\x1b]777;notify;warp://cli-agent;'
        assert sentinel in wire, wire[-1000:]
        assert b'{"v":1,"agent":"claude","event":"session_start"}\x07' in wire
        assert b'\x1bPtmux;' not in wire, "tmux did not unwrap the DCS passthrough"
    finally:
        listed = subprocess.run(
            [tmux, "-L", socket, "list-sessions", "-F", "#{session_name}"],
            env=env, text=True, capture_output=True,
        )
        for session in listed.stdout.splitlines():
            subprocess.run(
                [tmux, "-L", socket, "kill-session", "-t", "=" + session], env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
PY

printf 'warp-agent-transport contract: PASS\n'
