"""Database stage library (approved design 87653d31, part A of the code).

Implements contract section 7 for one job: a per-job login role and
database on the workflow's PostgreSQL service, created in the
`database-create` stage and dropped in the `database-drop` stage, with
durable attempt records so that a half-finished allocation is never
adopted, guessed or upgraded (DBC023-R2).

Part A (this module): the name rule, the SCRAM-SHA-256 verifier, the
bounded psql client, the creation state machine and the drop state
machine. The drop stage takes its writer gate as an argument; part B
supplies writer_quiescence / no_launch_quiescence (DBC023-R1), the
registry stage and the finalizer's database linkage.

Secrets: the job role's password exists only in this process, in the 0600
URL file and (later) in the registry. It is never in argv, the child
environment, a receipt or a journal; PostgreSQL receives only the SCRAM
verifier, in SQL on stdin. The service superuser's password is public in
the workflow file and is never given to the reference server.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import selectors
import stat
import subprocess
import time

from . import stage_journal as sj

CREATE_STAGE = "database-create"
DROP_STAGE = "database-drop"
DATABASE_DIR = "database"
URL_NAME = "url"
SERVER_VERSION_MIN, SERVER_VERSION_MAX = 160000, 169999
STAGE_BUDGET_NS = 60 * 1_000_000_000
CALL_TIMEOUT_S = 30.0
MAX_OUTPUT = 64 * 1024
MAX_PROFILE, MAX_RUN_ID, MAX_ATTEMPT = 31, 20, 3
SCRAM_ITERATIONS = 4096
_NAME = re.compile(r"alrust_[a-z0-9_]{1,56}\Z")
_PROFILE = re.compile(r"[a-z0-9][a-z0-9_-]{0,30}\Z")
_DIGITS = re.compile(r"[1-9][0-9]*\Z")
_VERIFIER = re.compile(r"SCRAM-SHA-256\$4096:[A-Za-z0-9+/=]+\$[A-Za-z0-9+/=]+:[A-Za-z0-9+/=]+\Z")
_CONTAINER = re.compile(r"[0-9a-f]{12,64}\Z")
_PATH = "/usr/bin:/bin"


class DatabaseStageError(Exception):
    """A fixed failure code; never a value, a path or psql's text."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _require(condition, code):
    if not condition:
        raise DatabaseStageError(code)


def _json(data, code):
    """Strict JSON (no duplicate keys, no non-finite numbers), mapped to a
    fixed DatabaseStageError code."""
    try:
        return sj.strict_json(data, code)
    except sj.JournalError:
        raise DatabaseStageError(code) from None


# ---------------------------------------------------------------------------
# Name (contract section 7, exactly)

def database_name(profile, run_id, attempt):
    """alrust_<profile>_<run_id>_<attempt>, '-' in the profile mapped to
    '_'. Over-long parts are refused, never truncated. The renderer's
    refusal of profile ids differing only by '-' and '_' stays where it is."""
    _require(type(profile) is str and _PROFILE.fullmatch(profile)
             and len(profile) <= MAX_PROFILE, "profile_invalid")
    _require(type(run_id) is str and _DIGITS.fullmatch(run_id)
             and len(run_id) <= MAX_RUN_ID, "run_id_invalid")
    _require(type(attempt) is str and _DIGITS.fullmatch(attempt)
             and len(attempt) <= MAX_ATTEMPT, "attempt_invalid")
    name = "alrust_%s_%s_%s" % (profile.replace("-", "_"), run_id, attempt)
    _require(len(name.encode()) <= 63 and _NAME.fullmatch(name), "name_invalid")
    return name


# ---------------------------------------------------------------------------
# SCRAM-SHA-256 verifier (RFC 5802 / RFC 7677; PostgreSQL's stored format)

def scram_keys(password, salt, iterations=SCRAM_ITERATIONS):
    salted = hashlib.pbkdf2_hmac("sha256", password, salt, iterations)
    client_key = hmac.new(salted, b"Client Key", "sha256").digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted, b"Server Key", "sha256").digest()
    return client_key, stored_key, server_key


