"""Pure controls for the executing workflow's deadline arithmetic
(alrust_conformance.deadlines; design section 5). The arithmetic takes NOW
and J as arguments; the job-deadline file is written in a private job root
from test_tool_supervisor_pure._job. No clock-dependent assertion, process,
socket or signal."""
import hashlib
import json
import os

import pytest

from alrust_conformance import deadlines as dl
from alrust_conformance import stage_journal as sj
from test_tool_supervisor_pure import _job

S = dl.SECOND_NS
J = 10_000 * S                                  # an arbitrary job deadline
C = J - dl.CLEANUP_RESERVE_NS


def test_the_cleanup_steps_sum_to_the_designs_402_seconds_inside_r():
    assert dl.cleanup_steps_ns() == 402 * S
    assert dl.cleanup_steps_ns() <= dl.CLEANUP_RESERVE_NS == 480 * S
    assert [name for name, _ in dl.CLEANUP_STEPS] == [
        "entrypoint-teardown", "entrypoint-exit", "stop", "registry", "registry-exit",
        "database-drop", "finalizer"]


def test_the_budgets_are_the_reviewed_constants():
    assert dl.B_NS == 16 * S and dl.STAGE_BUDGET_NS == 60 * S
    assert dl.TOOL_EXIT_RESERVE_NS == 96 * S
    assert dl.JOB_BUDGET_NS == 25 * 60 * S


def test_work_cutoff_is_j_minus_r():
    assert dl.work_cutoff(J) == C


# --- a work-phase tool launch --------------------------------------------------------

def test_early_launch_gets_the_full_stage_budget():
    now = C - 1000 * S
    assert dl.tool_launch_deadline(now, J) == now + dl.STAGE_BUDGET_NS


def test_late_launch_is_capped_so_its_exit_fits_before_c():
    now = C - dl.TOOL_EXIT_RESERVE_NS - 30 * S
    deadline = dl.tool_launch_deadline(now, J)
    assert deadline == C - dl.TOOL_EXIT_RESERVE_NS
    assert deadline + dl.TOOL_EXIT_RESERVE_NS == C


def test_a_launch_that_would_leave_less_than_b_is_refused():
    now = C - dl.TOOL_EXIT_RESERVE_NS - dl.B_NS + 1
    with pytest.raises(dl.DeadlineError, match="deadline_insufficient"):
        dl.tool_launch_deadline(now, J)
    # exactly B is still admitted
    assert dl.tool_launch_deadline(now - 1, J) == now - 1 + dl.B_NS


@pytest.mark.parametrize("now", [C, C + 1, J])
def test_no_work_phase_launch_at_or_after_c(now):
    with pytest.raises(dl.DeadlineError, match="work_phase_over"):
        dl.tool_launch_deadline(now, J)


# --- the near-boundary controls the design promises ------------------------------------

def test_work_ending_exactly_at_c_leaves_every_cleanup_step_its_maximum():
    t = C
    for _, ns in dl.CLEANUP_STEPS:
        t += ns
        assert t <= J
    assert J - t == dl.CLEANUP_RESERVE_NS - dl.cleanup_steps_ns()


def test_a_reserve_shortened_by_one_steps_maximum_refuses_before_allocation(monkeypatch):
    largest = max(ns for _, ns in dl.CLEANUP_STEPS)
    monkeypatch.setattr(dl, "CLEANUP_RESERVE_NS", dl.cleanup_steps_ns() - largest)
    with pytest.raises(dl.DeadlineError, match="cleanup_reserve_short"):
        dl.require_plan_fits(0, J, dl.S0_WORK_NS)


def test_plan_fit_is_exact_at_the_boundary():
    now = J - dl.S0_WORK_NS - dl.CLEANUP_RESERVE_NS
    assert dl.plan_fits(now, J, dl.S0_WORK_NS)
    dl.require_plan_fits(now, J, dl.S0_WORK_NS)
    assert not dl.plan_fits(now + 1, J, dl.S0_WORK_NS)
    with pytest.raises(dl.DeadlineError, match="plan_does_not_fit"):
        dl.require_plan_fits(now + 1, J, dl.S0_WORK_NS)


