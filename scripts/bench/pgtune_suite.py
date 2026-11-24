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
  plans     on a throwaway copy (bench_pgtune_plan), EXPLAIN (ANALYZE,
            BUFFERS, FORMAT JSON) of Alerta's three write statements of a
            duplicate receive, with the literal values psycopg2 sends: the
            tag and attribute updates by id (an existing alert's id), and
            the dedup UPDATE; first without FAST_INDEX, then with it.
            The statements run (the copy is not used again).
After each write cell (after_write()), once its server has stopped:
  pgbouncer's server connections to the copy are closed (KILL, then
  RESUME), and the counters are read only when no backend is left on the
  copy (a backend writes its counts before it leaves pg_stat_activity):
  per table, row inserts, row updates and HOT updates (and the HOT share);
  the copy's WAL (pg_current_wal_insert_lsn() before the server started
  and after the barrier; the cluster has no other load then); and the
  successful writes of the copy (the primed alert and its duplicates:
  duplicate_count + 1), so WAL bytes per successful write.
A failed barrier or measurement makes the cell invalid.
"""

import json
import os
import subprocess
import time

SEED_TEMPLATE = "bench_seed_pgtune"
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


def statements(bc, db):
    """Alerta's three write statements of a duplicate receive, as psycopg2
    sends them (literal values), on an existing seeded alert."""
    row = bc.sql_json(db, "SELECT row_to_json(a) FROM (SELECT id, environment, resource, event, severity "
                          "FROM alerts WHERE resource = 'host-0001') a")
    by_id = f"WHERE id = '{row['id']}' OR id LIKE '{row['id']}%'"
    return {"update_tags": f"UPDATE alerts SET tags = tags {by_id} RETURNING *",
            "update_attributes": f"UPDATE alerts SET attributes = attributes {by_id} RETURNING attributes",
            "dedup_alert": (f"UPDATE alerts SET duplicate_count = duplicate_count + 1 "
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
    texts = statements(bc, PLAN_DB)
    rec["statements"] = texts
    rec["plans"] = {"without_index": {k: plan(bc, PLAN_DB, s) for k, s in texts.items()}}
    bc.psql(PLAN_DB, bc.FAST_INDEX)
    bc.psql(PLAN_DB, "ANALYZE alerts")
    rec["plans"]["with_index"] = {k: plan(bc, PLAN_DB, s) for k, s in texts.items()}
    rec["cells"] = []


def wal_lsn(bc):
    return bc.psql("postgres", "SELECT pg_current_wal_insert_lsn()")


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


def measure(tables, wal_bytes, writes):
    """Per-table HOT shares and WAL per successful write (pure)."""
    per_table = {t["table"]: {**t, "hot_share": round(t["hot"] / t["upd"], 3) if t["upd"] else None}
                 for t in tables}
    return {"tables": per_table, "wal_bytes": wal_bytes, "successful_writes": writes,
            "wal_bytes_per_write": round(wal_bytes / writes, 1) if writes else None}


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
    after = (cell.get("persisted") or {}).get("dup_after")
    writes = after + 1 if isinstance(after, int) else 0
    result = {"arm": arm, "c": conc, "rep": rep, "db": db, **measure(tables, wal, writes), "problems": problems}
    if not writes:
        problems.append("no successful write is known")
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
    lines += ["", "| statement | index | seq scan | indexes | execution ms |", "|---|---|---|---|---|"]
    for which, plans in rec.get("plans", {}).items():
        for name, p in plans.items():
            lines.append(f"| {name} | {which.replace('_', ' ')} | {p['sequential_scan']} | "
                         f"{', '.join(p['indexes']) or '-'} | {p['execution_ms']} |")
    lines += ["", "| arm | c | rep | WAL bytes per write | alerts HOT share | alerts row updates | problems |",
              "|---|---|---|---|---|---|---|"]
    for c in rec.get("cells", []):
        alerts = c["tables"].get("alerts", {})
        lines.append(f"| {c['arm']} | {c['c']} | {c['rep']} | {c['wal_bytes_per_write']} | "
                     f"{alerts.get('hot_share')} | {alerts.get('upd')} | {'; '.join(c['problems']) or '-'} |")
    return lines


def self_test():
    sql = bulk_sql(99000, 1000)
    assert "generate_series(1, 99000)" in sql and "% 1000" in sql and "ORDER BY h.ord" in sql
    assert is_arm("alerta-4x4-pgb-safe-idx-pgt") and not is_arm("alerta-4x4-pgb") and not is_arm("alrust")
    assert template(("alerta-4x4-pgb-pgt", "read")) == SEED_TEMPLATE
    assert template(("pgtune-plan",)) == SEED_TEMPLATE and template(("alerta-4x4-pgb", "read")) == "bench_seed"
    m = measure([{"table": "alerts", "ins": 1, "upd": 40, "hot": 30, "dead": 2},
                 {"table": "keys", "ins": 0, "upd": 0, "hot": 0, "dead": 0}], 81920, 20)
    assert m["tables"]["alerts"]["hot_share"] == 0.75 and m["tables"]["keys"]["hot_share"] is None
    assert m["wal_bytes_per_write"] == 4096.0 and measure([], 10, 0)["wal_bytes_per_write"] is None
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
