#!/usr/bin/env python3
"""Launch the built reference server under the supervisor and hand it off.

Contract: docs/ci-launch-attestation-contract.md, sections 4 and 5 (SS-I1).
Run as `python3 -I -S -B launch-reference.py --job-root J --venv V --port N
--ready-timeout S`. run-reference.sh calls it in JOB_ROOT mode twice: first
with a leading --preflight, BEFORE building V, which applies every refusal
below that does not need the built venv (V must not exist yet) and starts
nothing; then, after the build, without it.

The launcher:
- refuses a job root that is not a private (0700) directory owned by this
  user, a venv outside the job root, an /etc/alertad.conf that is present
  or unreadable, an environment outside the allowlist, and a busy port;
- starts the supervisor, its own direct child P, with alertad as P's child
  C (`--no-reload --no-debugger`);
- cross-checks P's self-written launch receipt against its own
  observations (pid, process group, start_ticks, uid, boot_id, venv
  identity, terminal path);
- waits for owned readiness: P alive, a listener on 127.0.0.1:PORT held by
  C or a descendant of P, a healthy /management/healthcheck, and C running
  the venv's interpreter;
- prints one line: the receipt path and its sha256. That printed digest is
  the only trusted binding for the stop helper.

On any failure, or on TERM or INT, it signals only P, its own unreaped child,
waits a bounded time for P's exit, and reports the stop helper's verdict. It
never signals a group or any other process.

Stage mode (`--stage-index N [--previous-stage NAME]`, contract section 3):
the launch runs inside its own stage journal, `launch`, chained to the core
identity and the previous stage. The preflight also checks that the stage can
begin. The journal records, as they are observed:
- `owned_resource` for the venv, HOME (if it exists) and the observer socket
  directory, with their identities;
- `launch_receipt`: the receipt path and the digest the ready line prints;
- `launch_readiness`, after readiness and before the ready line: the receipt
  digest, both generations (re-read and equal to the receipt's), C's
  /proc/C/exe identity and the resolved venv bin/python3 identity (re-read
  and unchanged), and the monotonic time of that observation.

The stage closes COMPLETE after the ready line was written and BEFORE the
launcher gives up ownership of P (LB80-R1). If that close fails (an append,
fsync or close error), the launcher stops P exactly as on any other failure,
reports the stop helper's verdict and exits 1. What the journal then holds
depends on where the close failed (LB4E-R2):
- before stage_complete was written: FAILED, or no stage_complete
  (INCOMPLETE);
- after the full stage_complete COMPLETE bytes were written (a failed fsync
  or close): a syntactically COMPLETE record of a launch that FAILED. Those
  bytes are not rewritten or truncated.

A COMPLETE launch stage is therefore never, on its own, proof of a hand-off.
The proof is the next stage, `launch-confirm` (confirm-launch.py), run only
after this launcher exited 0. It observes the receipt's exact P and C
generations alive. A stopped tree fails it. In stage mode, a ready line
followed by a non-zero exit is a failure whose tree has been stopped; only
exit 0 followed by a COMPLETE launch-confirm hands the server off. Any other
failure closes the stage FAILED with a fixed code. These are earlier observations for a later
qualifier to compare with its own, never a substitute for them. Without the
stage options, the launcher behaves as before.
"""
import hashlib
import json
import os
import signal
import stat
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SUPERVISOR = os.path.join(HERE, "reference-supervisor.py")
STOP = os.path.join(HERE, "stop-reference.py")
EXTERNAL_CONFIG = "/etc/alertad.conf"
SCHEMA_LAUNCH = "alrust-reference-launch/1"
ALLOWED_ENV = {
    "PATH", "LANG", "HOME", "DATABASE_URL", "SECRET_KEY", "AUTH_REQUIRED",
    "ALERTA_SVR_CONF_FILE", "PORT", "VENV", "REFERENCE_PYTHON",
    "READY_TIMEOUT", "JOB_ROOT",
    # Set by the shell that execs this launcher.
    "PWD", "OLDPWD", "SHLVL", "_",
}
# Observer installation (contract section 6). The activator imports the
# reviewed module and calls its explicit activate_from_environment() only
# when the supervisor has set the socket variable, which it does for the
# served child C alone. The helper itself checks that the actual parent is P
# (ALRUST_OBSERVER_PARENT) and is inert otherwise; importing the module does
# nothing by itself. site.py prints and skips an exception raised by a .pth
# line, so a failed activation leaves the server running WITHOUT an observer:
# downstream qualification must treat a missing or refused observer as a
# failure, never as a pass (AN-1).
OBSERVER_MODULE_NAME = "alrust_observer.py"
# The installable observer files: the producer module (required) and the
# reviewed protocol codec it imports as a top-level module.
OBSERVER_FILE_NAMES = (OBSERVER_MODULE_NAME, "alrust_observer_protocol.py")
OBSERVER_MANIFEST_SCHEMA = "alrust-observer-install/1"
OBSERVER_ACTIVATOR_NAME = "alrust_observer.pth"
OBSERVER_ACTIVATOR = (b'import os; os.environ.get("ALRUST_OBSERVER_SOCKET") '
                      b'and __import__("alrust_observer").activate_from_environment()\n')
