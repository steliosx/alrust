"""Pure controls for paths-only server secrets (R-EP plan P2c).

reference-supervisor.py's barrier, private-file reader and validation, and
its main() on both sides of the fork, called in-process: fork, exec,
_exit, prctl, signal registration and the child scan are replaced, so no
process is started or signalled. launch-reference.py's argument and
environment refusals and its supervisor argv. Private temporary files and
real pipes only; the values are synthetic."""
import importlib.util
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sup = load("reference_supervisor_p2c", ROOT / "conformance/scripts/reference-supervisor.py")
lr = load("launch_reference_p2c", ROOT / "conformance/scripts/launch-reference.py")

ROLE = "alrust_scaffold_123_1"
PASSWORD = "a1" * 32
URL = "postgresql://%s:%s@127.0.0.1:5432/%s" % (ROLE, PASSWORD, ROLE)
KEY = "synthetic-secret-key-" + "k" * 24


def private(path, data, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(mode)
    return str(path)


@pytest.fixture
def files(tmp_path):
    return (private(tmp_path / "job" / "database" / "url", (URL + "\n").encode()),
            private(tmp_path / "job" / "secret-key", (KEY + "\n").encode()))


# --- the barrier --------------------------------------------------------------

def test_await_release_only_for_the_byte():
    r, w = os.pipe()
    os.write(w, b"1")
    os.close(w)
    assert sup.await_release(r) is True
    os.close(r)
    r, w = os.pipe()
    os.close(w)                       # parent closed unwritten: EOF
    assert sup.await_release(r) is False
    os.close(r)
    r, w = os.pipe()
    os.write(w, b"0")
    os.close(w)
    assert sup.await_release(r) is False
    os.close(r)


@pytest.mark.parametrize("receipt_ok,expected", [(True, b"1"), (False, b"")])
def test_release_writes_only_after_a_receipt_and_always_closes(receipt_ok, expected):
    r, w = os.pipe()
    assert sup.release(w, receipt_ok) is receipt_ok
    assert os.read(r, 4) == expected  # EOF after close
    os.close(r)
    with pytest.raises(OSError):
        os.close(w)                   # already closed


def test_release_write_failure_still_closes(monkeypatch):
    r, w = os.pipe()
    monkeypatch.setattr(sup.os, "write", lambda fd, data: (_ for _ in ()).throw(
        OSError("synthetic")))
    assert sup.release(w, True) is False
    assert os.read(r, 1) == b""
    os.close(r)


# --- the reader and validation ---------------------------------------------------

def test_child_secrets_positive(files):
    url, key = files
    assert sup.child_secrets(url, key, 5432) == {"DATABASE_URL": URL, "SECRET_KEY": KEY}


@pytest.mark.parametrize("make,code", [
    (lambda p: p.chmod(0o640), "secret_file_not_private"),
    (lambda p: p.chmod(0o604), "secret_file_not_private"),
    (lambda p: os.link(p, str(p) + ".2"), "secret_file_not_private"),
    (lambda p: p.write_bytes(b"x" * (sup.MAX_URL + 2)), "secret_file_not_private"),
])
def test_reader_refuses_non_private_files(files, make, code):
    url, key = files
    make(Path(url))
    with pytest.raises(sup.SecretRefused, match=code):
        sup.child_secrets(url, key, 5432)


def test_reader_refuses_a_final_symlink(files, tmp_path):
    url, key = files
    link = tmp_path / "job" / "database" / "link"
    link.symlink_to(url)
    with pytest.raises(sup.SecretRefused, match="secret_path_unreadable"):
        sup.read_private(str(link), sup.MAX_URL)


def test_reader_refuses_a_symlinked_component(files, tmp_path):
    url, key = files
    (tmp_path / "alias").symlink_to(tmp_path / "job")
    with pytest.raises(sup.SecretRefused, match="secret_path_unreadable"):
        sup.read_private(str(tmp_path / "alias" / "database" / "url"), sup.MAX_URL)


def test_reader_refuses_a_fifo_and_a_directory(tmp_path):
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo, 0o600)
    with pytest.raises(sup.SecretRefused, match="secret_file_not_private"):
        sup.read_private(str(fifo), sup.MAX_URL)
    (tmp_path / "d").mkdir(mode=0o700)
    with pytest.raises(sup.SecretRefused):
        sup.read_private(str(tmp_path / "d"), sup.MAX_URL)


def test_reader_refuses_a_missing_file(tmp_path):
    with pytest.raises(sup.SecretRefused, match="secret_path_unreadable"):
        sup.read_private(str(tmp_path / "absent"), sup.MAX_URL)


