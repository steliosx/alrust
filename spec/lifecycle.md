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
- **Slice status:** this file covers the **receive path** (`POST /alert`,
  R0–R7) and **operator status and actions** (`PUT /alert/<id>/status` and
  `/action`, A0–A6) and **housekeeping** (`/management/housekeeping`,
  K0–K5) and **heartbeats** (HB0–HB6). The ISA 18.2 model is out of scope.
  After Codex's review of these slices, item 2's remaining open points are
  live verification and the listed questions. The Phase 0 item 2 contract stays open until
  they are.

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
  as the strings `"True"`/`"False"`. The reference passes floats, arrays and
  objects to its database adapter unconverted, with unqualified results.
  **alrust diverges (ADR-0005 decision 8, DECISIONS Y38):** a float, array or
  object is stored as PostgreSQL's `jsonb` text form and returned as that
  string (`1.5` → `"1.5"`, `[1,2]` → `"[1, 2]"`, `{"a":1}` → `"{\"a\": 1}"`).
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
5. **R1.1 [V]** Any exception raised during step 4 (duplicate, correlate
   or create only, `:54-63`) is re-raised as `ApiError(str(e))` with the
   **default code 500** (`:64-65`,
   `exceptions.py:10`). This includes the 400 that `transition()` raises for
   an unknown severity (`sm.py:121-122`). **An alert with an unknown severity
   that gets past steps 1–3 therefore returns 500, not 400.**
6. **Post-receive** (`utils/api.py:67-90`) is outside the step-4 wrap. It is
   skipped entirely when `skip_plugins` was set during pre-receive (an
   alert that was already `blackout` when the pre-receive loop started,
   `:35-39`, `:70-72`). The final `is_suppressed` is **not** re-checked.
   Otherwise:
   - `forwarder.post_receive` **returns the alert** (`plugins/forwarder.py:43-67`).
     That marks the alert as updated, so the stored row is rewritten through
     two helpers (**R1.3**).
   - An `AlertaException` from a post-receive plugin propagates as itself,
     with its own code (`:75-76`); it is not wrapped as a 500. Other
     exceptions become `ApiError` 500 when `PLUGINS_RAISE_ON_ERROR`, which is
     the default.
   - With the default empty `FWD_DESTINATIONS`, the forwarder raises no error.
7. **R1.3 [V] The tag and attribute rewrite** (receive R1 step 6; actions A2.2):
   - `Alert.update_tags` stores `list(set(tags))` (`alert.py:460-461`): tags
     are de-duplicated, and their **order may change**.
   - `update_attributes` merges the model's attributes into the stored ones
     and then **drops every key whose value is `null`** (`pg.py:339-349`).
     So an alert received with `attributes: {"x": null}` is stored with `x`,
     and `x` is then removed by this rewrite. The same happens on any
     unsuppressed action.
   - Neither helper writes `update_time`.
   - With the current vector setup (a single tag, no null attributes),
     nothing observable changes. With several unique tags, their stored
     order may still change. Conformance evidence keeps raw tag order and
     does not normalize it.

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
- **R5.6 [V] Hook helper writes (OA-5).** After `process_status`,
  `HookTrigger.process_status_change` **always** runs two helpers on the
  alert it was given (`hooks.py:43-44`), even when `process_status` skipped
  every plugin because the alert is suppressed (`utils/api.py:173-175`):
  - `alert.tag(alert.tags)`, a SQL `DISTINCT UNNEST` union that
    de-duplicates the stored tags; their order may change (`pg.py:312-319`);
  - `update_attributes`, which drops null-valued keys (R1.3).

  On receive, the hook fires only on a status change (dedup R5.5,
  correlate `alert.py:358-360`), and it runs on the *stored* matched alert
  (`duplicate_of`/`correlate_with`).

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
- **R7.3b [V]** **API order differs from storage order.** Storage
  prepends, so newest is first in the DB array. `Alert.serialize` returns
  `history` **sorted ascending** by `update_time` (`alert.py:149`), so the
  newest entry is last. `sorted()` is stable, so entries with equal
  timestamps keep their storage order (newest first) among themselves.
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

## A. Operator status and actions

`views/alerts.py` is cited as `views.py`. Both endpoints need `write:alerts`,
look the alert up with the customer filter and the id-prefix regex
(customer-mapping C16–C18), and return **404** when it is missing. The
receive-path vector preconditions (R1) apply to the alert being acted on.

