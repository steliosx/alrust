"""The pg19 suite of bench_compare.py: Alerta on PostgreSQL 18.7 and on
PostgreSQL 19, side by side on one runner. No alrust arm: alrust's pin is
PostgreSQL 18.7 (DECISIONS Y67) and this suite does not change it.

Question: does a newer PostgreSQL alone make Alerta faster, with the
settings and the add-on (alerta_fast.py) that the tuning and addon suites
found?

Both clusters run for the whole suite, with the same initdb options and
server settings (setup.sh): 18.7 on port 5432 and 19 on port 5433. Only one
has load at a time. One pgbouncer serves both; it sends this suite's
PostgreSQL 19 databases to port 5433 by name.

Data: the seeded database (bench_seed, on 18.7) is dumped and restored into
each cluster with that cluster's own pg_dump and pg_restore, then analyzed.
Each cell's database is a copy of that restored database, so both versions
start from the same rows, loaded the same way.

Arms, each on both versions, Alerta's code unchanged:
  alerta-NxM-nofwd        direct connections, no forwarder plugin
  alerta-NxM-pgbu-nofwd   pgbouncer's Unix socket (the best settings arm)
  alerta-NxM-safe-nofwd   the add-on's safe patches (connection reuse)

Cases, checks and the validity of a cell are the compare suite's (check()
and cell() of bench_compare.py).

Order: in a repetition the two versions of an arm run one after the other.
Which version runs first changes with the arm and with the repetition
(pair_order()); the arms move by one each repetition. The order is in the
record. With an odd number of repetitions one version is first once more
than the other for each arm.

Raw samples: every warm-up and timed run has its own file,
`<phase>-pg19-pg<major>-<arm>-<case>-c<n>-r<rep>.json`. A file that exists
is never written again (oha() of bench_compare.py). After the cells, each
cell's numbers are computed again from its own file and must be equal
(verify_raw()).

The gate (gate()) wants each planned cell once and valid, each raw file
name used by one cell only and verified, a passed data check for each
version, arm and repetition, and the recorded order. A comparison is
published for an arm and case only when the gate passed and every planned
repetition has one valid cell on each version (scores()); the change is the
median of the changes inside each repetition. self_test() runs first: it
breaks a complete record in each of those ways, and the gate and the scores
must refuse each.

Limits: default server settings; 1000 alerts, all in memory; one runner. A
difference of a few percent is inside the noise between repetitions.
"""

import copy
import json
import os
import statistics
import subprocess
import time
from pathlib import Path

PORTS = {18: "5432", 19: "5433"}
DATA = {18: "data", 19: "data19"}
SEED = "bench_pgs_seed"


def arms(bc):
    return [f"alerta-{bc.VCPUS}x4-{o}" for o in ("nofwd", "pgbu-nofwd", "safe-nofwd")]


def bins(bc):
    return {18: Path(os.environ["BENCH_PG_BIN"]), 19: Path(os.environ["BENCH_PG19_BIN"])}


def pairs(bc):
    return [(pg, arm) for arm in arms(bc) for pg in PORTS]


def plan(bc):
    return [(pg, arm, case, conc, rep) for rep in range(bc.REPS) for pg, arm in pairs(bc)
            for case in bc.CASES for conc in bc.CONCURRENCY]


def pair_order(names, rep):
    """(version, arm) in the order of one repetition. The arms move by one
    each repetition; the version that runs first changes with the arm and
    with the repetition."""
    order = []
    for arm in names[rep % len(names):] + names[:rep % len(names)]:
        first = 18 if (names.index(arm) + rep) % 2 == 0 else 19
        order += [(first, arm), (37 - first, arm)]
    return order


def raw_tag(pg):
    return f"pg19-pg{pg}-"


def raw_names(pg, arm, case, conc, rep):
    return {phase: f"{phase}-{raw_tag(pg)}{arm}-{case}-c{conc}-r{rep}.json"
            for phase in ("warm", "timed")}


def databases(bc):
    """(version, arm, kind, repetition, concurrency) -> database name."""
    dbs = {}
    for pg, arm in pairs(bc):
        stem = f"bench_pg{pg}_{arm}".replace("-", "_")
        dbs[(pg, arm, "read")] = f"{stem}_read"
        for rep in range(bc.REPS):
            for conc in bc.CONCURRENCY:
                dbs[(pg, arm, "write", rep, conc)] = f"{stem}_w_r{rep}_c{conc}"
    return dbs


