//! `transition` against an independent transcription of the reference
//! state machine, on every combination of a fixed input space (item 8 of the
//! 2026-11-10 improvement list: exhaustive engine vectors).
//!
//! `reference` below is written from `reference/alerta/alerta/models/alarms/
//! alerta.py` (`StateMachine.transition`, lines 113-253, with
//! `SEVERITY_MAP`, `ACTION_ALL`, `trend` and the defaults at lines 7-24 and
//! 61-111) without using `alrust-domain`'s constants, so a wrong level, a
//! missing action, a changed default or a reordered rule in the engine or
//! the domain shows up as a difference. Python's `x or default` is kept as
//! "empty or missing means the default".

use alrust_engine::{TransitionError, TransitionInput, transition};

/// `SEVERITY_MAP` (alerta.py:7-21), in its insertion order.
const SEVERITY_MAP: &[(&str, u8)] = &[
    ("security", 0),
    ("critical", 1),
    ("major", 2),
    ("minor", 3),
    ("warning", 4),
    ("indeterminate", 5),
    ("informational", 6),
    ("normal", 7),
    ("ok", 7),
    ("cleared", 7),
    ("debug", 8),
    ("trace", 9),
    ("unknown", 10),
];
/// `ACTION_ALL` (alerta.py:61-71).
const ACTION_ALL: &[&str] = &[
    "open", "assign", "ack", "unack", "shelve", "unshelve", "close", "expired", "timeout",
];
const DEFAULT_STATUS: &str = "open";
const DEFAULT_NORMAL_SEVERITY: &str = "normal";
const DEFAULT_PREVIOUS_SEVERITY: &str = "indeterminate";

fn level(s: &str) -> Option<u8> {
    SEVERITY_MAP.iter().find(|(k, _)| *k == s).map(|(_, v)| *v)
}

/// `trend` (alerta.py:102-111): "moreSevere", "lessSevere" or "noChange".
fn trend(previous: &str, current: &str) -> &'static str {
    match (level(previous), level(current)) {
        (Some(p), Some(c)) if p > c => "moreSevere",
        (Some(p), Some(c)) if p < c => "lessSevere",
        _ => "noChange",
    }
}

#[derive(Debug, PartialEq, Eq)]
enum Outcome {
    Next(String, String, &'static str),
    Invalid(String),
    UnknownSeverity(String),
}

fn or<'a>(v: Option<&'a str>, default: &'a str) -> &'a str {
    v.filter(|s| !s.is_empty()).unwrap_or(default)
}

/// alerta.py:113-253, transcribed.
#[expect(
    clippy::too_many_lines,
    reason = "a transcription of one reference function, kept in one place"
)]
fn reference(i: &TransitionInput<'_>) -> Outcome {
    let current_status = or(i.current_status, DEFAULT_STATUS);
    let previous_status = or(i.previous_status, DEFAULT_STATUS);
    let current_severity = i.severity;
    let previous_severity = or(i.previous_severity, DEFAULT_PREVIOUS_SEVERITY);
    let action = i.action.filter(|a| !a.is_empty());
    let next = |rule, sev: &str, st: &str| Outcome::Next(sev.into(), st.into(), rule);
    let invalid = Outcome::Invalid;

    let Some(current_level) = level(current_severity) else {
        let mut sorted: Vec<&(&str, u8)> = SEVERITY_MAP.iter().collect();
        sorted.sort_by_key(|(_, l)| *l); // stable, as Python's sorted
        let names: Vec<&str> = sorted.iter().map(|(k, _)| *k).collect();
        return Outcome::UnknownSeverity(format!(
            "Severity ({current_severity}) is not one of {}",
            names.join(", ")
        ));
    };
    let normal = current_level == 7;

    if let Some(a) = action
        && !ACTION_ALL.contains(&a)
    {
        return next("ACT-1", current_severity, i.alert_status);
    }
    if action.is_none() && i.alert_status != DEFAULT_STATUS {
        if normal {
            return next("SET-1", DEFAULT_NORMAL_SEVERITY, "closed");
        }
        return next("SET-*", current_severity, i.alert_status);
    }
    let state = current_status;
    if action == Some("unack") {
        return if state == "ack" {
            next("UNACK-1", current_severity, previous_status)
        } else {
            invalid(format!("invalid action for current {state} status"))
        };
    }
    if action == Some("unshelve") {
        return if state == "shelved" {
            next("UNSHL-1", current_severity, previous_status)
        } else {
            invalid(format!("invalid action for current {state} status"))
        };
    }
    if action == Some("expired") {
        return next("EXP-0", current_severity, "expired");
    }
    if action == Some("timeout") {
        return if previous_status == "ack" {
            next("ACK-0", current_severity, "ack")
        } else {
            next("OPEN-0", current_severity, "open")
        };
    }
    if state == "open" {
        match action {
            Some("open") => return invalid(format!("alert is already in {state} status")),
            Some("ack") => return next("OPEN-1", current_severity, "ack"),
            Some("shelve") => return next("OPEN-2", current_severity, "shelved"),
            Some("close") => return next("OPEN-3", DEFAULT_NORMAL_SEVERITY, "closed"),
            _ => {}
        }
    }
    if state == "ack" {
        match action {
            Some("open") => return next("ACK-1", current_severity, "open"),
            Some("ack") => return invalid(format!("alert is already in {state} status")),
            Some("shelve") => return next("ACK-2", current_severity, "shelved"),
            Some("close") => return next("ACK-3", DEFAULT_NORMAL_SEVERITY, "closed"),
            _ => {}
        }
        if previous_severity != DEFAULT_PREVIOUS_SEVERITY
            && trend(previous_severity, current_severity) == "moreSevere"
        {
            return next("ACK-4", current_severity, "open");
        }
    }
    if state == "shelved" {
        match action {
            Some("open") => return next("SHL-1", current_severity, "open"),
            Some("ack") => return invalid(format!("invalid action for current {state} status")),
            Some("shelve") => return invalid(format!("alert is already in {state} status")),
            Some("close") => return next("SHL-2", DEFAULT_NORMAL_SEVERITY, "closed"),
            _ => {}
        }
    }
    if state == "blackout" {
        if action == Some("close") {
            return next("BLK-1", DEFAULT_NORMAL_SEVERITY, "closed");
        }
        return if previous_status == "blackout" {
            next("BLK-*", current_severity, i.alert_status)
        } else {
            next("BLK-2", current_severity, previous_status)
        };
    }
    if state == "closed" {
        match action {
            Some("open") => return next("CLS-1", previous_severity, "open"),
            Some("ack" | "shelve") => {
                return invalid(format!("invalid action for current {state} status"));
            }
            Some("close") => return invalid(format!("alert is already in {state} status")),
            _ => {}
        }
        if !normal {
            return if previous_status == "shelved" {
                next("CLS-2", previous_severity, "shelved")
            } else {
                next("CLS-3", previous_severity, "open")
            };
        }
    }
    if normal {
        return next("CLS-*", DEFAULT_NORMAL_SEVERITY, "closed");
    }
    if state == "expired" {
        if let Some(a) = action
            && a != "open"
        {
            return invalid(format!("invalid action for current {state} status"));
        }
        // alerta.py:250: non-normal here, normal returned above.
        return next("EXP-1", current_severity, "open");
    }
    next("ALL-*", current_severity, current_status)
}

