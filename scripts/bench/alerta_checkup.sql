-- alerta_checkup.sql: a read-only look at the PostgreSQL database of an
-- Alerta 9.1.0 installation, to see which of the measured findings in
-- docs/performance/ may apply to it. Guide:
-- docs/performance/measure-your-own-alerta.md.
--
-- What it does: reads settings, catalog views, statistics views and four
-- columns of Alerta's own `metrics` table, plans one predicate with plain
-- EXPLAIN (the predicate is not run), takes two snapshots of counters some
-- time apart and prints the differences. Every query that reads the
-- database runs in a READ ONLY transaction that sets its own statement,
-- lock and idle timeouts (SET LOCAL, so they also hold behind a pooler in
-- transaction mode). The wait between the snapshots is a fixed `sleep` in
-- a local shell; no transaction is open during it.
--
-- What it never does: write, create, alter, vacuum, analyze, reset a
-- statistic, load an extension, send an HTTP request, run EXPLAIN ANALYZE,
-- or run a command taken from the environment. Use a role that can only
-- read what this file reads (see the guide).
--
-- Run it with psql against the primary, PostgreSQL 14 or newer (the CI
-- check ran psql and PostgreSQL 18.7 only):
--   psql "<connection string of the read-only role>" -X -v interval=300 -f alerta_checkup.sql
-- `interval` is the wait between the two snapshots: a whole number of
-- seconds from 1 to 3600 (default 300); pick a time of ordinary traffic.
--
-- The numbers of part 3 are indicators, not measurements of one request:
-- sessions are those of the whole database, row updates those of the whole
-- `alerts` table, and receives and queries are what Alerta's own timers
-- counted. If the two snapshots are not of the same running primary, or a
-- counter went back, the result says `undetermined`.

\set ON_ERROR_STOP on
\set QUIET on
\if :{?interval}
\else
  \set interval 300
\endif
-- The interval is checked as text before it is used as a number, and it
-- reaches the shell only after that (digits only).
SELECT :'interval' ~ '^[0-9]{1,4}$' AS interval_is_number \gset
\if :interval_is_number
  SELECT :'interval'::int BETWEEN 1 AND 3600 AS interval_ok \gset
\else
  \set interval_ok false
\endif
\if :interval_ok
\else
  \warn 'alerta_checkup: interval must be a whole number of seconds from 1 to 3600. Nothing was read.'
  \quit
\endif
\setenv CHECKUP_INTERVAL :interval
\set budgets 'SET LOCAL statement_timeout = ''5s''; SET LOCAL lock_timeout = ''1s''; SET LOCAL idle_in_transaction_session_timeout = ''10s'';'
\pset footer off

\echo
\echo '== 1. Server and database'
BEGIN TRANSACTION READ ONLY;
:budgets
SELECT current_setting('server_version') AS server_version, current_database() AS database,
       pg_is_in_recovery() AS is_standby;
SELECT current_setting('server_version_num')::int >= 150000 AS has_provider \gset
\if :has_provider
SELECT datcollate AS collation, datctype AS ctype, datlocprovider AS provider
  FROM pg_database WHERE datname = current_database();
\else
SELECT datcollate AS collation, datctype AS ctype FROM pg_database WHERE datname = current_database();
\endif
SELECT name, setting, unit FROM pg_settings
 WHERE name IN ('max_connections', 'shared_buffers', 'synchronous_commit')
 ORDER BY name;
COMMIT;

\echo
\echo '== 2. The alerts table'
BEGIN TRANSACTION READ ONLY;
:budgets
SELECT pg_size_pretty(pg_total_relation_size('alerts')) AS total_size, reltuples::bigint AS estimated_rows
  FROM pg_class WHERE oid = 'alerts'::regclass;
SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'alerts' ORDER BY indexname;
\echo
\echo 'A clue only: the plan of a SELECT with the predicate of Alerta''s tag and attribute'
\echo 'writes, for an id that does not exist (planned, not run). A "Seq Scan on alerts"'
\echo 'here suggests that those writes read the whole table. The plan of the real UPDATE'
\echo 'needs your data on a separate copy (part B of the guide).'
EXPLAIN SELECT id FROM alerts
 WHERE id = '00000000-0000-0000-0000-000000000000' OR id LIKE '00000000-0000-0000-0000-000000000000%';