@pytest.mark.parametrize("url", [
    URL.replace(":5432/", ":5433/"),                        # another port
    URL.rsplit("/", 1)[0] + "/alrust_other",                # database is not the role
    URL.replace("postgresql://", "postgres://"),
    URL.replace("127.0.0.1", "localhost"),
    "postgresql://postgres:postgres@127.0.0.1:5432/postgres",
    URL.replace(PASSWORD, PASSWORD.upper()),
    URL + "?sslmode=disable",
    "",
])
def test_url_refusals(tmp_path, files, url):
    _, key = files
    path = private(tmp_path / "u2", url.encode())
    with pytest.raises(sup.SecretRefused, match="database_url_invalid"):
        sup.child_secrets(path, key, 5432)


@pytest.mark.parametrize("data", [b"short", b"x" * 15, ("k" * 4097).encode(),
                                  b"bad\x01control" + b"x" * 16, b"\xff" * 20,
                                  b"two\nlines" + b"x" * 16])
def test_key_refusals(tmp_path, files, data):
    url, _ = files
    path = private(tmp_path / "k2", data)
    with pytest.raises(sup.SecretRefused):
        sup.child_secrets(url, path, 5432)


# --- the supervisor's main(), in-process ---------------------------------------

class Exit(BaseException):
    def __init__(self, code):
        self.code = code


class Exec(BaseException):
    pass


def base_argv(tmp_path, files, port="5432"):
    url, key = files
    venv = tmp_path / "venv"
    venv.mkdir(exist_ok=True)
    return ["--receipt", str(tmp_path / "launch.json"), "--terminal",
            str(tmp_path / "terminal.json"), "--venv", str(venv),
            "--database-url-file", url, "--secret-key-file", key, "--database-port", port,
            "--", "/venv/bin/alertad", "run"]


@pytest.fixture
def harness(monkeypatch):
    log = {"events": [], "opened": [], "exec": None}
    for name in sup.SECRET_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(sup.os, "getpgid", lambda pid: os.getpid())
    monkeypatch.setattr(sup.signal, "signal", lambda *a: None)
    monkeypatch.setattr(sup, "set_subreaper", lambda: 1)
    monkeypatch.setattr(sup, "children", lambda me: [])
    monkeypatch.setattr(sup.os, "kill", lambda *a: log["events"].append(("kill",) + a))
    real_open = os.open

    def opened(path, flags, *a, **k):
        log["opened"].append(path)
        return real_open(path, flags, *a, **k)
    monkeypatch.setattr(sup.os, "open", opened)
    return log


def test_parent_refuses_secret_value_variables_before_any_fork(tmp_path, files, harness,
                                                              monkeypatch, capsys):
    for name in sup.SECRET_VARIABLES:
        monkeypatch.setenv(name, "SECRETVALUE")
        monkeypatch.setattr(sup.os, "fork", lambda: harness["events"].append("fork"))
        with pytest.raises(SystemExit) as exit_:
            sup.main(base_argv(tmp_path, files))
        assert exit_.value.code == sup.EXIT_SETUP and "fork" not in harness["events"]
        err = capsys.readouterr().err
        assert name in err and "SECRETVALUE" not in err
        monkeypatch.delenv(name)


@pytest.mark.parametrize("drop", ["--database-url-file", "--secret-key-file",
                                  "--database-port"])
def test_missing_secret_options_are_usage(tmp_path, files, harness, drop):
    argv = base_argv(tmp_path, files)
    i = argv.index(drop)
    del argv[i:i + 2]
    with pytest.raises(SystemExit) as exit_:
        sup.main(argv)
    assert exit_.value.code == sup.EXIT_USAGE


@pytest.mark.parametrize("port", ["0", "65536", "05432", "x"])
def test_bad_database_port_is_usage(tmp_path, files, harness, port):
    with pytest.raises(SystemExit) as exit_:
        sup.main(base_argv(tmp_path, files, port))
    assert exit_.value.code == sup.EXIT_USAGE


def _parent(monkeypatch, harness, receipt_fails):
    monkeypatch.setattr(sup.os, "fork", lambda: 4194302)        # parent side only
    monkeypatch.setattr(sup, "stop_requested", True)            # no serve loop
    real_write = sup.write_exclusive

    def write(path, record):
        harness["events"].append(("write", os.path.basename(path)))
        if receipt_fails and path.endswith("launch.json"):
            raise OSError("synthetic receipt failure")
        return real_write(path, record)
    monkeypatch.setattr(sup, "write_exclusive", write)
    real_release = sup.release
    monkeypatch.setattr(sup, "release", lambda fd, ok: harness["events"].append(
        ("release", ok)) or real_release(fd, ok))


@pytest.mark.parametrize("receipt_fails", [False, True])
def test_parent_releases_only_after_the_receipt_and_never_opens_secrets(
        tmp_path, files, harness, monkeypatch, receipt_fails):
    _parent(monkeypatch, harness, receipt_fails)
    sup.main(base_argv(tmp_path, files))
    events = harness["events"]
    assert events[:2] == [("write", "launch.json"), ("release", not receipt_fails)]
    assert not any(e[0] == "kill" for e in events)
    url, key = files
    assert url not in harness["opened"] and key not in harness["opened"]


