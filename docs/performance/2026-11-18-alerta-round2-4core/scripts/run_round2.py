#!/usr/bin/env python3
"""Round 2 driver. Resume skips finished timed JSON files.
Order: prereqs -> postgres -> templates -> smoke/auth -> test1 reads (3 servers,
c=1/8/16/32) -> pool reads (c=32) -> writes (c=1/8/16/32) -> mixed263 -> teardown.
The gunicorn worker count is carried over from the tuned run's sweep (8x4)."""
import asyncio
import os
import sys
import time
import traceback

os.environ.setdefault("RERUN_REBUILD", "1")  # templates were rebuilt after the 15:56 box reset
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_all  # noqa: E402  (ensure_affinity, lock, teardown, check_pins)
from common import (RAW, ROOT, append_loadavg, assert_sampler_excludes_self, capture_environment,  # noqa: E402
                    ensure_templates, file_done, log, smoke_and_auth, start_postgres, verify_prerequisites, write_json)
from reads import CHOICE_PATH, final_configs, order_self_check, run_test1, test1_complete  # noqa: E402
from writes import run_test2, test2_write_complete, write_order_self_check  # noqa: E402
from write_client import self_check  # noqa: E402
from pool_reads import pool_complete, run_pool_reads  # noqa: E402
from mixed263 import mixed263_complete, run_mixed263  # noqa: E402

STAGES = os.environ.get("R2_STAGES", "test1,pool,writes,mixed").split(",")


def main():
    run_all.ensure_affinity()
    run_all.acquire_lock()
    try:
        log("round2 runner start stages=" + ",".join(STAGES))
        order_self_check()
        write_order_self_check()
        self_check()
        problems, meta = verify_prerequisites()
        write_json(ROOT / "meta.json", meta)
        if problems:
            for p in problems:
                log("PREREQ " + p)
            raise SystemExit(2)
        append_loadavg("start")
        start_postgres()
        assert_sampler_excludes_self()
        ensure_templates()
        auth = smoke_and_auth()
        run_all.check_pins(auth)
        capture_environment(meta, auth)
        if not file_done(CHOICE_PATH):
            write_json(CHOICE_PATH, {
                "chosen_config": "alerta-gunicorn-8",
                "reason": "carried over from the tuned run's sweep (/workspace/alerta-bench-tuned/raw/chosen-gunicorn.json: 4/8/16 workers x 4 threads, list c=32, 8 highest median). Not re-swept in round 2.",
            })
        log("final configs " + ",".join(final_configs()))
        if "test1" in STAGES and not test1_complete():
            append_loadavg("test1-begin"); run_test1(); append_loadavg("test1-end")
        if "pool" in STAGES and not pool_complete():
            append_loadavg("pool-begin"); run_pool_reads(); append_loadavg("pool-end")
        if "writes" in STAGES and not test2_write_complete(final_configs()):
            append_loadavg("writes-begin"); asyncio.run(run_test2()); append_loadavg("writes-end")
        if "mixed" in STAGES and not mixed263_complete():
            append_loadavg("mixed-begin"); run_mixed263(); append_loadavg("mixed-end")
        log("round2 runner finished test1=%s pool=%s writes=%s mixed=%s" % (
            test1_complete(), pool_complete(), test2_write_complete(final_configs()), mixed263_complete()))
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        log("FATAL round2 runner stopped")
    finally:
        run_all.release_lock()


if __name__ == "__main__":
    main()