### A0. Receive entries and action entries in the same history

- **A0.1** Receive-path history entries take `updateTime` from the
  client-supplied `createTime` (R4.2, R5.4, R6.4). Action and status entries
  take the **server's** `now` (`alert.py:628`, `:682`). Both are ordered
  together by `update_time DESC` (R7.2).
- **A0.2 [V]** So when a source's clock lags the server, an alert received
  *after* an operator `ack` can sort *below* that ack entry. The history then
  still reports `ack` as the current status, and it drives the next
  transition. The reverse holds when the source clock leads. Vectors must fix
  their `createTime` values relative to the server clock and record both.
  This is the same class of problem as L2.

### A1. `PUT /alert/<id>/status` (`views.py:104-141`, `alert.py:616-642`)

- **A1.1 [V]** A missing or empty `status` gives **400** "must supply
  'status' as json data" (`views.py:115`). `text` defaults to `""`
  (`views.py:111`).
- **A1.2 [V]** **No state machine and no validation.** `from_status` does
  not call `transition()`. Any non-empty string is stored as the status,
  including ones outside the Status enum, such as `"foo"`. Severity and
  `previousSeverity` are unchanged. The default plugins' `status_change`
  returns nothing (`plugins/reject.py:45-46` and peers), so it does not
  rewrite the status.
- **A1.3 [V]** **The `timeout` column is overwritten with the raw request
  value.** The history entry gets `timeout or ALERT_TIMEOUT`
  (`alert.py:619`), but the row UPDATE passes the original `timeout`
  argument (`alert.py:638`, `pg.py:279-291`). A request without a timeout
  therefore stores **database NULL** in the nullable `alerts.timeout` column
  (`sql/schema.sql:49`).
- **A1.3a [V]** **The API never shows that NULL.** Every read hydrates the
  row through `from_record` → `Alert.__init__`, which maps a `None` timeout
  to `ALERT_TIMEOUT` (`alert.py:41`, `:203-231`). The serialized alert
  therefore shows `"timeout": 86400` under the defaults, not `null`. The raw
  NULL is visible only to SQL, which means to housekeeping (K2.2).
- **A1.4 [V]** The row update sets `status`, `update_time = now` and
  `attributes` = the loaded alert's attributes (a full replace, which is a
  no-op unless a plugin changed them). `tags` becomes the union. One `status`
  history entry is prepended: `{status, severity, value, text, updateTime:
  now, user: login, timeout: timeout or ALERT_TIMEOUT}`.
- **A1.5 [V]** Success returns **200** `{"status": "ok"}`, without the alert
  (`views.py:139`). A plugin RejectException gives 400. An AlertaException
  gives its own code. Anything else gives 500 (`views.py:126-134`).

### A2. `PUT /alert/<id>/action` (`views.py:145-190`, `alert.py:644-697`)

- **A2.1 [V]** A missing `action` gives **400** (`views.py:156`). `text`
  defaults to `"<action> operator action"` (`views.py:152`).
- **A2.2** The order of steps is:
  1. pre-action plugins (`take_action`);
  2. `from_action`;
  3. post-action plugins (`post_action`) (`views.py:166-170`).

  With the default plugins:
  - `remote_ip`, `reject`, `heartbeat` and `blackout` raise
    NotImplementedError for `take_action`, which is ignored
    (`utils/api.py:108-110`).
  - **`forwarder.take_action` returns the alert** (`plugins/forwarder.py:72-100`).
    `FWD_DESTINATIONS` is empty by default, so nothing is forwarded. Because
    the return value is truthy, `process_action` treats the alert as
    updated and **rewrites tags and attributes** (R1.3: tags are
    de-duplicated and may be reordered, null-valued attributes are dropped,
    `update_time` is not touched).
  - This applies **only when the alert is not suppressed**: `process_action`
    stops the plugin loop when `alert.is_suppressed`, meaning the status is
    `blackout` (`utils/api.py:102-103`). For a `blackout` alert, no plugin
    hook runs, so there is no rewrite and no ForwardingLoop 202 from this
    path.
  - `forwarder.take_action` raises ForwardingLoop when the request carries
    this server in its loop header. That is answered with **202**
    `{"status": "ok", "message": ...}` (`views.py:178-179`).
  - No default plugin implements `post_action`. The PluginBase
    NotImplementedError is ignored (`plugins/__init__.py:44-48`).

  (Corrected: an earlier head said every default `take_action` is ignored.
  Codex raised this as OA-1.)
