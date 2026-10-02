"""Offline controls for the core identity, stage journals and aggregation.

Only files in a private temporary job root; no process, socket or server.
"""
import hashlib
import json
import os
from pathlib import Path

import pytest

from alrust_conformance import stage_journal as sj
from alrust_conformance.security_evidence import PrivateJournal

GIT = "a" * 40
SHA = "b" * 64
ENV = {"GITHUB_REPOSITORY": "steliosx/alrust", "GITHUB_RUN_ID": "123",
       "GITHUB_RUN_ATTEMPT": "1", "GITHUB_JOB": "profile",
       "ImageOS": "ubuntu24", "ImageVersion": "20261001.1"}
STAGES = ["database", "launch", "entrypoint"]


@pytest.fixture
def job(tmp_path, monkeypatch):
    monkeypatch.setattr(sj, "EXTERNAL_CONFIG", str(tmp_path / "no-alertad.conf"))
    root = tmp_path / "job"
    root.mkdir(mode=0o700)
    return root


def core(job, **overrides):
    args = dict(profile="scaffold", event_head_sha=GIT, checkout_sha=GIT,
                reference_sha=GIT, reference_pin=GIT,
                lock_sha256={"build.lock": SHA}, lock_pins={"build.lock": SHA},
                manifest_sha256=SHA, manifest_pin=SHA, env=dict(ENV))
    args.update(overrides)
    return sj.write_core_identity(str(job), **args)


def run_stages(job, names=STAGES, fail_at=None):
    sj.prepare_journal_dir(str(job))
    previous = None
    for index, name in enumerate(names):
        try:
            with sj.StageJournal(str(job), index, name, previous) as stage:
                stage.record("observed", {"resource": None, "value": index})
                if name == fail_at:
                    stage.fail("synthetic_failure")
                    raise RuntimeError("secret detail must not be recorded")
        except RuntimeError:
            pass
        previous = name


def journal(job, index, name):
    return Path(sj.journal_path(str(job), index, name))


def test_core_identity_is_written_once_and_hashed(job):
    digest = core(job)
    data = (job / "core-identity.json").read_bytes()
    assert hashlib.sha256(data).hexdigest() == digest
    record = json.loads(data)
    assert record["schema"] == "alrust-hosted-core/1"
    assert record["run_id"] == "123" and record["profile"] == "scaffold"
    assert record["external_config"]["state"] == "absent"
    assert record["job_root"]["path"] == str(job)
    assert oct((job / "core-identity.json").stat().st_mode & 0o777) == "0o600"
    with pytest.raises(FileExistsError):
        core(job)


@pytest.mark.parametrize("override,code", [
    ({"reference_sha": "c" * 40}, "reference_not_pinned"),
    ({"lock_sha256": {"build.lock": "c" * 64}}, "locks_not_pinned"),
    ({"manifest_sha256": "c" * 64}, "manifest_not_pinned"),
    ({"checkout_sha": "A" * 40}, "git_sha_invalid"),
    ({"profile": "Bad Name"}, "profile_invalid"),
    ({"env": {k: v for k, v in ENV.items() if k != "GITHUB_RUN_ID"}},
     "environment_invalid"),
    ({"env": dict(ENV, GITHUB_RUN_ATTEMPT="0")}, "environment_invalid"),
    ({"env": dict(ENV, ImageOS="bad value")}, "environment_invalid"),
    ({"lock_pins": {}}, "locks_invalid"),
])
def test_core_identity_refusals_write_nothing(job, override, code):
    with pytest.raises(sj.JournalError) as error:
        core(job, **override)
    assert str(error.value) == code
    assert not (job / "core-identity.json").exists()


def test_external_config_present_is_refused(job, tmp_path, monkeypatch):
    conf = tmp_path / "alertad.conf"
    conf.write_text("DEBUG = True\n")
    monkeypatch.setattr(sj, "EXTERNAL_CONFIG", str(conf))
    with pytest.raises(sj.JournalError) as error:
        core(job)
    assert str(error.value) == "external_config_present"


