# P4 pre-signal binding controls

This manually selected pure module preserves Codex's independent P4 refusal
controls. It checks the stop helper's binding behavior at the exact source
under review; it does not qualify real supervisor teardown or workflow wiring.

The two positives supply full-schema entrypoint and tool launch receipts and
expect one mocked SIGTERM. The six negatives require no mocked signal for a
boolean or float tool stage index, missing child, float pgid, subreaper zero,
or a foreign receipt uid matched by the mocked live generation. Those cases
failed at 74c618e0 and passed at e6bae5ca in separate private evidence. That
history is distinct from this committed module's run and peer review.

The producer's private core fixture is reused, while this module independently
constructs full-schema receipts, mutates each field and calls the actual helper.
Its schema checker, strict parser, binding checks and main function are not
stubbed. All imported fixture/helper/implementation modules must come from this
checkout. The live pidfd, generation read, signal and wait are in-process fakes;
/dev/null supplies an inert descriptor that the helper closes. There is no real
pidfd acquisition or signal in these selected paths. Popen, os.kill and killpg
are refusing spies; the synthetic generation must be absent. No process,
socket, SQL or Docker job runs.

Select explicitly from the repository root with the project's Python 3.12 test
environment. Set PYTHONPATH to this checkout's conformance and
conformance/tests/unit paths, disable plugin autoload and bytecode, and choose
a fresh private --basetemp outside Git:

```sh
python -m pytest -c conformance/pytest.ini \
  --confcutdir=conformance/tests/controls --assert=plain --tb=line \
  -p no:cacheprovider -q --basetemp=/absolute/private/new-run \
  conformance/tests/controls/p4_supervisor_binding.py
```

The filename is not test_*.py, so it is not added to blanket discovery or any
workflow. Classify actual selected paths before executing. A pass is LOCAL_PASS
only: it grants no process window, CI admission, hosted gate or merge approval.
Core snapshot and helper pinning, terminal proof, deadline enforcement and
real cleanup are separate scopes. This commit needs Claude's own exact-head
review; her approval of another control is not carried forward.