- **A2.3 [V]** An invalid or redundant action raises `InvalidAction`, which
  gives **409** (`views.py:176-177`). An unknown severity on the stored
  alert gives 500.
- **A2.4** The current status, the previous status and the previous timeout
  come from history (`alert.py:647`, R7). For `unack` and `unshelve`, the
  history is scanned for the most recent `ack` or `shelve` entry, and the
  entry *after* it supplies the previous status and timeout
  (`alert.py:266-276`). If that entry was evicted by `HISTORY_LIMIT`, the
  previous status falls back to **`open`** with timeout `None`
  (`alert.py:277-280`).
- **A2.5** `transition(alert, current, previous, action)` runs. **Here the
  returned severity is applied**, unlike on receive (R3). The row's
  `previousSeverity` becomes the old severity only when the severity changed
  (`alert.py:659-694`).

### A3. Action transitions (`sm.py:141-253`, `action` set)

The first matching rule wins. "Current" means the history-derived current
status (A2.4).

| Current | Action | Result (severity, status) | Rule |
|---|---|---|---|
| any | not in {open, assign, ack, unack, shelve, unshelve, close, expired, timeout} | unchanged, **row** status | ACT-1 |
| any | `unack` | current severity, previous status if the current status is `ack`; else **409** | UNACK-1 |
| any | `unshelve` | current severity, previous status if the current status is `shelved`; else **409** | UNSHL-1 |
| any | `expired` | current severity, `expired` | EXP-0 |
| any | `timeout` | current severity, `ack` if the previous status is `ack`, else `open` | ACK-0 / OPEN-0 |
| open | open | **409** | — |
| open | ack / shelve | current severity, `ack` / `shelved` | OPEN-1/2 |
| open | close | **`normal`**, `closed` | OPEN-3 |
| ack | open / shelve | current severity, `open` / `shelved` | ACK-1/2 |
| ack | ack | **409** | — |
| ack | close | **`normal`**, `closed` | ACK-3 |
| ack | assign | `open` if the row's `previousSeverity` is not `indeterminate` and trend(`previousSeverity`, severity) = moreSevere; otherwise the fall-through | ACK-4 |
| shelved | open / close | `open` with current severity / **`normal`** `closed` | SHL-1/2 |
| shelved | ack / shelve | **409** | — |
| blackout | close | **`normal`**, `closed` | BLK-1 |
| blackout | *any other* | previous status (BLK-2), or unchanged when the previous status is `blackout` (BLK-*) | BLK-2 / BLK-* |
| closed | open | **the stored `previousSeverity`** (or `indeterminate`), `open` | CLS-1 |
| closed | ack / shelve / close | **409** | — |
| closed (non-normal severity) | assign | `shelved` if the previous status is `shelved`, else `open`, **with the stored `previousSeverity`** | CLS-2 / CLS-3 |
| expired (non-normal severity) | anything except `open` | **409** | — |
| expired (non-normal severity) | open | current severity, `open` | EXP-1 |

A status of `assign`, or any string outside the enum stored through A1.2,
has **no action rules of its own**. Only the global rows (ACT-1, UNACK,
UNSHL, EXP-0, timeout) and the fall-through apply to it.

Fall-through, when no row above matched:
- a severity at the normal level → `normal`, `closed` (CLS-*);
- otherwise the status is unchanged (ALL-*).

- **A3.1 [V]** `assign` has **no transition of its own**. An `assign`
  action on an `open` alert leaves the status `open` (ALL-*), and the
  history records `type: assign`. **Among the operator endpoints**, only
  `/status` (A1) can store the status `assign`. A receive with an explicit
  `status: "assign"` stores it too (R3 SET-*). For example, an incoming
  `major` with `status: "assign"` produces `major`/`assign`.
- **A3.2 [V]** On a `blackout` alert, `ack` (for example) does *not* ack. It
  returns the previous status (BLK-2).
