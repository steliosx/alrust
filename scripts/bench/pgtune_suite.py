"""The pgtune suite of bench_compare.py, step 1: the ID prefix index alone.

Question: does FAST_INDEX (an index on alerts.id with text_pattern_ops)
make Alerta's write path faster when the table is large? With Alerta's
default plugins, every receive also runs update_tags() and
update_attributes(), whose `WHERE id = %(id)s OR id LIKE %(like_id)s` the
primary key's index cannot serve under a collation other than C (the
fastpath suite's plan check). Other PostgreSQL changes (fillfactor,
compression, vacuum, the last_receive_time and count indexes, durability
settings) are later steps, each in its own arm.

Arms: N workers x 4 threads, Alerta's default plugins (`forwarder`
included), behind pgbouncer in transaction mode over TCP with SCRAM (the
two-site lab's pool hop). Stock Alerta and the add-on's default are
separate baselines, each without and with the index:
  alerta-NxM-pgb-pgt            stock Alerta
  alerta-NxM-pgb-idx-pgt        stock Alerta and FAST_INDEX
  alerta-NxM-pgb-safe-pgt       the add-on's default (`safe`)
  alerta-NxM-pgb-safe-idx-pgt   the add-on's default and FAST_INDEX
The option `pgt` only names the arms (and selects their seed); it changes
nothing in the server.

Data: PGTUNE_ROWS alerts in all (1000 or 100000; the workflow's input,
100000 on pull requests so that path runs before any full run). The
template bench_seed_pgtune is a copy of the seed with PGTUNE_ROWS - 1000
more alerts made from the seeded ones in SQL: new UUID ids (Alerta's
shape), last_receive_id the new id, every history entry's id the new id,
and every time one day or more older than the oldest seeded alert, so the
50 newest alerts, and so the read checks, are those of the seed. The other
columns (tags, attributes, history depth, payload sizes) are the seeded
rows'. Then VACUUM (ANALYZE), once, so every copy has the same statistics
and visibility map. Every pgtune copy (read, write and the plan copy) is a
copy of that template; the other suites keep bench_seed.

Cases, checks and the validity of a cell are the compare suite's. The
read checks compare the pgtune arms with the first pgtune arm (their
count bodies differ from the seed's).

Recorded before any cell (prepare()):
  database  each pgtune copy's collation, ctype, locale provider and
            locale, and the collation of alerts.id; the indexes of alerts;
  probes    on a throwaway copy (bench_pgtune_plan), EXPLAIN (ANALYZE,
            BUFFERS, FORMAT JSON) of three predicate probes: UPDATEs with
            the WHERE clauses of Alerta's tag, attribute and duplicate
            writes (an existing alert's id and identity), which set a
            column to itself or only the counter. They are not Alerta's
            statements: their plans show which scan each predicate gets;
            their times and buffers are the probes', not those of the
            deployed statements (the write cells measure those through
            Alerta). First without FAST_INDEX, then with it. The probes
            run (the copy is not used again).
Order (order()): the four arms are two pairs, stock and `safe`, each a
control and its index arm. pg_suite.pair_order() places them: the pairs
move by one each repetition, and which member runs first changes with the
pair and the repetition (with an odd number of repetitions one member is
first once more). Each repetition's order is in the record, with the pair
and the member of each arm; pair_changes() gives, per pair, case, clients
and repetition, the index arm's change against its control.
In each write cell, the WAL insert position is read at the two points
where the duplicate-count gate reads (after the warm-up and after the
timed run, each after the cell's pause), so WAL per write of the timed
window divides that window's WAL by that window's saved duplicates.
WAL is the cluster's, not the copy's: autovacuum or a checkpoint in the
window adds to it. So a CHECKPOINT runs before each write cell's server
starts (before_write()): every cell then writes its first full-page
images in start-up and warm-up, and no timed checkpoint (5 minutes
apart by default) is due inside a cell of well under a minute. As a
backstop, each read also takes the last completed checkpoint's redo
position and pg_stat_checkpointer's counts; if either changed in the
window the cell is marked (`checkpoint_in_window`) and its WAL is left
out of pair_changes(). The mark shows a checkpoint that completed, or
that the counters already report; it is not proof that none started.
Every write cell has its own new copy of the template, so each member
of a pair starts from the same table state.
After each write cell (after_write()), once its server has stopped:
  pgbouncer's server connections to the copy are closed (KILL, then
  RESUME), and the counters are read only when no backend is left on the
  copy (a backend writes its counts before it leaves pg_stat_activity):
  per table, row inserts, row updates and HOT updates (and the HOT share);
  the copy's lifecycle WAL (pg_current_wal_insert_lsn() after the
  CHECKPOINT before the server started, and after the barrier: start-up, schema statements, warm-up,
  checks and stop included) and its successful writes (the primed alert
  and its duplicates: duplicate_count + 1), kept apart from the timed
  window above. The alerts, metrics and keys tables must each have
  counters; a missing one is incomplete evidence.
A failed barrier or measurement makes the cell invalid.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

# `python3 -I` leaves the script's directory off the path.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import pg_suite  # noqa: E402

SEED_TEMPLATE = "bench_seed_pgtune"
# The tables a write cell must have counters for (stock Alerta and `safe`
# write each on every duplicate receive).
COUNTED = ("alerts", "keys", "metrics")
PLAN_DB = "bench_pgtune_plan"
ROWS_ALLOWED = (1000, 100000)


def rows():
    """PGTUNE_ROWS: the alerts of every pgtune copy (default 100000)."""
    value = os.environ.get("PGTUNE_ROWS", "100000")
    if not value.isdigit() or int(value) not in ROWS_ALLOWED:
        raise SystemExit(f"PGTUNE_ROWS must be one of {ROWS_ALLOWED}, not {value!r}")
    return int(value)


def arms(bc):
    n = bc.VCPUS
    return [f"alerta-{n}x4-pgb-pgt", f"alerta-{n}x4-pgb-idx-pgt",
            f"alerta-{n}x4-pgb-safe-pgt", f"alerta-{n}x4-pgb-safe-idx-pgt"]


def is_arm(arm):
    return isinstance(arm, str) and arm.startswith("alerta-") and "pgt" in arm.split("-")[2:]


def template(key):
    """The template of one copy (a key of bench_compare's `dbs`)."""
    return SEED_TEMPLATE if is_arm(key[0]) or key[0] == "pgtune-plan" else "bench_seed"


def bulk_sql(extra, seeded):
    """The insert of `extra` alerts made from the `seeded` ones (see the
    file header)."""
    return f"""
INSERT INTO alerts (id, resource, event, environment, severity, correlate, status, service, "group",
                    value, text, tags, attributes, origin, type, create_time, timeout, raw_data,
                    customer, duplicate_count, repeat, previous_severity, trend_indication,
                    receive_time, last_receive_id, last_receive_time, update_time, history)
SELECT n.id, 'bulk-' || n.g, s.event, s.environment, s.severity, s.correlate, s.status, s.service,
       s."group", s.value, s.text, s.tags, s.attributes, s.origin, s.type, s.create_time - n.shift,
       s.timeout, s.raw_data, s.customer, s.duplicate_count, s.repeat, s.previous_severity,
       s.trend_indication, s.receive_time - n.shift, n.id, s.last_receive_time - n.shift,
       s.update_time - n.shift,
       ARRAY(SELECT ROW(n.id, h.event, h.severity, h.status, h.value, h.text, h.type,
                        h.update_time - n.shift, h."user", h.timeout)::history
             FROM unnest(s.history) WITH ORDINALITY AS h(id, event, severity, status, value, text, type,
                                                        update_time, "user", timeout, ord)
             ORDER BY h.ord)
FROM (SELECT g, gen_random_uuid()::text AS id, interval '1 day' + g * interval '1 second' AS shift
      FROM generate_series(1, {int(extra)}) AS g) AS n
JOIN alerts AS s ON s.resource = 'host-' || lpad(((n.g - 1) % {int(seeded)})::text, 4, '0')"""


def seed(bc, record):
    """bench_seed_pgtune, from bench_seed (no connection may be open on it)."""
    wanted = rows()
    for attempt in range(10):
        try:
            bc.psql("postgres", f"CREATE DATABASE {SEED_TEMPLATE} TEMPLATE bench_seed")
            break
        except subprocess.CalledProcessError:
            if attempt == 9:
                raise
            time.sleep(1)
    seeded = record["seed"]["alerts"]
    if wanted > seeded:
        bc.psql(SEED_TEMPLATE, bulk_sql(wanted - seeded, seeded))
    bc.psql(SEED_TEMPLATE, "VACUUM (ANALYZE)")
    shape = bc.sql_json(SEED_TEMPLATE, """SELECT json_build_object(
        'alerts', count(*),
        'uuid_ids', count(*) FILTER (WHERE id ~ '^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'),
        'own_last_receive_id', count(*) FILTER (WHERE last_receive_id = id),
        'history_ids_own', count(*) FILTER (WHERE COALESCE((SELECT bool_and(h.id = alerts.id)
                                                             FROM unnest(history) h), true)),
        'history_entries', sum(cardinality(history)),
        'newest_50_seeded', (SELECT count(*) FROM (SELECT resource FROM alerts
                              ORDER BY last_receive_time DESC LIMIT 50) t WHERE resource LIKE 'host-%'),
        'table_bytes', pg_total_relation_size('alerts')) FROM alerts""")
    record["pgtune"] = {"rows": wanted, "seed_alerts": shape["alerts"], "shape": shape}
    problems = []
    if shape["alerts"] != wanted:
        problems.append(f"{shape['alerts']} alerts, {wanted} wanted")
    for field in ("uuid_ids", "own_last_receive_id", "history_ids_own"):
        if shape[field] != wanted:
            problems.append(f"{field}: {shape[field]} of {wanted}")
    if shape["newest_50_seeded"] != 50:
        problems.append(f"the 50 newest alerts include {50 - shape['newest_50_seeded']} bulk alerts")
    record["pgtune"]["seed_problems"] = problems
    if problems:
        raise RuntimeError(f"bench_seed_pgtune: {problems}")


def database(bc, db):
    """Collation and indexes of one copy."""
    info = bc.sql_json("postgres", f"SELECT row_to_json(d) FROM pg_database d WHERE datname = '{db}'")
    id_collation = bc.psql(db, "SELECT c.collname FROM pg_attribute a JOIN pg_collation c "
                               "ON c.oid = a.attcollation WHERE a.attrelid = 'alerts'::regclass "
                               "AND a.attname = 'id'")
    indexes = bc.psql(db, "SELECT indexdef FROM pg_indexes WHERE tablename = 'alerts' ORDER BY indexname")
    return {"collate": info.get("datcollate"), "ctype": info.get("datctype"),
            "locale_provider": info.get("datlocprovider"),
            "locale": info.get("datlocale", info.get("daticulocale")),
            "id_collation": id_collation, "alerts_indexes": indexes.split("\n") if indexes else []}


def probes(bc, db):
    """Three predicate probes on an existing seeded alert: UPDATEs with the
    WHERE clauses of Alerta's update_tags(), update_attributes() and
    dedup_alert(), which set a column to itself or only the counter. Not
    Alerta's statements (those bind new values and, for the duplicate,
    set many more columns); only their plans are compared."""
    row = bc.sql_json(db, "SELECT row_to_json(a) FROM (SELECT id, environment, resource, event, severity "
                          "FROM alerts WHERE resource = 'host-0001') a")
    by_id = f"WHERE id = '{row['id']}' OR id LIKE '{row['id']}%'"
    return {"tags_predicate_probe": f"UPDATE alerts SET tags = tags {by_id} RETURNING *",
            "attributes_predicate_probe": f"UPDATE alerts SET attributes = attributes {by_id} "
                                          "RETURNING attributes",
            "dedup_predicate_probe": (f"UPDATE alerts SET duplicate_count = duplicate_count + 1 "
                            f"WHERE environment = '{row['environment']}' AND resource = '{row['resource']}' "
                            f"AND event = '{row['event']}' AND severity = '{row['severity']}' "
                            "AND customer IS NULL RETURNING *")}


def plan_nodes(top):
    nodes, todo = [], [top]
    while todo:
        node = todo.pop()
        nodes.append({"node": node["Node Type"], "index": node.get("Index Name"),
                      "condition": node.get("Index Cond") or node.get("Recheck Cond") or node.get("Filter"),
                      "rows": node.get("Actual Rows")})
        todo += node.get("Plans", [])
    return nodes


def plan(bc, db, statement):
    out = bc.sql_json(db, f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {statement}")[0]
    nodes = plan_nodes(out["Plan"])
    return {"nodes": nodes, "sequential_scan": any(n["node"] == "Seq Scan" for n in nodes),
            "indexes": sorted({n["index"] for n in nodes if n["index"]}),
            "execution_ms": out.get("Execution Time"), "planning_ms": out.get("Planning Time"),
            "shared_hit": out["Plan"].get("Shared Hit Blocks"), "shared_read": out["Plan"].get("Shared Read Blocks")}


def prepare(bc, record, dbs):
    """The records of the header, before pgbouncer starts."""
    rec = record["pgtune"]
    rec["databases"] = {name: database(bc, name) for key, name in dbs.items() if template(key) == SEED_TEMPLATE}
    texts = probes(bc, PLAN_DB)
    rec["probes"] = texts
    rec["plans"] = {"without_index": {k: plan(bc, PLAN_DB, s) for k, s in texts.items()}}
    bc.psql(PLAN_DB, bc.FAST_INDEX)
    bc.psql(PLAN_DB, "ANALYZE alerts")
    rec["plans"]["with_index"] = {k: plan(bc, PLAN_DB, s) for k, s in texts.items()}
    rec["cells"] = []
    rec["order"] = []


def before_write(bc):
    """Before a write cell's server starts: a CHECKPOINT (see the header),
    then the WAL insert position where the cell's lifecycle begins."""
    bc.psql("postgres", "CHECKPOINT")
    return bc.psql("postgres", "SELECT pg_current_wal_insert_lsn()")


def wal_mark(bc):
    """One read: the WAL insert position (not the write position, which
    lags it), the last checkpoint's redo position and the checkpoints
    counted so far."""
    return bc.sql_json("postgres", "SELECT json_build_object('lsn', pg_current_wal_insert_lsn()::text, "
                                   "'redo_lsn', (SELECT redo_lsn::text FROM pg_control_checkpoint()), "
                                   "'checkpoints', (SELECT num_timed + num_requested FROM pg_stat_checkpointer))")


def lsn_int(lsn):
    high, low = lsn.split("/")
    return (int(high, 16) << 32) + int(low, 16)


def timed_window(persisted):
    """The timed window of one write cell, from the two marks and the two
    duplicate counts bench_compare.cell() took at the same points (pure).
    None where a part is missing."""
    before, after = persisted.get("wal_before"), persisted.get("wal_after")
    d0, d1 = persisted.get("dup_before"), persisted.get("dup_after")
    marks = isinstance(before, dict) and isinstance(after, dict)
    wal = lsn_int(after["lsn"]) - lsn_int(before["lsn"]) if marks else None
    writes = d1 - d0 if isinstance(d0, int) and isinstance(d1, int) else None
    checkpoint = ((before["redo_lsn"], before["checkpoints"]) != (after["redo_lsn"], after["checkpoints"])
                  if marks else None)
    return {"wal_bytes": wal, "writes": writes, "checkpoint_in_window": checkpoint,
            "wal_bytes_per_write": round(wal / writes, 1) if wal is not None and writes else None}


def pair_of(arm):
    """(pair, member) of a pgtune arm."""
    options = arm.split("-")[2:]
    return ("safe" if "safe" in options else "stock", "index" if "idx" in options else "control")


def order(bc, rep):
    """The four arms in the order of one repetition (see the header).
    pg_suite.pair_order() speaks of versions 18 and 19: here 18 is the
    control and 19 the index arm of a pair."""
    by = {pair_of(arm): arm for arm in arms(bc)}
    return [{"rep": rep, "position": position, "pair": pair, "member": "control" if v == 18 else "index",
             "arm": by[(pair, "control" if v == 18 else "index")], "first_of_its_pair": position % 2 == 0}
            for position, (v, pair) in enumerate(pg_suite.pair_order(["stock", "safe"], rep))]


def change(new, old):
    return round(100 * (new / old - 1), 2) if old and new is not None else None


def pair_changes(record):
    """Per pair, case, clients and repetition: the index arm against its
    control in that repetition (pure). A row has a `problem` and no change
    when a cell is absent or not valid; the WAL change is None when a
    checkpoint fell in either timed window or a part is missing."""
    cells = {(c["arm"], c["case"], c["c"], c["rep"]): c for c in record["cells"] if is_arm(c["arm"])}
    first = {(o["arm"], o["rep"]): o["first_of_its_pair"] for o in record["pgtune"].get("order", [])}
    out = []
    for (arm, case, conc, rep), index in sorted(cells.items()):
        pair, member = pair_of(arm)
        if member != "index":
            continue
        control_arm = "-".join(part for part in arm.split("-") if part != "idx")
        control = cells.get((control_arm, case, conc, rep))
        row = {"pair": pair, "case": case, "c": conc, "rep": rep, "control": control_arm, "index": arm,
               "index_ran_first": first.get((arm, rep))}
        if control is None or not (control.get("valid") and index.get("valid")):
            row["problem"] = "the control cell is absent" if control is None else "a cell of the pair is not valid"
            out.append(row)
            continue
        for field in ("rps", "p50_ms", "p99_ms"):
            row[f"control_{field}"], row[f"index_{field}"] = control[field], index[field]
            row[f"{field}_change_pct"] = change(index[field], control[field])
        if case == "write-dup":
            tc = (control.get("pgtune") or {}).get("timed") or {}
            ti = (index.get("pgtune") or {}).get("timed") or {}
            clean = tc.get("checkpoint_in_window") is False and ti.get("checkpoint_in_window") is False
            row["control_timed_wal_per_write"] = tc.get("wal_bytes_per_write")
            row["index_timed_wal_per_write"] = ti.get("wal_bytes_per_write")
            row["timed_wal_per_write_change_pct"] = (change(ti.get("wal_bytes_per_write"),
                                                            tc.get("wal_bytes_per_write")) if clean else None)
        out.append(row)
    return out


def pgbouncer_admin(bc, command):
    env = dict(os.environ, PGPASSWORD=bc.PG["password"])
    return subprocess.run([str(bc.PG_BIN / "psql"), "-h", str(bc.WORK / "pgb"), "-p", str(bc.PGB_PORT),
                           "-U", bc.PG["user"], "-d", "pgbouncer", "-tA", "-c", command],
                          env=env, capture_output=True, text=True, check=True).stdout


def quiet(bc, db, tenths=100):
    """True once no backend is left on `db`."""
    for _ in range(tenths):
        if bc.psql("postgres", f"SELECT count(*) FROM pg_stat_activity WHERE datname = '{db}'") == "0":
            return True
        time.sleep(0.1)
    return False


def measure(tables, lifecycle_wal, lifecycle_writes, timed):
    """Per-table HOT shares, the timed window and the lifecycle, each with
    its own WAL and its own writes (pure). `missing_counters` names the
    COUNTED tables with no counters."""
    per_table = {t["table"]: {**t, "hot_share": round(t["hot"] / t["upd"], 3) if t["upd"] else None}
                 for t in tables}
    return {"tables": per_table, "missing_counters": [t for t in COUNTED if t not in per_table],
            "timed": timed,
            "lifecycle": {"wal_bytes": lifecycle_wal, "writes": lifecycle_writes,
                          "wal_bytes_per_write": (round(lifecycle_wal / lifecycle_writes, 1)
                                                  if lifecycle_writes else None)}}


def after_write(bc, record, arm, rep, conc, db, lsn_before):
    """The counters of one write copy, once its server has stopped."""
    cell = next((c for c in reversed(record["cells"])
                 if (c["arm"], c["case"], c["c"], c["rep"]) == (arm, "write-dup", conc, rep)), None)
    problems = [] if cell else ["the write cell is missing"]
    cell = cell or {}
    try:
        pgbouncer_admin(bc, f"KILL {db}")
        pgbouncer_admin(bc, f"RESUME {db}")
    except subprocess.CalledProcessError as e:
        problems.append(f"pgbouncer KILL/RESUME {db}: {e.stderr.strip()[:200]}")
    if not quiet(bc, db):
        problems.append(f"a backend is still on {db}")
    tables = bc.sql_json(db, "SELECT COALESCE(json_agg(json_build_object('table', relname, 'ins', n_tup_ins, "
                             "'upd', n_tup_upd, 'hot', n_tup_hot_upd, 'dead', n_dead_tup) ORDER BY relname), "
                             "'[]') FROM pg_stat_user_tables WHERE n_tup_ins + n_tup_upd > 0")
    wal = int(bc.psql("postgres", f"SELECT pg_wal_lsn_diff(pg_current_wal_insert_lsn(), '{lsn_before}')"))
    persisted = cell.get("persisted") or {}
    after = persisted.get("dup_after")
    writes = after + 1 if isinstance(after, int) else 0
    result = {"arm": arm, "c": conc, "rep": rep, "db": db,
              **measure(tables, wal, writes, timed_window(persisted)), "problems": problems}
    if not writes:
        problems.append("no successful write is known")
    if result["timed"]["wal_bytes"] is None or not result["timed"]["writes"]:
        problems.append("the timed window's WAL or writes are not known")
    for table in result["missing_counters"]:
        problems.append(f"no counters for the {table} table (incomplete evidence)")
    cell["pgtune"] = result
    if problems:
        cell["valid"] = False
    record["pgtune"]["cells"].append(result)


def summary(record):
    rec = record["pgtune"]
    lines = ["", f"## pgtune step 1: the ID prefix index ({rec['rows']} alerts per copy)", ""]
    first = next(iter(rec.get("databases", {}).values()), {})
    lines.append(f"Collation {first.get('collate')!r}, ctype {first.get('ctype')!r}, provider "
                 f"{first.get('locale_provider')!r}, alerts.id collation {first.get('id_collation')!r}.")
    lines += ["", "Predicate probes (not Alerta's statements; the plan of each WHERE clause only):", "",
              "| probe | index | seq scan | indexes |", "|---|---|---|---|"]
    for which, plans in rec.get("plans", {}).items():
        for name, p in plans.items():
            lines.append(f"| {name} | {which.replace('_', ' ')} | {p['sequential_scan']} | "
                         f"{', '.join(p['indexes']) or '-'} |")
    lines += ["", "Write cells. Timed: the timed window's WAL over that window's saved duplicates. "
                  "Lifecycle: start-up to stop, over all saved writes. WAL is the cluster's.", "",
              "| arm | c | rep | timed WAL bytes per write | checkpoint in window | lifecycle WAL bytes per write "
              "| alerts HOT share | alerts row updates | problems |", "|---|---|---|---|---|---|---|---|---|"]
    for c in rec.get("cells", []):
        alerts = c["tables"].get("alerts", {})
        lines.append(f"| {c['arm']} | {c['c']} | {c['rep']} | {c['timed']['wal_bytes_per_write']} | "
                     f"{c['timed']['checkpoint_in_window']} | {c['lifecycle']['wal_bytes_per_write']} | "
                     f"{alerts.get('hot_share')} | {alerts.get('upd')} | {'; '.join(c['problems']) or '-'} |")
    lines += ["", "Index arm against its control, in the same repetition (order: " + "; ".join(
                  f"r{r}: " + ", ".join(o["arm"] for o in rec.get("order", []) if o["rep"] == r)
                  for r in sorted({o["rep"] for o in rec.get("order", [])})) + ").", "",
              "| pair | case | c | rep | index ran first | control/s | index/s | change | p50 change "
              "| timed WAL per write change |", "|---|---|---|---|---|---|---|---|---|---|"]
    for r in rec.get("pair_changes", []):
        if "problem" in r:
            lines.append(f"| {r['pair']} | {r['case']} | {r['c']} | {r['rep']} | {r['index_ran_first']} | "
                         f"{r['problem']} | | | | |")
            continue
        lines.append(f"| {r['pair']} | {r['case']} | {r['c']} | {r['rep']} | {r['index_ran_first']} | "
                     f"{r['control_rps']:.1f} | {r['index_rps']:.1f} | {r['rps_change_pct']:+.1f}% | "
                     f"{r['p50_ms_change_pct']:+.1f}% | {r.get('timed_wal_per_write_change_pct', '-')} |")
    return lines


def self_test():
    sql = bulk_sql(99000, 1000)
    assert "generate_series(1, 99000)" in sql and "% 1000" in sql and "ORDER BY h.ord" in sql
    assert is_arm("alerta-4x4-pgb-safe-idx-pgt") and not is_arm("alerta-4x4-pgb") and not is_arm("alrust")
    assert template(("alerta-4x4-pgb-pgt", "read")) == SEED_TEMPLATE
    assert template(("pgtune-plan",)) == SEED_TEMPLATE and template(("alerta-4x4-pgb", "read")) == "bench_seed"
    # The timed window: its own WAL over its own writes, apart from the lifecycle.
    assert lsn_int("1/A0") - lsn_int("0/FFFFFF00") == 0x1A0
    mark = {"lsn": "0/1000000", "redo_lsn": "0/900000", "checkpoints": 3}
    later = dict(mark, lsn="0/1014000")
    persisted = {"dup_before": 5, "dup_after": 25, "wal_before": mark, "wal_after": later}
    t = timed_window(persisted)
    assert t == {"wal_bytes": 81920, "writes": 20, "checkpoint_in_window": False, "wal_bytes_per_write": 4096.0}
    assert timed_window(dict(persisted, wal_after=dict(later, checkpoints=4)))["checkpoint_in_window"] is True
    assert timed_window(dict(persisted, wal_after=dict(later, redo_lsn="0/1001000")))["checkpoint_in_window"]
    assert timed_window({"dup_before": 5, "dup_after": 25})["wal_bytes"] is None
    assert timed_window(dict(persisted, dup_after=None))["wal_bytes_per_write"] is None
    assert timed_window(dict(persisted, dup_after=5))["wal_bytes_per_write"] is None
    m = measure([{"table": "alerts", "ins": 1, "upd": 40, "hot": 30, "dead": 2},
                 {"table": "keys", "ins": 0, "upd": 0, "hot": 0, "dead": 0}], 500000, 26, t)
    assert m["tables"]["alerts"]["hot_share"] == 0.75 and m["tables"]["keys"]["hot_share"] is None
    assert m["missing_counters"] == ["metrics"] and m["timed"]["wal_bytes_per_write"] == 4096.0
    # A busier arm does not look cheaper: the lifecycle figure is its own number.
    assert m["lifecycle"] == {"wal_bytes": 500000, "writes": 26, "wal_bytes_per_write": 19230.8}
    assert measure([], 10, 0, t)["lifecycle"]["wal_bytes_per_write"] is None
    assert measure([], 10, 0, t)["missing_counters"] == list(COUNTED)

    # Order: two pairs; over four repetitions each member is first twice,
    # each pair leads twice, and every repetition holds each arm once.
    class BC:
        VCPUS = 4
    names = arms(BC)
    assert [pair_of(a) for a in names] == [("stock", "control"), ("stock", "index"),
                                           ("safe", "control"), ("safe", "index")]
    firsts, leads = {a: 0 for a in names}, {"stock": 0, "safe": 0}
    for r in range(4):
        o = order(BC, r)
        assert sorted(x["arm"] for x in o) == sorted(names)
        assert [x["pair"] for x in o][0::2] == [x["pair"] for x in o][1::2]  # a pair's members are adjacent
        assert all(pair_of(x["arm"]) == (x["pair"], x["member"]) for x in o)
        leads[o[0]["pair"]] += 1
        for x in o:
            firsts[x["arm"]] += x["first_of_its_pair"]
    assert set(firsts.values()) == {2} and leads == {"stock": 2, "safe": 2}, (firsts, leads)
    three = [x for r in range(3) for x in order(BC, r)]
    assert all(sum(x["first_of_its_pair"] for x in three if x["arm"] == a) in (1, 2) for a in names)

    # Pair changes: per repetition, index against its control.
    def one(arm, case, rps, rep=0, valid=True, wal=None, checkpoint=False):
        c = {"arm": arm, "case": case, "c": 8, "rep": rep, "rps": rps, "p50_ms": 1000 / rps,
             "p99_ms": 2000 / rps, "valid": valid}
        if wal:
            c["pgtune"] = {"timed": {"wal_bytes_per_write": wal, "checkpoint_in_window": checkpoint}}
        return c
    rec = {"pgtune": {"order": order(BC, 0)},
           "cells": [one(names[0], "write-dup", 100.0, wal=4000.0), one(names[1], "write-dup", 150.0, wal=3000.0),
                     one(names[2], "write-dup", 200.0, wal=4000.0),
                     one(names[3], "write-dup", 100.0, wal=3000.0, checkpoint=True),
                     one(names[0], "list", 50.0, rep=1), one(names[1], "list", 55.0, rep=1, valid=False),
                     one(names[3], "count", 10.0, rep=2), one("alerta-4x4-pgb", "list", 1.0)]}
    rows_ = {(r["pair"], r["case"], r["rep"]): r for r in pair_changes(rec)}
    assert len(rows_) == 4
    stock = rows_[("stock", "write-dup", 0)]
    assert stock["rps_change_pct"] == 50.0 and stock["timed_wal_per_write_change_pct"] == -25.0
    assert stock["index_ran_first"] is False and rows_[("safe", "write-dup", 0)]["index_ran_first"] is True
    safe = rows_[("safe", "write-dup", 0)]
    assert safe["rps_change_pct"] == -50.0 and safe["timed_wal_per_write_change_pct"] is None
    assert safe["index_timed_wal_per_write"] == 3000.0
    assert rows_[("stock", "list", 1)]["problem"] == "a cell of the pair is not valid"
    assert rows_[("safe", "count", 2)]["problem"] == "the control cell is absent"
    record = {"pgtune": {"rows": 1000, "order": order(BC, 0), "pair_changes": pair_changes(rec),
                         "cells": [{"arm": names[0], "c": 8, "rep": 0, **m, "problems": []}]}}
    text = "\n".join(summary(record))
    assert "not Alerta's statements" in text and "+50.0%" in text and "execution ms" not in text
    os.environ["PGTUNE_ROWS"] = "1000"
    assert rows() == 1000
    os.environ["PGTUNE_ROWS"] = "5"
    try:
        rows()
        raise AssertionError("PGTUNE_ROWS=5 accepted")
    except SystemExit:
        pass
    print("pgtune_suite self-test: passed")


if __name__ == "__main__":
    self_test()
