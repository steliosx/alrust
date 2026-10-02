"""Offline controls for the supervised launcher and receipt custody (SS-I1).

Each case builds a fake job root whose venv holds a stub `alertad` (a small
HTTP server) and runs the real launch-reference.py, reference-supervisor.py
and stop-reference.py. There is no reference install, database or network
beyond 127.0.0.1. Every case checks that the launcher's process tree is gone
afterwards and that unrelated processes stay alive.
"""
import hashlib
import json
import os
from pathlib import Path
import re
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

SERVER = '''
import http.server, os, sys
port = int(sys.argv[sys.argv.index("--port") + 1])
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
if MODE == "noserve":
    import time
    time.sleep(300)
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
    (bin_dir / "python3").symlink_to(os.path.realpath(sys.executable))
    shebang = interpreter or str(bin_dir / "python3")
    alertad = bin_dir / "alertad"
    alertad.write_text("#!%s\n%s" % (shebang, SERVER % mode))
    alertad.chmod(0o755)
    return job


def launch(job, port, timeout=15, scripts=None, env=None, **kw):
    # Byte copies of the scripts in a small repository layout, so that the
    # reference VERSION file exists without the submodule.
    scripts = scripts or copy_scripts(job.parent)
    args = PY + [str(scripts / "launch-reference.py"), "--job-root", str(job),
                 "--venv", str(job / "venv"), "--port", str(port),
                 "--ready-timeout", str(timeout)]
    return subprocess.Popen(args, env=env or ENV, stdout=subprocess.PIPE,
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


@pytest.fixture
def cleanup():
    pids = []
    yield pids
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


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
    cleanup.append(victim.pid)
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
              "--ready-timeout", "15"],
        env=ENV, stdout=write_end, stderr=subprocess.PIPE, text=True)
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
                 "--port", str(port), "--ready-timeout", "5"]
    out = subprocess.run(args, env=ENV, capture_output=True, text=True,
                         timeout=30)
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


def copy_scripts(tmp_path, supervisor_source=None, external_config=None):
    """A private copy of the scripts, optionally with one component replaced."""
    repo = tmp_path / "repo"
    scripts = repo / "conformance" / "scripts"
    if supervisor_source is None and external_config is None \
            and scripts.exists():
        return scripts
    scripts.mkdir(parents=True)
    (repo / "reference" / "alerta").mkdir(parents=True)
    (repo / "reference" / "alerta" / "VERSION").write_text("9.1.0\n")
    for name in ("launch-reference.py", "stop-reference.py",
                 "reference-supervisor.py"):
        shutil.copy(SCRIPTS / name, scripts / name)
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
