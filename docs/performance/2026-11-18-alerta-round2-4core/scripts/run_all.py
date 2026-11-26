#!/usr/bin/env python3
"""Drive the tuned bench. Resume skips finished timed JSON files."""
import asyncio
import os
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (
    ALERTA_NAME,
    ALRUST_NAME,
    PG_NAME,
    RAW,
    ROOT,
    any_timed_success,
    append_loadavg,
    assert_sampler_excludes_self,
    capture_environment,
    container_state,
    docker,
    file_done,
    log,
    smoke_and_auth,
    start_postgres,
    verify_prerequisites,
    write_json,
)
from reads import (
    CHOICE_PATH,
    any_test1_cells,
    choose_from_summary,
    final_configs,
    order_self_check,
    refresh_sweep,
    run_sweep,
    run_test1,
    sweep_complete,
    sweep_settled,
    test1_complete,
)
from report import build, scrub_workspace_secrets
from write_client import self_check
from writes import run_test2, test2_mixed_complete, test2_write_complete, write_order_self_check

LOCK = RAW / "runner.lock"


def ensure_affinity():
    want = {6, 7}
    have = set(os.sched_getaffinity(0))
    if have == want:
        return
    if os.environ.get("TUNED_AFFINITY_REEXEC") == "1":
        raise SystemExit(f"CPU affinity is {sorted(have)}; need cores 6 and 7")
    os.environ["TUNED_AFFINITY_REEXEC"] = "1"
    os.execvp("taskset", ["taskset", "-c", "6-7", sys.executable, *sys.argv])


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def acquire_lock():
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    if LOCK.exists():
        text = LOCK.read_text(encoding="utf-8").strip()
        if text.isdigit() and pid_alive(int(text)):
            raise SystemExit(f"runner already active pid {text}")
    LOCK.write_text(str(os.getpid()), encoding="utf-8")


def release_lock():
    if LOCK.exists() and LOCK.read_text(encoding="utf-8").strip() == str(os.getpid()):
        LOCK.unlink()


def ensure_seed_link():
    link = ROOT / "seed.py"
    target = Path("/workspace/alerta-bench/seed.py")
    if link.is_symlink() or link.exists():
        return
    link.symlink_to(target)


