"""alerta_fast: start-up patches for an unmodified Alerta 9.1.0 (PostgreSQL).

Not a fork: Alerta is installed as released, and this one file replaces a
few of its functions when the server starts. Serve it in place of
`alerta.app:create_app()`:

    gunicorn --pythonpath <this directory> ... alerta_fast:app

ALERTA_FAST selects the patches, comma-separated. The default is "safe":
reuse, nolog, onecount and nosentry, which leave every write of Alerta as
it is. `hot` changes when and how surely two counters are written,
`noecho` leaves out a write that changes nothing, and `savedq` keeps the
SQL of searches in the worker's memory, so each is never on unless it is
named: ALERTA_FAST=safe,hot,noecho,savedq.
  reuse     One PostgreSQL connection per worker thread, kept between
            requests, in place of a new connection per request. A request
            ends with ROLLBACK (Alerta commits its own writes), so no open
            transaction passes to the next request. A kept connection is
            replaced before a request, never reused, when: the server has
            closed it or sent anything while it was idle (checked on the
            socket, with no round trip); the server has reported that the
            session is read-only or on a standby (`in_hot_standby`,
            `default_transaction_read_only`; no round trip); a statement
            failed in the request that used it; the ROLLBACK failed; or it
            is older than ALERTA_FAST_CONN_MAX_AGE seconds (default 300).
            A failed request is never sent again by this file.
            For a failover (Patroni or any other): where a new connection
            goes is the deployment's choice (a proxy that follows the
            leader, or several hosts in DATABASE_URL with
            target_session_attrs=read-write), as for Alerta as released.
            While new connections reach a standby, each request makes its
            own, as Alerta as released does.
            Not covered: session state that a plugin leaves behind (SET,
            temporary tables, advisory locks) stays on the connection, and
            ROLLBACK does not undo what a plugin committed. Check custom
            plugins before using `reuse` with them.
  hot       OPT-IN, best effort. The two writes that every request makes to
            one shared row are collected in the process and written about
            once a second (ALERTA_FAST_FLUSH_SECONDS, default 1.0) by one
            thread per worker on its own connection: the API key's use
            count and last-used time, and the `metrics` timers and
            counters. Key verification itself is not cached. What changes:
              late:      `GET /keys` and `/management/metrics` are behind
                         by up to the interval, and by longer while the
                         database cannot be written; the last-used time
                         is the time of the write, not of the request;
              lost:      a worker that is killed, or that stops while the
                         database cannot be written, loses what it had
                         not written. A stop that needs no kill writes it;
              too high:  a batch whose commit succeeded on the server but
                         was reported to the worker as failed is written
                         again.
            Do not use `hot` where key-use counts or metrics must be exact.
  noecho    OPT-IN. After a plugin's post-receive or action hook returns the
            alert (the default `forwarder` plugin always does, with or
            without a destination), Alerta writes the alert's tags and
            attributes again: two more UPDATEs, each with its own commit,
            and each with `WHERE id=... OR id LIKE ...`, which reads the
            whole table. With `noecho` the row is first read by its id
            (primary key). When the stored tags are the same set, with no
            tag twice and no NULL, the tags are not written. When
            PostgreSQL renders the stored attributes and the document that
            would be written as the same jsonb text, the attributes are
            not written. The comparison is made in PostgreSQL, with
            Alerta's own adaptation of the document, so 1, 1.0,
            1.0000000000000001 and true all differ. In every other case
            Alerta's own statement runs unchanged. The same holds for
            `PUT /alert/<id>/attributes` and the status and action paths,
            which use the same two functions.
            Kept: Alerta's function commits the request's connection, so
            a left-out write commits it too. What a plugin wrote earlier
            in the request without a commit is committed at the same
            point as in Alerta as released.
            What changes when a write is left out:
              order:     Alerta's write stores the tags in the order of a
                         Python set, which differs from worker to worker.
                         Without the write the stored order stays;
              race:      a change that another request commits between
                         this read and Alerta's write is overwritten by
                         Alerta as released. Without the write it stays;
              other ids: Alerta's statement has `id LIKE '<id>%'`, so it
                         also overwrites every other alert whose id
                         begins with this id, and `_` and `%` in this id
                         are wildcards there (possible only with ids
                         that a client chose). Without the write those
                         rows stay as they are;
              triggers:  an installation that added its own UPDATE
                         trigger or rule to `alerts` does not see the
                         two UPDATEs that changed nothing. Alerta itself
                         has no trigger. The row's xmin does not change
                         and no WAL is written for it.
  savedq    OPT-IN. For a search (`q=`) Alerta parses the text with
            pyparsing and writes the SQL condition and its values, again
            for every request, also when a dashboard sends the same saved
            search every few seconds. Alerta's PostgreSQL code calls the
            parser in two places: the alert lists (/alerts, /alerts/count
            and the other alert queries) and the blackout list
            (/blackouts). Both call it with the text and the default
            field only, so `savedq` serves both.
            With `savedq` each worker keeps, for the last
            ALERTA_FAST_SAVEDQ_ENTRIES (default 256) different pairs of
            search text and default field (`q.df`, or Alerta's default),
            what Alerta's own parser returned: the SQL condition and the
            names and values of its parameters. The next request with the
            same pair gets the same condition and a new dict of the same
            values, and Alerta's parser does not run. That is all that is
            saved: the parse and the writing of the condition. A kept
            answer comes from the two texts only; nothing of the request,
            the caller or the user goes into it.
            Not kept and not changed: the result of a search, the alerts,
            the customer scope, the key or token check, the dates, the
            page, the sort order and every other parameter. Alerta adds
            those to the condition after the parser, for each request,
            and each request reads the database.
            The parser is always called, and nothing is kept, for: a
            search that the parser refuses (the same error each time); a
            text of more than ALERTA_FAST_SAVEDQ_BYTES bytes with its
            field (default 2048); a parser object that is not exactly
            Alerta's class; an answer that is not one string and a dict
            of strings.
            Your own parser code. `savedq` is for Alerta's parser as
            released, not changed while the worker runs:
              - Your own grammar, or Alerta's grammar changed in place,
                at any time (another `query_expr` object, a copy of it,
                or a new parse action on one of its parts, for example):
                leave `savedq` out of ALERTA_FAST, or call
                `alerta_fast.savedq_disable()` before the first request
                (for example in your WSGI file, after the import of
                alerta_fast). The patch cannot see such a grammar: it
                takes the grammar object that it finds at start-up as
                Alerta's. With `savedq` on, a caller can then get the
                answer that your grammar gave to another caller.
                Importing your code before alerta_fast is not enough
                for a grammar.
              - At start-up, when QueryParser.parse or one of the parser's
                functions (the `to_sql` and `__init__` of its Search
                classes, `_next_param`) is not the function of Alerta's
                file, `savedq` stays off and says so in the log. Your
                function is called as before.
              - At each call the patch compares the grammar object
                (`query_expr`) and those functions with the ones of
                start-up. When one was replaced after start-up, `savedq`
                goes off for the rest of the worker's life and forgets
                what it kept.
              - savedq_disable() turns `savedq` off for this worker and
                forgets what it kept. There is no call to turn it on
                again.
              - A function put around QueryParser.parse after start-up
                gets the kept answer as it would get the parser's.
            When off, each call goes to the parser, one thread at a time.
            One thread at a time: Alerta's parser is called by one thread
            of the worker at a time (a lock), and a thread that waited
            looks in the store again before it parses. In one run of
            scripts/bench/savedq_check.py without the lock, 18 of 2400
            answers to eight threads differed from the answers to one
            thread. The cause is not confirmed: Alerta's parser alone gave
            no difference in the next run. The lock stays until it is.
            Alerta as released has no such lock.
            The server does not start with `savedq` when the installed
            postgres/queryparser.py is not the file of Alerta 9.1.0 (git
            blob 346ff01a). Memory: at most the entries times (the text,
            the condition and the values). No search text and no value is
            logged or counted by name; `savedq_stats` has numbers only.
  nolog     Alerta formats every SQL statement for a debug message before
            the logger checks its level. Skip that unless DEBUG is on.
  onecount  The list and the count read severity and status totals with
            two scans of the same rows. Read both with one grouped query.
            Its answer is kept for the one HTTP request only (on Flask's
            request object). An application context can be held open over
            several requests by your own code, so it is not used for this.
            Outside a request (a tool with an application context only),
            Alerta's two queries run as released.
  nosentry  Alerta starts the Sentry SDK even with no SENTRY_DSN, which
            wraps every request. When no DSN is set, only Alerta's own
            start-up call (in alerta.app, when it is imported) is left
            out. Every other call of sentry_sdk.init, as from a plugin of
            your own, goes to the SDK, during and after the start.

Written against Alerta 9.1.0 (f4312727). The names it replaces are checked
at start-up: a release that moves one of them stops the server with a
message instead of running half-patched. Standard library plus what Alerta
already installs.
"""

