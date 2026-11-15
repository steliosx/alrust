//! `GET /management/properties`, redacted (`spec/security-fixes.md` SF-MGMT
//! alrust item 1; DECISIONS Y8).
//!
//! The reference dumps the request environ, the process environment, the
//! app object and the whole config (`management/views.py:82-100`). alrust
//! shows one `NAME: VALUE` line per setting of its own fixed schema, sorted
//! by name, and nothing else. Each setting has a [`Class`]:
//! - public: the value as JSON, unless an emitted string contains a secret
//!   value; only booleans, integers, strings and lists of strings, and never
//!   under a name the spec reserves for secrets (such a setting is treated
//!   as secret);
//! - connection: `scheme://` + `[REDACTED]@` for any user information +
//!   host + `:port` + `/[REDACTED]` for any path other than empty or `/`,
//!   or any query or fragment; `[REDACTED]` when the value is not an RFC 3986
//!   URI with a scheme and a non-empty host, or when a shown part contains a
//!   secret value; `null` when unset;
//! - secret: always `[REDACTED]`, also when unset or empty.
//!
//! The secret values are every non-empty source string of a secret setting,
//! and every non-empty user information and password of a connection
//! setting, as written and percent-decoded. They are compared as raw text,
//! before any escaping. The page is plain ASCII: JSON strings escape every
//! control character and every non-ASCII character (`\uXXXX`), so no value
//! can start a line, also for readers that break lines at U+0085, U+2028
//! or U+2029.

use std::fmt;

/// The text of a hidden value.
pub const REDACTED: &str = "[REDACTED]";

/// How a setting is shown (Y8).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Class {
    Public,
    Connection,
    Secret,
}

/// A setting's effective value.
#[derive(Clone, PartialEq, Eq)]
pub enum SettingValue {
    Null,
    Bool(bool),
    Int(i64),
    Str(String),
    List(Vec<String>),
}

/// Names the variant only: a connection value can hold a password.
impl fmt::Debug for SettingValue {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            SettingValue::Null => "Null",
            SettingValue::Bool(_) => "Bool(..)",
            SettingValue::Int(_) => "Int(..)",
            SettingValue::Str(_) => "Str(..)",
            SettingValue::List(_) => "List(..)",
        })
    }
}

/// One line of the page.
#[derive(Clone, PartialEq, Eq)]
pub struct Setting {
    /// `[A-Z0-9_]+`, from the server's fixed schema.
    pub name: &'static str,
    pub class: Class,
    pub value: SettingValue,
    /// Every configured source string of a secret or connection setting
    /// (each environment variable, the generated signing key), for the
    /// secret-value check; not only the effective one.
    pub sources: Vec<String>,
}

impl fmt::Debug for Setting {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let shown = self.class == Class::Public && may_be_public(self.name);
        let mut d = f.debug_struct("Setting");
        d.field("name", &self.name).field("class", &self.class);
        if shown {
            d.field("value", &render_public_unchecked(&self.value));
        } else {
            d.field("value", &REDACTED);
        }
        d.field("sources", &self.sources.len()).finish()
    }
}

impl Setting {
    /// A public setting.
    pub fn public(name: &'static str, value: SettingValue) -> Self {
        Setting {
            name,
            class: Class::Public,
            value,
            sources: Vec::new(),
        }
    }

    /// A connection setting: its effective value (`None`: unset) and every
    /// configured source.
    pub fn connection(name: &'static str, value: Option<String>, sources: Vec<String>) -> Self {
        Setting {
            name,
            class: Class::Connection,
            value: value.map_or(SettingValue::Null, SettingValue::Str),
            sources,
        }
    }

    /// A secret setting and every configured source.
    pub fn secret(name: &'static str, sources: Vec<String>) -> Self {
        Setting {
            name,
            class: Class::Secret,
            value: SettingValue::Null,
            sources,
        }
    }
}

/// Words a public setting's name must not contain (Y8).
const RESERVED_WORDS: [&str; 6] = [
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "TOKEN",
    "CREDENTIAL",
    "PRIVATE",
];

