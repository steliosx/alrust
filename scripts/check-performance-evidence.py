#!/usr/bin/env python3
"""Check the published comparison evidence in docs/performance.

This script checks derived evidence only. It does not run a benchmark,
start a server or open a network connection. It uses only the Python
standard library. Run it with -I and give every file as an argument:

    python3 -I scripts/check-performance-evidence.py \
        --report docs/performance/2026-11-13-alerta-comparison.md \
        --results docs/performance/2026-11-13-results.json \
        --manifest docs/performance/evidence-manifest.json \
        --root .

Checks:
1. For each arm and case in the results JSON, the stored median paired
   ratio and ratio range follow from the stored per-run values.
2. Each results table in the report (one section per arm) shows the
   values that follow from the results JSON: the median of the per-run
   medians for each server (2 decimal places), the median paired ratio
   and the ratio range ("x.xx" and "x.xx-x.xxx").
3. Each section names the same source commit and row count as its arm.
4. With --manifest, each listed file has the listed SHA256 and size.

Exit status 0 means every check passed; 1 means at least one failed.
"""

import argparse
import hashlib
import json
import re
import statistics
import sys
from pathlib import Path

SECTION_FOR_ROLE = {
    "merged_main": "merged main",
    "unmerged_candidate": "unmerged candidate",
}
# The ratio range in the report uses an en dash between the two values.
ROW = re.compile(
    r"^\|\s*(?P<case>[a-z]+)\s*\|\s*(?P<alerta>[0-9.]+)\s*\|\s*(?P<alrust>[0-9.]+)\s*"
    r"\|\s*(?P<ratio>[0-9.]+)x\s*\|\s*(?P<lo>[0-9.]+)–(?P<hi>[0-9.]+)x\s*\|\s*$"
)
SOURCE = re.compile(r"^Source `(?P<sha>[0-9a-f]{40})`, (?P<rows>[0-9,]+) alerts\.$")


class Checker:
    def __init__(self):
        self.failures = []
        self.passes = 0

    def check(self, ok, what):
        if ok:
            self.passes += 1
        else:
            self.failures.append(what)


def sections(report_text):
    """Map each '### <title>' heading to the lines under it."""
    out = {}
    current = None
    for line in report_text.splitlines():
        if line.startswith("### "):
            current = line[4:].strip()
            out[current] = []
        elif line.startswith("## "):
            current = None
        elif current is not None:
            out[current].append(line)
    return out


def check_results(c, results):
    for arm in results["arms"]:
        for case in arm["cases"]:
            name = f"{arm['role']}/{case['case']}"
            ratios = case["paired_alerta_over_alrust_ratios"]
            a = case["alerta_run_medians_ms"]
            r = case["alrust_run_medians_ms"]
            c.check(len(a) == len(r) == len(ratios), f"{name}: run counts differ")
            for i, (x, y, q) in enumerate(zip(a, r, ratios)):
                c.check(abs(x / y - q) < 1e-9, f"{name}: run {i} ratio is not alerta/alrust")
            c.check(
                abs(statistics.median(ratios) - case["median_paired_ratio"]) < 1e-12,
                f"{name}: stored median_paired_ratio does not follow from the runs",
            )
            c.check(
                case["ratio_range"] == [min(ratios), max(ratios)],
                f"{name}: stored ratio_range does not follow from the runs",
            )


def check_report(c, report_text, results):
    secs = sections(report_text)
    for arm in results["arms"]:
        title = SECTION_FOR_ROLE.get(arm["role"])
        c.check(title in secs, f"report: no section for arm {arm['role']}")
        if title not in secs:
            continue
        lines = secs[title]
        src = [m for m in (SOURCE.match(x) for x in lines) if m]
        c.check(len(src) == 1, f"{title}: expected one 'Source' line, found {len(src)}")
        if src:
            c.check(src[0]["sha"] == arm["alrust_head"], f"{title}: source commit differs from the JSON")
            rows = int(src[0]["rows"].replace(",", ""))
            c.check(rows == arm["dataset_rows"], f"{title}: row count differs from the JSON")
        table = {m["case"]: m for m in (ROW.match(x) for x in lines) if m}
        c.check(
            sorted(table) == sorted(x["case"] for x in arm["cases"]),
            f"{title}: table cases {sorted(table)} differ from the JSON",
        )
        for case in arm["cases"]:
            row = table.get(case["case"])
            if row is None:
                continue
            name = f"{title}/{case['case']}"
            want = {
                "alerta": f"{statistics.median(case['alerta_run_medians_ms']):.2f}",
                "alrust": f"{statistics.median(case['alrust_run_medians_ms']):.2f}",
                "ratio": f"{statistics.median(case['paired_alerta_over_alrust_ratios']):.2f}",
                "lo": f"{min(case['paired_alerta_over_alrust_ratios']):.2f}",
                "hi": f"{max(case['paired_alerta_over_alrust_ratios']):.2f}",
            }
            for key, value in want.items():
                c.check(row[key] == value, f"{name}: {key} shows {row[key]}, the JSON gives {value}")


def check_manifest(c, manifest, root):
    for rel, want in manifest["files"].items():
        path = root / rel
        if not path.is_file():
            c.check(False, f"manifest: {rel} is missing")
            continue
        data = path.read_bytes()
        c.check(hashlib.sha256(data).hexdigest() == want["sha256"], f"manifest: {rel} SHA256 differs")
        c.check(len(data) == want["bytes"], f"manifest: {rel} size differs")


def main(argv):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--report", required=True, type=Path)
    p.add_argument("--results", required=True, type=Path)
    p.add_argument("--manifest", type=Path)
    p.add_argument("--root", type=Path, default=Path("."), help="base for manifest paths")
    args = p.parse_args(argv)

    c = Checker()
    results = json.loads(args.results.read_text(encoding="utf-8"))
    check_results(c, results)
    check_report(c, args.report.read_text(encoding="utf-8"), results)
    if args.manifest is not None:
        check_manifest(c, json.loads(args.manifest.read_text(encoding="utf-8")), args.root)

    for f in c.failures:
        print(f"FAIL {f}")
    print(f"{c.passes} checks passed, {len(c.failures)} failed")
    return 1 if c.failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
