# Alert lifecycle (Phase 0, spec item 2)

- Source: `reference/alerta` @ v9.1.0 (`f4312727783dad64198b62ed778bbca98a5144a3`).
  Citations are `path:line` inside that tree. `alert.py` means
  `alerta/models/alert.py`, `sm.py` means `alerta/models/alarms/alerta.py`,
  and `pg.py` means `alerta/database/backends/postgres/base.py`.
- Evidence level: **source-derived only**. Nothing here has been checked
  against a running server. Rules marked **[V]** are candidate conformance
  vectors; codex-alrust owns the suite and decides whether to adopt them.
- Config assumed: the defaults, meaning `ALARM_MODEL='ALERTA'`,
  `HISTORY_LIMIT=100`, `HISTORY_ON_VALUE_CHANGE=True`, `ALERT_TIMEOUT=86400`,
  `NOTIFICATION_BLACKOUT=False`, and
  `PLUGINS=[remote_ip, reject, heartbeat, blackout, forwarder]`
  (`settings.py:29`, `:32-33`, `:203-204`, `:272`, `:282`).
  ISA 18.2 is out of scope.
- **Slice status:** this file covers the **receive path** (`POST /alert`)
  only. Operator actions (`/status`, `/action`), timeout and expiry
  housekeeping, heartbeats, and the ISA model are **not yet written**. The
  Phase 0 item 2 contract stays open until they are.

## Severity and status vocabulary (`sm.py:7-58`)

| Severity | Level | | Severity | Level |
|---|---|---|---|---|
| security | 0 | | informational | 6 |
| critical | 1 | | normal / ok / cleared | 7 (the "normal level") |
| major | 2 | | debug | 8 |
| minor | 3 | | trace | 9 |
| warning | 4 | | unknown | 10 |
| indeterminate | 5 | | | |

A lower level means more severe. The defaults are: normal severity =
`normal`, previous severity = `indeterminate`, status = `open`
(`sm.py:22-24`, `:88`).

Statuses: `open`, `assign`, `ack`, `shelved`, `blackout`, `closed`,
`expired`, `unknown` (`sm.py:49-58`).

`trend(prev, cur)` (`sm.py:102-111`) returns:
- `moreSevere` when level(prev) > level(cur);
- `lessSevere` when level(prev) < level(cur);
- `noChange` when the levels are equal **or** either value is not a known
  severity.

## R0. Parsing and defaults (`alert.py:26-117`)

- **R0.1 [V]** `resource` and `event` are required (empty counts as missing),
  giving 400 (`:28-31`). The request fails with 400 if `correlate`,
  `service` or `tags` is not a list, or if `attributes` is not an object
  (`:83-90`).
- **R0.2 [V]** Attribute keys containing `.` or `$` give 400 (`:32-34`).
- **R0.3 [V]** An integer `value` is stored as a string (`:35-36`). Python's
  `bool` is a subclass of `int`, so JSON `true`/`false` in `value` are stored
  as the strings `"True"`/`"False"`. Floats and other types are not converted.
- **R0.4 [V]** `timeout`:
  - a null or absent value becomes `ALERT_TIMEOUT` (`:41`);
  - a negative value gives 400 (`:46-47`);
  - JSON strings and floats give 400 (`:91-92`).

  JSON `true`/`false` **pass** the `isinstance(x, int)` parse check and are
  converted by `int()` to timeout `1`/`0` (`:41-43`). Note `false` is not
  `None`, so it does not fall back to `ALERT_TIMEOUT`. An integer-only
  decoder would diverge here.
- **R0.5** These are the model's parse-time defaults, when a field is absent
  or empty. **They are not a claim that such an alert is stored**: under the
  default plugins, R1 rejects an empty environment and an empty service first.
  - `environment` becomes `""` (`:52`) and `group` becomes `Misc` (`:59`);
  - `severity` becomes `normal` (`:53`) and `status` becomes `open` (`:57`);
  - `type` becomes `exceptionAlert`;
  - `origin` becomes `<argv0 basename>/<hostname>` of the **server**;
  - `createTime` becomes the server's receive time.
- **R0.6 [V]** A non-empty `correlate` list gets the alert's own `event`
  appended when it is missing (`:55-56`). An empty list stays empty.

## R1. Dispatch (`alerta/utils/api.py:31-65`)

