// orchestratormaxxing — OpenCode done-notification plugin.
// Bridges OpenCode's session.idle event into the harness's deterministic
// Telegram notifier (bin/agent-done-notify --agent opencode), the same path
// Claude Code's Stop hook and Codex's notify hook use. Best-effort by design:
// a notification plugin must NEVER break or slow the session, so every step
// is wrapped and failures are silent (mirrors agent-done-notify's own
// always-exit-0 contract).
//
// Source of truth: <repo>/opencode/plugins/orchestratormaxxing-notify.js;
// install.sh deploys a copy to ~/.config/opencode/plugins/.

export const OrchestratormaxxingNotify = async ({ directory, client, $ }) => {
  // This plugin is installed globally, so it also receives events from a
  // human's ordinary interactive `o` session. Only `o delegate` marks its
  // OpenCode process tree at birth; never ask the machine-only event bridge to
  // bind an unmarked session.
  const delegatedWorker = process.env.ORCHESTRATORMAXXING_O_DELEGATED === "1"

  const safeErrorCode = (error) => {
    if (!error) return ""
    if (typeof error === "string") return error.slice(0, 64)
    if (typeof error !== "object") return ""
    const value = error.statusCode ?? error.code ?? error.name ?? ""
    return String(value).slice(0, 64)
  }

  return {
    event: async ({ event }) => {
      try {
        if (!event) return

        // Needs-input: OpenCode emits the permission.asked EVENT at runtime
        // (packages/opencode/src/permission/index.ts publishes it with the
        // PermissionRequest fields; the "permission.ask" plugin HOOK is
        // declared upstream but never triggered — do not register it).
        // Bridged as hook_event_name PermissionRequest so the tool's own
        // 120s per-session cooldown applies — parity with Claude/Codex
        // needs-input, no plugin-side copy of the cooldown.
        if (event.type === "permission.asked") {
          const p = event.properties ?? {}
          const sessionID = p.sessionID ?? p.sessionId ?? ""
          const patterns = Array.isArray(p.patterns)
            ? p.patterns.filter((x) => typeof x === "string")
            : []
          const reason =
            [typeof p.permission === "string" ? p.permission : "", patterns.join(", ")]
              .filter(Boolean)
              .join(": ") || "permiso pendiente"
          const payload = JSON.stringify({
            hook_event_name: "PermissionRequest",
            cwd: directory,
            session_id: sessionID,
            message: reason,
          })
          await $`printf '%s' ${payload} | agent-done-notify --agent opencode >/dev/null 2>&1 || true`
          return
        }

        if (event.type === "session.error") {
          const p = event.properties ?? {}
          const sessionID = p.sessionID ?? p.sessionId ?? ""
          const errorCode = safeErrorCode(p.error)
          if (delegatedWorker && sessionID && errorCode) {
            const handoffPayload = JSON.stringify({
              session_id: sessionID,
              message_id: "",
              finish: "error",
              text: "",
              error_code: errorCode,
            })
            try {
              await $`printf '%s' ${handoffPayload} | o bind-event --json >/dev/null 2>&1 || true`
            } catch {}
          }
          return
        }

        if (event.type !== "session.idle") return
        const sessionID =
          event.properties?.sessionID ?? event.properties?.sessionId ?? ""

        // Bind the exact terminal assistant message to the delegated run.
        // Empty text remains load-bearing: Root must distinguish a provider
        // empty from a completed answer lost in the alternate-screen pane.
        let summary = ""
        let handoffPayload = ""
        try {
          const resp = await client.session.messages({ path: { id: sessionID } })
          const msgs = resp?.data ?? resp ?? []
          for (let i = msgs.length - 1; i >= 0; i--) {
            const m = msgs[i]
            const role = m?.info?.role ?? m?.role
            if (role !== "assistant") continue
            const info = m?.info ?? m ?? {}
            const parts = m?.parts ?? []
            const text = parts
              .filter((p) => p?.type === "text" && typeof p?.text === "string")
              .map((p) => p.text)
              .join("\n")
              .trim()
            summary = text
            handoffPayload = JSON.stringify({
              session_id: sessionID,
              message_id: info.id ?? m?.id ?? "",
              finish: info.finish ?? "",
              text,
              error_code: safeErrorCode(info.error),
            })
            break
          }
        } catch {}

        if (delegatedWorker && handoffPayload) {
          try {
            await $`printf '%s' ${handoffPayload} | o bind-event --json >/dev/null 2>&1 || true`
          } catch {}
        }

        const payload = JSON.stringify({
          hook_event_name: "Stop",
          cwd: directory,
          session_id: sessionID,
          last_assistant_message: summary,
        })
        const recoveryPayload = JSON.stringify({
          cwd: directory,
          session_id: sessionID,
        })
        // The idle event runs inside the exact o/tmux process tree, so the
        // helper can bind this OpenCode ID to that pane's stable Warp UUID.
        // Repeating this on later idle events is an idempotent refresh.
        try {
          await $`printf '%s' ${recoveryPayload} | warp-agent-recovery register-agent opencode >/dev/null 2>&1 || true`
        } catch {}
        // Bun shell interpolation quotes ${payload} safely; agent-done-notify
        // reads the JSON from stdin, sanitizes, caps, and delivers.
        await $`printf '%s' ${payload} | agent-done-notify --agent opencode >/dev/null 2>&1 || true`
      } catch {}
    },
  }
}