def _child(monkeypatch, harness, released):
    r, w = os.pipe()
    if released:
        os.write(w, b"1")
    monkeypatch.setattr(sup.os, "pipe", lambda: (r, w))
    monkeypatch.setattr(sup.os, "fork", lambda: 0)               # child side only

    def execve(path, argv, env):
        harness["exec"] = (path, argv, env)
        raise Exec()
    monkeypatch.setattr(sup.os, "execve", execve)

    harness["exits"] = []

    def _exit(code):
        # A real _exit never returns; record the first code, as the process
        # would have ended there, before the outer finally's _exit(127).
        harness["exits"].append(code)
        raise Exit(code)
    monkeypatch.setattr(sup.os, "_exit", _exit)


def test_child_reads_after_release_and_execs_with_the_values(tmp_path, files, harness,
                                                            monkeypatch):
    _child(monkeypatch, harness, True)
    with pytest.raises(Exit) as exit_:
        sup.main(base_argv(tmp_path, files))
    assert harness["exits"] == [127]                            # only after the mocked exec
    path, argv, env = harness["exec"]
    assert (path, argv) == ("/venv/bin/alertad", ["/venv/bin/alertad", "run"])
    assert env["DATABASE_URL"] == URL and env["SECRET_KEY"] == KEY


def test_child_without_release_exits_having_opened_nothing(tmp_path, files, harness,
                                                          monkeypatch, capfd):
    _child(monkeypatch, harness, False)
    with pytest.raises(Exit) as exit_:
        sup.main(base_argv(tmp_path, files))
    assert harness["exits"][0] == sup.EXIT_CHILD_BARRIER and harness["exec"] is None
    url, key = files
    assert url not in harness["opened"] and key not in harness["opened"]
    assert "/" not in harness["opened"]                          # no component walk began


def test_child_refused_secret_exits_with_a_code_never_a_value(tmp_path, files, harness,
                                                             monkeypatch, capfd):
    url, key = files
    Path(url).write_bytes(URL.replace(":5432/", ":5433/").encode())
    _child(monkeypatch, harness, True)
    with pytest.raises(Exit) as exit_:
        sup.main(base_argv(tmp_path, files))
    assert harness["exits"][0] == sup.EXIT_CHILD_SECRET and harness["exec"] is None
    err = capfd.readouterr().err
    assert "database_url_invalid" in err and PASSWORD not in err and KEY not in err


# --- the launcher -------------------------------------------------------------

LAUNCH = ["--job-root", "/job", "--venv", "/job/venv", "--port", "18081",
          "--ready-timeout", "60", "--secret-key-file", "/job/secret-key",
          "--database-port", "5432"]


def test_launcher_parses_paths_only():
    *_, secrets = lr.parse_args(LAUNCH)
    assert secrets == {"url": "/job/database/url", "key": "/job/secret-key",
                       "database_port": 5432}


@pytest.mark.parametrize("change", [
    lambda a: a[:8] + a[10:],                                   # no key file
    lambda a: a[:10],                                           # no database port
    lambda a: a[:9] + ["/elsewhere/secret-key"] + a[10:],       # key outside the job root
    lambda a: a[:9] + ["/job/../secret-key"] + a[10:],
    lambda a: a[:9] + ["secret-key"] + a[10:],
    lambda a: a[:11] + ["0"],
    lambda a: a[:11] + ["65536"],
])
def test_launcher_refusals(change):
    with pytest.raises(lr.Refused):
        lr.parse_args(change(list(LAUNCH)))


@pytest.mark.parametrize("name", ["DATABASE_URL", "SECRET_KEY", "PGPASSWORD"])
def test_launcher_refuses_value_variables(monkeypatch, name):
    for key in list(os.environ):
        if key not in lr.ALLOWED_ENV:
            monkeypatch.delenv(key)
    monkeypatch.setenv(name, "SECRETVALUE")
    with pytest.raises(lr.Refused, match=name):
        lr.check_environment()


def test_launcher_allowlist_has_no_value_variables():
    assert not {"DATABASE_URL", "SECRET_KEY", "PGPASSWORD"} & lr.ALLOWED_ENV


def test_supervisor_argv_passes_paths_and_port_only():
    secrets = {"url": "/job/database/url", "key": "/job/secret-key", "database_port": 5432}
    argv = lr.supervisor_argv("/job/reference/launch.json", "/job/reference/terminal.json",
                              "/job/venv", secrets, None, ["/job/venv/bin/alertad", "run"])
    i = argv.index("--database-url-file")
    assert argv[i:i + 6] == ["--database-url-file", "/job/database/url", "--secret-key-file",
                             "/job/secret-key", "--database-port", "5432"]
    assert argv[argv.index("--") + 1:] == ["/job/venv/bin/alertad", "run"]