MAX_OBSERVER_MODULE = 1 << 20
STOP_WAIT_SECONDS = 20.0
POLL_SECONDS = 0.2
MAX_RECEIPT = 65536
STAGE_NAME = "launch"
MAX_STAGE_INDEX = 31

stop_signal = None


def on_signal(signum, frame):
    global stop_signal
    stop_signal = signum


class Refused(Exception):
    pass


def log(message):
    sys.stderr.write("launch-reference: %s\n" % message)
    sys.stderr.flush()


class _NoStage:
    """Without stage options: nothing is journaled."""

    def record(self, kind, payload):
        pass

    def fail(self, code):
        pass


class LaunchFailed(Exception):
    def __init__(self, rc):
        super().__init__(rc)
        self.rc = rc


def stage_journal_module():
    """The reviewed stage-journal module of this checkout (stdlib only). With
    -I only this script's directory is on sys.path, so the conformance
    directory is added explicitly, and only in stage mode."""
    conformance = os.path.dirname(HERE)
    if conformance not in sys.path:
        sys.path.insert(0, conformance)
    from alrust_conformance import stage_journal
    return stage_journal


def check_stage(sj, job_root, stage):
    """The launch stage can begin: a valid core identity for this job root,
    the previous stage's journal present, and no launch journal yet."""
    index, previous = stage
    # Validates the stage index and names without opening anything (LB80-N1).
    sj.StageJournal(job_root, index, STAGE_NAME, previous)
    sj.validate_core(sj.read_bounded(os.path.join(job_root, sj.CORE_NAME), sj.MAX_CORE),
                     job_root)
    if previous is not None and not os.path.isfile(
            sj.journal_path(job_root, index - 1, previous)):
        raise Refused("previous stage journal missing")
    if os.path.lexists(sj.journal_path(job_root, index, STAGE_NAME)):
        raise Refused("launch stage journal already exists")


def parse_args(argv):
    """--job-root J --venv V --port N --ready-timeout S, plus zero or more
    --observer-file NAME SHA256 PATH (NAME from OBSERVER_FILE_NAMES), plus
    optionally --stage-index N and, for N > 0, --previous-stage NAME."""
    opts = {"--job-root": None, "--venv": None, "--port": None,
            "--ready-timeout": None, "--stage-index": None, "--previous-stage": None}
    files, i = [], 0
    while i < len(argv):
        name = argv[i]
        if name == "--observer-file" and len(argv) >= i + 4:
            files.append(tuple(argv[i + 1:i + 4]))
            i += 4
        elif name in opts and opts[name] is None and i + 1 < len(argv):
            opts[name] = argv[i + 1]
            i += 2
        else:
            raise Refused("usage: --job-root J --venv V --port N --ready-timeout S "
                          "[--observer-file NAME SHA256 PATH]... "
                          "[--stage-index N [--previous-stage NAME]]")
    for name in ("--job-root", "--venv"):
        if opts[name] is None or not os.path.isabs(opts[name]) \
                or os.path.normpath(opts[name]) != opts[name]:
            raise Refused("%s must be an absolute normalized path" % name)
    for name, high in (("--port", 65535), ("--ready-timeout", 9999)):
        value = opts[name]
        if value is None or not value.isdigit() or not 1 <= int(value) <= high \
                or value[0] == "0":
            raise Refused("%s out of range" % name)
    names = [name for name, _, _ in files]
    if files and sorted(names) != sorted(OBSERVER_FILE_NAMES):
        raise Refused("observer files must be exactly %s, once each"
                      % ", ".join(OBSERVER_FILE_NAMES))
    for name, pin, path in files:
        if not os.path.isabs(path) or os.path.normpath(path) != path:
            raise Refused("observer file paths must be absolute and normalized")
        if len(pin) != 64 or pin.strip("0123456789abcdef"):
            raise Refused("observer pins must be 64 lowercase hex characters")
    stage = None
    index, previous = opts["--stage-index"], opts["--previous-stage"]
    if index is not None or previous is not None:
        if index is None or not index.isdigit() or (len(index) > 1 and index[0] == "0") \
                or int(index) > MAX_STAGE_INDEX:
            raise Refused("--stage-index out of range")
        if (int(index) == 0) != (previous is None):
            raise Refused("--previous-stage is required exactly when --stage-index > 0")
        stage = (int(index), previous)
    return (opts["--job-root"], opts["--venv"], int(opts["--port"]),
            int(opts["--ready-timeout"]), sorted(files) or None, stage)