def scram_verifier(password, salt=None):
    """SCRAM-SHA-256$4096:<salt>$<StoredKey>:<ServerKey>, base64."""
    _require(type(password) is bytes and len(password) >= 16, "password_invalid")
    salt = secrets.token_bytes(16) if salt is None else salt
    _, stored_key, server_key = scram_keys(password, salt)
    b64 = lambda data: base64.b64encode(data).decode()
    verifier = "SCRAM-SHA-256$%d:%s$%s:%s" % (SCRAM_ITERATIONS, b64(salt),
                                             b64(stored_key), b64(server_key))
    _require(_VERIFIER.fullmatch(verifier), "verifier_invalid")
    return verifier


# ---------------------------------------------------------------------------
# Files: every one exclusive, inside the 0700 database directory

def _write_private(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                 0o600)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)


def _database_dir(job_root, create):
    path = os.path.join(job_root, DATABASE_DIR)
    if create:
        os.mkdir(path, 0o700)
    info = os.lstat(path)
    _require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
             and stat.S_IMODE(info.st_mode) == 0o700, "database_dir_invalid")
    return path, info


# ---------------------------------------------------------------------------
# Bounded child processes (DB61-R1)

def _bounded_run(argv, stdin, env, timeout_s, limit):
    """Run ARGV, draining stdout and stderr AS THEY ARRIVE, each bounded to
    LIMIT bytes. Exceeding either bound or TIMEOUT_S is a refusal, never a
    truncation: the owned direct child is killed (it alone, never a group)
    and reaped. Returns (failure, returncode, stdout, stderr), where
    failure is None, "timeout" or "output_too_large", and the returned
    streams are the bounded bytes kept (failure evidence)."""
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=env, close_fds=True)
    deadline = time.monotonic() + timeout_s
    out, err = bytearray(), bytearray()
    failure = None
    try:
        try:
            if stdin:
                proc.stdin.write(stdin)
            proc.stdin.close()
        except BrokenPipeError:
            pass
        selector = selectors.DefaultSelector()
        selector.register(proc.stdout, selectors.EVENT_READ, out)
        selector.register(proc.stderr, selectors.EVENT_READ, err)
        streams = 2
        while streams and failure is None:
            left = deadline - time.monotonic()
            if left <= 0:
                failure = "timeout"
                break
            for key, _ in selector.select(timeout=left):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    streams -= 1
                    continue
                kept = key.data
                if len(kept) + len(chunk) > limit:
                    kept.extend(chunk[:limit - len(kept)])
                    failure = "output_too_large"
                    break
                kept.extend(chunk)
        selector.close()
        if failure is None:
            try:
                proc.wait(timeout=max(deadline - time.monotonic(), 0))
            except subprocess.TimeoutExpired:
                failure = "timeout"
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        for stream in (proc.stdout, proc.stderr):
            stream.close()
    return failure, proc.returncode, bytes(out), bytes(err)


# ---------------------------------------------------------------------------
# Bounded psql client