fn engine(i: TransitionInput<'_>) -> Outcome {
    match transition(i) {
        Ok(t) => Outcome::Next(t.severity, t.status, t.rule),
        Err(TransitionError::InvalidAction(m)) => Outcome::Invalid(m),
        Err(e @ TransitionError::UnknownSeverity(_)) => Outcome::UnknownSeverity(e.to_string()),
    }
}

const STATUSES: &[&str] = &[
    "open", "assign", "ack", "shelved", "blackout", "closed", "expired", "unknown",
];

/// Every severity, status, history status and action (each with an unknown
/// value, and the optional ones also missing and empty) against the
/// reference transcription.
#[test]
fn transition_equals_the_reference_on_every_input() {
    let severities: Vec<&str> = SEVERITY_MAP
        .iter()
        .map(|(k, _)| *k)
        .chain(["bogus"])
        .collect();
    let opt = |base: &[&'static str]| -> Vec<Option<&'static str>> {
        [None, Some(""), Some("bogus")]
            .into_iter()
            .chain(base.iter().map(|s| Some(*s)))
            .collect()
    };
    let previous_severities = opt(&SEVERITY_MAP.iter().map(|(k, _)| *k).collect::<Vec<_>>());
    let history_statuses = opt(STATUSES);
    let actions = opt(ACTION_ALL);
    let alert_statuses: Vec<&str> = STATUSES.iter().copied().chain(["bogus"]).collect();

    let mut cases = 0u64;
    let mut rules = std::collections::BTreeSet::new();
    for &severity in &severities {
        for &alert_status in &alert_statuses {
            for &previous_severity in &previous_severities {
                for &current_status in &history_statuses {
                    for &previous_status in &history_statuses {
                        for &action in &actions {
                            let i = TransitionInput {
                                severity,
                                alert_status,
                                previous_severity,
                                current_status,
                                previous_status,
                                action,
                            };
                            let want = reference(&i);
                            let got = engine(i);
                            assert_eq!(got, want, "{i:?}");
                            if let Outcome::Next(_, _, rule) = got {
                                rules.insert(rule);
                            }
                            cases += 1;
                        }
                    }
                }
            }
        }
    }
    assert_eq!(cases, 14 * 9 * 16 * 11 * 11 * 12);
    // Every rule of the reference is reached at least once.
    let all: std::collections::BTreeSet<&str> = [
        "ACT-1", "SET-1", "SET-*", "UNACK-1", "UNSHL-1", "EXP-0", "ACK-0", "OPEN-0", "OPEN-1",
        "OPEN-2", "OPEN-3", "ACK-1", "ACK-2", "ACK-3", "ACK-4", "SHL-1", "SHL-2", "BLK-1", "BLK-2",
        "BLK-*", "CLS-1", "CLS-2", "CLS-3", "CLS-*", "EXP-1", "ALL-*",
    ]
    .into_iter()
    .collect();
    assert_eq!(rules, all);
}