def pgbouncer_databases(bc):
    """pgbouncer.ini lines that send the PostgreSQL 19 databases to its port."""
    return "".join(f"{name} = host=127.0.0.1 port={PORTS[19]}\n"
                   for key, name in databases(bc).items() if key[0] == 19)


class use:
    """Point bench_compare's PostgreSQL helpers at one cluster."""

    def __init__(self, bc, pg):
        self.bc, self.pg = bc, pg

    def __enter__(self):
        bc = self.bc
        self.saved = (bc.PG["port"], bc.PG_DATA, bc.PG_BIN, bc.RAW_TAG)
        bc.PG["port"], bc.PG_DATA, bc.PG_BIN = PORTS[self.pg], DATA[self.pg], bins(bc)[self.pg]
        bc.RAW_TAG = raw_tag(self.pg)

    def __exit__(self, *exc):
        bc = self.bc
        bc.PG["port"], bc.PG_DATA, bc.PG_BIN, bc.RAW_TAG = self.saved


def prepare(bc, record):
    """Restore the seed into both clusters and make every copy. Before
    pgbouncer starts: a template must have no connection open."""
    rec = record["pg19"] = {"arms": arms(bc), "versions": {}, "order": [], "checks": [], "cells": []}
    env = dict(os.environ, PGPASSWORD=bc.PG["password"])
    common = ["-h", bc.PG["host"], "-U", bc.PG["user"]]
    for pg in PORTS:
        tools, dump = bins(bc)[pg], bc.WORK / f"seed-{pg}.dump"
        subprocess.run([str(tools / "pg_dump"), *common, "-p", PORTS[18], "-Fc", "-f", str(dump),
                        "bench_seed"], env=env, check=True)
        with use(bc, pg):
            bc.psql("postgres", f"CREATE DATABASE {SEED}")
            subprocess.run([str(tools / "pg_restore"), *common, "-p", PORTS[pg], "--no-owner",
                            "--exit-on-error", "-d", SEED, str(dump)], env=env, check=True)
            bc.psql(SEED, "ANALYZE")
            rec["versions"][str(pg)] = bc.psql("postgres", "SELECT version()")
            alerts = int(bc.psql(SEED, "SELECT count(*) FROM alerts"))
            if alerts != record["seed"]["alerts"]:
                raise RuntimeError(f"PostgreSQL {pg}: {alerts} alerts restored, "
                                   f"{record['seed']['alerts']} seeded")
            for key, name in databases(bc).items():
                if key[0] != pg:
                    continue
                for attempt in range(10):
                    try:
                        bc.psql("postgres", f"CREATE DATABASE {name} TEMPLATE {SEED}")
                        break
                    except subprocess.CalledProcessError:
                        if attempt == 9:
                            raise
                        time.sleep(1)
    bc.log(f"pg19 suite: {len(databases(bc))} database copies on {rec['versions']}")


def run(bc, record, json_flag):
    """Every cell of the suite. True when the gate passes."""
    rec, dbs, planned = record["pg19"], databases(bc), plan(bc)
    rec["self_test"] = self_test(planned)
    bc.log(f"pg19 suite: gate self-test, {len(rec['self_test'])} cases, as expected")
    for rep in range(bc.REPS):
        for position, (pg, arm) in enumerate(pair_order(arms(bc), rep)):
            rec["order"].append({"rep": rep, "position": position, "pg": pg, "arm": arm,
                                 "first_of_its_pair": position % 2 == 0})
            sub = {"cells": [], "checks": [], "seed": record["seed"]}
            with use(bc, pg):
                db = dbs[(pg, arm, "read")]
                with open(bc.OUT / f"server-pg{pg}-{arm}-r{rep}-read.log", "w") as logf:
                    proc, base = bc.start(arm, db, logf)
                    try:
                        if bc.check(f"pg{pg}-{arm}-r{rep}", base, db, sub):
                            alive = True
                            for case in ("list", "count"):
                                for conc in bc.CONCURRENCY:
                                    if alive:
                                        alive = bc.cell(arm, proc, base, case, conc, rep, db,
                                                        json_flag, sub)
                    finally:
                        bc.stop(proc)
                for conc in bc.CONCURRENCY if sub["cells"] else []:
                    db = dbs[(pg, arm, "write", rep, conc)]
                    with open(bc.OUT / f"server-pg{pg}-{arm}-r{rep}-write-c{conc}.log", "w") as logf:
                        proc, base = bc.start(arm, db, logf)
                        try:
                            bc.cell(arm, proc, base, "write-dup", conc, rep, db, json_flag, sub)
                        finally:
                            bc.stop(proc)
            for c in sub["cells"]:
                c.update({"pg": pg, "first_of_its_pair": position % 2 == 0})
            for c in sub["checks"]:
                c.update({"pg": pg, "suite_arm": arm, "rep": rep})
            rec["cells"] += sub["cells"]
            rec["checks"] += sub["checks"]
            bc.log(f"pg19 suite: PostgreSQL {pg}, {arm}, repetition {rep} done")
    verify_raw(bc, rec)
    rec["gate"] = gate(rec, planned)
    rec["scores"] = scores(rec, planned, rec["gate"]["ok"])
    return rec["gate"]["ok"]