def test_job_root_must_be_private(job):
    job.chmod(0o750)
    with pytest.raises(sj.JournalError) as error:
        core(job)
    assert str(error.value) == "directory_not_private"


def test_complete_chain_is_valid(job):
    core(job)
    run_stages(job)
    report = sj.aggregate(str(job), STAGES)
    assert report["verdict"] == "VALID", report
    assert [s["result"] for s in report["stages"]] == ["COMPLETE"] * 3
    assert [s["records"] for s in report["stages"]] == [3, 3, 3]
    first = json.loads(journal(job, 1, "launch").read_text().splitlines()[0])
    begin = first["evidence"]["payload"]
    assert begin["previous_stage"]["sha256"] == hashlib.sha256(
        journal(job, 0, "database").read_bytes()).hexdigest()


def test_failed_stage_is_a_failed_verdict_not_a_success(job, capsys):
    # JR542-R1: a recorded FAILED stage is structurally complete but never a
    # success; the CLI exits 2.
    core(job)
    run_stages(job, fail_at="launch")
    report = sj.aggregate(str(job), STAGES)
    assert (report["verdict"], report["reason"]) == ("FAILED", "stage_failed")
    assert [s["result"] for s in report["stages"]] == ["COMPLETE", "FAILED", "COMPLETE"]
    assert sj.main(["aggregate", "--job-root", str(job), "--stages",
                    ",".join(STAGES)]) == 2
    assert json.loads(capsys.readouterr().out)["verdict"] == "FAILED"
    text = journal(job, 1, "launch").read_text()
    assert "synthetic_failure" in text and "secret detail" not in text


def test_write_failure_leaves_the_stage_incomplete(job, monkeypatch):
    core(job)
    sj.prepare_journal_dir(str(job))
    original = PrivateJournal.append
    calls = {"n": 0}

    def failing(self, value):
        calls["n"] += 1
        if calls["n"] == 2:
            self.close()
            raise OSError("injected write failure")
        return original(self, value)
    monkeypatch.setattr(PrivateJournal, "append", failing)
    with pytest.raises(OSError):
        with sj.StageJournal(str(job), 0, "database") as stage:
            stage.record("observed", {"value": 1})
    monkeypatch.setattr(PrivateJournal, "append", original)
    report = sj.aggregate(str(job), ["database"])
    assert (report["verdict"], report["reason"]) == ("INCOMPLETE", "too_few_records")


def corrupt(job, mutate, stages=STAGES):
    core(job)
    run_stages(job, stages)
    mutate(job)
    report = sj.aggregate(str(job), stages)
    assert report["verdict"] == "INCOMPLETE", report
    return report["reason"]


def lines(path):
    return path.read_bytes().split(b"\n")[:-1]


def rewrite(path, new_lines, trailing=True):
    path.write_bytes(b"\n".join(new_lines) + (b"\n" if trailing else b""))


def test_missing_stage_file(job):
    assert corrupt(job, lambda j: journal(j, 2, "entrypoint").unlink()) == \
        "journal_set_mismatch"


def test_extra_unexpected_file(job):
    assert corrupt(job, lambda j: (j / "journal" / "99-extra.jsonl").write_text(
        "")) == "journal_set_mismatch"


def test_expected_order_mismatch(job):
    core(job)
    run_stages(job)
    report = sj.aggregate(str(job), ["launch", "database", "entrypoint"])
    assert (report["verdict"], report["reason"]) == ("INCOMPLETE",
                                                     "journal_set_mismatch")


def test_truncated_last_record(job):
    def mutate(j):
        p = journal(j, 1, "launch")
        p.write_bytes(p.read_bytes()[:-5])
    assert corrupt(job, mutate) == "truncated_record"


