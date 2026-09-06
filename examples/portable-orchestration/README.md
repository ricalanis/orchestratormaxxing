# Portable orchestration examples

These scripts demonstrate the public `bin/memoryctl`, `bin/model-eval`, and `bin/session-log` primitives in **self-contained, disposable environments**. They never touch the caller's real project memory, docs, or provider credentials, and they never call live providers or network services.

## Files

- `memory.sh` — adds, supersedes, and consolidates safe invented facts in an isolated git repo, then verifies the final memory states.
- `model-eval.sh` — preregisters a tiny offline evaluation spec, runs the golden gate (contracts only), and reports from hand-written synthetic rows.
- `session-log.sh` — writes two durable changelog entries, overwrites the sticky WIP twice, shows the tail/recovery view, and runs the staleness check in an isolated repo.

## Running

From any directory:

```bash
/path/to/repo/examples/portable-orchestration/memory.sh
/path/to/repo/examples/portable-orchestration/model-eval.sh
/path/to/repo/examples/portable-orchestration/session-log.sh
```

Each script creates its own `mktemp` directory, exports isolated `HOME`/project overrides, and cleans up on exit.

## What is verified vs. what requires a live provider

- **memory.sh** and **session-log.sh** exercise the real CLI against real local state transitions; the assertions are deterministic.
- **model-eval.sh** demonstrates the offline part of the methodology: preregistration, the golden gate, and report rendering. The synthetic rows are intentionally not from a real model run; running `model-eval run` would require a valid Ollama Cloud key and network access.
