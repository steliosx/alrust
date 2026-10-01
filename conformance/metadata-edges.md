# Metadata rewrite and suppression vectors

Two new tests are SOURCE_DERIVED / HTTP NOT_RUN. They cite immutable reference
`f4312727783dad64198b62ed778bbca98a5144a3` and cumulative reviewed lifecycle
`8f55c0de632ab7f1f4ad7c5154f12ac30f6ca910`. Runtime needs a fresh exact-head
Claude review, separately reviewed runner and new signed window. The released
five-case operator window does not authorize these cases.

| Case | Source-derived expectation |
|---|---|
| Ordinary receive | Post-receive forwarder returns the model; helpers remove top-level null attributes and deduplicate stored tags. The create response still has the pre-helper tag list, while detail GET has the deduplicated list. Other alert fields, including raw timestamps/history, match |
| Suppressed unknown action | An initial blackout alert skips pre/post-receive plugins and preserves its null attribute/duplicate tags. Pre-action plugin loop is also skipped, but the later status-change signal always invokes helpers; the unknown action remains blackout/major while the null attribute is removed, tags deduplicated and action history appended |

The second case distinguishes **pre-action plugin suppression** from the full
action endpoint. `Alert.from_action` always sends the status-change signal after
transition (`alert.py:666-667`). `HookTrigger.process_status_change` always
executes `alert.tag()` and `update_attributes()` (`utils/hooks.py:35-48`), even
when `process_status` skips its plugins for blackout. This nuance should be
explicit in the lifecycle specification's A5.2; it does not contradict the
skipped pre-action loop or propose a behavioral divergence.

Use the same private labeled synthetic fixture with an unscoped admin key and
an isolated default Alerta server: AuthFalse/customerViewsFalse/default plugins,
ALERTA alarm model/ALERT_TIMEOUT86400/ACK_TIMEOUT0/FWD_DESTINATIONS empty/no
housekeeping. Unique resources and known owned alert IDs are cleaned in fixture
teardown. No production inputs or credentials belong to this run.

Raw response evidence is printed after redacting known credential echoes;
lists, nulls, timestamps, fields and order are preserved. The future reviewed
runner must capture passed-test stdout using `pytest -rP` into its private0600
HTTP log; never attach raw logs to Git/mailbox. Assertions check stored tag
content/count without guessing the undefined set/SQL order. This is a scoped
assertion, not normalization of response data. The complete raw tag lists stay
in private evidence for review. Earlier history/timestamps are compared exactly;
fixed old createTime and50ms spacing keep this bounded sequence defined.

Prior five operator cases passed only at frozen
`a4d75716755b89b7a1ab9acfac122092ee7b736e`; four customer cases passed at
`a7426411181f4dd3cc859581cc203ee4067c07b4`. Their unchanged test content and
historical evidence can be identified explicitly, but neither prior approval
nor runtime result automatically applies to this new head or new scope.
Hosted Phase0 remains billing blocked; Rust parity/two-server/clock-skew/loop
header/config-variant behavior remains NOT_RUN. Preserve reference semantics;
any intentional alert/data-loss/auth divergence still needs an owner decision.
