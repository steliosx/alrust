"""Run the serial cache experiment once on a GitHub-hosted VM. No network calls."""

from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import time

from cache_replay_evidence import pack

HERE = Path(__file__).resolve().parent
# Each row uses the same load and fixture across both arms. Counterbalance
# order and rotate synthetic tenants using the same seeds in each comparison.
# label, profile, cadence, feedback, environments, newest, write route, repetitions
CASES = (
    ("aligned", "shared-5s", "aligned", False, False, False, "proxy", 4),
    ("spread", "shared-5s", "spread", False, False, False, "proxy", 4),
    ("pairs-0", "shared-5s", "paired-0", False, False, False, "proxy", 4),
    ("pairs-500", "shared-5s", "paired-500", False, False, False, "proxy", 4),
    ("pairs-750", "shared-5s", "paired-750", False, False, False, "proxy", 4),
    ("personal", "personal-5s", "aligned", False, False, False, "proxy", 2),
    ("personal-spread", "personal-5s", "spread", False, False, False, "proxy", 2),
    ("feedback", "shared-5s", "aligned", True, False, False, "proxy", 2),
    ("journey", "shared-5s", "aligned", True, True, False, "proxy", 2),
    ("journey-pairs-500", "shared-5s", "paired-500", True, True, False, "proxy", 2),
    ("writes-direct", "shared-5s", "aligned", False, False, False, "direct", 2),
    ("mixed-newest", "mixed", "spread", False, False, True, "proxy", 2),
)
SEEDS = (12, 13, 14, 15)


def schedule():
    for index, seed in enumerate(SEEDS):
        active = [row for row in CASES if index < row[-1]]
        # Interleave repeats instead of completing every repeat of the first case first.
        start = (index * 3) % len(active)
        ordered = active[start:] + active[:start]
        for row in ordered if index % 2 == 0 else ordered[::-1]:
            yield seed, row


def now():
    return datetime.now(timezone.utc).isoformat()


def save(path, value):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, indent=2) + "\n")
    tmp.replace(path)


def source_manifest():
    sources = [
        HERE.parent / "dual_site_quorum.py",
        *sorted(HERE.glob("*.py")),
        *sorted(HERE.glob("*.cfg")),
        *sorted(HERE.glob("*.vcl")),
        *sorted(HERE.glob("*.sh")),
        HERE / "fixture/alerta_fast.py",
        HERE / "patroni-extra.lock",
    ]
    return {str(path.relative_to(HERE.parent)): sha256(path.read_bytes()).hexdigest() for path in sources}


def command(run, private, seed, case, tmp):
    label, profile, cadence, feedback, environments, newest, route, _ = case
    venv = Path(os.environ["BENCH_ALERTA_VENV"])
    pg = Path(os.environ["BENCH_PG_BIN"])
    binary = Path(os.environ["BENCH_WORK"]) / "bin"
    env = {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "TMPDIR": str(tmp),
        "GITHUB_ACTIONS": "true",
        "RUNNER_ENVIRONMENT": "github-hosted",
        "REPLAY_HOSTED": "1",
        "REPLAY_PROFILE": profile,
        "REPLAY_CADENCE": cadence,
        "REPLAY_SEED": str(seed),
        "REPLAY_FEEDBACK": str(int(feedback)),
        "REPLAY_ENVIRONMENTS": str(int(environments)),
        "REPLAY_NEWEST": str(int(newest)),
        "REPLAY_WRITE_ROUTE": route,
        "REPLAY_PROBES": "1",
        "REPLAY_PROBE_ROUTE": "dashboard",
    }
    return [
        "sudo",
        "-n",
        "systemd-run",
        "--scope",
        "--quiet",
        "--unit",
        f"alerta-{run}-{label}-{seed}.scope",
        "-p",
        "CPUQuota=200%",
        "-p",
        "MemoryMax=4G",
        "-p",
        "MemorySwapMax=0",
        "-p",
        "RuntimeMaxSec=650",
        "env",
        "-i",
        *(f"{key}={value}" for key, value in env.items()),
        "/usr/bin/unshare",
        "--net",
        "--pid",
        "--fork",
        "--kill-child",
        "--mount-proc",
        "/bin/bash",
        str(HERE / "hosted-netem.sh"),
        os.readlink("/proc/self/ns/net"),
        str(os.getuid()),
        str(os.getgid()),
        "/usr/bin/timeout",
        "--signal=INT",
        "--kill-after=25s",
        "600",
        str(venv / "bin/python"),
        "-B",
        str(HERE / "run_cache_replay.py"),
        "--etcd",
        str(binary / "etcd"),
        "--haproxy",
        str(binary / "haproxy"),
        "--pgbouncer",
        os.environ["BENCH_PGBOUNCER"],
        "--pg-bin",
        str(pg),
        "--psql",
        str(pg / "psql"),
        "--venv",
        str(venv),
        "--addon",
        str(HERE / "fixture/alerta_fast.py"),
        "--output",
        str(private),
        "--features",
        "reuse+onecount+nolog+nosentry",
        "--ingress",
        "primary-aware",
        "--dashboard-encoding",
        "gzip",
        "--read-filter",
        "all",
        "--page-size",
        "100",
        "--seed-alerts",
        "32",
        "--bulk-alerts",
        "240",
        "--key-count",
        "13",
        "--client-idle",
        "3",
        "--client-inflight",
        "24",
        "--health-interval",
        "1000",
        "--health-timeout",
        "4000",
        "--pool-query-wait",
        "4",
        "--writer-idle",
        "120",
        "--pool-idle",
        "60",
        "--pool-login-retry",
        "2",
        "--rtt-ms",
        "50",
        "--wal-rtt-ms",
        "100",
        "--dcs-rtt-ms",
        "30",
        "--rates",
        "4",
        "--seconds",
        "50",
        "--repeats",
        "1",
    ]


