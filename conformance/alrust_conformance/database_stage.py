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
MAX_INPUT = 1024 * 1024
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
    """Run ARGV, feeding STDIN and draining stdout and stderr AS THEY
    PROGRESS, all inside one deadline. stdin is written in non-blocking
    chunks through the same selector (DBA2-R2), so a child that never reads
    cannot hold the caller past TIMEOUT_S; each output stream is bounded to
    LIMIT bytes. Exceeding a bound or TIMEOUT_S is a refusal, never a
    truncation: the owned direct child is killed (it alone, never a group)
    and reaped. Returns (failure, returncode, stdout, stderr), where
    failure is None, "timeout" or "output_too_large", and the returned
    streams are the bounded bytes kept (failure evidence)."""
    deadline = time.monotonic() + timeout_s
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=env, close_fds=True)
    out, err = bytearray(), bytearray()
    pending = memoryview(stdin or b"")
    failure = None
    try:
        selector = selectors.DefaultSelector()
        if pending:
            os.set_blocking(proc.stdin.fileno(), False)
            selector.register(proc.stdin, selectors.EVENT_WRITE, None)
        else:
            proc.stdin.close()
        selector.register(proc.stdout, selectors.EVENT_READ, out)
        selector.register(proc.stderr, selectors.EVENT_READ, err)
        streams = 2
        while (streams or pending) and failure is None:
            left = deadline - time.monotonic()
            if left <= 0:
                failure = "timeout"
                break
            for key, _ in selector.select(timeout=left):
                if key.fileobj is proc.stdin:
                    try:
                        pending = pending[os.write(proc.stdin.fileno(), pending[:65536]):]
                    except BlockingIOError:
                        continue
                    except BrokenPipeError:
                        pending = memoryview(b"")
                    if not pending:
                        selector.unregister(proc.stdin)
                        proc.stdin.close()
                    continue
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
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                stream.close()
            except OSError:
                pass   # an unwritten stdin buffer to a dead child
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
        _require(stdin is None or len(stdin) <= MAX_INPUT, "psql_input_too_large")
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

# The host addresses through which a client on 127.0.0.1 reaches a published
# port (PR #100 review, comment 4174199627).
ENDPOINT_HOSTS = ("0.0.0.0", "127.0.0.1")


def container_identity(docker_path, container_id, deadline_ns, port):
    """The service container's identity, and proof from `docker inspect` that
    it publishes 5432/tcp on PORT, the port every SQL call of this stage
    uses, on an IPv4 address reachable from 127.0.0.1. Refuses
    container_port_mismatch otherwise. That is published-mapping evidence,
    not proof that the TCP peer at 127.0.0.1:PORT is this container."""
    _require(type(container_id) is str and _CONTAINER.fullmatch(container_id),
             "container_id_invalid")
    _require(type(port) is int and 0 < port < 65536, "container_port_mismatch")

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
    _require(type(mapping) is list and 1 <= len(mapping) <= 16 and all(
        type(m) is dict and type(m.get("HostIp")) is str and len(m["HostIp"]) <= 45
        and type(m.get("HostPort")) is str and re.fullmatch(r"[0-9]{1,5}", m["HostPort"])
        for m in mapping), "docker_output_invalid")
    _require(any(m["HostPort"] == str(port) and m["HostIp"] in ENDPOINT_HOSTS
                 for m in mapping), "container_port_mismatch")
    digests = run(["image", "inspect", "--format", "{{json .RepoDigests}}", container["Image"]])
    _require(type(digests) is list and all(type(d) is str and len(d) <= 256 for d in digests)
             and len(digests) <= 16, "docker_output_invalid")
    return {"container_id": container_id, "image_id": container["Image"],
            "repo_digests": sorted(digests),
            "port": [{"container": "5432/tcp", "host_ip": m["HostIp"],
                      "host_port": m["HostPort"]} for m in mapping],
            "database_port": port}


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
    # Before any SQL: the port every query below uses must be the one the
    # service container publishes (PR #100 review, comment 4174199627).
    journal.record("database_container", container_identity(docker_path, container_id,
                                                             deadline_ns, port))
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