RAW_KEYS = ("rps", "requests", "ok", "errors", "non_2xx", "p50_ms", "p99_ms")


def verify_raw(bc, rec):
    """Each cell's numbers, computed again from its own raw file."""
    for c in rec["cells"]:
        want = raw_names(c["pg"], c["arm"], c["case"], c["c"], c["rep"])
        problems = []
        if c.get("raw") != want:
            problems.append(f"raw names {c.get('raw')!r}, {want!r} expected")
        else:
            for phase, numbers in (("warm", c["warm"]), ("timed", c)):
                path = bc.OUT / "raw" / want[phase]
                try:
                    again = bc.summarize_oha(json.loads(path.read_text()))
                except (OSError, ValueError) as e:
                    problems.append(f"{want[phase]}: {e}")
                    continue
                differ = [k for k in RAW_KEYS if again[k] != numbers[k]]
                if differ:
                    problems.append(f"{want[phase]}: {differ} differ from the cell's numbers")
        c["raw_verified"], c["raw_problems"] = not problems, problems
        if problems:
            bc.log(f"pg19 suite: RAW SAMPLE CHECK FAILED, PostgreSQL {c['pg']} {c['arm']} "
                   f"{c['case']} c={c['c']} r{c['rep']}: {problems}")


def gate(rec, planned):
    """What a complete, passed suite has. Anything absent is named."""
    g = {"expected": len(planned), "missing": [], "invalid": [], "duplicate": [],
         "raw_collisions": [], "raw_unverified": [], "missing_checks": [], "missing_order": []}
    cells, raw_owner = {}, {}
    for c in rec.get("cells", []):
        key = (c["pg"], c["arm"], c["case"], c["c"], c["rep"])
        if key in cells:
            g["duplicate"].append(list(key))
        cells[key] = c
        names = list((c.get("raw") or {}).values())
        if len(names) != 2 or len(set(names)) != 2:
            g["raw_unverified"].append(list(key))
        for name in names:
            if name in raw_owner:
                g["raw_collisions"].append(name)
            raw_owner[name] = key
    for key in planned:
        c = cells.get(key)
        if c is None:
            g["missing"].append(list(key))
            continue
        if c.get("valid") is not True:
            g["invalid"].append(list(key))
        if c.get("raw_verified") is not True and list(key) not in g["raw_unverified"]:
            g["raw_unverified"].append(list(key))
    runs = sorted({(pg, arm, rep) for pg, arm, _, _, rep in planned})
    for pg, arm, rep in runs:
        checks = [c for c in rec.get("checks", [])
                  if (c.get("pg"), c.get("suite_arm"), c.get("rep")) == (pg, arm, rep)]
        if len(checks) != 1 or checks[0].get("ok") is not True:
            g["missing_checks"].append([pg, arm, rep])
        order = [o for o in rec.get("order", [])
                 if (o.get("pg"), o.get("arm"), o.get("rep")) == (pg, arm, rep)]
        if len(order) != 1 or not isinstance(order[0].get("first_of_its_pair"), bool):
            g["missing_order"].append([pg, arm, rep])
    for arm, rep in sorted({(arm, rep) for _, arm, rep in runs}):
        firsts = [o.get("first_of_its_pair") for o in rec.get("order", [])
                  if (o.get("arm"), o.get("rep")) == (arm, rep)]
        if sorted(firsts) != [False, True] and [arm, rep] not in g["missing_order"]:
            g["missing_order"].append([arm, rep])
    g["ok"] = not any(g[k] for k in g if k != "expected")
    return g


