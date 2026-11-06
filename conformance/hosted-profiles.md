# Hosted reference profile inventory

`hosted-profiles.json` proposes one isolated profile for each of the 19 existing
HTTP modules at `b046c6b087a958a19f67ff83674b731c152d8cee`. It is inert data for
Claude's workflow design and review. It creates no workflow, launcher, service,
credentials or execution permission. Every profile is disabled and its hosted
result is `NOT_RUN`. Historical local results remain at their original heads.

The source tests need no proposed changes to begin the port. None can simply be
selected by the existing hosted healthcheck job with its current setup and count
as qualified evidence. Even the basic scaffold needs reviewed configuration,
isolation, bounded execution, cleanup and evidence. The other modules need the
four-role synthetic credential fixture; several need additional live observations.

Each profile inherits `baseline_settings` and applies `settings_overrides`.
These are proposed required settings, not observations. The seven-field config
attestation keeps its exact schema: do not put the extra plugin/forwarding or
heartbeat settings into that record. Observe and retain those separately. Use the
exact loopback target shown: several existing loaders require port 18081.
Credential roles and settings must be observed on the owned serving instance.
The baseline default plugin list is from pinned `alerta/settings.py:272`; retain
`remote_ip`, including the corrected metadata mutation case's `ip` prerequisite.

The per-module `missing` list adds to the common `hosted_prerequisites`. It is
not exhaustive launcher code. The contract document, imported fixtures and
loaders remain authoritative for complete file fields, limits, identity bindings
and case cleanup. `environment_inputs` lists the direct test/fixture inputs;
it is not permission to pass arbitrary inherited values to the server or pytest.
`ALRUST_API_KEY` is optional in the scaffold fixture and must be unset for the
proposed anonymous scaffold profile. Other profiles use the private four-role
file, not that environment variable. The API-key case also creates a fifth secret
inside its test; artifact scanning must cover it.

Profiles use their exact pytest configuration and explicitly name the module
from the repository root. Imported fixture modules are dependencies, not implicit
additional test selections. Clear inherited `PYTEST_ADDOPTS` and `ALRUST_*`, set
`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`, and use a reviewed bounded argument list.
The default `pytest.ini` modules need private capture of redacted passed-test
output where their contracts require it. Focused privacy configs must retain
plain assertions and line-only failures, without `-rP`, locals or body export.
Never upload raw logs, private fixtures or all files in an evidence directory.
A safe exported report requires a reviewed producer and complete credential scans.

The served-process adapters must observe the real serving process and database
connection after readiness, retaining task/run/PID/config provenance and any
required request-time checks. A separate application factory or authored JSON
is not a substitute. Route inventories come from the live registry, never the
response being tested. Instrumentation must leave `reference/` unchanged and
must not patch the behavior under test. The unknown-webhook vector still needs
its new producer, bounded runner and complete cleanup verifier; it expects an
API-key usage write and is not database read-only. Its raw server log must be
checked for credential leakage before any export.

Existing contracts use a signed-window identity. A hosted launcher must document
and obtain review of how its exact GitHub run/attempt, job, profile, commit,
resource and permission tuple fulfills that binding; never fabricate a historical
laptop ACK or reuse a closed window. A schema-compatible string alone grants no
permission. The shared VM still requires its shared lock, approved Phase B and
execution window. Fresh hosted machines do not depend on Phase B, but their
concurrency scope and finite budgets must be specified and reviewed under X5.
No paid usage is authorized, and no automatic self-hosted fallback is enabled.

All 19 modules remain gate-required. This inventory cannot close Phase 0 by itself:
the existing README explicitly identifies incomplete lifecycle, auth, query,
heartbeat, timeout and stateful coverage. Unimplemented vectors and intentional
security divergences need a reviewed coverage ledger against KICKOFF and the
specifications; the provider mappings are not covered by the unknown-webhook
case. A pass for every current module is necessary but not sufficient. Do not
remove a required or NOT_RUN case to get green. An explicit reclassification must
cite its reviewed scope, ADR or owner decision and preserve the gap history.
The lifecycle adversarial review and Codex acceptance of executed exact-main CI
evidence remain separate prerequisites. No candidate/Rust parity is asserted.

Next implementation: Claude's separately reviewed hash-locked reference build;
Codex's profile adapters as needed; Claude's reviewed workflow matrix and artifact
producer. Publication must not start a new heavy or HTTP path before its reviewed
prerequisites and execution permission are satisfied. Claude reviews Codex heads.
STATUS and DECISIONS remain Claude's; owner merge/release/access gates remain.
