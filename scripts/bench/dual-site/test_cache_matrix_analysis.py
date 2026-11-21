"""Offline checks for the published result aggregation and source inventory."""

from pathlib import Path
import unittest
from analyze_cache_matrix import aggregate, panel_cycles, source_check


class AnalysisTests(unittest.TestCase):
    def test_pair_aggregation_preserves_denominators_and_percent_changes(self):
        def pair(off, on, hits):
            row = {
                "off_p95_ms": off,
                "on_p95_ms": on,
                "diff_percent": 100 * (on / off - 1),
                "diff_ms": on - off,
                "hits": hits,
                "n_off": 8,
                "n_on": 7,
            }
            probes = {
                p: {s: {"fresh_by_ms": 1100, "old_until_ms": 850} for s in ("a", "b")}
                for p in ("new_alert", "key_revocation")
            }
            return {
                "rows": {f"{s}-{k}": row for s in ("a", "b") for k in ("dashboard", "write")},
                "freshness": {"one-second": probes},
                "total_off": 20,
                "total_on": 18,
                "origin_requests_off": 20,
                "origin_requests_on": 18 - hits,
                "backend_requests_avoided": hits,
            }

        result = aggregate([pair(20, 10, 3), pair(100, 90, 2)])
        self.assertEqual(result["completed_on"], 36)
        self.assertEqual(result["origin_on"], 31)
        group = result["groups"]["b-dashboard"]
        self.assertEqual((group["faster_pairs"], group["n_on"], group["n_off"]), (2, 14, 16))
        self.assertEqual(group["paired_pct"]["median"], -30)

    def test_whole_wall_includes_scheduled_phase(self):
        rows = [
            {
                "site": "b",
                "panel": panel,
                "kind": "dashboard",
                "due_s": 5 + panel / 2,
                "finished_s": 5 + panel / 2 + 0.1,
            }
            for panel in range(8)
        ]
        self.assertEqual(panel_cycles({"arrivals": rows})["b"]["cycles_ms"], [3600])
        with self.assertRaisesRegex(ValueError, "eight distinct"):
            panel_cycles({"arrivals": rows[:-1]})

    def test_fetch_diagnostics_retain_categories_only(self):
        from run_cache_replay import fetch_error_categories

        raw = (
            "  123 FetchError b http first read error: EOF\n  456 FetchError b unknown detail: sample-value\n"
        )
        self.assertEqual(fetch_error_categories(raw), {"eof": 1, "other": 1})
        self.assertEqual(fetch_error_categories("header which is not a diagnostic"), {})

    def test_empty_manifest_cannot_verify(self):
        with self.assertRaisesRegex(ValueError, "omits required"):
            source_check(Path("."), "HEAD", {})