- **A3.3 [V]** When `close` **matches a closing rule** (OPEN-3, ACK-3,
  SHL-2, BLK-1), it stores severity **`normal`** (not `ok`) and sets
  `previousSeverity` to the old severity. A later `open` (CLS-1) restores
  that stored `previousSeverity`. From an **unsupported state** (`assign`,
  or any non-enum status) with a non-normal severity, `close` falls through
  to ALL-*. The action then **succeeds (200) with no severity or status
  change**: for example, `assign`/`major` stays `assign`/`major`. A history
  entry of type `close` is still written (A5.1).

### A4. Action timeouts (`alert.py:649-657`)

1. For `unack`, `unshelve` and `timeout`: timeout = request timeout, **or**
   the previous timeout from history (A2.4).
2. Then:
   - for `ack` and `unack`: `timeout or ACK_TIMEOUT` (default **0**, which
     means no auto-unack; `settings.py:207`);
   - for `shelve` and `unshelve`: `timeout or SHELVE_TIMEOUT` (default
     **7200**; `settings.py:208`);
   - otherwise: `timeout or <row timeout> or ALERT_TIMEOUT`.
- **A4.1 [V]** The computed timeout is written **only to the history
  entry**. The row's `timeout` column is written from the **loaded model's**
  `self.timeout` (`alert.py:693`), which is unchanged at the model level.
  This is not necessarily the raw database value: when the column was NULL
  (A1.3), the hydrated model holds `ALERT_TIMEOUT` (A1.3a), so **any action
  writes `ALERT_TIMEOUT` back and clears the NULL**.
- **A4.2** Timeout `0` is falsy, so an explicit request timeout of 0 falls
  through to the next default.

### A5. Action writes (`alert.py:674-697`, `pg.py:279-291`)

- **A5.1 [V]** One history entry is added:
  - `type`: the action name when it is a ChangeType (`open`, `assign`, `ack`,
    `unack`, `shelve`, `unshelve`, `close`, `timeout`, `expired`); otherwise
    `action` (`alert.py:669-672`);
  - `severity` and `status` are the post-transition values;
  - `text` comes from the hook;
  - `updateTime` is now; `timeout` is the A4 value; `user` is the login.
- **A5.2** The status-change hook runs on **every** action, even when the
  status did not change (`alert.py:666-667`). Unlike receive (R5.5), it is
  not guarded by a status change. It is also not skipped for suppressed
  (`blackout`) alerts, even though the pre-action plugin loop is (A2.2). So
  **every action** applies the R5.6 helper writes. The three layers must
  be kept apart (OA-6):
  - **Loaded model:** `Alert.tag` returns the `db.tag_alert` result without
    assigning `self.tags` (`alert.py:452-453`), so the model **keeps its
    duplicate tags**. `process_status_change` does assign
    `alert.attributes` (`hooks.py:44`), so the model's attributes are
    null-free.
  - **Helper DB write:** `tag_alert` de-duplicates the stored tags, and
    `update_attributes` drops the null keys in the stored row.
  - **Final row and response:** `set_alert` writes
    `tags = DISTINCT UNNEST(stored tags || self.tags)` (`pg.py:279-291`),
    which de-duplicates again, together with the null-free
    `self.attributes`. The alert returned (`from_db`) and any later GET
    therefore show de-duplicated tags and no null keys.

  Example: a `blackout` alert with a null attribute and a duplicated tag,
  given an unknown action, keeps `major`/`blackout` (ACT-1), and its stored
  and returned state loses the null and the duplicate.
  (Added after Codex raised it as OA-5 on `678f1996`.)
- **A5.3 [V]** The row update sets `severity`, `status`, `previousSeverity`
  (A2.5) and `update_time = now`, even when nothing changed. `tags` becomes
  the union, `attributes` are replaced with the loaded ones, and `timeout` is
  the loaded model's value (A4.1: NULL becomes `ALERT_TIMEOUT`).
- **A5.4 [V]** Success returns **200** `{"status": "ok"}`, without the alert
  (`views.py:187-188`).

### A6. Items for Codex's review

- **A6.1** A1.2 (any status string is accepted) and A1.3 (the NULL timeout)
  look unintended upstream, but they are the reference contract. alrust
  reproduces them unless the owner approves an ADR divergence. alert-behavior
  contract changes go to the owner.
- **A6.2** A0.2 makes action/receive interleaving depend on the source
  clocks. The conformance vectors need a controlled `createTime` relative to
  the reference server's clock (see ALRUST-P0-CLOCK-01 CK-3).