import atexit
import logging
import os
import select
import threading
import time

SAFE = ("reuse", "nolog", "onecount", "nosentry")
ALL = SAFE + ("hot", "noecho", "savedq")
FEATURES = set()
for _name in os.environ.get("ALERTA_FAST", "safe").split(","):
    if _name == "safe":
        FEATURES.update(SAFE)
    elif _name in ALL:
        FEATURES.add(_name)
    elif _name:
        raise SystemExit(f"ALERTA_FAST: unknown patch {_name!r}; known: safe, {', '.join(ALL)}")
MAX_AGE = float(os.environ.get("ALERTA_FAST_CONN_MAX_AGE", "300"))
FLUSH_SECONDS = float(os.environ.get("ALERTA_FAST_FLUSH_SECONDS", "1.0"))
log = logging.getLogger("alerta.fast")

from flask import g, has_request_context, request  # noqa: E402
from psycopg2 import extensions as _ext  # noqa: E402

if "nosentry" in FEATURES and not os.environ.get("SENTRY_DSN"):
    import sys
    import sentry_sdk

    _sdk_init = sentry_sdk.init

    def _init_except_stock_startup(*args, **kwargs):
        # Stock Alerta 9.1.0 initializes an unconfigured SDK when its module loads.
        caller = sys._getframe(1)
        if (sentry_sdk.init is _init_except_stock_startup and not os.environ.get("SENTRY_DSN")
                and caller.f_code.co_name == "<module>" and caller.f_globals.get("__name__") == "alerta.app"
                and not args and kwargs.keys() == {"integrations", "release"}):
            return None
        return _sdk_init(*args, **kwargs)

    sentry_sdk.init = _init_except_stock_startup
    try:
        import alerta.app  # noqa: E402
    finally:
        if sentry_sdk.init is _init_except_stock_startup:
            sentry_sdk.init = _sdk_init
