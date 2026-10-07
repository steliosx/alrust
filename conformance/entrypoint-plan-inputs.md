# E1a: inert entrypoint plan inputs

Codex's first source component of the independently approved design
`b03cebda66b5e50763f631b3f4e8e827140acff2` (Claude `e6ce7d92`). That
approval is design only; this implementation needs its own exact-head verdict.

`entrypoint_plan.validate_plan` accepts bounded bytes and independent
caller-supplied plan digest, invocation identity and measured-file declarations.
It performs strict JSON (duplicate keys, floats/nonfinite numbers and depth
above16 refuse), exact typed schema and identity, fixed reference/loopback target,
explicit candidate api-keys selection/configuration, four required role labels,
explicit plugin-file pins, normalized interpreter declarations and bounded budget
checks. Returned tuples are detached from mutable caller input and frozen.
Every valid result remains **NOT_RUN**, `execution_enabled=false`. A plan with
execution enabled is refused. No workflow, CLI, fixture or existing manifest
selects this module; the existing inert `ci.py` and all19 disabled profiles
remain unchanged. The role labels are requirements, not credential values or
proof of actual provisioned/stored records.

This library performs **no file access, import of named plugins, current clock
read, subprocess, HTTP, SQL, credential creation or output**. The file map is a
caller-declared complete dependency list; equality to caller-declared measured
hashes is input consistency only. It does not discover transitive imports, verify
those declarations against actual files, qualify an interpreter/current process,
validate the closed launch prefix or authenticate caller approvals. The plan
hash must originate in independent exact-source review, not be selected by
runtime data. No runtime plan has been produced or approved by this head.
In particular, interpreter path/resolved-path/digest are syntactically validated
and frozen here, not compared with the live executable. E1b must separately bind
actual staged files, runtime and independent expected core/prefix evidence before
any invocation. E2 must enforce plugin/config/addopts suppression before actual
pytest; this plan alone does not enforce runner behavior.

`validate_work_deadline` checks *declared entrypoint work-phase* clock values and
budget arithmetic: real integers, at least16 seconds pre-fork/exit reserve, at
least the contract's8-minute cleanup reserve, no more than30 minutes remaining
job time, D within the stage/job work cap and client deadline at or before D.
It neither reads the clock nor creates/renews an execution lease. Caller clock
observations still need independent same-boot invocation binding. Do not reuse
this work-phase helper for registry/drop/finalizer deadlines: those belong inside
the reserved cleanup phase and require their own remaining-stage budget review
(EWD-R2). A manually constructed PlanInputs object is not an authorization token.

Controls are in the explicit file
`tests/controls/entrypoint_plan_controls.py`, whose filename is deliberately
outside default pytest test patterns. Invoke that filename only, with existing
CPython3.12 harness, plugin autoload disabled, confcutdir set to its controls
directory, the exact existing pytest configuration, plain assertions and private
output. They use bytes, synthetic declarations and arithmetic only; no actual
reference/role/fixture/plugin/closed-prefix producer is imported or activated.
Never discover the neighboring process-capable interoperability control or run
a blanket suite under the local hold.

The first run retained72PASS/1FAIL: under pytest's raised recursion limit the
1500-level nested JSON reached schema refusal instead of the test's expected
JSON refusal. Added explicit pre-decode and iterative depth16 independent of runtime
recursion settings, preserving the original failure and negative assertion. Later focused
runs record their exact source hashes/results separately. No test weakened,
external operation, helper call, rescan, retry/rebaseline of held cleanup or
window admission. This source does not finish E1b, E2/E3/E4, reference HTTP,
Phase0, Rust parity, shared-CI qualification or microVM cutover. ARM64 deferred;
owner merge/release and security-access gates preserved.
