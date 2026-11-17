"""Tests of the mixed suite's gates that need no server (Codex 266-R1,
266-R2). Run: python3 -I scripts/bench/test_mixed_checks.py

They cover the pure checks, the reader's body check and that an invalid
cell is reported as invalid by score_cell and makes run_mixed report
failure. They do not start a server, a database or a client. They do not
run mixed_cell (its last statement returns score_cell's answer) or
bench_compare.main (which exits non-zero when run_mixed reports failure).
"""

import copy
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import mixed_checks  # noqa: E402


def load(name):
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def good_cell():
    return {
        "server_alive": True, "problems": [],
        "writes": {"errors": 0, "non_2xx": 0, "ok": 10},
        "reads": {"errors": 0, "non_2xx": 0, "ok": 10, "body_problems": 0},
        "persisted": {"ok": True}, "final_check": {"ok": True},
    }


class CellValid(unittest.TestCase):
    def test_a_clean_cell_is_valid(self):
        self.assertTrue(mixed_checks.cell_valid(good_cell(), 8, 16))

    def test_each_post_load_failure_makes_the_cell_invalid(self):
        changes = {
            "persistence": lambda s: s["persisted"].update(ok=False),
            "final check": lambda s: s["final_check"].update(ok=False),
            "list body": lambda s: s["reads"].update(body_problems=1),
            "client did not stop": lambda s: s["problems"].append("a client did not stop in 60 s"),
            "client exit code": lambda s: s["problems"].append("a client exited with 1"),
            "no answer": lambda s: s["writes"].update(errors=1),
            "non-2xx": lambda s: s["reads"].update(non_2xx=1),
            "server died": lambda s: s.update(server_alive=False),
            "no write scored": lambda s: s["writes"].update(ok=0),
            "no read scored": lambda s: s["reads"].update(ok=0),
        }
        for name, change in changes.items():
            with self.subTest(name):
                s = copy.deepcopy(good_cell())
                change(s)
                self.assertFalse(mixed_checks.cell_valid(s, 8, 16))

    def test_a_class_with_no_clients_needs_no_answers(self):
        s = good_cell()
        s["reads"]["ok"] = 0
        self.assertTrue(mixed_checks.cell_valid(s, 8, 0))
        s = good_cell()
        s["writes"]["ok"] = 0
        self.assertTrue(mixed_checks.cell_valid(s, 0, 16))


