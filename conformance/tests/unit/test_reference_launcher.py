"""Offline controls for the supervised launcher and receipt custody (SS-I1).

Each case builds a fake job root whose venv holds a stub `alertad` (a small
HTTP server) and runs the real launch-reference.py, reference-supervisor.py
and stop-reference.py. There is no reference install, database or network
beyond 127.0.0.1. Every case checks that the launcher's process tree is gone
afterwards and that unrelated processes stay alive.
"""
import functools
import hashlib
import json
import os
from pathlib import Path
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "conformance/scripts"
PY = [sys.executable, "-I", "-S", "-B"]
ENV = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
READY = re.compile(r"^reference alerta \S+ up on :(\d+) \(supervisor (\d+), "
                   r"child (\d+), receipt (\S+) sha256 ([0-9a-f]{64})\)$")

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"),
                                reason="Linux /proc, prctl and pidfd")

# Synthetic server secrets (P2c): written by make_job as the job's private
# URL and key files; the stub checks it received exactly these.
TEST_DB_PORT = 5432
TEST_ROLE = "alrust_scaffold_1_1"
TEST_URL = "postgresql://%s:%s@127.0.0.1:%d/%s" % (TEST_ROLE, "5e" * 32, TEST_DB_PORT, TEST_ROLE)
TEST_KEY = "synthetic-launcher-test-key-" + "q" * 20

SERVER = '''
import http.server, os, sys
port = int(sys.argv[sys.argv.index("--port") + 1])
# P2c: the served child reads the secrets only after the launch receipt
# exists, and execs this server with exactly the job's values.
_job = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(sys.argv[0]))))
if not os.path.exists(os.path.join(_job, "reference", "launch.json")):
    sys.exit(6)
if (os.environ.get("DATABASE_URL"), os.environ.get("SECRET_KEY")) != (
        "postgresql://alrust_scaffold_1_1:5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e@127.0.0.1:5432/alrust_scaffold_1_1",
        "synthetic-launcher-test-key-qqqqqqqqqqqqqqqqqqqq"):
    sys.exit(5)
assert "--no-reload" in sys.argv and "--no-debugger" in sys.argv
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        ok = self.path == "/management/healthcheck" and MODE != "unhealthy"
        self.send_response(200 if ok else 503)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, *a):
        pass
MODE = %r
if MODE == "exit":
    sys.exit(3)
if MODE == "v6":
    import socket
    class V6(http.server.HTTPServer):
        address_family = socket.AF_INET6
    V6(("::1", port), H).serve_forever()
if MODE == "noserve":
    import time
    time.sleep(300)
if MODE == "slow":
    import time
    time.sleep(3)
if MODE == "grandchild" and os.fork() != 0:
    os.wait()
    sys.exit(0)
http.server.HTTPServer(("127.0.0.1", port), H).serve_forever()
'''


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def alive(pid):
    try:
        with open("/proc/%d/stat" % pid, "rb") as f:
            return f.read().rsplit(b")", 1)[1].split()[0] != b"Z"
    except FileNotFoundError:
        return False


def make_job(tmp_path, mode="healthy", name="job", interpreter=None):
    job = tmp_path / name
    job.mkdir(mode=0o700)
    bin_dir = job / "venv" / "bin"
    bin_dir.mkdir(parents=True)
    real = os.path.realpath(sys.executable)
    (bin_dir / "python3").symlink_to(real)
    # A minimal real venv layout, so .pth files in site-packages are
    # processed by the venv interpreter as in a built venv.
    (job / "venv" / "pyvenv.cfg").write_text(
        "home = %s\ninclude-system-site-packages = false\nversion = %d.%d.%d\n"
        % ((os.path.dirname(real),) + tuple(sys.version_info[:3])))
    (job / "venv" / "lib" / ("python%d.%d" % sys.version_info[:2])
     / "site-packages").mkdir(parents=True)
    database = job / "database"
    database.mkdir(mode=0o700)
    for path, value in ((database / "url", TEST_URL), (job / "secret-key", TEST_KEY)):
        path.write_text(value + "\n")
        path.chmod(0o600)
    shebang = interpreter or str(bin_dir / "python3")
    alertad = bin_dir / "alertad"
    alertad.write_text("#!%s\n%s" % (shebang, SERVER % mode))
    alertad.chmod(0o755)
    return job


def secret_args(job):
    """Paths and the database port only (P2c)."""
    return ["--secret-key-file", str(job / "secret-key"), "--database-port", str(TEST_DB_PORT)]


def launch(job, port, timeout=15, scripts=None, env=None, **kw):
    # Byte copies of the scripts in a small repository layout, so that the
    # reference VERSION file exists without the submodule.
    scripts = scripts or copy_scripts(job.parent)
    args = PY + [str(scripts / "launch-reference.py"), "--job-root", str(job),
                 "--venv", str(job / "venv"), "--port", str(port),
                 "--ready-timeout", str(timeout)] + secret_args(job)
    env = dict(env or ENV)
    env.setdefault("HOME", str(job / "home"))
    return subprocess.Popen(args, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, **kw)


def finish(proc, seconds=60):
    out, err = proc.communicate(timeout=seconds)
    return proc.returncode, out, err


def stop(receipt, digest, *job_root):
    out = subprocess.run(PY + [str(SCRIPTS / "stop-reference.py"), str(receipt),
                               digest] + [str(j) for j in job_root],
                         capture_output=True, text=True, timeout=60)
    return out.returncode, json.loads(out.stdout)


def tree_gone(job):
    """True when the job's supervisor and child have both exited."""
    path = job / "reference" / "launch.json"
    if not path.exists():
        return True
    record = json.loads(path.read_text())
    return not alive(record["supervisor"]["pid"]) \
        and not alive(record["child"]["pid"])


@pytest.fixture(autouse=True)
def stop_leftover_launches(tmp_path):
    """After each test, stop any launch still running under tmp_path.

    A defective launcher can hand off by mistake; its supervisor is then
    stopped through stop-reference.py itself (pidfd, generation-checked), so
    a failing control never leaves processes behind."""
    yield
    for receipt in tmp_path.glob("**/reference/launch.json"):
        job = receipt.parent.parent
        try:
            digest = hashlib.sha256(receipt.read_bytes()).hexdigest()
        except OSError:
            continue
        subprocess.run(PY + [str(SCRIPTS / "stop-reference.py"), str(receipt),
                             digest, str(job)],
                       capture_output=True, text=True, timeout=60)


def run_all(steps):
    """Attempt every cleanup step even if earlier ones fail, then raise the
    failure (an exception group if there were several), so one failed step
    never skips the cleanup of other known children (LTC-SOURCE-R1)."""
    errors = []
    for step in steps:
        try:
            step()
        except BaseException as error:
            errors.append(error)
    if len(errors) == 1:
        raise errors[0]
    if errors:
        raise BaseExceptionGroup("%d cleanup steps failed" % len(errors), errors)


def reap_steps(procs):
    """kill() then wait() for each Popen, as independent steps. Each is the
    test's own unreaped child, so neither can reach a reused PID."""
    return [step for proc in procs
            for step in (proc.kill, functools.partial(proc.wait, timeout=10))]


@pytest.fixture
def cleanup():
    """Popen objects this test started (LAUNCHER-TEST-CLEANUP-N1)."""
    procs = []
    try:
        yield procs
    finally:
        run_all(reap_steps(procs))


def read_generation(pid):
    """(ppid, start_ticks) of PID, or None if it is gone."""
    try:
        with open("/proc/%d/stat" % pid, "rb") as f:
            rest = f.read().rsplit(b")", 1)[1].split()
    except (FileNotFoundError, ProcessLookupError):
        return None
    return int(rest[1]), int(rest[19])


def hold_generation(pid, ppid, start_ticks):
    """A pidfd for exactly the generation (PID, ppid, start_ticks), or a
    test failure that signals nothing. The generation existed before the
    open, so a match read after it proves the descriptor names it."""
    try:
        fd = os.pidfd_open(pid)
    except ProcessLookupError:
        pytest.fail("PID %d gone before it was held: HOLD, nothing signalled" % pid)
    try:
        current = read_generation(pid)
    except BaseException:
        os.close(fd)
        raise
    if current != (ppid, start_ticks):
        os.close(fd)
        pytest.fail("PID %d is not the expected generation: HOLD, nothing signalled" % pid)
    return fd


