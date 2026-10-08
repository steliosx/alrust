"""Deadline arithmetic for the executing workflow (W5 prerequisite;
conformance/executing-workflow-design.md section 5, EWD-R2 with the
cleanup-phase cap of Codex afa16a6a).

All times are CLOCK_MONOTONIC nanoseconds on the core's boot. Every
function takes NOW and the job deadline J as arguments and reads no
clock, so the arithmetic is pure; only write_job_deadline and
read_job_deadline touch the job root.

- J, the job deadline, is fixed once by an early step (after the core is
  written) as NOW + JOB_BUDGET_NS, bound to the core's sha256 and boot,
  and never recomputed or extended. The producing step returns the job
  deadline file's full sha256; the workflow carries that original digest
  (a step output) to every reader, which refuses unless the file still
  has exactly those bytes (DL-J-R1). The expected digest is never taken
  from bytes read later.
- The cleanup reserve R covers the whole cleanup phase at its maxima
  (CLEANUP_STEPS, about 402 s); the work cutoff is C = J - R.
- A work-phase tool launch gets D = min(NOW + STAGE_BUDGET_NS,
  C - TOOL_EXIT_RESERVE_NS): its teardown B, stop-supervisor and its exit
  stage fit before C. No work-phase launch starts at or after C, and D
  must leave at least the teardown budget B (the supervisor refuses
  otherwise; this refuses first).
- The child (stage-tool.py) gets D - CHILD_CLOSE_MARGIN_NS, so it closes
  its journal before its supervisor tears the tree down at D.
- A plan is admitted only if NOW + its work budget + R <= J, checked
  before anything is allocated.

Refusals are DeadlineError with a fixed code; nothing is ever extended.

The command line (python -m alrust_conformance.deadlines, W5a) is the only
part that reads the clock: once per call, CLOCK_MONOTONIC via
time.monotonic_ns(), the same clock the core and the journals record."""
import hashlib
import json
import os

from . import stage_journal as _sj
from .database_stage import STAGE_BUDGET_NS
from .entrypoint_exit import TEARDOWN_BUDGET_NS

SECOND_NS = 1_000_000_000
B_NS = TEARDOWN_BUDGET_NS                      # a supervised run's teardown, 16 s
STOP_SUPERVISOR_WAIT_NS = 20 * SECOND_NS       # stop-supervisor.py's wait
EXIT_STEP_NS = 60 * SECOND_NS                  # one exit-stage verifier step
REFERENCE_STOP_NS = 30 * SECOND_NS             # stop-reference.py's 20 s wait plus margin
DROP_NS = 60 * SECOND_NS                       # database-drop
FINALIZER_NS = 60 * SECOND_NS                  # the finalizer (contract section 10)
# The cleanup phase at its maxima, in order (design section 5).
CLEANUP_STEPS = (
    ("entrypoint-teardown", B_NS),
    ("entrypoint-exit", STOP_SUPERVISOR_WAIT_NS + EXIT_STEP_NS),
    ("stop", REFERENCE_STOP_NS),
    ("registry", STAGE_BUDGET_NS + B_NS),
    ("registry-exit", STOP_SUPERVISOR_WAIT_NS + EXIT_STEP_NS),
    ("database-drop", DROP_NS),
    ("finalizer", FINALIZER_NS),
)
CLEANUP_RESERVE_NS = 480 * SECOND_NS           # R: 8 minutes, above the steps' sum
# A tool's own exit after its run: teardown, stop-supervisor, exit stage.
TOOL_EXIT_RESERVE_NS = B_NS + STOP_SUPERVISOR_WAIT_NS + EXIT_STEP_NS
CHILD_CLOSE_MARGIN_NS = 5 * SECOND_NS          # the child closes its journal before D
STEP_MARGIN_NS = 30 * SECOND_NS                # terminal receipt and step overhead
# The job's 30-minute timeout less a 5-minute allowance for the steps
# before J is fixed (checkout, setup, core); those steps' own
# timeout-minutes must sum to at most 5 (a workflow-head control).
JOB_BUDGET_NS = 25 * 60 * SECOND_NS
# S0 (no launch): the database-create run and its exit before C.
S0_WORK_NS = STAGE_BUDGET_NS + TOOL_EXIT_RESERVE_NS

JOB_DEADLINE_NAME = "job-deadline.json"
SCHEMA_JOB_DEADLINE = "alrust-job-deadline/1"
MAX_JOB_DEADLINE = 4096


class DeadlineError(ValueError):
    """Fixed codes only."""


def _require(condition, code):
    if not condition:
        raise DeadlineError(code)


