SELECT string_agg(line, E'\n' ORDER BY line) FROM (
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
) x