1. `assign_customer` runs (see `customer-mapping.md`). Then the pre-receive
   plugins run in order: `remote_ip`, `reject`, `heartbeat`, `blackout`,
   `forwarder`. **If an alert already has status `blackout` when the loop
   starts, the loop is skipped** (`:37-39`). That skips `reject`, `heartbeat`
   and `blackout` as well as the post-receive plugins, so a client-sent
   `status: "blackout"` bypasses policy rejection. Specify that case
   separately.
2. **R1.2 [V] Reject policy** (`plugins/reject.py:27-39`, `settings.py:276-278`).
   Before any lookup or `transition()`, the request fails with **403**
   (`views/alerts.py:57-60`) when:
   - the origin matches a pattern in `ORIGIN_BLACKLIST` (empty by default).
     Each entry is a regular expression applied with `re.match`, which is
     anchored at the start only: `foo` also blacklists `foobar` (`:24`, `:27`);
   - the environment does not **fully** match one of `ALLOWED_ENVIRONMENTS`
     (default `Production`, `Development`), which includes the default `""`.
     Each entry is a regular expression applied with `re.fullmatch` (`:25`,
     `:31`), so `Production-DR` is rejected under the defaults;
   - `service` is empty.
3. Next, an `event` of `Heartbeat` is diverted to a heartbeat with **202**
   (`plugins/heartbeat.py`). Then, with the default blackout plugin
   (`NOTIFICATION_BLACKOUT=False`), an alert that matches a blackout is
   dropped with **202** and never stored (`plugins/blackout.py`).
4. `is_duplicate` is checked first, then `is_correlated`, else `create`
   (`:55-63`).
5. **R1.1 [V]** Any exception raised during step 4 is re-raised as
   `ApiError(str(e))` with the **default code 500** (`:64-65`,
   `exceptions.py:10`). This includes the 400 that `transition()` raises for
   an unknown severity (`sm.py:121-122`). **An alert with an unknown severity
   that gets past steps 1–3 therefore returns 500, not 400.**

**Vector preconditions.** These are the default setup for an ordinary
stored-alert vector in R1.1 and R2–R7:
- an allowed environment (`Development`);
- a non-empty synthetic `service`;
- an event other than `Heartbeat`;
- no matching blackout;
- the default incoming `status`, unless the vector explicitly sets another
  one (for example R3.1's `ack`, or the separately specified
  `status: "blackout"` bypass).

**Deliberate probes are exempt** from the defaults they exist to test. These
are the policy and ordering probes (R1.2, R1 steps 1 and 3) and the clock
probes (R7.3a ties, R7.4 older `createTime`). Each probe states its own setup.

## R2. Match keys (`pg.py:160-214`)

In every case below, `customer` joins the key as `customer=X`, or as
`customer IS NULL` when the alert has no customer.

- **R2.1 [V] Duplicate:** same `environment`, `resource`, `event` and
  `severity` (`:179-188`).
- **R2.2 [V] Correlated:** same `environment` and `resource`, and one of:
  - same `event` with a **different** severity; or
  - a different `event`, where the incoming event appears in the **stored**
    alert's `correlate` list (`:190-198`).

  The incoming alert's own `correlate` list plays no part in matching.
- **R2.3** Both lookups use `fetchone` with no `ORDER BY`
  (`pg.py:179-198`). When several rows match, which one is picked is
  undefined. The dedup and correlate UPDATEs in R5 and R6 re-apply the same
  WHERE clause (R4 inserts). They can therefore **update every matching row**
  and return one of them. Nothing guarantees that the row returned is the
  row the lookup found.

## R3. `transition()` as used on receive (`sm.py:113-253`)

On receive, `action` is always `None`. **The receive path keeps only the
returned status and discards the returned severity**:
`_, new_status = …` (`alert.py:290`, `:346`) and `_, self.status = …`
(`:387`). Any severity a rule "sets", such as `CLS-*` returning `normal` or
`CLS-2/3` returning the previous severity, is **not stored** on receive.

Inputs:
- `current_status` defaults to `open`.
- `previous_status` defaults to `open`.
- `previous_severity` is `alert.previous_severity`, defaulting to
  `indeterminate`.

Rules in evaluation order (no action):