def scores(rec, planned, gate_ok):
    """One row for each case, concurrency and arm. A row has numbers only
    when the gate passed and every planned repetition has one valid,
    verified cell on each version."""
    rows = []
    groups = sorted({(case, conc, arm) for _, arm, case, conc, _ in planned})
    reps = sorted({rep for *_, rep in planned})
    for case, conc, arm in groups:
        row = {"case": case, "c": conc, "arm": arm, "scored": False, "reps": reps}
        rows.append(row)
        if not gate_ok:
            row["reason"] = "the gate did not pass"
            continue
        matched, unmatched = [], []
        for rep in reps:
            pair = {}
            for pg in PORTS:
                cs = [c for c in rec["cells"]
                      if (c["pg"], c["arm"], c["case"], c["c"], c["rep"]) == (pg, arm, case, conc, rep)]
                if len(cs) == 1 and cs[0].get("valid") is True and cs[0].get("raw_verified") is True:
                    pair[pg] = cs[0]
            (matched if len(pair) == len(PORTS) else unmatched).append((rep, pair))
        if unmatched:
            row["reason"] = f"repetitions {[rep for rep, _ in unmatched]} have no valid cell on each version"
            continue
        med = lambda pg, k: statistics.median(pair[pg][k] for _, pair in matched)
        row.update({"scored": True,
                    "first_was_18_in_reps": [rep for rep, pair in matched
                                             if pair[18].get("first_of_its_pair")],
                    "change_by_rep": [pair[19]["rps"] / pair[18]["rps"] - 1 for _, pair in matched]})
        row["change"] = statistics.median(row["change_by_rep"])
        for pg in PORTS:
            for k in ("rps", "p50_ms", "postgres_cpu_ms_per_request", "server_cpu_ms_per_request"):
                row[f"{k}_{pg}"] = med(pg, k)
    return rows


def self_test(planned):
    """The gate and the scores must accept a complete record and refuse it
    when it is broken. Returns the cases; raises when one is wrong."""
    full = {"cells": [], "checks": [], "order": []}
    for pg, arm, case, conc, rep in planned:
        full["cells"].append({"pg": pg, "arm": arm, "case": case, "c": conc, "rep": rep,
                              "valid": True, "raw_verified": True, "first_of_its_pair": pg == 18,
                              "raw": raw_names(pg, arm, case, conc, rep), "rps": 100.0 + pg,
                              "p50_ms": 1.0, "postgres_cpu_ms_per_request": 1.0,
                              "server_cpu_ms_per_request": 1.0})
    for pg, arm, rep in sorted({(pg, arm, rep) for pg, arm, _, _, rep in planned}):
        full["checks"].append({"pg": pg, "suite_arm": arm, "rep": rep, "ok": True})
        full["order"].append({"pg": pg, "arm": arm, "rep": rep, "first_of_its_pair": pg == 18})

    def same_raw(rec):
        # What the suite did before: the samples of one version under the
        # other version's file names.
        a = rec["cells"][0]
        b = next(c for c in rec["cells"] if c["pg"] != a["pg"]
                 and (c["arm"], c["case"], c["c"], c["rep"]) == (a["arm"], a["case"], a["c"], a["rep"]))
        b["raw"] = dict(a["raw"])

    def twice(rec):
        rec["cells"].append(dict(rec["cells"][0]))

    mutations = [("a cell removed (a repetition without its pair)", lambda r: r["cells"].pop(0)),
                 ("a cell not valid", lambda r: r["cells"][0].update(valid=False)),
                 ("a cell twice", twice),
                 ("two cells with the same raw file names", same_raw),
                 ("a cell without raw file names", lambda r: r["cells"][0].pop("raw")),
                 ("a cell whose raw sample is not verified",
                  lambda r: r["cells"][0].update(raw_verified=False)),
                 ("a data check removed", lambda r: r["checks"].pop(0)),
                 ("a data check not passed", lambda r: r["checks"][0].update(ok=False)),
                 ("an order record removed", lambda r: r["order"].pop(0)),
                 ("both versions recorded as first",
                  lambda r: next(o for o in r["order"] if o["pg"] == 19).update(first_of_its_pair=True))]
    g = gate(full, planned)
    rows = scores(full, planned, g["ok"])
    cases = [{"case": "complete record", "gate_must_pass": True, "gate_passed": g["ok"],
              "rows_scored": sum(r["scored"] for r in rows), "rows": len(rows)}]
    wrong = [] if g["ok"] and all(r["scored"] for r in rows) else ["complete record"]
    for name, change in mutations:
        rec = copy.deepcopy(full)
        change(rec)
        g = gate(rec, planned)
        rows = scores(rec, planned, g["ok"])
        cases.append({"case": name, "gate_must_pass": False, "gate_passed": g["ok"],
                      "rows_scored": sum(r["scored"] for r in rows), "rows": len(rows)})
        if g["ok"] or any(r["scored"] for r in rows):
            wrong.append(name)
    # The scores alone, with a gate that is said to have passed: the row of
    # an unmatched repetition must still have no numbers.
    rec = copy.deepcopy(full)
    gone = rec["cells"].pop(0)
    rows = scores(rec, planned, True)
    bad = [r for r in rows if (r["case"], r["c"], r["arm"]) == (gone["case"], gone["c"], gone["arm"])]
    cases.append({"case": "scores alone: a repetition without its pair", "gate_must_pass": False,
                  "gate_passed": False, "rows_scored": sum(r["scored"] for r in rows), "rows": len(rows)})
    if len(bad) != 1 or bad[0]["scored"]:
        wrong.append("scores alone")
    if wrong:
        raise RuntimeError(f"the pg19 suite's gate or scores are wrong for: {wrong}")
    return cases


