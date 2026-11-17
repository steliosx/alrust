"""Pure checks of the mixed suite (bench_compare.py), kept apart so that
scripts/bench/test_mixed_checks.py can run them without a server.

Time policy. Both servers answer `lastReceiveTime` as UTC with exactly
three fraction digits and a `Z` (`2026-11-16T01:02:03.456Z`), and both
truncate stored times to milliseconds. So a served time must have that
exact form, and must equal its row's `last_receive_time` truncated to the
millisecond. Any other form is a failure, not something to repair.

Standard library only.
"""

import re
from datetime import datetime, timezone

API_TIME = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z")
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _micros(dt):
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    d = dt - _EPOCH
    return (d.days * 86400 + d.seconds) * 1_000_000 + d.microseconds


def parse_api_time(value):
    """Microseconds since 1970 of a served time, or None when it does not
    have the exact served form or is not a real date."""
    if not isinstance(value, str) or not API_TIME.fullmatch(value):
        return None
    try:
        return _micros(datetime.strptime(value[:-1] + "000+0000", "%Y-%m-%dT%H:%M:%S.%f%z"))
    except ValueError:
        return None


def parse_db_time(value):
    """Microseconds since 1970 of a time as PostgreSQL's row_to_json writes
    it (ISO 8601; a value without an offset is UTC), or None."""
    if not isinstance(value, str):
        return None
    try:
        return _micros(datetime.fromisoformat(value))
    except ValueError:
        return None


def served_time_problems(served, by_id):
    """Each served alert's lastReceiveTime against its own row, and the
    served order. `by_id` maps an id to its row with `last_receive_time`.
    Equal times are in order: the order among them is not compared."""
    problems, times = [], []
    for a in served:
        ident = a.get("id")
        api = parse_api_time(a.get("lastReceiveTime"))
        if api is None:
            problems.append(f"{ident}: lastReceiveTime {a.get('lastReceiveTime')!r} "
                            "is not a UTC time with three fraction digits and Z")
            continue
        times.append(api)
        row = by_id.get(ident)
        if row is None:
            continue  # reported by the field comparison
        db = parse_db_time(row.get("last_receive_time"))
        if db is None:
            problems.append(f"{ident}: the row's last_receive_time "
                            f"{row.get('last_receive_time')!r} does not parse")
        elif api != db // 1000 * 1000:
            problems.append(f"{ident}: served lastReceiveTime {a['lastReceiveTime']!r}, "
                            f"row {row['last_receive_time']!r}")
    if any(x < y for x, y in zip(times, times[1:])):
        problems.append("the served lastReceiveTime values are not in descending order")
    return problems


def cell_valid(s, writers, readers):
    """Whether one finished cell is scored. `s` is the cell's record as
    mixed_cell builds it. Everything after the load counts: the clients'
    stop, every status, every list body, the persisted rows, the final
    check and the server still running."""
    bad = sum(s[k]["errors"] + s[k]["non_2xx"] for k in ("writes", "reads"))
    return bool(s["server_alive"] and not s["problems"] and bad == 0
                and s["reads"]["body_problems"] == 0
                and (writers == 0 or s["writes"]["ok"] > 0)
                and (readers == 0 or s["reads"]["ok"] > 0)
                and s["persisted"]["ok"] and s["final_check"]["ok"])