## K. Housekeeping: expiry, deletion, auto-unshelve and auto-unack

`management/views.py` is cited as `mgmt.py`. This is the **data-loss**
surface of the lifecycle: any divergence here is a data-loss contract change
and goes to the owner.

### K0. Trigger and thresholds (`mgmt.py:156-225`, `config.py:86-96`, `settings.py:211-212`)

- **K0.1 [V]** `GET` or `POST /management/housekeeping` requires
  `admin:management` (`mgmt.py:156-158`). The reference does not run it on a
  schedule; an external caller (cron) must invoke it.
- **K0.2 [V]** The optional query parameters `expired` and `info` are in
  **hours**. A missing or zero value falls back to config:
  - `DELETE_EXPIRED_AFTER`, default 2 h (7200 s);
  - `DELETE_INFO_AFTER`, default 12 h (43200 s).

  So `?expired=0` **cannot** disable deletion (`mgmt.py:160-171`). The
  config accepts the aliases `DEFAULT_*_DELETE_HRS` and `HK_*_DELETE_HRS`, in
  hours, and they take precedence (`config.py:86-96`). A config value of `0`
  disables that deletion.
- **K0.3** All three candidate lists are computed **before** any action runs
  (`alert.py:609-614`):
  1. expired (after the deletions, K1);
  2. to-unshelve;
  3. to-unack.

### K1. Deletion: irreversible (`pg.py:1478-1496`)

- **K1.1 [V]** First, every alert whose `status` is `closed` or `expired`
  and whose `last_receive_time` is older than the expired threshold is
  **deleted**.
- **K1.2 [V]** Then every alert whose severity is `informational` (the
  default inform severity) is **deleted when its `last_receive_time` is older
  than the info threshold, whatever its status**. That includes open and
  acked alerts.
- **K1.3** Both run and commit before the expiry SELECT. Deleted ids are
  **not** reported in the response (K4.1).

### K2. Expiry (`pg.py:1498-1506`, then `from_expired`)

- **K2.1 [V]** An alert is selected when:
  - its `status` is not `expired`, **and**
  - `COALESCE(timeout, ALERT_TIMEOUT) != 0`, **and**
  - `last_receive_time + timeout seconds < now (UTC)`.

  This includes `closed`, `ack`, `shelved` and `blackout` alerts.
- **K2.2 [V]** **A NULL `timeout` never expires.** `COALESCE` appears only in
  the `!= 0` test. The interval uses the raw column, so NULL makes the
  comparison NULL. Combined with A1.3, **a `PUT /status` without a `timeout`
  makes that alert immune to expiry** until something writes a non-NULL
  timeout again. That happens on:
  - a receive (dedup or correlate writes the incoming timeout);
  - a `/status` call with a timeout;
  - **any operator action**, which writes back the hydrated `ALERT_TIMEOUT`
    (A4.1).

  The immunity is therefore not permanent. The API still shows a non-null
  timeout throughout (A1.3a).
- **K2.3 [V]** Timeout `0` never expires.
- **K2.4 [V]** The expiry clock is `last_receive_time`, which a duplicate
  refreshes (R5.2). A steady stream of duplicates therefore keeps an alert
  from expiring.
- **K2.5 [V]** Each selected alert gets the `expired` action (A2/A3): EXP-0
  sets the status `expired` and leaves severity unchanged. The history
  records `type: expired` with timeout `self.timeout or ALERT_TIMEOUT`. The
  pre- and post-action plugins run (`mgmt.py:176-194`).

### K3. Auto-unshelve and auto-unack (`pg.py:1508-1534`, then `from_timeout`)

- **K3.1 [V]** An alert is selected for unshelve when:
  - its row status is `shelved`;
  - **any** history entry has `type=shelve` and `status=shelved`;
  - `COALESCE(h.timeout, SHELVE_TIMEOUT) != 0`;
  - `a.update_time + h.timeout seconds < now`.

  Auto-unack works the same way with `ack` and `ACK_TIMEOUT`.
- **K3.2** The clock is the **row's `update_time`**, not the time of the
  shelve or ack. Any later status change moves `update_time` and postpones
  the timeout. The entry is *any* matching entry (DISTINCT ON id), not
  necessarily the latest. A NULL `h.timeout` never fires (same reasoning as
  K2.2).
