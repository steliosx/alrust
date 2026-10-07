#!/usr/bin/env python3
"""Run one verifier or drop stage of the executing workflow (design W2;
conformance/executing-workflow-design.md section 2).

Thin command-line wrappers over the reviewed stage runners; each stage's
gate, records and closure are the runner's own:

  entrypoint-exit --job-root J --prefix a,b,... --expected-pins P
      entrypoint_exit.run_entrypoint_exit_stage (PREFIX ends at entrypoint)
  tool-exit --job-root J --prefix a,b,... --tool NAME --expected-pins P
      tool_exit.run_tool_exit_stage (PREFIX ends at NAME)
  database-drop --job-root J --prefix a,b,... --port N --superuser U
      --public-superuser-password W --psql PATH --deadline-monotonic-ns D
      --expected-pins P
      database_stage.run_drop_stage. Whether a server was launched is
      taken from the evidence (PREFIX; aggregate's exact journal set
      refuses a prefix that omits an existing journal), never from a
      caller flag. The only password in argv is the service superuser's,
      which is public (database design section 1).

P is the expected-pins JSON file (workflow literals; not a secret).
Standalone source: no workflow runs it yet (W5). The caller runs
stop-supervisor.py before an exit stage (P4); this script never signals.

One JSON line on stdout, never a value or exception text:
  0  COMPLETE: the stage closed COMPLETE;
  2  FAILED: the stage refused or closed FAILED, with a fixed code;
  3  UNKNOWN: an unexpected error; the stage journal, not this line, says
     whether and how the stage closed;
  64 NOT_RUN: usage;
  70 NOT_RUN: setup failed before any stage was opened.
"""
import json
import os
import re
import stat
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MAX_PINS = 1 << 16
SECRET_VARIABLES = ("DATABASE_URL", "SECRET_KEY", "PGPASSWORD")
_STAGE = re.compile(r"[a-z][a-z0-9-]{0,31}\Z")
_DIGITS = re.compile(r"(0|[1-9][0-9]{0,18})\Z")
_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
OPTIONS = {
    "entrypoint-exit": ("--job-root", "--prefix", "--expected-pins"),
    "tool-exit": ("--job-root", "--prefix", "--tool", "--expected-pins"),
    "database-drop": ("--job-root", "--prefix", "--port", "--superuser",
                      "--public-superuser-password", "--psql", "--deadline-monotonic-ns",
                      "--expected-pins"),
}


def emit(record, code):
    sys.stdout.write(json.dumps(record, sort_keys=True) + "\n")
    sys.stdout.flush()
    return code


def modules():
    conformance = os.path.dirname(HERE)
    if conformance not in sys.path:
        sys.path.insert(0, conformance)
    from alrust_conformance import database_stage, entrypoint_exit, tool_exit
    return database_stage, entrypoint_exit, tool_exit


def read_pins(path):
    """The expected-pins JSON object: a regular file (checked on the opened
    descriptor), not followed if a link, bounded. Raises ValueError."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("pins")
        data = os.read(fd, MAX_PINS + 1)
    finally:
        os.close(fd)
    if len(data) > MAX_PINS:
        raise ValueError("pins")
    value = json.loads(data)
    if type(value) is not dict:
        raise ValueError("pins")
    return value


def parse(argv):
    """(command, options) or None for usage. Each option exactly once."""
    if not argv or argv[0] not in OPTIONS:
        return None
    command, names = argv[0], OPTIONS[argv[0]]
    rest = argv[1:]
    if len(rest) != 2 * len(names) or sorted(rest[0::2]) != sorted(names):
        return None
    opts = dict(zip(rest[0::2], rest[1::2]))
    for name in ("--job-root", "--expected-pins", "--psql"):
        if name in opts and not (os.path.isabs(opts[name])
                                 and os.path.normpath(opts[name]) == opts[name]):
            return None
    prefix = opts["--prefix"].split(",")
    if not (1 <= len(prefix) <= 31 and all(_STAGE.fullmatch(n) for n in prefix)):
        return None
    opts["--prefix"] = prefix
    if "--tool" in opts and not _STAGE.fullmatch(opts["--tool"]):
        return None
    for name in ("--port", "--deadline-monotonic-ns"):
        if name in opts:
            if not _DIGITS.fullmatch(opts[name]):
                return None
            opts[name] = int(opts[name])
    if "--port" in opts and not 0 < opts["--port"] < 65536:
        return None
    return command, opts


def main(argv):
    parsed = parse(argv)
    if parsed is None:
        return emit({"result": "NOT_RUN", "reason": "usage"}, 64)
    command, opts = parsed
    if any(name in os.environ for name in SECRET_VARIABLES):
        return emit({"result": "NOT_RUN", "reason": "secret_in_environment"}, 70)
    try:
        ds, ee, te = modules()
        pins = read_pins(opts["--expected-pins"])
    except BaseException:  # no stage was opened; never print the error text
        return emit({"result": "NOT_RUN", "reason": "setup_failed"}, 70)
    try:
        if command == "entrypoint-exit":
            record = ee.run_entrypoint_exit_stage(opts["--job-root"], opts["--prefix"],
                                                  pins.get("entrypoint"))
            verdict = record["verdict"]
        elif command == "tool-exit":
            tools = pins.get("tools")
            record = te.run_tool_exit_stage(
                opts["--job-root"], opts["--prefix"], opts["--tool"],
                tools.get(opts["--tool"]) if type(tools) is dict else None)
            verdict = record["verdict"]
        else:
            ds.run_drop_stage(
                opts["--job-root"], opts["--prefix"], launched="launch" in opts["--prefix"],
                expected=pins, port=opts["--port"], superuser=opts["--superuser"],
                superuser_password=opts["--public-superuser-password"],
                psql_path=opts["--psql"], deadline_ns=opts["--deadline-monotonic-ns"])
            verdict = "COMPLETE"
    except ds.DatabaseStageError as error:
        return emit({"result": "FAILED", "stage": command, "code": error.code}, 2)
    except ds.sj.JournalError as error:
        # A refusal with the journal module's fixed code (for example an
        # exit stage's prefix check, before its journal opens).
        code = str(error)
        return emit({"result": "FAILED", "stage": command,
                     "code": code if _CODE.fullmatch(code) else "journal_error"}, 2)
    except Exception:  # never a traceback: it could carry a value
        return emit({"result": "UNKNOWN", "stage": command, "code": "runner_error"}, 3)
    if verdict == "COMPLETE":
        return emit({"result": "COMPLETE", "stage": command}, 0)
    return emit({"result": "FAILED", "stage": command,
                 "code": "%s_%s" % (command.replace("-", "_"), verdict.lower())}, 2)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