def test_an_admitted_s0_plan_can_launch_its_tool_with_the_full_budget():
    now = J - dl.S0_WORK_NS - dl.CLEANUP_RESERVE_NS
    assert dl.tool_launch_deadline(now, J) == now + dl.STAGE_BUDGET_NS


# --- the child deadline and the step timeout ---------------------------------------------

def test_the_child_closes_before_its_supervisor_tears_down():
    assert dl.child_deadline(0, 60 * S) == 55 * S
    with pytest.raises(dl.DeadlineError, match="deadline_insufficient"):
        dl.child_deadline(55 * S, 60 * S)


@pytest.mark.parametrize("span,minutes", [(0, 1), (60 * S, 2), (74 * S, 2), (75 * S, 3)])
def test_step_timeout_covers_d_teardown_and_margin(span, minutes):
    assert dl.step_timeout_minutes(1000 * S, 1000 * S + span) == minutes


@pytest.mark.parametrize("call", [
    lambda: dl.tool_launch_deadline(True, J),
    lambda: dl.tool_launch_deadline(-1, J),
    lambda: dl.tool_launch_deadline(0, 1.5),
    lambda: dl.work_cutoff(dl.CLEANUP_RESERVE_NS),
    lambda: dl.plan_fits(0, J, "1"),
    lambda: dl.child_deadline(0, None),
    lambda: dl.step_timeout_minutes(10, 5),
])
def test_malformed_inputs_are_refused(call):
    with pytest.raises(dl.DeadlineError):
        call()


# --- J, fixed once and bound to the core -------------------------------------------------

def _captured(root):
    data = sj.read_bounded(os.path.join(str(root), sj.CORE_NAME), sj.MAX_CORE)
    return json.loads(data)["captured_monotonic_ns"]


def test_j_is_fixed_once_and_read_back(tmp_path, monkeypatch):
    root, _ = _job(tmp_path, monkeypatch)
    now = _captured(root) + 1
    j, digest = dl.write_job_deadline(str(root), now)
    assert j == now + dl.JOB_BUDGET_NS
    path = os.path.join(str(root), dl.JOB_DEADLINE_NAME)
    assert digest == hashlib.sha256(open(path, "rb").read()).hexdigest()
    assert dl.read_job_deadline(str(root), digest) == j
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    with pytest.raises(dl.DeadlineError, match="job_deadline_exists"):
        dl.write_job_deadline(str(root), now + 5)
    assert dl.read_job_deadline(str(root), digest) == j


def _rewrite(root, change):
    """Rewrite the J file in place (same UID), as an accident or a later
    step could; returns the new bytes' digest."""
    path = os.path.join(str(root), dl.JOB_DEADLINE_NAME)
    record = json.loads(open(path, "rb").read())
    change(record)
    data = json.dumps(record, sort_keys=True).encode() + b"\n"
    os.chmod(path, 0o600)
    with open(path, "wb") as f:
        f.write(data)
    return hashlib.sha256(data).hexdigest()


def test_a_j_extended_in_place_is_refused_against_the_original_digest(tmp_path, monkeypatch):
    # DL-J-R1: same core bytes, same boot, J moved by 3600 s.
    root, _ = _job(tmp_path, monkeypatch)
    j, digest = dl.write_job_deadline(str(root), _captured(root) + 1)
    _rewrite(root, lambda r: r.update(job_deadline_monotonic_ns=j + 3600 * S))
    with pytest.raises(dl.DeadlineError, match="job_deadline_changed"):
        dl.read_job_deadline(str(root), digest)