else:
    import alerta.app  # noqa: E402

from alerta.database.backends.postgres import base as _pg  # noqa: E402
from alerta.models import metrics as _metrics  # noqa: E402

Backend = _pg.Backend


def _require(owner, *names):
    for name in names:
        if not callable(getattr(owner, name, None)):
            raise SystemExit(f"alerta_fast: {owner.__name__}.{name} not found; this Alerta "
                             "release is not the one the patches were written for")


# ------------------------------------------------------------------ reuse

_local = threading.local()


def _discard(conn):
    try:
        conn.close()
    except Exception:
        pass


def _usable(conn):
    """False when the server closed the connection or sent anything while it
    was idle (a shutdown or termination message, or end of file), or when
    the session is read-only by the server's own report. Both are read
    from what the client already has: no round trip."""
    try:
        if conn.closed or select.select([conn.fileno()], [], [], 0)[0]:
            return False
        status = conn.info.parameter_status
        return status("in_hot_standby") != "on" and status("default_transaction_read_only") != "on"
    except Exception:
        return False


def _get_db(self):
    if "db" not in g:
        conn = getattr(_local, "conn", None)
        if conn is not None and _local.pid != os.getpid():
            conn = None  # made before a fork: the parent's, not ours to close
        if conn is not None and (time.monotonic() - _local.born > MAX_AGE or not _usable(conn)):
            _discard(conn)
            conn = None
        if conn is None:
            conn = self.connect()
            _local.conn, _local.born, _local.pid = conn, time.monotonic(), os.getpid()
        g.db = conn
    return g.db


def _teardown_db(self, exc):
    conn = g.pop("db", None)
    if conn is None:
        return
    try:
        # A failed statement (for example a write on a server that is now
        # read-only) or a lost connection: do not keep it. Nothing is retried.
        healthy = conn.info.transaction_status in (_ext.TRANSACTION_STATUS_IDLE,
                                                   _ext.TRANSACTION_STATUS_INTRANS)
        conn.rollback()
    except Exception:
        healthy = False
    if not healthy:
        _discard(conn)
        if getattr(_local, "conn", None) is conn:
            _local.conn = None


if "reuse" in FEATURES:
    _require(Backend, "get_db", "teardown_db", "connect")
    Backend.get_db = _get_db
    Backend.teardown_db = _teardown_db

# -------------------------------------------------------------------- hot

