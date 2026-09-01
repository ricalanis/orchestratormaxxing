# Exact Warp terminal recovery for `c`, `g`, `o`, `c-ubuntu`, and `g-ubuntu`

## Decision

Extend Warp's native Session Restoration with a small local registry; do not add
a daemon. Warp restores each terminal's `WARP_TERMINAL_SESSION_UUID` across app
restarts. The `c`/`g`/`o` launchers bind that stable terminal UUID to an exact
tmux session. Claude and Codex SessionStart hooks, plus OpenCode's idle event,
add the exact agent session ID.

Warp owns the graphical reconstruction: windows, tabs, split layout, and each
split's stable UUID. This helper never redraws or guesses that layout; it only
reattaches the exact tmux/agent session assigned to each restored UUID.

Recovery is gated by the owning Warp app process generation. On Linux that
generation includes the kernel boot ID, so a reboot cannot look like the prior
Warp process merely because PID and process start fields repeat. Reopening a tab,
window, or shell while the same Warp process is alive is therefore inert. A new
Warp generation means the whole app quit or crashed; only then may a restored
terminal claim its prior binding.

The rc file only ARMS recovery. The actual claim runs from the first shell
prompt hook, after Warp's own `Bootstrapped` and `Precmd` frames. Executing tmux
directly while `.bashrc`/`.zshrc` is still being sourced preempts Warp's shell
bootstrap: the terminal remains visible, but its editor has no live shell
session to receive typed input. Bash preserves the hook through
`PROMPT_COMMAND`; Zsh sends Warp's appended `warp_precmd` frame before attach.
Because a prompt callback is not a user-submitted command, recovery then calls
Warp's own `warp_preexec` hook before attaching. That transition moves keyboard
ownership from Warp's command editor to the tmux-backed terminal; a read-write
tmux client alone is not sufficient.

## Recovery order

1. If the exact tmux session still exists and its stored terminal UUID matches,
   attach it. `session_attached` is not an ownership signal: a crashed terminal
   or SSH transport may remain counted until timeout. Recovery co-attaches and
   never uses `-d`, `detach-client`, read-only mode, or client killing.
2. If tmux is gone but an exact agent session ID was captured, recreate the same
   tmux name and cwd, source the installed launcher, and call `c ... --resume
   <id>`, `g ... resume <id>`, or `o ... -s <id>`. The nested launcher path then starts the agent
   with its normal safety flags without creating a second tmux session.
3. If identity, ownership, metadata, or state is ambiguous, do nothing.

For `c-ubuntu`/`g-ubuntu`, `gpu-agent` uses two explicit SSH phases only for an
interactive local Warp launch. The first creates the remote c/g session
detached, tags it with the stable local terminal UUID, and returns its exact
name. The local registry records that name, then the second phase validates the
remote UUID tag and attaches it. After a local reboot, the prompt hook repeats
only the exact validated attach. Ordinary non-Warp, prompt, headless, explicit
attach, and explicit detach calls retain the legacy one-call path. If the
Ubuntu tmux server also rebooted, recovery fails to a writable local shell; it
does not guess or create a new agent identity.

There is no cwd/title/order guessing and no `--last`, `--continue`, or fuzzy tmux
target. A lock makes a restored terminal binding one-shot within each Warp app
generation, including when several restored shells start concurrently.

Each tmux name has exactly one terminal UUID owner. After a launcher creates the
session it tags the exact session scope with `@warp_terminal_uuid`; a crash-time
attach refuses missing/mismatched tags, and an untaggable newly reconstructed
session is removed so a later attempt can retry safely.

## State and privacy

`warp-agent-recovery` stores only terminal UUID, Warp generation, agent kind,
exact tmux name, cwd, and exact agent session ID under the user's state directory.
Writes are locked and atomic. Prompt text, transcripts, commands, and model output
are never recorded.

## Verification

- `tests/warp-agent-recovery/run.sh`: boot-aware generation gate, unique UUID
  ownership, exact local/Ubuntu actions, non-destructive co-attachment,
  mismatch fail-closed paths,
  untaggable-session cleanup, concurrency, and privacy using a tmux simulator.
- `tests/warp-recovery-shell/run.sh`: launcher ordering/environment propagation,
  prompt-deferred Bash/Zsh recovery, the Preexec-before-attach transition for
  `c`/`g`/`o`, and interactive top-level startup guards.
- `tests/ubuntu-launchers/run.sh`: exact remote preparation, UUID tagging, local
  registration, PTY attachment, cwd mapping, and literal argv preservation.
- `tests/warp-agent-install/run.sh`: idempotent deployment, exact installed
  launcher bytes, Claude/Codex hook wiring, and OpenCode recovery wiring.

The real-Warp smoke test is deliberately manual because it requires restarting
the graphical app. It must confirm that every restored split retains its own
UUID and receives only its own c/g/o session. Do not run it while active work
cannot tolerate a terminal restart.

Any future real-tmux contract must address every tmux call through an explicit,
unique test socket (`-L`/`-S`), assert that the production socket is unchanged,
and avoid all `kill-server` operations. `TMUX_TMPDIR` alone is not accepted as
isolation evidence after an attempted boundary test coincided with live `g`
session loss on 2026-08-13.
