-- 0002: convert the alpha schema (0001) to the pinned reference layout,
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
         WHERE c.contype = 'f' AND c.confrelid IN ('public.alerts'::regclass, 'public.heartbeats'::regclass, 'public.keys'::regclass)
        UNION
        SELECT format('%s depends on %s', r.ev_class::regclass, d.refobjid::regclass)
          FROM pg_depend d JOIN pg_rewrite r ON r.oid = d.objid
         WHERE d.classid = 'pg_rewrite'::regclass AND d.refclassid = 'pg_class'::regclass
           AND d.refobjid IN ('public.alerts'::regclass, 'public.heartbeats'::regclass, 'public.keys'::regclass) AND r.ev_class <> d.refobjid
        UNION
        SELECT format('trigger %s on %s', t.tgname, t.tgrelid::regclass)
          FROM pg_trigger t WHERE NOT t.tgisinternal AND t.tgrelid IN ('public.alerts'::regclass, 'public.heartbeats'::regclass, 'public.keys'::regclass)
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
          FROM pg_class c WHERE c.oid IN ('public.alerts'::regclass, 'public.heartbeats'::regclass, 'public.keys'::regclass) AND (c.relrowsecurity OR c.relforcerowsecurity)
        UNION
        SELECT format('policy %s on %s', p.polname, p.polrelid::regclass)
          FROM pg_policy p WHERE p.polrelid IN ('public.alerts'::regclass, 'public.heartbeats'::regclass, 'public.keys'::regclass)
        UNION
        SELECT format('publication %s includes %s', b.pubname, r.prrelid::regclass)
          FROM pg_publication_rel r JOIN pg_publication b ON b.oid = r.prpubid WHERE r.prrelid IN ('public.alerts'::regclass, 'public.heartbeats'::regclass, 'public.keys'::regclass)
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
          FROM pg_inherits i WHERE i.inhrelid IN ('public.alerts'::regclass, 'public.heartbeats'::regclass, 'public.keys'::regclass) OR i.inhparent IN ('public.alerts'::regclass, 'public.heartbeats'::regclass, 'public.keys'::regclass)
        UNION
        SELECT format('%s is partitioned or a partition', c.oid::regclass)
          FROM pg_class c WHERE c.oid IN ('public.alerts'::regclass, 'public.heartbeats'::regclass, 'public.keys'::regclass) AND (c.relkind = 'p' OR c.relispartition)
        UNION
        SELECT format('extended statistics %s on %s', s.stxname, s.stxrelid::regclass)
          FROM pg_statistic_ext s WHERE s.stxrelid IN ('public.alerts'::regclass, 'public.heartbeats'::regclass, 'public.keys'::regclass)
        UNION
        SELECT format('non-default privileges on %s', c.oid::regclass)
          FROM pg_class c WHERE c.oid IN ('public.alerts'::regclass, 'public.heartbeats'::regclass, 'public.keys'::regclass) AND c.relacl IS NOT NULL
        UNION
        SELECT format('column privileges on %s.%s', a.attrelid::regclass, a.attname)
          FROM pg_attribute a WHERE a.attrelid IN ('public.alerts'::regclass, 'public.heartbeats'::regclass, 'public.keys'::regclass) AND a.attacl IS NOT NULL
        UNION
        SELECT format('%s is owned by %s, not %s', c.oid::regclass, pg_get_userbyid(c.relowner), current_user)
          FROM pg_class c
         WHERE c.oid IN ('public.alerts'::regclass, 'public.heartbeats'::regclass, 'public.keys'::regclass) AND c.relowner <> (SELECT oid FROM pg_roles WHERE rolname = current_user)
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
    actual := (SELECT string_agg(line, E'\n' ORDER BY line) FROM (
    SELECT format('%s table kind=%s persistence=%s rls=%s force_rls=%s options=%s tablespace=%s replica_identity=%s partition=%s',
                  c.relname, c.relkind, c.relpersistence, c.relrowsecurity, c.relforcerowsecurity,
                  COALESCE(c.reloptions::text, '-'), c.reltablespace, c.relreplident, c.relispartition) AS line
      FROM pg_class c WHERE c.oid IN ('public.alerts'::regclass, 'public.heartbeats'::regclass, 'public.keys'::regclass)
    UNION ALL
    SELECT format('%s column %s %s %s collation=%s not_null=%s default=%s identity=%s generated=%s',
                  c.relname, lpad(a.attnum::text, 3, '0'), a.attname, format_type(a.atttypid, a.atttypmod),
                  COALESCE(a.attcollation::regcollation::text, '-'), a.attnotnull,
                  COALESCE(pg_get_expr(d.adbin, d.adrelid), '-'), a.attidentity, a.attgenerated)
      FROM pg_class c
      JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
      LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
     WHERE c.oid IN ('public.alerts'::regclass, 'public.heartbeats'::regclass, 'public.keys'::regclass)
    UNION ALL
    SELECT format('%s constraint %s %s %s', c.relname, con.conname, con.contype, pg_get_constraintdef(con.oid))
      FROM pg_constraint con JOIN pg_class c ON c.oid = con.conrelid WHERE c.oid IN ('public.alerts'::regclass, 'public.heartbeats'::regclass, 'public.keys'::regclass)
    UNION ALL
    SELECT format('%s index %s valid=%s ready=%s live=%s', c.relname, pg_get_indexdef(x.indexrelid),
                  x.indisvalid, x.indisready, x.indislive)
      FROM pg_index x JOIN pg_class c ON c.oid = x.indrelid WHERE c.oid IN ('public.alerts'::regclass, 'public.heartbeats'::regclass, 'public.keys'::regclass)
    UNION ALL
    SELECT format('%s rule %s', c.relname, r.rulename)
      FROM pg_rewrite r JOIN pg_class c ON c.oid = r.ev_class WHERE c.oid IN ('public.alerts'::regclass, 'public.heartbeats'::regclass, 'public.keys'::regclass)
    UNION ALL
    SELECT format('%s policy %s', c.relname, p.polname)
      FROM pg_policy p JOIN pg_class c ON c.oid = p.polrelid WHERE c.oid IN ('public.alerts'::regclass, 'public.heartbeats'::regclass, 'public.keys'::regclass)
    UNION ALL
    SELECT format('%s trigger %s', c.relname, t.tgname)
      FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid WHERE c.oid IN ('public.alerts'::regclass, 'public.heartbeats'::regclass, 'public.keys'::regclass)
) x);
    IF actual IS DISTINCT FROM $shape$alerts column 001 id text collation="default" not_null=t default=- identity= generated=
