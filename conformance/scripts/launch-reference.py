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
STOP_WAIT_SECONDS = 20.0
POLL_SECONDS = 0.2
MAX_RECEIPT = 65536

stop_signal = None


def on_signal(signum, frame):
    global stop_signal
    stop_signal = signum


class Refused(Exception):
    pass


def log(message):
    sys.stderr.write("launch-reference: %s\n" % message)
    sys.stderr.flush()


def parse_args(argv):
    opts = {"--job-root": None, "--venv": None, "--port": None,
            "--ready-timeout": None}
    if len(argv) != 2 * len(opts):
        raise Refused("usage: --job-root J --venv V --port N --ready-timeout S")
    for name, value in zip(argv[0::2], argv[1::2]):
        if name not in opts or opts[name] is not None:
            raise Refused("bad argument %r" % name)
        opts[name] = value
    for name in ("--job-root", "--venv"):
        if not os.path.isabs(opts[name]) or os.path.normpath(opts[name]) != opts[name]:
            raise Refused("%s must be an absolute normalized path" % name)
    for name, high in (("--port", 65535), ("--ready-timeout", 9999)):
        value = opts[name]
        if not value.isdigit() or not 1 <= int(value) <= high or value[0] == "0":
            raise Refused("%s out of range" % name)
    return (opts["--job-root"], opts["--venv"], int(opts["--port"]),
            int(opts["--ready-timeout"]))


def check_job_root(job_root):
    info = os.lstat(job_root)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() \
            or info.st_mode & 0o077:
        raise Refused("job root must be a 0700 directory owned by this user")
    if os.path.realpath(job_root) != job_root:
        raise Refused("job root must not traverse symlinks")
    return info


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
                   record["terminal_receipt"])
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
        job_root, venv, port, ready_timeout = parse_args(argv[preflight:])
        check_environment()
        check_external_config()
        check_job_root(job_root)
        if preflight:
            check_venv_path(job_root, venv)
            if os.path.lexists(venv):
                raise Refused("venv %s already exists" % venv)
            if os.path.lexists(os.path.join(job_root, "reference")):
                raise Refused("job root already holds a launch")
            if listeners(port):
                raise Refused("port %d already in use" % port)
            return 0
        venv_info = check_venv(job_root, venv)
        if listeners(port):
            raise Refused("port %d already in use" % port)
        rundir = os.path.join(job_root, "reference")
        os.mkdir(rundir, 0o700)
        receipt = os.path.join(rundir, "launch.json")
        terminal = os.path.join(rundir, "terminal.json")
        server_log = os.open(os.path.join(rundir, "server.log"),
                             os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
                             0o600)
        with open(os.path.join(HERE, "..", "..", "reference", "alerta",
                               "VERSION")) as f:
            version = f.read().strip()
    except (Refused, OSError) as error:
        log("refused: %s" % error)
        return 1

    command = [os.path.join(venv, "bin", "alertad"), "run", "--host", "127.0.0.1",
               "--port", str(port), "--no-reload", "--no-debugger"]
    deadline = time.monotonic() + ready_timeout
    proc = subprocess.Popen(
        [sys.executable, "-I", "-S", "-B", SUPERVISOR, "--receipt", receipt,
         "--terminal", terminal, "--venv", venv, "--"] + command,
        process_group=0, stdin=subprocess.DEVNULL, stdout=server_log,
        stderr=server_log)
    os.close(server_log)
    digest = None
    observed = None
    try:
        # P is our unreaped child, so its /proc entry names it until we reap.
        _, pgrp, ticks = read_stat(proc.pid)
        observed = (SCHEMA_LAUNCH, proc.pid, proc.pid, ticks, os.getuid(), 1,
                    boot_id(), venv_info.st_dev, venv_info.st_ino, terminal)
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
                if (exe.st_dev, exe.st_ino) != (python.st_dev, python.st_ino):
                    raise Refused("the served child does not run the venv's python")
                break
            time.sleep(POLL_SECONDS)

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
        handed_off = True
        rc = 0
    except (Refused, OSError, ValueError, IndexError) as error:
        log("failed: %s" % error)
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