def test_blank_line_in_the_middle(job):
    def mutate(j):
        p = journal(j, 1, "launch")
        ls = lines(p)
        rewrite(p, [ls[0], b"", ls[1], ls[2]])
    assert corrupt(job, mutate) == "record_not_json"


@pytest.mark.parametrize("change", ["gap", "duplicate", "bool"])
def test_sequence_faults(job, change):
    def mutate(j):
        p = journal(j, 1, "launch")
        ls = [json.loads(x) for x in lines(p)]
        if change == "gap":
            ls[1]["sequence"] = 2
        elif change == "duplicate":
            ls[2]["sequence"] = 1
        else:
            ls[1]["sequence"] = True
        rewrite(p, [json.dumps(x).encode() for x in ls])
    assert corrupt(job, mutate) == "sequence_invalid"


def test_tampered_previous_journal_breaks_the_chain(job):
    def mutate(j):
        p = journal(j, 0, "database")
        ls = [json.loads(x) for x in lines(p)]
        ls[1]["evidence"]["payload"]["value"] = 999
        rewrite(p, [json.dumps(x).encode() for x in ls])
    assert corrupt(job, mutate) == "chain_broken"


def test_tampered_core_identity(job):
    def mutate(j):
        p = j / "core-identity.json"
        p.chmod(0o600)
        p.write_bytes(p.read_bytes().replace(b'"123"', b'"124"'))
    assert corrupt(job, mutate) == "core_identity_mismatch"


def test_stage_name_mismatch_in_records(job):
    def mutate(j):
        p = journal(j, 1, "launch")
        ls = [json.loads(x) for x in lines(p)]
        ls[1]["evidence"]["stage"]["name"] = "other"
        rewrite(p, [json.dumps(x).encode() for x in ls])
    assert corrupt(job, mutate) == "record_stage_mismatch"


def test_record_after_stage_complete(job):
    def mutate(j):
        p = journal(j, 2, "entrypoint")
        ls = [json.loads(x) for x in lines(p)]
        extra = json.loads(json.dumps(ls[1]))
        extra["sequence"] = len(ls)
        extra["evidence"]["monotonic_ns"] = ls[-1]["evidence"]["monotonic_ns"] + 1
        ls.append(extra)
        rewrite(p, [json.dumps(x).encode() for x in ls])
    assert corrupt(job, mutate) == "no_stage_complete"


def test_missing_stage_complete(job):
    def mutate(j):
        p = journal(j, 2, "entrypoint")
        rewrite(p, lines(p)[:-1])
    assert corrupt(job, mutate) == "no_stage_complete"


def test_boundary_record_in_the_middle(job):
    def mutate(j):
        p = journal(j, 1, "launch")
        ls = [json.loads(x) for x in lines(p)]
        ls[1]["evidence"]["kind"] = "stage_complete"
        rewrite(p, [json.dumps(x).encode() for x in ls])
    assert corrupt(job, mutate) == "misplaced_boundary_record"


def test_deeply_nested_record_is_typed(job):
    def mutate(j):
        p = journal(j, 1, "launch")
        ls = lines(p)
        deep = ls[1][:-1] + b',"x":' + b"[" * 10000 + b"]" * 10000 + b"}"
        rewrite(p, [ls[0], deep, ls[2]])
    assert corrupt(job, mutate) == "record_not_json"


@pytest.mark.parametrize("kind", ["symlink", "fifo", "oversized"])
def test_non_regular_or_oversized_journal(job, kind):
    def mutate(j):
        p = journal(j, 2, "entrypoint")
        p.unlink()
        if kind == "symlink":
            p.symlink_to(journal(j, 0, "database"))
        elif kind == "fifo":
            os.mkfifo(p, 0o600)
        else:
            p.write_bytes(b"x" * (sj.MAX_JOURNAL + 1))
    assert corrupt(job, mutate) in ("not_bounded_regular_file", "file_unreadable")


