# E1b preparation: four-stage prefix expectations

Codex adds `entrypoint_prefix.validate_prefix_inputs` on the independently
reviewed inert E1a base `67998ac8667836b778032702ba8ff2f74532338e`. This new
head requires its own peer review. It freezes only independently supplied
expectations: exactly `database-create`, `database-create-exit`, `launch`,
`launch-confirm`, in that order, four original journal hashes, original core
and launch hashes, and integer-zero actual launcher/confirmation exit codes.
Missing, extra, reordered, duplicate or legacy two-stage prefixes refuse.
The entrypoint index is fixed at4; input cannot choose it. Diagnostics are fixed.

The four stage digests mean **SHA-256 of each complete closed journal file's
bytes**, in prefix order: the same per-stage file hashes used by `aggregate`
and the independently supplied `stages` tuple in `read_closed_launch`. They
are not digests of individual records, payloads, parsed objects or reserialized
JSON. The adapter must retain each original closed-file hash from its producer.
The separate core/launch digests name the original core and launch-receipt bytes.
The launcher exit comes from the actual `launch-reference.py` launch invocation
(ready line and hand-off), never its preflight-only invocation; confirmation
exit comes from actual `confirm-launch.py`
(recorded live hand-off). Exit0 is necessary but does not replace the later
receipt, closed-journal and current-generation checks. Neither exit is inferred
from a journal, receipt, synthetic declaration or this guard's return value.

This is a pure preparatory guard: no file, clock, reader, journal, import of
named plugins, service, credential or process action. Valid results stay
NOT_RUN, execution disabled. Hash syntax cannot authenticate originals or
establish closed bytes. Callers must retain hashes from the original producers
and exit codes from actual invocations; observed evidence cannot select its own
expected hash. Manually constructed dataclasses are not capabilities.

Remaining E1b work: actual bounded measurement of the complete independently
reviewed dependency closure with held no-follow ancestors/leaves and replacement
rechecks; interpreter/cwd/plugin/config/addopts qualification; authenticated
binding-reader input; exact closed-prefix read before entrypoint index4 opens.
The existing reader allows a broader prefix for other callers and is unchanged.
Its declarations remain historical byte evidence, not current serving-process
or connection qualification. No adapter or workflow invokes this new guard.
The /3 supervisor compatibility correction is separately approved at
`f8fe8d7af39338892eb0df2c7a8771bbaaf55f30`; it is not composed into this branch.

Run only the named `tests/controls/entrypoint_prefix_controls.py` after classifying
its imports, with the existing CPython3.12 harness, explicit pytest configuration,
confcutdir at `tests/controls`, plain assertions and third-party autoload disabled.
Controls use tuples, strings and integers only. Do not discover neighboring
process-capable tests. Local pure success supplies no hosted or cleanup credit.

Existing inert CLI, all19 disabled profiles, reference pins and project
STATUS/DECISIONS are unchanged. F3CD hold and execution/window/owner gates remain;
no helper, scan, retry, rebaseline, HTTP/SQL/VM, hosted job, runner change, merge,
release or cutover. ARM64 deferred. E1b, Phase0 and x64 beta remain incomplete.
