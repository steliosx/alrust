# Webhook batch retry safety

Status: review proposal. This note narrows the unresolved reliability edge in
owner decision Y10 and `SF-WEBHOOKS`; it does not amend either contract.

## Problem

Y10 deliberately processes every item in a webhook batch and returns the
status of the first item whose processing did not complete. A non-2xx response
can therefore be returned after other items have already completed. A sender
that retries the whole request will submit those completed items again.

The current specification has no request- or item-level idempotency contract.
Store deduplication alone is insufficient: a replay can increment duplicate
state, run pre- and post-receive plugins again, create another heartbeat, emit
more audit events, or repeat an outbound notification. An error after the store
step is the sharpest case because the response asks for a retry even though the
durable write already happened.

`inputId`, `resourceId`, and the ordered `results` array make this visible to a
caller, but they cannot stop an automatic sender from retrying the original
body. The server cannot infer retries from payload equality without also
collapsing legitimate repeated notifications.

## Required invariants

Whichever contract is selected must state and test all of the following:

1. whether delivery is at-most-once, at-least-once, or effectively-once for
   the store, plugins, heartbeat creation, audit events, and outbound effects;
2. how a retry is identified across restarts and multiple server instances;
3. the retention, customer/authentication binding, and size bounds of any
   durable replay record;
4. the response to a reused key with a different route, identity, or body;
5. behavior when the result is written but the connection is lost before the
   response reaches the sender;
6. behavior for legacy senders that cannot supply an idempotency key; and
7. cleanup and conformance evidence for every retained replay record.

## Candidate contracts

### A. Durable sender idempotency key

Require a sender-provided idempotency key for multi-item requests. Bind the
record to the effective authenticated identity, route, and a digest of the raw
body. Persist each item outcome before returning it. A replay with the same
binding returns the recorded response without re-running processing; a key
reused with a different binding is refused.

This preserves Y10's non-2xx response and makes compliant retries safe, but it
is a compatibility change. The provider review must establish which real
senders can set and preserve such a header. A request without the key still
needs an explicit policy: refuse the batch, or accept documented at-least-once
effects.

### B. Successful partial-batch response

Return a success-class or multi-status response after all items have outcomes,
so generic senders do not retry the whole request. The body remains the source
of per-item failures.

This avoids replaying successful items but changes Y10 and can lose failed
items when a sender ignores the response body. It requires a new owner decision.

### C. Explicit at-least-once effects

Keep Y10 unchanged and specify that all completed effects may repeat after a
retry. Every built-in plugin and sink must then be replay-safe, and custom
plugins must accept the same contract.

This is the smallest protocol change, but it cannot provide a general safety
guarantee for external or custom effects. It should not be described as
idempotent or effectively-once.

## Recommendation and review gate

Do not implement or claim retry safety until the owner selects one contract.
Option A is the only candidate that preserves Y10 while offering a strong
guarantee, but provider compatibility must be demonstrated first. The selected
contract belongs in `SF-WEBHOOKS` and `SF-PROVIDERS`, with vectors for partial
success, post-store failure, connection loss, replay after restart, concurrent
replay, cross-customer key reuse, body mismatch, expiry, and cleanup.

Review routing:

- **Claude agent:** establish provider/header compatibility and propose the
  implementation and persistence boundary.
- **Codex agent:** adversarially review the retry model and own the conformance
  vectors and evidence requirements.

