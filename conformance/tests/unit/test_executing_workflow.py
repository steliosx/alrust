"""Static controls for .github/workflows/executing.yml (W5a, slice S0).

The workflow is read as text (no YAML parser is installed in the harness
venv). File reads and hashing only: nothing is imported from the workflow,
and no process, socket, clock or network is used.

- the trigger is workflow_dispatch only; permissions are contents: read;
  concurrency never cancels;
- every file pin literal equals the sha256 of that file in this checkout,
  and the expected-pins file the workflow writes has exit_fixtures.EXPECTED's
  exact key shape with the same literals;
- every tool, verifier and deadline invocation runs under env -i;
- the steps before J is fixed have timeouts summing to at most 5 minutes
  (the allowance JOB_BUDGET_NS assumes);
- no continue-on-error and no `|| true`;
- the steps appear in the design's stage order."""
import hashlib
import json
import re
from pathlib import Path

from alrust_conformance import database_stage as ds
from alrust_conformance import stage_journal as sj

ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github" / "workflows" / "executing.yml"
TEXT = WORKFLOW.read_text()
LINES = TEXT.splitlines()
PY = "/usr/bin/python3"


def _sha(rel):
    return hashlib.sha256((ROOT / rel).read_bytes()).hexdigest()


def test_dispatch_only_read_only_never_cancelled():
    top = {line.split(":")[0]: i for i, line in enumerate(LINES)
           if re.match(r"[a-z]+:", line)}
    on_block = LINES[top["on"] + 1:min(i for i in top.values() if i > top["on"])]
    assert [line.strip() for line in on_block if line.strip()] == ["workflow_dispatch:"]
    assert "permissions:\n  contents: read\n" in TEXT
    assert "cancel-in-progress: false" in TEXT and "cancel-in-progress: true" not in TEXT
    runs_on = [l.strip() for l in LINES if l.strip().startswith("runs-on:")]
    assert runs_on == ["runs-on: ubuntu-24.04"]


def test_every_file_pin_equals_the_committed_bytes():
    pins = re.findall(r"--(?:lock-pin|supervisor-pin|entrypoint-script-pin) ([^=\s]+)=([0-9a-f]{64})",
                      TEXT)
    pins += [(p, h) for _, p, h in
             re.findall(r"--tool-pin ([a-z-]+)=([^=\s]+)=([0-9a-f]{64})", TEXT)]
    assert len(pins) == 7
    for rel, digest in pins:
        assert _sha(rel) == digest, rel
    [manifest] = re.findall(r"--manifest-pin ([0-9a-f]{64})", TEXT)
    assert manifest == _sha(sj.MANIFEST_PATH)
    tools = dict((t, p) for t, p, _ in re.findall(r"--tool-pin ([a-z-]+)=([^=\s]+)=([0-9a-f]{64})", TEXT))
    assert set(tools) == set(sj.TOOL_PIN_NAMES)
    assert "--interpreter /usr/bin/python3" in TEXT and "  PY: /usr/bin/python3" in TEXT


def test_the_expected_pins_file_matches_the_literals_and_shape():
    [line] = [l.strip() for l in LINES if l.strip().startswith('{"entrypoint":')]
    pins = json.loads(line)
    assert set(pins) == {"entrypoint", "tools"}
    assert pins["entrypoint"] == {
        "entrypoint_script_sha256": _sha("conformance/scripts/refuse-entrypoint.py"),
        "interpreter_path": PY}
    assert set(pins["tools"]) == set(sj.TOOL_PIN_NAMES)
    for tool in sj.TOOL_PIN_NAMES:
        assert pins["tools"][tool] == {"driver_sha256": _sha("conformance/scripts/stage-tool.py"),
                                       "interpreter_path": PY}


def test_the_profile_is_a_valid_infrastructure_id_not_a_manifest_profile():
    [profile] = re.findall(r"^  PROFILE: (\S+)$", TEXT, re.M)
    assert sj._STAGE.fullmatch(profile) and ds._PROFILE.fullmatch(profile)
    manifest = json.loads((ROOT / sj.MANIFEST_PATH).read_text())
    assert profile not in {p["id"] for p in manifest["profiles"]}