def summary(record):
    rec = record["pg19"]
    g = rec.get("gate")
    lines = ["", "## Alerta on PostgreSQL 18.7 and on PostgreSQL 19", ""]
    lines += [f"- {v}" for v in rec["versions"].values()]
    lines += ["", "Both clusters have the same settings and the same restored data. A row has numbers "
                  "only when every planned repetition has one valid cell on each version; the numbers "
                  "are medians across those repetitions and the change is the median of the changes "
                  "inside each repetition. The version that ran first changes with the arm and the "
                  "repetition (column `18 first`). A change of a few percent is inside the noise of "
                  "one runner.", ""]
    if g:
        lines += [f"Cells: {g['expected']} planned, {len(g['missing'])} missing, {len(g['invalid'])} "
                  f"invalid, {len(g['duplicate'])} twice; raw samples: {len(g['raw_collisions'])} file "
                  f"names used twice, {len(g['raw_unverified'])} cells not verified against their own "
                  f"file; data checks absent or failed: {len(g['missing_checks'])}; order records "
                  f"absent: {len(g['missing_order'])}. Gate self-test: {len(rec.get('self_test', []))} "
                  "cases as expected.", ""]
    lines += ["| case | c | arm | reps | 18 first in | rps on 18 | rps on 19 | change | p50 ms 18 | p50 ms 19 | "
              "postgres CPU ms/req 18 | postgres CPU ms/req 19 | server CPU ms/req 18 | server CPU ms/req 19 |",
              "|---|---:|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    by = {(r["case"], r["c"], r["arm"]): r for r in rec.get("scores", [])}
    for case in ("list", "count", "write-dup"):
        for conc in record["concurrency"]:
            for arm in rec["arms"]:
                r = by.get((case, conc, arm))
                if not r or not r["scored"]:
                    why = r["reason"] if r else "no result"
                    lines.append(f"| {case} | {conc} | {arm} | not scored: {why} | | | | | | | | | | |")
                    continue
                first = ", ".join(f"r{x}" for x in r["first_was_18_in_reps"]) or "none"
                lines.append(
                    f"| {case} | {conc} | {arm} | {len(r['reps'])} | {first} | {r['rps_18']:.0f} | "
                    f"{r['rps_19']:.0f} | {100 * r['change']:+.1f}% | {r['p50_ms_18']:.2f} | "
                    f"{r['p50_ms_19']:.2f} | {r['postgres_cpu_ms_per_request_18']:.3f} | "
                    f"{r['postgres_cpu_ms_per_request_19']:.3f} | {r['server_cpu_ms_per_request_18']:.3f} | "
                    f"{r['server_cpu_ms_per_request_19']:.3f} |")
    for c in rec["cells"]:
        if not c["valid"] or not c.get("raw_verified"):
            lines.append(f"- INVALID PostgreSQL {c['pg']} {c['arm']} {c['case']} c={c['c']} r{c['rep']}: "
                         f"errors {c['errors']}, non-2xx {c['non_2xx']}, alive {c['server_alive']}, "
                         f"raw sample {c.get('raw_problems')}")
    for c in rec["checks"]:
        if not c["ok"]:
            lines.append(f"- FAILED check {c['arm']}")
    return lines
