# PR100 database evidence controls

This manually selected pure pytest module checks the database evidence contract
independently of the producer's unit regressions. It is source/offline evidence.
It does not attest a live TCP peer, PostgreSQL cluster provenance, real holder
custody, reference HTTP behavior or Rust parity.

The seven cases cover a matching endpoint and valid seal, plus refusal of a
wrong published port before SQL, an invalid container before any SQL adapter
call, absent/malformed container evidence, and a cleanup record authored in the
wrong stage. The last case rebuilds actual chained journals and rebinds the
launch-confirm digest and the tool/entrypoint exit proofs against each rewritten
journal. The proof helpers create synthetic receipts and call the actual
verifiers; aggregation, proof verification and finalization are not stubbed.
The finalizer receives the fixture's explicit expected pins (/3 core). Only the SQL/Docker
adapters are in-process fakes. Fixture generation is explicitly synthetic and
must be absent; a refusing Popen spy prevents accidental process creation.
All implementation modules and the fixture must come from this checkout.

Select this file explicitly with the project's Python 3.12 test environment,
from the repository root. Set PYTHONPATH to this checkout's conformance and
conformance/tests/unit directories; disable plugin autoload and bytecode.
Use a fresh private --basetemp outside Git. For example, with those environment
settings and the environment's python:

```sh
python -m pytest -c conformance/pytest.ini \
  --confcutdir=conformance/tests/controls --assert=plain --tb=line \
  -p no:cacheprovider -q --basetemp=/absolute/private/new-run \
  conformance/tests/controls/pr100_database_evidence.py
```

The file is not named test_*.py, so this change does not add it to a blanket
suite or a workflow. Review the actual selected paths before every execution.
Failure is a code/fixture finding; a pass is LOCAL_PASS only. It grants no
process window, CI admission, hosted gate or merge approval. Historical main
0de29ce4 admitted all five invalid cases in the separate private reproduction;
the corrected main-based producer at 54cebbb0 refuses them. This module adapts
the independently reviewed controls at 2a440854 onto the proof-bearing producer
d8e10cb6a2d6089e703bb53ca5d157afd3fd1fc9. Its seven private adapted cases also
passed at that exact producer head (2.18s); that is a separate run from this
module. Approval and execution scope do not carry forward to this new commit. Those exact run reports and the
first journal-rebuild fixture error are retained outside Git in the signed
review evidence. The current full commit must receive its own peer review.
