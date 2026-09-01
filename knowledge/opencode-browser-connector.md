# OpenCode browser connector — CDP MCP server for kimi-coder / glm-coder

`bin/opencode-browser-mcp` gives the OpenCode coding agents (`kimi-coder`, `glm-coder`,
both on Ollama Cloud) the same browser-automation capability Claude Code has, as a set of
MCP tools over the Chrome DevTools Protocol (CDP). Zero dependencies — a single Node ≥ 22
file (built-in `WebSocket` + `node:http`), speaking MCP stdio (newline-delimited JSON-RPC).

## Architecture

```
Chrome (--remote-debugging-port=18800)           ← shared human + agent browser
  ↓  CDP: HTTP /json/* + WebSocket /devtools/page/<id>
bin/opencode-browser-mcp                          ← deployed to ~/.local/bin by install.sh
  ↓  MCP stdio (tools)
OpenCode agents (kimi-coder / glm-coder)          ← registered as mcp server "browser"
```

`install.sh` deploys the bridge to `~/.local/bin/opencode-browser-mcp` and registers it in
`~/.config/opencode/opencode.json` under `mcp.browser` (idempotent; `enabled` uses
setdefault so a user opt-out survives re-runs). Unlike `hermes-orchestrator`, this MCP
server IS a copied bridge — it's a single self-contained file with no repo imports.

## Tools (OpenCode surfaces them prefixed: `browser_browser_navigate`, …)