class Psql:
    """One psql invocation per call, under a cleared environment, with a
    0600 passfile, stdin SQL, a per-call timeout no later than the stage
    deadline, and stdout and stderr each drained and bounded to MAX_OUTPUT
    (an overflow or timeout stops and reaps the child, then refuses).
    Errors are fixed codes; psql's stderr is kept private (bounded) in the
    database directory."""

    def __init__(self, psql_path, *, port, user, password, database_dir, deadline_ns):
        _require(type(psql_path) is str and os.path.isabs(psql_path)
                 and os.path.normpath(psql_path) == psql_path, "psql_invalid")
        info = os.stat(psql_path)
        _require(stat.S_ISREG(info.st_mode) and info.st_mode & 0o111, "psql_invalid")
        _require(type(port) is int and 1 <= port <= 65535, "port_invalid")
        _require(type(user) is str and re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", user),
                 "user_invalid")
        self.path, self.port, self.user, self._dir = psql_path, port, user, database_dir
        self._deadline = deadline_ns
        self._calls = 0
        self.passfile = os.path.join(database_dir, "pgpass-%s-%s" % (user, secrets.token_hex(4)))
        # host:port:database:user:password; ':' and '\\' escaped per libpq.
        escaped = password.replace("\\", "\\\\").replace(":", "\\:")
        _write_private(self.passfile, ("127.0.0.1:%d:*:%s:%s\n"
                                       % (port, user, escaped)).encode())

    def version(self):
        out = self._run(["--version"], None)
        return out.decode("ascii", "replace").strip()[:128]

    def query(self, sql, database, variables=()):
        """Run SQL on stdin; return stdout bytes (bounded). variables are
        (name, value) pairs of non-secret identifiers for :'x' / :"x"."""
        args = ["-X", "-q", "-At", "-v", "ON_ERROR_STOP=1", "-h", "127.0.0.1",
                "-p", str(self.port), "-U", self.user, "-d", database, "-w"]
        for name, value in variables:
            _require(re.fullmatch(r"[a-z_]{1,16}", name)
                     and type(value) is str and re.fullmatch(r"[A-Za-z0-9_]{1,63}", value),
                     "variable_invalid")
            args += ["-v", "%s=%s" % (name, value)]
        return self._run(args, sql.encode())

    def query_json(self, sql, database, variables=()):
        out = self.query(sql, database, variables)
        lines = out.splitlines()
        _require(len(lines) == 1, "psql_output_invalid")
        value = _json(lines[0], "psql_output_invalid")
        _require(type(value) is dict, "psql_output_invalid")
        return value

    def _run(self, args, stdin):
        left = (self._deadline - time.monotonic_ns()) / 1e9
        _require(left > 0, "stage_deadline_expired")
        self._calls += 1
        env = {"PATH": _PATH, "LANG": "C.UTF-8", "PGPASSFILE": self.passfile,
               "PGCONNECT_TIMEOUT": "10",
               "PGOPTIONS": "-c statement_timeout=30s -c lock_timeout=10s"}
        try:
            failure, returncode, stdout, stderr = _bounded_run(
                [self.path] + args, stdin, env, min(CALL_TIMEOUT_S, left), MAX_OUTPUT)
        except OSError:
            raise DatabaseStageError("psql_unavailable") from None
        if stderr:
            # Bounded private evidence (at most MAX_OUTPUT bytes), kept on
            # success and failure alike; an overflow is refused below.
            _write_private(os.path.join(self._dir, "psql-stderr-%s-%d"
                                        % (secrets.token_hex(4), self._calls)), stderr)
        _require(failure != "timeout", "psql_timeout")
        _require(failure != "output_too_large", "psql_output_too_large")
        _require(returncode == 0, "psql_failed")
        return stdout


# The SQL. Identifiers and names come from psql variables; the only literal
# built here is the SCRAM verifier, checked against _VERIFIER first.
SQL_IDENTITY = """-- alrust:op=identity
SELECT json_build_object(
  'server_version_num', current_setting('server_version_num')::int,
  'system_identifier', (pg_control_system()).system_identifier::text,
  'postmaster_start_time', to_char(pg_postmaster_start_time() AT TIME ZONE 'UTC',
                                   'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
  'version', version());
"""
SQL_EXISTS = """-- alrust:op=exists
SELECT json_build_object(
  'database', EXISTS (SELECT 1 FROM pg_database WHERE datname = :'name'),
  'role', EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'name'));
"""
SQL_CREATE_ROLE = """-- alrust:op=create_role
SET log_statement = 'none';
CREATE ROLE :"name" LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
  PASSWORD '%s';
"""
SQL_ROLE_INFO = """-- alrust:op=role_info
SELECT json_build_object('oid', oid::bigint, 'login', rolcanlogin, 'super', rolsuper,
  'createdb', rolcreatedb, 'createrole', rolcreaterole,
  'replication', rolreplication, 'bypassrls', rolbypassrls)
FROM pg_roles WHERE rolname = :'name';
"""
SQL_CREATE_DATABASE = """-- alrust:op=create_database
CREATE DATABASE :"name" OWNER :"name" TEMPLATE template0 ENCODING 'UTF8';
"""
SQL_DATABASE_INFO = """-- alrust:op=database_info
SELECT json_build_object('oid', d.oid::bigint, 'owner', r.rolname)
FROM pg_database d JOIN pg_roles r ON r.oid = d.datdba WHERE d.datname = :'name';
"""
SQL_DROP_DATABASE = """-- alrust:op=drop_database
DROP DATABASE :"name" WITH (FORCE);
"""
SQL_DROP_ROLE = """-- alrust:op=drop_role
DROP ROLE :"name";
"""
SQL_ABSENCE = """-- alrust:op=absence
SELECT json_build_object(
  'database_by_name', EXISTS (SELECT 1 FROM pg_database WHERE datname = :'name'),
  'database_by_oid', EXISTS (SELECT 1 FROM pg_database WHERE oid = :'database_oid'::oid),
  'role_by_name', EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'name'),
  'role_by_oid', EXISTS (SELECT 1 FROM pg_roles WHERE oid = :'role_oid'::oid));
"""
_IDENTITY_KEYS = {"server_version_num", "system_identifier", "postmaster_start_time",
                  "version"}


