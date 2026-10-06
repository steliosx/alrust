#!/usr/bin/env python3
"""The credential-holding stage-tool driver (R-EP plan P2b-2).

Run only as C_T, the child of its own tool supervisor generation P_T
(entrypoint-supervisor.py --tool NAME --stage-index N), with the reviewed
grammar `INTERPRETER -I [-S] [-B] [-u] stage-tool.py NAME ...`. The whole
driver is C_T, so the job role's password is generated (database-create)
or read (registry) only inside the supervised tree, after P_T exists
(conformance/tool-exit-contract.md, sections 2 and 3).

  database-create --job-root J --profile P --run-id R --attempt A --port N
      --superuser U --public-superuser-password W --container-id C
      --psql PATH --docker PATH --deadline-monotonic-ns D
  registry --job-root J --prefix a,b,c --secret-key-file F --port N
      --psql PATH --deadline-monotonic-ns D

No secret value is accepted in argv or the environment: the only password
in argv is the service superuser's, which is public (database design
section 1). The registry reads SECRET_KEY from its 0600 file, the job
role's URL from the 0600 URL file and the entrypoint values from the
inventory. One JSON line on stdout names the stage result, never a value.
Exit 0 COMPLETE, 2 the stage closed FAILED (code given), 64 usage.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

CREATE_OPTIONS = ("--job-root", "--profile", "--run-id", "--attempt", "--port",
                  "--superuser", "--public-superuser-password", "--container-id",
                  "--psql", "--docker", "--deadline-monotonic-ns")
REGISTRY_OPTIONS = ("--job-root", "--prefix", "--secret-key-file", "--port", "--psql",
                    "--deadline-monotonic-ns")
PATH_OPTIONS = ("--job-root", "--secret-key-file", "--psql", "--docker")


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
    ds = _modules()
    try:
        if tool == "database-create":
            ds.run_create_stage(
                opts["--job-root"], profile=opts["--profile"], run_id=opts["--run-id"],
                attempt=opts["--attempt"], port=opts["--port"],
                superuser=opts["--superuser"],
                superuser_password=opts["--public-superuser-password"],
                container_id=opts["--container-id"], psql_path=opts["--psql"],
                docker_path=opts["--docker"], deadline_ns=opts["--deadline-monotonic-ns"])
        else:
            prefix = opts["--prefix"].split(",")
            ds.run_registry_stage(
                opts["--job-root"], prefix, secret_key_path=opts["--secret-key-file"],
                port=opts["--port"], psql_path=opts["--psql"],
                deadline_ns=opts["--deadline-monotonic-ns"])
    except ds.DatabaseStageError as error:
        return _emit({"result": "FAILED", "tool": tool, "code": error.code}, 2)
    except Exception:  # never a traceback: it could carry a value
        return _emit({"result": "FAILED", "tool": tool, "code": "driver_error"}, 2)
    return _emit({"result": "COMPLETE", "tool": tool}, 0)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
