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
and cell() of bench_compare.py). The order of the version and arm pairs
moves by one each repetition. Every planned cell must exist and be valid.

Limits: default server settings; 1000 alerts, all in memory; one runner. A
difference of a few percent is inside the noise between repetitions.
"""

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
        self.saved = (bc.PG["port"], bc.PG_DATA, bc.PG_BIN)
        bc.PG["port"], bc.PG_DATA, bc.PG_BIN = PORTS[self.pg], DATA[self.pg], bins(bc)[self.pg]

    def __exit__(self, *exc):
        bc = self.bc
        bc.PG["port"], bc.PG_DATA, bc.PG_BIN = self.saved


def prepare(bc, record):
    """Restore the seed into both clusters and make every copy. Before
    pgbouncer starts: a template must have no connection open."""
    rec = record["pg19"] = {"arms": arms(bc), "versions": {}, "checks": [], "cells": []}
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
    """Every cell of the suite. True when all checks passed and every
    planned cell exists and is valid."""
    rec, dbs, ok = record["pg19"], databases(bc), True
    order0 = pairs(bc)
    for rep in range(bc.REPS):
        order = order0[rep % len(order0):] + order0[:rep % len(order0)]
        for pg, arm in order:
            sub = {"cells": [], "checks": [], "seed": record["seed"]}
            with use(bc, pg):
                db = dbs[(pg, arm, "read")]
                with open(bc.OUT / f"server-pg{pg}-{arm}-r{rep}-read.log", "w") as logf:
                    proc, base = bc.start(arm, db, logf)
                    try:
                        if bc.check(f"pg{pg}-{arm}", base, db, sub):
                            alive = True
                            for case in ("list", "count"):
                                for conc in bc.CONCURRENCY:
                                    if alive:
                                        alive = bc.cell(arm, proc, base, case, conc, rep, db,
                                                        json_flag, sub)
                        else:
                            ok = False
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
                c["pg"] = pg
            rec["cells"] += sub["cells"]
            rec["checks"] += sub["checks"]
            bc.log(f"pg19 suite: PostgreSQL {pg}, {arm}, repetition {rep} done")
    planned = plan(bc)
    got = {(c["pg"], c["arm"], c["case"], c["c"], c["rep"]): c["valid"] for c in rec["cells"]}
    rec["gate"] = {"expected": len(planned),
                   "missing": [list(k) for k in planned if k not in got],
                   "invalid": [list(k) for k in planned if got.get(k) is False],
                   "checks_ok": ok}
    rec["gate"]["ok"] = ok and not rec["gate"]["missing"] and not rec["gate"]["invalid"]
    return rec["gate"]["ok"]


def summary(record):
    rec = record["pg19"]
    g = rec.get("gate")
    lines = ["", "## Alerta on PostgreSQL 18.7 and on PostgreSQL 19", ""]
    lines += [f"- {v}" for v in rec["versions"].values()]
    lines += ["", "Both clusters have the same settings and the same restored data. Medians across "
                  "valid repetitions. A change of a few percent is inside the noise of one runner.", ""]
    if g:
        lines += [f"Cells: {g['expected']} planned, {len(g['missing'])} missing, "
                  f"{len(g['invalid'])} invalid; checks {'passed' if g['checks_ok'] else 'FAILED'}.", ""]
    rows = {}
    for c in rec["cells"]:
        if c["valid"]:
            rows.setdefault((c["case"], c["c"], c["arm"], c["pg"]), []).append(c)
    lines += ["| case | c | arm | rps on 18 | rps on 19 | change | p50 ms 18 | p50 ms 19 | "
              "postgres CPU ms/req 18 | postgres CPU ms/req 19 | server CPU ms/req 18 | server CPU ms/req 19 |",
              "|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for case in ("list", "count", "write-dup"):
        for conc in record["concurrency"]:
            for arm in rec["arms"]:
                a, b = rows.get((case, conc, arm, 18)), rows.get((case, conc, arm, 19))
                if not a or not b:
                    lines.append(f"| {case} | {conc} | {arm} | not scored | | | | | | | | |")
                    continue
                med = lambda cs, k: statistics.median(x[k] for x in cs)
                r18, r19 = med(a, "rps"), med(b, "rps")
                lines.append(
                    f"| {case} | {conc} | {arm} | {r18:.0f} | {r19:.0f} | {100 * (r19 / r18 - 1):+.1f}% | "
                    f"{med(a, 'p50_ms'):.2f} | {med(b, 'p50_ms'):.2f} | "
                    f"{med(a, 'postgres_cpu_ms_per_request'):.3f} | {med(b, 'postgres_cpu_ms_per_request'):.3f} | "
                    f"{med(a, 'server_cpu_ms_per_request'):.3f} | {med(b, 'server_cpu_ms_per_request'):.3f} |")
    for c in rec["cells"]:
        if not c["valid"]:
            lines.append(f"- INVALID PostgreSQL {c['pg']} {c['arm']} {c['case']} c={c['c']} r{c['rep']}: "
                         f"errors {c['errors']}, non-2xx {c['non_2xx']}, alive {c['server_alive']}")
    for c in rec["checks"]:
        if not c["ok"]:
            lines.append(f"- FAILED check {c['arm']}")
    return lines