def _cluster_identity(psql):
    value = psql.query_json(SQL_IDENTITY, "postgres")
    _require(set(value) == _IDENTITY_KEYS
             and type(value["server_version_num"]) is int
             and SERVER_VERSION_MIN <= value["server_version_num"] <= SERVER_VERSION_MAX,
             "server_version_unsupported")
    _require(type(value["system_identifier"]) is str
             and re.fullmatch(r"[0-9]{1,20}", value["system_identifier"])
             and type(value["postmaster_start_time"]) is str
             and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z",
                              value["postmaster_start_time"])
             and type(value["version"]) is str and 0 < len(value["version"]) <= 512,
             "cluster_identity_invalid")
    return value


def _oid(value):
    return type(value) is int and 0 < value < (1 << 32)


# ---------------------------------------------------------------------------
# Container identity (docker inspect of the workflow-supplied service id)

def container_identity(docker_path, container_id, deadline_ns):
    _require(type(container_id) is str and _CONTAINER.fullmatch(container_id),
             "container_id_invalid")

    def run(args):
        left = (deadline_ns - time.monotonic_ns()) / 1e9
        _require(left > 0, "stage_deadline_expired")
        try:
            failure, returncode, stdout, _ = _bounded_run(
                [docker_path] + args, None, {"PATH": _PATH, "LANG": "C.UTF-8"},
                min(CALL_TIMEOUT_S, left), 4 * MAX_OUTPUT)
        except OSError:
            raise DatabaseStageError("docker_unavailable") from None
        _require(failure is None, "docker_" + (failure or "failed"))
        _require(returncode == 0, "docker_failed")
        return _json(stdout.strip(), "docker_output_invalid")

    container = run(["inspect", "--format", "{{json .}}", container_id])
    _require(type(container) is dict and type(container.get("Image")) is str
             and re.fullmatch(r"sha256:[0-9a-f]{64}", container["Image"]), "docker_output_invalid")
    ports = (container.get("NetworkSettings") or {}).get("Ports") or {}
    mapping = ports.get("5432/tcp")
    _require(type(mapping) is list and len(mapping) >= 1 and all(
        type(m) is dict and type(m.get("HostIp")) is str and type(m.get("HostPort")) is str
        for m in mapping), "docker_output_invalid")
    digests = run(["image", "inspect", "--format", "{{json .RepoDigests}}", container["Image"]])
    _require(type(digests) is list and all(type(d) is str and len(d) <= 256 for d in digests)
             and len(digests) <= 16, "docker_output_invalid")
    return {"container_id": container_id, "image_id": container["Image"],
            "repo_digests": sorted(digests),
            "port": [{"container": "5432/tcp", "host_ip": m["HostIp"],
                      "host_port": m["HostPort"]} for m in mapping]}


