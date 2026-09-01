# Orchestrator — operator entry points.

.PHONY: help deploy-check test e2e gate

help:
	@echo "make deploy-check   post-deploy gates (health + contract + container security)"
	@echo "                    against a deployed instance. Config: ORCH_URL / MCP_URL"
	@echo "                    (ORCH_URL defaults to \$$COOLIFY_URL). Exit != 0 on any failure."
	@echo "make test           run the local unit suite (unittest)"
	@echo "make e2e            run the Playwright board + drawer specs (dashboard regression gate)"
	@echo "make gate           full pre-refactor gate: unit tests + e2e specs"

# Post-deploy verification against the DEPLOYED public URLs — one command.
# Credential-free; exits non-zero if any gate fails (so CI/operators fail loud).
deploy-check:
	@deploy/coolify/deploy-check.sh

test:
	@.venv/bin/python -m unittest discover -s tests

# The dashboard refactor gate (P0-12): board (cycle) + drawer (board/crm/roadmap/
# sessions/core) specs against a wiped-DB copy. Playwright auto-starts the server.
e2e:
	@npx playwright test

# Run both before/after a dashboard refactor — the "refactor guarded by tests" gate.
gate: test e2e