- **K3.3 [V]** With the default `ACK_TIMEOUT` of 0, an `ack` **without a
  request timeout** records history timeout 0, and that ack never
  auto-unacks. An `ack` with an explicit request timeout (for example
  `timeout: 1`) records it as `h.timeout` (`alert.py:652-653`). That ack
  **does** qualify for auto-unack under the same default config
  (`pg.py:1522-1534`). Auto-unshelve defaults to 7200 s, or uses the request
  timeout.
- **K3.4 [V]** Each selected alert gets the `timeout` action. The rule is
  ACK-0 when the **second-newest history status** (not the ack/shelve
  lookback that A2.4 uses for manual unack) is `ack`, otherwise OPEN-0
  (`open`). A shelved alert that was acked before shelving therefore returns
  to `ack`.
- **K3.5 [V]** Because of K0.3, an alert selected for expiry **and** for
  unshelve or unack is expired first, then gets `timeout`. The TIMEOUT rule
  is checked before the state rules, so it **ends up `open` (or `ack`), not
  `expired`**.

### K4. Response and errors (`mgmt.py:216-225`)

- **K4.1 [V]** Success returns **200** `{status: "ok", expired: [ids],
  unshelve: [ids], unack: [ids], count}`. The ids are those selected before
  processing. **Deletions are not listed.**
- **K4.2** A plugin RejectException on any alert is collected. Processing
  continues, and then the call returns **500** "housekeeping failed" with
  `errors`. Any other exception returns **500** immediately. Alerts already
  processed, and the deletions, **stay committed**.

### K5. Items for Codex and the owner

- **K5.1** K1.2 (informational alerts are deleted whatever their status) and
  K2.2 (NULL timeout means never expires) are the reference contract. alrust
  reproduces them unless the owner approves a data-loss ADR. K1.2 **deletes
  data**, so any change is owner-gated.