class Times(unittest.TestCase):
    def test_parse(self):
        api = mixed_checks.parse_api_time("2026-11-16T01:02:03.456Z")
        self.assertEqual(api, mixed_checks.parse_db_time("2026-11-16T01:02:03.456"))
        self.assertEqual(api, mixed_checks.parse_db_time("2026-11-16T01:02:03.456+00:00"))
        self.assertEqual(api + 789, mixed_checks.parse_db_time("2026-11-16T01:02:03.456789"))
        self.assertEqual(mixed_checks.parse_api_time("1970-01-01T00:00:00.001Z"), 1000)
        # PostgreSQL drops trailing zeros of the fraction.
        self.assertEqual(mixed_checks.parse_api_time("2026-11-16T01:02:03.400Z"),
                         mixed_checks.parse_db_time("2026-11-16T01:02:03.4"))
        self.assertEqual(mixed_checks.parse_api_time("2026-11-16T01:02:03.000Z"),
                         mixed_checks.parse_db_time("2026-11-16T01:02:03"))

    def test_malformed_served_times(self):
        for bad in ("2026-11-16T01:02:03Z", "2026-11-16T01:02:03.456", "2026-11-16 01:02:03.456Z",
                    "2026-11-16T01:02:03.456789Z", "2026-11-16T01:02:03.456+00:00",
                    "2026-13-16T01:02:03.456Z", "zzzz-zz-zzTzz:zz:zz.zzzZ", "", None, 1763254923456):
            with self.subTest(bad):
                self.assertIsNone(mixed_checks.parse_api_time(bad))
                problems = mixed_checks.served_time_problems(
                    [{"id": "a", "lastReceiveTime": bad}],
                    {"a": {"last_receive_time": "2026-11-16T01:02:03.456"}})
                self.assertEqual(len(problems), 1)

    def test_a_served_time_must_be_its_rows_time(self):
        rows = {"a": {"last_receive_time": "2026-11-16T01:02:03.456"},
                "b": {"last_receive_time": "2026-11-16T01:02:03.455"}}
        good = [{"id": "a", "lastReceiveTime": "2026-11-16T01:02:03.456Z"},
                {"id": "b", "lastReceiveTime": "2026-11-16T01:02:03.455Z"}]
        self.assertEqual(mixed_checks.served_time_problems(good, rows), [])
        # Codex's counterexample: the right ids, in order, with one stale or
        # one made-up time for all of them.
        stale = [dict(good[0], lastReceiveTime="2026-11-16T01:02:03.455Z"), good[1]]
        self.assertEqual(len(mixed_checks.served_time_problems(stale, rows)), 1)
        bogus = [dict(a, lastReceiveTime="2000-01-01T00:00:00.000Z") for a in good]
        self.assertEqual(len(mixed_checks.served_time_problems(bogus, rows)), 2)

    def test_a_row_with_microseconds_is_compared_by_its_millisecond(self):
        rows = {"a": {"last_receive_time": "2026-11-16T01:02:03.456999"}}
        self.assertEqual(mixed_checks.served_time_problems(
            [{"id": "a", "lastReceiveTime": "2026-11-16T01:02:03.456Z"}], rows), [])
        self.assertEqual(len(mixed_checks.served_time_problems(
            [{"id": "a", "lastReceiveTime": "2026-11-16T01:02:03.457Z"}], rows)), 1)

    def test_equal_times_are_in_order_and_a_rise_is_not(self):
        t = "2026-11-16T01:02:03.456"
        rows = {k: {"last_receive_time": t} for k in "abc"}
        equal = [{"id": k, "lastReceiveTime": t + "Z"} for k in "cab"]
        self.assertEqual(mixed_checks.served_time_problems(equal, rows), [])
        rows["c"] = {"last_receive_time": "2026-11-16T01:02:03.457"}
        rising = [{"id": "a", "lastReceiveTime": t + "Z"},
                  {"id": "c", "lastReceiveTime": "2026-11-16T01:02:03.457Z"}]
        self.assertEqual(mixed_checks.served_time_problems(rising, rows),
                         ["the served lastReceiveTime values are not in descending order"])

    def test_a_row_time_that_does_not_parse_is_a_problem(self):
        self.assertEqual(len(mixed_checks.served_time_problems(
            [{"id": "a", "lastReceiveTime": "2026-11-16T01:02:03.456Z"}],
            {"a": {"last_receive_time": "yesterday"}})), 1)