def test_every_invocation_runs_under_env_i():
    invocations = [i for i, l in enumerate(LINES)
                   if re.search(r'"\$PY" -(?:I -)?S -B', l)]
    assert invocations
    for i in invocations:
        start = i
        while start > 0 and LINES[start - 1].rstrip().endswith("\\"):
            start -= 1
        command = " ".join(LINES[start:i + 1])
        assert "env -i PATH=/usr/bin:/bin LANG=C.UTF-8" in command, LINES[i]


def _steps():
    return [(i, l.split("- name:", 1)[1].strip() if "- name:" in l else l.strip())
            for i, l in enumerate(LINES) if re.match(r"\s+- (name|uses):", l)]


def test_steps_before_j_fit_the_five_minute_allowance():
    steps = _steps()
    fix = next(i for i, (_, name) in enumerate(steps) if name.startswith("Fix the job deadline"))
    total = 0
    for k in range(fix):
        start = steps[k][0]
        end = steps[k + 1][0]
        found = [int(m) for m in re.findall(r"timeout-minutes: (\d+)", "\n".join(LINES[start:end]))]
        assert len(found) == 1, steps[k][1]
        total += found[0]
    assert total <= 5


def test_no_masked_failures():
    assert "continue-on-error" not in TEXT
    assert "|| true" not in TEXT


def test_the_steps_follow_the_design_order():
    names = [name for _, name in _steps()]
    order = ["Core identity", "Fix the job deadline", "Expected pins", "database-create (stage 0)",
             "Stop the tool supervisor", "database-drop (stage 2)", "Finalizer",
             "Dispose of the credential-bearing files", "Stage the original non-credential evidence",
             "Upload the evidence", "S0 verdict"]
    positions = [next(i for i, n in enumerate(names) if n.startswith(o)) for o in order]
    assert positions == sorted(positions)


def test_the_verdict_checks_the_exact_expected_lines():
    for line in ('{"result": "COMPLETE", "tool": "database-create"}',
                 '{"result": "COMPLETE", "stage": "tool-exit"}',
                 '{"result": "COMPLETE", "stage": "database-drop"}',
                 '{"cleanup": "UNPROVEN", "failure_record": "NONE", "outcome": "NOT_FINALIZED", '
                 '"reason": "entrypoint_stage_missing"}'):
        assert "grep -qx '%s'" % line in TEXT, line
    assert '[[ $(cat "$OUT/finalize.rc" 2>/dev/null) == 2 ]]' in TEXT


def test_the_stop_helper_is_pin_checked_before_it_runs():
    # W5A-R1: the literal equals the committed bytes, the check precedes the
    # P4 helper and the exit stage, and a mismatch exits before either.
    # Two checks since W5b (S0PLAN-N1): before allocation, and again here.
    literals = re.findall(r'scripts/stop-supervisor.py" ([0-9a-f]{64})', TEXT)
    assert literals == [_sha("conformance/scripts/stop-supervisor.py")] * 2
    check = next(i for i, l in enumerate(LINES)
                 if "scripts/check-pin.py" in l and l.strip().startswith("if !"))
    p4 = next(i for i, l in enumerate(LINES) if 'scripts/stop-supervisor.py" \\' in l
              and "check-pin" not in l and i > check + 1)
    exit_stage = next(i for i, l in enumerate(LINES) if "run-stage.py\" tool-exit" in l)
    assert check < p4 < exit_stage
    failure = LINES[check:p4]
    assert any(l.strip() == "exit 1" for l in failure)
    assert LINES[check].strip().startswith("if ! env -i PATH=/usr/bin:/bin LANG=C.UTF-8")