def drop_database(job_root, journal, *, create_journal_bytes, port, superuser,
                  superuser_password, psql_path, deadline_ns):
    """Run inside the open `database-drop` StageJournal, which run_drop_stage
    opens only after its gate (writer_quiescence or no_launch_quiescence)
    passed. Only RECORDED objects that match on cluster, name and oid are
    dropped. Appends exactly one database_drop record and returns its
    verdict."""
    role_state, database_state, role_record, database_record = \
        allocation_states(create_journal_bytes)
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


# ---------------------------------------------------------------------------
# Part B: the no-launch gate and the registry stage

def no_launch_quiescence(job_root, prefix):
    """Gate for database-drop when nothing was launched (design section 4,
    DBC023-N4). Evaluated BEFORE the drop journal is opened. PREFIX is the
    stage list ending with database-create. Requires the prefix closed (VALID
    or FAILED, never INCOMPLETE), the journal directory holding exactly the
    prefix journals (aggregate's exact-set rule: no launch or later journal),
    and no reference/ or observer/ directory (no launch receipt, no observer
    socket). Anything not positively established refuses."""
    _require(type(prefix) is list and prefix and prefix[-1] == CREATE_STAGE,
             "no_launch_prefix_invalid")
    aggregation = sj.aggregate(job_root, prefix)
    _require(aggregation["verdict"] in ("VALID", "FAILED"), "no_launch_prefix_not_closed")
    for name in ("reference", "observer"):
        try:
            os.lstat(os.path.join(job_root, name))
        except FileNotFoundError:
            continue
        raise DatabaseStageError("launch_evidence_present")


SQL_KEYS = """-- alrust:op=registry_keys
SELECT json_build_object('keys', coalesce(json_agg(key), '[]'::json))
FROM (SELECT key FROM keys ORDER BY key LIMIT :limit) AS k;
"""
_URL = re.compile(r"postgresql://(alrust_[a-z0-9_]{1,56}):([0-9a-f]{64})"
                  r"@127\.0\.0\.1:([0-9]{1,5})/(alrust_[a-z0-9_]{1,56})\Z")


def _read_private_file(path, limit, dir_fd=None):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                 dir_fd=dir_fd)
    try:
        info = os.fstat(fd)
        _require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                 and stat.S_IMODE(info.st_mode) == 0o600 and info.st_size <= limit,
                 "private_file_invalid")
        data = os.read(fd, limit + 1)
        _require(len(data) == info.st_size, "private_file_invalid")
        return data, info
    finally:
        os.close(fd)


