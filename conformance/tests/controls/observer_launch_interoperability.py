"""Opt-in real-script/synthetic-server control, adapted from Claude's reviewed
CL62-R1 probe. No Alerta imports, database, actual observer capture or CI job.
Explicit clean producer SHA required; receipt content never supplies commands.
Run with a NEW private --work directory; keep the report and failed attempts.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time


SERVER = '''
import http.server, sys
port = int(sys.argv[sys.argv.index("--port") + 1])
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"OK")
    def log_message(self, *a): pass
http.server.HTTPServer(("127.0.0.1", port), H).serve_forever()
'''
STUBS = {"alrust_observer.py": b"def activate_from_environment():\n    return None\n",
         "alrust_observer_protocol.py": b"VALUE = 'SYNTHETIC codec'\n"}
SCRIPTS = ("launch-reference.py", "reference-supervisor.py",
           "confirm-launch.py", "stop-reference.py")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, timeout=10).stdout


def generation_alive(pair):
    try:
        text = Path("/proc/%d/stat" % pair["pid"]).read_text()
        rest = text[text.rfind(")") + 2:].split()
        return int(rest[19]) == pair["start_ticks"]
    except FileNotFoundError:
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--producer-repo", type=Path, required=True)
    parser.add_argument("--producer-head", required=True)
    parser.add_argument("--consumer-conformance", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    opts = parser.parse_args()
    producer = opts.producer_repo.resolve()
    consumer = opts.consumer_conformance.resolve()
    work = opts.work.absolute()
    work.mkdir(mode=0o700)  # Never initialize/reset an occupied directory.
    report = {"scope": "REAL_REVIEWED_SCRIPTS_SYNTHETIC_SERVER_AND_OBSERVER_ONLY",
              "result": "FAIL", "stop": "NOT_RUN", "source": {},
              "NOT_RUN": ["Alerta/reference HTTP", "observer capture", "SQL",
                          "Rust parity", "shared VM", "hosted job"]}
    launched = None
    receipt = None
    receipt_digest = None
    env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
    py = [sys.executable, "-I", "-S", "-B"]
    try:
        head = git(producer, "rev-parse", "HEAD").decode().strip()
        assert head == opts.producer_head, "producer_head_mismatch"
        assert not git(producer, "status", "--porcelain", "--untracked-files=no"), "producer_dirty"
        # Record metadata BEFORE launching. Later failures cannot erase report.
        report["source"] = {"producer_head": head,
                            "consumer_head": git(consumer.parent, "rev-parse", "HEAD").decode().strip(),
                            "consumer_tracked_dirty": bool(git(consumer.parent, "status", "--porcelain", "--untracked-files=no")),
                            "consumer_sha256": {}, "producer_sha256": {},
                            "control_sha256": sha(Path(__file__).read_bytes())}
        sys.path.insert(0, str(consumer))
        from alrust_conformance import stage_journal as sj
        from alrust_conformance import observer_binding_reader as br
        from alrust_conformance import observer_closed_launch as cl
        for module in (sj, br, cl):
            path = Path(module.__file__).resolve()
            assert path.parent == consumer / "alrust_conformance", "wrong_consumer_import"
            report["source"]["consumer_sha256"][path.name] = sha(path.read_bytes())
        scripts = work / "producer/conformance/scripts"
        scripts.mkdir(parents=True)
        source_package = producer / "conformance/alrust_conformance"
        shutil.copytree(source_package, scripts.parent / "alrust_conformance",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for path in sorted(source_package.glob("*.py")):
            relative = path.relative_to(producer).as_posix()
            assert path.read_bytes() == git(producer, "show", head + ":" + relative)
            report["source"]["producer_sha256"][relative] = sha(path.read_bytes())
        for name in SCRIPTS:
            path = producer / "conformance/scripts" / name
            relative = path.relative_to(producer).as_posix()
            assert path.read_bytes() == git(producer, "show", head + ":" + relative)
            shutil.copyfile(path, scripts / name)
            report["source"]["producer_sha256"][relative] = sha(path.read_bytes())
        version = work / "producer/reference/alerta/VERSION"
        version.parent.mkdir(parents=True)
        version.write_text("9.1.0\n")  # Labeled synthetic reference-version fixture.
        job = work / "job"
        job.mkdir(mode=0o700)
        env["HOME"] = str(job / "home")
        venv = job / "venv"
        bindir = venv / "bin"
        bindir.mkdir(parents=True)
        real = os.path.realpath(sys.executable)
        (bindir / "python3").symlink_to(real)
        (venv / "pyvenv.cfg").write_text(
            "home = %s\ninclude-system-site-packages = false\nversion = %d.%d.%d\n"
            % ((os.path.dirname(real),) + tuple(sys.version_info[:3])))
        (venv / "lib" / ("python%d.%d" % sys.version_info[:2]) / "site-packages").mkdir(parents=True)
        app = bindir / "alertad"
        app.write_text("#!%s\n%s" % (bindir / "python3", SERVER))
        app.chmod(0o755)
        files = []
        for name, data in STUBS.items():
            path = work / name
            path.write_bytes(data)
            files += ["--observer-file", name, sha(data), str(path)]
        pin = "e" * 40
        core_sha = sj.write_core_identity(
            str(job), profile="scaffold", event_head_sha="a" * 40, checkout_sha="b" * 40,
            reference_sha=pin, reference_pin=pin, lock_sha256={"build": "c" * 64},
            lock_pins={"build": "c" * 64}, manifest_sha256="d" * 64, manifest_pin="d" * 64,
            env={"GITHUB_REPOSITORY": "steliosx/alrust", "GITHUB_RUN_ID": "1",
                 "GITHUB_RUN_ATTEMPT": "1", "GITHUB_JOB": "reference",
                 "ImageOS": "ubuntu24", "ImageVersion": "SYNTHETIC"})
        sj.prepare_journal_dir(str(job))
        with sj.StageJournal(str(job), 0, "database-create"):
            pass  # Structural fixture only: NO database creation.
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        launch_argv = py + [str(scripts / "launch-reference.py"), "--job-root", str(job),
                            "--venv", str(venv), "--port", str(port), "--ready-timeout", "10"] + files
        launch_argv += ["--stage-index", "1", "--previous-stage", "database-create"]
        report["launcher_argv"] = launch_argv
        receipt = job / "reference/launch.json"
        out = subprocess.run(launch_argv, env=env, capture_output=True, timeout=30)
        report["launcher_rc"] = out.returncode
        (work / "launcher.stdout").write_bytes(out.stdout)
        (work / "launcher.stderr").write_bytes(out.stderr)
        receipt_digest = sha(receipt.read_bytes())
        launched = json.loads(receipt.read_bytes())
        assert out.returncode == 0, "launcher_not_successful"
        confirm_argv = py + [str(scripts / "confirm-launch.py"), "--job-root", str(job),
                             "--stage-index", "2", "--receipt-digest", receipt_digest]
        report["confirm_argv"] = confirm_argv
        conf = subprocess.run(confirm_argv, env=env, capture_output=True, timeout=15)
        report["confirm_rc"] = conf.returncode
        (work / "confirm.stdout").write_bytes(conf.stdout)
        (work / "confirm.stderr").write_bytes(conf.stderr)
        assert conf.returncode == 0, "confirmation_not_successful"
        core = json.loads((job / sj.CORE_NAME).read_bytes())
        info = job.stat()
        bindings = br.read_binding_inputs(
            str(job), (info.st_dev, info.st_ino),
            expected_core={k: core[k] for k in br.CORE_EXPECTATIONS}, core_sha256=core_sha,
            launch_sha256=receipt_digest, readiness_line=out.stdout,
            readiness_sha256=sha(out.stdout), expected_version="9.1.0", expected_port=port,
            expected_venv_path=str(venv),
            observer_manifest_sha256=launched["observer"]["manifest_sha256"])
        stages = tuple((name, sha(Path(sj.journal_path(str(job), i, name)).read_bytes()))
                       for i, name in enumerate(("database-create", "launch", "launch-confirm")))
        closed = cl.read_closed_launch(
            str(job), (info.st_dev, info.st_ino), bindings=bindings, stages=stages,
            launcher_exit_code=out.returncode, confirmation_exit_code=conf.returncode,
            deadline_ns=time.monotonic_ns() + 4_000_000_000)
        assert closed.supervisor == tuple(launched["supervisor"][k] for k in ("pid", "start_ticks"))
        assert closed.child == tuple(launched["child"][k] for k in ("pid", "start_ticks"))
        assert launched["observer"]["socket"] == str(job / "observer/observer.sock")
        report["closed_launch"] = {"supervisor": closed.supervisor, "child": closed.child,
                                   "interpreter": closed.readiness_interpreter, "scope": closed.scope,
                                   "observer_socket": launched["observer"]["socket"], "stages": stages}
        report["result"] = "LOCAL_PASS_PRODUCER_CONSUMER"
    except Exception as error:
        report["error"] = "%s: %s" % (type(error).__name__, error)
    finally:
        # Only the reviewed receipt-bound stop helper may signal this own tree.
        # Retain partial evidence even when reading receipt/metadata/cleanup fails.
        try:
            if receipt is not None and receipt.exists():
                receipt_digest = receipt_digest or sha(receipt.read_bytes())
                launched = launched or json.loads(receipt.read_bytes())
                stop_argv = py + [str(scripts / "stop-reference.py"), str(receipt), receipt_digest, str(job)]
                report["stop_argv"] = stop_argv
                stop = subprocess.run(stop_argv, capture_output=True, timeout=15)
                (work / "stop.stdout").write_bytes(stop.stdout)
                (work / "stop.stderr").write_bytes(stop.stderr)
                report["stop_rc"] = stop.returncode
                report["stop"] = json.loads(stop.stdout)["verdict"]
                report["owned_generations_alive_after_stop"] = {
                    key: generation_alive(launched[key]) for key in ("supervisor", "child")}
                assert stop.returncode == 0 and report["stop"] == "COMPLETE", "stop_not_complete"
                assert not any(report["owned_generations_alive_after_stop"].values()), "owned_generation_survived"
        except Exception as error:
            report["cleanup_error"] = "%s: %s" % (type(error).__name__, error)
            report["result"] = "FAIL"
        (work / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({k: report.get(k) for k in ("result", "error", "launcher_rc", "confirm_rc", "stop", "cleanup_error")}))
    return 0 if report["result"] == "LOCAL_PASS_PRODUCER_CONSUMER" and report["stop"] == "COMPLETE" else 1


if __name__ == "__main__":
    raise SystemExit(main())