| tool | args | does |
|---|---|---|
| `browser_navigate` | `url` | Navigate + wait for load event; returns final URL + title |
| `browser_screenshot` | `path?`, `as_base64?` | PNG → file path (default `~/.cache/opencode-browser-mcp/`) or base64 image content |
| `browser_click` | `selector` | Scroll into view + click first CSS match |
| `browser_type` | `selector`, `text` | Set value via native setter + fire `input`/`change` (React-safe) |
| `browser_extract` | `selector?` (default `body`), `wait_for?`, `wait_ms?` | Visible text of up to 50 matches (incl. open shadow DOM + same-origin iframes), 60k-char cap; `wait_for` polls a selector (≤10s) for dynamic content |
| `browser_eval` | `javascript` | Evaluate in page context (REPL semantics — `const`/`let` don't leak between calls, top-level `await` works), promises awaited, JSON result |
| `browser_get_url` | — | Current `location.href` |
| `browser_get_title` | — | Current `document.title` |

Errors (no matching element, page exception, unreachable Chrome) come back as
`isError` tool results with a readable message, so the agent can adjust.

## Config (env, set via `mcp.browser.environment` in opencode.json)

- `CDP_URL` — Chrome DevTools endpoint. Default `http://127.0.0.1:18800`.
- `CDP_HOST_HEADER` — Host header sent to Chrome, default `localhost`. **Load-bearing for
  remote access**: Chrome rejects DevTools HTTP requests whose Host header is not an IP or
  localhost (`Host header is specified and is not an IP address or localhost.` — verified
  against Chrome 149). The server always sends `Host: localhost` on the `/json/*` endpoints
  and rewrites the reported `ws://localhost:18800/...` target URL to go through `CDP_URL`'s
  host/port. The WebSocket upgrade itself is not Host-validated by Chrome (also verified).
- `BROWSER_MCP_SHOT_DIR` — screenshot dir, default `~/.cache/opencode-browser-mcp`.

## Local setup (verified end-to-end 2026-07-11)

```bash
# 1. Chrome with CDP (headless or headed)
google-chrome --headless=new --remote-debugging-port=18800 --user-data-dir=/tmp/cdp-profile &

# 2. Nothing else — OpenCode spawns the MCP server itself
opencode run --agent kimi-coder "Navigate to https://example.com and extract the page title"
```

Verified: kimi-coder called `browser_browser_navigate` → "Example Domain"; the fuller flow
(navigate + screenshot + h1 extract) returned a real 780×493 PNG path + `Example Domain`.
A deterministic 23-assertion contract (initialize / tools-list / all 8 tools incl. error
paths, unicode, shadow DOM, iframes, dynamic content, eval isolation) passes against
Chrome 149 headless.

## Text extraction & eval semantics (deep-dive fixes, 2026-07-13)

Root causes found and fixed in the 2026-07-13 investigation (v1.1.0):

- **`browser_eval` state leak** — `Runtime.evaluate` runs in the page's global lexical
  scope, so a top-level `const x` persisted across calls and a repeat threw
  `SyntaxError: Identifier already declared`. Fixed with CDP `replMode: true`
  (DevTools-console semantics: redeclaration allowed, top-level `await` enabled).
  Caveat handled: the REPL wrapper boxes the completion value so `awaitPromise` no
  longer unwraps promise results — the server explicitly `Runtime.awaitPromise`s a
  promise-valued result and serializes objects via `Runtime.callFunctionOn`, keeping
  the documented "promises are awaited" behavior. Internal tool scripts (click/type/
  extract IIFEs) still use the plain non-REPL path.
- **`browser_extract` missed shadow DOM and iframes** — `innerText` returns `""` for a
  shadow host and skips iframe content entirely. The extractor now recurses into open
  shadow roots (nested included, depth-capped at 8) and same-origin `iframe`/`frame`
  documents. Closed shadow roots and cross-origin frames remain out of reach by design.
  Appended shadow/iframe text follows the host element's text (not strict document order).
- **Dynamic content raced extraction** — content rendered after the load event was
  missed with no way to wait. `browser_extract` now takes `wait_for` (poll a CSS
  selector up to 10s; errors on timeout) and `wait_ms` (fixed delay, ≤15s).
- **Truncation could split surrogate pairs** — the 60k-char cap used a bare `.slice()`,
  which can cut an emoji in half leaving a lone surrogate (U+FFFD downstream). All
  truncation now backs off to a code-point boundary.
- **Encoding is NOT a transport problem** — CDP JSON/WebSocket and Node stdio are clean
  UTF-8 end to end (verified: accents, em-dash, emoji, CJK round-trip intact). Mojibake
  (`cafÃ©`) only appears when the *page itself* lacks a charset declaration and Chrome
  falls back to windows-1252 — a page authoring bug, not a connector bug. (The contract's
  own test page had exactly this latent bug: no `meta charset`; fixed there too.)
- **CDP quirks worth knowing**: `returnByValue` serializes `Map`/`Set` to `{}` and
  errors on cyclic objects ("Object reference chain is too long") — have the eval'd
  JS return plain JSON-able data. `--headless=new` shares the headed rendering
  pipeline, so `innerText` visibility/layout semantics match headed Chrome.

## Production deployment — everything local on the GPU box (LIVE since 2026-07-11)

The topology: Chrome, the MCP server, Hermes, and the OpenCode agents all run **on
the GPU box** (RTX 3060 Ti). The CDP port never leaves loopback — Chrome
binds `127.0.0.1:18800`, so the browser is reachable *only* by processes on this box.
That loopback boundary is the access control; no tunnel, no exposure.

Four pieces, all armed:

1. **Endpoint** — `chrome-cdp.service` (systemd `--user`, from `deploy/chrome-cdp.service`,
   shipped by install.sh): persistent **headed, native-Wayland** Chrome on
   `127.0.0.1:18800` with the real profile at `~/.config/clawdbot/cdp-profile`, so the operator's
   keyboard/mouse work alongside the agents' CDP calls — headless Chrome can never
   receive OS input; the DevTools screencast only re-encodes mouse well, keyboard
   badly). `--ozone-platform=wayland` is **load-bearing for keyboard input** and pinned
   explicitly: on this stack (Chrome 149 + mutter 48) Chromium's X11/XWayland backend
   never gains keyboard focus and silently drops ALL key events — XTEST, mutter virtual
   keyboard, keycode and keysym paths — while holding X input focus (measured 2026-07-13
   via an injection matrix; confirmed end-to-end with real RDP keystrokes). Any Chrome
   run with `--ozone-platform=x11` here is keyboard-dead for humans. Bound to
   `graphical-session.target` (starts on login, stops on logout — a headed window can't
   outlive its display) with `Restart=always` (closing the window exits 0, but agents
   depend on the endpoint — it reappears in 5s; stop it via `systemctl --user stop
   chrome-cdp`). install.sh re-syncs the unit file but never bounces an armed service
   (a restart would yank the browser from an agent mid-task). The same guarantee is
   generalized to **every** debug-port Chrome on the box by
   `bin/chrome-debug-wayland-shim`, installed by install.sh (Linux only) as
   `~/.local/bin/google-chrome{,-stable}`: any PATH-based launch carrying
   `--remote-debugging-port` under a Wayland session gets `--ozone-platform=wayland`
   injected (an explicit `x11` is rewritten, loudly); everything else passes through
   untouched. Absolute-path launches (`/opt/google/chrome/chrome`, puppeteer
   `executablePath`) bypass it — fix those launchers individually (clawdbot's was
   relaunched on Wayland 2026-07-13).