alerts column 002 resource text collation="default" not_null=t default=- identity= generated=
alerts column 003 event text collation="default" not_null=t default=- identity= generated=
alerts column 004 environment text collation="default" not_null=t default=- identity= generated=
alerts column 005 severity text collation="default" not_null=t default=- identity= generated=
alerts column 006 correlate text[] collation="default" not_null=t default='{}'::text[] identity= generated=
alerts column 007 status text collation="default" not_null=t default=- identity= generated=
alerts column 008 service text[] collation="default" not_null=t default='{}'::text[] identity= generated=
alerts column 009 group text collation="default" not_null=t default=- identity= generated=
alerts column 010 value jsonb collation=- not_null=f default=- identity= generated=
alerts column 011 text text collation="default" not_null=t default=''::text identity= generated=
alerts column 012 tags text[] collation="default" not_null=t default='{}'::text[] identity= generated=
alerts column 013 attributes jsonb collation=- not_null=t default='{}'::jsonb identity= generated=
alerts column 014 origin text collation="default" not_null=t default=- identity= generated=
alerts column 015 type text collation="default" not_null=t default=- identity= generated=
alerts column 016 create_time timestamp with time zone collation=- not_null=t default=- identity= generated=
alerts column 017 timeout bigint collation=- not_null=f default=- identity= generated=
alerts column 018 raw_data text collation="default" not_null=f default=- identity= generated=
alerts column 019 customer text collation="default" not_null=f default=- identity= generated=
alerts column 020 duplicate_count bigint collation=- not_null=t default=0 identity= generated=
alerts column 021 repeat boolean collation=- not_null=t default=false identity= generated=
alerts column 022 previous_severity text collation="default" not_null=t default=- identity= generated=
alerts column 023 trend_indication text collation="default" not_null=t default=- identity= generated=
alerts column 024 receive_time timestamp with time zone collation=- not_null=t default=- identity= generated=
alerts column 025 last_receive_id text collation="default" not_null=t default=- identity= generated=
alerts column 026 last_receive_time timestamp with time zone collation=- not_null=t default=- identity= generated=
alerts column 027 update_time timestamp with time zone collation=- not_null=t default=- identity= generated=
alerts column 028 history jsonb collation=- not_null=t default='[]'::jsonb identity= generated=
alerts constraint alerts_attributes_not_null n NOT NULL attributes
alerts constraint alerts_correlate_not_null n NOT NULL correlate
alerts constraint alerts_create_time_not_null n NOT NULL create_time
alerts constraint alerts_duplicate_count_not_null n NOT NULL duplicate_count
alerts constraint alerts_environment_not_null n NOT NULL environment
alerts constraint alerts_event_not_null n NOT NULL event
alerts constraint alerts_group_not_null n NOT NULL "group"
alerts constraint alerts_history_not_null n NOT NULL history
alerts constraint alerts_id_not_null n NOT NULL id
alerts constraint alerts_last_receive_id_not_null n NOT NULL last_receive_id
alerts constraint alerts_last_receive_time_not_null n NOT NULL last_receive_time
alerts constraint alerts_origin_not_null n NOT NULL origin
alerts constraint alerts_pkey p PRIMARY KEY (id)
alerts constraint alerts_previous_severity_not_null n NOT NULL previous_severity
alerts constraint alerts_receive_time_not_null n NOT NULL receive_time
alerts constraint alerts_repeat_not_null n NOT NULL repeat
alerts constraint alerts_resource_not_null n NOT NULL resource
alerts constraint alerts_service_not_null n NOT NULL service
alerts constraint alerts_severity_not_null n NOT NULL severity
alerts constraint alerts_status_not_null n NOT NULL status
alerts constraint alerts_tags_not_null n NOT NULL tags
alerts constraint alerts_text_not_null n NOT NULL text
alerts constraint alerts_trend_indication_not_null n NOT NULL trend_indication
alerts constraint alerts_type_not_null n NOT NULL type
alerts constraint alerts_update_time_not_null n NOT NULL update_time
alerts index CREATE INDEX alerts_identity_idx ON public.alerts USING btree (environment, resource, COALESCE(customer, ''::text)) valid=t ready=t live=t
alerts index CREATE INDEX alerts_last_receive_time_idx ON public.alerts USING btree (last_receive_time DESC) valid=t ready=t live=t
alerts index CREATE INDEX alerts_status_idx ON public.alerts USING btree (status) valid=t ready=t live=t
alerts index CREATE UNIQUE INDEX alerts_pkey ON public.alerts USING btree (id) valid=t ready=t live=t
alerts table kind=r persistence=p rls=f force_rls=f options=- tablespace=0 replica_identity=d partition=f
heartbeats column 001 id text collation="default" not_null=t default=- identity= generated=
heartbeats column 002 origin text collation="default" not_null=t default=- identity= generated=
heartbeats column 003 tags text[] collation="default" not_null=t default='{}'::text[] identity= generated=
heartbeats column 004 attributes jsonb collation=- not_null=t default='{}'::jsonb identity= generated=
heartbeats column 005 type text collation="default" not_null=t default=- identity= generated=
heartbeats column 006 create_time timestamp with time zone collation=- not_null=t default=- identity= generated=
heartbeats column 007 timeout bigint collation=- not_null=t default=- identity= generated=
heartbeats column 008 receive_time timestamp with time zone collation=- not_null=t default=- identity= generated=
heartbeats column 009 customer text collation="default" not_null=f default=- identity= generated=
heartbeats constraint heartbeats_attributes_not_null n NOT NULL attributes
heartbeats constraint heartbeats_create_time_not_null n NOT NULL create_time
heartbeats constraint heartbeats_id_not_null n NOT NULL id
heartbeats constraint heartbeats_origin_not_null n NOT NULL origin
heartbeats constraint heartbeats_pkey p PRIMARY KEY (id)
heartbeats constraint heartbeats_receive_time_not_null n NOT NULL receive_time
heartbeats constraint heartbeats_tags_not_null n NOT NULL tags
heartbeats constraint heartbeats_timeout_not_null n NOT NULL timeout
heartbeats constraint heartbeats_type_not_null n NOT NULL type
heartbeats index CREATE UNIQUE INDEX heartbeats_origin_customer_idx ON public.heartbeats USING btree (origin, COALESCE(customer, ''::text)) valid=t ready=t live=t
heartbeats index CREATE UNIQUE INDEX heartbeats_pkey ON public.heartbeats USING btree (id) valid=t ready=t live=t
heartbeats table kind=r persistence=p rls=f force_rls=f options=- tablespace=0 replica_identity=d partition=f
keys column 001 id text collation="default" not_null=t default=- identity= generated=
keys column 002 key text collation="default" not_null=t default=- identity= generated=
keys column 003 user text collation="default" not_null=t default=- identity= generated=
keys column 004 scopes text[] collation="default" not_null=t default='{}'::text[] identity= generated=
keys column 005 text text collation="default" not_null=t default=''::text identity= generated=
keys column 006 expire_time timestamp with time zone collation=- not_null=t default=- identity= generated=
keys column 007 count bigint collation=- not_null=t default=0 identity= generated=
keys column 008 last_used_time timestamp with time zone collation=- not_null=f default=- identity= generated=
keys column 009 customer text collation="default" not_null=f default=- identity= generated=
keys constraint keys_count_not_null n NOT NULL count
keys constraint keys_expire_time_not_null n NOT NULL expire_time
keys constraint keys_id_not_null n NOT NULL id
keys constraint keys_key_key u UNIQUE (key)
keys constraint keys_key_not_null n NOT NULL key
keys constraint keys_pkey p PRIMARY KEY (id)
keys constraint keys_scopes_not_null n NOT NULL scopes
keys constraint keys_text_not_null n NOT NULL text
keys constraint keys_user_not_null n NOT NULL "user"
keys index CREATE UNIQUE INDEX keys_key_key ON public.keys USING btree (key) valid=t ready=t live=t
keys index CREATE UNIQUE INDEX keys_pkey ON public.keys USING btree (id) valid=t ready=t live=t
keys table kind=r persistence=p rls=f force_rls=f options=- tablespace=0 replica_identity=d partition=f$shape$ THEN
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
                                                       AND (f.value #>> '{}')::numeric = trunc((f.value #>> '{}')::numeric))))));
    IF bad IS NOT NULL THEN
        RAISE EXCEPTION 'alrust 0002: alerts with a history entry field of the wrong JSON type; nothing was changed: %', bad;
    END IF;
    SELECT string_agg(id, ', ' ORDER BY id) INTO bad FROM alerts a
     WHERE NOT (a.timeout BETWEEN -2147483648 AND 2147483647) OR NOT (a.duplicate_count BETWEEN -2147483648 AND 2147483647)
        OR EXISTS (SELECT 1 FROM jsonb_array_elements(a.history) e
                    WHERE jsonb_typeof(e->'timeout') = 'number' AND NOT ((e->>'timeout')::numeric BETWEEN -2147483648 AND 2147483647));
    IF bad IS NOT NULL THEN
        RAISE EXCEPTION 'alrust 0002: alerts with timeout, duplicateCount or a history timeout outside integer range; nothing was changed: %', bad;
    END IF;
    SELECT string_agg(id, ', ' ORDER BY id) INTO bad FROM heartbeats WHERE NOT (timeout BETWEEN -2147483648 AND 2147483647);
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
    -- ADR-0005 decision 8 is PENDING THE OWNER. Until it is decided, a value
    -- that is not a JSON string or null is refused here rather than
    -- converted. (Integers and booleans are already stored as strings, R0.3.)
    SELECT string_agg(id, ', ' ORDER BY id) INTO bad FROM alerts a
     WHERE jsonb_typeof(a.value) NOT IN ('string', 'null')
        OR EXISTS (SELECT 1 FROM jsonb_array_elements(a.history) e
                    WHERE jsonb_typeof(e->'value') NOT IN ('string', 'null'));
    IF bad IS NOT NULL THEN
        RAISE EXCEPTION 'alrust 0002: alerts whose value or a history value is not a string (ADR-0005 decision 8 pending); nothing was changed: %', bad;
    END IF;
END
$alrust$;

-- 3.2: the history type (reference, verbatim).
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'history' AND typnamespace = (SELECT oid FROM pg_namespace WHERE nspname = current_schema())) THEN
        CREATE TYPE history AS (
            id text,
            event text,
            severity text,
            status text,
            value text,
            text text,
            type text,
            update_time timestamp without time zone,
            "user" text,
            timeout integer
        );
    ELSE
        BEGIN
            ALTER TYPE history ADD ATTRIBUTE "user" text CASCADE;
        EXCEPTION
            WHEN duplicate_column THEN RAISE NOTICE 'column "user" already exists in history type.';
        END;
        BEGIN
            ALTER TYPE history ADD ATTRIBUTE timeout integer CASCADE;
        EXCEPTION
            WHEN duplicate_column THEN RAISE NOTICE 'column "timeout" already exists in history type.';
        END;
    END IF;
