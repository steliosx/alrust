"""Pack or verify synthetic benchmark records. Accepts result data only and never starts a lab."""

import argparse
import gzip
from hashlib import sha256
import json
from pathlib import Path
import re

from summarize_cache_replay import pair_cells


VERSION = 1
# Timed reports contain ordinal key slots only. Refuse to distribute a lab credential
# if any later runner accidentally records one in its diagnostic text.
SECRET = re.compile(rb"bench-replay-[0-9a-f]{40}|(?:PRIVATE|SECRET) KEY-----", re.I)


def pack(root, output):
    runs = []
    for src in sorted(root.glob("*-control.json")):
        control = json.loads(src.read_bytes())
        if "exit_code" not in control:
            raise ValueError(f"Pending scope: {src.name}")
        name = control["args"]["label"]
        directory = root / name
        result = directory / "cache-replay.json"
        topology = directory / "topology.json"
        record = {
            "name": name,
            "control": {
                k: control[k]
                for k in (
                    "args",
                    "started",
                    "finished",
                    "exit_code",
                    "runtime_empty",
                    "manifest",
                    "addon_sha256",
                )
            },
            "topology": json.loads(topology.read_bytes()) if topology.exists() else None,
            "result": json.loads(result.read_bytes()) if result.exists() else None,
            "original_result_sha256": sha256(result.read_bytes()).hexdigest() if result.exists() else None,
        }
        for key, path in (
            ("scope_memory", directory / "scope-memory.json"),
            ("subsequent_cleanup", directory / "cleanup-confirmation.json"),
            ("external_thermal_guard", root / (name + "-thermal.json")),
        ):
            if path.is_file():
                record[key] = json.loads(path.read_bytes())
        runs.append(record)
    data = json.dumps({"version": VERSION, "runs": runs}, separators=(",", ":"), ensure_ascii=True).encode()
    if not runs or SECRET.search(data):
        raise ValueError("Empty evidence or disallowed credential text")
    verified = verify(data)
    output.write_bytes(gzip.compress(data, compresslevel=6, mtime=0))
    return verified


def verify(data):
    document = json.loads(data)
    if document.get("version") != VERSION or not document["runs"]:
        raise ValueError("Unknown or empty archive")
    names = set()
    counts = {"verified_runs": 0, "verified_pairs": 0, "timed_arrivals": 0, "excluded_runs": 0}
    for entry in document["runs"]:
        name = entry["name"]
        if name in names or name != entry["control"]["args"]["label"]:
            raise ValueError("Repeated or inconsistent run label")
        names.add(name)
        control, run = entry["control"], entry["result"]
        if run is not None:
            encoded = json.dumps(run, separators=(",", ":")) + "\n"
            if sha256(encoded.encode()).hexdigest() != entry["original_result_sha256"]:
                raise ValueError(f"Run content differs from the original report for {name}")
        if not run or not run.get("valid") or control["exit_code"] or not control["runtime_empty"]:
            counts["excluded_runs"] += 1
            continue
        t = entry["topology"]
        if not (
            t
            and t["postgres_a_role"] == "f"
            and t["postgres_b_role"] == "t"
            and t["streaming"] == "streaming"
            and t["replication_sync_state"] == "async"
        ):
            raise ValueError(f"Unconfirmed primary and asynchronous standby in {name}")
        counts["verified_pairs"] += len(pair_cells(run))
        counts["verified_runs"] += 1
        counts["timed_arrivals"] += sum(len(c["arrivals"]) for c in run["cells"])
    return counts


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(required=True, dest="action")
    publish = actions.add_parser("pack")
    publish.add_argument("root", type=Path)
    publish.add_argument("output", type=Path)
    audit = actions.add_parser("verify")
    audit.add_argument("archive", type=Path)
    args = parser.parse_args()
    if args.action == "pack":
        result = pack(args.root, args.output)
    else:
        with gzip.open(args.archive, "rb") as stream:
            result = verify(stream.read())
    print(json.dumps(result, sort_keys=True))
