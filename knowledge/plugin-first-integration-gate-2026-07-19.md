# Plugin-first integration gate

## What happened

The custom Claude→Codex `orch-*` layer grew into a ledger, dispatcher, pane monitor,
escalation state machine, install wiring, tests, and governed memory. Its core job already
existed in OpenAI's official `codex-plugin-cc`, backed by the Codex app server and exposed
inside Claude Code as rescue, transfer, status, result, and cancel commands.

The mistake was not prototyping. It was allowing a prototype to become a persistent control
plane before doing a bounded ecosystem search. Once it touched install, tmux, host doctrine,
tests, and memory, replacing it became a cross-layer migration instead of a one-command
plugin install.

## Gate before a large custom addition

Trigger this gate when a proposal adds any of the following:

- a persistent daemon, queue, ledger, monitor, scheduler, or background-job manager;
- cross-host or cross-agent orchestration;
- three or more integration files across install, hooks, commands, skills, or memory;
- a new protocol adapter around a host that already has plugins, MCP, or an app server;
- a feature whose lifecycle and compatibility burden will outlive the first task.

Spend a short, bounded discovery pass first:

1. Search the official marketplace/plugin registry for every host involved.
2. Search the vendors' official repositories and current documentation.
3. Inventory native CLI, app-server, MCP, hooks, subagents, and background-task primitives.
4. Check maintained community options only after the official surfaces.
5. Write one line: `ADOPT`, `EXTEND`, or `BUILD`, with the missing capability that justifies it.

## Selection checklist

Existence is not enough. Before adopting, verify:

- maintainer and provenance;
- release activity and compatibility with installed host versions;
- permissions, authentication reuse, data flow, and local state paths;
- quota, retry, review-gate, and crash-loop behavior;
- cancellation, resume, status, and recovery semantics;
- whether it composes with existing operator layers instead of replacing them.

For this migration the answer is `ADOPT`: OpenAI owns both Codex and the plugin, the plugin
uses the installed Codex CLI/app server and existing authentication, and it supplies the
delegation lifecycle directly. The optional review gate stays disabled because an automatic
Claude↔Codex review loop can drain quota.

## Boundary retained

Adopting a plugin does not mean collapsing unrelated layers. The official plugin owns
Claude→Codex delegation. The `c` and `g` helpers own manual tmux session lifecycle.
`tmux-send` remains a remote operator primitive for `gpu-agent`. The Hermes dashboard owns
human-facing session visibility and management. Each layer has one reason to exist.

## Default reminder

Before we build something large around an agent host: **search official plugins and native
surfaces first, then decide adopt/extend/build.** Ten minutes of discovery is cheaper than
unwinding a second control plane.