def _ns(value):
    return type(value) is int and value >= 0


def cleanup_steps_ns():
    """The cleanup phase's maximum, the sum of CLEANUP_STEPS."""
    return sum(ns for _, ns in CLEANUP_STEPS)


def work_cutoff(job_deadline):
    """C = J - R."""
    _require(_ns(job_deadline) and job_deadline > CLEANUP_RESERVE_NS, "job_deadline_invalid")
    return job_deadline - CLEANUP_RESERVE_NS


def plan_fits(now, job_deadline, work_ns):
    """True if NOW + WORK_NS + R <= J. Pure."""
    _require(_ns(now) and _ns(job_deadline) and _ns(work_ns), "deadline_input_invalid")
    return now + work_ns + CLEANUP_RESERVE_NS <= job_deadline


def require_plan_fits(now, job_deadline, work_ns):
    """Refuse before any allocation if R no longer covers the cleanup
    phase's maxima, or if the plan does not fit."""
    _require(CLEANUP_RESERVE_NS >= cleanup_steps_ns(), "cleanup_reserve_short")
    _require(plan_fits(now, job_deadline, work_ns), "plan_does_not_fit")


def tool_launch_deadline(now, job_deadline):
    """D for a work-phase tool launch (database-create). Refuses at or after
    C, or if D would leave less than the teardown budget B."""
    _require(_ns(now), "deadline_input_invalid")
    cutoff = work_cutoff(job_deadline)
    _require(now < cutoff, "work_phase_over")
    deadline = min(now + STAGE_BUDGET_NS, cutoff - TOOL_EXIT_RESERVE_NS)
    _require(deadline >= now + B_NS, "deadline_insufficient")
    return deadline


def child_deadline(now, deadline):
    """The child's own deadline: D - CHILD_CLOSE_MARGIN_NS, after NOW."""
    _require(_ns(now) and _ns(deadline), "deadline_input_invalid")
    child = deadline - CHILD_CLOSE_MARGIN_NS
    _require(child > now, "deadline_insufficient")
    return child


def cleanup_deadline(now, job_deadline, step):
    """D for a cleanup-phase step named STEP (one of CLEANUP_STEPS):
    min(NOW + that step's maximum, J - the maxima of every step after it),
    so the rest of the cleanup still fits before J. Refused if D would not
    leave the teardown budget B. Never capped by C."""
    _require(_ns(now) and _ns(job_deadline), "deadline_input_invalid")
    names = [name for name, _ in CLEANUP_STEPS]
    _require(step in names, "cleanup_step_invalid")
    index = names.index(step)
    remaining = sum(ns for _, ns in CLEANUP_STEPS[index + 1:])
    deadline = min(now + CLEANUP_STEPS[index][1], job_deadline - remaining)
    _require(deadline >= now + B_NS, "deadline_insufficient")
    return deadline