_lock = threading.Lock()        # the pending numbers
_flush_lock = threading.Lock()  # one flush at a time, and the flush connection
_state = {"pid": None, "conn": None}
_keys = {}     # key or key id -> uses
_rows = {}     # (type, group, name) -> [title, description, count, total_time]

_KEY_SQL = ("UPDATE keys SET last_used_time=NOW() at time zone 'utc', count=count + %s "
            "WHERE id=%s OR key=%s")
_ROW_SQL = {
    "timer": ('INSERT INTO metrics ("group", name, title, description, count, total_time, type) '
              "VALUES (%(group)s, %(name)s, %(title)s, %(description)s, %(count)s, %(total_time)s, 'timer') "
              'ON CONFLICT ("group", name, type) DO UPDATE '
              "SET count=metrics.count + %(count)s, total_time=metrics.total_time + %(total_time)s"),
    "counter": ('INSERT INTO metrics ("group", name, title, description, count, type) '
                "VALUES (%(group)s, %(name)s, %(title)s, %(description)s, %(count)s, 'counter') "
                'ON CONFLICT ("group", name, type) DO UPDATE SET count=metrics.count + %(count)s'),
}


def _started():
    """Called with _lock held. One flusher per process, started on first use."""
    pid = os.getpid()
    if _state["pid"] == pid:
        return
    if _state["pid"] is not None:
        # A forked child: the pending numbers and the connection are the parent's.
        _keys.clear()
        _rows.clear()
        _state["conn"] = None
    _state["pid"] = pid
    threading.Thread(target=_flusher, name="alerta-fast-flush", daemon=True).start()
    atexit.register(flush)


def _flusher():
    while True:
        time.sleep(FLUSH_SECONDS)
        flush()


def flush():
    """Write the pending numbers. On failure they are kept for the next try."""
    with _flush_lock:
        with _lock:
            if _state["pid"] != os.getpid() or not (_keys or _rows):
                return
            keys, rows = dict(_keys), {k: list(v) for k, v in _rows.items()}
            _keys.clear()
            _rows.clear()
        try:
            conn = _state["conn"]
            if conn is None or conn.closed:
                conn = _state["conn"] = alerta.app.db.connect()
            cursor = conn.cursor()
            # One order in every worker, so two flushes cannot deadlock.
            for key in sorted(keys):
                cursor.execute(_KEY_SQL, (keys[key], key, key))
            for (kind, group, name) in sorted(rows):
                title, description, count, total_time = rows[(kind, group, name)]
                cursor.execute(_ROW_SQL[kind], {"group": group, "name": name, "title": title,
                                                "description": description, "count": count,
                                                "total_time": total_time})
            conn.commit()
        except Exception as e:
            log.warning("flush failed, will retry: %r", e)
            if _state["conn"] is not None:
                _discard(_state["conn"])
                _state["conn"] = None
            with _lock:
                for key, n in keys.items():
                    _keys[key] = _keys.get(key, 0) + n
                for k, v in rows.items():
                    cur = _rows.setdefault(k, [v[0], v[1], 0, 0])
                    cur[2] += v[2]
                    cur[3] += v[3]


def _update_key_last_used(self, key):
    with _lock:
        _started()
        _keys[key] = _keys.get(key, 0) + 1


def _add_row(kind, metric, count, total_time):
    with _lock:
        _started()
        cur = _rows.setdefault((kind, metric.group, metric.name),
                               [metric.title, metric.description, 0, 0])
        cur[2] += count
        cur[3] += total_time


def _stop_timer(self, start, count=1):
    elapsed = self._time_in_millis() - start
    _add_row("timer", self, count, elapsed)
    self.count = (self.count or 0) + count
    self.total_time = (self.total_time or 0) + elapsed


def _inc(self, count=1):
    _add_row("counter", self, count, 0)
    self.count = (self.count or 0) + count


if "hot" in FEATURES:
    _require(Backend, "update_key_last_used", "connect")
    _require(_metrics.Timer, "stop_timer", "_time_in_millis")
    _require(_metrics.Counter, "inc")
    Backend.update_key_last_used = _update_key_last_used
    _metrics.Timer.stop_timer = _stop_timer
    _metrics.Counter.inc = _inc

# ----------------------------------------------------------------- noecho

