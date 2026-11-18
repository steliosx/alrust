-- 0003: the note bindings (DECISIONS Y77; ADR-0005 decision 10;
-- spec/security-fixes.md SF-NOTES).
--
-- One alrust-owned table says which stored alert received a note, and
-- which stored version of the note row was bound (its xmin). Triggers on
-- public.alerts remove an alert's bindings when any program deletes the
-- alert, stores an alert with that id, changes an alert's id, or truncates
-- the table. The reference tables themselves are not changed.
--
-- The function is SECURITY DEFINER, so a program that writes public.alerts
-- under another role needs no privilege on the bindings table. EXECUTE is
-- revoked from PUBLIC, so only its owner can attach it to a table, and it
-- refuses to run for anything but an AFTER trigger on public.alerts. If
-- the bindings table is gone it does nothing: a write to alerts must not
-- fail because of it.
--
-- alrust compares the stored function body with this file at run time
-- (notes::bindings_state). Once applied anywhere this file must never
-- change (SQLx checksums).
SET LOCAL lock_timeout = '30s';

CREATE TABLE public.alrust_note_bindings (
    note text NOT NULL,
    alert text NOT NULL,
    note_xmin xid NOT NULL,
    CONSTRAINT alrust_note_bindings_pkey PRIMARY KEY (note)
);

CREATE INDEX alrust_note_bindings_alert_idx ON public.alrust_note_bindings USING btree (alert);

CREATE FUNCTION public.alrust_note_bindings_clear() RETURNS trigger
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = pg_catalog, pg_temp
AS $alrust$
BEGIN
    IF TG_RELID IS DISTINCT FROM pg_catalog.to_regclass('public.alerts')::oid
       OR TG_WHEN <> 'AFTER'
       OR (TG_OP = 'TRUNCATE') <> (TG_LEVEL = 'STATEMENT') THEN
        RAISE EXCEPTION 'alrust_note_bindings_clear() runs only as an AFTER trigger on public.alerts';
    END IF;
    IF pg_catalog.to_regclass('public.alrust_note_bindings') IS NULL THEN
        RETURN NULL;
    END IF;
    IF TG_OP = 'TRUNCATE' THEN
        DELETE FROM public.alrust_note_bindings;
        RETURN NULL;
    END IF;
    IF TG_OP = 'UPDATE' AND OLD.id IS NOT DISTINCT FROM NEW.id THEN
        RETURN NULL;
    END IF;
    IF TG_OP IN ('DELETE', 'UPDATE') THEN
        DELETE FROM public.alrust_note_bindings WHERE alert = OLD.id;
    END IF;
    IF TG_OP IN ('INSERT', 'UPDATE') THEN
        DELETE FROM public.alrust_note_bindings WHERE alert = NEW.id;
    END IF;
    RETURN NULL;
END
$alrust$;

REVOKE ALL ON FUNCTION public.alrust_note_bindings_clear() FROM PUBLIC;

CREATE TRIGGER alrust_note_bindings_insert AFTER INSERT ON public.alerts
    FOR EACH ROW EXECUTE FUNCTION public.alrust_note_bindings_clear();

CREATE TRIGGER alrust_note_bindings_delete AFTER DELETE ON public.alerts
    FOR EACH ROW EXECUTE FUNCTION public.alrust_note_bindings_clear();

CREATE TRIGGER alrust_note_bindings_update AFTER UPDATE ON public.alerts
    FOR EACH ROW WHEN (OLD.id IS DISTINCT FROM NEW.id)
    EXECUTE FUNCTION public.alrust_note_bindings_clear();

CREATE TRIGGER alrust_note_bindings_truncate AFTER TRUNCATE ON public.alerts
    FOR EACH STATEMENT EXECUTE FUNCTION public.alrust_note_bindings_clear();