def read_observer_module(module, pin):
    """A file's bytes, read once without following links or blocking, and
    only if they hash to the reviewed pin."""
    fd = os.open(module, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
                 | os.O_NONBLOCK | os.O_NOCTTY)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_OBSERVER_MODULE:
            raise Refused("observer module is not a bounded regular file")
        chunks, size = [], 0
        while size <= MAX_OBSERVER_MODULE:
            chunk = os.read(fd, MAX_OBSERVER_MODULE + 1 - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
    finally:
        os.close(fd)
    data = b"".join(chunks)
    if size > MAX_OBSERVER_MODULE or hashlib.sha256(data).hexdigest() != pin:
        raise Refused("observer module does not match its reviewed pin")
    return data


def site_packages(venv):
    """The venv's single site-packages directory, symlink-free."""
    lib = os.path.join(venv, "lib")
    found = [os.path.join(lib, name, "site-packages") for name in os.listdir(lib)
             if name.startswith("python3.")]
    found = [path for path in found if os.path.lexists(path)]
    if len(found) != 1:
        raise Refused("venv must have exactly one site-packages directory")
    path = found[0]
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or os.path.realpath(path) != path:
        raise Refused("venv site-packages must be a real directory")
    return path


def write_new(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                 | os.O_CLOEXEC, 0o600)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)


