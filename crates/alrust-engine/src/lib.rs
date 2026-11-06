//! Pure alert lifecycle engine (dedup, correlate, severity/status transitions). No I/O.
//!
//! `transition` is a line-by-line port of the reference `ALERTA` alarm model
//! (`alerta/models/alarms/alerta.py`, `StateMachine.transition`), with the
//! rule names used in `spec/lifecycle.md` R3 and A3.

use alrust_domain::{
    DEFAULT_PREVIOUS_SEVERITY, HistoryEntry, NORMAL_SEVERITY, NORMAL_SEVERITY_LEVEL,
    VALID_SEVERITIES, action, severity_level, status, trend,
};

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TransitionError {
    /// 400 in `transition()`; the receive path wraps it as 500 (spec R1.1).
    UnknownSeverity(String),
    /// `InvalidAction`, answered with 409.
    InvalidAction(String),
}

impl std::fmt::Display for TransitionError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            TransitionError::UnknownSeverity(s) => {
                write!(
                    f,
                    "Severity ({s}) is not one of {}",
                    VALID_SEVERITIES.join(", ")
                )
            }
            TransitionError::InvalidAction(m) => f.write_str(m),
        }
    }
}

impl std::error::Error for TransitionError {}

/// Inputs to `transition`, mirroring the reference arguments.
#[derive(Debug, Clone, Copy)]
pub struct TransitionInput<'a> {
    /// `alert.severity`.
    pub severity: &'a str,
    /// `alert.status`: the incoming status on receive, the row status on actions.
    pub alert_status: &'a str,
    /// `alert.previous_severity`; `None` means the default (`indeterminate`).
    pub previous_severity: Option<&'a str>,
    /// History-derived current status; `None` means `open`.
    pub current_status: Option<&'a str>,
    /// History-derived previous status; `None` means `open`.
    pub previous_status: Option<&'a str>,
    /// The operator action; `None` on receive.
    pub action: Option<&'a str>,
}

/// The result of a transition: the new severity and status, and the rule
/// that fired (for logs and tests).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Transition {
    pub severity: String,
    pub status: String,
    pub rule: &'static str,
}

#[expect(
    clippy::unnecessary_wraps,
    reason = "pairs with `invalid` so every arm of the transition table returns the same type"
)]
fn next(rule: &'static str, severity: &str, status: &str) -> Result<Transition, TransitionError> {
    Ok(Transition {
        severity: severity.to_string(),
        status: status.to_string(),
        rule,
    })
}

fn invalid(msg: String) -> Result<Transition, TransitionError> {
    Err(TransitionError::InvalidAction(msg))
}