def write_blocker(problems):
    lines = [
        "# Benchmark not run",
        "",
        "A required tool or image check failed. No timings were invented.",
        "",
    ]
    for problem in problems:
        lines.append(f"- {problem}")
    lines.append("")
    (ROOT / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")


def write_started(meta):
    text = "\n".join([
        "# TUNED like-for-like benchmark (in progress)",
        "",
        "Docker, the pinned images, oha 1.17.0, and the alrust binary hash were verified.",
        "Timed cells are written under raw/ as they finish. This file is replaced when the run ends.",
        "No result from another benchmark directory is copied into this run.",
        "",
        f"Docker: {meta.get('docker_version')}",
        f"oha: {meta.get('oha_version')}",
        f"nproc: {meta.get('nproc')}",
        f"alrust binary sha256: {meta.get('alrust_binary_sha256')}",
        f"uwsgi: {meta.get('uwsgi_version')}",
        "",
    ])
    (ROOT / "REPORT.md").write_text(text, encoding="utf-8")


def check_pins(auth):
    for probe in auth["probes"]:
        inside = probe["inside"]
        if inside.get("nproc") != "4" or inside.get("cpuset_cpus_effective") != "0-3":
            raise RuntimeError(f"{probe['config']} inside pins {inside}")
        if int(probe["nano_cpus"]) != 4_000_000_000 or probe["cpuset_cpus"] != "0-3":
            raise RuntimeError(f"{probe['config']} inspect pins nano={probe['nano_cpus']} set={probe['cpuset_cpus']}")
    pg = auth["postgres_inside"]
    if pg.get("nproc") != "2" or pg.get("cpuset_cpus_effective") != "4-5":
        raise RuntimeError(f"postgres inside pins {pg}")
    if int(auth["postgres_nano_cpus"]) != 2_000_000_000 or auth["postgres_cpuset_cpus"] != "4-5":
        raise RuntimeError("postgres inspect pins do not match 2 cores on 4-5")


def ensure_choice():
    if file_done(CHOICE_PATH):
        return
    if not sweep_complete():
        return
    summary = refresh_sweep()
    choice = choose_from_summary(summary)
    write_json(CHOICE_PATH, choice)
    log(f"recomputed gunicorn choice {choice.get('chosen_config')} {choice.get('reason')}")


def teardown():
    removed = []
    for name in (ALERTA_NAME, ALRUST_NAME, PG_NAME):
        if container_state(name) != "missing":
            docker(["rm", "-f", name], check=False, timeout=90)
            removed.append(name)
    remaining = [name for name in (ALERTA_NAME, ALRUST_NAME, PG_NAME) if container_state(name) != "missing"]
    ps = docker(["ps", "-a", "--format", "{{.Names}}"], check=False)
    names = [line.strip() for line in (ps.stdout or "").splitlines() if line.strip()]
    doc = {
        "removed": removed,
        "remaining": remaining,
        "docker_ps_names": names,
        "git": "not invoked",
    }
    write_json(ROOT / "teardown.json", doc)
    log(f"teardown removed={removed} remaining={remaining} ps={names}")
    return doc


def suite_complete():
    if not file_done(CHOICE_PATH) or not sweep_settled():
        return False
    ids = final_configs()
    return test1_complete() and test2_write_complete(ids) and test2_mixed_complete(ids)


def main():
    ensure_affinity()
    acquire_lock()
    meta = {}
    teardown_doc = None
    blocker = False
    try:
        ensure_seed_link()
        if not any_timed_success() and (RAW / "progress.txt").exists():
            (RAW / "progress.txt").write_text("", encoding="utf-8")
        log("runner start affinity=" + ",".join(str(c) for c in sorted(os.sched_getaffinity(0))))
        order_self_check()
        write_order_self_check()
        self_check()
        problems, meta = verify_prerequisites()
        write_json(ROOT / "meta.json", meta)
        if problems:
            for problem in problems:
                log("PREREQ " + problem)
            write_blocker(problems)
            blocker = True
            raise SystemExit(2)
        write_started(meta)
        append_loadavg("start")
        start_postgres()
        assert_sampler_excludes_self()
        from common import ensure_templates
        ensure_templates()
        auth = smoke_and_auth()
        check_pins(auth)
        capture_environment(meta, auth)
        if not sweep_settled() and not any_test1_cells():
            append_loadavg("sweep-begin")
            run_sweep()
            append_loadavg("sweep-end")
        elif not sweep_complete():
            log("sweep has recorded failures; keeping the choice and continuing")
            ensure_choice()
        else:
            log("sweep already complete")
            ensure_choice()
        if not test1_complete():
            append_loadavg("test1-begin")
            run_test1()
            append_loadavg("test1-end")
        else:
            log("test1 already complete")
        ids = final_configs()
        if not (test2_write_complete(ids) and test2_mixed_complete(ids)):
            append_loadavg("test2-begin")
            asyncio.run(run_test2())
            append_loadavg("test2-end")
        else:
            log("test2 already complete")
        if suite_complete():
            teardown_doc = teardown()
            log("ALL DONE")
        else:
            log("runner finished incomplete; containers left for resume")
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        log("FATAL runner stopped; containers left unless teardown already ran")
    finally:
        if not blocker:
            try:
                if teardown_doc is None and file_done(ROOT / "teardown.json") and suite_complete():
                    teardown_doc = json_load(ROOT / "teardown.json")
                build(meta, partial=not suite_complete(), teardown=teardown_doc)
                changed = scrub_workspace_secrets()
                if changed:
                    log("scrubbed secrets from " + ",".join(changed))
                log("report written")
            except Exception:
                traceback.print_exc()
        release_lock()


def json_load(path):
    import json
    return json.loads(Path(path).read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