def test_journal_directory_must_be_private(job):
    def mutate(j):
        (j / "journal").chmod(0o755)
    assert corrupt(job, mutate) == "directory_not_private"


def test_stage_journal_refuses_a_public_directory(job):
    core(job)
    sj.prepare_journal_dir(str(job))
    (job / "journal").chmod(0o750)
    with pytest.raises(sj.JournalError):
        with sj.StageJournal(str(job), 0, "database"):
            pass


@pytest.mark.parametrize("args", [
    (0, "database", "prev"), (1, "launch", None), (0, "Bad", None),
    (32, "late", "x")])
def test_stage_journal_arguments(job, args):
    with pytest.raises(sj.JournalError):
        sj.StageJournal(str(job), *args)


def test_reserved_kinds_and_codes(job):
    core(job)
    sj.prepare_journal_dir(str(job))
    with sj.StageJournal(str(job), 0, "database") as stage:
        for bad in ("stage_begin", "stage_complete", "Bad Kind"):
            with pytest.raises(sj.JournalError):
                stage.record(bad, {})
        with pytest.raises(sj.JournalError):
            stage.fail("has spaces")


def test_cli_exit_codes(job, capsys):
    core(job)
    run_stages(job)
    assert sj.main(["aggregate", "--job-root", str(job), "--stages",
                    ",".join(STAGES)]) == 0
    assert json.loads(capsys.readouterr().out)["verdict"] == "VALID"
    assert sj.main(["aggregate", "--job-root", str(job), "--stages",
                    "database,launch"]) == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "journal_set_mismatch"
    assert sj.main(["nope"]) == 64


def git_repo(path):
    import subprocess
    env = {"PATH": "/usr/bin:/bin", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t", "HOME": str(path)}
    subprocess.run(["git", "init", "-q", str(path)], check=True, env=env)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "--allow-empty", "-m", "x"],
                   check=True, env=env)
    return subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], check=True,
                          env=env, capture_output=True, text=True).stdout.strip()


def cli_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    head = git_repo(repo)
    ref = repo / "reference" / "alerta"
    ref.mkdir(parents=True)
    ref_head = git_repo(ref)
    (repo / "conformance").mkdir()
    (repo / "conformance" / "hosted-profiles.json").write_bytes(b"{}\n")
    (repo / "build.lock").write_bytes(b"lock\n")
    return repo, head, ref_head


def cli_args(repo, job, ref_head, **change):
    args = {"--repo-root": str(repo), "--job-root": str(job), "--profile": "scaffold",
            "--event-head-sha": GIT, "--reference-pin": ref_head,
            "--manifest-pin": hashlib.sha256(b"{}\n").hexdigest()}
    args.update(change)
    out = ["core"]
    for k, v in args.items():
        out += [k, v]
    return out + ["--lock-pin", "build.lock=" + hashlib.sha256(b"lock\n").hexdigest()]


def test_core_cli_measures_the_repository(job, tmp_path, monkeypatch, capsys):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    repo, head, ref_head = cli_repo(tmp_path)
    assert sj.main(cli_args(repo, job, ref_head)) == 0
    out = json.loads(capsys.readouterr().out)
    record = json.loads((job / "core-identity.json").read_text())
    assert out["status"] == "WRITTEN"
    assert record["checkout_sha"] == head and record["reference_sha"] == ref_head
    assert record["lock_sha256"] == {"build.lock": hashlib.sha256(b"lock\n").hexdigest()}


@pytest.mark.parametrize("change,reason", [
    ({"--reference-pin": "c" * 40}, "reference_not_pinned"),
    ({"--manifest-pin": "c" * 64}, "manifest_not_pinned"),
])
def test_core_cli_refuses_unpinned_inputs(job, tmp_path, monkeypatch, capsys,
                                          change, reason):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    repo, head, ref_head = cli_repo(tmp_path)
    assert sj.main(cli_args(repo, job, ref_head, **change)) == 2
    assert json.loads(capsys.readouterr().out)["reason"] == reason
    assert not (job / "core-identity.json").exists()