if "noecho" in FEATURES:
    _require(Backend, "update_tags", "update_attributes", "_fetchone")
    _orig_update_tags = Backend.update_tags
    _orig_update_attributes = Backend.update_attributes

    def _same_tags(stored, sent):
        """The same set of strings, and neither list has a tag twice."""
        if not isinstance(stored, list) or not isinstance(sent, list):
            return False
        if not all(isinstance(t, str) for t in stored + sent):
            return False
        return len(set(sent)) == len(sent) and sorted(stored) == sorted(sent)

    def _noecho_update_tags(self, id, tags):
        row = self._fetchone("SELECT * FROM alerts WHERE id=%(id)s", {"id": id})
        if row is not None and _same_tags(row.tags, tags):
            self.get_db().commit()  # Alerta's _updateone commits here
            return row
        return _orig_update_tags(self, id, tags)

    def _noecho_update_attributes(self, id, old_attrs, new_attrs):
        # Alerta's own first two lines; its function repeats them with the
        # same result.
        old_attrs.update(new_attrs)
        attrs = {k: v for k, v in old_attrs.items() if v is not None}
        # `attrs` is sent as in Alerta's UPDATE. A document that cannot be
        # sent, or that PostgreSQL refuses, fails here as it would there.
        row = self._fetchone("""
            SELECT attributes, attributes::text = (%(attrs)s::jsonb)::text AS same
              FROM alerts WHERE id=%(id)s
        """, {"id": id, "attrs": attrs})
        if row is not None and row.same:
            self.get_db().commit()  # Alerta's _updateone commits here
            return row.attributes
        return _orig_update_attributes(self, id, old_attrs, new_attrs)

    Backend.update_tags = _noecho_update_tags
    Backend.update_attributes = _noecho_update_attributes

# ----------------------------------------------------------------- savedq

if "savedq" in FEATURES:
    import hashlib
    from collections import OrderedDict

    from alerta.database.backends.postgres import queryparser as _qp

    _QP_BLOB = "346ff01a80afd08d907f9ac276127a2963dc7726"  # Alerta 9.1.0 (f4312727)
    SAVEDQ_ENTRIES = int(os.environ.get("ALERTA_FAST_SAVEDQ_ENTRIES", "256"))
    SAVEDQ_BYTES = int(os.environ.get("ALERTA_FAST_SAVEDQ_BYTES", "2048"))
    if SAVEDQ_ENTRIES < 1 or SAVEDQ_BYTES < 1:
        raise SystemExit("alerta_fast: ALERTA_FAST_SAVEDQ_ENTRIES and ALERTA_FAST_SAVEDQ_BYTES must be 1 or more")
    with open(_qp.__file__, "rb") as _file:
        _data = _file.read()
    if hashlib.sha1(b"blob %d\0" % len(_data) + _data).hexdigest() != _QP_BLOB:
        raise SystemExit("alerta_fast: savedq is written for the query parser of Alerta 9.1.0; the installed "
                         "alerta/database/backends/postgres/queryparser.py is another file")
    del _data
    _require(_qp.QueryParser, "parse")
    _orig_parse = _qp.QueryParser.parse
    _savedq = OrderedDict()  # (text, field) -> (condition, ((name, value), ...)); the last used at the end
    _savedq_lock = threading.Lock()
    _parse_lock = threading.Lock()  # one thread at a time in Alerta's parser
    savedq_stats = {"hit": 0, "miss": 0, "refused": 0, "not_kept": 0, "dropped": 0, "off": 0}
    savedq_state = {"on": True, "why_off": None}
    _SAVEDQ_FUNCTIONS = (("UnaryOperation", "__init__"), ("BinaryOperation", "__init__"), ("SearchTerm", "__init__"),
                         *((name, "to_sql") for name in ("SearchModifier", "SearchAnd", "SearchOr", "SearchNot",
                                                         "SearchTerm")))

    def _savedq_functions():
        """The parser's functions, as the module has them now."""
        return [_qp.__dict__.get("_next_param"),
                *(getattr(_qp.__dict__.get(owner), "__dict__", {}).get(name) for owner, name in _SAVEDQ_FUNCTIONS)]

    def _savedq_parts():
        """What a kept answer depends on, other than the two texts."""
        return [id(_qp.__dict__.get("query_expr")), *map(id, _savedq_functions())]

    def savedq_disable(why="savedq_disable() was called"):
        """Turns `savedq` off for this worker and forgets what it kept.
        For code that changes Alerta's parser; see the file header."""
        with _savedq_lock:
            if savedq_state["on"]:
                savedq_state.update(on=False, why_off=str(why)[:200])
                log.warning("alerta_fast: savedq is off: %s", savedq_state["why_off"])
            _savedq.clear()

    _file = os.path.realpath(_qp.__file__)
    _own = [f for f in [_orig_parse, *_savedq_functions()]
            if os.path.realpath(getattr(getattr(f, "__code__", None), "co_filename", "")) != _file]
    _stock = [_qp.query_expr, *_savedq_functions()]  # held, so that an id is not used again
    _stock_parts = _savedq_parts()

    def _savedq_parse(self, query, default_field=None):
        field = default_field or _qp.QueryParser.DEFAULT_FIELD
        if savedq_state["on"] and _savedq_parts() != _stock_parts:
            savedq_disable("the parser's grammar object or one of its functions was replaced")
        if not savedq_state["on"]:
            with _savedq_lock:
                savedq_stats["off"] += 1
            with _parse_lock:
                return _orig_parse(self, query, default_field)
        if (type(self) is not _qp.QueryParser
                or type(query) is not str or type(field) is not str
                # A character is 1 to 4 bytes: no encoding of a text that is too long.
                or len(query) + len(field) > SAVEDQ_BYTES
                or len(query.encode("utf-8", "surrogatepass"))
                + len(field.encode("utf-8", "surrogatepass")) > SAVEDQ_BYTES):
            with _savedq_lock:
                savedq_stats["not_kept"] += 1
            with _parse_lock:
                return _orig_parse(self, query, default_field)
        key = (query, field)

        def kept_answer():
            with _savedq_lock:
                kept = _savedq.get(key)
                if kept is not None:
                    _savedq.move_to_end(key)
                    savedq_stats["hit"] += 1
            return kept

        kept = kept_answer()
        if kept is not None:
            return kept[0], dict(kept[1])
        with _parse_lock:
            # Another thread parsed the same pair while this one waited.
            kept = kept_answer()
            if kept is not None:
                return kept[0], dict(kept[1])
            try:
                sql, params = _orig_parse(self, query, default_field)
            except BaseException:
                with _savedq_lock:
                    savedq_stats["refused"] += 1
                raise
            keep = (type(sql) is str and type(params) is dict
                    and all(type(name) is str and type(value) is str for name, value in params.items())
                    and _savedq_parts() == _stock_parts)
            with _savedq_lock:
                if keep and savedq_state["on"]:
                    savedq_stats["miss"] += 1
                    _savedq[key] = (sql, tuple(params.items()))
                    while len(_savedq) > SAVEDQ_ENTRIES:
                        _savedq.popitem(last=False)
                        savedq_stats["dropped"] += 1
                else:
                    savedq_stats["not_kept"] += 1
        return sql, params

    if _own:
        # The installation's own parser code was there first: it stays, and
        # nothing is put around it.
        savedq_disable("QueryParser.parse or one of the parser's functions is not Alerta's at start-up")
    else:
        _qp.QueryParser.parse = _savedq_parse