def test_an_unchanged_j_still_reads_with_its_original_digest(tmp_path, monkeypatch):
    root, _ = _job(tmp_path, monkeypatch)
    j, digest = dl.write_job_deadline(str(root), _captured(root) + 1)
    _rewrite(root, lambda r: None)                  # same bytes rewritten
    assert dl.read_job_deadline(str(root), digest) == j


@pytest.mark.parametrize("expected", [None, "", "a" * 63, "A" * 64, "g" * 64, b"a" * 64])
def test_the_expected_digest_must_be_a_full_lowercase_sha256(tmp_path, monkeypatch, expected):
    root, _ = _job(tmp_path, monkeypatch)
    dl.write_job_deadline(str(root), _captured(root) + 1)
    with pytest.raises(dl.DeadlineError, match="job_deadline_expected_invalid"):
        dl.read_job_deadline(str(root), expected)


def test_j_cannot_precede_the_core(tmp_path, monkeypatch):
    root, _ = _job(tmp_path, monkeypatch)
    with pytest.raises(dl.DeadlineError, match="deadline_before_core"):
        dl.write_job_deadline(str(root), _captured(root) - 1)
    assert not os.path.exists(os.path.join(str(root), dl.JOB_DEADLINE_NAME))


def test_a_changed_core_invalidates_j(tmp_path, monkeypatch):
    root, _ = _job(tmp_path, monkeypatch)
    _, digest = dl.write_job_deadline(str(root), _captured(root) + 1)
    path = os.path.join(str(root), sj.CORE_NAME)
    core = json.loads(open(path, "rb").read())
    core["captured_utc_ns"] += 1
    os.chmod(path, 0o600)
    with open(path, "wb") as f:
        f.write(sj._dumps(core))
    with pytest.raises(dl.DeadlineError, match="job_deadline_core_changed"):
        dl.read_job_deadline(str(root), digest)


def test_another_boot_invalidates_j(tmp_path, monkeypatch):
    root, _ = _job(tmp_path, monkeypatch)
    _, digest = dl.write_job_deadline(str(root), _captured(root) + 1)
    monkeypatch.setattr(sj, "boot_id", lambda: "0" * 8 + "-0000-0000-0000-" + "0" * 12)
    with pytest.raises(dl.DeadlineError, match="job_deadline_other_boot"):
        dl.read_job_deadline(str(root), digest)


@pytest.mark.parametrize("change", [
    lambda r: r.update(extra=1),
    lambda r: r.update(schema="alrust-job-deadline/2"),
    lambda r: r.update(job_deadline_monotonic_ns=-1),
    lambda r: r.update(job_deadline_monotonic_ns=True),
    lambda r: r.pop("boot_id"),
])
def test_a_malformed_record_is_refused(tmp_path, monkeypatch, change):
    # The expected digest is that of the malformed bytes, as if a faulty
    # producer had written them: the record checks still refuse.
    root, _ = _job(tmp_path, monkeypatch)
    dl.write_job_deadline(str(root), _captured(root) + 1)
    digest = _rewrite(root, change)
    with pytest.raises(dl.DeadlineError, match="job_deadline_invalid"):
        dl.read_job_deadline(str(root), digest)


def test_a_linked_or_missing_record_is_unreadable(tmp_path, monkeypatch):
    root, _ = _job(tmp_path, monkeypatch)
    with pytest.raises(dl.DeadlineError, match="job_deadline_unreadable"):
        dl.read_job_deadline(str(root), "0" * 64)
    target = tmp_path / "elsewhere.json"
    target.write_text("{}")
    os.symlink(target, os.path.join(str(root), dl.JOB_DEADLINE_NAME))
    with pytest.raises(dl.DeadlineError, match="job_deadline_unreadable"):
        dl.read_job_deadline(str(root), hashlib.sha256(b"{}").hexdigest())


def test_no_core_means_no_j(tmp_path):
    root = tmp_path / "job"
    root.mkdir(mode=0o700)
    with pytest.raises(dl.DeadlineError, match="core_unavailable"):
        dl.write_job_deadline(str(root), 1)