/// Whether a setting of this name may be public: none of the reserved
/// words, no `_KEY` or `_KEYS` ending, except the one reviewed name.
pub fn may_be_public(name: &str) -> bool {
    if name == "TOKEN_EXPIRE_DAYS" {
        return true;
    }
    let upper = name.to_ascii_uppercase();
    !RESERVED_WORDS.iter().any(|w| upper.contains(w))
        && !upper.ends_with("_KEY")
        && !upper.ends_with("_KEYS")
}

fn hex(b: u8) -> Option<u8> {
    match b {
        b'0'..=b'9' => Some(b - b'0'),
        b'a'..=b'f' => Some(b - b'a' + 10),
        b'A'..=b'F' => Some(b - b'A' + 10),
        _ => None,
    }
}

/// `%hh` decoded; any other `%` is kept. Invalid UTF-8 becomes U+FFFD.
pub fn percent_decode(text: &str) -> String {
    let bytes = text.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%'
            && let (Some(high), Some(low)) = (
                bytes.get(i + 1).copied().and_then(hex),
                bytes.get(i + 2).copied().and_then(hex),
            )
        {
            out.push(high * 16 + low);
            i += 3;
            continue;
        }
        out.push(bytes[i]);
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

/// RFC 3986 `reg-name` characters: `unreserved / sub-delims` or a valid
/// `pct-encoded` triple, throughout `text`.
fn reg_name_chars(text: &str) -> bool {
    valid_chars(text, b"")
}

/// As [`reg_name_chars`], and `:`: user information and an IP literal.
fn colon_chars(text: &str) -> bool {
    valid_chars(text, b":")
}

fn valid_chars(text: &str, extra: &[u8]) -> bool {
    let bytes = text.as_bytes();
    let mut i = 0;
    while i < bytes.len() {
        let c = bytes[i];
        if c == b'%' {
            if bytes.get(i + 1).copied().and_then(hex).is_none()
                || bytes.get(i + 2).copied().and_then(hex).is_none()
            {
                return false;
            }
            i += 3;
            continue;
        }
        let ok = c.is_ascii_alphanumeric() || b"-._~!$&'()*+,;=".contains(&c) || extra.contains(&c);
        if !ok {
            return false;
        }
        i += 1;
    }
    true
}

/// The parts of a URI that the page may show.
#[derive(Debug, Clone, PartialEq, Eq)]
struct Uri<'a> {
    scheme: &'a str,
    userinfo: Option<&'a str>,
    host: &'a str,
    port: Option<&'a str>,
    /// A path other than empty or `/`, a query or a fragment.
    more: bool,
}

/// RFC 3986 generic syntax (appendix B split, then the authority's
/// grammar): `None` without a scheme or a non-empty host, or when a part
/// is not valid.
fn split_uri(s: &str) -> Option<Uri<'_>> {
    let colon = s.find([':', '/', '?', '#'])?;
    if !s[colon..].starts_with(':') {
        return None;
    }
    let scheme = &s[..colon];
    let mut sc = scheme.bytes();
    if !sc.next().is_some_and(|c| c.is_ascii_alphabetic())
        || !sc.all(|c| c.is_ascii_alphanumeric() || matches!(c, b'+' | b'-' | b'.'))
    {
        return None;
    }
    let rest = s[colon + 1..].strip_prefix("//")?;
    let end = rest.find(['/', '?', '#']).unwrap_or(rest.len());
    let (authority, tail) = rest.split_at(end);
    let (userinfo, hostport) = match authority.split_once('@') {
        Some((u, h)) => (Some(u), h),
        None => (None, authority),
    };
    if userinfo.is_some_and(|u| !colon_chars(u)) || hostport.contains('@') {
        return None;
    }
    let (host, port) = if hostport.starts_with('[') {
        let close = hostport.find(']')?;
        let (host, after) = hostport.split_at(close + 1);
        let inner = &host[1..host.len() - 1];
        if inner.is_empty() || !colon_chars(inner) {
            return None;
        }
        match after {
            "" => (host, None),
            p => (host, Some(p.strip_prefix(':')?)),
        }
    } else {
        match hostport.split_once(':') {
            Some((h, p)) => (h, Some(p)),
            None => (hostport, None),
        }
    };
    if host.is_empty() || (!host.starts_with('[') && !reg_name_chars(host)) {
        return None;
    }
    let port = port.filter(|p| !p.is_empty());
    if port.is_some_and(|p| !p.bytes().all(|c| c.is_ascii_digit())) {
        return None;
    }
    let path_end = tail.find(['?', '#']).unwrap_or(tail.len());
    let more = !matches!(&tail[..path_end], "" | "/") || path_end < tail.len();
    Some(Uri {
        scheme,
        userinfo,
        host,
        port,
        more,
    })
}