def test_core_cli_refuses_a_changed_lock_and_traversal(job, tmp_path, monkeypatch, capsys):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    repo, head, ref_head = cli_repo(tmp_path)
    (repo / "build.lock").write_bytes(b"changed\n")
    assert sj.main(cli_args(repo, job, ref_head)) == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "locks_not_pinned"
    args = cli_args(repo, job, ref_head)
    args[-1] = "../escape=" + "b" * 64
    assert sj.main(args) == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "locks_invalid"
    assert sj.main(["core", "--profile"]) == 64



# JR542-R2: schema validation of the core and of every record envelope.
def rewrite_records(job, index, name, change):
    p = journal(job, index, name)
    rows = [json.loads(x) for x in lines(p)]
    change(rows)
    rewrite(p, [json.dumps(x, separators=(",", ":")).encode() for x in rows])


@pytest.mark.parametrize("field", ["utc_ns", "monotonic_ns", "boot_id", "pid"])
def test_missing_capture_field(job, field):
    def mutate(j):
        rewrite_records(j, 1, "launch",
                        lambda rows: [r["evidence"].pop(field) for r in rows])
    assert corrupt(job, mutate) == "record_envelope_invalid"


@pytest.mark.parametrize("field,value,reason", [
    ("pid", True, "record_capture_invalid"),
    ("pid", 0, "record_capture_invalid"),
    ("utc_ns", -1, "record_capture_invalid"),
    ("monotonic_ns", 1.5, "record_capture_invalid"),
    ("boot_id", "00000000-0000-0000-0000-000000000000", "record_boot_mismatch"),
    ("kind", "Bad Kind", "record_kind_invalid"),
    ("payload", [], "record_payload_invalid"),
    ("schema", "other/1", "record_envelope_invalid"),
])
def test_invalid_capture_values(job, field, value, reason):
    def mutate(j):
        def change(rows):
            rows[1]["evidence"][field] = value
        rewrite_records(j, 1, "launch", change)
    assert corrupt(job, mutate) == reason


def test_boolean_stage_index(job):
    def mutate(j):
        def change(rows):
            for r in rows:
                r["evidence"]["stage"]["index"] = False
        rewrite_records(j, 0, "database", change)
    assert corrupt(job, mutate) == "record_stage_mismatch"


def test_monotonic_time_going_backwards(job):
    def mutate(j):
        def change(rows):
            rows[2]["evidence"]["monotonic_ns"] = rows[0]["evidence"]["monotonic_ns"] - 1
        rewrite_records(j, 1, "launch", change)
    assert corrupt(job, mutate) == "record_monotonic_regressed"


def test_duplicate_json_member(job):
    def mutate(j):
        p = journal(j, 0, "database")
        p.write_bytes(p.read_bytes().replace(b'"sequence":0', b'"sequence":99,"sequence":0', 1))
    assert corrupt(job, mutate) == "record_not_json"


def test_non_finite_number(job):
    def mutate(j):
        p = journal(j, 1, "launch")
        ls = lines(p)
        ls[1] = ls[1].replace(b'"value":1', b'"value":NaN')
        rewrite(p, ls)
    assert corrupt(job, mutate) == "record_not_json"


@pytest.mark.parametrize("payload", [
    {"result": "FAILED"}, {"result": "FAILED", "code": "Bad Code"},
    {"result": "COMPLETE", "extra": 1}, {"result": "DONE"}])
def test_invalid_final_payload(job, payload):
    def mutate(j):
        def change(rows):
            rows[-1]["evidence"]["payload"] = payload
        rewrite_records(j, 2, "entrypoint", change)
    assert corrupt(job, mutate) == "result_invalid"


