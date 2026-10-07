#!/usr/bin/env python3
"""The credential-holding stage-tool driver (R-EP plan P2b-2).

Run only as C_T, the child of its own tool supervisor generation P_T
(entrypoint-supervisor.py --tool NAME --stage-index N), with the reviewed
grammar `INTERPRETER -I [-S] [-B] [-u] stage-tool.py NAME ...`. The whole
driver is C_T, so the job role's password is generated (database-create)
or read (registry) only inside the supervised tree, after P_T exists
(conformance/tool-exit-contract.md, sections 2 and 3).

  database-create --job-root J --port N --superuser U
      --public-superuser-password W --container-id C --psql PATH
      --docker PATH --deadline-monotonic-ns D
      (the database name's profile, run id and attempt come from the job's
      /3 core, never argv; executing workflow W3)
  registry --job-root J --prefix a,b,c --secret-key-file F --port N
      --psql PATH --deadline-monotonic-ns D --expected-pins P

P is a JSON file (not a secret) holding the workflow head's pins,
{"entrypoint": {...}, "tools": {...}}, which the registry stage's writer
gate checks against the /3 core (R-EP plan P3a).

No secret value is accepted in argv: the only password in argv is the
service superuser's, which is public (database design section 1). The
environment is the caller's, inherited through P_T's execv: the caller
must pass a minimal environment with no secret values (the window uses
env -i; the workflow's paths-only wiring is P2c). The driver refuses to
start if DATABASE_URL, SECRET_KEY or PGPASSWORD is present, but that
does not make an arbitrary inherited environment safe (P2B2-SCOPE-N1).
The registry reads SECRET_KEY from its 0600 file, the job role's URL from
the 0600 URL file and the entrypoint values from the inventory.

One JSON line on stdout names the outcome, never a value or exception
text:
  0  COMPLETE: the stage closed COMPLETE;
  2  FAILED: the stage closed FAILED with the given fixed code;
  3  UNKNOWN: an unexpected error after the runner started; the stage
     journal, not this line, says whether and how it closed;
  70 NOT_RUN: setup failed before any stage was opened;
  64 NOT_RUN: usage.
"""
import json
import os
import stat
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

CREATE_OPTIONS = ("--job-root", "--port", "--superuser", "--public-superuser-password",
                  "--container-id", "--psql", "--docker", "--deadline-monotonic-ns")
REGISTRY_OPTIONS = ("--job-root", "--prefix", "--secret-key-file", "--port", "--psql",
                    "--deadline-monotonic-ns", "--expected-pins")
PATH_OPTIONS = ("--job-root", "--secret-key-file", "--psql", "--docker", "--expected-pins")
MAX_PINS = 1 << 16


def read_pins(path):
    """The expected-pins JSON object: a regular file read without following
    a final link, bounded. Its shape is checked by the gate."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        # P3A-INPUT-R1: the same opened descriptor must be a regular file.
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("pins not a regular file")
        data = os.read(fd, MAX_PINS + 1)
    finally:
        os.close(fd)
    if len(data) > MAX_PINS:
        raise ValueError("pins too large")
    value = json.loads(data)
    if type(value) is not dict:
        raise ValueError("pins not an object")
    return value
SECRET_VARIABLES = ("DATABASE_URL", "SECRET_KEY", "PGPASSWORD")


def _modules():
    """The reviewed database-stage module of this checkout. With -I only
    this script's directory would be on sys.path, so the conformance
    directory is added explicitly."""
    conformance = os.path.dirname(HERE)
    if conformance not in sys.path:
        sys.path.insert(0, conformance)
    from alrust_conformance import database_stage
    return database_stage


def _emit(record, code):
    sys.stdout.write(json.dumps(record, sort_keys=True) + "\n")
    return code


def parse(argv, names):
    opts = {}
    if len(argv) % 2:
        return None
    for name, value in zip(argv[0::2], argv[1::2]):
        if name not in names or name in opts:
            return None
        opts[name] = value
    if set(opts) != set(names):
        return None
    for name in PATH_OPTIONS:
        if name in opts and not (os.path.isabs(opts[name])
                                 and os.path.normpath(opts[name]) == opts[name]):
            return None
    for name in ("--port", "--deadline-monotonic-ns"):
        value = opts[name]
        if not value.isdigit() or len(value) > 20 or value != str(int(value)):
            return None
        opts[name] = int(value)
    if not 1 <= opts["--port"] <= 65535:
        return None
    return opts


def main(argv):
    if not argv or argv[0] not in ("database-create", "registry"):
        return _emit({"result": "NOT_RUN", "reason": "usage"}, 64)
    tool = argv[0]
    opts = parse(argv[1:], CREATE_OPTIONS if tool == "database-create" else REGISTRY_OPTIONS)
    if opts is None:
        return _emit({"result": "NOT_RUN", "reason": "usage"}, 64)
    if any(name in os.environ for name in SECRET_VARIABLES):
        return _emit({"result": "NOT_RUN", "reason": "secret_in_environment"}, 70)
    try:
        ds = _modules()
        pins = read_pins(opts["--expected-pins"]) if tool == "registry" else None
    except BaseException:  # no stage was opened; never print the error text
        return _emit({"result": "NOT_RUN", "reason": "driver_setup_failed"}, 70)
    try:
        if tool == "database-create":
            ds.run_create_stage(
                opts["--job-root"], port=opts["--port"], superuser=opts["--superuser"],
                superuser_password=opts["--public-superuser-password"],
                container_id=opts["--container-id"], psql_path=opts["--psql"],
                docker_path=opts["--docker"], deadline_ns=opts["--deadline-monotonic-ns"])
        else:
            prefix = opts["--prefix"].split(",")
            ds.run_registry_stage(
                opts["--job-root"], prefix, secret_key_path=opts["--secret-key-file"],
                port=opts["--port"], psql_path=opts["--psql"],
                deadline_ns=opts["--deadline-monotonic-ns"], expected=pins)
    except ds.DatabaseStageError as error:
        return _emit({"result": "FAILED", "tool": tool, "code": error.code}, 2)
    except Exception:  # never a traceback: it could carry a value
        # The stage may or may not have been opened or closed; its journal
        # is the record, so this line does not claim FAILED.
        return _emit({"result": "UNKNOWN", "tool": tool, "code": "driver_error"}, 3)
    return _emit({"result": "COMPLETE", "tool": tool}, 0)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