/// The lenient user information of a source that is not a valid URI: the
/// authority's text before its last `@`, so a malformed value's password
/// still counts as a secret value.
fn loose_userinfo(s: &str) -> Option<&str> {
    let after = &s[s.find("//")? + 2..];
    let authority = &after[..after.find(['/', '?', '#']).unwrap_or(after.len())];
    authority.rsplit_once('@').map(|(u, _)| u)
}

/// The secret values of all settings: non-empty, each once.
pub fn secret_values(settings: &[Setting]) -> Vec<String> {
    let mut out: Vec<String> = Vec::new();
    let mut add = |v: &str| {
        if !v.is_empty() && !out.iter().any(|o| o == v) {
            out.push(v.to_owned());
        }
    };
    for s in settings {
        match s.class {
            Class::Secret => {
                for v in &s.sources {
                    add(v);
                }
            }
            Class::Connection => {
                for source in &s.sources {
                    let userinfo = split_uri(source)
                        .and_then(|u| u.userinfo)
                        .or_else(|| loose_userinfo(source));
                    if let Some(u) = userinfo {
                        let password = u.split_once(':').map_or("", |(_, p)| p);
                        for v in [u, password] {
                            add(v);
                            add(&percent_decode(v));
                        }
                    }
                }
            }
            Class::Public => {}
        }
    }
    out
}

/// A JSON string in plain ASCII: `"` and `\` escaped, every control
/// character and every non-ASCII character as `\uXXXX` (UTF-16).
pub fn json_ascii(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 2);
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            ' '..='~' => out.push(c),
            _ => {
                let mut units = [0u16; 2];
                for unit in c.encode_utf16(&mut units).iter() {
                    out.push_str("\\u");
                    for shift in [12, 8, 4, 0] {
                        let digit = u32::from((*unit >> shift) & 0xf);
                        out.push(char::from_digit(digit, 16).unwrap_or('0'));
                    }
                }
            }
        }
    }
    out.push('"');
    out
}

fn json_list(items: &[String]) -> String {
    let parts: Vec<String> = items.iter().map(|i| json_ascii(i)).collect();
    format!("[{}]", parts.join(","))
}

fn render_public_unchecked(value: &SettingValue) -> String {
    match value {
        SettingValue::Null => "null".to_owned(),
        SettingValue::Bool(b) => b.to_string(),
        SettingValue::Int(i) => i.to_string(),
        SettingValue::Str(s) => json_ascii(s),
        SettingValue::List(l) => json_list(l),
    }
}

fn contains_secret(text: &str, secrets: &[String]) -> bool {
    secrets.iter().any(|s| text.contains(s.as_str()))
}

fn public_value(value: &SettingValue, secrets: &[String]) -> String {
    let hit = match value {
        SettingValue::Null => false,
        SettingValue::Bool(b) => contains_secret(&b.to_string(), secrets),
        SettingValue::Int(i) => contains_secret(&i.to_string(), secrets),
        SettingValue::Str(s) => contains_secret(s, secrets),
        SettingValue::List(l) => l.iter().any(|i| contains_secret(i, secrets)),
    };
    if hit {
        REDACTED.to_owned()
    } else {
        render_public_unchecked(value)
    }
}

