-- alerta_checkup.sql: a read-only look at the PostgreSQL database of an
-- Alerta 9.1.0 installation, to see which of the measured findings in
-- docs/performance/ may apply to it. Guide:
-- docs/performance/measure-your-own-alerta.md.
--
-- What it does: reads settings, catalog views, statistics views and
-- Alerta's own `metrics` table, plans one predicate with plain EXPLAIN (the
-- predicate is not run), takes two snapshots of counters some time apart
-- and prints the differences. Every query runs in a READ ONLY transaction
-- with short statement and lock timeouts. The wait between the snapshots
-- holds no transaction open (psql waits in a shell, not in the database).
--
-- What it never does: write, create, alter, vacuum, analyze, reset a
-- statistic, load an extension, send an HTTP request, or run EXPLAIN
-- ANALYZE. Use a role that can only read (see the guide).
--
-- Run it with psql against PostgreSQL 14 or newer (the CI check ran psql
-- and PostgreSQL 18.7 only):
--   psql "<connection string of a read-only role>" -X -v interval=300 -f alerta_checkup.sql
-- `interval` is the wait in seconds between the two snapshots (default
-- 300); pick a time of ordinary traffic.
--
-- CHECKUP_WAIT, if set in the environment, is run by the shell in place of
-- the wait. It exists for the CI check, which sends known traffic there.
-- Leave it unset.

\set ON_ERROR_STOP on
\if :{?interval}
\else
  \set interval 300
\endif
\setenv CHECKUP_INTERVAL :interval
SET application_name = 'alerta_checkup';
SET statement_timeout = '5s';
SET lock_timeout = '1s';
SET idle_in_transaction_session_timeout = '10s';
\pset footer off

\echo
\echo '== 1. Server and database'
BEGIN TRANSACTION READ ONLY;
SELECT current_setting('server_version') AS server_version, current_database() AS database;
SELECT current_setting('server_version_num')::int >= 150000 AS has_provider \gset
\if :has_provider
SELECT datcollate AS collation, datctype AS ctype, datlocprovider AS provider
  FROM pg_database WHERE datname = current_database();
\else
SELECT datcollate AS collation, datctype AS ctype FROM pg_database WHERE datname = current_database();
\endif
SELECT name, setting, unit FROM pg_settings
 WHERE name IN ('max_connections', 'shared_buffers', 'synchronous_commit', 'shared_preload_libraries')
 ORDER BY name;
COMMIT;

\echo
\echo '== 2. The alerts table'
BEGIN TRANSACTION READ ONLY;
SELECT pg_size_pretty(pg_total_relation_size('alerts')) AS total_size, reltuples::bigint AS estimated_rows
  FROM pg_class WHERE oid = 'alerts'::regclass;
SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'alerts' ORDER BY indexname;
\echo
\echo 'The predicate of Alerta''s tag and attribute writes, planned only (not run).'
\echo 'A "Seq Scan on alerts" here means each of those writes reads the whole table.'
EXPLAIN SELECT id FROM alerts
 WHERE id = '00000000-0000-0000-0000-000000000000' OR id LIKE '00000000-0000-0000-0000-000000000000%';
COMMIT;

\echo
\echo '== 3. Activity over' :interval 'seconds'
BEGIN TRANSACTION READ ONLY;
SELECT sessions AS a_sessions FROM pg_stat_database WHERE datname = current_database() \gset
SELECT n_tup_upd AS a_upd, n_tup_hot_upd AS a_hot, n_tup_ins AS a_ins
  FROM pg_stat_user_tables WHERE relid = 'alerts'::regclass \gset
SELECT coalesce(sum(count) FILTER (WHERE name = 'received'), 0) AS a_received,
       coalesce(sum(count) FILTER (WHERE name = 'queries'), 0) AS a_queries
  FROM metrics WHERE "group" = 'alerts' AND type = 'timer' \gset
SELECT extract(epoch FROM clock_timestamp()) AS a_t \gset
COMMIT;
\echo 'First snapshot taken; waiting (no transaction is open) ...'
\! ${CHECKUP_WAIT:-sleep "$CHECKUP_INTERVAL"}
BEGIN TRANSACTION READ ONLY;
SELECT sessions AS b_sessions FROM pg_stat_database WHERE datname = current_database() \gset
SELECT n_tup_upd AS b_upd, n_tup_hot_upd AS b_hot, n_tup_ins AS b_ins
  FROM pg_stat_user_tables WHERE relid = 'alerts'::regclass \gset
SELECT coalesce(sum(count) FILTER (WHERE name = 'received'), 0) AS b_received,
       coalesce(sum(count) FILTER (WHERE name = 'queries'), 0) AS b_queries
  FROM metrics WHERE "group" = 'alerts' AND type = 'timer' \gset
SELECT extract(epoch FROM clock_timestamp()) AS b_t \gset
COMMIT;
\x on
BEGIN TRANSACTION READ ONLY;
SELECT round((:b_t - :a_t)::numeric, 1) AS seconds,
       :b_received - :a_received AS alert_receives,
       :b_queries - :a_queries AS alert_queries,
       :b_sessions - :a_sessions AS new_sessions,
       round((:b_sessions - :a_sessions)::numeric
             / nullif(:b_received - :a_received + :b_queries - :a_queries, 0), 2)
         AS sessions_per_receive_or_query,
       :b_upd - :a_upd AS alert_row_updates,
       round((:b_upd - :a_upd)::numeric / nullif(:b_received - :a_received, 0), 2)
         AS alert_row_updates_per_receive,
       round(100.0 * (:b_hot - :a_hot) / nullif(:b_upd - :a_upd, 0), 1) AS hot_update_percent,
       :b_ins - :a_ins AS alerts_inserted;
COMMIT;
\x off
