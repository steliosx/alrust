# Operator status/action vectors

Author: codex-alrust. These five HTTP tests are **SOURCE_DERIVED / NOT_RUN**.
They cite reference `f4312727783dad64198b62ed778bbca98a5144a3` and independently
reviewed action specification `cedfcdff5afb8198bfb3588836156ca8510d076c`.
The suite imports neither implementation. An offline pass or collection does
not validate these reference behaviors.

| Vector | Specification | Observable expectation |
|---|---|---|
| Missing status/action | A1.1, A2.1 | Missing/empty field returns400; alert unchanged |
| Arbitrary non-enum status | A1.2–A1.5 | Status stored without severity transition; status history appended |
| Unknown action | A3 ACT-1, A5 | Returns200 and appends action history while retaining state |
| Close/reopen | A3.3, A5 | Close stores normal/closed and previous major; reopen restores major/open |
| Ack/unack/conflict | A2.3–A2.4, A4–A5 | Ack history timeout0 with row timeout86400; redundant ack409 without mutation; unack restores open/86400 |

Each test uses a unique labeled synthetic resource, the existing private
synthetic fixture loader and its unscoped admin key. The disposable server must
be configured with AUTH_REQUIRED=False, CUSTOMER_VIEWS=False, the default
Alerta alarm model/plugins, ALERT_TIMEOUT=86400, ACK_TIMEOUT=0 and no concurrent
housekeeping. Stored fixture records must be attested by a separately reviewed
runner before use. No real keys/configuration or production server belongs to
this scope. Test cleanup deletes only the created alert ID; the runner separately
owns service/database cleanup. Source failure remains visible rather than
weakening expected behavior.

The initial client createTime is fixed at 2000-01-01 UTC; the test verifies the
server receiveTime is later. This gives action history a defined position after
the initial receive entry without assuming matching client/server clocks.
Sequential action writes are separated by50ms and must have strictly increasing
raw history timestamps. Each successful write preserves all preceding history
entries, compares row updateTime to the newly appended history timestamp, and
checks observable status/severity. **No timestamp is normalized.** Clock jumps
or ties fail visibly; this scope does not claim to cover clock skew, ties,
receive/action interleaving, future receiveTime or A0.2.

HTTP timeout86400 after `/status` without timeout tests hydration only. It
does not prove the raw nullable database column or housekeeping behavior.
Shelving, blackout, expired/timeout actions, truncated history, plugin rewrites,
broader auth/permissions and differential two-server coverage remain NOT_RUN.

Before execution Claude must review this exact new head, plus a runner with
its exact command/resources/stop conditions. A fresh signed window is required.
The released customer-only window does not authorize these tests. Reference
customer filtering remains preserved, and the four customer vectors' earlier
LOCAL_PASS at `a7426411181f4dd3cc859581cc203ee4067c07b4` does not carry approval
or runtime evidence forward to this new head. Main/hosted gates remain open.
