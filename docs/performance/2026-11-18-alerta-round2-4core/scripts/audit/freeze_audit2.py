#!/usr/bin/env python3
"""Round 2 freeze audit over every timed result file.

Flags a result when any holds:
  - its logged span (progress.txt start line -> result line) is more than 20 s
    past its measured time plus a fixed startup allowance;
  - the DB sampler averaged more than 0.26 s per sample (target 0.2 s);
  - any error / non-2xx (reads, writes) or the mixed rep is invalid;
  - its rate is more than 12% from the median of its 3 reps;
  - (mixed) realtime and monotonic clocks differ by > 1 s over the timed window.
The log record must match the file's own "when" stamp (else: stale).
Usage: freeze_audit2.py [flag|all]  -> JSON lines on stdout.
"""
import json
import re
import statistics
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "raw"
TS = lambda s: datetime.strptime(s, "%Y-%m-%dT%H:%M:%S%z")
lines = (RAW / "progress.txt").read_text().splitlines()
recs = {}
cur = None
for ln in lines:
    m = re.match(r"(\S+) (.*)$", ln)
    if not m or not re.match(r"\d{4}-", m.group(1)):
        continue
    ts, msg = TS(m.group(1)), m.group(2)
    s = msg.strip()
    if (mm := re.match(r"timed (\S+)$", msg)):
        cur = ("read", mm.group(1), ts)
    elif (mm := re.match(r"write (empty|seeded) c=(\d+) rep=(\d) (\S+) ", msg)):
        cur = ("write", f"{mm.group(1)}_{mm.group(4)}_c{mm.group(2)}_r{mm.group(3)}", ts)
    elif (mm := re.match(r"mixed263 (\S+) (\S+) r(\d) ", msg)):
        cur = ("mixed", f"{mm.group(1)}_{mm.group(2)}_r{mm.group(3)}", ts)
    elif cur and (s.startswith("rps=") or s.startswith("accepted=") or s.startswith("valid=")):
        recs.setdefault((cur[0], cur[1]), []).append((cur[2], ts))
        cur = None
    elif cur and s.startswith("FAIL"):
        cur = None


def find_log(kind, stem, when):
    for t0, t1 in reversed(recs.get((kind, stem), [])):
        if t1.strftime("%Y-%m-%dT%H:%M:%S%z") == when:
            return t0, t1
    return None


rows = []
groups = {}
for sub, kind in (("test1", "read"), ("pool-reads", "read"), ("test2", "write"), ("mixed263", "mixed")):
    d = RAW / sub
    if not d.exists():
        continue
    for p in sorted(d.glob("*.json")):
        if p.name.startswith(("size_", "api-")) or p.name.endswith(".fail.json"):
            continue
        j = json.loads(p.read_text())
        stem = p.stem
        clock_gap = None
        if kind == "read":
            o = j["oha"]
            measured = o["summary"]["total"]
            bad = sum(v for k, v in o.get("statusCodeDistribution", {}).items() if not k.startswith("2")) + sum(o.get("errorDistribution", {}).values())
            rate = o["summary"]["requestsPerSec"]
            allowance = 1.5
            key = (sub, j["config"], j["case"], j["concurrency"])
        elif kind == "write":
            measured = j["timed"]["elapsed_s"] + j["warmup"]["elapsed_s"]
            bad = j["timed"]["error_count"] + j["warmup"]["error_count"] + (j["timed"]["offered"] - j["timed"]["accepted"])
            rate = j["timed"]["rps"]
            allowance = 15 if "gunicorn" in stem else 6
            key = (sub, j["condition"], j["config"], j["concurrency"])
        else:
            measured = j["warm_s"] + j["timed_s"] + 2.0
            bad = 0 if j["valid"] else 1
            rate = (j["writers"] or j["readers"])["timed_rate"]
            allowance = 20 if "gunicorn" in stem else 8
            key = (sub, j["cell"], j["config"])
            cpu = j.get("cpu") or {}
            if "window_monotonic_s" in cpu:
                clock_gap = cpu["window_s"] - cpu["window_monotonic_s"]
        lg = find_log(kind, stem, j.get("when"))
        stale = lg is None
        span = (lg[1] - lg[0]).total_seconds() if lg else None
        excess = (span - measured - allowance) if span is not None else None
        db = j.get("db_connections") or {}
        interval = db.get("sample_interval_actual_s")
        r = dict(suite=sub, name=stem, file=str(p.relative_to(ROOT)), span=span, measured=round(measured, 1),
                 excess=None if excess is None else round(excess, 1), bad=bad, rate=rate, stale=stale,
                 sampler_interval=interval, clock_gap=clock_gap)
        rows.append(r)
        groups.setdefault(key, []).append(r)
for key, rs in groups.items():
    med = statistics.median([r["rate"] for r in rs]) if rs else None
    for r in rs:
        r["dev_from_median"] = (abs(r["rate"] - med) / med) if med else None
flags = []
for r in rows:
    why = []
    if r["stale"]:
        why.append("no matching log record")
    if r["excess"] is not None and r["excess"] > 20:
        why.append(f"span excess {r['excess']}s")
    if r["sampler_interval"] is not None and r["sampler_interval"] > 0.26:
        why.append(f"sampler {r['sampler_interval']:.3f}s/sample")
    if r["bad"]:
        why.append(f"errors/invalid {r['bad']}")
    if r["dev_from_median"] is not None and r["dev_from_median"] > 0.12:
        why.append(f"rate {r['dev_from_median']*100:.1f}% from rep median")
    if r["clock_gap"] is not None and abs(r["clock_gap"]) > 1.0:
        why.append(f"clock gap {r['clock_gap']:.2f}s")
    r["why"] = why
    if why:
        flags.append(r)
mode = sys.argv[1] if len(sys.argv) > 1 else "flag"
for r in (flags if mode == "flag" else rows):
    print(json.dumps(r))
print("total records", len(rows), "flagged", len(flags), file=sys.stderr)
