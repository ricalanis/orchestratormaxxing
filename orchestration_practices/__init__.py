"""Pure, advisory Orchestra-of-One practice catalog and evaluator."""
from __future__ import annotations

import copy
import json
import re
import unicodedata
from pathlib import Path

class CatalogError(ValueError):
    pass

_catalog = None
_CHECKS = frozenset({"dependency.healthy", "progress.advancing",
                     "progress.not-spinning", "completion.objective",
                     "graph.single-writer", "contract.present", "brakes.exact-four",
                     "checkpoint.present", "evidence.adequate"})
_RESCUES = frozenset({"rescue.dependency-unhealthy", "rescue.no-progress",
                      "rescue.false-green", "rescue.multiple-writers",
                      "rescue.missing-contract", "rescue.missing-brakes",
                      "rescue.iteration-budget", "rescue.deadline-exceeded",
                      "rescue.envelope-blocked"})
_HOSTS = frozenset({"hermes", "orchestrator", "claude", "codex", "opencode", "open_design"})

def normalize_text(text):
    return unicodedata.normalize("NFKC", str(text)).casefold()

def allowlisted_check_ids():
    return set(_CHECKS)

def allowlisted_rescue_policy_ids():
    return set(_RESCUES)

def validate_catalog(catalog):
    if not isinstance(catalog, dict) or catalog.get("schema_version") != 1:
        raise CatalogError("schema_version")
    practices = catalog.get("practices")
    if not isinstance(practices, list) or len(practices) != 20:
        raise CatalogError("practices")
    ids = set()
    expression_owners = {}
    for p in practices:
        required = ("practice_id", "level", "expressions", "hosts", "required_brakes",
                    "preflight_ids", "rescue_policy_ids", "evidence_refs")
        if not all(k in p for k in required) or p["practice_id"] in ids:
            raise CatalogError("practice schema")
        ids.add(p["practice_id"])
        if p["level"] not in {"prompt", "context", "harness", "loop", "graph"}:
            raise CatalogError("level")
        expressions = p["expressions"]
        if (not isinstance(expressions, list) or not 1 <= len(expressions) <= 8 or
                set(p["hosts"]) != _HOSTS):
            raise CatalogError("expressions/hosts")
        for expression in expressions:
            if (not isinstance(expression, str) or not expression.strip() or
                    len(expression) > 120):
                raise CatalogError("expression")
            normalized = normalize_text(expression)
            owner = expression_owners.get(normalized)
            if owner is not None:
                raise CatalogError("expression collision")
            expression_owners[normalized] = p["practice_id"]
        if not set(p["required_brakes"]) <= {"max_iterations", "budget_or_deadline", "no_progress", "completion_check"}:
            raise CatalogError("brakes")
        if not set(p["preflight_ids"]) <= _CHECKS or not set(p["rescue_policy_ids"]) <= _RESCUES:
            raise CatalogError("allowlist")
        if not p["evidence_refs"]:
            raise CatalogError("evidence")
    return True

def load_catalog():
    global _catalog
    if _catalog is None:
        with (Path(__file__).with_name("catalog.json")).open(encoding="utf-8") as f:
            value = json.load(f)
        # Keep the shipped catalog compact while exposing grounded, typed provenance.
        # Every path names a real source; the matcher never reads it at runtime.
        book_refs = {
            "prompt.contract-first": "book:first-movement:chapter-3",
            "prompt.external-critic": "book:first-movement:chapter-3",
            "prompt.checklist-grade": "book:first-movement:chapter-3",
            "context.minimal-frontier": "book:first-movement:chapter-4",
            "context.checkpoint-not-memory": "book:second-movement:chapter-15",
            "context.fresh-window": "book:first-movement:chapter-4",
            "context.governed-memory": "book:first-movement:chapter-6",
            "harness.etclovg-attribution": "book:first-movement:chapter-2",
            "harness.deterministic-verifier": "book:first-movement:chapter-3",
            "harness.untrusted-tools": "book:first-movement:chapter-5",
            "harness.data-contracts": "book:first-movement:chapter-8",
            "loop.four-brakes": "book:second-movement:chapter-15",
            "loop.event-driven": "book:first-movement:chapter-10",
            "loop.warranted-ratchet": "book:second-movement:chapter-17",
            "loop.repeat-safe-writes": "book:second-movement:chapter-15",
            "loop.operator-ledger": "book:second-movement:chapter-18",
            "graph.node-collapse": "book:second-movement:chapter-16",
            "graph.typed-state-single-writer": "book:second-movement:chapter-16",
            "graph.code-first-routing": "book:second-movement:chapter-16",
            "graph.correlated-agreement": "book:second-movement:chapter-16",
        }
        repo_refs = {
            "prompt.contract-first": "repo:CLAUDE.md",
            "prompt.external-critic": "repo:knowledge/delegation-science-2026-08-12.md",
            "prompt.checklist-grade": "repo:knowledge/gauntlet-loop-design.md",
            "context.minimal-frontier": "repo:knowledge/harness-corpus-worker-team-mode-audit.md",
            "context.checkpoint-not-memory": "repo:knowledge/loop-graph-mastery-program-2026-08-10.md",
            "context.fresh-window": "repo:knowledge/context-lifecycle.md",
            "context.governed-memory": "repo:knowledge/memory-protocol-upgrade.md",
            "harness.etclovg-attribution": "repo:knowledge/etclovg-harness-audit.md",
            "harness.deterministic-verifier": "repo:knowledge/signal-vs-artifact-2026-07-19.md",
            "harness.untrusted-tools": "repo:knowledge/open-design-integration-2026-08-05.md",
            "harness.data-contracts": "repo:knowledge/plugin-first-integration-gate-2026-07-19.md",
            "loop.four-brakes": "repo:knowledge/loop-engineering-2026-06-10.md",
            "loop.event-driven": "repo:knowledge/loop-engineering-2026-06-10.md",
            "loop.warranted-ratchet": "repo:knowledge/self-improve-log.md",
            "loop.repeat-safe-writes": "repo:CLAUDE.md",
            "loop.operator-ledger": "repo:knowledge/loop-graph-mastery-program-2026-08-10.md",
            "graph.node-collapse": "repo:knowledge/harness-corpus-worker-team-mode-audit.md",
            "graph.typed-state-single-writer": "repo:knowledge/loop-graph-mastery-program-2026-08-10.md",
            "graph.code-first-routing": "repo:knowledge/provider-routing.md",
            "graph.correlated-agreement": "repo:knowledge/delegation-science-2026-08-12.md",
        }
        for practice in value.get("practices", []):
            pid = practice["practice_id"]
            practice["evidence_refs"] = [book_refs[pid], repo_refs[pid]]
        validate_catalog(value)
        _catalog = value
    return copy.deepcopy(_catalog)

