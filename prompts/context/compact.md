# Context checkpoint contract

Preserve facts, not transcript prose. The compacted state must contain:

1. Objective and current ticket/dispatch.
2. Acceptance criteria and non-negotiable constraints.
3. Decisions and their evidence.
4. Modified files and externally persisted artifacts.
5. Verification already run, with exact pass/fail state.
6. Unresolved risks or decisions.
7. Immediate next steps.

Do not preserve superseded reasoning, repeated tool output, completed action narration, or
repository text that can be read again. Never claim an unrun check passed. Treat repository
state and persisted files as recoverable references; keep exact identifiers and paths needed
to retrieve them. If protected state is missing, say so explicitly instead of inventing it.