/// Decide the next severity and status (and the rule that fired).
///
/// # Errors
///
/// `UnknownSeverity` when a severity is not in `VALID_SEVERITIES`, and
/// `InvalidAction` when the action is not allowed in the current status.
#[expect(
    clippy::too_many_lines,
    reason = "a line-by-line port of the reference state machine, kept in one place"
)]
pub fn transition(i: TransitionInput<'_>) -> Result<Transition, TransitionError> {
    let current_status = i
        .current_status
        .filter(|s| !s.is_empty())
        .unwrap_or(status::OPEN);
    let previous_status = i
        .previous_status
        .filter(|s| !s.is_empty())
        .unwrap_or(status::OPEN);
    let current_severity = i.severity;
    let previous_severity = i
        .previous_severity
        .filter(|s| !s.is_empty())
        .unwrap_or(DEFAULT_PREVIOUS_SEVERITY);

    let Some(level) = severity_level(current_severity) else {
        return Err(TransitionError::UnknownSeverity(
            current_severity.to_string(),
        ));
    };
    let is_normal = level == NORMAL_SEVERITY_LEVEL;
    let action = i.action.filter(|a| !a.is_empty());

    // An unrecognised action: a take_action() plugin handled it.
    if let Some(a) = action
        && !action::ALL.contains(&a)
    {
        return next("ACT-1", current_severity, i.alert_status);
    }

    // A non-default incoming status on receive: a pre_receive() plugin set it.
    if action.is_none() && i.alert_status != status::OPEN {
        if is_normal {
            return next("SET-1", NORMAL_SEVERITY, status::CLOSED);
        }
        return next("SET-*", current_severity, i.alert_status);
    }

    let state = current_status;

    if action == Some(action::UNACK) {
        if state == status::ACK {
            return next("UNACK-1", current_severity, previous_status);
        }
        return invalid(format!("invalid action for current {state} status"));
    }
    if action == Some(action::UNSHELVE) {
        if state == status::SHELVED {
            return next("UNSHL-1", current_severity, previous_status);
        }
        return invalid(format!("invalid action for current {state} status"));
    }
    if action == Some(action::EXPIRED) {
        return next("EXP-0", current_severity, status::EXPIRED);
    }
    if action == Some(action::TIMEOUT) {
        if previous_status == status::ACK {
            return next("ACK-0", current_severity, status::ACK);
        }
        return next("OPEN-0", current_severity, status::OPEN);
    }

    if state == status::OPEN {
        match action {
            Some(action::OPEN) => return invalid(format!("alert is already in {state} status")),
            Some(action::ACK) => return next("OPEN-1", current_severity, status::ACK),
            Some(action::SHELVE) => return next("OPEN-2", current_severity, status::SHELVED),
            Some(action::CLOSE) => return next("OPEN-3", NORMAL_SEVERITY, status::CLOSED),
            _ => {}
        }
    }

    if state == status::ACK {
        match action {
            Some(action::OPEN) => return next("ACK-1", current_severity, status::OPEN),
            Some(action::ACK) => return invalid(format!("alert is already in {state} status")),
            Some(action::SHELVE) => return next("ACK-2", current_severity, status::SHELVED),
            Some(action::CLOSE) => return next("ACK-3", NORMAL_SEVERITY, status::CLOSED),
            _ => {}
        }
        // Re-open ack'ed alerts only if the severity really increases.
        if previous_severity != DEFAULT_PREVIOUS_SEVERITY
            && trend(previous_severity, current_severity) == "moreSevere"
        {
            return next("ACK-4", current_severity, status::OPEN);
        }
    }

    if state == status::SHELVED {
        match action {
            Some(action::OPEN) => return next("SHL-1", current_severity, status::OPEN),
            Some(action::ACK) => {
                return invalid(format!("invalid action for current {state} status"));
            }
            Some(action::SHELVE) => return invalid(format!("alert is already in {state} status")),
            Some(action::CLOSE) => return next("SHL-2", NORMAL_SEVERITY, status::CLOSED),
            _ => {}
        }
    }

    if state == status::BLACKOUT {
        if action == Some(action::CLOSE) {
            return next("BLK-1", NORMAL_SEVERITY, status::CLOSED);
        }
        if previous_status != status::BLACKOUT {
            return next("BLK-2", current_severity, previous_status);
        }
        return next("BLK-*", current_severity, i.alert_status);
    }

    if state == status::CLOSED {
        match action {
            Some(action::OPEN) => return next("CLS-1", previous_severity, status::OPEN),
            Some(action::ACK | action::SHELVE) => {
                return invalid(format!("invalid action for current {state} status"));
            }
            Some(action::CLOSE) => return invalid(format!("alert is already in {state} status")),
            _ => {}
        }
        if !is_normal {
            if previous_status == status::SHELVED {
                return next("CLS-2", previous_severity, status::SHELVED);
            }
            return next("CLS-3", previous_severity, status::OPEN);
        }
    }

    // Auto-close normal severity alerts from any state.
    if is_normal {
        return next("CLS-*", NORMAL_SEVERITY, status::CLOSED);
    }

    if state == status::EXPIRED {
        if let Some(a) = action
            && a != action::OPEN
        {
            return invalid(format!("invalid action for current {state} status"));
        }
        return next("EXP-1", current_severity, status::OPEN);
    }

    next("ALL-*", current_severity, current_status)
}

