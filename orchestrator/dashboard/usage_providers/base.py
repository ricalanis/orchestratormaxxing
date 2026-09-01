"""Unified usage-provider interface.

One shape for every usage source (Ollama Cloud, Claude Max, future
OpenRouter…) so the aggregator is a thin dispatcher and a broken source
degrades to an "unavailable" card instead of breaking the whole Usage tab.

Providers WRAP the existing usage.py logic — they don't re-implement it —
so this layer is a clean interface, not a rewrite.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional, Protocol, runtime_checkable


@dataclass
class UsageSnapshot:
    """A provider's current usage. session_pct / weekly_pct are the two
    billing windows (0–100, or None when the provider can't supply one).
    `source` says how the number was obtained (api | live | scraped | manual |
    estimate | transcript); `detail` carries provider-specific extras."""
    provider: str
    healthy: bool
    session_pct: Optional[float] = None
    weekly_pct: Optional[float] = None
    tier: Optional[str] = None
    source: Optional[str] = None
    reason: Optional[str] = None
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class UsageEvent:
    """One historical usage record (from a local log)."""
    ts: int
    provider: str
    model: str
    total_tokens: int

    def to_dict(self) -> dict:
        return asdict(self)


@runtime_checkable
class UsageProvider(Protocol):
    name: str

    def get_session_usage(self) -> Optional[UsageSnapshot]:
        """Current billing-window (session) usage; None if unavailable."""
        ...

    def get_period_usage(self) -> Optional[UsageSnapshot]:
        """Weekly/period usage; None if unavailable."""
        ...

    def get_history(self, limit: int = 100) -> list[UsageEvent]:
        """Recent historical usage events from local logs (newest first)."""
        ...

    def is_healthy(self) -> bool:
        """Can this provider supply data right now?"""
        ...
