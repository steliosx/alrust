import copy
from collections import Counter
import unittest
from cache_replay_model import BASE_PATH, PROFILES, SITES, TENANTS, check_view, expected_views, plan
from run_cache_replay import validate_timed


class ReplayTests(unittest.TestCase):
    def test_same_offered_traffic_in_both_arms(self):
        for profile in ("wallboard", "mixed", "personal", "unique"):
            events = plan(profile, 8, 20, "spread", 3)
            self.assertEqual(events, plan(profile, 8, 20, "spread", 3))
            self.assertEqual([r["sequence"] for r in events], list(range(160)))
            self.assertEqual([r["due_s"] for r in events], [n / 8 for n in range(160)])
            self.assertEqual(
                Counter((r["site"], r["kind"]) for r in events),
                {(s, k): n for s in SITES for k, n in (("dashboard", 64), ("write", 16))},
            )
            self.assertTrue(all(r["index"] % 4 == r["key"] - 8 for r in events if r["kind"] == "write"))

    def test_different_reuse_with_same_volume(self):
        def reuse(profile):
            rows = [r for r in plan(profile, 5, 20, "aligned") if r["kind"] == "dashboard"]
            return len({(r["site"], r["due_s"], r["key"], r["path"]) for r in rows}), len(rows)

        self.assertEqual(reuse("wallboard"), (20, 80))
        self.assertEqual(reuse("mixed"), (60, 80))
        self.assertEqual(reuse("unique"), (80, 80))
        self.assertEqual(reuse("personal"), (80, 80))

    def test_five_second_identities_and_cadence(self):
        for profile in ("personal-5s", "shared-5s"):
            for cadence in ("aligned", "spread"):
                trace = plan(profile, 4, 30, cadence, 2)
                self.assertEqual(len(trace), 120)
                self.assertEqual([r["due_s"] for r in trace], sorted(r["due_s"] for r in trace))
                panels = [r for r in trace if r["kind"] == "dashboard"]
                for panel in range(16):
                    rows = [r for r in panels if r["panel"] == panel]
                    self.assertEqual(len({(r["key"], r["site"], r["path"]) for r in rows}), 1)
                    self.assertEqual([b["due_s"] - a["due_s"] for a, b in zip(rows, rows[1:])], [5] * 5)
                keys = {r["key"] for r in panels}
                self.assertEqual(len(keys), 8 if profile == "personal-5s" else 4)

    def test_tenant_and_order(self):
        data = [[str(i), f"r{i}", "major", "open", tenant, 0, "x"] for i, tenant in enumerate(TENANTS)]
        expected = expected_views(data)[("blue", "major")]
        self.assertEqual(expected["total"], 1)
        body = {
            "status": "ok",
            "total": 1,
            "page": 1,
            "pages": 1,
            "pageSize": 100,
            "more": False,
            "severityCounts": {"major": 1},
            "statusCounts": {"open": 1},
            "alerts": [dict(zip(("id", "resource", "severity", "status", "customer"), data[0]))],
        }
        check_view(body, expected)
        for key in ("id", "customer", "status"):
            wrong = copy.deepcopy(body)
            wrong["alerts"][0][key] = "different"
            with self.assertRaises(ValueError):
                check_view(wrong, expected)

    def test_write_and_cache_ledgers(self):
        before = {
            "alerts": [["id", "resource", "major", "open", "blue", 3, "old"]],
            "uses": [3, 0],
            "timers": {"received": 3},
        }
        after = {
            "alerts": [["id", "resource", "major", "open", "blue", 4, "new"]],
            "uses": [3, 1],
            "timers": {"received": 4},
        }
        rows = [
            {"kind": "dashboard", "sequence": 0, "key": 0, "ok": True, "hit": True, "site": "a"},
            {
                "kind": "write",
                "sequence": 1,
                "key": 1,
                "ok": True,
                "hit": False,
                "site": "a",
                "alert_id": "id",
                "ordinal": 4,
                "receipt": "new",
            },
        ]
        good = {"a": {"backend_req": 4}}

        def validate(end, stat):
            validate_timed(before, end, end, rows, {"a": {"backend_req": 3}}, stat, rows)

        validate(after, good)
        with self.assertRaises(ValueError):
            validate(after, {"a": {"backend_req": 3}})
        for key, value in (("uses", [4, 1]), ("timers", {"received": 4, "queries": 1})):
            with self.assertRaises(ValueError):
                validate(dict(after, **{key: value}), good)
        bad = copy.deepcopy(after)
        bad["alerts"][0][-1] = "other"
        with self.assertRaises(ValueError):
            validate(bad, good)

    def test_newest_fresh_arrivals_and_cache_window(self):
        import cache_replay_mutations as m

        seed = [["a", "a", "major", "open", "blue", 1, "p"]]
        new = ["b", "b", "minor", "open", "blue", 0, "b"]
        final = [new, *seed]
        write = {"kind": "write", "started_s": 1.0, "finished_s": 1.2, "fresh": True, "write_row": new}

        def reader(seq, ts, snapshot, hit):
            return {
                "kind": "dashboard",
                "site": "a",
                "sequence": seq,
                "view": "all",
                "key": 0,
                "started_s": ts,
                "finished_s": ts + 0.05,
                "page_signature": m.signature(snapshot, "all"),
                "hit": hit,
            }

        rows = [write, reader(0, 0.1, seed, False), reader(1, 1.4, seed, True), reader(2, 2.8, final, False)]
        obs = m.verify(seed, final, rows, "one-second")
        self.assertTrue(all(o["valid"] for o in obs))
        self.assertEqual(obs[1]["known_lag_ms"], 200)
        wrong = dict(rows[2], hit=False)
        self.assertFalse(m.verify(seed, final, [write, wrong], "off")[0]["valid"])
        mixed = dict(
            rows[2],
            page_signature=[m.signature(seed, "all")[0], *m.signature(final, "all")[1:]],
            hit=False,
            started_s=1.05,
            finished_s=1.19,
        )
        torn = m.verify(seed, final, [write, mixed], "off")[0]
        self.assertEqual((torn["page_seen"], torn["counts_seen"], torn["valid"]), (True, True, False))

    def test_feedback_offers_follow_receipts_and_stop_at_horizon(self):
        from cache_replay_panels import effective_plan

        nominal = plan("shared-5s", 4, 20, "aligned", 0)
        rows = []
        latest = {}
        for event in nominal:
            if event["kind"] == "write":
                rows.append(dict(event, finished_s=event["due_s"] + 0.1))
                continue
            panel = event["panel"]
            due = latest[panel] + 5 if panel in latest else event["due_s"]
            if due < 20:
                latest[panel] = due + 2
                rows.append(dict(event, due_s=due, finished_s=latest[panel]))
        self.assertEqual(len(effective_plan(nominal, rows, 20)), 16 * 3 + 16)
        with self.assertRaises(ValueError):
            effective_plan(nominal, rows[1:], 20)
        broken = copy.deepcopy(rows)
        broken[-1]["sequence"] = broken[0]["sequence"]
        with self.assertRaises(ValueError):
            effective_plan(nominal, broken, 20)

    def test_environments_companion_is_scoped_and_passes(self):
        from cache_replay_journey import environments

        expected = {"total": 2, "sev": {"major": 2}, "status": {"open": 2}}
        body = {
            "status": "ok",
            "total": 1,
            "page": 1,
            "pages": 1,
            "more": False,
            "environments": [
                {
                    "environment": "Production",
                    "count": 2,
                    "severityCounts": {"major": 2},
                    "statusCounts": {"open": 2},
                }
            ],
        }
        trace = {"encoded_body_bytes": 128, "cache_result": "origin"}

        def request(*args):
            return 200, body, "a", "vm1", trace

        self.assertEqual(environments(request, None, None, {"key": 0}, expected, 0, True)["wire_bytes"], 128)
        trace["cache_result"] = "hit"
        with self.assertRaises(ValueError):
            environments(request, None, None, {"key": 0}, expected, 0, True)

    def test_trace_fit_isolates_sites_keys_and_exact_representations(self):
        from cache_replay_fit import estimate

        def row(t, site=0, identity=0, representation=0, eligible=True):
            return dict(
                time_ms=t, site=site, identity=identity, representation=representation, eligible=eligible
            )

        rows = [
            row(0),
            row(10),
            row(100),
            row(1100),
            row(1100, site=1),
            row(1100, identity=1),
            row(1100, representation=1),
            row(1100, eligible=False),
        ]
        actual = estimate(rows, 1000, 100)["total"]
        self.assertEqual(
            (actual["coalesced"], actual["warm"], actual["fills"], actual["bypass"]), (1, 1, 5, 1)
        )
        for invalid in ([row(2), row(1)], [dict(row(0), url="/alerts")], [row(0, identity="abc")]):
            with self.assertRaises(ValueError):
                estimate(invalid)

    def test_pair_gap_preserves_inputs_and_write_times(self):
        from cache_replay_model import PAIR_GAPS

        for cadence, gap in PAIR_GAPS.items():
            events = plan("shared-5s", 4, 5, cadence, 2)
            groups = {}
            for r in events:
                if r["kind"] == "dashboard":
                    groups.setdefault((r["site"], r["key"], r["path"]), []).append(r)
            self.assertEqual(len(groups), 8)
            for offers in groups.values():
                first, second = sorted(offers, key=lambda r: r["panel"])
                self.assertEqual((second["due_s"] - first["due_s"]) % 5, gap)
            writes = [r["due_s"] for r in events if r["kind"] == "write"]
            self.assertEqual(writes, [0.15, 1.4, 2.65, 3.9])
        with self.assertRaises(ValueError):
            plan("mixed", 4, 5, "paired-500")

    def test_newest_plan(self):
        rows = plan("mixed", 4, 50, "spread", newest=True)
        writes = [r for r in rows if r["kind"] == "write"]
        self.assertEqual(sum(r["fresh"] for r in writes), 10)
        self.assertEqual({r["key"] for r in writes if r["fresh"]}, {8, 9, 10, 11})
        self.assertTrue(all("sort-by=lastReceiveTime" in r["path"] for r in rows if r["kind"] == "dashboard"))


if __name__ == "__main__":
    unittest.main()
