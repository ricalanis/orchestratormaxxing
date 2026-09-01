"""Claude Max usage provider — wraps usage.get_claude_usage().

Real session/weekly % come from Claude Code's own OAuth usage endpoint
(limits.source == 'live'); when that token is missing/expired the underlying
layer falls back to transcript-scan estimates. Logic lives in usage.py.
"""
from __future__ import annotations

from typing import Optional

from .base import UsageEvent, UsageSnapshot


class ClaudeMaxProvider:
    name = "claude_max"

    def _raw(self) -> dict:
        from .. import usage
        return usage.get_claude_usage()

    def _snapshot(self) -> UsageSnapshot:
        r = self._raw()
        limits = r.get("limits", {}) or {}
        sub = r.get("subscription", {}) or {}
        live = limits.get("source") == "live"
        healthy = bool(r.get("available"))
        if limits.get("live_unavailable"):
            source, reason = "transcript", "OAuth usage API unavailable — estimate from transcripts"
        elif live:
            source, reason = "live", "real % from Claude Code's usage endpoint"
        else:
            source, reason = "estimate", "estimated % vs a per-tier ceiling"
        return UsageSnapshot(
            provider=self.name,
            healthy=healthy,
            session_pct=(limits.get("session") or {}).get("pct"),
            weekly_pct=(limits.get("weekly") or {}).get("pct"),
            tier=limits.get("tier") or sub.get("organization_type"),
            source=source,
            reason=reason,
            detail={
                "plan": limits.get("plan") or sub.get("organization_type"),
                "session_resets_at": (limits.get("session") or {}).get("resets_at"),
                "weekly_resets_at": (limits.get("weekly") or {}).get("resets_at"),
                "week_tokens": r.get("week_tokens"),
                "session_tokens": r.get("session_tokens"),
                "cost_est_usd": r.get("cost_est_usd"),
            },
        )

    def get_session_usage(self) -> Optional[UsageSnapshot]:
        return self._snapshot()

    def get_period_usage(self) -> Optional[UsageSnapshot]:
        return self._snapshot()

    def get_history(self, limit: int = 100) -> list[UsageEvent]:
        # Claude usage is aggregated per-model/day (no per-call log); expose the
        # daily roll-up as coarse history events, newest first.
        r = self._raw()
        out = []
        for d in reversed((r.get("by_day") or [])[-limit:]):
            out.append(UsageEvent(
                ts=0,  # by_day carries a `day` date string, not an epoch
                provider=self.name,
                model=d.get("day") or d.get("label") or "",
                total_tokens=int(d.get("tokens") or 0),
            ))
        return out

    def is_healthy(self) -> bool:
        return bool(self._raw().get("available"))
