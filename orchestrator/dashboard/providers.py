"""
Unified provider usage tracking — transversal across all LLM providers.

The core abstraction is the **ProviderAdapter**: each provider (Claude Max,
Ollama Cloud, future additions) implements a common interface that produces
a **normalized usage dict** with a stable schema. The registry collects them
and `get_unified_summary()` produces cross-provider roll-ups (combined tokens,
combined cost, per-provider comparison) alongside the existing per-provider
breakdowns.

Normalized provider usage dict shape:
    {
        "provider": "claude" | "ollama" | ...,
        "label": "Claude Max" | "Ollama Cloud" | ...,
        "available": bool,
        "subscription": {...},        # provider-specific plan info
        "limits": {
            "session": {"pct": float, "resets_at": str|None, "window": "5h rolling"},
            "weekly": {"pct": float, "resets_at": str|None, "window": "7 days"},
            "source": "live" | "estimate" | "manual" | "real",
            "tier": str,
            "plan": str,
        },
        "totals": {
            "input": int, "output": int, "cache_read": int, "cache_creation": int,
            # OR for providers that don't distinguish cache:
            "prompt_tokens": int, "completion_tokens": int, "total_tokens": int,
            "messages": int, "calls": int,
        },
        "grand_total_tokens": int,
        "today_tokens": int,
        "week_tokens": int,
        "session_tokens": int,
        "cost_est_usd": float | None,   # None when the provider has no $ equivalent
        "by_model": [...],
        "by_day": [...],
        "by_project": [...] | None,     # only when the provider tracks per-project
        "capacity": {...} | None,       # provider-specific capacity model
        "recent": [...] | None,
        "models": [...] | None,         # available model catalog
    }

The `get_unified_summary()` roll-up adds:
    {
        "providers": {provider_name: normalized_dict, ...},
        "combined": {
            "grand_total_tokens": int,
            "today_tokens": int,
            "week_tokens": int,
            "cost_est_usd": float,
            "by_provider": [{provider, label, tokens, pct, cost_est_usd}, ...],
        },
        "fetched_at": int,
    }

Backward compatibility: `get_usage_summary()` still returns {claude: ..., ollama: ..., fetched_at: ...}
so existing consumers (dashboard template, MCP tool) keep working unchanged.
"""
from __future__ import annotations

import time
from typing import Any, Callable, Optional

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, "ProviderAdapter"] = {}


def register_adapter(adapter: "ProviderAdapter") -> None:
    """Register a provider adapter by its .name."""
    _REGISTRY[adapter.name] = adapter


def get_adapter(name: str) -> Optional["ProviderAdapter"]:
    return _REGISTRY.get(name)


def list_adapters() -> list["ProviderAdapter"]:
    """All registered adapters in registration order."""
    return list(_REGISTRY.values())


# ---------------------------------------------------------------------------
# Adapter base class
# ---------------------------------------------------------------------------


class ProviderAdapter:
    """Base class for a usage-tracking provider adapter.

    Subclasses must set ``name`` and ``label`` and implement ``fetch()``.
    """

    name: str = ""
    label: str = ""

    def fetch(self) -> dict[str, Any]:
        """Return the normalized usage dict for this provider.

        Must include at minimum:
            provider, label, available, totals, grand_total_tokens,
            today_tokens, week_tokens
        """
        raise NotImplementedError

    def invalidate_cache(self) -> None:
        """Drop any memoized data so the next fetch re-reads source data."""
        pass


# ---------------------------------------------------------------------------
# Unified cross-provider summary
# ---------------------------------------------------------------------------


def get_unified_summary() -> dict[str, Any]:
    """Fetch all providers and produce a cross-provider roll-up.

    Returns:
        {
            "providers": {name: normalized_dict, ...},
            "combined": {
                "grand_total_tokens": int,
                "today_tokens": int,
                "week_tokens": int,
                "cost_est_usd": float,
                "by_provider": [{provider, label, tokens, pct, cost_est_usd}, ...],
            },
            "fetched_at": int,
        }
    """
    providers: dict[str, Any] = {}
    combined_total = 0
    combined_today = 0
    combined_week = 0
    combined_cost = 0.0
    by_provider: list[dict] = []

    for adapter in list_adapters():
        try:
            data = adapter.fetch()
        except Exception as exc:
            # A single provider failing should never break the whole summary.
            data = {
                "provider": adapter.name,
                "label": adapter.label,
                "available": False,
                "error": str(exc)[:200],
                "totals": {},
                "grand_total_tokens": 0,
                "today_tokens": 0,
                "week_tokens": 0,
                "cost_est_usd": 0.0,
                "by_model": [],
                "by_day": [],
            }
        providers[adapter.name] = data

        total = data.get("grand_total_tokens", 0) or 0
        today = data.get("today_tokens", 0) or 0
        week = data.get("week_tokens", 0) or 0
        cost = data.get("cost_est_usd", 0.0) or 0.0

        combined_total += total
        combined_today += today
        combined_week += week
        combined_cost += cost

        by_provider.append(
            {
                "provider": adapter.name,
                "label": adapter.label,
                "tokens": total,
                "pct": 0.0,  # filled in below
                "cost_est_usd": round(cost, 2),
                "available": data.get("available", False),
            }
        )

    # Fill in percentage shares
    if combined_total > 0:
        for entry in by_provider:
            entry["pct"] = round(entry["tokens"] / combined_total * 100, 1)

    return {
        "providers": providers,
        "combined": {
            "grand_total_tokens": combined_total,
            "today_tokens": combined_today,
            "week_tokens": combined_week,
            "cost_est_usd": round(combined_cost, 2),
            "by_provider": sorted(by_provider, key=lambda x: -x["tokens"]),
        },
        "fetched_at": int(time.time()),
    }