class ReaderBodyCheck(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = load("mixed_client")
        cls.checks = load("mixed_checks")

    def body(self, times):
        return json.dumps({"alerts": [{"lastReceiveTime": t} for t in times]}).encode()

    def test_good_bodies(self):
        ok = ["2026-11-16T01:02:03.456Z", "2026-11-16T01:02:03.456Z", "2026-11-16T01:02:03.455Z"]
        self.assertIsNone(self.client.read_problem(200, self.body(ok)))
        self.assertIsNone(self.client.read_problem(200, self.body([])))
        self.assertIsNone(self.client.read_problem(500, b"not json"))  # counted by its status

    def test_bad_bodies(self):
        rising = ["2026-11-16T01:02:03.455Z", "2026-11-16T01:02:03.456Z"]
        for raw in (self.body(rising), self.body(["x" * 24, "x" * 24]),
                    self.body(["2026-11-16T01:02:03Z"]), self.body([None]),
                    self.body(["2026-11-16T01:02:03.456Z"] * 51), b"{", b"{}", b"[]"):
            with self.subTest(raw[:40]):
                self.assertIsNotNone(self.client.read_problem(200, raw))

    def test_times_of_the_right_form_that_are_no_real_time(self):
        good = "2026-11-16T01:02:03.456Z"
        for bad in ("2026-13-16T01:02:03.456Z", "2026-00-16T01:02:03.456Z",
                    "2026-02-30T01:02:03.456Z", "2026-11-31T01:02:03.456Z",
                    "2026-11-00T01:02:03.456Z", "2026-02-29T01:02:03.456Z",
                    "2026-11-16T24:02:03.456Z", "2026-11-16T01:60:03.456Z",
                    "2026-11-16T01:02:60.456Z", "2026-11-16T01:02:61.456Z",
                    "0000-11-16T01:02:03.456Z"):
            with self.subTest(bad):
                self.assertFalse(self.client.real_time(bad))
                self.assertIsNone(self.checks.parse_api_time(bad))  # one rule in both
                for times in ([bad], [bad, bad], [bad, good], ["9999-12-31T23:59:59.999Z", bad]):
                    self.assertIsNotNone(self.client.read_problem(200, self.body(times)))

    def test_equal_real_times_are_in_order(self):
        for t in ("2026-11-16T01:02:03.456Z", "2028-02-29T23:59:59.999Z"):
            with self.subTest(t):
                self.assertTrue(self.client.real_time(t))
                self.assertIsNotNone(self.checks.parse_api_time(t))
                self.assertIsNone(self.client.read_problem(200, self.body([t] * 50)))


class SuiteExit(unittest.TestCase):
    """run_mixed with mixed_cell replaced: an invalid cell fails the suite
    and the cells after it still run."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        names = ("BENCH_WORK", "BENCH_PG_BIN", "BENCH_ALERTA_VENV", "BENCH_PGBOUNCER",
                 "ALRUST_BIN", "OHA_BIN")
        saved_env = {k: os.environ.get(k) for k in names + ("BENCH_MODE", "BENCH_SUITE")}
        saved_argv = sys.argv
        try:
            for k in names:
                os.environ[k] = cls.tmp.name
            os.environ["BENCH_MODE"], os.environ["BENCH_SUITE"] = "quick", "mixed"
            sys.argv = ["bench_compare.py", cls.tmp.name]
            cls.bench = load("bench_compare")
        finally:
            sys.argv = saved_argv
            for k, v in saved_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_with(self, answer):
        calls = []

        def fake(arm, name, rep, db, record):
            calls.append((arm, name, rep))
            return answer(len(calls))

        class AnyDb(dict):
            def __missing__(self, key):
                return "db"

        real = self.bench.mixed_cell
        self.bench.mixed_cell = fake
        try:
            return self.bench.run_mixed({}, AnyDb()), calls
        finally:
            self.bench.mixed_cell = real

    def full_cell(self):
        s = good_cell()
        s.update(arm="alrust-p16", case="mixed", rep=0, writers=8, readers=16)
        for k in ("writes", "reads"):
            s[k].update(rps=1.0, p50_ms=1.0, p99_ms=1.0)
        s["persisted"].update(alerts=1000, problems=[])
        s["final_check"].update(problems=[])
        return s

    def test_score_cell_returns_the_validity_and_keeps_the_record(self):
        record = {"mixed_cells": []}
        self.assertTrue(self.bench.score_cell(self.full_cell(), record))
        for name, change in (
                ("persistence", lambda s: s["persisted"].update(ok=False, problems=["1 row missing"])),
                ("list body", lambda s: s["reads"].update(body_problems=1)),
                ("drain", lambda s: s["problems"].append("a client did not stop in 60 s")),
                ("final check", lambda s: s["final_check"].update(ok=False, problems=["time"]))):
            with self.subTest(name):
                s = self.full_cell()
                change(s)
                self.assertFalse(self.bench.score_cell(s, record, ["a body problem"]))
                self.assertIs(record["mixed_cells"][-1], s)
                self.assertFalse(s["valid"])
        self.assertEqual(len(record["mixed_cells"]), 5)
        self.assertTrue(record["mixed_cells"][0]["valid"])

    def test_all_valid(self):
        ok, calls = self.run_with(lambda n: True)
        self.assertTrue(ok)
        self.assertEqual(len(calls), len(self.bench.MIX_ARMS) * len(self.bench.MIX_CELLS))

    def test_one_invalid_cell_fails_the_suite_and_the_rest_still_run(self):
        ok, calls = self.run_with(lambda n: n != 2)
        self.assertFalse(ok)
        self.assertEqual(len(calls), len(self.bench.MIX_ARMS) * len(self.bench.MIX_CELLS))


if __name__ == "__main__":
    unittest.main()
