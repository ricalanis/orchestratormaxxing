"""Regression guard for the cycle detail view's per-project status derivation
(sprints._cycle_project_status → on-track / at-risk / blocked).

Pure function, no DB — imports only dashboard.sprints (no side effects). Stdlib
unittest, pytest-discoverable. Run: python -m unittest tests.test_cycle_project_status
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dashboard.sprints import _cycle_project_status as status  # noqa: E402


class CycleProjectStatus(unittest.TestCase):
    def test_blocked_beats_everything(self):
        # A blocked task → blocked even at 100% done / 0% elapsed.
        self.assertEqual(status(5, 5, 1, 0, 0.1), "blocked")

    def test_blocked_takes_priority_over_behind_pace(self):
        self.assertEqual(status(10, 0, 2, 0, 0.9), "blocked")

    def test_rejected_is_at_risk(self):
        self.assertEqual(status(5, 4, 0, 1, 0.1), "at-risk")

    def test_behind_pace_is_at_risk(self):
        # 10% done at 60% elapsed → >15pts behind the ideal → at-risk.
        self.assertEqual(status(10, 1, 0, 0, 0.6), "at-risk")

    def test_on_pace_is_on_track(self):
        self.assertEqual(status(10, 5, 0, 0, 0.5), "on-track")

    def test_slightly_behind_within_margin_is_on_track(self):
        # 40% done at 50% elapsed → only 10pts behind (< 15) → still on-track.
        self.assertEqual(status(10, 4, 0, 0, 0.5), "on-track")

    def test_just_started_never_at_risk_by_pace(self):
        self.assertEqual(status(10, 0, 0, 0, 0.0), "on-track")

    def test_complete_is_on_track(self):
        self.assertEqual(status(4, 4, 0, 0, 0.99), "on-track")


if __name__ == "__main__":
    unittest.main(verbosity=2)
