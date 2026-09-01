"""Ollama Cloud usage provider — wraps usage.get_ollama_usage().

Primary source is the oll-usage.jsonl log (Ollama Cloud has no usage API);
the CDP scraper enriches with a real % when available. All of that logic
lives in usage.py — this is just the interface adapter.
"""
from __future__ import annotations

from typing import Optional

from .base import UsageEvent, UsageSnapshot


class OllamaCloudProvider:
    name = "ollama_cloud"

    def _raw(self) -> dict:
        from .. import usage
        return usage.get_ollama_usage()

    def _snapshot(self) -> UsageSnapshot:
        r = self._raw()
        cap = r.get("capacity", {}) or {}
        health = r.get("health", {}) or {}
        return UsageSnapshot(
            provider=self.name,
            healthy=bool(health.get("healthy")),
            session_pct=(cap.get("session") or {}).get("pct"),
            weekly_pct=(cap.get("weekly") or {}).get("pct"),
            tier=cap.get("tier"),
            source=health.get("source") or cap.get("source"),
            reason=health.get("reason"),
            detail={
                "key_present": r.get("key_present"),
                "totals": r.get("totals"),
                "scrape_stale": cap.get("scrape_stale"),
                "settings_url": cap.get("settings_url"),
                "log_present": r.get("log_present"),
            },
        )

    # Both windows come from the one underlying (TTL-cached) fetch.
    def get_session_usage(self) -> Optional[UsageSnapshot]:
        return self._snapshot()

    def get_period_usage(self) -> Optional[UsageSnapshot]:
        return self._snapshot()

    def get_history(self, limit: int = 100) -> list[UsageEvent]:
        r = self._raw()
        out = []
        for e in (r.get("recent") or [])[:limit]:
            out.append(UsageEvent(
                ts=int(e.get("ts") or 0),
                provider=self.name,
                model=e.get("model") or "unknown",
                total_tokens=int(e.get("total_tokens") or 0),
            ))
        return out

    def is_healthy(self) -> bool:
        return bool(self._raw().get("health", {}).get("healthy"))
