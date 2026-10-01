# Hosted entrypoint: inert preflight

This first component is offline. All 19 profiles stay disabled and the manifest
is unchanged. It imports no HTTP client, invokes no process, creates no database
or credentials, runs no pytest selection and exports no file. It cannot produce
PASS. A caller-supplied digest checks input integrity; it is not an approval or
an execution permission.

From the repository root, using the existing harness environment:

```
PYTHONPATH=conformance python -m alrust_conformance.ci \
  --profile scaffold \
  --manifest-sha256 b50ab8fc325416b4e91e8bed5da28c42845d8910f86264c6517cac27dfb1c113
```

The valid disabled result is JSON `NOT_RUN`, exit 2, `execution_started=false`
and an empty artifact allowlist. Invalid inventory, digest, module contents,
selection, dependency, path, target or gate policy produces FAIL, exit 1, before
execution. Every listed module is checked, including other profiles and imported
fixture modules. Reads are bounded regular-file reads through directory descriptors
without following symlinks. The preflight ignores inherited pytest/ALRUST settings.
It does not interpret or serialize them.

## Proposed producer and workflow interface

Claude owns the workflow, isolated per-profile PostgreSQL database, reviewed
server launch and full owned-resource cleanup. Codex owns this entrypoint, the
manifest and needed adapters. Each enabling head needs exact review of the
producer, adapter, configuration, credentials, selection, privacy, cleanup and
execution basis. The initial workflow must also stay inert for disabled profiles;
it must not allocate a service or launch a server for them.

Future arguments for the executing revision are `--reference-venv`,
`--evidence-dir` and `--run-identity`; this component does not accept them or
pretend to implement their producers. The future identity record binds repository,
run id/attempt, job, profile, head SHA, tested merge SHA, reference pin, lock hashes
and database image digest. Those declarations must be checked against the actual
checkout and owned serving process/connection. They do not substitute for a signed
historical laptop window or authorize shared-VM access. N8 capture-event bindings,
N9 separately observed ADMIN_USERS and N10 explicit PORT=18081 remain required.

The future entrypoint provisions synthetic keys through the reviewed reference
CLI, observes the serving process through reviewed adapters, selects the exact
pytest configuration and vectors, captures failures privately and scans all
actual credentials plus generic patterns before exporting allowlisted files.
Counts and registry observations come from the serving process/connection, never
responses or a separate factory. The pinned reference stays immutable; neither
assertions nor HTTP semantics are patched.

PASS (exit 0) will require all selected vectors actually executed and passed,
with no skipped/empty result, complete attestations and successful cleanup.
Once execution starts, failures/timeouts/cancellation remain FAIL/CANCELLED;
they must not become NOT_RUN because a late producer or scan failed. A pre-run
missing prerequisite can be NOT_RUN but keeps the required gate open. The workflow
must preserve that distinction and upload only allowlisted, scanned evidence on
every outcome. Public error messages must not echo untrusted identity, environment
or credential contents.

The 39 existing collected vectors do not constitute full Phase 0 coverage. The
requirements ledger, remaining vectors, adversarial lifecycle review and accepted
suite evidence at one exact main commit remain required. Shared runner work still
needs approved/qualified Phase B, shared exclusion and its execution permission.
No new access, merge/release or ARM64 decision is made here.