def build_registry(job_root, journal, *, secret_key_path, other_secrets, port,
                   psql_path, deadline_ns):
    """The registry stage (contract section 9 step 2), inside the open
    `registry` StageJournal, which run_registry_stage opens only after
    writer_quiescence passed: before any private file is opened or any key
    is enumerated. Then: the job
    role's URL and password from the 0600 URL file, SECRET_KEY from its 0600
    file (recorded as an owned resource), every keys.key value read as the
    job role (bounded: one row more than the room left is a refusal, never a
    truncation), and OTHER_SECRETS (the provisioner and entrypoint secrets;
    their file interface is Codex's and not yet approved, Q1). The registry
    is written in the finalizer's format, 0600, exclusively, and recorded."""
    from . import finalizer as fz
    directory, _ = _database_dir(job_root, create=False)
    url_bytes, _ = _read_private_file(os.path.join(directory, URL_NAME), 1024)
    url = url_bytes.decode("ascii", "replace").rstrip("\n")
    match = _URL.fullmatch(url)
    _require(match and match[1] == match[4] and int(match[3]) == port, "url_invalid")
    role, password = match[1], match[2]
    key_bytes, key_info = _read_private_file(secret_key_path, fz.MAX_SECRET)
    journal.record("owned_resource", {"path": secret_key_path, "dev": key_info.st_dev,
                                      "ino": key_info.st_ino, "type": "file"})
    try:
        secret_key = key_bytes.decode("utf-8").rstrip("\n")
    except UnicodeError:
        raise DatabaseStageError("secret_key_invalid") from None
    _require(type(other_secrets) is list and all(type(v) is str for v in other_secrets),
             "other_secrets_invalid")
    fixed = list(other_secrets) + [secret_key, url, password]
    room = fz.MAX_SECRETS - len(fixed)
    _require(room >= 0, "registry_incomplete")
    client = Psql(psql_path, port=port, user=role, password=password,
                  database_dir=directory, deadline_ns=deadline_ns)
    value = client.query_json(SQL_KEYS, role, [("limit", str(room + 1))])
    _require(set(value) == {"keys"} and type(value["keys"]) is list, "psql_output_invalid")
    keys = value["keys"]
    _require(len(keys) <= room, "registry_incomplete")
    secrets_out = []
    for item in keys + fixed:
        _require(type(item) is str, "registry_incomplete")
        try:
            size = len(item.encode("utf-8"))
        except UnicodeError:
            raise DatabaseStageError("registry_incomplete") from None
        _require(fz.MIN_SECRET <= size <= fz.MAX_SECRET, "registry_incomplete")
        if item not in secrets_out:
            secrets_out.append(item)
    path = os.path.join(job_root, fz.REGISTRY_NAME)
    data = (json.dumps({"schema": fz.SCHEMA_REGISTRY, "secrets": secrets_out},
                       sort_keys=True) + "\n").encode()
    _require(len(data) <= fz.MAX_REGISTRY, "registry_incomplete")
    _write_private(path, data)
    info = os.lstat(path)
    journal.record("credential_registry", {"path": path, "dev": info.st_dev,
                                           "ino": info.st_ino,
                                           "sha256": hashlib.sha256(data).hexdigest()})
    return path


# ---------------------------------------------------------------------------
# Stage runners: gate first, then the journal, then the action

def _gate_code(error):
    if isinstance(error, DatabaseStageError):
        return error.code
    if isinstance(error, (sj.JournalError,)) or type(error).__name__ == "FinalizerError":
        code = str(error)
        return code if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code) else "gate_refused"
    return "gate_unavailable"


def _run_stage(job_root, prefix, name, gate, action):
    """Evaluate GATE (read-only) with no journal of this stage open, then open
    it (index len(prefix), previous prefix[-1], binding that journal's sha256)
    and either record the refusal and close FAILED, or run ACTION with the
    gate's own result. ACTION(journal, evidence) returns (result, failure
    code or None)."""
    refusal = evidence = None
    try:
        evidence = gate()
    except Exception as error:  # every gate failure is a fixed-code refusal
        refusal = _gate_code(error)
    with sj.StageJournal(job_root, len(prefix), name, prefix[-1]) as journal:
        if refusal is not None:
            journal.fail(refusal)
            raise DatabaseStageError(refusal)
        try:
            result, failure = action(journal, evidence)
        except DatabaseStageError as error:
            journal.fail(error.code)
            raise
        if failure is not None:
            journal.fail(failure)
            raise DatabaseStageError(failure)
        return result


INVENTORY_NAME = "entrypoint-secrets.json"


