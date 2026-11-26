#!/usr/bin/env python3
"""Flag tests whose wall-clock span in progress.txt far exceeds their measured time."""
import json, re, sys
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
    if (mm := re.match(r"timed (\S+)$", msg)):
        cur = ("test1", mm.group(1), ts)
    elif (mm := re.match(r"write (empty|seeded) c=(\d+) rep=(\d) (\S+) ", msg)):
        cur = ("test2", f"{mm.group(1)}_{mm.group(4)}_c{mm.group(2)}_r{mm.group(3)}", ts)
    elif (mm := re.match(r"mixed rep=(\d) (\S+) ", msg)):
        cur = ("mixed", f"{mm.group(2)}_r{mm.group(1)}", ts)
    elif cur and (msg.strip().startswith("rps=") or msg.strip().startswith("accepted=") or msg.strip().startswith("write rps=")):
        recs[(cur[0], cur[1])] = (cur[2], ts, msg.strip())
        cur = None
out = []
for (suite, name), (t0, t1, msg) in sorted(recs.items()):
    span = (t1 - t0).total_seconds()
    d = {"test1": RAW / "test1" / f"{name}.json", "test2": RAW / "test2" / f"{name}.json", "mixed": RAW / "test2-mixed" / f"{name}.json"}[suite]
    if not d.exists():
        continue
    j = json.loads(d.read_text())
    if j.get("when") != t1.strftime("%Y-%m-%dT%H:%M:%S%z"):
        # log record is not the one in the current file
        stale = True
    else:
        stale = False
    if suite == "test1":
        measured = j["oha"]["summary"]["total"]
        sc = j["oha"].get("statusCodeDistribution", {})
        bad = sum(v for k, v in sc.items() if not k.startswith("2")) + sum(j["oha"].get("errorDistribution", {}).values())
        rps = j["oha"]["summary"]["requestsPerSec"]
        overhead = 1.5
    elif suite == "test2":
        measured = j["timed"]["elapsed_s"] + j["warmup"]["elapsed_s"]
        bad = j["timed"]["error_count"] + j["warmup"]["error_count"] + (j["timed"]["offered"] - j["timed"]["accepted"])
        rps = j["timed"]["rps"]
        overhead = 15 if "gunicorn" in name else 6
    else:
        measured = j["write"]["elapsed_s"] + j["warmup_write"]["elapsed_s"]
        bad = j["write"]["error_count"] + j["read"]["error_count"] + (j["write"]["offered"] - j["write"]["accepted"])
        rps = j["write"]["rps"]
        overhead = 20 if "gunicorn" in name else 8
    excess = span - measured - overhead
    db = j.get("db_connections", {})
    out.append(dict(suite=suite, name=name, start=t0.strftime("%H:%M:%S"), end=t1.strftime("%m-%d %H:%M:%S"), span=round(span), measured=round(measured, 1), excess=round(excess), bad=bad, rps=round(rps, 1), stale=stale, peak_db=db.get("peak_total_backends"), samp=db.get("sample_count"), win=round(db.get("sample_window_s", 0), 1)))
flag = [r for r in out if r["excess"] > 20 or r["bad"] > 0 or r["stale"]]
mode = sys.argv[1] if len(sys.argv) > 1 else "flag"
for r in (flag if mode == "flag" else out):
    print(json.dumps(r))
print("total records", len(out), "flagged", len(flag), file=sys.stderr)
