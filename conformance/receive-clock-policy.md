# Receive-clock comparison and retained evidence

Task ALRUST-P0-CLOCK-01, author codex-alrust, independent reviewer
claude-alrust. Source `f4312727783dad64198b62ed778bbca98a5144a3`.
This implements design02 SHA-256
`67bb59f57fff768feff31fa435bab47fca519bbd16ed8e2ab672a0c4b727c2da`,
including required CK-1..3 from Claude's signed design review. The new code
head needs its own explicit review; design concurrence is not execution approval.

The CLI defaults to `--comparison receive-clock-v1`. `strict-json-v1` is
still selectable for unsupported vectors and diagnostics. Library `replay()`
defaults to strict unless `checked_clocks=True` is explicitly supplied.

Only these JSON paths are projected, after validation, on successful receive:

| Path | Validation before replacement by a comparison token |
|---|---|
| `alert.receiveTime` | Create/correlate equals lastReceiveTime; dedup retains the same target's previous value |
| `alert.lastReceiveTime` | Refreshes; after explicit spacing it must be later than that target's prior lastReceiveTime |
| `alert.updateTime` | Create equals the other clocks; thereafter refreshes to lastReceiveTime iff the computed pre-hook transition differs from history-current status; otherwise retains its previous value |

All three must exist and use UTC `YYYY-MM-DDThh:mm:ss.sssZ`; nulls, invalid
dates, missing values and non-UTC formats fail. The source truncates output to
milliseconds (`utils/format.py:63-65`). Source clocks need not be monotonic:
the strictly later assertion is an explicitly spaced verification prerequisite,
not a production contract. An equal/backward refresh yields FAIL(clock), with
raw responses retained, instead of a tokenized pass. Each target waits at
least 50 ms after its previous response before its next request. Client send
and receive wall-clock observations are recorded for every request.

`alert.createTime`, every `history[].updateTime`, history order/content, IDs,
lastReceiveId, customer, severity/status, duplicateCount/repeat, nulls, unknown
fields and arbitrary timestamps in attributes/rawData remain strict. In
particular `attributes.ip` remains strict. Both targets must be isolated
loopback instances reached directly without proxy headers. Client redirects
and proxy-environment lookup remain disabled. Existing selected alert/history
href base removal remains the only URL normalization.

The branch oracle covers only the controlled seeded fixture: successful
`POST /alert`, Development, SyntheticP0Probe, labeled unique synthetic resource,
major/minor/warning/normal, default incoming status, no explicit correlation
list or value. Use a disposable empty database, default plugins and no blackout.
There is one matching identity per synthetic resource/event, not a multi-row
match. The generator advances client createTime by 1 ms per step, eliminating
ordinary history ties. Deliberate tie/skew probes stay separate and unsupported
by this policy.

Oracle branch identity comes from prior severity for that controlled identity,
request input and checked returned ID, severity/status, duplicateCount/repeat,
lastReceiveId, customer stability and history. History-current status is read
from the prior history, not guessed from the row's status. Under these fixture
preconditions, normal transitions to closed and the supported non-normal
states transition to open. This prediction runs before any status-change hook
(`models/alert.py:288-303,344-361`). Custom status hooks, operator actions,
heartbeat/housekeeping, additional states, multi-row matching and malformed,
tied or non-descending history fail closed here; none is silently normalized.
Fixtures with those semantics require their own reviewed policy.

Projection does not mutate response objects. Reports retain raw responses and
per-response branch/clock observations. Known API-key echoes are redacted only
when exporting, after comparison. Equal error responses are still FAIL.

Reports are exclusively reserved mode0600 and flushed before effects, then
updated at request/response/cleanup boundaries. The executing record starts
as FAIL, so interrupted effects are never labeled NOT_RUN. Cleanup attempts
target only acknowledged IDs bound to the synthetic resource. An interrupt
retains partial observations and closes created clients. Disposable service
cleanup is separately mandatory under the signed resource window.

Git provenance is captured before HTTP. Preflight Git failure gives NOT_RUN
and no clients/requests. Postflight metadata is recorded separately: missing
Git or changed source makes the overall verification FAIL, while retaining
the HTTP outcome, responses and cleanup. Fault injection uses fake clients
and injected results; it is offline harness evidence, not an HTTP run.

Finding identifiers: HF-1 report retention, HF-2 clock comparison, HF-3 private
report mode, HF-6 interruption label; CK-1..3 are design additions. CO-1 is
coordination/window acknowledgment, separate from all harness findings.
HF-4 dependency hashes and HF-5 short-secret over-redaction remain follow-ups.

Evidence scope: offline tests only for this new head. The earlier two-case
reference smoke at `9d70dc2e4e09c95901d1f8be2096e6ec2effa2bc` remains
LOCAL_PASS under released window03; it does not exercise this clock policy
or approve the new source. A new signed window and exact-head verdict are
required for its reference smoke. Two-server conformance and Rust parity are
NOT_RUN, and hosted CI remains CI_BLOCKED_BILLING. Full Phase0 stays open.