2. **OpenCode path** — `mcp.browser` in `~/.config/opencode/opencode.json` (install.sh,
   idempotent). Tools appear as `browser_browser_*` in kimi-coder/glm-coder.
3. **Hermes path** — registered in `~/.hermes/config.yaml` `mcp_servers` via
   `hermes mcp add browser --command node --args ~/.local/bin/opencode-browser-mcp`
   (CLI only — never hand-edit config.yaml, never restart hermes-gateway; new sessions
   pick the tools up automatically). So Hermes-native sessions AND the OpenCode agents
   Hermes spins off both ride the same binary and the same Chrome.
4. **Weekly self-update** — Hermes cron job "Browser MCP weekly update"
   (`30 5 * * 0`, `--script browser-mcp-update.sh --no-agent`, deployed to
   `~/.hermes/scripts/` from `deploy/browser-mcp-update.sh`). Mirrors Claude's update
   flow: clean-tree-guarded `git pull --rebase` → `./install.sh` → `harness-verify` →
   endpoint smoke (one bounded chrome-cdp restart) → `browser-mcp-contract` → Hermes
   registration drift-repair. Silent when green; on failure it reports via Telegram AND
   `loop-queue add`s the flaw for the self-improve loop. Sunday 05:30 — clear of the
   07:00 daily loop tick so the two never race the git sync.

### The one health command

```bash
browser-mcp-contract
```

`bin/browser-mcp-contract` (deployed globally) checks the whole stack: install checks
(bridge deployed, opencode.json + Hermes registration, cron job present, service active)
then the live 23-assertion MCP protocol contract against the real Chrome (all 8 tools,
error paths, PNG magic bytes, unicode/shadow-DOM/iframe/dynamic-content extraction,
eval isolation, surrogate-safe truncation) using a self-served local test page — no
external network.
Exit 0 + "ALL GREEN" = safe to build or update browser-using skills on this stack.
Note: the live contract navigates the shared browser's tab (and resets it to
about:blank after) — don't run it while an agent is mid-browse.

### Cross-machine access (optional, not the production path)

If a connector on *another* tailnet machine ever needs this box's Chrome, expose the port
tailnet-only (`tailscale serve --bg --tcp 18800 tcp://127.0.0.1:18800`, or socat bound to
`$(tailscale ip -4)`) and set `mcp.browser.environment.CDP_URL` to
`http://<gpu-box>.<tailnet>.ts.net:18800` in opencode.json (`environment` is
user-owned — install.sh never overwrites it). The required `Host: localhost` override is
already default behavior. Never use `tailscale funnel` for this (public internet).

## Notes / limits

- **Human + agent co-browsing**: since the endpoint went headed (2026-07-13), the Chrome
  window lives on the desktop session — typing/clicking in it directly works, including
  through the `gpu-desktop` RDP mirror. Agents drive the same tab over CDP; expect
  surprises if both act at once (an agent navigating mid-keystroke wins).
- Attaches to the **first page target** (creates `about:blank` if none). Multi-tab
  orchestration is out of scope v1.
- `browser_click`/`browser_type` are DOM-level (JS `click()` + native value setter), not
  trusted input events — right for forms and research, not for sites that require real
  input provenance. If that's ever needed, `Input.dispatchMouseEvent`/`insertText` are the
  CDP upgrade path.
- The MCP server holds one CDP WebSocket, lazily connected on first tool call and
  re-established automatically if Chrome restarts (pending commands fail fast with
  "CDP connection closed").
- Chrome must be started with `--remote-debugging-port` — the connector never launches
  Chrome itself (deliberate: whose Chrome/profile runs where is a human decision).
- harness-verify checks this tool with `node --check` (node-shebang support added
  alongside this connector) and enforces its deploy coverage in install.sh like every
  other bridge.
