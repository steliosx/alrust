//! The log output boundary for hook spec section 4.2's diagnostics rule:
//! every formatted record, from alrust or from a dependency (for example
//! sqlx's `sqlx::postgres::notice` warnings, which carry a PostgreSQL
//! `NOTICE` or `WARNING` raised by a trigger), is written whole, or replaced
//! by `REDACTED_DIAGNOSTIC` when it may carry restricted evidence (after
//! Zabalack's `CaptureLogRedactor`,
//! `zab-al-ack@eaf2acf:src/zabalack/plugins.py:30-46`).

use std::io::{self, Write};

use alrust_domain::{REDACTED_DIAGNOSTIC, contains_evidence};
use tracing_subscriber::fmt::MakeWriter;

/// The log subscriber alrust runs with, writing through `make_writer`.
pub(crate) fn subscriber<W>(filter: &str, make_writer: W) -> impl tracing::Subscriber + Send + Sync
where
    W: for<'a> MakeWriter<'a> + Send + Sync + 'static,
{
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env().unwrap_or_else(|_| filter.into()),
        )
        .with_writer(Redacting(make_writer))
        .finish()
}

/// Wraps a `MakeWriter`: each record is held until its writer is dropped
/// (the formatter makes one writer per record), then checked and written.
#[derive(Debug)]
pub(crate) struct Redacting<M>(pub(crate) M);

impl<'a, M: MakeWriter<'a>> MakeWriter<'a> for Redacting<M> {
    type Writer = RecordWriter<M::Writer>;

    fn make_writer(&'a self) -> Self::Writer {
        RecordWriter {
            inner: self.0.make_writer(),
            record: Vec::new(),
        }
    }
}

#[derive(Debug)]
pub(crate) struct RecordWriter<W: Write> {
    inner: W,
    record: Vec<u8>,
}

impl<W: Write> Write for RecordWriter<W> {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        self.record.extend_from_slice(buf);
        Ok(buf.len())
    }

    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

impl<W: Write> Drop for RecordWriter<W> {
    fn drop(&mut self) {
        let text = String::from_utf8_lossy(&self.record);
        // Both forms: the raw text (an escape-like byte sequence must not
        // hide a literal marker) and the text without escapes (colouring
        // must not split one).
        let out = if contains_evidence(&text) || contains_evidence(&without_ansi(&text)) {
            format!("{REDACTED_DIAGNOSTIC}\n").into_bytes()
        } else {
            std::mem::take(&mut self.record)
        };
        // A log write that fails has nowhere to report to.
        let _ = self.inner.write_all(&out);
        let _ = self.inner.flush();
    }
}

/// `text` without ANSI escape sequences (`ESC [` parameters, then a final
/// letter), so terminal colouring cannot split the text being checked.
fn without_ansi(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut chars = text.chars().peekable();
    while let Some(c) = chars.next() {
        if c == '\u{1b}' && chars.peek() == Some(&'[') {
            chars.next();
            for d in chars.by_ref() {
                if d.is_ascii_alphabetic() {
                    break;
                }
            }
        } else {
            out.push(c);
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use std::sync::{Arc, Mutex};

    use super::*;

    #[derive(Clone, Debug, Default)]
    struct Captured(Arc<Mutex<Vec<u8>>>);

    impl Write for Captured {
        fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
            self.0.lock().expect("test lock").extend_from_slice(buf);
            Ok(buf.len())
        }

        fn flush(&mut self) -> io::Result<()> {
            Ok(())
        }
    }

    impl<'a> MakeWriter<'a> for Captured {
        type Writer = Captured;

        fn make_writer(&'a self) -> Self::Writer {
            self.clone()
        }
    }

    fn lines(c: &Captured) -> Vec<String> {
        let bytes = c.0.lock().expect("test lock").clone();
        String::from_utf8(bytes)
            .expect("utf-8 log")
            .lines()
            .map(str::to_owned)
            .collect()
    }

    // Codex's PR176-LOG-BOUNDARY-1 counterexamples, through the subscriber
    // main uses, with its default filter: a trigger name logged at start-up
    // and a driver notice at sqlx's WARN target, each carrying evidence;
    // ordinary records are written whole.
    #[test]
    fn records_carrying_evidence_are_redacted_at_the_output() {
        let out = Captured::default();
        let sub = subscriber("info,sqlx=warn", out.clone());
        tracing::subscriber::with_default(sub, || {
            tracing::info!(trigger = %"[ackline:synthetic]", "trigger on a reference table");
            tracing::warn!(
                target: "sqlx::postgres::notice",
                message = "capture refused for [ZABALACK:v1 action=a kid=k mac=00]"
            );
            tracing::info!(column = "zabbix_ack_sync_intent", "ordinary text naming it");
            tracing::info!(trigger = %"zabalack_capture", "trigger on a reference table");
            tracing::warn!(target: "sqlx::postgres::notice", message = "relation exists, skipping");
        });
        let got = lines(&out);
        assert_eq!(got.len(), 5, "{got:?}");
        for line in &got[..3] {
            assert_eq!(line, REDACTED_DIAGNOSTIC);
        }
        assert!(
            got[3].contains("zabalack_capture") && got[3].contains("trigger on a reference table")
        );
        assert!(got[4].contains("relation exists, skipping"));
    }

    // Codex's PR176-LOG-RAW-CHECK-1: stripping "ESC [" up to the first letter
    // eats the marker's own "[z", so the raw text must be checked too.
    #[test]
    fn an_escape_before_a_marker_cannot_hide_it() {
        let out = Captured::default();
        let sub = subscriber("info,sqlx=warn", out.clone());
        tracing::subscriber::with_default(sub, || {
            tracing::info!(
                trigger = %"\u{1b}[[zabalack:v1 action=synthetic kid=k mac=00]",
                "trigger on a reference table"
            );
        });
        let got = lines(&out);
        assert_eq!(got, [REDACTED_DIAGNOSTIC]);
        let all = got.concat();
        assert!(!all.contains("mac=00") && !all.contains("synthetic"));
    }

    #[test]
    fn colouring_cannot_split_the_evidence() {
        assert_eq!(without_ansi("\u{1b}[1m[zaba\u{1b}[0mlack:"), "[zabalack:");
        assert!(contains_evidence(&without_ansi(
            "x \u{1b}[2m[ackline:\u{1b}[0m y"
        )));
    }
}