END$$;

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
              WHERE conrelid = 'public.alerts_alpha'::regclass AND conname LIKE 'alerts\_%'
              ORDER BY conname LOOP
        EXECUTE format('ALTER TABLE public.alerts_alpha RENAME CONSTRAINT %I TO %I',
                       c.conname, 'alpha_' || c.conname);
    END LOOP;
END
$alrust$;

CREATE TABLE IF NOT EXISTS alerts (
    id text PRIMARY KEY,
    resource text NOT NULL,
    event text NOT NULL,
    environment text,
    severity text,
    correlate text[],
    status text,
    service text[],
    "group" text,
    value text,
    text text,
    tags text[],
    attributes jsonb,
    origin text,
    type text,
    create_time timestamp without time zone,
    timeout integer,
    raw_data text,
    customer text,
    duplicate_count integer,
    repeat boolean,
    previous_severity text,
    trend_indication text,
    receive_time timestamp without time zone,
    last_receive_id text,
    last_receive_time timestamp without time zone,
    history history[]
);

ALTER TABLE alerts ADD COLUMN IF NOT EXISTS update_time timestamp without time zone;

INSERT INTO alerts (id, resource, event, environment, severity, correlate, status, service, "group",
        value, text, tags, attributes, origin, type, create_time, timeout, raw_data, customer,
        duplicate_count, repeat, previous_severity, trend_indication, receive_time, last_receive_id,
        last_receive_time, history, update_time)