- **K5.2** The housekeeping vectors need short times.
  - The query parameters are in hours.
  - `DELETE_EXPIRED_AFTER` and `DELETE_INFO_AFTER` are in **seconds**
    (`config.py:86-96`). An isolated reference instance configured with
    short positive thresholds can therefore test deletion over HTTP without
    DB manipulation (Codex's observation).
  - Synthetic alerts with small `timeout` values cover expiry and timeout.

  Any such run needs its own granted window. The design choice is the
  harness owner's.

## HB. Heartbeats

`models/heartbeat.py` is cited as `hb.py`, and `views/heartbeats.py` as
`hbv.py`.

### HB0. Identity and upsert (`pg.py:851-861`)

- **HB0.1 [V]** A heartbeat's identity is **`(origin, customer)`**
  (`ON CONFLICT (origin, COALESCE(customer, ''))`). A repeat from the same
  origin and customer updates `tags`, `attributes`, `create_time`, `timeout`
  and `receive_time`. The stored **`id` is kept**; an `id` in the request is
  ignored on conflict. `type` is not updated on conflict.
- **HB0.2 [V]** `POST /heartbeat` needs `write:heartbeats`. Customer
  assignment uses `admin:heartbeats` (customer-mapping C11). It returns
  **201** `{status, id, heartbeat}` (`hbv.py:17-40`).

### HB1. Parsing and defaults (`hb.py:27-92`)

- **HB1.1 [V]** `timeout`:
  - it defaults to `HEARTBEAT_TIMEOUT` (= `ALERT_TIMEOUT`, 86400;
    `settings.py:205`);
  - it must be an int and non-negative, as in R0.4, and booleans behave the
    same way;
  - an empty `customer` gives 400; attribute keys containing `.` or `$` give
    400.
- **HB1.2** `origin` defaults to the server's `<argv0>/<hostname>`,
  `createTime` defaults to server now, and `type` defaults to `Heartbeat`.
  `maxLatency` is the config `HEARTBEAT_MAX_LATENCY` (2000 ms;
  `settings.py:206`). It is not stored.

### HB2. Derived fields: recomputed on every read (`hb.py:62-72`)

The model ignores the `latency` and `since` columns the SQL returns
(`from_record`, `hb.py:133-146`), and **recomputes both on the server clock**:
- `latency` = `int((receiveTime - createTime) ms)`;
- `since` = `utcnow - receiveTime`, serialized as **integer seconds**
  (`format.py:41-42`);
- `status`, in order:
  - `expired` when `since > timeout`;
  - otherwise `slow` when `latency > maxLatency`;
  - otherwise `ok`.
- **HB2.1** Because `createTime` comes from the client, a skewed source clock
  shows up as latency and can make a heartbeat `slow` (or give a negative
  latency).

### HB3. Heartbeats sent as alerts (`plugins/heartbeat.py`)

- **HB3.1 [V]** An alert whose event is in `HEARTBEAT_EVENTS` (default
  `Heartbeat`) becomes a heartbeat. This happens after it passes the reject
  policy (R1.2).
  - It copies `origin`, `tags`, `timeout` and the assigned `customer`.
  - Its `attributes` are `{environment, severity, service, group}`.
  - The **alert's `createTime` is not passed**, so the heartbeat's
    `createTime` is server now and its latency is about 0.
  - The response is **202** `{status: "ok", message, id}`, where `id` is the
    heartbeat id. No alert is stored.

### HB4. Reads, deletes and customer scope (`hbv.py:43-110`)

- **HB4.1 [V]** `GET /heartbeats` filters by `customers=g.customers` (as
  C16) and accepts repeated `status=ok|slow|expired`. That status filter is
  evaluated **in SQL** against `NOW() at time zone 'utc'` (`pg.py:886-918`).
  Its boundaries are **not identical** to the serialized `status` (HB2):
  - **Latency:** SQL compares the untruncated
    `EXTRACT(EPOCH …) * 1000 > maxLatency`, while the model truncates
    (`int(ms)`). At a latency of 2000.5 ms with `maxLatency` 2000, the SQL
    `slow` filter selects the heartbeat, but it serializes as
    `latency: 2000`, `status: "ok"`.
  - **Time:** the SQL `since` uses the query's `NOW()`, and the model
    recomputes `utcnow()` later on the app clock. A heartbeat near the
    `timeout` boundary can be filtered as one status and serialized as
    another.
- **HB4.1a [V]** `total` and paging are computed from
  `Heartbeat.count(query)` **before** the status filter is applied
  (`hbv.py:62-66`).
  - When the status-filtered page is **non-empty**, the response `total`
    (and `pages`/`more`) counts every heartbeat the customer/query selects,
    **not** the status-filtered subset.
  - When it is **empty**, the else-branch returns `total: 0` explicitly, with
    `message: "not found"` and `heartbeats: []` (`hbv.py:78-87`).
  - Candidate vector: two heartbeats, one `ok` and one `expired`, then
    `?status=expired` → one heartbeat with `total: 2`.
- **HB4.2 [V]** **Per-id `GET` and `DELETE /heartbeat/<id>` apply no
  customer filter.** They read `g.get('customer')` (singular), which
  `permission()` never sets (`hbv.py:48`, `:96`; compare
  `auth/decorators.py`, which sets `g.customers`). So a customer-scoped key
  can read or delete **any** heartbeat by id or id prefix (`id LIKE
  '<id>%'`, `pg.py:863-872`). This differs from alerts (C17). The same
  pattern appears in `views/blackouts.py:132`. **Follow-up:**
  `spec/customer-mapping.md` should gain a rule for this in its next head.

### HB5. Health check and staleness (`mgmt.py:137-154`)

- **HB5.1 [V]** `GET /management/healthcheck` returns **503**
  `HEARTBEAT_STALE: <origin>` when any heartbeat's `delta.seconds >
  4 × timeout`. It returns 503 `HEALTH_CHECK_FAILED` on any exception, and
  otherwise `OK`.
- **HB5.2 [V]** It uses `timedelta.seconds`, the 0–86399 seconds
  *component*, not `total_seconds()`. A heartbeat stale for **more than a
  day** can therefore read as fresh, depending on the remainder. For
  example, a stale age of 1 day plus 10 s gives `.seconds` = 10.
- **HB5.3** The healthcheck reads **every** heartbeat with no customer
  filter. Any tenant's stale heartbeat makes the whole server unhealthy.

### HB6. What the reference does *not* do

- **HB6.1** The reference server **never turns a stale heartbeat into an
  alert.** Staleness only shows in `status` and in the healthcheck. The
  per-tenant "source went quiet raises an alert" behavior in KICKOFF Phase 5
  is a **new alrust feature**, not a compatibility requirement. Its design
  must not change the HB0–HB5 contract.

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
