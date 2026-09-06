// orchestratormaxxing — OpenCode security guard plugin.
// Bridges OpenCode's tool.execute.before/after hooks into the harness's
// deterministic cross-host guard (bin/agent-guard), the same runner Claude
// Code's PreToolUse/PostToolUse hooks and Codex's plugin hooks call.
//
//   * before: `bash` with a string command → `agent-guard pre --format json`;
//     a "deny" decision THROWS, which is what aborts the tool call and hands
//     the reason to the model. This is the one place the plugin must not
//     swallow an error.
//   * after: `write`/`edit` → normalized to the Claude payload shape →
//     `agent-guard post --format json`; a non-empty guidance block is appended
//     to the tool output after a blank line. Never throws.
//
// Fail-open by design: a guard that cannot run or cannot be parsed allows
// (mirrors bin/agent-guard's own missing-patterns behaviour) — coverage is
// measured by `agent-guard status --gate`, never inferred from silence.
// Other tools never shell out.
//
// Source of truth: <repo>/opencode/plugins/orchestratormaxxing-guard.js;
// install.sh deploys a copy to ~/.config/opencode/plugins/.

export const OrchestratormaxxingGuard = async ({ $ }) => {
  const GUARD_TIMEOUT_MS = 8000

  const runGuard = async (verb, payload) => {
    // The payload goes to the guard's STDIN, never into argv: `printf '%s' ${payload}`
    // execs /usr/bin/printf, and Linux MAX_ARG_STRLEN (128 KiB) made it fail E2BIG
    // on a 140 KB command — the guard then read an empty stdin and answered
    // "allow" (measured 2026-09-05). Bun's shell parser rejects a `2>/dev/null`
    // next to an object redirect ("expected a command … got: Redirect"), so
    // stderr (the human-facing channel) is swallowed by .quiet() instead; stdout
    // carries the JSON.
    const input = Buffer.from(payload)
    const run =
      verb === "pre"
        ? $`agent-guard pre --format json < ${input}`
        : $`agent-guard post --format json < ${input}`
    const quiet = typeof run?.quiet === "function" ? run.quiet() : run
    // A hung guard must never stall the TUI: after the timeout the caller's
    // catch fails open (before) or leaves the output untouched (after).
    let timer = null
    const timeout = new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error("agent-guard timeout")), GUARD_TIMEOUT_MS)
      if (typeof timer?.unref === "function") timer.unref()
    })
    let r
    try {
      r = await Promise.race([quiet, timeout])
    } finally {
      if (timer) clearTimeout(timer)
    }
    const text = typeof r?.text === "function" ? await r.text() : String(r?.stdout ?? "")
    return JSON.parse(text)
  }

  return {
    "tool.execute.before": async (input, output) => {
      if (String(input?.tool ?? "").toLowerCase() !== "bash") return
      const command = output?.args?.command
      if (typeof command !== "string") return
      let verdict = null
      try {
        verdict = await runGuard("pre", JSON.stringify({ tool_name: "Bash", tool_input: { command } }))
      } catch {
        return // fail open: a broken guard must never brick the terminal tool
      }
      if (verdict && verdict.decision === "deny") {
        const reason =
          typeof verdict.reason === "string" && verdict.reason
            ? verdict.reason
            : "blocked by the dangerous-command guard"
        throw new Error("agent-guard: " + reason)
      }
    },

    "tool.execute.after": async (input, output) => {
      try {
        const tool = String(input?.tool ?? "").toLowerCase()
        const args = input?.args ?? {}
        let payload = null
        if (tool === "write" && typeof args.filePath === "string" && typeof args.content === "string") {
          payload = { tool_name: "Write", tool_input: { file_path: args.filePath, content: args.content } }
        } else if (tool === "edit" && typeof args.filePath === "string" && typeof args.newString === "string") {
          payload = { tool_name: "Edit", tool_input: { file_path: args.filePath, new_string: args.newString } }
        }
        if (!payload) return
        const result = await runGuard("post", JSON.stringify(payload))
        const context = typeof result?.context === "string" ? result.context : ""
        if (!context || !output) return
        output.output = String(output.output ?? "") + "\n\n" + context
      } catch {}
    },
  }
}
