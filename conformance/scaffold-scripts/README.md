# Scaffold-only reference launch scripts

`launch-reference.py`, `reference-supervisor.py` and `stop-reference.py` here
are byte-identical to `conformance/scripts/` at main `e3dee31b`, before the
R-EP stack's paths-only secrets (P2c) and staged launch gate (P3c) changed
the launcher and supervisor there. Only `conformance/scripts/run-reference.sh`
calls them, for the hosted scaffold job's reference boot and cleanup proof
(`ci.yml`, unchanged). They are kept so that job's behaviour and evidence do
not change when the stack lands (owner decision on STACK-SCAFFOLD-R1,
option 3).

- They are not reviewed for, and must never be used by, any executing path.
  The executing workflow, its core pins and its gates name only
  `conformance/scripts/`.
- They are not a security boundary. In stage mode the frozen launcher would
  import `alrust_conformance.stage_journal` from the parent directory; the
  scaffold never passes stage or observer arguments.
- Their output (a launch receipt, no stage journal) cannot satisfy the
  staged gates or any pin check.
- Remove this directory when the scaffold boot moves to the staged
  interface (option 1).
