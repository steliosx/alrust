#!/usr/bin/env python3
"""Traffic for the CI check of scripts/bench/alerta_checkup.sql
(.github/workflows/alerta-checkup.yml): stock Alerta 9.1.0 with its
default plugins, driven with Flask's test client on a throwaway database.
Run with the reference Alerta environment's Python and DATABASE_URL set:

    <venv>/bin/python -I scripts/bench/checkup_ci.py prime
    <venv>/bin/python -I scripts/bench/checkup_ci.py load

prime: Alerta makes its tables at start; one new alert and one alert list,
so the `metrics` timers exist before the checkup's first snapshot.
load:  the checkup's wait (CHECKUP_WAIT): RECEIVES duplicates of that
alert and QUERIES alert lists, then a pause so PostgreSQL's statistics
are flushed. Each request is checked; a wrong answer exits 1.
"""
import sys
import time

from alerta.app import create_app

RECEIVES, QUERIES = 20, 10
ALERT = {"resource": "checkup-host", "event": "CheckupEvent", "environment": "Production",
         "severity": "major", "service": ["checkup"], "attributes": {"zone": "lab"}}


def main(mode):
    client = create_app().test_client()
    receives, queries = (1, 1) if mode == "prime" else (RECEIVES, QUERIES)
    for _ in range(receives):
        r = client.post("/alert", json=ALERT)
        if r.status_code != 201:
            sys.exit(f"POST /alert gave {r.status_code}: {r.get_data(as_text=True)[:200]}")
    for _ in range(queries):
        r = client.get("/alerts")
        if r.status_code != 200 or (r.get_json(silent=True) or {}).get("status") != "ok":
            sys.exit(f"GET /alerts gave {r.status_code}")
    if mode == "load":
        time.sleep(2)  # backends flush their statistics when they end; give it time
    print(f"checkup_ci {mode}: {receives} receives, {queries} alert lists")


if __name__ == "__main__":
    if sys.argv[1:] not in (["prime"], ["load"]):
        sys.exit(__doc__)
    main(sys.argv[1])
