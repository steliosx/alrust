-- alerta_checkup_privileges.sql: what the login that will run
-- alerta_checkup.sql can really do in this database. Guide:
-- docs/performance/measure-your-own-alerta.md, part A1.
--
-- The grants of the guide do not prove what a role can do: a role also has
-- what PUBLIC has, what the roles it is a member of have, and what was
-- granted before. This file reads the effective privileges of the current
-- login with PostgreSQL's own has_*_privilege functions. It reads catalogs
-- only, in one READ ONLY transaction, and changes nothing.
--
-- Run it as the checkup login, before the first checkup on production:
--   psql "<connection string of the read-only role>" -X -f alerta_checkup_privileges.sql
--
-- Expected for a login that can only do what the checkup needs:
--   1. no row (the login is a member of no other role);
--   2. superuser f, create_in_database f, create_in_public f;
--   3. can_write f and can_read_whole_table f for every table; readable_columns
--      exactly `id` for `alerts`, exactly `count, group, name, type` for
--      `metrics`, and empty for every other table. The last column says `ok`
--      or names what is missing or unexpected.
-- It looks at the `public` schema of this database only.

\set ON_ERROR_STOP on
\set QUIET on
\pset footer off
BEGIN TRANSACTION READ ONLY;
SET LOCAL statement_timeout = '5s'; SET LOCAL lock_timeout = '1s';
\echo '== 1. Roles this login is a member of (expected: none)'
SELECT r.rolname AS member_of FROM pg_roles r
 WHERE pg_has_role(current_user, r.oid, 'MEMBER') AND r.rolname <> current_user ORDER BY 1;
\echo
\echo '== 2. Superuser, and creating objects (expected: f, f, f)'
SELECT current_setting('is_superuser') = 'on' AS superuser,
       has_database_privilege(current_user, current_database(), 'CREATE') AS create_in_database,
       has_schema_privilege(current_user, 'public', 'CREATE') AS create_in_public;
\echo
\echo '== 3. Tables of schema public (expected: can_write f and can_read_whole_table f everywhere;'
\echo '      readable_columns: id for alerts; count, group, name, type for metrics; none elsewhere)'
WITH need(table_name, column_name) AS (
  VALUES ('alerts', 'id'), ('metrics', 'group'), ('metrics', 'name'), ('metrics', 'type'), ('metrics', 'count')),
t AS (
  SELECT c.oid, c.relname::text AS table_name
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
   WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')),
cols AS (
  SELECT t.table_name, a.attname::text AS column_name,
         has_column_privilege(current_user, t.oid, a.attnum, 'SELECT') AS readable
    FROM t JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum > 0 AND NOT a.attisdropped)
SELECT t.table_name,
       (has_any_column_privilege(current_user, t.oid, 'INSERT, UPDATE')
        OR has_table_privilege(current_user, t.oid, 'DELETE, TRUNCATE')) AS can_write,
       has_table_privilege(current_user, t.oid, 'SELECT') AS can_read_whole_table,
       coalesce((SELECT string_agg(column_name, ', ' ORDER BY column_name)
                   FROM cols WHERE cols.table_name = t.table_name AND readable), '') AS readable_columns,
       coalesce(nullif(concat_ws('; ',
         'missing: ' || (SELECT string_agg(need.column_name, ', ' ORDER BY need.column_name) FROM need
                          WHERE need.table_name = t.table_name
                            AND NOT EXISTS (SELECT 1 FROM cols WHERE cols.table_name = need.table_name
                                               AND cols.column_name = need.column_name AND cols.readable)),
         'unexpected: ' || (SELECT string_agg(cols.column_name, ', ' ORDER BY cols.column_name) FROM cols
                             WHERE cols.table_name = t.table_name AND cols.readable
                               AND NOT EXISTS (SELECT 1 FROM need WHERE need.table_name = cols.table_name
                                                  AND need.column_name = cols.column_name))), ''), 'ok')
         AS compared_to_the_checkup
  FROM t ORDER BY 1;
COMMIT;