def kill_held(fd, seconds=10):
    """SIGKILL through the held pidfd only, prove exit by polling it, and
    close it."""
    try:
        try:
            signal.pidfd_send_signal(fd, signal.SIGKILL)
        except ProcessLookupError:
            pass  # that exact generation has already exited
        poller = select.poll()
        poller.register(fd, select.POLLIN)
        if not poller.poll(int(seconds * 1000)):
            pytest.fail("held process did not exit")
    finally:
        os.close(fd)


def started(tmp_path, mode="healthy", **kw):
    job, port = make_job(tmp_path, mode, **kw), free_port()
    rc, out, err = finish(launch(job, port))
    assert rc == 0, err
    m = READY.match(out.strip())
    assert m, out
    return job, port, m


def test_ready_line_binds_the_receipt_and_stop_completes(tmp_path):
    job, port, m = started(tmp_path)
    sup, child, receipt, digest = int(m[2]), int(m[3]), Path(m[4]), m[5]
    assert receipt == job / "reference" / "launch.json"
    assert hashlib.sha256(receipt.read_bytes()).hexdigest() == digest
    record = json.loads(receipt.read_text())
    assert (record["supervisor"]["pid"], record["child"]["pid"]) == (sup, child)
    assert alive(sup) and alive(child)
    rc, out = stop(receipt, digest, job)
    assert (rc, out["verdict"]) == (0, "COMPLETE")
    assert tree_gone(job)


def test_listener_held_by_a_descendant_is_accepted(tmp_path):
    job, port, m = started(tmp_path, mode="grandchild")
    rc, out = stop(Path(m[4]), m[5], job)
    assert (rc, out["verdict"]) == (0, "COMPLETE")


def test_cross_job_receipt_is_refused(tmp_path):
    job, port, m = started(tmp_path)
    other = tmp_path / "other"
    other.mkdir(mode=0o700)
    rc, out = stop(Path(m[4]), m[5], other)
    assert (rc, out["reason"]) == (2, "outside_job_root")
    assert alive(int(m[2]))
    assert stop(Path(m[4]), m[5], job)[1]["verdict"] == "COMPLETE"


def test_forged_receipt_cannot_use_the_launcher_digest(tmp_path, cleanup):
    job, port, m = started(tmp_path)
    victim = subprocess.Popen(["/bin/sleep", "300"], process_group=0)
    cleanup.append(victim)
    with open("/proc/%d/stat" % victim.pid, "rb") as f:
        ticks = int(f.read().rsplit(b")", 1)[1].split()[19])
    forged = json.loads(Path(m[4]).read_text())
    forged["supervisor"].update(pid=victim.pid, pgid=victim.pid,
                                start_ticks=ticks)
    path = job / "forged.json"
    data = json.dumps(forged).encode()
    path.write_bytes(data)
    rc, out = stop(path, m[5], job)
    assert (rc, out["reason"]) == (2, "receipt_hash_mismatch")
    assert alive(victim.pid)
    # A forged receipt from another job root is refused before parsing.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    (elsewhere / "forged.json").write_bytes(data)
    rc, out = stop(elsewhere / "forged.json",
                   hashlib.sha256(data).hexdigest(), job)
    assert (rc, out["reason"]) == (2, "outside_job_root")
    assert alive(victim.pid)
    assert stop(Path(m[4]), m[5], job)[1]["verdict"] == "COMPLETE"


@pytest.mark.parametrize("job_root", ["relative", "/tmp/../tmp"])
def test_bad_job_root_argument_for_stop(job_root):
    out = subprocess.run(PY + [str(SCRIPTS / "stop-reference.py"), "/x.json",
                               "0" * 64, job_root],
                         capture_output=True, text=True, timeout=30)
    assert json.loads(out.stdout)["reason"] == "job_root_invalid"


def test_unhealthy_server_is_stopped_and_cleanup_complete(tmp_path):
    job, port = make_job(tmp_path, "unhealthy"), free_port()
    rc, out, err = finish(launch(job, port, timeout=3))
    assert rc == 1 and out == ""
    assert "not healthy" in err and '"verdict": "COMPLETE"' in err
    assert tree_gone(job)


def test_child_exit_during_startup(tmp_path):
    job, port = make_job(tmp_path, "exit"), free_port()
    rc, out, err = finish(launch(job, port))
    assert rc == 1 and out == ""
    assert "supervisor exited" in err or "not healthy" in err
    assert '"verdict": "COMPLETE"' in err
    assert tree_gone(job)


def test_wrong_interpreter_is_refused(tmp_path):
    other = "/usr/bin/python3"
    if os.path.realpath(other) == os.path.realpath(sys.executable):
        pytest.skip("needs a second interpreter")
    job, port = make_job(tmp_path, interpreter=other), free_port()
    rc, out, err = finish(launch(job, port))
    assert rc == 1 and "does not run the venv's python" in err
    assert '"verdict": "COMPLETE"' in err
    assert tree_gone(job)


def test_term_during_readiness_stops_the_tree(tmp_path):
    job, port = make_job(tmp_path, "unhealthy"), free_port()
    proc = launch(job, port, timeout=30)
    receipt = job / "reference" / "launch.json"
    deadline = time.monotonic() + 15
    while not receipt.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    time.sleep(0.5)
    proc.send_signal(signal.SIGTERM)
    rc, out, err = finish(proc)
    assert rc == 143 and out == ""
    assert '"verdict": "COMPLETE"' in err
    assert tree_gone(job)


def test_closed_output_is_a_failure_with_cleanup(tmp_path):
    job, port = make_job(tmp_path), free_port()
    read_end, write_end = os.pipe()
    os.close(read_end)
    proc = subprocess.Popen(
        PY + [str(copy_scripts(tmp_path) / "launch-reference.py"),
              "--job-root", str(job),
              "--venv", str(job / "venv"), "--port", str(port),
              "--ready-timeout", "15"] + secret_args(job),
        env=dict(ENV, HOME=str(job / "home")), stdout=write_end,
        stderr=subprocess.PIPE, text=True)
    os.close(write_end)
    _, err = proc.communicate(timeout=60)
    assert proc.returncode == 1
    assert "could not report readiness" in err
    assert tree_gone(job)


def test_second_launch_in_same_job_root_is_refused(tmp_path):
    job, port, m = started(tmp_path)
    rc, out, err = finish(launch(job, free_port()))
    assert rc == 1 and "refused" in err
    assert stop(Path(m[4]), m[5], job)[1]["verdict"] == "COMPLETE"


def test_environment_outside_allowlist_is_refused(tmp_path):
    job, port = make_job(tmp_path), free_port()
    rc, out, err = finish(launch(job, port, env=dict(ENV, FLASK_DEBUG="1")))
    assert rc == 1 and "FLASK_DEBUG" in err
    assert not (job / "reference").exists()


