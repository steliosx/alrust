-- 0003: the note bindings (DECISIONS Y77; ADR-0005 decision 10;
-- spec/security-fixes.md SF-NOTES).
--
-- One alrust-owned table says which stored alert received a note. Triggers
-- on public.alerts remove an alert's bindings when any program deletes the
-- alert, stores an alert with that id, changes an alert's id, or truncates
-- the table. The reference tables themselves are not changed.
--
-- The function is SECURITY DEFINER with a pinned search path, so a program
-- that writes public.alerts under another role needs no privilege on the
-- bindings table and cannot be refused because of it.
--
-- Once applied anywhere this file must never change (SQLx checksums).
SET LOCAL lock_timeout = '30s';

CREATE TABLE public.alrust_note_bindings (
    note text NOT NULL,
    alert text NOT NULL,
    CONSTRAINT alrust_note_bindings_pkey PRIMARY KEY (note)
);

CREATE INDEX alrust_note_bindings_alert_idx ON public.alrust_note_bindings USING btree (alert);

CREATE FUNCTION public.alrust_note_bindings_clear() RETURNS trigger
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = pg_catalog, public, pg_temp
AS $alrust$
BEGIN
    IF TG_OP = 'TRUNCATE' THEN
        DELETE FROM public.alrust_note_bindings;
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

CREATE TRIGGER alrust_note_bindings_insert AFTER INSERT ON public.alerts
    FOR EACH ROW EXECUTE FUNCTION public.alrust_note_bindings_clear();

CREATE TRIGGER alrust_note_bindings_delete AFTER DELETE ON public.alerts
    FOR EACH ROW EXECUTE FUNCTION public.alrust_note_bindings_clear();

CREATE TRIGGER alrust_note_bindings_update AFTER UPDATE ON public.alerts
    FOR EACH ROW WHEN (OLD.id IS DISTINCT FROM NEW.id)
    EXECUTE FUNCTION public.alrust_note_bindings_clear();

CREATE TRIGGER alrust_note_bindings_truncate AFTER TRUNCATE ON public.alerts
    FOR EACH STATEMENT EXECUTE FUNCTION public.alrust_note_bindings_clear();