| Rule | Condition | Resulting status |
|---|---|---|
| SET-1 | the incoming `status` ≠ `open` and the severity is at the normal level | `closed` |
| SET-* | the incoming `status` ≠ `open` otherwise | the incoming `status`, as sent |
| ACK-4 | current status `ack`, previous severity ≠ `indeterminate`, trend(previous, current) = moreSevere | `open` |
| BLK-2 | current status `blackout`, previous status ≠ `blackout` | previous status |
| BLK-* | current status `blackout`, previous status = `blackout` | the incoming status |
| CLS-2 | current status `closed`, severity not at the normal level, previous status `shelved` | `shelved` |
| CLS-3 | current status `closed`, severity not at the normal level, otherwise | `open` |
| CLS-* | severity at the normal level (any state not caught above) | `closed` |
| EXP-1 | current status `expired`, severity not at the normal level | `open` |
| ALL-* | otherwise | current status unchanged |

- **R3.1 [V]** A client may set any status on receive. For example,
  `status: "ack"` with severity `major` produces a new alert that is `ack`
  (SET-*).
- **R3.2 [V]** A `shelved` alert that receives a non-normal severity stays
  `shelved`. An `assign` alert has no rules of its own and falls through to
  CLS-* or ALL-*.

## R4. Create (`alert.py:382-413`, `pg.py:264-277`)

- **R4.1 [V]** The fields are set as follows:
  - `status` comes from `transition(alert)` with every default applied. The
    result is `closed` when the severity is at the normal level (CLS-*), the
    incoming non-default status when there is one (SET), and `open`
    otherwise.
  - `previousSeverity` = `indeterminate`, and
    `trendIndication` = trend(`indeterminate`, severity).
  - `duplicateCount` = 0 and `repeat` = false.
  - `receiveTime`, `lastReceiveTime` and `updateTime` = now.
  - `lastReceiveId` = the alert id.
- **R4.2 [V]** History has exactly one entry: `{type: new, status, severity,
  value, text, timeout, user: <login>}`, whose `updateTime` is the **alert's
  `createTime`**, not the server time (`:408`).

## R5. Deduplicate (`alert.py:285-335`, `pg.py:216-240`)

- **R5.1** The current status and the previous value are read from the
  history (R7), **not from the stored row's `status` column**
  (`alert.py:288`). `transition()` runs with `previous_severity` still unset,
  so it counts as `indeterminate` and **ACK-4 can never fire on a
  duplicate**.
- **R5.2 [V]** The stored row is updated as follows:
  - `status` becomes the new status;
  - `service`, `value`, `text`, `timeout` and `rawData` are overwritten;
  - `tags` becomes the union of stored and incoming tags, de-duplicated;
  - `attributes` is merged shallowly, with incoming keys winning (jsonb `||`);
  - `duplicateCount` increases by 1 and `repeat` is set to true;
  - `lastReceiveId` becomes the **incoming** id, and `lastReceiveTime` becomes now.
- **R5.3 [V]** A duplicate does **not** change `severity`, `group`, `origin`,
  `type`, `createTime`, `receiveTime`, `correlate` or `customer`.
  `updateTime` changes only when the status changes (`pg.py:237`).
- **R5.4 [V]** History:
  - when the status changes, one `status` entry is added (its text comes
    from the status-change hook);
  - otherwise, when `value` differs from the previous history value, one
    `value` entry is added;
  - otherwise nothing is added.

  Entries are prepended, and the list is capped at `HISTORY_LIMIT`.
- **R5.5** When the status changes, the status-change hook runs **plugin
  `status_change`** on the *stored* alert, and it may rewrite the new status
  and text (`alert.py:301-302`, `utils/hooks.py:34-45`).

## R6. Correlate (`Alert.update`, `alert.py:338-379`, `pg.py:242-262`)

- **R6.1 [V]** `previousSeverity` comes from a **separate**
  `db.get_severity()` SELECT (`alert.py:341`, `pg.py:160-167`). It does not
  use the `correlate_with` row that `is_correlated()` returned, so it is the
  stored severity of *a* row matching the correlation predicate.
  `trendIndication` = trend(`previousSeverity`, incoming severity) (`:342`).
  - With exactly one matching row, that is the matched row's severity.
  - With several matching rows, the row the `get_severity` SELECT returns
    and the row `is_correlated` returns are each an unordered `fetchone`
    choice. The UPDATE is **deterministic in effect**: it applies to *every*
    row matching its predicate (R2.3). Only the single row it **returns** is
    an unordered choice. Nothing guarantees that the two SELECT choices and
    the returned row are the same row.