# ------------------------------------------------------------------ nolog

if "nolog" in FEATURES:
    _require(Backend, "_log")
    _orig_log = Backend._log

    def _log(self, cursor, query, vars):
        if _pg.current_app.logger.isEnabledFor(logging.DEBUG):
            _orig_log(self, cursor, query, vars)

    Backend._log = _log

# --------------------------------------------------------------- onecount

if "onecount" in FEATURES:
    _require(Backend, "get_counts_by_severity", "get_counts_by_status", "_fetchall")
    _orig_by = {"severity": Backend.get_counts_by_severity, "status": Backend.get_counts_by_status}

    def _counts(self, query, which):
        if query is None or not has_request_context():
            return _orig_by[which](self, query)
        # An application context can span multiple requests in a custom integration.
        cache = request.__dict__.setdefault("_alerta_fast_counts", {})
        key = (query.where, repr(query.vars))
        if key not in cache:
            select = f"""
                SELECT severity, status, COUNT(*) FROM alerts
                 WHERE {query.where}
                GROUP BY severity, status
            """
            both = {"severity": {}, "status": {}}
            for row in self._fetchall(select, query.vars):
                both["severity"][row.severity] = both["severity"].get(row.severity, 0) + row.count
                both["status"][row.status] = both["status"].get(row.status, 0) + row.count
            cache[key] = both
        return dict(cache[key][which])

    Backend.get_counts_by_severity = lambda self, query=None: _counts(self, query, "severity")
    Backend.get_counts_by_status = lambda self, query=None: _counts(self, query, "status")

# -------------------------------------------------------------------- app

log.warning("alerta_fast patches: %s", ",".join(f for f in ALL if f in FEATURES) or "none")
app = alerta.app.create_app()
