"""Usage-provider registry.

`PROVIDERS` is the ordered list the aggregator (usage.get_usage_summary)
iterates. Add a new source by appending its provider here — nothing else in
the dispatcher changes.
"""
from .base import UsageEvent, UsageProvider, UsageSnapshot
from .claude_max import ClaudeMaxProvider
from .ollama_cloud import OllamaCloudProvider

PROVIDERS: list[UsageProvider] = [
    ClaudeMaxProvider(),
    OllamaCloudProvider(),
]

__all__ = [
    "PROVIDERS",
    "UsageProvider",
    "UsageSnapshot",
    "UsageEvent",
    "ClaudeMaxProvider",
    "OllamaCloudProvider",
]
