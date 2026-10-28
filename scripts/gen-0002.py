"""Generate crates/alrust-store-pg/migrations/0002_reference_layout.sql.

Run from the repository root: python3 scripts/gen-0002.py

The reference blocks are spliced verbatim from the pinned
reference/alerta/alerta/sql/schema.sql by line range. The verification and
alpha-shape queries, and the expected alpha shape, come from
crates/alrust-store-pg/src/verify/. Tests check that 0002 contains each of
them verbatim, so this script and the migration cannot drift apart.

Once 0002 has been applied anywhere it must never change (SQLx checksums).
"""
import sys

ref = open("reference/alerta/alerta/sql/schema.sql").read().split("\n")


def L(a, b):  # 1-based inclusive line range, verbatim
    return "\n".join(ref[a - 1:b])


assert ref[1] == "DO $$" and ref[28] == "END$$;"
assert ref[31].startswith("CREATE TABLE IF NOT EXISTS alerts (")
assert ref[61].startswith("ALTER TABLE alerts ADD COLUMN IF NOT EXISTS update_time")
assert ref[64].startswith("CREATE TABLE IF NOT EXISTS notes (")
assert ref[105].startswith("ALTER TABLE customers DROP CONSTRAINT")
assert ref[108].startswith("CREATE TABLE IF NOT EXISTS heartbeats (")
assert ref[119].startswith("ALTER TABLE heartbeats ADD COLUMN IF NOT EXISTS attributes")
assert ref[135].startswith("CREATE TABLE IF NOT EXISTS metrics (")
assert ref[191] == ");"
assert ref[194].startswith("CREATE UNIQUE INDEX IF NOT EXISTS env_res_evt_cust_key")
assert ref[197].startswith("CREATE UNIQUE INDEX IF NOT EXISTS org_cust_key")

verify_dir = "crates/alrust-store-pg/src/verify/"
V = {name: open(verify_dir + name + ".sql").read().strip()
     for name in ("alerts", "history", "heartbeats", "keys", "alpha_shape")}
ALPHA_SHAPE = open(verify_dir + "alpha_shape.expected").read().rstrip("\n")
assert "$shape$" not in ALPHA_SHAPE

# ADR-0005 decision 8 (b), DECISIONS Y38: the same expression as
# alrust_store_pg::VALUE_TEXT; the static test checks 0002 embeds both uses.
VALUE_TEXT = "CASE jsonb_typeof({v}) WHEN 'string' THEN ({v}) #>> '{{}}' WHEN 'null' THEN NULL ELSE ({v})::text END"
TABLES = "('public.alerts'::regclass, 'public.heartbeats'::regclass, 'public.keys'::regclass)"
I32 = "BETWEEN -2147483648 AND 2147483647"