/// What `_get_hist_info` derives from history (spec R7.3, A2.4).
#[derive(Debug, Clone, Default, PartialEq)]
pub struct HistInfo {
    pub current_status: Option<String>,
    /// `None` without history; `Some(None)` for a NULL value.
    pub current_value: Option<Option<String>>,
    pub previous_status: Option<String>,
    pub previous_timeout: Option<i64>,
}

/// Derive the current/previous status from history entries.
///
/// `entries` must already be filtered to the alert identity (spec R7.1) and
/// in storage order (newest first); this sorts them by `updateTime`
/// descending, stably, as the reference SQL does (R7.2).
pub fn hist_info(entries: &[&HistoryEntry], action: Option<&str>) -> HistInfo {
    let mut h: Vec<&HistoryEntry> = entries.to_vec();
    h.sort_by_key(|e| std::cmp::Reverse(e.update_time));
    let Some(first) = h.first() else {
        return HistInfo::default();
    };
    let mut info = HistInfo {
        current_status: first.status.clone(),
        current_value: Some(first.value.clone()),
        ..HistInfo::default()
    };
    if h.len() == 1 {
        return info;
    }
    let find = match action {
        Some(action::UNACK) => Some("ack"),
        Some(action::UNSHELVE) => Some("shelve"),
        _ => None,
    };
    if let Some(find) = find {
        for pair in h.windows(2) {
            if pair[0].change_type == find {
                info.previous_status.clone_from(&pair[1].status);
                info.previous_timeout = pair[1].timeout;
                return info;
            }
        }
        // The entry was evicted by HISTORY_LIMIT: fall back to open.
        info.previous_status = Some(status::OPEN.to_string());
        return info;
    }
    info.previous_status.clone_from(&h[1].status);
    info.previous_timeout = h[1].timeout;
    info
}

/// Timeout configuration used by actions (spec A4).
#[derive(Debug, Clone, Copy)]
pub struct Timeouts {
    pub alert: i64,
    pub ack: i64,
    pub shelve: i64,
}