COMMIT;

\echo
\echo '== 3. Activity over' :interval 'seconds (indicators; see the file header)'
BEGIN TRANSACTION READ ONLY;
:budgets
SELECT md5(concat_ws('|', current_database(), pg_postmaster_start_time(), d.stats_reset)) AS a_identity,
       pg_is_in_recovery() AS a_standby, d.sessions AS a_sessions
  FROM pg_stat_database d WHERE d.datname = current_database() \gset
SELECT n_tup_upd AS a_upd, n_tup_hot_upd AS a_hot, n_tup_ins AS a_ins
  FROM pg_stat_user_tables WHERE relid = 'alerts'::regclass \gset
SELECT coalesce(sum(count) FILTER (WHERE name = 'received'), 0) AS a_received,
       coalesce(sum(count) FILTER (WHERE name = 'queries'), 0) AS a_queries
  FROM metrics WHERE "group" = 'alerts' AND type = 'timer' \gset
SELECT extract(epoch FROM clock_timestamp()) AS a_t \gset
COMMIT;
\warn 'alerta_checkup: first snapshot taken; waiting (no transaction is open) ...'
\! sleep "$CHECKUP_INTERVAL"
\warn 'alerta_checkup: taking the second snapshot'
BEGIN TRANSACTION READ ONLY;
:budgets
SELECT md5(concat_ws('|', current_database(), pg_postmaster_start_time(), d.stats_reset)) AS b_identity,
       pg_is_in_recovery() AS b_standby, d.sessions AS b_sessions
  FROM pg_stat_database d WHERE d.datname = current_database() \gset
SELECT n_tup_upd AS b_upd, n_tup_hot_upd AS b_hot, n_tup_ins AS b_ins
  FROM pg_stat_user_tables WHERE relid = 'alerts'::regclass \gset
SELECT coalesce(sum(count) FILTER (WHERE name = 'received'), 0) AS b_received,
       coalesce(sum(count) FILTER (WHERE name = 'queries'), 0) AS b_queries
  FROM metrics WHERE "group" = 'alerts' AND type = 'timer' \gset
SELECT extract(epoch FROM clock_timestamp()) AS b_t \gset
COMMIT;
\x on
\pset null 'undetermined'
BEGIN TRANSACTION READ ONLY;
:budgets
WITH d AS (
  SELECT :b_t - :a_t AS seconds,
         :b_received - :a_received AS receives, :b_queries - :a_queries AS queries,
         :b_sessions - :a_sessions AS sessions, :b_upd - :a_upd AS upd, :b_hot - :a_hot AS hot,
         :b_ins - :a_ins AS ins,
         CASE WHEN :'a_standby'::boolean OR :'b_standby'::boolean
                THEN 'undetermined: this server is a standby; run the checkup on the primary'
              WHEN :'a_identity' <> :'b_identity'
                THEN 'undetermined: the snapshots are not of the same running database (restart, other server or statistics reset)'
              WHEN :b_t - :a_t <= 0 OR least(:b_received - :a_received, :b_queries - :a_queries,
                                             :b_sessions - :a_sessions, :b_upd - :a_upd, :b_hot - :a_hot,
                                             :b_ins - :a_ins) < 0
                THEN 'undetermined: a counter or the clock went back between the snapshots'
              ELSE 'ok' END AS result)
SELECT result,
       round(seconds::numeric, 1) AS seconds,
       receives AS alert_receives,
       queries AS alert_queries,
       sessions AS new_sessions,
       CASE WHEN result = 'ok' THEN round(sessions::numeric / nullif(receives + queries, 0), 2) END
         AS sessions_per_receive_or_query,
       upd AS alert_row_updates,
       CASE WHEN result = 'ok' THEN round(upd::numeric / nullif(receives, 0), 2) END
         AS alert_row_updates_per_receive,
       CASE WHEN result = 'ok' THEN round(100.0 * hot / nullif(upd, 0), 1) END AS hot_update_percent,
       ins AS alerts_inserted
  FROM d;
COMMIT;
\x off
\pset null ''