# ---------------------------------------------------------------------------
# Stage database-create (section 3 of the design): an explicit state machine

def create_database(job_root, journal, *, profile, run_id, attempt, port, superuser,
                    superuser_password, container_id, psql_path, docker_path,
                    deadline_ns):
    """Run inside the open `database-create` StageJournal. Every CREATE is
    preceded by a durable attempt record; every created object's identity
    is recorded as soon as it is read back. Returns the job role's
    DATABASE_URL path. Raises DatabaseStageError with a fixed code."""
    name = database_name(profile, run_id, attempt)
    directory, info = _database_dir(job_root, create=True)
    journal.record("owned_resource", {"path": directory, "dev": info.st_dev,
                                      "ino": info.st_ino, "type": "dir"})
    admin = Psql(psql_path, port=port, user=superuser, password=superuser_password,
                 database_dir=directory, deadline_ns=deadline_ns)
    client = {"path": psql_path, "version": admin.version()}
    cluster = _cluster_identity(admin)
    exists = admin.query_json(SQL_EXISTS, "postgres", [("name", name)])
    _require(exists == {"database": False, "role": False}, "name_already_exists")
    identity = {"system_identifier": cluster["system_identifier"],
                "postmaster_start_time": cluster["postmaster_start_time"]}

    password = secrets.token_hex(32)
    verifier = scram_verifier(password.encode())
    journal.record("database_role_attempted", {"role": name})
    admin.query(SQL_CREATE_ROLE % verifier, "postgres", [("name", name)])
    role = admin.query_json(SQL_ROLE_INFO, "postgres", [("name", name)])
    _require(set(role) == {"oid", "login", "super", "createdb", "createrole",
                           "replication", "bypassrls"} and _oid(role["oid"])
             and role["login"] is True and not any(
                 role[k] is not False for k in ("super", "createdb", "createrole",
                                                "replication", "bypassrls")),
             "role_attributes_invalid")
    journal.record("database_role_created", dict(identity, role=name, role_oid=role["oid"]))

    journal.record("database_create_attempted", {"name": name})
    admin.query(SQL_CREATE_DATABASE, "postgres", [("name", name)])
    database = admin.query_json(SQL_DATABASE_INFO, "postgres", [("name", name)])
    _require(set(database) == {"oid", "owner"} and _oid(database["oid"])
             and database["owner"] == name, "database_owner_invalid")
    journal.record("database_created", dict(
        identity, name=name, oid=database["oid"], role=name, role_oid=role["oid"],
        server_version_num=cluster["server_version_num"], version=cluster["version"],
        psql=client))
    journal.record("database_container", container_identity(docker_path, container_id,
                                                             deadline_ns))
    url_path = os.path.join(directory, URL_NAME)
    _write_private(url_path, ("postgresql://%s:%s@127.0.0.1:%d/%s\n"
                              % (name, password, port, name)).encode())
    return url_path


# ---------------------------------------------------------------------------
# Stage database-drop (section 6 of the design)

NONE, UNKNOWN, RECORDED = "NONE", "UNKNOWN", "RECORDED"


def allocation_states(create_journal_bytes):
    """(role_state, database_state, role_record, database_record) from the
    closed database-create journal. Raises if it is not closed."""
    records = [_json(line, "create_journal_invalid")["evidence"]
               for line in create_journal_bytes.splitlines()]
    _require(records and records[-1]["kind"] == "stage_complete",
             "create_journal_not_closed")
    kinds = [r["kind"] for r in records]
    for kind in ("database_role_attempted", "database_role_created",
                 "database_create_attempted", "database_created"):
        _require(kinds.count(kind) <= 1, "create_journal_invalid")

    def payload(kind):
        found = [r["payload"] for r in records if r["kind"] == kind]
        return found[0] if found else None
    role_record, database_record = payload("database_role_created"), payload("database_created")
    role_state = NONE if payload("database_role_attempted") is None else \
        RECORDED if role_record is not None else UNKNOWN
    database_state = NONE if payload("database_create_attempted") is None else \
        RECORDED if database_record is not None else UNKNOWN
    _require(not (database_state != NONE and role_state != RECORDED), "create_journal_invalid")
    return role_state, database_state, role_record, database_record


