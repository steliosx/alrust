"""Verify the live application ingress and writable SQL routes."""

import run_lab


def attested_route(mode):
    if mode not in ("local", "primary-aware"):
        raise ValueError("Unsupported operator ingress")
    return {
        "ingress": mode,
        "ingress_endpoints": {"a": "a", "b": "b"},
        "operator_app_by_ingress": {"a": "a", "b": "b" if mode == "local" else "a"},
        "pooled_writer_by_app_site": {"a": "a", "b": "a"},
        "sql_path_by_app_site": {"a": "local", "b": "emulated-wan"},
    }


def probe_routes(lab, db):
    mode = lab.args.ingress
    listener = "http" if mode == "local" else "affinity"
    for site in run_lab.SITES:
        if lab.ingress[site] != (lab.p(site, listener), run_lab.HOST):
            raise ValueError("Operator HTTP ingress does not point to its assigned site listener")
        role = lab.sql(db, "SELECT pg_is_in_recovery()", site)
        if role != ("f" if site == "a" else "t"):
            raise ValueError("Primary/standby roles changed during the operator comparison")
        # This uses the same local PgBouncer endpoint and writer HAProxy as the app.
        # Both routes must actually reach the A port and obtain a writable primary session.
        query = f"SELECT (inet_server_port()={lab.p('a', 'pg')} AND NOT pg_is_in_recovery() AND NOT current_setting('transaction_read_only')::bool)"
        if lab.sql(db, query, site, "pooler") != "t":
            raise ValueError("Operator pooled SQL did not reach writable primary A")
    return attested_route(mode)
