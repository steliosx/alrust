#!/usr/bin/env python3
"""Record the reference stop helper's verdict as the `stop` stage (W1).

Usage:
  python3 -I -S -B record-stop.py --job-root J --stage-index N --receipt-digest H

H is the launch receipt digest from the launcher's single ready line. The
stage opens at index N after `entrypoint-exit`, runs
`stop-reference.py <J>/reference/launch.json H J` once under a bounded
runner (database_stage._bounded_run: a 60 s deadline, bounded output, the
child killed and reaped on expiry), and records one stop_verdict
(alrust_conformance.stop_recorder). Standalone source: no workflow runs it
yet (executing-workflow design W1/W5).

One JSON line on stdout, never a value or exception text:
  0  stage COMPLETE and verdict COMPLETE;
  1  stage COMPLETE with verdict FAILED or UNPROVEN (the gates refuse);
  2  stage FAILED with a fixed code;
  64 usage.
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
STOP_HELPER = os.path.join(HERE, "stop-reference.py")
_INDEX = re.compile(r"[1-9][0-9]?\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")


def emit(record, code):
    sys.stdout.write(json.dumps(record, sort_keys=True) + "\n")
    sys.stdout.flush()
    return code


def package():
    conformance = os.path.dirname(HERE)
    if conformance not in sys.path:
        sys.path.insert(0, conformance)
    from alrust_conformance import database_stage, stop_recorder
    return database_stage, stop_recorder


def parse_args(argv):
    if len(argv) != 6 or argv[0::2] != ["--job-root", "--stage-index", "--receipt-digest"]:
        raise ValueError("usage")
    job_root, index, digest = argv[1], argv[3], argv[5]
    if not (os.path.isabs(job_root) and os.path.normpath(job_root) == job_root
            and _INDEX.fullmatch(index) and _SHA.fullmatch(digest)):
        raise ValueError("usage")
    return job_root, int(index), digest


def main(argv):
    try:
        job_root, index, digest = parse_args(argv)
    except ValueError:
        return emit({"stage": "NOT_RUN", "code": "usage"}, 64)
    ds, sr = package()
    launch_path, _ = sr.receipt_paths(job_root)
    helper_argv = [sys.executable, "-I", "-S", "-B", STOP_HELPER, launch_path, digest, job_root]

    def run_helper(argv):
        failure, returncode, stdout, stderr = ds._bounded_run(
            argv, None, {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
            sr.HELPER_TIMEOUT_S, sr.MAX_OUTPUT)
        if failure is not None:
            raise OSError(failure)
        return returncode, stdout, stderr

    result = sr.run_stop_stage(job_root, index, digest, run_helper=run_helper,
                               helper_argv=helper_argv)
    if result["stage"] == "FAILED":
        return emit(result, 2)
    return emit(result, 0 if result["verdict"] == "COMPLETE" else 1)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
