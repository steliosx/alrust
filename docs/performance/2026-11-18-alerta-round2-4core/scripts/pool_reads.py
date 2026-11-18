#!/usr/bin/env python3
"""Pool-size sweep for reads at 32 clients: alrust with ALRUST_DB_MAX_CONNECTIONS
unset (default 16), 32 and 64. All five read cases, 3 reps, arm order rotated.
The default arm is rerun here (not borrowed from test1) so all three share one
interleaved schedule."""
from common import RAW, READ_CASES, REPS, ROOT, config_db, ensure_clones, file_done, log, remove_apps, rotate
from reads import record_failure, run_parity, run_timed_read

POOL_DIR = RAW / "pool-reads"
POOL_WARM = RAW / "pool-reads-warmup"
POOL_ARMS = ["alrust", "alrust-p32", "alrust-p64"]
LEVEL = 32


def pool_path(arm, case, rep):
    return POOL_DIR / f"{arm}_{case}_c{LEVEL}_r{rep}.json"


def pool_expected():
    return [(a, c, r) for r in REPS for c, _ in READ_CASES for a in POOL_ARMS]


def pool_complete():
    return all(file_done(pool_path(*x)) for x in pool_expected())


def run_pool_reads():
    POOL_DIR.mkdir(parents=True, exist_ok=True)
    POOL_WARM.mkdir(parents=True, exist_ok=True)
    ensure_clones("t1", POOL_ARMS, "base_seed", 1000)
    run_parity(POOL_ARMS, "t1", READ_CASES, ROOT / "parity-pool.json")
    idx = 0
    for rep in REPS:
        for case, path in READ_CASES:
            order = rotate(POOL_ARMS, idx)
            idx += 1
            for arm in order:
                dest = pool_path(arm, case, rep)
                if file_done(dest):
                    continue
                try:
                    run_timed_read(arm, case, path, LEVEL, rep, config_db("t1", arm), dest, POOL_WARM / dest.name, order)
                except Exception as exc:
                    record_failure(dest, str(exc))
    remove_apps()
    log(f"pool reads finished complete={pool_complete()}")
