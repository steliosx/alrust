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
Claude's launch-stage writer and readiness record remain pending. No new
journal schema or readiness file location is invented here.

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