def test_check_pin_is_a_standard_library_script():
    text = (ROOT / "conformance" / "scripts" / "check-pin.py").read_text()
    imports = set(re.findall(r"^import (\w+)", text, re.M))
    assert imports <= {"hashlib", "json", "os", "re", "stat", "sys"}
    assert "from " not in "\n".join(l for l in text.splitlines() if l.startswith("from "))


# --- W5b: custody, credential disposal, the deadline margin, the pre-create pin ---

SETUP_MARGIN_MINUTES = 10   # runner and service setup allowed before J (S0PLAN-R2)


def _step_block(prefix):
    steps = _steps()
    k = next(k for k, (_, name) in enumerate(steps) if name.startswith(prefix))
    end = steps[k + 1][0] if k + 1 < len(steps) else len(LINES)
    return LINES[steps[k][0]:end]


def test_the_job_timeout_leaves_j_and_a_setup_margin():
    # J = capture + JOB_BUDGET_NS; the job timeout counts from job start.
    from alrust_conformance import deadlines as dl
    [job_timeout] = [int(m) for m in re.findall(r"^    timeout-minutes: (\d+)$", TEXT, re.M)]
    steps = _steps()
    fix = next(i for i, (_, name) in enumerate(steps) if name.startswith("Fix the job deadline"))
    pre_j = 0
    for k in range(fix + 1):        # the steps before J plus the J step itself
        block = "\n".join(LINES[steps[k][0]:steps[k + 1][0]])
        pre_j += int(re.search(r"timeout-minutes: (\d+)", block).group(1))
    budget = dl.JOB_BUDGET_NS // (60 * dl.SECOND_NS)
    assert job_timeout - budget >= pre_j + SETUP_MARGIN_MINUTES


def test_the_stop_helper_pin_is_also_checked_before_any_allocation():
    block = _step_block("database-create (stage 0)")
    pin = next(i for i, l in enumerate(block) if "scripts/check-pin.py" in l)
    launch = next(i for i, l in enumerate(block) if "tool-launch" in l)
    attempted = next(i for i, l in enumerate(block)
                     if l.strip() == 'echo "attempted=1" >> "$GITHUB_OUTPUT"')
    supervisor = next(i for i, l in enumerate(block) if "entrypoint-supervisor.py" in l)
    assert pin < launch < attempted < supervisor
    assert _sha("conformance/scripts/stop-supervisor.py") in block[pin + 1]
    assert "set -euo pipefail" in "\n".join(block)


def test_custody_steps_follow_the_finalizer_and_precede_the_verdict():
    for prefix in ("Dispose of the credential-bearing files", "Stage the original non-credential evidence"):
        block = "\n".join(_step_block(prefix))
        assert "if: always() && steps.create.outputs.attempted == '1'" in block
    upload = "\n".join(_step_block("Upload the evidence"))
    assert "if: always() && steps.export.outcome == 'success'" in upload


def test_the_upload_action_is_pinned_and_short_lived():
    upload = "\n".join(_step_block("Upload the evidence"))
    [sha] = re.findall(r"uses: actions/upload-artifact@([0-9a-f]+)", upload)
    assert len(sha) == 40
    for line in ("retention-days: 1", "if-no-files-found: error", "overwrite: false",
                 "include-hidden-files: false", "path: ${{ steps.export.outputs.staging }}"):
        assert line in upload, line
    assert "permissions:\n  contents: read\n" in TEXT and "actions: write" not in TEXT


def test_the_verdict_requires_disposal_and_staging():
    verdict = "\n".join(_step_block("S0 verdict"))
    assert '"result": "DISPOSED"' in verdict and '"result": "STAGED"' in verdict


def test_the_export_allowlist_never_names_a_credential():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "export_evidence", ROOT / "conformance" / "scripts" / "export-evidence.py")
    ex = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ex)
    names = list(ex.JOB_FILES) + list(ex.OUT_FILES)
    assert not any(n.startswith(ds.DATABASE_DIR + "/") for n in ex.JOB_FILES)
    assert not any(ex._CREDENTIAL.match(n.rsplit("/", 1)[-1]) for n in names)
    assert len(set(names)) == len(names)