sql = f"""-- 0002: convert the alpha schema (0001) to the pinned reference layout,
-- exactly (ADR-0005 decision 3). The reference statements below are copied
-- verbatim from reference/alerta/alerta/sql/schema.sql at f4312727, and the
-- verification queries verbatim from src/verify/; unit tests check both.
-- SQLx runs this file in one transaction, so every RAISE rolls all of it
-- back and the alpha schema and rows stay as they were.

-- 3.0: lock, then refuse any layout this conversion does not support.
SET LOCAL lock_timeout = '30s';
LOCK TABLE alerts, heartbeats, keys IN ACCESS EXCLUSIVE MODE;

DO $alrust$
DECLARE
    problems text;
BEGIN
    SELECT string_agg(p, '; ' ORDER BY p) INTO problems FROM (
        SELECT format('foreign key %s on %s references %s', c.conname, c.conrelid::regclass, c.confrelid::regclass) AS p
          FROM pg_constraint c
         WHERE c.contype = 'f' AND c.confrelid IN {TABLES}
        UNION
        SELECT format('%s depends on %s', r.ev_class::regclass, d.refobjid::regclass)
          FROM pg_depend d JOIN pg_rewrite r ON r.oid = d.objid
         WHERE d.classid = 'pg_rewrite'::regclass AND d.refclassid = 'pg_class'::regclass
           AND d.refobjid IN {TABLES} AND r.ev_class <> d.refobjid
        UNION
        SELECT format('trigger %s on %s', t.tgname, t.tgrelid::regclass)
          FROM pg_trigger t WHERE NOT t.tgisinternal AND t.tgrelid IN {TABLES}
        UNION
        SELECT format('owned sequence %s depends on %s.%I', s.oid::regclass, d.refobjid::regclass, a.attname)
          FROM pg_depend d
          JOIN pg_class s ON s.oid = d.objid AND s.relkind = 'S'
          JOIN pg_attribute a ON a.attrelid = d.refobjid AND a.attnum = d.refobjsubid
         WHERE d.classid = 'pg_class'::regclass AND d.refclassid = 'pg_class'::regclass
           AND d.objsubid = 0 AND d.refobjsubid > 0 AND d.deptype IN ('a', 'i')
           AND d.refobjid IN ('public.alerts'::regclass, 'public.heartbeats'::regclass)
        UNION
        SELECT format('row-level security on %s', c.oid::regclass)
          FROM pg_class c WHERE c.oid IN {TABLES} AND (c.relrowsecurity OR c.relforcerowsecurity)
        UNION
        SELECT format('policy %s on %s', p.polname, p.polrelid::regclass)
          FROM pg_policy p WHERE p.polrelid IN {TABLES}
        UNION
        SELECT format('publication %s includes %s', b.pubname, r.prrelid::regclass)
          FROM pg_publication_rel r JOIN pg_publication b ON b.oid = r.prpubid WHERE r.prrelid IN {TABLES}
        UNION
        SELECT format('publication %s includes all tables', pubname) FROM pg_publication WHERE puballtables
        UNION
        SELECT format('publication %s includes schema %s', b.pubname, n.nspname)
          FROM pg_publication_namespace pn
          JOIN pg_publication b ON b.oid = pn.pnpubid
          JOIN pg_namespace n ON n.oid = pn.pnnspid
         WHERE pn.pnnspid = 'public'::regnamespace
        UNION
        SELECT format('inheritance between %s and %s', i.inhrelid::regclass, i.inhparent::regclass)
          FROM pg_inherits i WHERE i.inhrelid IN {TABLES} OR i.inhparent IN {TABLES}
        UNION
        SELECT format('%s is partitioned or a partition', c.oid::regclass)
          FROM pg_class c WHERE c.oid IN {TABLES} AND (c.relkind = 'p' OR c.relispartition)
        UNION
        SELECT format('extended statistics %s on %s', s.stxname, s.stxrelid::regclass)
          FROM pg_statistic_ext s WHERE s.stxrelid IN {TABLES}
        UNION
        SELECT format('non-default privileges on %s', c.oid::regclass)
          FROM pg_class c WHERE c.oid IN {TABLES} AND c.relacl IS NOT NULL
        UNION
        SELECT format('column privileges on %s.%s', a.attrelid::regclass, a.attname)
          FROM pg_attribute a WHERE a.attrelid IN {TABLES} AND a.attacl IS NOT NULL
        UNION
        SELECT format('%s is owned by %s, not %s', c.oid::regclass, pg_get_userbyid(c.relowner), current_user)
          FROM pg_class c
         WHERE c.oid IN {TABLES} AND c.relowner <> (SELECT oid FROM pg_roles WHERE rolname = current_user)
        UNION
        SELECT format('default privileges of %s on new tables', pg_get_userbyid(d.defaclrole))
          FROM pg_default_acl d
         WHERE d.defaclobjtype = 'r' AND d.defaclrole = (SELECT oid FROM pg_roles WHERE rolname = current_user)
           AND (d.defaclnamespace = 0 OR d.defaclnamespace = 'public'::regnamespace)
        UNION
        SELECT format('%s already exists', c.oid::regclass)
          FROM pg_class c
         WHERE c.relnamespace = 'public'::regnamespace
           AND c.relname IN ('notes', 'blackouts', 'customers', 'metrics', 'perms', 'users', 'groups',
                             'alerts_alpha', 'heartbeats_alpha', 'env_res_evt_cust_key', 'org_cust_key')
        UNION
        SELECT 'type history already exists'
          FROM pg_type t WHERE t.typname = 'history' AND t.typnamespace = 'public'::regnamespace
    ) x;
    IF problems IS NOT NULL THEN
        RAISE EXCEPTION 'alrust 0002: unsupported database layout; nothing was changed: %', problems;
    END IF;
END
$alrust$;

-- 3.0b: the three tables must be exactly as 0001 created them (columns,
-- collations, defaults, constraints, indexes and their validity, rules,
-- policies, triggers, persistence, options, tablespace), so that nothing
-- unknown is dropped with the old tables (ADR5-IMPL-ALPHA-1).
DO $alrust$
DECLARE
    actual text;
BEGIN
    actual := ({V['alpha_shape']});
    IF actual IS DISTINCT FROM $shape${ALPHA_SHAPE}$shape$ THEN
        RAISE EXCEPTION 'alrust 0002: the alpha tables differ from what 0001 created; nothing was changed. Found:%',
            chr(10) || COALESCE(actual, '');
    END IF;
END
$alrust$;

-- 3.1: values that the reference layout cannot hold.
DO $alrust$
DECLARE
    bad text;
BEGIN
    SELECT string_agg(id, ', ' ORDER BY id) INTO bad FROM alerts a
     WHERE CASE WHEN jsonb_typeof(a.history) <> 'array' THEN true
                ELSE EXISTS (SELECT 1 FROM jsonb_array_elements(a.history) e
                              WHERE CASE WHEN jsonb_typeof(e) <> 'object' THEN true
                                         ELSE EXISTS (SELECT 1 FROM jsonb_object_keys(e) k
                                                       WHERE k NOT IN ('id', 'event', 'severity', 'status', 'value',
                                                                       'text', 'type', 'updateTime', 'user', 'timeout'))
                                    END)
           END;
    IF bad IS NOT NULL THEN
        RAISE EXCEPTION 'alrust 0002: alerts with a history entry the history type cannot hold; nothing was changed: %', bad;
    END IF;
    -- Field types (ADR5-HISTORY-TYPE-1): the text fields must be JSON strings
    -- (or null or absent), and timeout a whole JSON number (or null or
    -- absent). Anything else would be coerced by ->>, so it is refused, not
    -- converted. value is checked by the value rule below.
    SELECT string_agg(id, ', ' ORDER BY id) INTO bad FROM alerts a
     WHERE EXISTS (SELECT 1 FROM jsonb_array_elements(a.history) e
                    WHERE EXISTS (SELECT 1 FROM jsonb_each(e) f
                                   WHERE (f.key IN ('id', 'event', 'severity', 'status', 'text', 'type', 'user', 'updateTime')
                                          AND jsonb_typeof(f.value) NOT IN ('string', 'null'))
                                      OR (f.key = 'timeout'
                                          AND NOT (jsonb_typeof(f.value) = 'null'
                                                   OR (jsonb_typeof(f.value) = 'number'
                                                       AND (f.value #>> '{{}}')::numeric = trunc((f.value #>> '{{}}')::numeric))))));
    IF bad IS NOT NULL THEN
        RAISE EXCEPTION 'alrust 0002: alerts with a history entry field of the wrong JSON type; nothing was changed: %', bad;
    END IF;
    SELECT string_agg(id, ', ' ORDER BY id) INTO bad FROM alerts a
     WHERE NOT (a.timeout {I32}) OR NOT (a.duplicate_count {I32})
        OR EXISTS (SELECT 1 FROM jsonb_array_elements(a.history) e
                    WHERE jsonb_typeof(e->'timeout') = 'number' AND NOT ((e->>'timeout')::numeric {I32}));
    IF bad IS NOT NULL THEN
        RAISE EXCEPTION 'alrust 0002: alerts with timeout, duplicateCount or a history timeout outside integer range; nothing was changed: %', bad;
    END IF;
    SELECT string_agg(id, ', ' ORDER BY id) INTO bad FROM heartbeats WHERE NOT (timeout {I32});
    IF bad IS NOT NULL THEN
        RAISE EXCEPTION 'alrust 0002: heartbeats with timeout outside integer range; nothing was changed: %', bad;
    END IF;
    SELECT string_agg(format('(%s, %s, %s, %s): %s', environment, resource, event,
                             COALESCE(customer, '<no customer>'), ids), '; ')
      INTO bad
      FROM (SELECT environment, resource, event, COALESCE(customer, '') AS customer,
                   string_agg(id, ', ' ORDER BY id) AS ids
              FROM alerts GROUP BY environment, resource, event, COALESCE(customer, '')
            HAVING count(*) > 1) g;
    IF bad IS NOT NULL THEN
        RAISE EXCEPTION 'alrust 0002: alerts sharing (environment, resource, event, customer); nothing was changed: %', bad;
    END IF;
END
$alrust$;

-- 3.2: the history type (reference, verbatim).
{L(2, 29)}

-- 3.3: alerts, rebuilt by copy so that the column order is the reference's.
ALTER TABLE alerts RENAME TO alerts_alpha;
ALTER INDEX alerts_pkey RENAME TO alerts_alpha_pkey;
DROP INDEX alerts_identity_idx, alerts_last_receive_time_idx, alerts_status_idx;
-- PostgreSQL 18 names NOT NULL constraints (<table>_<column>_not_null), and
-- a renamed table keeps them; free those names for the new table so that its
-- constraints get exactly the reference's names.
DO $alrust$
DECLARE
    c record;
BEGIN
    FOR c IN SELECT conname FROM pg_constraint
              WHERE conrelid = 'public.alerts_alpha'::regclass AND conname LIKE 'alerts\\_%'
              ORDER BY conname LOOP
        EXECUTE format('ALTER TABLE public.alerts_alpha RENAME CONSTRAINT %I TO %I',
                       c.conname, 'alpha_' || c.conname);
    END LOOP;
END
$alrust$;

{L(32, 62)}

INSERT INTO alerts (id, resource, event, environment, severity, correlate, status, service, "group",
        value, text, tags, attributes, origin, type, create_time, timeout, raw_data, customer,
        duplicate_count, repeat, previous_severity, trend_indication, receive_time, last_receive_id,
        last_receive_time, history, update_time)
SELECT a.id, a.resource, a.event, a.environment, a.severity, a.correlate, a.status, a.service, a."group",
       {VALUE_TEXT.format(v='a.value')}, a.text, a.tags, a.attributes, a.origin, a.type,
       a.create_time AT TIME ZONE 'UTC', a.timeout::integer, a.raw_data, a.customer,
       a.duplicate_count::integer, a.repeat, a.previous_severity, a.trend_indication,
       a.receive_time AT TIME ZONE 'UTC', a.last_receive_id, a.last_receive_time AT TIME ZONE 'UTC',
       ARRAY(SELECT ROW(e->>'id', e->>'event', e->>'severity', e->>'status', {VALUE_TEXT.format(v="e->'value'")},
                        e->>'text', e->>'type', (e->>'updateTime')::timestamptz AT TIME ZONE 'UTC',
                        e->>'user', (e->>'timeout')::integer)::history
               FROM jsonb_array_elements(a.history) WITH ORDINALITY AS h(e, n) ORDER BY h.n),
       a.update_time AT TIME ZONE 'UTC'
  FROM alerts_alpha a;

-- 3.3.4: verify every field before dropping anything (ADR5-COPY-1).
DO $alrust$
DECLARE
    n bigint;
BEGIN
    n := ({V['alerts']});
    IF n <> 0 THEN
        RAISE EXCEPTION 'alrust 0002: % alert rows differ after the copy; nothing was changed', n;
    END IF;
    n := ({V['history']});
    IF n <> 0 THEN
        RAISE EXCEPTION 'alrust 0002: % history entries differ after the copy; nothing was changed', n;
    END IF;
END
$alrust$;

DROP TABLE alerts_alpha;

-- 3.4: heartbeats, rebuilt the same way.
ALTER TABLE heartbeats RENAME TO heartbeats_alpha;
ALTER INDEX heartbeats_pkey RENAME TO heartbeats_alpha_pkey;
DROP INDEX heartbeats_origin_customer_idx;
-- PostgreSQL 18 names NOT NULL constraints (<table>_<column>_not_null), and
-- a renamed table keeps them; free those names for the new table so that its
-- constraints get exactly the reference's names.
DO $alrust$
DECLARE
    c record;
BEGIN
    FOR c IN SELECT conname FROM pg_constraint
              WHERE conrelid = 'public.heartbeats_alpha'::regclass AND conname LIKE 'heartbeats\\_%'
              ORDER BY conname LOOP
        EXECUTE format('ALTER TABLE public.heartbeats_alpha RENAME CONSTRAINT %I TO %I',
                       c.conname, 'alpha_' || c.conname);
    END LOOP;
END
$alrust$;

{L(109, 120)}

INSERT INTO heartbeats (id, origin, tags, type, create_time, timeout, receive_time, customer, attributes)
SELECT h.id, h.origin, h.tags, h.type, h.create_time AT TIME ZONE 'UTC', h.timeout::integer,
       h.receive_time AT TIME ZONE 'UTC', h.customer, h.attributes
  FROM heartbeats_alpha h;

DO $alrust$
DECLARE
    n bigint;
BEGIN
    n := ({V['heartbeats']});
    IF n <> 0 THEN
        RAISE EXCEPTION 'alrust 0002: % heartbeat rows differ after the copy; nothing was changed', n;
    END IF;
END
$alrust$;

DROP TABLE heartbeats_alpha;

-- 3.5: keys already has the reference's column order. It is altered in
-- place and compared with a snapshot taken first.
CREATE TEMP TABLE alrust_keys_before ON COMMIT DROP AS SELECT * FROM keys;

ALTER TABLE keys
    ALTER COLUMN scopes DROP DEFAULT,
    ALTER COLUMN scopes DROP NOT NULL,
    ALTER COLUMN text DROP DEFAULT,
    ALTER COLUMN text DROP NOT NULL,
    ALTER COLUMN expire_time DROP NOT NULL,
    ALTER COLUMN expire_time TYPE timestamp without time zone USING expire_time AT TIME ZONE 'UTC',
    ALTER COLUMN count DROP DEFAULT,
    ALTER COLUMN count DROP NOT NULL,
    ALTER COLUMN last_used_time TYPE timestamp without time zone USING last_used_time AT TIME ZONE 'UTC';

DO $alrust$
DECLARE
    n bigint;
BEGIN
    n := ({V['keys']});
    IF n <> 0 THEN
        RAISE EXCEPTION 'alrust 0002: % key rows differ after the conversion; nothing was changed', n;
    END IF;
END
$alrust$;

-- 3.6: the tables the alpha did not have (reference, verbatim). The users
-- block's UPDATE runs only on the empty table created just before it.
{L(65, 107)}
{L(136, 193)}
-- The reference's unique indexes (verbatim).
{L(195, 198)}
"""
open("crates/alrust-store-pg/migrations/0002_reference_layout.sql", "w").write(sql)
print("ok", len(sql.splitlines()), "lines")
