# Core, launch and readiness reader

Inert read-only component. It implements the byte-reading slice of the
peer-approved binding plan `71dc4763c83354d9c536bc00dd07f6d511fe9ae5`.
It starts no processes/services, imports no reference package, writes no
files and authorizes no run. Tests use labeled synthetic files and stdout.

`read_binding_inputs` receives independent caller expectations: the original
core/launch/readiness hashes, all core fields except captured clocks, version,
port, expected venv path and reviewed install-manifest digest. Event head and
checkout SHA remain distinct. These arguments must come from independently
authenticated invocation and closed stage evidence. This reader cannot
authenticate their origin, validate a journal chain or prove launch success.
The reviewed launch/confirmation producer is at
`effe409933390c75d2062f3a2e1fef4aedd00703`. Its observer socket declaration is
`<job_root>/observer/observer.sock`; the manifest is
`<job_root>/reference/observer-install.json`. The earlier reader incorrectly
required a socket under `reference/` (CL62-R1). That path rule and its
synthetic fixture are corrected together; the producer is unchanged.

It opens the root from `/` through held directory descriptors without following
ancestor links, verifies the independent private root identity, reads bounded
0600 regular core/launch files through that root, and applies the existing
fully typed core schema and exact launch/1 schema. Duplicate/non-finite JSON,
boolean numeric aliases, missing/extra fields and mismatched bindings refuse.
Original readiness stdout must match its independent digest and exact launcher
line, including version, port, P/C PIDs, receipt path and receipt digest.
File handles and named entries, reference directory and root path are rechecked
before return; every owned handle gets a close attempt even on unexpected
exceptions. A close error on an otherwise successful path fails the read;
secondary close errors preserve the primary exception and do not prove cleanup.

The returned frozen record contains private bytes, P/C generation declarations,
venv identity declarations and manifest binding. **Those declarations are not
current process, interpreter, venv or installed-file observations.** This
component does not open the venv, procfs processes, observer socket or database,
inspect loaded origins, run a marked request, compose the qualifier or export
evidence. A synthetic receipt can pass these controls with independent matching
synthetic expectations; that is not reference qualification. Same-user mutation
between observations is outside any claimed security boundary.

Next separate component: independently inspect actual venv aliases and resolved
executable, then compose reviewed process/install/capture controls and explicit
original deadline. Each exact executable head needs independent peer review.
Real reference/HTTP/SQL, full Phase 0, hosted execution, shared-VM qualification
and Rust parity remain NOT_RUN for this component.

The opt-in `tests/controls/observer_launch_interoperability.py` control uses
an explicitly pinned, clean producer checkout, its real launcher/supervisor/
confirmation/stop scripts, a synthetic HTTP stand-in and stub observer modules.
It feeds the resulting receipt/stdout/journals to the actual consumer imports.
It is separate from offline unit tests and does not run Alerta, exercise an
observer capture, use a database or qualify Rust parity. No heavy-test window
or reference-execution approval follows from its result.

From this consumer checkout, using the separately reviewed producer checkout:

```sh
python conformance/tests/controls/observer_launch_interoperability.py \
  --producer-repo /absolute/path/to/clean-reviewed-producer \
  --producer-head effe409933390c75d2062f3a2e1fef4aedd00703 \
  --consumer-conformance "$PWD/conformance" \
  --work /absolute/path/to/new-private-synthetic-fixture
```

Use the existing conformance Python environment. The control refuses a producer
head mismatch or tracked edits, asserts the actual consumer import paths, and
records input source hashes and invocation results before reporting. It retains
partial results on failure and invokes only the reviewed receipt-bound stop
helper for its own process tree. Success requires COMPLETE cleanup and absent
original P/C generations. The private fixture and `result.json` remain local;
they are synthetic test evidence, not production receipts.