/// The history timeout for an action (spec A4). `0` is falsy (A4.2).
pub fn action_timeout(
    action: &str,
    request_timeout: Option<i64>,
    previous_timeout: Option<i64>,
    row_timeout: Option<i64>,
    t: Timeouts,
) -> i64 {
    let truthy = |v: Option<i64>| v.filter(|x| *x != 0);
    let mut timeout = request_timeout;
    if matches!(action, action::UNACK | action::UNSHELVE | action::TIMEOUT) {
        timeout = truthy(timeout).or(previous_timeout);
    }
    match action {
        action::ACK | action::UNACK => truthy(timeout).unwrap_or(t.ack),
        action::SHELVE | action::UNSHELVE => truthy(timeout).unwrap_or(t.shelve),
        _ => truthy(timeout).or(truthy(row_timeout)).unwrap_or(t.alert),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rx<'a>(
        severity: &'a str,
        incoming: &'a str,
        cur: Option<&'a str>,
        prev: Option<&'a str>,
    ) -> TransitionInput<'a> {
        TransitionInput {
            severity,
            alert_status: incoming,
            previous_severity: None,
            current_status: cur,
            previous_status: prev,
            action: None,
        }
    }

    fn act<'a>(
        severity: &'a str,
        row: &'a str,
        cur: &'a str,
        prev: Option<&'a str>,
        a: &'a str,
    ) -> TransitionInput<'a> {
        TransitionInput {
            severity,
            alert_status: row,
            previous_severity: None,
            current_status: Some(cur),
            previous_status: prev,
            action: Some(a),
        }
    }

    #[test]
    fn create_open_and_auto_close() {
        assert_eq!(
            transition(rx("major", "open", None, None)).unwrap().status,
            "open"
        );
        let t = transition(rx("ok", "open", None, None)).unwrap();
        assert_eq!(
            (t.severity.as_str(), t.status.as_str(), t.rule),
            ("normal", "closed", "CLS-*")
        );
    }

    #[test]
    fn client_status_wins_on_receive() {
        let t = transition(rx("major", "ack", None, None)).unwrap();
        assert_eq!((t.status.as_str(), t.rule), ("ack", "SET-*"));
        assert_eq!(
            transition(rx("ok", "ack", None, None)).unwrap().rule,
            "SET-1"
        );
    }

    #[test]
    fn closed_alert_reopens_on_non_normal() {
        let t = transition(rx("major", "open", Some("closed"), Some("open"))).unwrap();
        assert_eq!((t.status.as_str(), t.rule), ("open", "CLS-3"));
        let t = transition(rx("major", "open", Some("closed"), Some("shelved"))).unwrap();
        assert_eq!((t.status.as_str(), t.rule), ("shelved", "CLS-2"));
    }

    #[test]
    fn shelved_stays_shelved_on_receive() {
        let t = transition(rx("major", "open", Some("shelved"), None)).unwrap();
        assert_eq!((t.status.as_str(), t.rule), ("shelved", "ALL-*"));
    }

    #[test]
    fn ack4_needs_real_previous_severity() {
        let mut i = rx("critical", "open", Some("ack"), None);
        assert_eq!(transition(i).unwrap().status, "ack");
        i.previous_severity = Some("major");
        let t = transition(i).unwrap();
        assert_eq!((t.status.as_str(), t.rule), ("open", "ACK-4"));
    }

    #[test]
    fn actions() {
        assert_eq!(
            transition(act("major", "open", "open", None, "ack"))
                .unwrap()
                .status,
            "ack"
        );
        assert!(matches!(
            transition(act("major", "open", "open", None, "open")),
            Err(TransitionError::InvalidAction(_))
        ));
        let t = transition(act("major", "ack", "ack", None, "close")).unwrap();
        assert_eq!(
            (t.severity.as_str(), t.status.as_str()),
            ("normal", "closed")
        );
        let t = transition(act("major", "ack", "ack", Some("shelved"), "unack")).unwrap();
        assert_eq!(t.status, "shelved");
        assert_eq!(
            transition(act("major", "open", "open", None, "frobnicate"))
                .unwrap()
                .rule,
            "ACT-1"
        );
        assert_eq!(
            transition(act("major", "open", "open", None, "assign"))
                .unwrap()
                .status,
            "open"
        );
    }

    #[test]
    fn closed_open_restores_previous_severity() {
        let mut i = act("normal", "closed", "closed", None, "open");
        i.previous_severity = Some("major");
        let t = transition(i).unwrap();
        assert_eq!(
            (t.severity.as_str(), t.status.as_str(), t.rule),
            ("major", "open", "CLS-1")
        );
    }

    #[test]
    fn unknown_severity_is_an_error() {
        assert!(matches!(
            transition(rx("bogus", "open", None, None)),
            Err(TransitionError::UnknownSeverity(_))
        ));
    }

    #[test]
    fn timeouts() {
        let t = Timeouts {
            alert: 86400,
            ack: 0,
            shelve: 7200,
        };
        assert_eq!(action_timeout("ack", None, None, Some(100), t), 0);
        assert_eq!(action_timeout("ack", Some(5), None, Some(100), t), 5);
        assert_eq!(action_timeout("shelve", None, None, Some(100), t), 7200);
        assert_eq!(action_timeout("close", None, None, Some(100), t), 100);
        assert_eq!(action_timeout("close", Some(0), None, None, t), 86400);
        assert_eq!(action_timeout("unshelve", None, Some(42), None, t), 42);
    }
}