- **R6.2** The current and previous statuses come from history (R7). Here
  ACK-4 **can** fire, so an `ack` alert that becomes more severe reopens.
- **R6.3 [V]** The row is updated as follows:
  - `event`, `severity`, `status`, `service`, `value`, `text`, `timeout` and
    `rawData` are overwritten;
  - **`createTime` is overwritten with the incoming alert's `createTime`**
    (`pg.py:247`);
  - `duplicateCount` becomes 0 and `repeat` becomes false;
  - `previousSeverity` and `trendIndication` are set as in R6.1;
  - `receiveTime`, `lastReceiveTime` and `lastReceiveId` are refreshed;
  - `tags` becomes the union and `attributes` are merged, as in R5.2.

  `updateTime` changes only when the status changes (`pg.py:259`). `group`, `origin`,
  `type`, `correlate` and `customer` are not updated.
- **R6.4 [V]** History always gains exactly one `severity` entry, even when
  the status does not change (`:364-374`).

## R7. Deriving "current status" from history (`alert.py:256-283`, `pg.py:442-475`)

- **R7.1** `get_alert_history(self)` reads the history entries of **every**
  alert that matches environment, resource and customer where the history
  entry's `event` equals the incoming event, **or** the incoming event
  appears in that alert's `correlate` list.
- **R7.2** The entries are ordered by `update_time DESC`. The select list
  outputs `h.*` and not the alert's own `update_time`, so, by Postgres
  output-column resolution, this is the **history entry's** `updateTime`.
  That value is the client-supplied `createTime` of the alert that wrote the
  entry (R4.2, R5.4, R6.4). This resolution reading is itself unverified.
- **R7.3** Current status = the status of the first entry. Previous
  status = the status of the second entry, or `None` (treated as `open`)
  when there is only one.
- **R7.3a** `ORDER BY update_time DESC` has **no tie-breaker** (`pg.py:449`).
  When entries have equal `updateTime`, for example several alerts sent with
  the same `createTime`, the current and previous statuses are **undefined**.
  Ordinary lifecycle vectors use strictly increasing `createTime`. Tie probes
  and older-time probes (R7.4) deliberately do not, and they assert only what
  stays defined.
- **R7.4 [V]** Because of R7.2, an alert sent with an older `createTime` than
  earlier entries does not become the "current" entry for later transitions.
  Clock skew between monitoring sources can therefore change the transitions
  that follow.

## Zabbix OK-event consequence (for Phase 1 fixtures)

Assumptions:
- a single alert identity (one environment, resource, event and customer, no
  other correlated alerts);
- the vector preconditions of R1;
- **strictly increasing `createTime`** t1 < t2 < t3 < t4.

1. At t1, a `major` arrives and creates an `open` alert.
2. At t2, an OK event mapped to severity `ok` (or `normal`/`cleared`) is
   **correlated** (same event, different severity). CLS-* closes it with
   **severity stored as `ok`**, because the receive path does not normalize
   to `normal` (R3).
3. At t3, the same OK again is a **duplicate** of the closed alert, and the
   status stays `closed`.
4. At t4, a `major` is correlated again, and CLS-3 reopens the alert.

If any `createTime` is not later than the previous one, R7.3a/R7.4 apply
instead and the vector does not hold.

## Open questions

- **L1 (R1.1):** an unknown severity gives 500 in the reference. Once the
  live run confirms it, reproducing 500 is ordinary conformance. Changing it
  to 400 would be an **intentional, observable API-behavior divergence**.
  That goes through the charter escalation to the owner, with an ADR; Codex's
  concurrence alone is not enough. No divergence is proposed.
- **L2 (R7.2, R7.4):** state is derived from client-timestamp order across
  correlated alerts, not from the row's `status`. Matching this exactly is
  needed for parity, but it is fragile. Any divergence is an **alert-behavior
  contract change**, and would go to the owner.
- **L3 (R2.3):** the multi-row UPDATE behavior. alrust should either
  reproduce it or prove the case unreachable under its constraints. Codex to
  review.