def test_malformed_core_refuses_stage_and_aggregation(job):
    (job / "core-identity.json").write_bytes(b"not JSON\n")
    sj.prepare_journal_dir(str(job))
    with pytest.raises(sj.JournalError) as error:
        with sj.StageJournal(str(job), 0, "database"):
            pass
    assert str(error.value) == "core_invalid"
    report = sj.aggregate(str(job), ["database"])
    assert report["verdict"] == "INCOMPLETE"


@pytest.mark.parametrize("change,reason", [
    (lambda c: c.pop("boot_id"), "core_invalid"),
    (lambda c: c.update(uid=c["uid"] + 1), "core_invalid"),
    (lambda c: c.update(run_id=True), "core_invalid"),
    (lambda c: c.update(external_config={"path": "/x", "state": "absent"}),
     "core_invalid"),
    (lambda c: c["job_root"].update(ino=c["job_root"]["ino"] + 1),
     "core_job_root_mismatch"),
    (lambda c: c.update(extra=1), "core_invalid"),
])
def test_core_schema_and_job_root_binding(job, change, reason):
    core(job)
    run_stages(job)
    p = job / "core-identity.json"
    record = json.loads(p.read_text())
    change(record)
    p.write_text(json.dumps(record) + "\n")
    report = sj.aggregate(str(job), STAGES)
    assert (report["verdict"], report["reason"]) == ("INCOMPLETE", reason)


def test_core_copied_to_another_root_is_refused(job, tmp_path):
    core(job)
    run_stages(job)
    other = tmp_path / "other-root"
    import shutil
    shutil.copytree(job, other)
    other.chmod(0o700)
    report = sj.aggregate(str(other), STAGES)
    assert (report["verdict"], report["reason"]) == ("INCOMPLETE",
                                                     "core_job_root_mismatch")


# JR542-R2a: exponent overflow to infinity, nested in an observed payload.
@pytest.mark.parametrize("token", [b"1e999", b"-1e999", b"[1e999]"])
def test_exponent_overflow_is_refused(job, token):
    def mutate(j):
        p = journal(j, 1, "launch")
        ls = lines(p)
        ls[1] = ls[1].replace(b'"value":1', b'"value":{"nested":' + token + b'}')
        assert token in ls[1]
        rewrite(p, ls)
    assert corrupt(job, mutate) == "record_not_json"


# JR542-R2b: the previous-stage link must have exact types. The LAST stage's
# link is changed, so no later chain hash can catch it instead.
@pytest.mark.parametrize("change", [
    lambda link: link.update(index=True),      # True == 1
    lambda link: link.update(index=1.0),       # 1.0 == 1
    lambda link: link.update(extra=1),
    lambda link: link.update(sha256=link["sha256"].upper()),
])
def test_previous_link_exact_types(job, change):
    core(job)
    run_stages(job)
    p = journal(job, 2, "entrypoint")
    rows = [json.loads(x) for x in lines(p)]
    link = rows[0]["evidence"]["payload"]["previous_stage"]
    assert link["index"] == 1
    change(link)
    rewrite(p, [json.dumps(x, separators=(",", ":")).encode() for x in rows])
    report = sj.aggregate(str(job), STAGES)
    assert (report["verdict"], report["reason"]) == ("INCOMPLETE", "chain_broken")


def test_previous_link_false_index_on_stage_one(job):
    # Codex's case: index False for the link to stage 0, on the last stage of
    # a two-stage set.
    core(job)
    run_stages(job, ["database", "launch"])
    p = journal(job, 1, "launch")
    rows = [json.loads(x) for x in lines(p)]
    rows[0]["evidence"]["payload"]["previous_stage"]["index"] = False
    rewrite(p, [json.dumps(x, separators=(",", ":")).encode() for x in rows])
    report = sj.aggregate(str(job), ["database", "launch"])
    assert (report["verdict"], report["reason"]) == ("INCOMPLETE", "chain_broken")