def match_practices(text, host, catalog=None):
    if host not in _HOSTS:
        return {"status": "abstain", "reason": "unsupported_host", "matches": []}
    cat = catalog if catalog is not None else load_catalog()
    norm = normalize_text(text)
    matches = []
    for p in cat["practices"]:
        if host in p["hosts"] and any(
                re.search(r"(?<!\w)" + re.escape(normalize_text(expr)) + r"(?!\w)", norm)
                for expr in p["expressions"]):
            matches.append({"practice_id": p["practice_id"], "level": p["level"]})
    return {"status": "matched", "matches": matches} if matches else {"status": "abstain", "reason": "no_match", "matches": []}

def _action_results_not_spinning(ctx):
    if "action_results" not in ctx:
        return True, "action_results:omitted"
    trace = ctx["action_results"]
    if not isinstance(trace, list) or len(trace) > 100:
        return False, "action_results:malformed"
    pairs = []
    for entry in trace:
        if not isinstance(entry, dict) or "action" not in entry or "result" not in entry:
            return False, "action_results:malformed"
        try:
            pair = json.dumps(
                [entry["action"], entry["result"]], ensure_ascii=False,
                sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError):
            return False, "action_results:malformed"
        if len(pair.encode("utf-8")) > 4096:
            return False, "action_results:malformed"
        pairs.append(pair)
    if len(pairs) >= 3 and len(set(pairs[-3:])) == 1:
        return False, "action_results:third-identical-pair"
    return True, "action_results:clear"

def evaluate(text, host, context, catalog=None):
    matched = match_practices(text, host, catalog)
    base = {"status": matched["status"], "reason": matched.get("reason"), "checks": [],
            "rescue_policy_ids": [], "receipt": None}
    if matched["status"] != "matched":
        return base
    ctx = context if isinstance(context, dict) else {}
    def check(cid, passed, detail):
        base["checks"].append({"check_id": cid, "passed": bool(passed), "detail": detail})
    deps = ctx.get("dependencies", {})
    check("dependency.healthy", bool(deps) and all(bool(v) for v in deps.values()), "dependencies")
    progress = ctx.get("progress", [])
    check("progress.advancing", len(progress) >= 2 and any(a < b for a, b in zip(progress, progress[1:])), "progress")
    not_spinning, spin_detail = _action_results_not_spinning(ctx)
    check("progress.not-spinning", not_spinning, spin_detail)
    completion = ctx.get("completion", {})
    check("completion.objective", bool(completion) and not bool(completion.get("self_report")) and completion.get("type") != "self_report", "completion")
    writers = ctx.get("writers", {})
    check("graph.single-writer", all(isinstance(v, list) and len(v) == 1 for v in writers.values()), "writers")
    check("contract.present", bool(ctx.get("contract")), "contract")
    brakes = ctx.get("brakes", {})
    exact = set(brakes) == {"max_iterations", "budget_or_deadline", "no_progress", "completion_check"}
    check("brakes.exact-four", exact, "brakes")
    check("checkpoint.present", isinstance(ctx.get("checkpoint"), dict) and bool(ctx["checkpoint"].get("state_ref")), "checkpoint")
    check("evidence.adequate", bool(ctx.get("evidence_refs")), "evidence")
    failed = {c["check_id"] for c in base["checks"] if not c["passed"]}
    mapping = {"dependency.healthy":"rescue.dependency-unhealthy", "progress.advancing":"rescue.no-progress", "progress.not-spinning":"rescue.no-progress", "completion.objective":"rescue.false-green", "graph.single-writer":"rescue.multiple-writers", "contract.present":"rescue.missing-contract", "brakes.exact-four":"rescue.missing-brakes"}
    base["rescue_policy_ids"] = [mapping[x] for x in mapping if x in failed]
    base["status"] = "blocked" if failed else "ready"
    base["reason"] = None
    matched_ids = [x["practice_id"] for x in matched["matches"]]
    refs = []
    cat = catalog if catalog is not None else load_catalog()
    by_id = {p["practice_id"]: p for p in cat["practices"]}
    for pid in matched_ids:
        refs.extend(by_id[pid].get("evidence_refs", []))
    refs.extend(ctx.get("evidence_refs", []))
    refs = list(dict.fromkeys(str(x) for x in refs))
    base["receipt"] = {"brakes": sorted(brakes), "practice_ids": matched_ids,
                        "checks": list(base["checks"]),
                        "evidence_refs": refs,
                        "authority": {"may_accept": False, "may_write": False, "may_retry": False}}
    return base