def versions():
    work = Path(os.environ["BENCH_WORK"])
    commands = {
        "etcd": [work / "bin/etcd", "--version"],
        "haproxy": [work / "bin/haproxy", "-v"],
        "varnish": ["/usr/sbin/varnishd", "-V"],
        "pgbouncer": [os.environ["BENCH_PGBOUNCER"], "--version"],
        "postgres": [Path(os.environ["BENCH_PG_BIN"]) / "postgres", "--version"],
        "patroni": [Path(os.environ["BENCH_ALERTA_VENV"]) / "bin/patroni", "--version"],
        "gunicorn": [Path(os.environ["BENCH_ALERTA_VENV"]) / "bin/gunicorn", "--version"],
        "alerta_source": ["git", "rev-parse", "HEAD:reference/alerta"],
    }
    return {
        name: subprocess.check_output(argv, stderr=subprocess.STDOUT, text=True).splitlines()[0]
        for name, argv in commands.items()
    }


def main():
    if os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted":
        raise SystemExit("This entry point requires a GitHub-hosted runner")
    run = int(os.environ["GITHUB_RUN_ID"])
    out = Path(os.environ["RUNNER_TEMP"]) / "cache-public"
    out.mkdir(mode=0o700)
    root = Path(tempfile.mkdtemp(prefix="alerta-private-"))
    manifest = source_manifest()
    info = {
        "source": os.environ["GITHUB_SHA"],
        "run_id": run,
        "platform": platform.platform(),
        "runner_image": os.environ.get("ImageVersion"),
        "started": now(),
        "planned": len(list(schedule())),
        "cases": [],
        "manifest": manifest,
        "versions": versions(),
    }
    save(out / "matrix.json", info)
    deadline = time.monotonic() + 170 * 60
    unstarted = 0
    for seed, case in schedule():
        if time.monotonic() + 660 > deadline:
            info["end_reason"] = "remaining time below one case allowance"
            break
        name = f"{case[0]}-{seed}"
        private = root / name
        tmp = Path(tempfile.mkdtemp(prefix="acr-"))
        started = now()
        print(f"Starting {name}", flush=True)
        with (root / f"{name}.log").open("x") as log:
            result = subprocess.run(
                command(run, private, seed, case, tmp), stdout=log, stderr=subprocess.STDOUT
            )
        clean = not any(tmp.iterdir())
        if clean:
            tmp.rmdir()
        p = private / "cache-replay.json"
        report = json.loads(p.read_bytes()) if p.is_file() else {}
        ok = result.returncode == 0 and report.get("valid") is True and clean
        unstarted = unstarted + 1 if not report.get("cells") else 0
        record = {
            "args": {"label": name, "profile": case[1], "cadence": case[2], "seed": seed},
            "started": started,
            "finished": now(),
            "exit_code": result.returncode,
            "runtime_empty": clean,
            "manifest": manifest,
            "addon_sha256": manifest["dual-site/fixture/alerta_fast.py"],
        }
        save(root / f"{name}-control.json", record)
        info["cases"].append(
            {
                "name": name,
                "valid": ok,
                "exit": result.returncode,
                "clean": clean,
                "started": started,
                "finished": record["finished"],
                "cells": len(report.get("cells", [])),
            }
        )
        save(out / "matrix.json", info)
        print(f"{name}: valid={ok} exit={result.returncode} cleanup={clean}", flush=True)
        # Capture independently verified, allowlisted evidence after every pair.
        partial = out / "partial.gz"
        info["verified"] = pack(root, partial)
        partial.replace(out / "samples.json.gz")
        save(out / "matrix.json", info)
        if not clean or unstarted >= 2:
            info["end_reason"] = (
                "cleanup could not be confirmed" if not clean else "two cases could not reach the first arm"
            )
            if not report:
                # Only namespace setup errors, never application or database logs.
                for line in (root / f"{name}.log").read_text(errors="replace").splitlines()[-8:]:
                    if line.startswith(
                        (
                            "unshare:",
                            "setpriv:",
                            "Error: Specified",
                            "RTNETLINK answers:",
                            "Failed to start transient",
                        )
                    ):
                        print(line[:240])
            break
    info["finished"] = now()
    save(out / "matrix.json", info)
    print(json.dumps(info.get("verified", {})), flush=True)
    if len(info["cases"]) != info["planned"] or any(not row["valid"] for row in info["cases"]):
        raise SystemExit("Matrix incomplete; see per-case evidence and exclusions")


if __name__ == "__main__":
    main()