def _open_bound_root(job_root, core_sha256):
    """Q1R44-R1: open JOB_ROOT one component at a time from "/", each with
    O_DIRECTORY|O_NOFOLLOW relative to the previous held directory, so a
    symlink anywhere in the path (ancestors included) refuses at this
    boundary. The held root must be a uid-owned 0700 directory whose
    core-identity.json, read through it, has exactly the gate's validated
    core digest and names this path, device and inode as its job root.
    Returns the held descriptor; the caller closes it. Not a boundary
    against a hostile same-uid writer."""
    _require(type(job_root) is str and os.path.isabs(job_root)
             and os.path.normpath(job_root) == job_root and job_root != "/",
             "inventory_root_changed")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        fd = os.open("/", flags)
    except OSError:
        raise DatabaseStageError("inventory_root_changed") from None
    try:
        for name in job_root.split("/")[1:]:
            try:
                child = os.open(name, flags, dir_fd=fd)
            except OSError:
                raise DatabaseStageError("inventory_root_changed") from None
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        _require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
                 and stat.S_IMODE(info.st_mode) == 0o700, "inventory_root_changed")
        try:
            core, _ = _read_private_file(sj.CORE_NAME, sj.MAX_CORE, dir_fd=fd)
        except OSError:
            raise DatabaseStageError("inventory_root_changed") from None
        _require(hashlib.sha256(core).hexdigest() == core_sha256, "inventory_root_changed")
        try:
            bound = json.loads(core)["job_root"]
        except (ValueError, TypeError, KeyError):
            raise DatabaseStageError("inventory_root_changed") from None
        _require(bound == {"path": job_root, "dev": info.st_dev, "ino": info.st_ino},
                 "inventory_root_changed")
        held, fd = fd, None
        return held
    finally:
        if fd is not None:
            os.close(fd)


def read_inventory(job_root, evidence):
    """Q1 reader, steps 2 and 3 of the approved interface (6e2bfcc8), run
    only after writer_quiescence passed: EVIDENCE is that gate's validated
    result, so the closed COMPLETE entrypoint journal and the core digest
    come from it, never from the snapshot. Requires exactly one
    private_secret_inventory record, in the entrypoint stage, with the exact
    payload {path, dev, ino, sha256, count} and the fixed path; then the
    snapshot, opened without following links relative to the job root held
    open by _open_bound_root (no symlinked component anywhere in the path;
    bound to the validated core's digest and job-root device/inode), must be
    a private regular single-link file of that identity and digest, and
    decode against the independent core digest and count.
    Returns the private values; never log or repr them."""
    from . import finalizer as fz
    from . import private_secret_inventory as inventory
    aggregation = evidence["aggregation"]
    root = _open_bound_root(job_root, aggregation["core_identity_sha256"])
    try:
        found = [(stage, payload) for stage, kind, payload
                 in fz._journal_records(job_root, aggregation)
                 if kind == "private_secret_inventory"]
        _require(len(found) == 1 and found[0][0] == "entrypoint",
                 "inventory_record_missing_or_repeated")
        record = found[0][1]
        path = os.path.join(job_root, INVENTORY_NAME)
        _require(type(record) is dict
                 and set(record) == {"path", "dev", "ino", "sha256", "count"}
                 and record["path"] == path
                 and all(type(record[k]) is int for k in ("dev", "ino", "count"))
                 and type(record["sha256"]) is str, "inventory_record_invalid")
        try:
            data, info = _read_private_file(INVENTORY_NAME, inventory.MAX_INVENTORY,
                                            dir_fd=root)
        except OSError:
            raise DatabaseStageError("inventory_unreadable") from None
    finally:
        os.close(root)
    _require(info.st_nlink == 1
             and (info.st_dev, info.st_ino) == (record["dev"], record["ino"])
             and hashlib.sha256(data).hexdigest() == record["sha256"], "inventory_changed")
    try:
        return inventory.decode_inventory(
            data, expected_core_sha256=aggregation["core_identity_sha256"],
            expected_count=record["count"])
    except inventory.InventoryError as error:
        raise DatabaseStageError(str(error)) from None


