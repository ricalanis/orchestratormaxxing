#!/usr/bin/env bash
# Contract for the global OpenCode event plugin: interactive sessions must not
# call the delegation-only bridge, and best-effort hooks must stay TUI-silent.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PLUGIN="${PLUGIN_UNDER_TEST:-$ROOT/opencode/plugins/claudemaxxing-notify.js}"
JSRUN="$(command -v node 2>/dev/null || command -v bun 2>/dev/null || true)"
for candidate in /opt/homebrew/bin/node /usr/local/bin/node "$HOME/.local/bin/node" \
                 /opt/homebrew/bin/bun /usr/local/bin/bun "$HOME/.local/bin/bun"; do
  [[ -z "$JSRUN" && -x "$candidate" ]] && JSRUN="$candidate"
done
[[ -n "$JSRUN" ]] || { printf 'opencode-event-plugin: node/bun unavailable\n' >&2; exit 1; }

"$JSRUN" - "$PLUGIN" <<'JS'
const pluginPath = process.argv[2]
const { ClaudemaxxingNotify } = await import(pluginPath)

async function exercise(delegated) {
  if (delegated) process.env.CLAUDEMAXXING_O_DELEGATED = "1"
  else delete process.env.CLAUDEMAXXING_O_DELEGATED
  const calls = []
  const shell = (strings, ...values) => {
    calls.push(strings.reduce((out, part, i) => out + part + (i < values.length ? String(values[i]) : ""), ""))
    return Promise.resolve({ exitCode: 0 })
  }
  const plugin = await ClaudemaxxingNotify({
    directory: "/tmp/project",
    $: shell,
    client: { session: { messages: async () => ({ data: [{
      info: { role: "assistant", id: "msg_contract", finish: "stop" },
      parts: [{ type: "text", text: "done" }],
    }] }) } },
  })
  await plugin.event({ event: { type: "session.idle", properties: { sessionID: "ses_contract" } } })
  return calls
}

const interactive = await exercise(false)
if (interactive.some((call) => call.includes("o bind-event"))) {
  throw new Error("interactive session called delegation-only bind-event")
}
const delegated = await exercise(true)
if (delegated.filter((call) => call.includes("o bind-event")).length !== 1) {
  throw new Error("delegated idle event did not call bind-event exactly once")
}
for (const call of [...interactive, ...delegated]) {
  if (!call.includes(">/dev/null 2>&1 || true")) {
    throw new Error(`best-effort hook can leak an error into the TUI: ${call}`)
  }
}
JS

printf 'opencode-event-plugin: PASS\n'
