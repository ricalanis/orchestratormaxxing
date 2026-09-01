---
name: memory
description: Read, add, reverify, import, or supersede governed project memory shared by Claude Code and Codex through memoryctl. Use when the user asks either host to remember something, recalls prior project context, migrates Claude memory, or changes an existing belief; never write credentials or bypass the contradiction gate.
---

Use `memoryctl`; do not edit Codex's private SQLite store and do not hand-edit the shared index.

1. Recall
   - SessionStart provides a bounded active-memory brief. Run `memoryctl list` if more discovery is needed, then `memoryctl show <name>` only for facts relevant to the task.
   - Sensitive memory stays undisclosed unless the current task actually requires it.
2. Add
   - Check `memoryctl list` for an existing fact on the same subject.
   - Routine new fact: pipe the body to `memoryctl add <kebab-name> --description <one-line> --type user|feedback|project|reference --source <provenance> [--sensitivity sensitive]`.
   - Never pass credentials, tokens, private keys, or secret values. The CLI rejects known patterns, but the write-time rule is authoritative.
3. Reverify or change an existing belief
   - When current evidence confirms an active fact unchanged, run `memoryctl reverify <name>`. This refreshes only `last_verified`; never use it when the fact's meaning changed.
   - Treat the candidate as external evidence. For a contradictory `project`/`reference` fact or other high-stakes change, obtain one different-family critic before writing.
   - Use `memoryctl supersede <old> <new> --resolution-rule last-writer-wins|evidence-merge|await-confirm|per-rule` plus the same metadata flags as `add`. Never overwrite or delete the losing fact.
   - To merge two or more active facts without deleting history, use `memoryctl consolidate <old> <old>... --into <new> --resolution-rule evidence-merge` plus explicit `--type`, `--source`, and `--sensitivity`. Inspect the JSON receipt, which names the exact changed paths and hashes; never hand-edit multi-source backlinks.
4. Migrate/link Claude memory
   - `memoryctl import-claude` is dry-run. Review its add/unchanged/conflict list before `--apply`.
   - `memoryctl bridge-claude` is dry-run. `--apply` first imports, verifies byte identity, backs up the legacy directory, then creates the compatibility symlink.
5. Verify
   - Run `mem-audit --json` and act only on its flags. Do not re-read the whole vault to recompute staleness.

Shared memory is versioned with the private project repository for cross-machine transport. Do not start a memory server or invent a second sync channel.
