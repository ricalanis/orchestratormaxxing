"""
Tests for the unified provider usage tracking API.

Tests the provider adapter pattern, the registry, and the cross-provider
roll-up (combined tokens, cost, per-provider % share) — without needing
real Claude transcripts or the Ollama usage log.
"""
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Make the dashboard package importable.
sys.path.insert(0, str(Path(__file__).parent.parent))

from dashboard import providers
# Importing usage.py triggers adapter registration (ClaudeAdapter, OllamaAdapter).
from dashboard import usage  # noqa: F401


# ---------------------------------------------------------------------------
# Test adapters
# ---------------------------------------------------------------------------


class FakeProviderA(providers.ProviderAdapter):
    name = "fake_a"
    label = "Fake Provider A"

    def fetch(self):
        return {
            "provider": "claude",
            "label": "Fake Provider A",
            "available": True,
            "totals": {"input": 100, "output": 200, "cache_read": 50, "cache_creation": 0, "messages": 5},
            "grand_total_tokens": 350,
            "today_tokens": 100,
            "week_tokens": 300,
            "cost_est_usd": 1.50,
            "by_model": [],
            "by_day": [],
        }


class FakeProviderB(providers.ProviderAdapter):
    name = "fake_b"
    label = "Fake Provider B"

    def fetch(self):
        return {
            "provider": "ollama",
            "label": "Fake Provider B",
            "available": True,
            "totals": {"prompt_tokens": 500, "completion_tokens": 500, "total_tokens": 1000, "calls": 10},
            "grand_total_tokens": 1000,
            "today_tokens": 200,
            "week_tokens": 800,
            "cost_est_usd": 0.0,
            "by_model": [],
            "by_day": [],
        }


class FailingProvider(providers.ProviderAdapter):
    name = "failing"
    label = "Failing Provider"

    def fetch(self):
        raise RuntimeError("API unreachable")


# ---------------------------------------------------------------------------
# Registry tests
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_register_and_retrieve(self):
        adapter = FakeProviderA()
        providers.register_adapter(adapter)
        assert providers.get_adapter("fake_a") is adapter

    def test_list_adapters_includes_registered(self):
        adapter = FakeProviderA()
        providers.register_adapter(adapter)
        names = [a.name for a in providers.list_adapters()]
        assert "fake_a" in names

    def test_get_adapter_returns_none_for_unknown(self):
        assert providers.get_adapter("nonexistent") is None


# ---------------------------------------------------------------------------
# Unified summary tests
# ---------------------------------------------------------------------------


class TestUnifiedSummary:
    def test_combined_totals(self):
        """The roll-up sums tokens + cost across all providers."""
        # Save and restore the registry so we don't pollute other tests.
        original = dict(providers._REGISTRY)
        try:
            providers._REGISTRY.clear()
            providers.register_adapter(FakeProviderA())
            providers.register_adapter(FakeProviderB())

            summary = providers.get_unified_summary()

            assert "combined" in summary
            combined = summary["combined"]
            assert combined["grand_total_tokens"] == 350 + 1000
            assert combined["today_tokens"] == 100 + 200
            assert combined["week_tokens"] == 300 + 800
            assert combined["cost_est_usd"] == 1.50
            assert "fetched_at" in summary
        finally:
            providers._REGISTRY.clear()
            providers._REGISTRY.update(original)

    def test_by_provider_pct_share(self):
        """Each provider gets a % share proportional to its tokens."""
        original = dict(providers._REGISTRY)
        try:
            providers._REGISTRY.clear()
            providers.register_adapter(FakeProviderA())
            providers.register_adapter(FakeProviderB())

            summary = providers.get_unified_summary()
            by_provider = summary["combined"]["by_provider"]

            # FakeProviderA=350, FakeProviderB=1000, total=1350
            assert len(by_provider) == 2
            pct_a = next(p for p in by_provider if p["provider"] == "fake_a")["pct"]
            pct_b = next(p for p in by_provider if p["provider"] == "fake_b")["pct"]
            assert round(pct_a, 1) == round(350 / 1350 * 100, 1)
            assert round(pct_b, 1) == round(1000 / 1350 * 100, 1)
            # Sorted by tokens descending
            assert by_provider[0]["tokens"] >= by_provider[1]["tokens"]
        finally:
            providers._REGISTRY.clear()
            providers._REGISTRY.update(original)

    def test_failing_provider_does_not_break_summary(self):
        """A provider that raises should produce an error entry, not crash."""
        original = dict(providers._REGISTRY)
        try:
            providers._REGISTRY.clear()
            providers.register_adapter(FakeProviderA())
            providers.register_adapter(FailingProvider())

            summary = providers.get_unified_summary()

            # The working provider still contributes.
            assert summary["combined"]["grand_total_tokens"] == 350
            # The failing provider shows up as unavailable with an error.
            failing_data = summary["providers"]["failing"]
            assert failing_data["available"] is False
            assert "API unreachable" in failing_data.get("error", "")
        finally:
            providers._REGISTRY.clear()
            providers._REGISTRY.update(original)

    def test_providers_dict_contains_all_adapters(self):
        """The .providers key has an entry per registered adapter."""
        original = dict(providers._REGISTRY)
        try:
            providers._REGISTRY.clear()
            providers.register_adapter(FakeProviderA())
            providers.register_adapter(FakeProviderB())

            summary = providers.get_unified_summary()
            assert set(summary["providers"].keys()) == {"fake_a", "fake_b"}
        finally:
            providers._REGISTRY.clear()
            providers._REGISTRY.update(original)


# ---------------------------------------------------------------------------
# Real adapter wiring tests (Claude + Ollama registered on import)
# ---------------------------------------------------------------------------


class TestRealAdapters:
    def test_claude_adapter_registered(self):
        adapter = providers.get_adapter("claude")
        assert adapter is not None
        assert adapter.label == "Claude Max"

    def test_ollama_adapter_registered(self):
        adapter = providers.get_adapter("ollama")
        assert adapter is not None
        assert adapter.label == "Ollama Cloud"

    def test_unified_summary_includes_real_providers(self):
        """The summary should include at least claude and ollama."""
        summary = providers.get_unified_summary()
        assert "claude" in summary["providers"]
        assert "ollama" in summary["providers"]
        assert "combined" in summary
        assert summary["combined"]["grand_total_tokens"] >= 0