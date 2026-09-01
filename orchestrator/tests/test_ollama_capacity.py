"""Regression tests for resilient Ollama Cloud capacity scraping."""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from dashboard import scrape_ollama_usage as scraper
from dashboard import usage


class OllamaScrapeStore(unittest.TestCase):
    def test_failed_attempt_preserves_last_good_reading(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "ollama-usage.json"
            good = {
                "ok": True,
                "scraped_at": int(time.time()) - 60,
                "session_pct": 2.5,
                "weekly_pct": 1.2,
                "tier": "max",
            }
            with mock.patch.object(scraper, "STORE", store):
                scraper._write(good)
                scraper._write({
                    "ok": False,
                    "reason": "page still loading",
                    "scraped_at": int(time.time()),
                })

            saved = json.loads(store.read_text())
            self.assertFalse(saved["ok"])
            self.assertEqual(saved["last_good"]["tier"], "max")
            self.assertEqual(saved["last_good"]["weekly_pct"], 1.2)

            with mock.patch.object(usage, "OLLAMA_SCRAPE_FILE", store):
                reading = usage.get_ollama_scraped()
                meta = usage.get_ollama_scrape_meta()

            self.assertEqual(reading["tier"], "max")
            self.assertEqual(reading["session_pct"], 2.5)
            self.assertEqual(reading["refresh_error"], "page still loading")
            self.assertFalse(meta["ok"])
            self.assertTrue(meta["using_last_good"])


class OllamaCapacityFallback(unittest.TestCase):
    def test_missing_real_scrape_never_fabricates_free_100_percent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = {
                key: value for key, value in os.environ.items()
                if not key.startswith("HERMES_OLLAMA_")
            }
            with (
                mock.patch.dict(os.environ, env, clear=True),
                mock.patch.object(usage, "OLLAMA_SCRAPE_FILE", root / "missing.json"),
                mock.patch.object(usage, "OLL_USAGE_LOG", root / "missing.jsonl"),
                mock.patch.object(usage, "HERMES_STATE_DB", root / "missing.db"),
                mock.patch.object(usage, "get_ollama_models", return_value=[]),
            ):
                usage.invalidate_ollama_cache()
                result = usage.get_ollama_usage()

            capacity = result["capacity"]
            self.assertEqual(capacity["source"], "unavailable")
            self.assertIsNone(capacity["tier"])
            self.assertIsNone(capacity["session"]["pct"])
            self.assertIsNone(capacity["weekly"]["pct"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
