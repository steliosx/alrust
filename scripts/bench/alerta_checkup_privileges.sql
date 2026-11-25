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
--   3. can_write f for every table; can_read_whole_table f for every table;
--      can_read_some_column t for `alerts` and `metrics` only.
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
\echo '      can_read_some_column t for alerts and metrics only)'
SELECT c.relname AS table_name,
       (has_any_column_privilege(current_user, c.oid, 'INSERT, UPDATE')
        OR has_table_privilege(current_user, c.oid, 'DELETE, TRUNCATE')) AS can_write,
       has_table_privilege(current_user, c.oid, 'SELECT') AS can_read_whole_table,
       has_any_column_privilege(current_user, c.oid, 'SELECT') AS can_read_some_column
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') ORDER BY 1;
COMMIT;
