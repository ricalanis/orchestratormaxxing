"""Pure scoring helpers for worker-path-bench."""
import json
import math


def parse_envelope(text):
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None, False
    if not isinstance(value, dict) or "output" not in value:
        return None, False
    if "usage" in value and not isinstance(value["usage"], dict):
        return None, False
    if "events" in value and not isinstance(value["events"], list):
        return None, False
    for key, metric in value.get("usage", {}).items():
        if not isinstance(metric, (int, float)) or isinstance(metric, bool) or not math.isfinite(metric) or metric < 0:
            return None, False
    for event in value.get("events", []):
        if not isinstance(event, dict):
            return None, False
    return value, True


def score_contract(contract, output):
    kind = contract.get("type")
    expected = contract.get("expected")
    if kind == "exact":
        return output == expected
    if kind == "json_fields":
        if isinstance(output, str):
            try:
                output = json.loads(output)
            except json.JSONDecodeError:
                return False
        return isinstance(output, dict) and isinstance(expected, dict) and all(output.get(k) == v for k, v in expected.items())
    return False


def percentile(values, quantile):
    if not values:
        return 0.0
    values = sorted(values)
    if len(values) == 1:
        return float(values[0])
    rank = quantile * (len(values) - 1)
    lower = int(rank)
    upper = min(lower + 1, len(values) - 1)
    return float(values[lower] + (values[upper] - values[lower]) * (rank - lower))


def compute_summary(results):
    adapters = {}
    has_context = {}
    for row in results:
        stats = adapters.setdefault(row["adapter"], {
            "total": 0, "passed": 0, "contract_failures": 0,
            "infrastructure_failures": 0, "schema_valid": 0, "_latencies": [],
        })
        ctx = has_context.setdefault(row["adapter"], {
            "protected_fact_losses": 0, "compaction_count": 0,
            "input_tokens_proxy_total": 0, "input_tokens_actual_total": 0,
            "cache_read_tokens_total": 0,
        })
        stats["total"] += 1
        stats["passed"] += int(row["contract_pass"])
        stats["infrastructure_failures"] += int(row["infrastructure_failure"])
        stats["contract_failures"] += int(not row["contract_pass"] and not row["infrastructure_failure"])
        stats["schema_valid"] += int(row["schema_valid"])
        stats["_latencies"].append(row["latency_ms"])
        if row.get("protected_fact"):
            lost = row.get("protected_fact_pass") is False
            if lost or not row.get("contract_pass"):
                ctx["protected_fact_losses"] += 1
        ctx["compaction_count"] += int(row.get("compaction_count") or 0)
        ctx["input_tokens_proxy_total"] += int(row.get("input_tokens_proxy") or 0)
        ctx["input_tokens_actual_total"] += int(row.get("input_tokens_actual") or 0)
        ctx["cache_read_tokens_total"] += int(row.get("cache_read_tokens") or 0)
    for stats in adapters.values():
        latencies = stats.pop("_latencies")
        stats["latency_ms_p50"] = round(percentile(latencies, 0.50), 3)
        stats["latency_ms_p95"] = round(percentile(latencies, 0.95), 3)
    for adapter, stats in adapters.items():
        ctx = has_context[adapter]
        if any(ctx.values()):
            stats.update(ctx)
    # A missing measurement is unknown, not measured zero. Include coverage
    # counts so partial totals cannot be mistaken for complete observations.
    for adapter, stats in adapters.items():
        rows = [row for row in results if row["adapter"] == adapter]
        for field in ("input_tokens_proxy", "input_tokens_actual", "cache_read_tokens"):
            observed = [row[field] for row in rows if row.get(field) is not None]
            stats[field + "_observed_cases"] = len(observed)
            stats[field + "_total"] = sum(observed) if observed else None
        observed = [row["compaction_count"] for row in rows if row.get("compaction_count") is not None]
        stats["compaction_observed_cases"] = len(observed)
        stats["compaction_count"] = sum(observed) if observed else None
    return {"adapters": adapters}
