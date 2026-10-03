#!/usr/bin/env python3
"""Confirm a launcher hand-off positively, in its own stage (LB4E-R2).

Run as `python3 -I -S -B confirm-launch.py --job-root J --stage-index N
--receipt-digest D`, only after launch-reference.py exited 0. The previous
stage must be `launch`.

Why: the launch stage's stage_complete COMPLETE is written before its fsync
and close. If either fails, the launcher stops P and exits 1, but the full
COMPLETE bytes can remain. A COMPLETE launch journal is therefore a
syntactic record, not proof of a hand-off. This stage is the proof: it
observes that the exact generations from the receipt are alive now.

The `launch-confirm` stage closes COMPLETE only if all of the following hold:
- the launch journal (bound by this stage's stage_begin) holds exactly one
  launch_receipt and one launch_readiness record, both naming digest D,
  and its stage_complete is COMPLETE;
- reference/launch.json re-hashes to D;
- P and C are alive now, each with the receipt's start_ticks (held pidfd,
  /proc stat, not a zombie), C's parent is P, and both are on the core's
  boot.

It then records `launch_handoff` {launch_journal_sha256, receipt_sha256,
supervisor, child, observed_monotonic_ns}. Any other outcome closes the
stage FAILED with a fixed code. A launch counts as handed off only with
both `launch` and `launch-confirm` COMPLETE. Nothing is signalled, started
or changed.
"""
import hashlib
import json
import os
import select
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
STAGE_NAME = "launch-confirm"
PREVIOUS = "launch"
MAX_STAGE_INDEX = 31


class Refused(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def stage_journal_module():
    conformance = os.path.dirname(HERE)
    if conformance not in sys.path:
        sys.path.insert(0, conformance)
    from alrust_conformance import stage_journal
    return stage_journal


def parse_args(argv):
    if len(argv) != 6 or argv[0::2] != ["--job-root", "--stage-index", "--receipt-digest"]:
        raise Refused("usage")
    job_root, index, digest = argv[1], argv[3], argv[5]
    if not os.path.isabs(job_root) or os.path.normpath(job_root) != job_root:
        raise Refused("usage")
    if not index.isdigit() or (len(index) > 1 and index[0] == "0") \
            or not 1 <= int(index) <= MAX_STAGE_INDEX:
        raise Refused("usage")
    if len(digest) != 64 or digest.strip("0123456789abcdef"):
        raise Refused("usage")
    return job_root, int(index), digest


def read_stat(pid):
    with open("/proc/%d/stat" % pid, "rb") as f:
        fields = f.read(4096).rsplit(b")", 1)[1].split()
    return fields[0], int(fields[1]), int(fields[19])  # state, ppid, starttime


def alive_generation(pid, start_ticks):
    """Hold a pidfd, then require the same generation, alive and not a
    zombie. Returns the parent pid. The pidfd is open across the read, so
    the PID cannot be reused under us."""
    try:
        fd = os.pidfd_open(pid, 0)
    except OSError:
        raise Refused("process_not_live") from None
    try:
        state, ppid, ticks = read_stat(pid)
        poller = select.poll()
        poller.register(fd, select.POLLIN)
        if ticks != start_ticks or state in (b"Z", b"X", b"x") or poller.poll(0):
            raise Refused("process_not_live")
        return ppid
    except (OSError, ValueError, IndexError):
        raise Refused("process_not_live") from None
    finally:
        os.close(fd)


def launch_records(sj, job_root, index):
    data = sj.read_bounded(sj.journal_path(job_root, index - 1, PREVIOUS), sj.MAX_JOURNAL)
    records = []
    for line in data.splitlines():
        evidence = sj.strict_json(line, "launch_journal_invalid")["evidence"]
        records.append((evidence["kind"], evidence["payload"]))
    return data, records


def confirm(sj, job_root, index, digest, journal):
    core = sj.validate_core(sj.read_bounded(os.path.join(job_root, sj.CORE_NAME),
                                            sj.MAX_CORE), job_root)
    data, records = launch_records(sj, job_root, index)
    kinds = [kind for kind, _ in records]
    receipts = [p for k, p in records if k == "launch_receipt"]
    readiness = [p for k, p in records if k == "launch_readiness"]
    if kinds[-1:] != ["stage_complete"] or records[-1][1] != {"result": "COMPLETE"} \
            or len(receipts) != 1 or len(readiness) != 1:
        raise Refused("launch_stage_not_complete")
    receipt_path = os.path.join(job_root, "reference", "launch.json")
    if receipts[0] != {"path": receipt_path, "sha256": digest} \
            or readiness[0].get("receipt_sha256") != digest:
        raise Refused("launch_digest_mismatch")
    receipt = sj.read_bounded(receipt_path, 65536)
    if hashlib.sha256(receipt).hexdigest() != digest:
        raise Refused("launch_digest_mismatch")
    launched = sj.strict_json(receipt, "launch_receipt_invalid")
    sup, child = launched["supervisor"], launched["child"]
    if launched["boot_id"] != core["boot_id"] or sj.boot_id() != core["boot_id"]:
        raise Refused("boot_mismatch")
    for value in (sup["pid"], sup["start_ticks"], child["pid"], child["start_ticks"]):
        if type(value) is not int or value < 1:
            raise Refused("launch_receipt_invalid")
    alive_generation(sup["pid"], sup["start_ticks"])
    if alive_generation(child["pid"], child["start_ticks"]) != sup["pid"]:
        raise Refused("parent_mismatch")
    journal.record("launch_handoff", {
        "launch_journal_sha256": hashlib.sha256(data).hexdigest(),
        "receipt_sha256": digest,
        "supervisor": {"pid": sup["pid"], "start_ticks": sup["start_ticks"]},
        "child": {"pid": child["pid"], "start_ticks": child["start_ticks"]},
        "observed_monotonic_ns": time.monotonic_ns()})


def main(argv):
    try:
        job_root, index, digest = parse_args(argv)
        sj = stage_journal_module()
    except Refused as refused:
        sys.stderr.write("confirm-launch: refused: %s\n" % refused.code)
        return 64
    try:
        journal = sj.StageJournal(job_root, index, STAGE_NAME, PREVIOUS)
        journal.__enter__()
    except (OSError, ValueError) as error:
        sys.stderr.write("confirm-launch: refused: stage journal could not begin: %s\n"
                         % error)
        return 1
    code = None
    try:
        confirm(sj, job_root, index, digest, journal)
    except Refused as refused:
        code = refused.code
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        code = "launch_evidence_unavailable"
    if code is not None:
        journal.fail(code)
        journal.__exit__(Refused, Refused(code), None)
        sys.stderr.write("confirm-launch: refused: %s\n" % code)
        return 1
    try:
        journal.__exit__(None, None, None)
    except (OSError, ValueError) as error:
        # Complete bytes may remain here too. Unlike the launch stage, they
        # are still true: the hand-off WAS observed alive. Only this record's
        # durability failed, so the step fails (exit 1) and the workflow's
        # always-run stop helper stops the tree. Later stages do not run.
        sys.stderr.write("confirm-launch: could not close the stage COMPLETE: %s\n" % error)
        return 1
    sys.stdout.write(json.dumps({"stage": STAGE_NAME, "result": "COMPLETE"}) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