@pytest.mark.parametrize("variant", ["mode", "symlink", "venv_outside"])
def test_job_root_and_venv_checks(tmp_path, variant):
    job, port = make_job(tmp_path), free_port()
    venv = job / "venv"
    if variant == "mode":
        job.chmod(0o755)
    elif variant == "symlink":
        link = tmp_path / "link"
        link.symlink_to(job)
        job = link
    else:
        outside = tmp_path / "outside-venv"
        shutil.copytree(venv, outside, symlinks=True)
        venv = outside
    args = PY + [str(copy_scripts(tmp_path) / "launch-reference.py"),
                 "--job-root", str(job), "--venv", str(venv),
                 "--port", str(port), "--ready-timeout", "5"] + secret_args(job)
    out = subprocess.run(args, env=dict(ENV, HOME=str(job / "home")),
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 1 and "refused" in out.stderr


def test_busy_port_is_refused(tmp_path):
    job = make_job(tmp_path)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        rc, out, err = finish(launch(job, s.getsockname()[1]))
    assert rc == 1 and "already in use" in err
    assert not (job / "reference").exists()


@pytest.mark.parametrize("args", [
    [], ["--job-root", "/j", "--venv", "/j/v", "--port", "0",
         "--ready-timeout", "5"],
    ["--job-root", "/j", "--venv", "/j/v", "--port", "80",
         "--ready-timeout", "05"],
    ["--job-root", "j", "--venv", "/j/v", "--port", "80",
     "--ready-timeout", "5"],
])
def test_launcher_usage(args):
    out = subprocess.run(PY + [str(SCRIPTS / "launch-reference.py")] + args,
                         env=ENV, capture_output=True, text=True, timeout=30)
    assert out.returncode == 1 and "refused" in out.stderr


def test_copies_are_byte_identical(tmp_path):
    scripts = copy_scripts(tmp_path)
    for name in ("launch-reference.py", "stop-reference.py",
                 "reference-supervisor.py"):
        assert (scripts / name).read_bytes() == (SCRIPTS / name).read_bytes()


def copy_scripts(tmp_path, supervisor_source=None, external_config=None,
                 journal_patch=None):
    """A private copy of the scripts, optionally with one component replaced
    (journal_patch: (old, new) applied to the copied stage_journal.py)."""
    repo = tmp_path / "repo"
    scripts = repo / "conformance" / "scripts"
    if supervisor_source is None and external_config is None \
            and journal_patch is None and scripts.exists():
        return scripts
    scripts.mkdir(parents=True)
    (repo / "reference" / "alerta").mkdir(parents=True)
    (repo / "reference" / "alerta" / "VERSION").write_text("9.1.0\n")
    for name in ("launch-reference.py", "stop-reference.py",
                 "reference-supervisor.py", "confirm-launch.py"):
        shutil.copy(SCRIPTS / name, scripts / name)
    # The stage-journal package that stage mode imports (stdlib only).
    package = repo / "conformance" / "alrust_conformance"
    package.mkdir()
    for name in ("__init__.py", "stage_journal.py", "security_evidence.py"):
        shutil.copy(SCRIPTS.parent / "alrust_conformance" / name, package / name)
    if journal_patch is not None:
        text = (package / "stage_journal.py").read_text()
        assert text.count(journal_patch[0]) == 1
        (package / "stage_journal.py").write_text(text.replace(*journal_patch))
    if supervisor_source is not None:
        (scripts / "reference-supervisor.py").write_text(supervisor_source)
    if external_config is not None:
        text = (scripts / "launch-reference.py").read_text()
        text = text.replace('EXTERNAL_CONFIG = "/etc/alertad.conf"',
                            'EXTERNAL_CONFIG = %r' % str(external_config))
        (scripts / "launch-reference.py").write_text(text)
    return scripts


LYING_SUPERVISOR = '''
import json, os, sys, time
args = sys.argv[1:]
receipt = args[args.index("--receipt") + 1]
with open("/proc/self/stat", "rb") as f:
    ticks = int(f.read().rsplit(b")", 1)[1].split()[19])
with open("/proc/sys/kernel/random/boot_id") as f:
    boot = f.read().strip()
venv = os.stat(args[args.index("--venv") + 1])
record = {"schema": "alrust-reference-launch/1",
          "supervisor": {"pid": os.getpid(), "pgid": os.getpgid(0),
                         "start_ticks": ticks + %d, "uid": os.getuid(),
                         "subreaper": 1},
          "child": {"pid": os.getpid(), "start_ticks": ticks},
          "boot_id": boot,
          "venv": {"path": "", "dev": venv.st_dev, "ino": venv.st_ino},
          "terminal_receipt": args[args.index("--terminal") + 1]}
with open(receipt, "x") as f:
    f.write(json.dumps(record) + "\\n")
time.sleep(300)
'''


def test_receipt_that_disagrees_with_the_launcher_is_refused(tmp_path):
    # P lies about its own start_ticks. The launcher must refuse it and stop
    # only P, its own child; no digest is ever printed.
    scripts = copy_scripts(tmp_path, LYING_SUPERVISOR % 1)
    job, port = make_job(tmp_path), free_port()
    proc = launch(job, port, scripts=scripts)
    rc, out, err = finish(proc)
    assert rc == 1 and out == ""
    assert "disagrees" in err
    assert "UNPROVEN" in err  # no validated receipt, P killed by TERM
    sup = json.loads((job / "reference" / "launch.json").read_text())
    assert not alive(sup["supervisor"]["pid"])


def test_receipt_child_must_be_the_supervisors_child(tmp_path):
    # A truthful start_ticks, but the receipt names P itself as its child.
    scripts = copy_scripts(tmp_path, LYING_SUPERVISOR % 0)
    job, port = make_job(tmp_path), free_port()
    rc, out, err = finish(launch(job, port, scripts=scripts))
    assert rc == 1 and out == ""
    assert "disagrees" in err or "not the supervisor's child" in err


def test_external_config_present_is_refused(tmp_path):
    conf = tmp_path / "alertad.conf"
    conf.write_text("DEBUG = True\n")
    scripts = copy_scripts(tmp_path, external_config=conf)
    job, port = make_job(tmp_path), free_port()
    rc, out, err = finish(launch(job, port, scripts=scripts))
    assert rc == 1 and "hidden configuration" in err
    assert not (job / "reference").exists()


def test_foreign_listener_is_not_readiness(tmp_path):
    # The stub never listens. After the launch has passed its port check, an
    # unrelated server (this test's own thread) answers on the port. Its
    # healthy answers must not count: the listener is not in P's tree.
    import http.server
    import threading

    class Healthy(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")

        def log_message(self, *a):
            pass

    job, port = make_job(tmp_path, "noserve"), free_port()
    proc = launch(job, port, timeout=4)
    receipt = job / "reference" / "launch.json"
    deadline = time.monotonic() + 10
    while not receipt.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    server = http.server.HTTPServer(("127.0.0.1", port), Healthy)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        rc, out, err = finish(proc)
    finally:
        server.shutdown()
        server.server_close()
    assert rc == 1 and out == ""
    assert "not healthy" in err and '"verdict": "COMPLETE"' in err
    assert tree_gone(job)


def test_foreign_listener_elsewhere_on_the_port_blocks_readiness(tmp_path):
    # Our server holds [::1]:PORT; an unrelated server answers healthy on
    # 127.0.0.1:PORT, where the health check connects. Holding *a* listener
    # on the port is not enough: every listener must be ours, and one must
    # be on 127.0.0.1.
    import http.server
    import threading

    class Healthy(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")

        def log_message(self, *a):
            pass

    if not socket.has_ipv6:
        pytest.skip("needs IPv6 loopback")
    job, port = make_job(tmp_path, "v6"), free_port()
    proc = launch(job, port, timeout=4)
    receipt = job / "reference" / "launch.json"
    deadline = time.monotonic() + 10
    while not receipt.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    server = http.server.HTTPServer(("127.0.0.1", port), Healthy)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        rc, out, err = finish(proc)
    finally:
        server.shutdown()
        server.server_close()
    assert rc == 1 and out == ""
    assert "not healthy" in err and '"verdict": "COMPLETE"' in err
    assert tree_gone(job)


def fake_checkout(tmp_path):
    """run-reference.sh in a layout where every check before the build can
    run offline: a stub reference tree and a stub pg_config."""
    repo = tmp_path / "shrepo"
    (repo / "conformance" / "scripts").mkdir(parents=True)
    (repo / "reference" / "alerta").mkdir(parents=True)
    (repo / "reference" / "alerta" / "setup.py").write_text("")
    for name in ("run-reference.sh", "launch-reference.py"):
        shutil.copy(SCRIPTS / name, repo / "conformance" / "scripts" / name)
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "pg_config").write_text("#!/bin/sh\nexit 0\n")
    (tools / "pg_config").chmod(0o755)
    # A build sentinel: the interpreter shim passes everything to the real
    # Python except `-m venv`, which records the sentinel and exits 97. A
    # control that wrongly reaches the build therefore never installs.
    shim = tools / "reference-python"
    shim.write_text(
        "#!%s\nimport os, sys\n"
        "if sys.argv[1:3] == ['-m', 'venv']:\n"
        "    open(%r, 'x').write('BUILD_REACHED\\n')\n"
        "    sys.exit(97)\n"
        "os.execv(%r, [%r] + sys.argv[1:])\n"
        % (sys.executable, str(tmp_path / "build-reached"), sys.executable,
           sys.executable))
    shim.chmod(0o755)
    return repo, tools


@pytest.mark.parametrize("variant", [
    "valid", "mode", "group_writable", "symlink", "not_normalized",
    "trailing_slash", "venv_outside", "venv_dotdot", "venv_symlinked_parent",
    "home_outside", "home_dotdot", "home_final_symlink",
    "home_symlinked_parent", "home_unset", "env_extra", "busy_port",
    "earlier_launch"])
def test_run_reference_refuses_before_the_build(tmp_path, variant):
    repo, tools = fake_checkout(tmp_path)
    job = tmp_path / "job"
    job.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    env = dict(ENV, PATH="%s:/usr/bin:/bin" % tools, JOB_ROOT=str(job),
               HOME=str(job / "home"),
               REFERENCE_PYTHON=str(tools / "reference-python"),
               PORT=str(free_port()))
    holder = None
    if variant == "mode":
        job.chmod(0o750)
    elif variant == "group_writable":
        job.chmod(0o770)
    elif variant == "symlink":
        link = tmp_path / "link"
        link.symlink_to(job)
        env.update(JOB_ROOT=str(link), HOME=str(link / "home"))
    elif variant == "not_normalized":
        env.update(JOB_ROOT=str(tmp_path / "x" / ".." / "job"),
                   HOME=str(tmp_path / "x" / ".." / "job" / "home"))
        (tmp_path / "x").mkdir()
    elif variant == "trailing_slash":
        env.update(JOB_ROOT=str(job) + "/")
    elif variant == "venv_outside":
        env["VENV"] = str(tmp_path / "elsewhere-venv")
    elif variant == "venv_dotdot":
        env["VENV"] = str(job / ".." / "escaped-venv")
    elif variant == "venv_symlinked_parent":
        (tmp_path / "real").mkdir()
        (job / "sub").symlink_to(tmp_path / "real")
        env["VENV"] = str(job / "sub" / "venv")
    elif variant == "home_outside":
        env["HOME"] = str(tmp_path)
    elif variant == "home_dotdot":
        env["HOME"] = str(job) + "/../outside"
    elif variant == "home_final_symlink":
        (job / "home").symlink_to(outside)
    elif variant == "home_symlinked_parent":
        (job / "sub").symlink_to(outside)
        env["HOME"] = str(job / "sub" / "home")
    elif variant == "home_unset":
        del env["HOME"]
    elif variant == "valid":
        pass
    elif variant == "env_extra":
        env["FLASK_DEBUG"] = "1"
    elif variant == "busy_port":
        holder = socket.socket()
        holder.bind(("127.0.0.1", 0))
        holder.listen()
        env["PORT"] = str(holder.getsockname()[1])
    else:
        (job / "reference").mkdir(mode=0o700)
    before = sorted(p.name for p in job.iterdir())
    try:
        out = subprocess.run(
            ["bash", str(repo / "conformance/scripts/run-reference.sh")],
            env=env, capture_output=True, text=True, timeout=60)
    finally:
        if holder is not None:
            holder.close()
    if variant == "valid":
        # The positive control: every check passes and the build is reached
        # (the sentinel stops it there).
        assert out.returncode == 97, out.stderr
        assert (tmp_path / "build-reached").exists()
        return
    assert out.returncode == 1, out.stderr
    assert not (tmp_path / "build-reached").exists()
    assert "environment" not in out.stdout  # no venv was built
    assert sorted(p.name for p in job.iterdir()) == before
    assert not (tmp_path / "elsewhere-venv").exists()
    assert not (tmp_path / "escaped-venv").exists()
    assert not (tmp_path / "real" / "venv").exists()


def test_preflight_alone_accepts_a_clean_job_root(tmp_path):
    job = tmp_path / "job"
    job.mkdir(mode=0o700)
    scripts = copy_scripts(tmp_path)
    out = subprocess.run(
        PY + [str(scripts / "launch-reference.py"), "--preflight",
              "--job-root", str(job), "--venv", str(job / "venv"),
              "--port", str(free_port()), "--ready-timeout", "5"] + secret_args(job),
        env=dict(ENV, HOME=str(job / "home")), capture_output=True,
        text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert list(job.iterdir()) == []


@pytest.mark.parametrize("variant", ["dotdot", "final_symlink", "unset"])
def test_launch_rechecks_home(tmp_path, variant):
    # The launch step itself, not only the preflight, refuses a HOME that
    # leaves the job root, before starting anything.
    job, port = make_job(tmp_path), free_port()
    outside = tmp_path / "outside"
    outside.mkdir()
    env = dict(ENV)
    if variant == "dotdot":
        env["HOME"] = str(job) + "/../outside"
    elif variant == "final_symlink":
        (job / "home").symlink_to(outside)
        env["HOME"] = str(job / "home")
    proc = subprocess.Popen(
        PY + [str(copy_scripts(tmp_path) / "launch-reference.py"),
              "--job-root", str(job), "--venv", str(job / "venv"),
              "--port", str(port), "--ready-timeout", "5"] + secret_args(job),
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    rc, out, err = finish(proc)
    assert rc == 1 and "HOME" in err
    assert not (job / "reference").exists()


# Observer installation and activation (contract section 6). The stub module
# records every import and every activation next to its socket path.
# A stub with the reviewed helper's shape: import only records itself;
# activate_from_environment() activates only when the actual parent matches.
OBSERVER_STUB = b"""import os
_dir = os.path.dirname(os.environ["ALRUST_OBSERVER_SOCKET"])
open(os.path.join(_dir, "imported-%d" % os.getpid()), "x").close()


def activate_from_environment():
    if os.environ.get("ALRUST_OBSERVER_PARENT") != str(os.getppid()):
        return None
    open(os.path.join(_dir, "activated-%d" % os.getpid()), "x").close()
    return True
"""

# A stub whose activation fails: site.py prints the error and the server
# keeps running without an observer (AN-1).
FAILING_STUB = b"""import os
_dir = os.path.dirname(os.environ["ALRUST_OBSERVER_SOCKET"])


def activate_from_environment():
    if os.environ.get("ALRUST_OBSERVER_PARENT") != str(os.getppid()):
        return None
    open(os.path.join(_dir, "failed-%d" % os.getpid()), "x").close()
    raise ValueError("activation_start_failed")
"""


CODEC_STUB = b"VALUE = 'codec'\n"


def observer_args(tmp_path, data=OBSERVER_STUB, pin=None, codec=True):
    module = tmp_path / "observer_module.py"
    module.write_bytes(data)
    args = ["--observer-file", "alrust_observer.py",
            pin or hashlib.sha256(data).hexdigest(), str(module)]
    if codec:
        codec_path = tmp_path / "observer_codec.py"
        codec_path.write_bytes(CODEC_STUB)
        args += ["--observer-file", "alrust_observer_protocol.py",
                 hashlib.sha256(CODEC_STUB).hexdigest(), str(codec_path)]
    return args


def launch_with(job, port, extra, timeout=15, scripts=None, env=None):
    scripts = scripts or copy_scripts(job.parent)
    args = PY + [str(scripts / "launch-reference.py"), "--job-root", str(job),
                 "--venv", str(job / "venv"), "--port", str(port),
                 "--ready-timeout", str(timeout)] + secret_args(job) + extra
    env = dict(env or ENV, HOME=str(job / "home"))
    return subprocess.Popen(args, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)


def site_dir(job):
    return job / "venv" / "lib" / ("python%d.%d" % sys.version_info[:2]) \
        / "site-packages"


def test_observer_is_pinned_installed_and_active_only_in_the_child(tmp_path):
    job, port = make_job(tmp_path), free_port()
    rc, out, err = finish(launch_with(job, port, observer_args(tmp_path)))
    assert rc == 0, err
    m = READY.match(out.strip())
    record = json.loads(Path(m[4]).read_text())
    site = site_dir(job)
    module = (site / "alrust_observer.py").read_bytes()
    codec = (site / "alrust_observer_protocol.py").read_bytes()
    activator = (site / "alrust_observer.pth").read_bytes()
    assert module == OBSERVER_STUB and codec == CODEC_STUB
    assert activator == (b'import os; os.environ.get("ALRUST_OBSERVER_SOCKET") '
                         b'and __import__("alrust_observer").activate_from_environment()\n')
    manifest_path = job / "reference" / "observer-install.json"
    manifest_bytes = manifest_path.read_bytes()
    assert record["observer"] == {
        "socket": str(job / "observer" / "observer.sock"),
        "manifest": str(manifest_path),
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest()}
    manifest = json.loads(manifest_bytes)
    assert manifest["schema"] == "alrust-observer-install/1"
    assert manifest["site_packages"] == str(site)
    assert manifest["files"] == [
        {"name": "alrust_observer.py", "path": str(site / "alrust_observer.py"),
         "size": len(module), "sha256": hashlib.sha256(module).hexdigest()},
        {"name": "alrust_observer_protocol.py",
         "path": str(site / "alrust_observer_protocol.py"),
         "size": len(codec), "sha256": hashlib.sha256(codec).hexdigest()}]
    assert manifest["activator"] == {
        "name": "alrust_observer.pth", "path": str(site / "alrust_observer.pth"),
        "sha256": hashlib.sha256(activator).hexdigest()}
    assert oct((job / "observer").stat().st_mode & 0o777) == "0o700"
    sup, child = int(m[2]), int(m[3])
    marks = sorted(p.name for p in (job / "observer").iterdir())
    # Imported and activated in C only; P runs -I -S and processes no .pth.
    assert marks == ["activated-%d" % child, "imported-%d" % child]
    venv_python = str(job / "venv" / "bin" / "python3")
    # Another interpreter of the venv with the variable set: imported (the
    # activator is unconditional on the parent) but NOT activated.
    other = subprocess.run(
        [venv_python, "-c", "import os; print(os.getpid())"],
        env=dict(ENV, ALRUST_OBSERVER_SOCKET=record["observer"]["socket"],
                 ALRUST_OBSERVER_PARENT=str(sup)),
        capture_output=True, text=True, timeout=30)
    assert other.returncode == 0, other.stderr
    pid = int(other.stdout)
    assert (job / "observer" / ("imported-%d" % pid)).exists()
    assert not (job / "observer" / ("activated-%d" % pid)).exists()
    # Without the variable (pip, provisioning, CLI): not even imported.
    plain = subprocess.run([venv_python, "-c", "import os; print(os.getpid())"],
                           env=ENV, capture_output=True, text=True, timeout=30)
    assert not (job / "observer" / ("imported-%d" % int(plain.stdout))).exists()
    assert stop(Path(m[4]), m[5], job)[1]["verdict"] == "COMPLETE"


def test_without_observer_nothing_is_installed(tmp_path):
    job, port, m = started(tmp_path)
    record = json.loads(Path(m[4]).read_text())
    assert record["observer"] is None
    assert not (site_dir(job) / "alrust_observer.pth").exists()
    assert not (job / "observer").exists()
    assert stop(Path(m[4]), m[5], job)[1]["verdict"] == "COMPLETE"


@pytest.mark.parametrize("variant", [
    "wrong_pin", "wrong_codec_pin", "symlinked_module", "truncated_option",
    "bad_pin_format", "oversized", "unknown_name", "duplicate_name",
    "codec_without_module", "module_without_codec", "relative_path",
    "existing_destination"])
def test_observer_module_refusals(tmp_path, variant):
    job, port = make_job(tmp_path), free_port()
    extra = observer_args(tmp_path)
    if variant == "wrong_pin":
        extra[2] = "0" * 64
    elif variant == "wrong_codec_pin":
        extra[6] = "0" * 64
    elif variant == "symlinked_module":
        link = tmp_path / "linked.py"
        link.symlink_to(tmp_path / "observer_module.py")
        extra[3] = str(link)
    elif variant == "truncated_option":
        extra = extra[:3]
    elif variant == "bad_pin_format":
        extra[2] = extra[2].upper()
    elif variant == "oversized":
        data = b"#" * ((1 << 20) + 1)
        extra = observer_args(tmp_path, data)
    elif variant == "unknown_name":
        extra[5] = "sitecustomize.py"
    elif variant == "duplicate_name":
        extra[5] = "alrust_observer.py"
    elif variant == "codec_without_module":
        extra = extra[4:]
    elif variant == "module_without_codec":
        extra = extra[:4]
    elif variant == "existing_destination":
        # A stale codec already in site-packages: refused before any write
        # (the preflight cannot see it; the launch refuses it).
        (site_dir(job) / "alrust_observer_protocol.py").write_text("stale")
    else:
        extra[3] = "observer_module.py"
    # The preflight runs against a fresh job root whose venv does not exist
    # yet (as before a real build), so it is the observer fault that it
    # refuses, not the existing venv.
    fresh = tmp_path / "fresh-job"
    fresh.mkdir(mode=0o700)
    pre = subprocess.run(
        PY + [str(copy_scripts(tmp_path) / "launch-reference.py"), "--preflight",
              "--job-root", str(fresh), "--venv", str(fresh / "venv"),
              "--port", str(port), "--ready-timeout", "5"] + secret_args(fresh) + extra,
        env=dict(ENV, HOME=str(fresh / "home")), capture_output=True,
        text=True, timeout=30)
    if variant == "existing_destination":
        # A stale file inside a venv cannot exist before the build; only
        # the launch can see it.
        assert pre.returncode == 0, pre.stderr
    else:
        assert pre.returncode == 1 and "refused" in pre.stderr, pre.stderr
        assert "already exists" not in pre.stderr
    proc = subprocess.run(
        PY + [str(copy_scripts(tmp_path) / "launch-reference.py"),
              "--job-root", str(job), "--venv", str(job / "venv"),
              "--port", str(port), "--ready-timeout", "5"] + secret_args(job) + extra,
        env=dict(ENV, HOME=str(job / "home")), capture_output=True,
        text=True, timeout=30)
    assert proc.returncode == 1 and "refused" in proc.stderr, proc.stderr
    assert not (job / "reference" / "launch.json").exists()
    assert not (site_dir(job) / "alrust_observer.py").exists()
    if variant != "existing_destination":
        assert not (site_dir(job) / "alrust_observer_protocol.py").exists()
    assert not (site_dir(job) / "alrust_observer.pth").exists()


def test_receipt_without_the_observer_record_is_refused(tmp_path):
    # A supervisor that drops the observer record from its receipt: the
    # launcher installed the observer, so its cross-check refuses.
    source = (SCRIPTS / "reference-supervisor.py").read_text()
    marker = '"observer": None if opts["--observer-socket"] is None else {'
    assert source.count(marker) == 1
    lying = source.replace(marker, '"observer": None if True else {')
    assert lying != source
    scripts = copy_scripts(tmp_path, supervisor_source=lying)
    job, port = make_job(tmp_path), free_port()
    rc, out, err = finish(launch_with(job, port, observer_args(tmp_path),
                                      scripts=scripts))
    assert rc == 1 and out == ""
    # A receipt that disagrees can never bind cleanup: the tree is stopped
    # (P was the launcher's own child), but the verdict stays UNPROVEN.
    assert "disagrees" in err and "cleanup UNPROVEN" in err
    assert tree_gone(job)


def test_installed_file_read_back_detects_a_change(tmp_path):
    # verify_observer_install re-reads every installed file and the
    # activator before the server is spawned; a changed byte is refused.
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "launch_reference", SCRIPTS / "launch-reference.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    entries = []
    for name, data in (("a.py", b"A\n"), ("b.pth", b"B\n")):
        path = tmp_path / name
        path.write_bytes(data)
        entries.append({"path": str(path),
                        "sha256": hashlib.sha256(data).hexdigest()})
    manifest = {"files": entries[:1], "activator": entries[1]}
    mod.verify_observer_install(manifest)
    (tmp_path / "b.pth").write_bytes(b"C\n")
    with pytest.raises(mod.Refused):
        mod.verify_observer_install(manifest)
    link = tmp_path / "link.py"
    link.symlink_to(tmp_path / "a.py")
    manifest["files"][0]["path"] = str(link)
    with pytest.raises(OSError):
        mod.verify_observer_install(manifest)



def test_failed_activation_leaves_the_server_without_an_observer(tmp_path):
    # AN-1: the .pth line raises in C; site.py reports it and continues, the
    # server becomes ready, and no activation marker exists. Downstream
    # qualification must therefore never treat "server ready" as "observer
    # active".
    job, port = make_job(tmp_path), free_port()
    rc, out, err = finish(launch_with(job, port,
                                      observer_args(tmp_path, FAILING_STUB)))
    assert rc == 0, err
    m = READY.match(out.strip())
    child = int(m[3])
    marks = sorted(p.name for p in (job / "observer").iterdir())
    assert marks == ["failed-%d" % child]
    log = (job / "reference" / "server.log").read_text()
    assert "Error processing line" in log and "alrust_observer.pth" in log
    assert stop(Path(m[4]), m[5], job)[1]["verdict"] == "COMPLETE"



# Stage mode (BP71-N1/N2): the launch runs inside its own stage journal.
from alrust_conformance import stage_journal as sj  # noqa: E402

STAGE_ARGS = ["--stage-index", "1", "--previous-stage", "database-create"]
CORE_ENV = {"GITHUB_REPOSITORY": "steliosx/alrust", "GITHUB_RUN_ID": "1",
            "GITHUB_RUN_ATTEMPT": "1", "GITHUB_JOB": "reference",
            "ImageOS": "ubuntu24", "ImageVersion": "20261001.1"}


def stage_job(tmp_path, mode="healthy", previous=True):
    """A job root with a core identity and, optionally, a closed previous
    stage (database-create), as the workflow will prepare it."""
    job = make_job(tmp_path, mode)
    ref = "e" * 40
    sj.write_core_identity(str(job), profile="scaffold", event_head_sha="a" * 40,
                           checkout_sha="b" * 40, reference_sha=ref, reference_pin=ref,
                           lock_sha256={"build": "c" * 64}, lock_pins={"build": "c" * 64},
                           manifest_sha256="d" * 64, manifest_pin="d" * 64, env=CORE_ENV)
    sj.prepare_journal_dir(str(job))
    if previous:
        with sj.StageJournal(str(job), 0, "database-create"):
            pass
    return job


def stage_records(job):
    data = Path(sj.journal_path(str(job), 1, "launch")).read_bytes()
    return [json.loads(line)["evidence"] for line in data.splitlines()]


def by_kind(records, kind):
    return [r["payload"] for r in records if r["kind"] == kind]


def test_stage_mode_records_launch_and_readiness(tmp_path):
    job, port = stage_job(tmp_path), free_port()
    (job / "home").mkdir(mode=0o700)
    rc, out, err = finish(launch_with(job, port, STAGE_ARGS))
    assert rc == 0, err
    m = READY.match(out.strip())
    sup, child, receipt, digest = int(m[2]), int(m[3]), Path(m[4]), m[5]
    records = stage_records(job)
    assert records[-1]["kind"] == "stage_complete"
    assert records[-1]["payload"] == {"result": "COMPLETE"}
    report = sj.aggregate(str(job), ["database-create", "launch"])
    assert report["verdict"] == "VALID", report
    owned = {r["path"]: r for r in by_kind(records, "owned_resource")}
    for path in (job / "venv", job / "home"):
        info = os.lstat(path)
        assert owned[str(path)] == {"path": str(path), "dev": info.st_dev,
                                    "ino": info.st_ino, "type": "dir"}
    assert by_kind(records, "launch_receipt") == [{"path": str(receipt), "sha256": digest}]
    [ready] = by_kind(records, "launch_readiness")
    launched = json.loads(receipt.read_text())
    python = os.stat(job / "venv" / "bin" / "python3")
    exe = os.stat("/proc/%d/exe" % child)
    assert ready == {
        "receipt_sha256": digest,
        "supervisor": {"pid": sup, "start_ticks": launched["supervisor"]["start_ticks"]},
        "child": {"pid": child, "start_ticks": launched["child"]["start_ticks"]},
        "child_exe": {"dev": exe.st_dev, "ino": exe.st_ino},
        "venv_python3": {"dev": python.st_dev, "ino": python.st_ino},
        "observed_monotonic_ns": ready["observed_monotonic_ns"]}
    assert type(ready["observed_monotonic_ns"]) is int
    # The readiness record precedes stage_complete, and the receipt record
    # precedes the readiness record.
    kinds = [r["kind"] for r in records]
    assert kinds.index("launch_receipt") < kinds.index("launch_readiness") \
        < kinds.index("stage_complete")
    assert stop(receipt, digest, job)[1]["verdict"] == "COMPLETE"


def test_stage_mode_records_the_observer_socket_directory(tmp_path):
    job, port = stage_job(tmp_path), free_port()
    rc, out, err = finish(launch_with(job, port, observer_args(tmp_path) + STAGE_ARGS))
    assert rc == 0, err
    m = READY.match(out.strip())
    owned = {r["path"] for r in by_kind(stage_records(job), "owned_resource")}
    assert str(job / "observer") in owned and str(job / "venv") in owned
    assert stop(Path(m[4]), m[5], job)[1]["verdict"] == "COMPLETE"


def test_stage_mode_unhealthy_server_closes_the_stage_failed(tmp_path):
    job, port = stage_job(tmp_path, "unhealthy"), free_port()
    rc, out, err = finish(launch_with(job, port, STAGE_ARGS, timeout=3))
    assert rc == 1 and out == ""
    records = stage_records(job)
    assert records[-1]["payload"] == {"result": "FAILED", "code": "launch_not_ready"}
    assert by_kind(records, "launch_readiness") == []
    assert sj.aggregate(str(job), ["database-create", "launch"])["verdict"] == "FAILED"
    assert tree_gone(job)


def test_stage_mode_refusal_before_spawn_closes_the_stage_failed(tmp_path):
    job = stage_job(tmp_path)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        rc, out, err = finish(launch_with(job, s.getsockname()[1], STAGE_ARGS))
    assert rc == 1 and "already in use" in err
    records = stage_records(job)
    assert records[-1]["payload"] == {"result": "FAILED", "code": "launch_refused"}
    assert not (job / "reference").exists()


def test_stage_mode_term_during_readiness_is_interrupted(tmp_path):
    job, port = stage_job(tmp_path, "unhealthy"), free_port()
    proc = launch_with(job, port, STAGE_ARGS, timeout=30)
    receipt = job / "reference" / "launch.json"
    deadline = time.monotonic() + 15
    while not receipt.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    time.sleep(0.5)
    proc.send_signal(signal.SIGTERM)
    rc, out, err = finish(proc)
    assert rc == 143 and out == ""
    assert stage_records(job)[-1]["payload"] == {"result": "FAILED",
                                                 "code": "launch_interrupted"}
    assert tree_gone(job)


def test_stage_mode_interpreter_swap_before_the_record_is_refused(tmp_path):
    # The venv's python3 is replaced after the launcher's first observation
    # but before readiness: the record is never written.
    job, port = stage_job(tmp_path, "slow"), free_port()
    proc = launch_with(job, port, STAGE_ARGS, timeout=30)
    receipt = job / "reference" / "launch.json"
    deadline = time.monotonic() + 15
    while not receipt.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    time.sleep(0.3)
    other = tmp_path / "python3-elsewhere"
    shutil.copy(os.path.realpath(sys.executable), other)
    alias = job / "venv" / "bin" / "python3"
    temporary = alias.with_name("python3.new")
    temporary.symlink_to(other)
    os.replace(temporary, alias)
    rc, out, err = finish(proc)
    assert rc == 1 and out == ""
    records = stage_records(job)
    assert by_kind(records, "launch_readiness") == []
    assert records[-1]["payload"] == {"result": "FAILED", "code": "launch_not_ready"}
    assert tree_gone(job)


@pytest.mark.parametrize("variant", ["no_core", "no_previous", "launch_exists"])
def test_stage_preflight_refuses_before_the_build(tmp_path, variant):
    job = stage_job(tmp_path, previous=variant != "no_previous") \
        if variant != "no_core" else make_job(tmp_path)
    if variant == "launch_exists":
        Path(sj.journal_path(str(job), 1, "launch")).write_text("")
    shutil.rmtree(job / "venv")
    scripts = copy_scripts(job.parent)
    args = PY + [str(scripts / "launch-reference.py"), "--preflight",
                 "--job-root", str(job), "--venv", str(job / "venv"),
                 "--port", str(free_port()), "--ready-timeout", "5"] + secret_args(job) + STAGE_ARGS
    out = subprocess.run(args, env=dict(ENV, HOME=str(job / "home")),
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 1 and "refused" in out.stderr


@pytest.mark.parametrize("extra", [
    ["--stage-index", "1"],                                   # no previous
    ["--previous-stage", "database-create"],                  # no index
    ["--stage-index", "0", "--previous-stage", "database-create"],
    ["--stage-index", "01", "--previous-stage", "database-create"],
    ["--stage-index", "32", "--previous-stage", "database-create"],
    ["--stage-index", "x", "--previous-stage", "database-create"],
])
def test_stage_option_usage(tmp_path, extra):
    job = stage_job(tmp_path)
    rc, out, err = finish(launch_with(job, free_port(), extra))
    assert rc == 1 and "refused" in err
    assert not (job / "reference").exists()
    assert not Path(sj.journal_path(str(job), 1, "launch")).exists()


def test_without_stage_options_nothing_is_journaled(tmp_path):
    job, port = stage_job(tmp_path), free_port()
    rc, out, err = finish(launch_with(job, port, []))
    assert rc == 0, err
    m = READY.match(out.strip())
    assert not Path(sj.journal_path(str(job), 1, "launch")).exists()
    assert stop(Path(m[4]), m[5], job)[1]["verdict"] == "COMPLETE"



# LB80-R1: a failure to close the stage COMPLETE after readiness must not
# leave P and C running. The fault is injected only into the copied journal
# module; the launcher, supervisor and stop helper are the real copies.
_COMPLETE_LINE = '        result = {"result": "COMPLETE"} if exc_type is None else \\\n'
_APPEND_LINE = '            self._journal.append(self._envelope("stage_complete", result))\n'
CLOSE_FAULTS = {
    # __exit__ raises before stage_complete is appended.
    "exit_raises": (_COMPLETE_LINE,
                    '        if exc_type is None:\n'
                    '            raise OSError("synthetic close fault")\n' + _COMPLETE_LINE),
    # The stage_complete append itself fails (the journal then closes).
    "append_fails": (_APPEND_LINE,
                     '            if exc_type is None:\n'
                     '                self._journal.close()\n'
                     '                raise OSError("synthetic append fault")\n' + _APPEND_LINE),
}


@pytest.mark.parametrize("fault", sorted(CLOSE_FAULTS))
def test_late_stage_close_failure_stops_the_tree(tmp_path, fault):
    job, port = stage_job(tmp_path), free_port()
    scripts = copy_scripts(tmp_path / "faulty", journal_patch=CLOSE_FAULTS[fault])
    rc, out, err = finish(launch_with(job, port, STAGE_ARGS, scripts=scripts))
    # The ready line was written, but the launch failed and the tree is gone.
    assert rc == 1
    assert READY.match(out.strip()), out
    assert "could not close the launch stage COMPLETE" in err
    assert '"verdict": "COMPLETE"' in err          # the stop helper's verdict
    assert tree_gone(job)
    records = stage_records(job)
    completes = [r["payload"] for r in records if r["kind"] == "stage_complete"]
    report = sj.aggregate(str(job), ["database-create", "launch"])
    # Never COMPLETE. If the journal is still open after the fault, the
    # failure path records FAILED; if the fault closed it, the stage stays
    # without stage_complete (INCOMPLETE).
    if fault == "exit_raises":
        assert completes == [{"result": "FAILED", "code": "launch_not_ready"}]
        assert report["verdict"] == "FAILED"
    else:
        assert completes == [] and records[-1]["kind"] == "launch_readiness"
        assert report["verdict"] == "INCOMPLETE"


def test_stage_preflight_refuses_an_invalid_previous_stage_name(tmp_path):
    # LB80-N1: a file with the odd name exists, but the name itself is invalid.
    job = stage_job(tmp_path)
    Path(sj.journal_path(str(job), 0, "Bad_Name")).write_text("")
    shutil.rmtree(job / "venv")
    scripts = copy_scripts(job.parent)
    args = PY + [str(scripts / "launch-reference.py"), "--preflight",
                 "--job-root", str(job), "--venv", str(job / "venv"),
                 "--port", str(free_port()), "--ready-timeout", "5",
                 "--stage-index", "1", "--previous-stage", "Bad_Name"] + secret_args(job)
    out = subprocess.run(args, env=dict(ENV, HOME=str(job / "home")),
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 1 and "refused" in out.stderr



# LB4E-R2: a failed fsync or close AFTER the full stage_complete COMPLETE
# bytes were written leaves a syntactically COMPLETE launch journal for a
# launch that failed. The launcher still stops the tree, and the
# launch-confirm stage refuses, so the chain never shows a hand-off.
_FSYNC_FAULT = (
    '            if exc_type is None:\n'
    '                real_fsync = os.fsync\n'
    '                def failing_fsync(fd):\n'
    '                    raise OSError("synthetic fsync fault")\n'
    '                os.fsync = failing_fsync\n'
    '                try:\n'
    '        ' + _APPEND_LINE +
    '                finally:\n'
    '                    os.fsync = real_fsync\n'
    '            else:\n'
    '    ' + _APPEND_LINE)
_CLOSE_AFTER_WRITE_FAULT = (
    _APPEND_LINE +
    '            if exc_type is None:\n'
    '                self._journal.close()\n'
    '                raise OSError("synthetic close fault after the write")\n')
LATE_DURABILITY_FAULTS = {"fsync_after_write": (_APPEND_LINE, _FSYNC_FAULT),
                          "close_after_write": (_APPEND_LINE, _CLOSE_AFTER_WRITE_FAULT)}
CONFIRM_STAGES = ["database-create", "launch", "launch-confirm"]


def confirm(job, digest, scripts=None, index="2"):
    scripts = scripts or copy_scripts(job.parent)
    out = subprocess.run(PY + [str(scripts / "confirm-launch.py"), "--job-root", str(job),
                               "--stage-index", index, "--receipt-digest", digest],
                         env=ENV, capture_output=True, text=True, timeout=60)
    return out.returncode, out.stdout, out.stderr


def confirm_records(job):
    data = Path(sj.journal_path(str(job), 2, "launch-confirm")).read_bytes()
    return [json.loads(line)["evidence"] for line in data.splitlines()]


def test_confirm_records_a_live_hand_off(tmp_path):
    job, port = stage_job(tmp_path), free_port()
    rc, out, err = finish(launch_with(job, port, STAGE_ARGS))
    assert rc == 0, err
    m = READY.match(out.strip())
    sup, child, receipt, digest = int(m[2]), int(m[3]), Path(m[4]), m[5]
    rc, out, err = confirm(job, digest)
    assert rc == 0, err
    records = confirm_records(job)
    [handoff] = by_kind(records, "launch_handoff")
    launched = json.loads(receipt.read_text())
    launch_bytes = Path(sj.journal_path(str(job), 1, "launch")).read_bytes()
    assert handoff == {
        "launch_journal_sha256": hashlib.sha256(launch_bytes).hexdigest(),
        "receipt_sha256": digest,
        "supervisor": {"pid": sup, "start_ticks": launched["supervisor"]["start_ticks"]},
        "child": {"pid": child, "start_ticks": launched["child"]["start_ticks"]},
        "observed_monotonic_ns": handoff["observed_monotonic_ns"]}
    assert sj.aggregate(str(job), CONFIRM_STAGES)["verdict"] == "VALID"
    assert stop(receipt, digest, job)[1]["verdict"] == "COMPLETE"


@pytest.mark.parametrize("fault", sorted(LATE_DURABILITY_FAULTS))
def test_complete_bytes_after_a_failed_close_never_confirm(tmp_path, fault):
    job, port = stage_job(tmp_path), free_port()
    faulty = copy_scripts(tmp_path / "faulty", journal_patch=LATE_DURABILITY_FAULTS[fault])
    rc, out, err = finish(launch_with(job, port, STAGE_ARGS, scripts=faulty))
    assert rc == 1 and READY.match(out.strip())
    assert '"verdict": "COMPLETE"' in err and tree_gone(job)
    # The launch journal is syntactically COMPLETE: stated, not hidden.
    records = stage_records(job)
    assert records[-1]["kind"] == "stage_complete"
    assert records[-1]["payload"] == {"result": "COMPLETE"}
    assert sj.aggregate(str(job), ["database-create", "launch"])["verdict"] == "VALID"
    # The confirmation (unpatched copy) refuses: P and C are gone.
    digest = READY.match(out.strip())[5]
    rc, out, err = confirm(job, digest)
    assert rc == 1 and "process_not_live" in err
    assert confirm_records(job)[-1]["payload"] == {"result": "FAILED",
                                                   "code": "process_not_live"}
    assert sj.aggregate(str(job), CONFIRM_STAGES)["verdict"] == "FAILED"


def test_confirm_refuses_a_failed_launch_stage(tmp_path):
    job, port = stage_job(tmp_path, "unhealthy"), free_port()
    rc, out, err = finish(launch_with(job, port, STAGE_ARGS, timeout=3))
    assert rc == 1
    receipt = job / "reference" / "launch.json"
    digest = hashlib.sha256(receipt.read_bytes()).hexdigest()
    rc, out, err = confirm(job, digest)
    assert rc == 1 and "launch_stage_not_complete" in err
    assert sj.aggregate(str(job), CONFIRM_STAGES)["verdict"] == "FAILED"


def test_confirm_refuses_another_digest(tmp_path):
    job, port = stage_job(tmp_path), free_port()
    rc, out, err = finish(launch_with(job, port, STAGE_ARGS))
    assert rc == 0, err
    m = READY.match(out.strip())
    rc, _, err = confirm(job, "0" * 64)
    assert rc == 1 and "launch_digest_mismatch" in err
    assert stop(Path(m[4]), m[5], job)[1]["verdict"] == "COMPLETE"


@pytest.mark.parametrize("argv", [
    [], ["--job-root", "relative", "--stage-index", "2", "--receipt-digest", "0" * 64],
    ["--job-root", "/tmp/x", "--stage-index", "0", "--receipt-digest", "0" * 64],
    ["--job-root", "/tmp/x", "--stage-index", "02", "--receipt-digest", "0" * 64],
    ["--job-root", "/tmp/x", "--stage-index", "2", "--receipt-digest", "XYZ"],
])
def test_confirm_usage(tmp_path, argv):
    scripts = copy_scripts(tmp_path)
    out = subprocess.run(PY + [str(scripts / "confirm-launch.py")] + argv, env=ENV,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 64 and "usage" in out.stderr



# Isolation controls for each confirm-launch rule: a synthetic launch
# journal and receipt naming live processes this test owns. Each case
# breaks exactly one rule while every other rule holds.
def _ticks(pid):
    with open("/proc/%d/stat" % pid, "rb") as f:
        return int(f.read().rsplit(b")", 1)[1].split()[19])


def synthetic_launch(tmp_path, sup, child, *, complete=True, journal_digest=None):
    job = stage_job(tmp_path)
    (job / "reference").mkdir(mode=0o700)
    core = json.loads((job / sj.CORE_NAME).read_text())
    receipt = job / "reference" / "launch.json"
    receipt.write_text(json.dumps({
        "schema": "alrust-reference-launch/1", "boot_id": core["boot_id"],
        "supervisor": {"pid": sup, "start_ticks": _ticks(sup)},
        "child": {"pid": child, "start_ticks": _ticks(child)}}))
    digest = hashlib.sha256(receipt.read_bytes()).hexdigest()
    recorded = journal_digest or digest

    class Synthetic(Exception):
        pass
    try:
        with sj.StageJournal(str(job), 1, "launch", "database-create") as journal:
            journal.record("launch_receipt", {"path": str(receipt), "sha256": recorded})
            journal.record("launch_readiness", {"receipt_sha256": recorded})
            if not complete:
                raise Synthetic()
    except Synthetic:
        pass
    return job, digest


@pytest.fixture
def owned_pair():
    """A parent P (a shell) with its own child C, plus an unrelated process."""
    procs, child, held = [], None, None
    try:
        # Each Popen is registered as soon as it exists, so a failed later
        # spawn still cleans up the earlier one.
        p = subprocess.Popen(["/bin/sh", "-c", "sleep 30 & wait"])
        procs.append(p)
        unrelated = subprocess.Popen(["/bin/sleep", "30"])
        procs.append(unrelated)
        deadline = time.monotonic() + 10
        while child is None and time.monotonic() < deadline:
            for name in os.listdir("/proc"):
                if name.isdigit():
                    try:
                        found = read_generation(int(name))
                    except (OSError, ValueError, IndexError):
                        continue
                    if found is not None and found[0] == p.pid:
                        child = int(name)
                        # Held while P is alive and unreaped, so C is still
                        # P's child; C is never signalled by bare PID.
                        held = hold_generation(child, p.pid, found[1])
                        break
            time.sleep(0.02)
        yield p.pid, child, unrelated
    finally:
        # C first, through its held pidfd while P still exists; then each
        # Popen, independently of any earlier failure.
        steps = [functools.partial(kill_held, held)] if held is not None else []
        run_all(steps + reap_steps(procs))


def test_confirm_isolated_success(tmp_path, owned_pair):
    sup, child, _ = owned_pair
    job, digest = synthetic_launch(tmp_path, sup, child)
    rc, _, err = confirm(job, digest)
    assert rc == 0, err


def test_confirm_isolated_launch_stage_not_complete(tmp_path, owned_pair):
    sup, child, _ = owned_pair
    job, digest = synthetic_launch(tmp_path, sup, child, complete=False)
    rc, _, err = confirm(job, digest)
    assert rc == 1 and "launch_stage_not_complete" in err


def test_confirm_isolated_journal_names_another_digest(tmp_path, owned_pair):
    sup, child, _ = owned_pair
    job, digest = synthetic_launch(tmp_path, sup, child, journal_digest="1" * 64)
    rc, _, err = confirm(job, digest)
    assert rc == 1 and "launch_digest_mismatch" in err


def test_confirm_isolated_wrong_parent(tmp_path, owned_pair):
    sup, _, unrelated = owned_pair
    job, digest = synthetic_launch(tmp_path, sup, unrelated.pid)
    rc, _, err = confirm(job, digest)
    assert rc == 1 and "parent_mismatch" in err


def test_confirm_isolated_zombie_child(tmp_path):
    # The child has exited but is not reaped: the PID exists, as a zombie.
    sup = os.getpid()
    zombie = subprocess.Popen(["/bin/true"])
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with open("/proc/%d/stat" % zombie.pid, "rb") as f:
            if f.read().rsplit(b")", 1)[1].split()[0] == b"Z":
                break
        time.sleep(0.02)
    try:
        job, digest = synthetic_launch(tmp_path, sup, zombie.pid)
        rc, _, err = confirm(job, digest)
        assert rc == 1 and "process_not_live" in err
    finally:
        zombie.wait()