SELECT a.id, a.resource, a.event, a.environment, a.severity, a.correlate, a.status, a.service, a."group",
       a.value #>> '{}', a.text, a.tags, a.attributes, a.origin, a.type,
       a.create_time AT TIME ZONE 'UTC', a.timeout::integer, a.raw_data, a.customer,
       a.duplicate_count::integer, a.repeat, a.previous_severity, a.trend_indication,
       a.receive_time AT TIME ZONE 'UTC', a.last_receive_id, a.last_receive_time AT TIME ZONE 'UTC',
       ARRAY(SELECT ROW(e->>'id', e->>'event', e->>'severity', e->>'status', e->'value' #>> '{}',
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
    n := (SELECT count(*) FROM alerts_alpha o FULL JOIN alerts r ON r.id = o.id
 WHERE o.id IS NULL OR r.id IS NULL
    OR r.resource IS DISTINCT FROM o.resource
    OR r.event IS DISTINCT FROM o.event
    OR r.environment IS DISTINCT FROM o.environment
    OR r.severity IS DISTINCT FROM o.severity
    OR r.correlate IS DISTINCT FROM o.correlate
    OR r.status IS DISTINCT FROM o.status
    OR r.service IS DISTINCT FROM o.service
    OR r."group" IS DISTINCT FROM o."group"
    OR r.value IS DISTINCT FROM (o.value #>> '{}')
    OR r.text IS DISTINCT FROM o.text
    OR r.tags IS DISTINCT FROM o.tags
    OR r.attributes IS DISTINCT FROM o.attributes
    OR r.origin IS DISTINCT FROM o.origin
    OR r.type IS DISTINCT FROM o.type
    OR r.create_time IS DISTINCT FROM (o.create_time AT TIME ZONE 'UTC')
    OR (r.create_time AT TIME ZONE 'UTC') IS DISTINCT FROM o.create_time
    OR r.timeout::bigint IS DISTINCT FROM o.timeout
    OR r.raw_data IS DISTINCT FROM o.raw_data
    OR r.customer IS DISTINCT FROM o.customer
    OR r.duplicate_count::bigint IS DISTINCT FROM o.duplicate_count
    OR r.repeat IS DISTINCT FROM o.repeat
    OR r.previous_severity IS DISTINCT FROM o.previous_severity
    OR r.trend_indication IS DISTINCT FROM o.trend_indication
    OR r.receive_time IS DISTINCT FROM (o.receive_time AT TIME ZONE 'UTC')
    OR (r.receive_time AT TIME ZONE 'UTC') IS DISTINCT FROM o.receive_time
    OR r.last_receive_id IS DISTINCT FROM o.last_receive_id
    OR r.last_receive_time IS DISTINCT FROM (o.last_receive_time AT TIME ZONE 'UTC')
    OR (r.last_receive_time AT TIME ZONE 'UTC') IS DISTINCT FROM o.last_receive_time
    OR r.update_time IS DISTINCT FROM (o.update_time AT TIME ZONE 'UTC')
    OR (r.update_time AT TIME ZONE 'UTC') IS DISTINCT FROM o.update_time
    OR cardinality(r.history) IS DISTINCT FROM jsonb_array_length(o.history));
    IF n <> 0 THEN
        RAISE EXCEPTION 'alrust 0002: % alert rows differ after the copy; nothing was changed', n;
    END IF;
    n := (SELECT count(*)
  FROM (SELECT a.id, h.n, h.e
          FROM alerts_alpha a CROSS JOIN LATERAL jsonb_array_elements(a.history) WITH ORDINALITY AS h(e, n)) o
  FULL JOIN (SELECT a.id, h.*
          FROM alerts a CROSS JOIN LATERAL unnest(a.history)
               WITH ORDINALITY AS h(hid, event, severity, status, value, text, type, update_time, usr, timeout, n)) r
    ON r.id = o.id AND r.n = o.n
 WHERE o.id IS NULL OR r.id IS NULL
    OR r.hid IS DISTINCT FROM (o.e->>'id')
    OR r.event IS DISTINCT FROM (o.e->>'event')
    OR r.severity IS DISTINCT FROM (o.e->>'severity')
    OR r.status IS DISTINCT FROM (o.e->>'status')
    OR r.value IS DISTINCT FROM (o.e->'value' #>> '{}')
    OR r.text IS DISTINCT FROM (o.e->>'text')
    OR r.type IS DISTINCT FROM (o.e->>'type')
    OR r.update_time IS DISTINCT FROM ((o.e->>'updateTime')::timestamptz AT TIME ZONE 'UTC')
    OR (r.update_time AT TIME ZONE 'UTC') IS DISTINCT FROM (o.e->>'updateTime')::timestamptz
    OR r.usr IS DISTINCT FROM (o.e->>'user')
    OR r.timeout::bigint IS DISTINCT FROM (o.e->>'timeout')::bigint);
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
              WHERE conrelid = 'public.heartbeats_alpha'::regclass AND conname LIKE 'heartbeats\_%'
              ORDER BY conname LOOP
        EXECUTE format('ALTER TABLE public.heartbeats_alpha RENAME CONSTRAINT %I TO %I',
                       c.conname, 'alpha_' || c.conname);
    END LOOP;
END
$alrust$;

CREATE TABLE IF NOT EXISTS heartbeats (
    id text PRIMARY KEY,
    origin text NOT NULL,
    tags text[],
    type text,
    create_time timestamp without time zone,
    timeout integer,
    receive_time timestamp without time zone,
    customer text
);

ALTER TABLE heartbeats ADD COLUMN IF NOT EXISTS attributes jsonb;

INSERT INTO heartbeats (id, origin, tags, type, create_time, timeout, receive_time, customer, attributes)
SELECT h.id, h.origin, h.tags, h.type, h.create_time AT TIME ZONE 'UTC', h.timeout::integer,
       h.receive_time AT TIME ZONE 'UTC', h.customer, h.attributes
  FROM heartbeats_alpha h;

DO $alrust$
DECLARE
    n bigint;
BEGIN
    n := (SELECT count(*) FROM heartbeats_alpha o FULL JOIN heartbeats r ON r.id = o.id
 WHERE o.id IS NULL OR r.id IS NULL
    OR r.origin IS DISTINCT FROM o.origin
    OR r.tags IS DISTINCT FROM o.tags
    OR r.type IS DISTINCT FROM o.type
    OR r.create_time IS DISTINCT FROM (o.create_time AT TIME ZONE 'UTC')
    OR (r.create_time AT TIME ZONE 'UTC') IS DISTINCT FROM o.create_time
    OR r.timeout::bigint IS DISTINCT FROM o.timeout
    OR r.receive_time IS DISTINCT FROM (o.receive_time AT TIME ZONE 'UTC')
    OR (r.receive_time AT TIME ZONE 'UTC') IS DISTINCT FROM o.receive_time
    OR r.customer IS DISTINCT FROM o.customer
    OR r.attributes IS DISTINCT FROM o.attributes);
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
    n := (SELECT count(*) FROM alrust_keys_before o FULL JOIN keys r ON r.id = o.id
 WHERE o.id IS NULL OR r.id IS NULL
    OR r.key IS DISTINCT FROM o.key
    OR r."user" IS DISTINCT FROM o."user"
    OR r.scopes IS DISTINCT FROM o.scopes
    OR r.text IS DISTINCT FROM o.text
    OR r.expire_time IS DISTINCT FROM (o.expire_time AT TIME ZONE 'UTC')
    OR (r.expire_time AT TIME ZONE 'UTC') IS DISTINCT FROM o.expire_time
    OR r.count IS DISTINCT FROM o.count
    OR r.last_used_time IS DISTINCT FROM (o.last_used_time AT TIME ZONE 'UTC')
    OR (r.last_used_time AT TIME ZONE 'UTC') IS DISTINCT FROM o.last_used_time
    OR r.customer IS DISTINCT FROM o.customer);
    IF n <> 0 THEN
        RAISE EXCEPTION 'alrust 0002: % key rows differ after the conversion; nothing was changed', n;
    END IF;
END
$alrust$;

-- 3.6: the tables the alpha did not have (reference, verbatim). The users
-- block's UPDATE runs only on the empty table created just before it.
CREATE TABLE IF NOT EXISTS notes (
    id text PRIMARY KEY,
    text text,
    "user" text,
    attributes jsonb,
    type text NOT NULL,
    create_time timestamp without time zone NOT NULL,
    update_time timestamp without time zone,
    alert text,
    customer text
);


CREATE TABLE IF NOT EXISTS blackouts (
    id text PRIMARY KEY,
    priority integer NOT NULL,
    environment text NOT NULL,
    service text[],
    resource text,
    event text,
    "group" text,
    tags text[],
    customer text,
    start_time timestamp without time zone NOT NULL,
    end_time timestamp without time zone NOT NULL,
    duration integer
);

ALTER TABLE blackouts
ADD COLUMN IF NOT EXISTS "user" text,
ADD COLUMN IF NOT EXISTS create_time timestamp without time zone,
ADD COLUMN IF NOT EXISTS text text,
ADD COLUMN IF NOT EXISTS origin text;


CREATE TABLE IF NOT EXISTS customers (
    id text PRIMARY KEY,
    match text NOT NULL,
    customer text
);

ALTER TABLE customers DROP CONSTRAINT IF EXISTS customers_match_key;

CREATE TABLE IF NOT EXISTS metrics (
    "group" text NOT NULL,
    name text NOT NULL,
    title text,
    description text,
    value integer,
    count integer,
    total_time integer,
    type text NOT NULL,
    CONSTRAINT metrics_pkey PRIMARY KEY ("group", name, type)
);
ALTER TABLE metrics ALTER COLUMN total_time TYPE BIGINT;
ALTER TABLE metrics ALTER COLUMN count TYPE BIGINT;


CREATE TABLE IF NOT EXISTS perms (
    id text PRIMARY KEY,
    match text UNIQUE NOT NULL,
    scopes text[]
);


CREATE TABLE IF NOT EXISTS users (
    id text PRIMARY KEY,
    name text,
    email text UNIQUE,
    password text NOT NULL,
    status text,
    roles text[],
    attributes jsonb,
    create_time timestamp without time zone NOT NULL,
    last_login timestamp without time zone,
    text text,
    update_time timestamp without time zone,
    email_verified boolean,
    hash text
);
ALTER TABLE users ALTER COLUMN email DROP NOT NULL;

DO $$
BEGIN
    ALTER TABLE users ADD COLUMN login text UNIQUE;
    UPDATE users SET login = email;
    ALTER TABLE users ALTER COLUMN login SET NOT NULL;
EXCEPTION
    WHEN duplicate_column THEN RAISE NOTICE 'column "login" already exists in users.';
END$$;

CREATE TABLE IF NOT EXISTS groups (
    id text PRIMARY KEY,
    name text UNIQUE NOT NULL,
    users text[],
    text text,
    tags text[],
    attributes jsonb,
    update_time timestamp without time zone
);

-- The reference's unique indexes (verbatim).
CREATE UNIQUE INDEX IF NOT EXISTS env_res_evt_cust_key ON alerts USING btree (environment, resource, event, (COALESCE(customer, ''::text)));


CREATE UNIQUE INDEX IF NOT EXISTS org_cust_key ON heartbeats USING btree (origin, (COALESCE(customer, ''::text)));