def drop_database(job_root, journal, *, gate, create_journal_bytes, port, superuser,
                  superuser_password, psql_path, deadline_ns):
    """Run inside the open `database-drop` StageJournal, after `gate()`
    (writer_quiescence or no_launch_quiescence, part B) has passed; the gate
    runs here, before the first SQL statement. Only RECORDED objects that
    match on cluster, name and oid are dropped. Appends exactly one
    database_drop record and returns its verdict."""
    role_state, database_state, role_record, database_record = \
        allocation_states(create_journal_bytes)
    gate()
    record = {"verdict": "UNPROVEN", "reason": "drop_not_proven",
              "database": None, "oid": None, "role": None, "role_oid": None,
              "system_identifier": None, "postmaster_start_time": None}
    if role_state == NONE and database_state == NONE:
        record.update(verdict="NONE_ALLOCATED", reason="nothing_was_attempted")
        journal.record("database_drop", record)
        return record["verdict"]
    if role_state == UNKNOWN:
        # Never adopted by name, never guessed: nothing is dropped.
        record.update(reason="role_outcome_unknown")
        journal.record("database_drop", record)
        return record["verdict"]
    name = role_record["role"]
    record.update(role=name, role_oid=role_record["role_oid"],
                  system_identifier=role_record["system_identifier"],
                  postmaster_start_time=role_record["postmaster_start_time"])
    if database_record is not None:
        record.update(database=database_record["name"], oid=database_record["oid"])
    directory, _ = _database_dir(job_root, create=False)
    admin = Psql(psql_path, port=port, user=superuser, password=superuser_password,
                 database_dir=directory, deadline_ns=deadline_ns)
    try:
        cluster = _cluster_identity(admin)
        if (cluster["system_identifier"], cluster["postmaster_start_time"]) != (
                role_record["system_identifier"], role_record["postmaster_start_time"]):
            record.update(reason="cluster_mismatch")
            journal.record("database_drop", record)
            return record["verdict"]
        role = admin.query_json(SQL_ROLE_INFO, "postgres", [("name", name)])
        if role.get("oid") != role_record["role_oid"]:
            record.update(reason="role_identity_mismatch")
            journal.record("database_drop", record)
            return record["verdict"]
        if database_state == RECORDED:
            database = admin.query_json(SQL_DATABASE_INFO, "postgres", [("name", name)])
            if database != {"oid": database_record["oid"], "owner": name}:
                record.update(reason="database_identity_mismatch")
                journal.record("database_drop", record)
                return record["verdict"]
            admin.query(SQL_DROP_DATABASE, "postgres", [("name", name)])
        elif database_state == UNKNOWN:
            exists = admin.query_json(SQL_EXISTS, "postgres", [("name", name)])
            if exists.get("database") is not False:
                record.update(reason="database_outcome_unknown_present")
                journal.record("database_drop", record)
                return record["verdict"]
        admin.query(SQL_DROP_ROLE, "postgres", [("name", name)])
        absent = admin.query_json(SQL_ABSENCE, "postgres", [
            ("name", name), ("role_oid", str(role_record["role_oid"])),
            ("database_oid", str(database_record["oid"] if database_record else 0))])
        _require(set(absent) == {"database_by_name", "database_by_oid", "role_by_name",
                                 "role_by_oid"}, "psql_output_invalid")
        if any(value is not False for value in absent.values()):
            record.update(reason="still_present")
        elif database_state == RECORDED:
            record.update(verdict="COMPLETE", reason="dropped_and_verified_absent")
        elif database_state == NONE:
            record.update(verdict="ROLE_ONLY_COMPLETE", reason="role_dropped_no_database")
        else:
            # An unknown CREATE DATABASE outcome is never upgraded.
            record.update(reason="role_dropped_database_outcome_unknown")
    except DatabaseStageError as error:
        record.update(reason=error.code)
    journal.record("database_drop", record)
    return record["verdict"]