def verify_observer_install(manifest):
    """Read back every installed file and the activator, before the server is
    spawned, and require the exact bytes the manifest records."""
    entries = manifest["files"] + [manifest["activator"]]
    for entry in entries:
        fd = os.open(entry["path"], os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
                     | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_OBSERVER_MODULE:
                raise Refused("installed observer file is not a bounded regular file")
            data = os.read(fd, MAX_OBSERVER_MODULE + 1)
        finally:
            os.close(fd)
        if hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise Refused("installed observer file differs from its manifest")


def install_observer(job_root, venv, files, rundir):
    """Install the pinned files and the activator into the fresh venv, create
    the private socket directory, and write the install manifest that binds
    every installed path and digest. Returns the receipt record."""
    site = site_packages(venv)
    # Read and pin-check EVERY file before writing any, so a bad file never
    # leaves a partial install. The bytes checked are the bytes installed.
    verified = [(name, read_observer_module(path, pin))
                for name, pin, path in files]
    activator = os.path.join(site, OBSERVER_ACTIVATOR_NAME)
    for target in [os.path.join(site, name) for name, _ in verified] + [activator]:
        if os.path.lexists(target):
            raise Refused("observer destination already exists in the venv")
    installed = []
    for name, data in verified:
        target = os.path.join(site, name)
        write_new(target, data)
        installed.append({"name": name, "path": target, "size": len(data),
                          "sha256": hashlib.sha256(data).hexdigest()})
    write_new(activator, OBSERVER_ACTIVATOR)
    manifest = {"schema": OBSERVER_MANIFEST_SCHEMA, "site_packages": site,
                "files": installed,
                "activator": {"name": OBSERVER_ACTIVATOR_NAME, "path": activator,
                              "sha256": hashlib.sha256(OBSERVER_ACTIVATOR)
                              .hexdigest()}}
    data = (json.dumps(manifest, sort_keys=True) + "\n").encode()
    manifest_path = os.path.join(rundir, "observer-install.json")
    write_new(manifest_path, data)
    verify_observer_install(manifest)
    socket_dir = os.path.join(job_root, "observer")
    os.mkdir(socket_dir, 0o700)
    return {"socket": os.path.join(socket_dir, "observer.sock"),
            "manifest": manifest_path,
            "manifest_sha256": hashlib.sha256(data).hexdigest()}


def check_job_root(job_root):
    info = os.lstat(job_root)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() \
            or info.st_mode & 0o077:
        raise Refused("job root must be a 0700 directory owned by this user")
    if os.path.realpath(job_root) != job_root:
        raise Refused("job root must not traverse symlinks")
    return info


def check_home(job_root):
    """HOME is an absolute normalized path strictly inside the job root, with
    symlink-free parents; if it exists, it is a real directory of this user
    (SS-I1-HOME-1)."""
    home = os.environ.get("HOME", "")
    parent = os.path.dirname(home)
    if not os.path.isabs(home) or os.path.normpath(home) != home \
            or not home.startswith(job_root + os.sep) \
            or os.path.realpath(parent) != parent:
        raise Refused("HOME must be inside the job root, without symlinks")
    try:
        info = os.lstat(home)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise Refused("HOME must be a real directory of this user")


def check_venv_path(job_root, venv):
    """V lies strictly inside the job root, with no symlink in its parents."""
    parent = os.path.dirname(venv)
    if not venv.startswith(job_root + os.sep) \
            or os.path.realpath(parent) != parent:
        raise Refused("venv must be inside the job root, without symlinks")


def check_venv(job_root, venv):
    check_venv_path(job_root, venv)
    if os.path.realpath(venv) != venv:
        raise Refused("venv must be inside the job root, without symlinks")
    info = os.lstat(venv)
    if not stat.S_ISDIR(info.st_mode):
        raise Refused("venv is not a directory")
    for name in ("bin/alertad", "bin/python3"):
        if not os.path.exists(os.path.join(venv, name)):
            raise Refused("venv lacks %s" % name)
    return info


def check_external_config():
    try:
        os.lstat(EXTERNAL_CONFIG)
    except FileNotFoundError:
        return
    except OSError as error:
        raise Refused("%s unreadable: %s" % (EXTERNAL_CONFIG, error))
    raise Refused("%s exists; refusing hidden configuration" % EXTERNAL_CONFIG)


def check_environment():
    extra = sorted(set(os.environ) - ALLOWED_ENV)
    if extra:
        raise Refused("environment outside the allowlist: %s" % ", ".join(extra))


LOOPBACK_V4 = "0100007F"  # 127.0.0.1 as /proc/net/tcp writes it


def listeners(port):
    """{inode: (table, address)} for every TCP listener on PORT, any address."""
    found = {}
    for table in ("tcp", "tcp6"):
        with open("/proc/net/" + table) as f:
            next(f)
            for line in f:
                fields = line.split()
                address, state, inode = fields[1], fields[3], fields[9]
                host, _, hexport = address.rpartition(":")
                if state == "0A" and int(hexport, 16) == port:
                    found[inode] = (table, host)
    return found


def owned_readiness(port, tree):
    """Every listener on PORT, on any address, is held by P's tree, and one of
    them is on 127.0.0.1, where the health check connects. A foreign
    listener anywhere on the port means not ready."""
    found = listeners(port)
    if not found or set(found) - owned_inodes(tree):
        return False
    return ("tcp", LOOPBACK_V4) in found.values()


def read_stat(pid):
    with open("/proc/%d/stat" % pid, "rb") as f:
        rest = f.read(4096).rsplit(b")", 1)[1].split()
    return int(rest[1]), int(rest[2]), int(rest[19])  # ppid, pgrp, starttime


def descendants(root):
    """PIDs whose parent chain reaches ROOT (ROOT excluded)."""
    parent = {}
    for name in os.listdir("/proc"):
        if name.isdigit():
            try:
                parent[int(name)] = read_stat(int(name))[0]
            except (OSError, ValueError, IndexError):
                continue
    found = set()
    for pid in parent:
        seen, cur = 0, pid
        while cur in parent and seen < 256:
            cur, seen = parent[cur], seen + 1
            if cur == root:
                found.add(pid)
                break
    return found


def owned_inodes(pids):
    """Socket inodes held open by any of PIDS."""
    inodes = set()
    for pid in pids:
        try:
            for fd in os.listdir("/proc/%d/fd" % pid):
                try:
                    target = os.readlink("/proc/%d/fd/%s" % (pid, fd))
                except OSError:
                    continue
                if target.startswith("socket:[") and target.endswith("]"):
                    inodes.add(target[8:-1])
        except OSError:
            continue
    return inodes


def boot_id():
    with open("/proc/sys/kernel/random/boot_id") as f:
        return f.read().strip()


def read_receipt(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_RECEIPT:
            raise Refused("launch receipt is not a bounded regular file")
        chunks, size = [], 0
        while size <= MAX_RECEIPT:
            chunk = os.read(fd, MAX_RECEIPT + 1 - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
    finally:
        os.close(fd)
    if size > MAX_RECEIPT:
        raise Refused("launch receipt too large")
    return b"".join(chunks)


def parse_receipt(data):
    try:
        record = json.loads(data)
        sup, child = record["supervisor"], record["child"]
        claimed = (record["schema"], sup["pid"], sup["pgid"], sup["start_ticks"],
                   sup["uid"], sup["subreaper"], record["boot_id"],
                   record["venv"]["dev"], record["venv"]["ino"],
                   record["terminal_receipt"], record.get("observer"))
        child_pid = child["pid"]
    except (KeyError, TypeError, ValueError, RecursionError):
        raise Refused("launch receipt malformed")
    return claimed, child_pid


def supervisor_matches(data, observed):
    """P's receipt agrees with the launcher's own observation of P."""
    claimed, _ = parse_receipt(data)
    return claimed == observed


def cross_check(data, pid, observed):
    """Compare P's self-written receipt with this launcher's observations."""
    claimed, child_pid = parse_receipt(data)
    if claimed != observed or read_stat(pid)[1] != pid:
        raise Refused("launch receipt disagrees with the launcher's observation")
    if type(child_pid) is not int or read_stat(child_pid)[0] != pid:
        raise Refused("receipt child is not the supervisor's child")
    return child_pid


def healthy(port, seconds):
    try:
        with urllib.request.urlopen(
                "http://127.0.0.1:%d/management/healthcheck" % port,
                timeout=seconds) as response:
            return response.status == 200
    except (OSError, ValueError):
        return False


def stop_supervisor(proc, receipt, digest, observed, job_root):
    """Signal only P (our own unreaped child); report the stop verdict."""
    if proc.poll() is None:
        try:
            proc.send_signal(signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        code = proc.wait(timeout=STOP_WAIT_SECONDS)
    except subprocess.TimeoutExpired:
        log("cleanup UNPROVEN: the supervisor did not exit")
        return False
    if digest is None and observed is not None:
        # P may have finished before its receipt was checked. Its generation
        # was observed at spawn, so the receipt can still be bound to it.
        try:
            data = read_receipt(receipt)
            if data.endswith(b"\n") and supervisor_matches(data, observed):
                digest = hashlib.sha256(data).hexdigest()
        except (Refused, OSError):
            pass
    if digest is None:
        if code in (64, 70):
            log("cleanup complete: the supervisor exited before starting a child")
            return True
        log("cleanup UNPROVEN: no validated launch receipt (supervisor exit %d)"
            % code)
        return False
    out = subprocess.run([sys.executable, "-I", "-S", "-B", STOP, receipt, digest,
                          job_root],
                         capture_output=True, text=True, timeout=60)
    log("cleanup verdict: %s" % out.stdout.strip())
    return out.returncode == 0


def main(argv):
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    preflight = argv[:1] == ["--preflight"]
    try:
        job_root, venv, port, ready_timeout, observer, stage = \
            parse_args(argv[preflight:])
        check_environment()
        check_external_config()
        check_job_root(job_root)
        check_home(job_root)
        sj = None
        if stage is not None:
            sj = stage_journal_module()
            check_stage(sj, job_root, stage)
        if preflight:
            check_venv_path(job_root, venv)
            if os.path.lexists(venv):
                raise Refused("venv %s already exists" % venv)
            if os.path.lexists(os.path.join(job_root, "reference")) \
                    or os.path.lexists(os.path.join(job_root, "observer")):
                raise Refused("job root already holds a launch")
            for name, pin, path in observer or ():
                read_observer_module(path, pin)
            if listeners(port):
                raise Refused("port %d already in use" % port)
            return 0
    except (Refused, OSError, ValueError) as error:
        log("refused: %s" % error)
        return 1
    if stage is None:
        return launch(job_root, venv, port, ready_timeout, observer, _NoStage(), None)
    journal = sj.StageJournal(job_root, stage[0], STAGE_NAME, stage[1])
    try:
        journal.__enter__()
    except (OSError, ValueError) as error:
        log("refused: stage journal could not begin: %s" % error)
        return 1
    rc = 1
    try:
        # launch() closes the stage COMPLETE itself, while it still owns P.
        rc = launch(job_root, venv, port, ready_timeout, observer, journal,
                    lambda: journal.__exit__(None, None, None))
    finally:
        if rc != 0:
            # FAILED with the code launch() set. A journal whose COMPLETE close
            # already failed is closed and stays without stage_complete.
            journal.__exit__(LaunchFailed, LaunchFailed(rc), None)
    return rc


def launch(job_root, venv, port, ready_timeout, observer, stage, close_stage):
    """The launch after the shared checks; stage is a StageJournal or
    _NoStage, and close_stage (stage mode only) records stage_complete
    COMPLETE. Returns the exit status; 0 only after a hand-off."""
    try:
        venv_info = check_venv(job_root, venv)
        if listeners(port):
            raise Refused("port %d already in use" % port)
        stage.record("owned_resource", {"path": venv, "dev": venv_info.st_dev,
                                        "ino": venv_info.st_ino, "type": "dir"})
        home = os.environ["HOME"]
        try:
            home_info = os.lstat(home)
        except FileNotFoundError:
            home_info = None
        if home_info is not None:
            stage.record("owned_resource", {"path": home, "dev": home_info.st_dev,
                                            "ino": home_info.st_ino, "type": "dir"})
        rundir = os.path.join(job_root, "reference")
        os.mkdir(rundir, 0o700)
        receipt = os.path.join(rundir, "launch.json")
        terminal = os.path.join(rundir, "terminal.json")
        server_log = os.open(os.path.join(rundir, "server.log"),
                             os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
                             0o600)
        # The installed bytes are re-read and re-hashed here, so the pin
        # binds what is installed, not what the preflight saw.
        observer_record = None if observer is None else \
            install_observer(job_root, venv, observer, rundir)
        if observer_record is not None:
            socket_dir = os.path.dirname(observer_record["socket"])
            socket_info = os.lstat(socket_dir)
            stage.record("owned_resource", {"path": socket_dir,
                                            "dev": socket_info.st_dev,
                                            "ino": socket_info.st_ino, "type": "dir"})
        with open(os.path.join(HERE, "..", "..", "reference", "alerta",
                               "VERSION")) as f:
            version = f.read().strip()
    except (Refused, OSError, ValueError) as error:
        log("refused: %s" % error)
        stage.fail("launch_refused")
        return 1

    command = [os.path.join(venv, "bin", "alertad"), "run", "--host", "127.0.0.1",
               "--port", str(port), "--no-reload", "--no-debugger"]
    deadline = time.monotonic() + ready_timeout
    proc = subprocess.Popen(
        [sys.executable, "-I", "-S", "-B", SUPERVISOR, "--receipt", receipt,
         "--terminal", terminal, "--venv", venv]
        + ([] if observer_record is None else
           ["--observer-socket", observer_record["socket"],
            "--observer-manifest", observer_record["manifest"],
            "--observer-manifest-sha256", observer_record["manifest_sha256"]])
        + ["--"] + command,
        process_group=0, stdin=subprocess.DEVNULL, stdout=server_log,
        stderr=server_log)
    os.close(server_log)
    digest = None
    observed = None
    try:
        # P is our unreaped child, so its /proc entry names it until we reap.
        _, pgrp, ticks = read_stat(proc.pid)
        observed = (SCHEMA_LAUNCH, proc.pid, proc.pid, ticks, os.getuid(), 1,
                    boot_id(), venv_info.st_dev, venv_info.st_ino, terminal,
                    observer_record)
    except (OSError, ValueError, IndexError):
        pass
    handed_off = False
    rc = 1
    try:
        if observed is None:
            raise Refused("cannot observe the supervisor")
        while digest is None:
            if stop_signal is not None:
                raise Refused("interrupted")
            if time.monotonic() >= deadline:
                raise Refused("no launch receipt within %ds" % ready_timeout)
            if proc.poll() is not None:
                raise Refused("supervisor exited (status %d)" % proc.returncode)
            if os.path.exists(receipt):
                data = read_receipt(receipt)
                if data.endswith(b"\n"):
                    child = cross_check(data, proc.pid, observed)
                    digest = hashlib.sha256(data).hexdigest()
                    stage.record("launch_receipt", {"path": receipt, "sha256": digest})
                    break
            time.sleep(POLL_SECONDS)

        python = os.stat(os.path.join(venv, "bin", "python3"))
        while True:
            if stop_signal is not None:
                raise Refused("interrupted")
            left = deadline - time.monotonic()
            if left <= 0:
                raise Refused("not healthy within %ds" % ready_timeout)
            if proc.poll() is not None:
                raise Refused("supervisor exited (status %d)" % proc.returncode)
            tree = descendants(proc.pid)
            if child in tree and owned_readiness(port, tree) \
                    and healthy(port, min(5.0, left)) and proc.poll() is None \
                    and owned_readiness(port, descendants(proc.pid)):
                # Readiness proves C has exec'd, so its exe is now meaningful.
                exe = os.stat("/proc/%d/exe" % child)
                observed_ns = time.monotonic_ns()
                if (exe.st_dev, exe.st_ino) != (python.st_dev, python.st_ino):
                    raise Refused("the served child does not run the venv's python")
                break
            time.sleep(POLL_SECONDS)

        if not isinstance(stage, _NoStage):
            # The readiness record (stage mode only): both generations must
            # still be the receipt's, and the venv interpreter unchanged.
            record = json.loads(data)
            sup_ticks = record["supervisor"]["start_ticks"]
            child_ticks = record["child"]["start_ticks"]
            now = os.stat(os.path.join(venv, "bin", "python3"))
            if type(child_ticks) is not int or read_stat(child)[2] != child_ticks \
                    or read_stat(proc.pid)[2] != sup_ticks \
                    or (now.st_dev, now.st_ino) != (python.st_dev, python.st_ino):
                raise Refused("a generation or the venv interpreter changed before "
                              "readiness was recorded")
            stage.record("launch_readiness", {
                "receipt_sha256": digest,
                "supervisor": {"pid": proc.pid, "start_ticks": sup_ticks},
                "child": {"pid": child, "start_ticks": child_ticks},
                "child_exe": {"dev": exe.st_dev, "ino": exe.st_ino},
                "venv_python3": {"dev": python.st_dev, "ino": python.st_ino},
                "observed_monotonic_ns": observed_ns})

        line = ("reference alerta %s up on :%d (supervisor %d, child %d, "
                "receipt %s sha256 %s)\n" % (version, port, proc.pid, child,
                                             receipt, digest))
        signal.signal(signal.SIGPIPE, signal.SIG_IGN)
        try:
            # Unbuffered, so that nothing is left for Python's exit flush.
            view = memoryview(line.encode())
            while view:
                view = view[os.write(1, view):]
        except OSError:
            raise Refused("could not report readiness")
        if close_stage is not None:
            try:
                close_stage()
            except (OSError, ValueError) as error:
                # Still our child: the failure path below stops P (LB80-R1).
                raise Refused("could not close the launch stage COMPLETE: %s" % error)
        handed_off = True
        rc = 0
    except (Refused, OSError, ValueError, IndexError, KeyError, TypeError) as error:
        log("failed: %s" % error)
        stage.fail("launch_interrupted" if stop_signal is not None else "launch_not_ready")
    finally:
        # Any failure, expected or not, stops only our own supervisor child.
        if not handed_off:
            stop_supervisor(proc, receipt, digest, observed, job_root)
            if stop_signal == signal.SIGTERM:
                rc = 143
            elif stop_signal == signal.SIGINT:
                rc = 130
    return rc

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