def step_timeout_minutes(now, deadline):
    """A step's timeout-minutes covering D, the teardown and a margin, from
    the same NOW: ceil((D - NOW + B + margin) / 60 s)."""
    _require(_ns(now) and _ns(deadline) and deadline >= now, "deadline_input_invalid")
    span = deadline - now + B_NS + STEP_MARGIN_NS
    return -(-span // (60 * SECOND_NS))


def write_job_deadline(job_root, now):
    """Fix J = NOW + JOB_BUDGET_NS once, bound to the current core's sha256
    and boot: <job_root>/job-deadline.json, 0600, exclusive (a second call
    refuses). NOW must not precede the core's capture. Returns (J, the
    file's full sha256); the caller keeps that digest as the original, for
    every read_job_deadline."""
    core_bytes, core = _core(job_root)
    _require(_ns(now) and now >= core["captured_monotonic_ns"], "deadline_before_core")
    job_deadline = now + JOB_BUDGET_NS
    record = {"schema": SCHEMA_JOB_DEADLINE, "job_deadline_monotonic_ns": job_deadline,
              "boot_id": core["boot_id"], "core_identity_sha256": hashlib.sha256(core_bytes).hexdigest()}
    data = json.dumps(record, sort_keys=True).encode() + b"\n"
    try:
        _sj._write_exclusive(os.path.join(job_root, JOB_DEADLINE_NAME), data)
    except FileExistsError:
        raise DeadlineError("job_deadline_exists") from None
    except OSError:
        raise DeadlineError("job_deadline_unwritable") from None
    return job_deadline, hashlib.sha256(data).hexdigest()


def read_job_deadline(job_root, expected_sha256):
    """J from <job_root>/job-deadline.json, only if the file's full sha256
    equals EXPECTED_SHA256, the original digest write_job_deadline returned
    (DL-J-R1), compared before anything is parsed; then exact keys, the
    current core's sha256 and boot, and the running boot. Never recomputed."""
    _require(type(expected_sha256) is str and len(expected_sha256) == 64
             and not expected_sha256.strip("0123456789abcdef"), "job_deadline_expected_invalid")
    core_bytes, core = _core(job_root)
    try:
        data = _sj.read_bounded(os.path.join(job_root, JOB_DEADLINE_NAME), MAX_JOB_DEADLINE)
    except (OSError, _sj.JournalError):
        raise DeadlineError("job_deadline_unreadable") from None
    _require(hashlib.sha256(data).hexdigest() == expected_sha256, "job_deadline_changed")
    try:
        record = _sj.strict_json(data.rstrip(b"\n"), "job_deadline_invalid")
    except _sj.JournalError:
        raise DeadlineError("job_deadline_invalid") from None
    _require(type(record) is dict and set(record) == {
        "schema", "job_deadline_monotonic_ns", "boot_id", "core_identity_sha256"}
        and record["schema"] == SCHEMA_JOB_DEADLINE
        and _ns(record["job_deadline_monotonic_ns"]), "job_deadline_invalid")
    _require(record["core_identity_sha256"] == hashlib.sha256(core_bytes).hexdigest(), "job_deadline_core_changed")
    _require(record["boot_id"] == core["boot_id"] == _sj.boot_id(), "job_deadline_other_boot")
    return record["job_deadline_monotonic_ns"]


def _core(job_root):
    try:
        core_bytes = _sj.read_bounded(os.path.join(job_root, _sj.CORE_NAME), _sj.MAX_CORE)
        core = _sj.validate_core(core_bytes, job_root)
    except (OSError, _sj.JournalError):
        raise DeadlineError("core_unavailable") from None
    return core_bytes, core


# --- command line (W5a): one clock read per call, one JSON line out -----------------

_USAGE = {"fix": ("--job-root",),
          "plan-s0": ("--job-root", "--job-deadline-sha256"),
          "tool-launch": ("--job-root", "--job-deadline-sha256"),
          "cleanup": ("--job-root", "--job-deadline-sha256", "--step")}


def _parse(argv):
    if not argv or argv[0] not in _USAGE:
        return None
    names, rest = _USAGE[argv[0]], argv[1:]
    if len(rest) != 2 * len(names) or sorted(rest[0::2]) != sorted(names):
        return None
    opts = dict(zip(rest[0::2], rest[1::2]))
    root = opts["--job-root"]
    if not (os.path.isabs(root) and os.path.normpath(root) == root):
        return None
    return argv[0], opts


def main(argv, clock=None):
    """Exit 0 with the values; 2 REFUSED with a fixed code; 64 usage."""
    import sys
    import time
    clock = clock or time.monotonic_ns
    parsed = _parse(argv)
    if parsed is None:
        line, code = {"result": "NOT_RUN", "reason": "usage"}, 64
    else:
        command, opts = parsed
        try:
            now = clock()
            if command == "fix":
                job_deadline, digest = write_job_deadline(opts["--job-root"], now)
                line = {"job_deadline_monotonic_ns": job_deadline, "job_deadline_sha256": digest}
            else:
                job_deadline = read_job_deadline(opts["--job-root"],
                                                 opts["--job-deadline-sha256"])
                if command == "plan-s0":
                    require_plan_fits(now, job_deadline, S0_WORK_NS)
                    line = {"plan": "S0", "fits": True}
                elif command == "tool-launch":
                    deadline = tool_launch_deadline(now, job_deadline)
                    line = {"deadline_monotonic_ns": deadline,
                            "child_deadline_monotonic_ns": child_deadline(now, deadline),
                            "step_timeout_minutes": step_timeout_minutes(now, deadline)}
                else:
                    deadline = cleanup_deadline(now, job_deadline, opts["--step"])
                    line = {"deadline_monotonic_ns": deadline,
                            "step_timeout_minutes": step_timeout_minutes(now, deadline)}
            code = 0
        except DeadlineError as error:
            line, code = {"result": "REFUSED", "code": str(error)}, 2
        except Exception:  # never a traceback or exception text
            line, code = {"result": "UNKNOWN", "code": "deadline_error"}, 3
    sys.stdout.write(json.dumps(line, sort_keys=True) + "\n")
    sys.stdout.flush()
    return code


if __name__ == "__main__":
    import sys
    sys.exit(main(sys.argv[1:]))
