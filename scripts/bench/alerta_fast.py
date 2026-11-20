"""alerta_fast: start-up patches for an unmodified Alerta 9.1.0 (PostgreSQL).

Not a fork: Alerta is installed as released, and this one file replaces a
few of its functions when the server starts. Serve it in place of
`alerta.app:create_app()`:

    gunicorn --pythonpath <this directory> ... alerta_fast:app

ALERTA_FAST selects the patches, comma-separated; the default is "all".
  reuse     One PostgreSQL connection per worker thread, kept between
            requests, in place of a new connection per request. A request
            ends with ROLLBACK (Alerta commits its own writes), and a
            connection that fails it, or is older than
            ALERTA_FAST_CONN_MAX_AGE seconds (default 300), is replaced.
  hot       The two writes that every request makes to one shared row are
            collected in the process and written about once a second
            (ALERTA_FAST_FLUSH_SECONDS, default 1.0) by one thread per
            worker on its own connection: the API key's use count and
            last-used time, and the `metrics` timers and counters. Key
            verification itself is not cached. `GET /keys` and
            `/management/metrics` are late by up to that interval, the
            last-used time is the time of the write, and a worker that is
            killed loses what it had not written. A clean stop writes it.
  nolog     Alerta formats every SQL statement for a debug message before
            the logger checks its level. Skip that unless DEBUG is on.
  onecount  The list and the count read severity and status totals with
            two scans of the same rows. Read both with one grouped query.
  nosentry  Alerta starts the Sentry SDK even with no SENTRY_DSN, which
            wraps every request. Do not start it when no DSN is set.

Written against Alerta 9.1.0 (f4312727). The names it replaces are checked
at start-up: a release that moves one of them stops the server with a
message instead of running half-patched. Standard library plus what Alerta
already installs.
"""

import atexit
import logging
import os
import threading
import time

ALL = ("reuse", "hot", "nolog", "onecount", "nosentry")
_asked = [f for f in os.environ.get("ALERTA_FAST", "all").split(",") if f]
FEATURES = set(ALL) if "all" in _asked else set(_asked)
if FEATURES - set(ALL):
    raise SystemExit(f"ALERTA_FAST: unknown patches {sorted(FEATURES - set(ALL))}; known: {ALL}")
MAX_AGE = float(os.environ.get("ALERTA_FAST_CONN_MAX_AGE", "300"))
FLUSH_SECONDS = float(os.environ.get("ALERTA_FAST_FLUSH_SECONDS", "1.0"))
log = logging.getLogger("alerta.fast")

if "nosentry" in FEATURES and not os.environ.get("SENTRY_DSN"):
    import sentry_sdk

    sentry_sdk.init = lambda *args, **kwargs: None  # alerta.app calls it at import

from flask import g, has_app_context  # noqa: E402

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


def _get_db(self):
    if "db" not in g:
        conn = getattr(_local, "conn", None)
        if conn is not None and _local.pid != os.getpid():
            conn = None  # made before a fork: the parent's, not ours to close
        if conn is not None and (conn.closed or time.monotonic() - _local.born > MAX_AGE):
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
        conn.rollback()
    except Exception:
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
        if query is None or not has_app_context():
            return _orig_by[which](self, query)
        cache = g.setdefault("alerta_fast_counts", {})
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