fn connection_value(value: &SettingValue, secrets: &[String]) -> String {
    let SettingValue::Str(s) = value else {
        return if *value == SettingValue::Null {
            "null".to_owned()
        } else {
            REDACTED.to_owned()
        };
    };
    let Some(u) = split_uri(s) else {
        return REDACTED.to_owned();
    };
    let shown = [
        u.scheme,
        u.host,
        &percent_decode(u.host),
        u.port.unwrap_or(""),
    ];
    if shown.iter().any(|p| contains_secret(p, secrets)) {
        return REDACTED.to_owned();
    }
    let mut text = format!("{}://", u.scheme);
    if u.userinfo.is_some() {
        text.push_str(REDACTED);
        text.push('@');
    }
    text.push_str(u.host);
    if let Some(p) = u.port {
        text.push(':');
        text.push_str(p);
    }
    if u.more {
        text.push('/');
        text.push_str(REDACTED);
    }
    json_ascii(&text)
}

/// The page: one `NAME: VALUE` line per setting, sorted by name, each
/// ending with a newline.
pub fn render(settings: &[Setting]) -> String {
    let secrets = secret_values(settings);
    let mut lines: Vec<(&'static str, String)> = settings
        .iter()
        .map(|s| {
            let class = if s.class == Class::Public && !may_be_public(s.name) {
                Class::Secret
            } else {
                s.class
            };
            let value = match class {
                Class::Public => public_value(&s.value, &secrets),
                Class::Connection => connection_value(&s.value, &secrets),
                Class::Secret => REDACTED.to_owned(),
            };
            (s.name, value)
        })
        .collect();
    lines.sort_by_key(|l| l.0);
    let mut out = String::new();
    for (name, value) in lines {
        out.push_str(name);
        out.push_str(": ");
        out.push_str(&value);
        out.push('\n');
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn s(v: &str) -> SettingValue {
        SettingValue::Str(v.to_owned())
    }

    fn line<'a>(page: &'a str, name: &str) -> &'a str {
        let prefix = format!("{name}: ");
        page.lines()
            .find_map(|l| l.strip_prefix(prefix.as_str()))
            .unwrap_or_else(|| panic!("no {name} in {page}"))
    }

    fn connection(url: &str) -> String {
        render(&[Setting::connection(
            "X_URL",
            Some(url.to_owned()),
            vec![url.to_owned()],
        )])
    }

    /// Every line is `NAME: VALUE` with a value the page may emit.
    fn assert_well_formed(page: &str) {
        assert!(page.is_ascii(), "{page}");
        assert!(page.ends_with('\n'));
        for l in page.lines() {
            let (name, value) = l.split_once(": ").unwrap();
            assert!(
                !name.is_empty()
                    && name
                        .bytes()
                        .all(|c| c.is_ascii_uppercase() || c.is_ascii_digit() || c == b'_'),
                "{l}"
            );
            let ok = value == REDACTED
                || value == "null"
                || value == "true"
                || value == "false"
                || value.parse::<i64>().is_ok()
                || (value.starts_with('"') && value.ends_with('"') && value.len() >= 2)
                || (value.starts_with('[') && value.ends_with(']'));
            assert!(ok, "{l}");
            if value != REDACTED && (value.starts_with('"') || value.starts_with('[')) {
                serde_json::from_str::<serde_json::Value>(value).unwrap();
            }
        }
    }

    /// Vector 2: user information and anything after the authority hidden.
    #[test]
    fn connections_show_scheme_host_and_port() {
        for (url, shown) in [
            (
                "postgres://alrust:SENTINEL-db-0001@db:5432/alrust",
                "\"postgres://[REDACTED]@db:5432/[REDACTED]\"",
            ),
            (
                "redis://:SENTINEL-celery-0001@cache.test:6379/0",
                "\"redis://[REDACTED]@cache.test:6379/[REDACTED]\"",
            ),
            ("http://localhost:8080", "\"http://localhost:8080\""),
            ("http://localhost:8080/", "\"http://localhost:8080\""),
            ("https://a.test/api", "\"https://a.test/[REDACTED]\""),
            ("https://a.test?x=1", "\"https://a.test/[REDACTED]\""),
            ("https://a.test#f", "\"https://a.test/[REDACTED]\""),
            ("http://@a.test", "\"http://[REDACTED]@a.test\""),
            ("http://[::1]:80/x", "\"http://[::1]:80/[REDACTED]\""),
            ("http://a.test:", "\"http://a.test\""),
        ] {
            assert_eq!(line(&connection(url), "X_URL"), shown, "{url}");
        }
    }

    /// Vector 4 and other values that are not a URI with a host.
    #[test]
    fn a_value_that_is_not_a_uri_with_a_host_is_redacted() {
        for url in [
            "not a url",
            "redis://",
            "redis:x",
            "//host",
            "1http://a.test",
            "http://a.test:80x",
            "http://u@v@a.test",
            "http://a test",
            "http://[]",
            "http://a.test%zz",
        ] {
            assert_eq!(line(&connection(url), "X_URL"), REDACTED, "{url}");
        }
        let unset = render(&[Setting::connection("X_URL", None, vec![])]);
        assert_eq!(line(&unset, "X_URL"), "null");
    }

    /// Vectors 1, 5, 5b, 5c, 5d and 7: secret values hide every public or
    /// connection value that contains one; empty is never a secret value.
    #[test]
    fn values_holding_a_secret_are_redacted() {
        let admin = "SYNTHETIC\"pass\\word\tX";
        let page = render(&[
            Setting::secret("SECRET_KEY", vec!["secret-host-SYNTHETIC.test".into()]),
            Setting::secret("ALRUST_ADMIN_KEY", vec![admin.into()]),
            Setting::secret("EMPTY_KEY", vec![String::new()]),
            Setting::connection(
                "DATABASE_URL",
                Some("postgres://alrust:p%40ss-SYNTHETIC-0001@db:5432/x".into()),
                vec!["postgres://alrust:p%40ss-SYNTHETIC-0001@db:5432/x".into()],
            ),
            Setting::connection(
                "BASE_URL",
                Some("http://secret-host-SYNTHETIC.test:8080".into()),
                vec!["http://secret-host-SYNTHETIC.test:8080".into()],
            ),
            Setting::public(
                "ALLOWED_ENVIRONMENTS",
                SettingValue::List(vec!["Production".into(), format!("x{admin}y")]),
            ),
            Setting::public("AUTH_PROVIDER", s(&format!("before {admin} after"))),
            Setting::public("CLIPBOARD", s("copy p@ss-SYNTHETIC-0001 here")),
            Setting::public("OTHER", s("unrelated")),
            Setting::public("NUMBER", SettingValue::Int(5432)),
        ]);
        assert_well_formed(&page);
        for (name, want) in [
            ("SECRET_KEY", REDACTED),
            ("ALRUST_ADMIN_KEY", REDACTED),
            ("EMPTY_KEY", REDACTED),
            (
                "DATABASE_URL",
                "\"postgres://[REDACTED]@db:5432/[REDACTED]\"",
            ),
            ("BASE_URL", REDACTED),
            ("ALLOWED_ENVIRONMENTS", REDACTED),
            ("AUTH_PROVIDER", REDACTED),
            ("CLIPBOARD", REDACTED),
            ("OTHER", "\"unrelated\""),
            ("NUMBER", "5432"),
        ] {
            assert_eq!(line(&page, name), want, "{name}");
        }
        for sentinel in ["SYNTHETIC", "p@ss", "p%40ss", "secret-host"] {
            assert!(!page.contains(sentinel), "{sentinel} in {page}");
        }
    }

    /// A hit is an emitted string containing a secret value, not the reverse.
    #[test]
    fn the_check_is_containment_of_the_secret() {
        let page = render(&[
            Setting::secret("A_KEY", vec!["SENTINEL-abcdef-0001".into()]),
            Setting::public("INSIDE", s("SENTINEL-abcdef")),
            Setting::public("AROUND", s("xxSENTINEL-abcdef-0001xx")),
        ]);
        assert_eq!(line(&page, "INSIDE"), "\"SENTINEL-abcdef\"");
        assert_eq!(line(&page, "AROUND"), REDACTED);
    }

    /// The scheme or port holding the password hides the value; a weak
    /// password elsewhere does not.
    #[test]
    fn a_password_in_the_scheme_or_port_hides_the_connection() {
        let db = |url: &str| {
            render(&[Setting::connection(
                "DATABASE_URL",
                Some(url.into()),
                vec![url.into()],
            )])
        };
        assert_eq!(
            line(&db("postgres://u:postgres@db:5432/x"), "DATABASE_URL"),
            REDACTED
        );
        assert_eq!(
            line(&db("postgres://u:543@db:5432/x"), "DATABASE_URL"),
            REDACTED
        );
        assert_eq!(
            line(
                &db("postgres://alrust:alrust@db:5432/alrust"),
                "DATABASE_URL"
            ),
            "\"postgres://[REDACTED]@db:5432/[REDACTED]\""
        );
        // A malformed source still gives up its password.
        let page = render(&[
            Setting::connection(
                "DATABASE_URL",
                Some("postgres://u:SENTINEL@pw-0001@db/x".into()),
                vec!["postgres://u:SENTINEL@pw-0001@db/x".into()],
            ),
            Setting::public("NOTE", s("has SENTINEL@pw-0001")),
        ]);
        assert_eq!(line(&page, "DATABASE_URL"), REDACTED);
        assert_eq!(line(&page, "NOTE"), REDACTED);
    }

    /// Vector 6 and the line breaks beyond `\n`.
    #[test]
    fn no_value_starts_a_line() {
        let page = render(&[
            Setting::public("A", s("x\nSECRET_KEY: leaked")),
            Setting::public(
                "B",
                s("cr\r tab\t nel\u{85} ls\u{2028} ps\u{2029} del\u{7f}"),
            ),
            Setting::public("C", s("é 😀")),
        ]);
        assert_well_formed(&page);
        assert_eq!(page.lines().count(), 3);
        assert_eq!(line(&page, "A"), "\"x\\nSECRET_KEY: leaked\"");
        assert_eq!(
            line(&page, "B"),
            "\"cr\\r tab\\t nel\\u0085 ls\\u2028 ps\\u2029 del\\u007f\""
        );
        assert_eq!(line(&page, "C"), "\"\\u00e9 \\ud83d\\ude00\"");
    }

    /// A public setting under a reserved name is shown as secret.
    #[test]
    fn reserved_names_cannot_be_public() {
        for name in [
            "SECRET_KEY",
            "SMTP_PASSWORD",
            "LDAP_PASSWD",
            "GITHUB_TOKEN",
            "AWS_CREDENTIALS",
            "PRIVATE_THING",
            "ALRUST_ADMIN_KEY",
            "API_KEYS",
        ] {
            assert!(!may_be_public(name), "{name}");
            let page = render(&[Setting::public(name, s("visible"))]);
            assert_eq!(line(&page, name), REDACTED, "{name}");
        }
        for name in [
            "TOKEN_EXPIRE_DAYS",
            "AUTH_REQUIRED",
            "KEYBOARD",
            "MONKEY_COUNT",
        ] {
            assert!(may_be_public(name), "{name}");
        }
    }

    /// Sorted by name, one line each; `Debug` shows no secret.
    #[test]
    fn lines_are_sorted_and_debug_hides_secrets() {
        let settings = [
            Setting::public("B_SETTING", SettingValue::Bool(true)),
            Setting::public(
                "A_SETTING",
                SettingValue::List(vec!["x".into(), "y".into()]),
            ),
            Setting::secret("C_KEY", vec!["SENTINEL-debug-0001".into()]),
            Setting::public("D_SETTING", SettingValue::Null),
        ];
        assert_eq!(
            render(&settings),
            "A_SETTING: [\"x\",\"y\"]\nB_SETTING: true\nC_KEY: [REDACTED]\nD_SETTING: null\n"
        );
        assert!(!format!("{settings:?}").contains("SENTINEL"));
        assert_eq!(percent_decode("a%40b%zz%4"), "a@b%zz%4");
    }
}