def run_registry_stage(job_root, prefix, **kwargs):
    """The `registry` stage, gated by the finalizer's writer_quiescence over
    PREFIX (through stop). After the gate: the entrypoint inventory is read
    (read_inventory) and its values join the registry as other secrets;
    the frozen registry is re-read and must contain EVERY inventory value
    (the value-set inclusion gate; the finalizer's count check is only a
    lower bound); only then is obtained_secret_count {count,
    registry_sha256} recorded, in this registry journal, exactly once.
    Returns the registry path."""
    from . import finalizer as fz

    def action(journal, evidence):
        values = read_inventory(job_root, evidence)
        extra = list(kwargs.pop("other_secrets", []))
        path = build_registry(job_root, journal, other_secrets=extra + values, **kwargs)
        data, _ = _read_private_file(path, fz.MAX_REGISTRY)
        digest = hashlib.sha256(data).hexdigest()
        try:
            frozen = set(fz.load_registry(job_root, digest))
        except fz.FinalizerError as error:
            raise DatabaseStageError(str(error)) from None
        _require(all(value.encode("utf-8") in frozen for value in values),
                 "registry_inclusion_failed")
        journal.record("obtained_secret_count", {"count": len(values),
                                                 "registry_sha256": digest})
        return path, None
    return _run_stage(job_root, prefix, "registry",
                      lambda: fz.writer_quiescence(job_root, prefix), action)


def registry_frozen(job_root, evidence):
    """DBFE-R2: on the launched path, the database may be dropped only after
    the credential registry was frozen from it. Checked AFTER the writer gate
    passed (so the gate's N3 read set is unchanged), using the gate's own
    validated aggregation: a COMPLETE `registry` stage after `stop` holding
    exactly one credential_registry record whose path, identity and sha256
    match the registry file now."""
    from . import finalizer as fz
    aggregation = evidence["aggregation"]
    names = [stage["name"] for stage in aggregation["stages"]]
    _require("registry" in names and names.index("registry") > names.index("stop"),
             "registry_not_frozen")
    records = [(stage, kind, payload) for stage, kind, payload
               in fz._journal_records(job_root, aggregation) if kind == "credential_registry"]
    _require(len(records) == 1 and records[0][0] == "registry", "registry_not_frozen")
    record = records[0][2]
    path = os.path.join(job_root, fz.REGISTRY_NAME)
    _require(type(record) is dict and set(record) == {"path", "dev", "ino", "sha256"}
             and record["path"] == path, "registry_not_frozen")
    data, info = _read_private_file(path, fz.MAX_REGISTRY)
    _require((info.st_dev, info.st_ino) == (record["dev"], record["ino"])
             and hashlib.sha256(data).hexdigest() == record["sha256"], "registry_changed")


def run_drop_stage(job_root, prefix, *, launched, **kwargs):
    """The `database-drop` stage. LAUNCHED selects the gate: writer_quiescence
    over PREFIX followed by registry_frozen (DBFE-R2), or
    no_launch_quiescence for an allocation that never reached launch. The stage closes COMPLETE only for verdict COMPLETE; any other
    verdict is recorded, then the stage closes FAILED with that verdict."""
    from . import finalizer as fz
    _require(CREATE_STAGE in prefix, "drop_prefix_invalid")
    create = sj.read_bounded(sj.journal_path(job_root, prefix.index(CREATE_STAGE),
                                             CREATE_STAGE), sj.MAX_JOURNAL)
    if launched:
        def gate():
            # The registry check runs only after the writer gate passed.
            registry_frozen(job_root, fz.writer_quiescence(job_root, prefix))
    else:
        def gate():
            # A positively proven no-launch cleanup never needs a registry.
            no_launch_quiescence(job_root, prefix)

    def action(journal, evidence):
        verdict = drop_database(job_root, journal, create_journal_bytes=create, **kwargs)
        return verdict, None if verdict == "COMPLETE" else "drop_" + verdict.lower()
    return _run_stage(job_root, prefix, DROP_STAGE, gate, action)
