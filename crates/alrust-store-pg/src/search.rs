//! `q=` search (reference `postgres/queryparser.py` with pyparsing 3.3.2;
//! `spec/search-q.md`; `spec/security-fixes.md` SF-QUERY items 3, 4 and 6).
//!
//! Two layers:
//! - [`parse`] and [`translate`] reproduce the reference byte for byte:
//!   the same grammar, the same silently dropped trailing input (F1), the
//!   same SQL text and the same parameters in the same order. With
//!   [`Chains::Reference`] a chain keeps only its first two operands (F19),
//!   as the reference does; the corpus test checks this layer.
//! - [`check_fields`] and [`Chains::Fold`] apply SF-QUERY: every search
//!   field must be a column (item 4), and a chain is folded to the left
//!   (item 6).
//!
//! The grammar, as pyparsing 3.3.2 applies it here (`core.py`, `helpers.py`):
//! - tabs are expanded to 8-column tab stops before parsing
//!   (`ParserElement.parse_string`), and space, tab, newline and carriage
//!   return are skipped before every token;
//! - a word is a run of printable ASCII characters (33 to 126) other than
//!   `=><(){}[]^"~*?:\/.&|`;
//! - `AND`, `OR`, `NOT` and `TO` are keywords only when neither the
//!   character before nor the one after is a letter, digit, `_` or `$`
//!   (`Keyword.parseImpl`); `&&`, `||` and `!` are plain literals;
//! - a quoted string ends at its closing quote and cannot contain a newline
//!   or a carriage return; its `\t`, `\n`, `\f`, `\r`, `\ooo`, `\0`, `\xhh`
//!   and `\uhhhh` escapes are converted (`QuotedString.parseImpl`);
//! - levels, innermost first: `+`/`-`, `NOT`/`!`, `AND`/`&&`,
//!   `OR`/`||`/adjacency (`infix_notation`). A binary level stops at the
//!   first operator whose operand is missing, without retrying the empty
//!   operator.
//!
//! Like the reference (`ParserElement.enablePackrat()`), the parser
//! memoises each rule's result per position, so no input makes it
//! re-parse the same text more than once per rule.

use std::borrow::Cow;
use std::collections::HashMap;
use std::sync::Arc;

use sqlx::{Postgres, QueryBuilder};

/// One piece of a SQL template.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Piece {
    Text(String),
    /// The reference's parameter `_qp_<index>`. `compare` is the column a
    /// range or one-sided bound is compared with (see [`cast_for`]).
    Param {
        index: usize,
        compare: Option<String>,
    },
}

/// A translated query: the SQL template and the parameters.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SearchSql {
    pub pieces: Vec<Piece>,
    pub params: Vec<String>,
}

impl SearchSql {
    /// The template in the reference's `%(_qp_N)s` form.
    pub fn reference_text(&self) -> String {
        let mut out = String::new();
        for p in &self.pieces {
            match p {
                Piece::Text(t) => out.push_str(t),
                Piece::Param { index, .. } => {
                    out.push_str("%(_qp_");
                    out.push_str(&index.to_string());
                    out.push_str(")s");
                }
            }
        }
        out
    }

    /// Appends the template to `qb`: each parameter bound in turn, and a
    /// compared bound followed by its column's cast ([`cast_for`]).
    pub fn push_to<'q>(&'q self, qb: &mut QueryBuilder<'q, Postgres>) {
        for piece in &self.pieces {
            match piece {
                Piece::Text(t) => {
                    qb.push(t);
                }
                Piece::Param { index, compare } => {
                    qb.push_bind(&self.params[*index]);
                    if let Some(c) = compare {
                        qb.push(cast_for(c));
                    }
                }
            }
        }
    }
}

/// The cast a range or one-sided bound needs. psycopg2 sends a bound value
/// as an untyped literal, which PostgreSQL reads as the compared column's
/// type; sqlx binds text, so a non-text column needs its type named
/// (`sql/schema.sql:32-62`).
pub fn cast_for(column: &str) -> &'static str {
    match column {
        "timeout" | "duplicate_count" => "::integer",
        "create_time" | "receive_time" | "last_receive_time" | "update_time" => "::timestamp",
        "repeat" => "::boolean",
        "attributes" => "::jsonb",
        "correlate" | "service" | "tags" => "::text[]",
        "history" => "::history[]",
        _ => "",
    }
}

const SPECIAL: &[u8] = b"=><(){}[]^\"~*?:\\/.&|";

/// The deepest nesting accepted, counting the query itself, each
/// parenthesis and each unary operator: 31 nested parentheses (or 31
/// `NOT`s) parse, 32 do not. The reference recurses without a limit and
/// fails at Python's recursion limit with an uncaught error; alrust
/// refuses deeper input as a parse error.
pub const MAX_DEPTH: usize = 32;

fn word_byte(b: u8) -> bool {
    (33..=126).contains(&b) && !SPECIAL.contains(&b)
}

/// `Keyword.DEFAULT_KEYWORD_CHARS`: ASCII letters, digits, `_` and `$`.
fn keyword_byte(b: u8) -> bool {
    b.is_ascii_alphanumeric() || b == b'_' || b == b'$'
}

/// `str.expandtabs()`: tab stops every 8 columns, counted in characters
/// and reset by newline and carriage return.
fn expand_tabs(q: &str) -> Cow<'_, str> {
    if !q.contains('\t') {
        return Cow::Borrowed(q);
    }
    let mut out = String::with_capacity(q.len() + 8);
    let mut column = 0;
    for c in q.chars() {
        match c {
            '\t' => {
                let n = 8 - column % 8;
                out.extend(std::iter::repeat_n(' ', n));
                column += n;
            }
            '\n' | '\r' => {
                out.push(c);
                column = 0;
            }
            _ => {
                out.push(c);
                column += 1;
            }
        }
    }
    Cow::Owned(out)
}

fn hex_value(digits: &[u8]) -> Option<u32> {
    if !digits.iter().all(u8::is_ascii_hexdigit) {
        return None;
    }
    u32::from_str_radix(std::str::from_utf8(digits).ok()?, 16).ok()
}

/// The escape after a backslash, as `QuotedString`'s unquoting scan
/// (whitespace escapes first, then octal, NUL, hex and Unicode): the
/// character and the number of bytes it uses after the backslash.
fn escape(rest: &[u8]) -> Option<(char, usize)> {
    let octal = |b: &u8| (b'0'..=b'7').contains(b);
    match rest.first()? {
        b't' => Some(('\t', 1)),
        b'n' => Some(('\n', 1)),
        b'f' => Some(('\x0c', 1)),
        b'r' => Some(('\r', 1)),
        b'0'..=b'7' if rest.len() >= 3 && rest[..3].iter().all(octal) => {
            let v = u32::from_str_radix(std::str::from_utf8(&rest[..3]).ok()?, 8).ok()?;
            Some((char::from_u32(v)?, 3))
        }
        b'0' => Some(('\0', 1)),
        b'x' => Some((char::from_u32(hex_value(rest.get(1..3)?)?)?, 3)),
        b'u' => {
            let v = hex_value(rest.get(1..5)?)?;
            // A lone surrogate is held as NUL: neither can be sent.
            Some((char::from_u32(v).unwrap_or('\0'), 5))
        }
        _ => None,
    }
}

fn unquote(raw: &str) -> String {
    let b = raw.as_bytes();
    let mut out = String::with_capacity(raw.len());
    let mut i = 0;
    while i < b.len() {
        if b[i] == b'\\'
            && let Some((c, used)) = escape(&b[i + 1..])
        {
            out.push(c);
            i += 1 + used;
            continue;
        }
        let Some(c) = raw[i..].chars().next() else {
            break;
        };
        out.push(c);
        i += c.len_utf8();
    }
    out
}

/// The field of a clause.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Field {
    /// No `field:`: the default field.
    Default,
    /// `attr.name:` or `name:`.
    Named { attr: Option<String>, name: String },
}

impl Field {
    /// pyparsing's `tokens.field[0]`.
    fn first(&self) -> &str {
        match self {
            Field::Default => DEFAULT,
            Field::Named { attr: Some(a), .. } => a,
            Field::Named { attr: None, name } => name,
        }
    }

    /// The field as the caller wrote it, for messages.
    pub fn written(&self) -> String {
        match self {
            Field::Default => DEFAULT.to_owned(),
            Field::Named {
                attr: Some(a),
                name,
            } => format!("{a}.{name}"),
            Field::Named { attr: None, name } => name.clone(),
        }
    }
}

/// A range bound: a word or `*`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Bound {
    pub value: String,
    pub inclusive: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Term {
    Word(String),
    /// Unquoted, escapes converted.
    Phrase(String),
    /// Already converted: `?` to `.?`, `*` to `.*`.
    Wildcard(String),
    /// Unquoted, escapes converted.
    Regex(String),
    Range {
        lower: Bound,
        upper: Bound,
    },
    OneSided {
        op: &'static str,
        bound: String,
    },
    Sub(Arc<Node>),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Node {
    Clause {
        field: Field,
        term: Term,
    },
    /// `+` or `-`.
    Modifier(char, Arc<Node>),
    Not(Arc<Node>),
    /// Two or more operands.
    And(Vec<Arc<Node>>),
    /// Two or more operands.
    Or(Vec<Arc<Node>>),
}

type Parsed = Option<(Arc<Node>, usize)>;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
enum Rule {
    Or,
    And,
    Not,
    Modifier,
    Clause,
}

struct Parser<'a> {
    s: &'a [u8],
    depth: usize,
    too_deep: bool,
    memo: HashMap<(Rule, usize), Parsed>,
}

impl Parser<'_> {
    fn ws(&self, mut p: usize) -> usize {
        while p < self.s.len() && matches!(self.s[p], b' ' | b'\t' | b'\n' | b'\r') {
            p += 1;
        }
        p
    }

    fn literal(&self, p: usize, lit: &str) -> Option<usize> {
        let p = self.ws(p);
        self.s[p..]
            .starts_with(lit.as_bytes())
            .then_some(p + lit.len())
    }

    fn keyword(&self, p: usize, kw: &str) -> Option<usize> {
        let start = self.ws(p);
        if start > 0 && keyword_byte(self.s[start - 1]) {
            return None;
        }
        let end = self.literal(start, kw)?;
        match self.s.get(end) {
            Some(&b) if keyword_byte(b) => None,
            _ => Some(end),
        }
    }

    fn text(&self, from: usize, to: usize) -> String {
        String::from_utf8_lossy(&self.s[from..to]).into_owned()
    }

    fn word(&self, p: usize) -> Option<(String, usize)> {
        let start = self.ws(p);
        let mut end = start;
        while end < self.s.len() && word_byte(self.s[end]) {
            end += 1;
        }
        (end > start).then(|| (self.text(start, end), end))
    }

    fn quoted(&self, p: usize, q: u8) -> Option<(String, usize)> {
        let start = self.ws(p);
        if self.s.get(start) != Some(&q) {
            return None;
        }
        let mut end = start + 1;
        while end < self.s.len() && self.s[end] != q {
            if matches!(self.s[end], b'\n' | b'\r') {
                return None;
            }
            end += 1;
        }
        (end < self.s.len()).then(|| (unquote(&self.text(start + 1, end)), end + 1))
    }

    /// `[a-z0-9]*[?*][a-z0-9]*` at the position.
    fn wildcard(&self, p: usize) -> Option<(String, usize)> {
        let start = self.ws(p);
        let lower = |b: u8| b.is_ascii_lowercase() || b.is_ascii_digit();
        let mut i = start;
        while i < self.s.len() && lower(self.s[i]) {
            i += 1;
        }
        if !matches!(self.s.get(i), Some(b'?' | b'*')) {
            return None;
        }
        i += 1;
        while i < self.s.len() && lower(self.s[i]) {
            i += 1;
        }
        let raw = self.text(start, i);
        Some((raw.replace('?', ".?").replace('*', ".*"), i))
    }

    fn bound(&self, p: usize) -> Option<(String, usize)> {
        self.word(p)
            .or_else(|| self.literal(p, "*").map(|e| ("*".to_owned(), e)))
    }

    fn range(&self, p: usize) -> Option<(Term, usize)> {
        let (inclusive_low, p) = match (self.literal(p, "["), self.literal(p, "{")) {
            (Some(e), _) => (true, e),
            (None, Some(e)) => (false, e),
            _ => return None,
        };
        let (low, p) = self.bound(p)?;
        let p = self.keyword(p, "TO")?;
        let (high, p) = self.bound(p)?;
        let (inclusive_high, p) = match (self.literal(p, "]"), self.literal(p, "}")) {
            (Some(e), _) => (true, e),
            (None, Some(e)) => (false, e),
            _ => return None,
        };
        let lower = Bound {
            value: low,
            inclusive: inclusive_low,
        };
        let upper = Bound {
            value: high,
            inclusive: inclusive_high,
        };
        Some((Term::Range { lower, upper }, p))
    }

    fn one_sided(&self, p: usize) -> Option<(Term, usize)> {
        let (op, p) = [">=", ">", "<=", "<"]
            .iter()
            .find_map(|op| self.literal(p, op).map(|e| (*op, e)))?;
        let (bound, p) = self.word(p)?;
        Some((Term::OneSided { op, bound }, p))
    }

    fn term(&mut self, p: usize) -> Option<(Term, usize)> {
        if let Some(r) = self.range(p) {
            return Some(r);
        }
        if let Some(r) = self.one_sided(p) {
            return Some(r);
        }
        if let Some((r, e)) = self.quoted(p, b'/') {
            return Some((Term::Regex(r), e));
        }
        if let Some((w, e)) = self.wildcard(p) {
            return Some((Term::Wildcard(w), e));
        }
        if let Some((ph, e)) = self.quoted(p, b'"') {
            return Some((Term::Phrase(ph), e));
        }
        if let Some((w, e)) = self.word(p) {
            return Some((Term::Word(w), e));
        }
        let open = self.literal(p, "(")?;
        let (inner, e) = self.query(open)?;
        let e = self.literal(e, ")")?;
        Some((Term::Sub(inner), e))
    }

    fn field(&self, p: usize) -> Option<(Field, usize)> {
        let (first, after_first) = self.word(p)?;
        if let Some(dot) = self.literal(after_first, ".") {
            // `Optional(attr + DOT)` matched: no retry without it.
            let (name, after_name) = self.word(dot)?;
            let colon = self.literal(after_name, ":")?;
            let field = Field::Named {
                attr: Some(first),
                name,
            };
            return Some((field, colon));
        }
        let colon = self.literal(after_first, ":")?;
        let field = Field::Named {
            attr: None,
            name: first,
        };
        Some((field, colon))
    }

    fn memo(&mut self, rule: Rule, p: usize, f: impl FnOnce(&mut Self, usize) -> Parsed) -> Parsed {
        if let Some(hit) = self.memo.get(&(rule, p)) {
            return hit.clone();
        }
        let result = f(self, p);
        self.memo.insert((rule, p), result.clone());
        result
    }

    fn deeper(&mut self, f: impl FnOnce(&mut Self) -> Parsed) -> Parsed {
        if self.depth >= MAX_DEPTH {
            self.too_deep = true;
            return None;
        }
        self.depth += 1;
        let r = f(self);
        self.depth -= 1;
        r
    }

    fn clause(&mut self, p: usize) -> Parsed {
        self.memo(Rule::Clause, p, |s, p| {
            let (field, p) = s.field(p).unwrap_or((Field::Default, p));
            let (term, e) = s.term(p)?;
            Some((Arc::new(Node::Clause { field, term }), e))
        })
    }

    /// A unary level: `op operand` with the operand at the same level,
    /// else the next level at the same position.
    fn modifier(&mut self, p: usize) -> Parsed {
        self.memo(Rule::Modifier, p, |s, p| {
            for (sign, lit) in [('+', "+"), ('-', "-")] {
                if let Some(after) = s.literal(p, lit)
                    && let Some((inner, e)) = s.deeper(|s| s.modifier(after))
                {
                    return Some((Arc::new(Node::Modifier(sign, inner)), e));
                }
            }
            s.clause(p)
        })
    }

    fn not(&mut self, p: usize) -> Parsed {
        self.memo(Rule::Not, p, |s, p| {
            if let Some(after) = s.keyword(p, "NOT").or_else(|| s.literal(p, "!"))
                && let Some((inner, e)) = s.deeper(|s| s.not(after))
            {
                return Some((Arc::new(Node::Not(inner)), e));
            }
            s.modifier(p)
        })
    }

    fn and(&mut self, p: usize) -> Parsed {
        self.memo(Rule::And, p, |s, p| {
            let (first, mut end) = s.not(p)?;
            let mut items = vec![first];
            while let Some(after) = s.keyword(end, "AND").or_else(|| s.literal(end, "&&"))
                && let Some((next, e)) = s.not(after)
            {
                items.push(next);
                end = e;
            }
            Some((chain(items, Node::And), end))
        })
    }

    fn or(&mut self, p: usize) -> Parsed {
        self.memo(Rule::Or, p, |s, p| {
            let (first, mut end) = s.and(p)?;
            let mut items = vec![first];
            loop {
                // `Optional(OR)`: a matched operator is not retried as empty.
                let after = s
                    .keyword(end, "OR")
                    .or_else(|| s.literal(end, "||"))
                    .unwrap_or(end);
                let Some((next, e)) = s.and(after) else {
                    break;
                };
                items.push(next);
                end = e;
            }
            Some((chain(items, Node::Or), end))
        })
    }

    fn query(&mut self, p: usize) -> Parsed {
        self.deeper(|s| s.or(p))
    }
}

fn chain(mut items: Vec<Arc<Node>>, make: fn(Vec<Arc<Node>>) -> Node) -> Arc<Node> {
    if items.len() == 1 {
        items.remove(0)
    } else {
        Arc::new(make(items))
    }
}

/// `query_expr.parseString(q)[0]`: `None` where the reference raises
/// `ParseException`, and beyond [`MAX_DEPTH`]. Trailing input that does
/// not parse is dropped, as in the reference (F1).
pub fn parse(q: &str) -> Option<Arc<Node>> {
    let expanded = expand_tabs(q);
    let mut p = Parser {
        s: expanded.as_bytes(),
        depth: 0,
        too_deep: false,
        memo: HashMap::new(),
    };
    let parsed = p.query(0).map(|(node, _)| node);
    if p.too_deep { None } else { parsed }
}

/// How a chain of three or more operands is translated.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Chains {
    /// The reference: only the first two operands (F19).
    Reference,
    /// SF-QUERY item 6: folded to the left.
    Fold,
}

const DEFAULT: &str = "__default_field__";
const ARRAYS: [&str; 3] = ["correlate", "service", "tags"];

struct Out {
    params: Vec<String>,
}

impl Out {
    fn param(&mut self, v: String) -> usize {
        self.params.push(v);
        self.params.len() - 1
    }
}

fn text(t: impl Into<String>) -> Piece {
    Piece::Text(t.into())
}

fn param(index: usize) -> Piece {
    Piece::Param {
        index,
        compare: None,
    }
}

fn compare(index: usize, column: &str) -> Piece {
    Piece::Param {
        index,
        compare: Some(column.to_owned()),
    }
}

fn col(c: &str) -> String {
    format!("\"{c}\"")
}

/// `str.replace(needle, with)` over the text pieces. A bound compared with
/// the default field now compares with `column` (`None`: a text value).
fn replace(pieces: Vec<Piece>, needle: &str, with: &[Piece], column: Option<&str>) -> Vec<Piece> {
    let mut out = Vec::with_capacity(pieces.len());
    for p in pieces {
        match p {
            Piece::Text(t) if t.contains(needle) => {
                let mut parts = t.split(needle).peekable();
                while let Some(part) = parts.next() {
                    if !part.is_empty() {
                        out.push(text(part));
                    }
                    if parts.peek().is_some() {
                        out.extend(with.iter().cloned());
                    }
                }
            }
            Piece::Param {
                index,
                compare: Some(c),
            } if c == DEFAULT => out.push(Piece::Param {
                index,
                compare: column.map(str::to_owned),
            }),
            other => out.push(other),
        }
    }
    out
}

fn parts(field: &Field) -> (Option<&str>, &str) {
    match field {
        Field::Named { attr, name } => (attr.as_deref(), name.as_str()),
        Field::Default => (None, ""),
    }
}

/// The `singleterm` branch of `SearchTerm.to_sql`.
fn word_sql(field: &Field, word: &str, o: &mut Out) -> Vec<Piece> {
    let (attr, name) = parts(field);
    let first = field.first();
    if name == "_exists_" {
        let p = o.param(word.to_owned());
        vec![text("\"attributes\"::jsonb ? "), param(p)]
    } else if ARRAYS.contains(&name) {
        let p = o.param(word.to_owned());
        vec![param(p), text(format!("=ANY({})", col(first)))]
    } else if let Some(a) = attr {
        let prefix = a.replace('_', "attributes");
        let key = o.param(name.to_owned());
        let value = o.param(format!("%{word}%"));
        vec![
            text(format!("{}::jsonb ->>", col(&prefix))),
            param(key),
            text(" ILIKE "),
            param(value),
        ]
    } else {
        let p = o.param(format!("%{word}%"));
        vec![text(format!("{} ILIKE ", col(first))), param(p)]
    }
}

/// The `phrase` branch: it tests `field[0]`, where the word branch tests
/// the field name.
fn phrase_sql(field: &Field, ph: &str, o: &mut Out) -> Vec<Piece> {
    let (attr, name) = parts(field);
    let first = field.first();
    if first == DEFAULT {
        let p = o.param(format!("\\y{ph}\\y"));
        vec![text(format!("{} ~* ", col(DEFAULT))), param(p)]
    } else if ARRAYS.contains(&first) {
        let p = o.param(ph.to_owned());
        vec![param(p), text(format!("=ANY({})", col(first)))]
    } else if let Some(a) = attr {
        let prefix = a.replace('_', "attributes");
        let key = o.param(name.to_owned());
        let value = o.param(format!("\\y{ph}\\y"));
        vec![
            text(format!("{}::jsonb ->>", col(&prefix))),
            param(key),
            text(" ~* "),
            param(value),
        ]
    } else {
        let p = o.param(format!("\\y{ph}\\y"));
        vec![text(format!("{} ~* ", col(first))), param(p)]
    }
}

fn range_sql(first: &str, lower: &Bound, upper: &Bound, o: &mut Out) -> Vec<Piece> {
    let mut out = vec![text("(")];
    for (bound, ops, after) in [(lower, (">=", ">"), " AND "), (upper, ("<=", "<"), ")")] {
        if bound.value == "*" {
            out.push(text("1=1"));
        } else {
            let p = o.param(bound.value.clone());
            let op = if bound.inclusive { ops.0 } else { ops.1 };
            out.push(text(format!("{} {op} ", col(first))));
            out.push(compare(p, first));
        }
        out.push(text(after));
    }
    out
}

fn sub_sql(field: &Field, inner: &Node, chains: Chains, o: &mut Out) -> Vec<Piece> {
    let (attr, name) = parts(field);
    let inner_sql = node_sql(inner, chains, o);
    if let Some(a) = attr {
        let a = if a == "_" { "attributes" } else { a };
        let k = o.param(name.to_owned());
        let with = [text(format!("{}::jsonb ->>", col(a))), param(k)];
        replace(inner_sql, &col(DEFAULT), &with, None)
    } else {
        let f = if name.is_empty() { field.first() } else { name };
        replace(inner_sql, &col(DEFAULT), &[text(col(f))], Some(f))
    }
}

fn clause_sql(field: &Field, term: &Term, chains: Chains, o: &mut Out) -> Vec<Piece> {
    let first = field.first();
    match term {
        Term::Word(w) => word_sql(field, w, o),
        Term::Phrase(ph) => phrase_sql(field, ph, o),
        Term::Wildcard(w) => {
            let p = o.param(format!("\\y{w}\\y"));
            vec![text(format!("{} ~* ", col(first))), param(p)]
        }
        Term::Regex(r) => {
            let p = o.param(r.clone());
            vec![text(format!("{} ~* ", col(first))), param(p)]
        }
        Term::Range { lower, upper } => range_sql(first, lower, upper, o),
        Term::OneSided { op, bound } => {
            let p = o.param(bound.clone());
            vec![
                text(format!("({} {op} ", col(first))),
                compare(p, first),
                text(")"),
            ]
        }
        Term::Sub(inner) => sub_sql(field, inner, chains, o),
    }
}

/// A chain as nested pairs, `((x1 op x2) op x3) …`. Each pair is the
/// reference's: `OR` with a `NOT` right operand becomes `AND`
/// (`queryparser.py:54-57`).
fn chain_sql(op: &str, items: &[Arc<Node>], chains: Chains, o: &mut Out) -> Vec<Piece> {
    let take = match chains {
        Chains::Reference => &items[..2],
        Chains::Fold => items,
    };
    let mut out = vec![text("(".repeat(take.len() - 1))];
    out.extend(node_sql(&take[0], chains, o));
    for rhs in &take[1..] {
        let op = if op == "OR" && matches!(**rhs, Node::Not(_)) {
            "AND"
        } else {
            op
        };
        out.push(text(format!(" {op} ")));
        out.extend(node_sql(rhs, chains, o));
        out.push(text(")"));
    }
    out
}

fn node_sql(node: &Node, chains: Chains, o: &mut Out) -> Vec<Piece> {
    match node {
        Node::Clause { field, term } => clause_sql(field, term, chains, o),
        Node::Modifier(sign, inner) => {
            let mut out = vec![text(format!("{sign} "))];
            out.extend(node_sql(inner, chains, o));
            out
        }
        Node::Not(inner) => {
            let mut out = vec![text("NOT (")];
            out.extend(node_sql(inner, chains, o));
            out.push(text(")"));
            out
        }
        Node::And(items) => chain_sql("AND", items, chains, o),
        Node::Or(items) => chain_sql("OR", items, chains, o),
    }
}

/// `parsed.to_sql(params)` and the final default-field replacement
/// (`QueryParser.parse`): `default_field` is `q.df`, else `text`. A `q.df`
/// must already be checked against the columns (SF-QUERY item 3): it is
/// written into the SQL text.
pub fn translate(node: &Node, default_field: Option<&str>, chains: Chains) -> SearchSql {
    let mut o = Out { params: Vec::new() };
    let pieces = node_sql(node, chains, &mut o);
    let df = default_field.filter(|d| !d.is_empty()).unwrap_or("text");
    let pieces = replace(pieces, DEFAULT, &[text(df)], Some(df));
    SearchSql {
        pieces,
        params: o.params,
    }
}

/// The `alerts` columns (`sql/schema.sql:32-62`; SF-QUERY alrust item 2).
pub const ALERT_COLUMNS: [&str; 28] = [
    "id",
    "resource",
    "event",
    "environment",
    "severity",
    "correlate",
    "status",
    "service",
    "group",
    "value",
    "text",
    "tags",
    "attributes",
    "origin",
    "type",
    "create_time",
    "timeout",
    "raw_data",
    "customer",
    "duplicate_count",
    "repeat",
    "previous_severity",
    "trend_indication",
    "receive_time",
    "last_receive_id",
    "last_receive_time",
    "history",
    "update_time",
];

/// The identifier the reference writes for a named field in this form
/// (`queryparser.py:136-215`); `None` for `_exists_:` with a word, which
/// always writes `attributes`.
fn emitted(attr: Option<&str>, name: &str, first: &str, term: &Term) -> Option<String> {
    let id = match term {
        Term::Word(_) if name == "_exists_" => return None,
        Term::Word(_) if ARRAYS.contains(&name) => first.to_owned(),
        Term::Phrase(_) if ARRAYS.contains(&first) => first.to_owned(),
        Term::Word(_) | Term::Phrase(_) => match attr {
            // Every `_` of the prefix becomes `attributes` (`:146`, `:166`).
            Some(a) => a.replace('_', "attributes"),
            None => name.to_owned(),
        },
        Term::Wildcard(_) | Term::Regex(_) | Term::Range { .. } | Term::OneSided { .. } => {
            first.to_owned()
        }
        // `_` alone becomes `attributes` (`:208`).
        Term::Sub(_) => match attr {
            Some("_") => "attributes".to_owned(),
            Some(a) => a.to_owned(),
            None => name.to_owned(),
        },
    };
    Some(id)
}

/// SF-QUERY alrust item 4: each named field must lead the reference's
/// translation to a column of `alerts` (or, for an attribute path, to its
/// `jsonb` column `attributes`). `Err` holds the first refused field as
/// written, in order of appearance. Default-field terms take `q.df`, which
/// the caller checks (item 3); inside a subquery they take its field,
/// which is checked here whether or not the subquery uses it.
///
/// # Errors
///
/// The first field that is not supported.
pub fn check_fields(node: &Node) -> Result<(), String> {
    match node {
        Node::Clause { field, term } => {
            if let Field::Named { attr, name } = field {
                let ok = match emitted(attr.as_deref(), name, field.first(), term) {
                    None => true,
                    Some(id) if attr.is_some() => id == "attributes",
                    Some(id) => ALERT_COLUMNS.contains(&id.as_str()),
                };
                if !ok {
                    return Err(field.written());
                }
            }
            match term {
                Term::Sub(inner) => check_fields(inner),
                _ => Ok(()),
            }
        }
        Node::Modifier(_, inner) | Node::Not(inner) => check_fields(inner),
        Node::And(items) | Node::Or(items) => items.iter().try_for_each(|n| check_fields(n)),
    }
}

/// Whether a phrase or regex holds NUL after unquoting (a `\0`, `\000`,
/// `\x00` or `\u0000` escape, or a lone surrogate held as NUL). PostgreSQL
/// text cannot hold it; the reference's driver refuses it with an uncaught
/// error (a 500). Words, bounds and fields are printable ASCII, so a raw
/// NUL outside quotes ends the word and, like any input that does not
/// parse, is dropped with the rest (F1).
fn has_nul(node: &Node) -> bool {
    match node {
        Node::Clause { term, .. } => match term {
            Term::Phrase(t) | Term::Regex(t) => t.contains('\0'),
            Term::Sub(inner) => has_nul(inner),
            _ => false,
        },
        Node::Modifier(_, inner) | Node::Not(inner) => has_nul(inner),
        Node::And(items) | Node::Or(items) => items.iter().any(|n| has_nul(n)),
    }
}

/// Why a `q` is refused, in SF-QUERY alrust item 7's order.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SearchError {
    /// The reference's `Failed to parse query string.`; alrust also gives
    /// it for a value that cannot be sent and beyond [`MAX_DEPTH`], where
    /// the reference fails with an uncaught error.
    Parse,
    /// Item 3: `q.df` is not an `alerts` column.
    DefaultField(String),
    /// Item 4: the first unsupported search field, as written.
    Field(String),
    /// `q.mode` is neither `alerta` nor `lucene`.
    Mode(String),
    /// `q.op` (`q.mode=lucene`) is neither `AND` nor `OR`.
    DefaultOperator(String),
    /// `q.mode=lucene` M16: `~` or `^` after a term or a phrase.
    Operator(char),
    /// `q.mode=lucene` M15: a value the field's type cannot hold.
    Value { field: String, value: String },
    /// Hook 4.2, after parsing: an attribute key the query names, once
    /// unescaped, is a restricted one. The guard's content-free message.
    Restricted,
}

impl SearchError {
    /// The 400 message.
    pub fn message(&self) -> String {
        match self {
            SearchError::Parse => "Failed to parse query string.".to_owned(),
            SearchError::DefaultField(v) => format!("Default field '{v}' not supported."),
            SearchError::Field(f) => format!("Search field '{f}' not supported."),
            SearchError::Mode(m) => format!("Query mode '{m}' not supported."),
            SearchError::DefaultOperator(op) => format!("Default operator '{op}' not supported."),
            SearchError::Operator(c) => format!("Query operator '{c}' not supported."),
            SearchError::Value { field, value } => {
                format!("Search value '{value}' not supported for '{field}'.")
            }
            SearchError::Restricted => {
                "Query selector is not permitted on restricted attributes".to_owned()
            }
        }
    }
}

/// A non-empty `q` with its `q.df`, for an alert query: parsed, checked
/// in item 7's order (syntax, then `q.df`, then the search fields) and
/// translated with chains folded (item 6). `q.df` is checked whether or
/// not the query uses the default field (item 3); an empty `q.df` is the
/// default field `text`, as in the reference (`default_field or 'text'`).
///
/// # Errors
///
/// The first failed check.
pub fn compile(q: &str, default_field: Option<&str>) -> Result<SearchSql, SearchError> {
    let node = parse(q).ok_or(SearchError::Parse)?;
    if has_nul(&node) {
        return Err(SearchError::Parse);
    }
    if names_restricted_key(&node) {
        return Err(SearchError::Restricted);
    }
    let df = default_field.filter(|d| !d.is_empty());
    if let Some(d) = df
        && !ALERT_COLUMNS.contains(&d)
    {
        return Err(SearchError::DefaultField(d.to_owned()));
    }
    check_fields(&node).map_err(SearchError::Field)?;
    Ok(translate(&node, df, Chains::Fold))
}

/// Whether an attribute key the query names is a restricted one (hook 4.2).
/// The raw-text guard already refuses every such query in this grammar,
/// whose words have no escapes; this is the same rule on the parse tree,
/// as `q.mode=lucene` needs it.
fn names_restricted_key(node: &Node) -> bool {
    match node {
        Node::Clause { field, term } => {
            let own = match (field, term) {
                (
                    Field::Named {
                        attr: Some(a),
                        name,
                    },
                    _,
                ) => {
                    (a == "_" || a.replace('_', "attributes") == "attributes")
                        && alrust_domain::is_restricted_attribute(name)
                }
                (Field::Named { attr: None, name }, Term::Word(k) | Term::Phrase(k)) => {
                    name == "_exists_" && alrust_domain::is_restricted_attribute(k)
                }
                _ => false,
            };
            own || matches!(term, Term::Sub(inner) if names_restricted_key(inner))
        }
        Node::Modifier(_, inner) | Node::Not(inner) => names_restricted_key(inner),
        Node::And(items) | Node::Or(items) => items.iter().any(|n| names_restricted_key(n)),
    }
}

/// `q` with `q.df`, `q.mode` and `q.op` (each the first occurrence): the
/// default mode ([`compile`]) unless `q.mode=lucene`
/// (`spec/search-lucene-mode.md`, [`crate::search_lucene::compile`]). An
/// empty `q.mode` is the default.
///
/// # Errors
///
/// [`SearchError::Mode`] for any other mode, else the mode's first failed
/// check.
pub fn compile_mode(
    q: &str,
    default_field: Option<&str>,
    mode: Option<&str>,
    op: Option<&str>,
) -> Result<SearchSql, SearchError> {
    match mode.filter(|m| !m.is_empty()) {
        None | Some("alerta") => compile(q, default_field),
        Some("lucene") => crate::search_lucene::compile(q, default_field, op),
        Some(other) => Err(SearchError::Mode(other.to_owned())),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::{Map, Value, json};

    /// The reference's own output for 69 queries (`scripts/characterise-q.py`
    /// in #228, `spec/search-q-reference-sql.jsonl`), copied byte for byte.
    const CORPUS: &str = include_str!("../tests/fixtures/search-q-reference-sql.jsonl");

    fn run(q: &str, df: Option<&str>, chains: Chains) -> Option<(String, Vec<String>)> {
        parse(q).map(|n| {
            let s = translate(&n, df, chains);
            (s.reference_text(), s.params)
        })
    }

    fn reference(q: &str) -> Option<(String, Vec<String>)> {
        run(q, None, Chains::Reference)
    }

    fn folded(q: &str) -> (String, Vec<String>) {
        run(q, None, Chains::Fold).unwrap()
    }

    fn strings(v: &[&str]) -> Vec<String> {
        v.iter().map(|s| (*s).to_owned()).collect()
    }

    fn cases() -> Vec<Value> {
        CORPUS
            .lines()
            .skip(1)
            .map(|l| serde_json::from_str(l).unwrap())
            .collect()
    }

    #[test]
    fn the_corpus_identity_is_the_pinned_reference() {
        let first: Value = serde_json::from_str(CORPUS.lines().next().unwrap()).unwrap();
        assert_eq!(
            first,
            json!({"identity": {
                "reference_commit": "f4312727783dad64198b62ed778bbca98a5144a3",
                "module": "alerta/database/backends/postgres/queryparser.py",
                "module_sha256": "d9c13317bb36208bbe83bd305bed1199a9e7a586659ca8256479f6158b9d0ce6",
                "python": "CPython 3.12.14",
                "pyparsing": "3.3.2"
            }})
        );
    }

    #[test]
    fn every_corpus_line_matches_the_reference() {
        let cases = cases();
        assert_eq!(cases.len(), 69);
        for case in &cases {
            let q = case["q"].as_str().unwrap();
            let got = run(q, case["q.df"].as_str(), Chains::Reference);
            if case.get("error").is_some() {
                assert_eq!(got, None, "{q:?} must not parse");
                continue;
            }
            let (sql, params) = got.unwrap_or_else(|| panic!("{q:?} did not parse"));
            assert_eq!(sql, case["sql"].as_str().unwrap(), "{q:?}");
            let mut want = Map::new();
            for (i, p) in params.iter().enumerate() {
                want.insert(format!("_qp_{i}"), Value::String(p.clone()));
            }
            assert_eq!(Value::Object(want), case["params"], "{q:?}");
        }
    }

    /// SF-QUERY item 6: folding changes only the chains of three or more.
    #[test]
    fn folding_changes_only_the_long_chains() {
        let mut changed = Vec::new();
        for case in cases() {
            let q = case["q"].as_str().unwrap();
            let df = case["q.df"].as_str();
            if run(q, df, Chains::Reference) != run(q, df, Chains::Fold) {
                changed.push(q.to_owned());
            }
        }
        assert_eq!(
            changed,
            strings(&[
                "a AND b AND c",
                "a OR b OR c",
                "a b c",
                "a b c d",
                "a && b && c",
                "resource:(a OR b OR c)",
            ])
        );
    }

    #[test]
    fn chains_fold_to_the_left() {
        let t = |n: usize| format!("\"text\" ILIKE %(_qp_{n})s");
        for (q, sql) in [
            (
                "a AND b AND c",
                format!("(({} AND {}) AND {})", t(0), t(1), t(2)),
            ),
            (
                "a && b && c",
                format!("(({} AND {}) AND {})", t(0), t(1), t(2)),
            ),
            (
                "a OR b OR c",
                format!("(({} OR {}) OR {})", t(0), t(1), t(2)),
            ),
            ("a b c", format!("(({} OR {}) OR {})", t(0), t(1), t(2))),
            (
                "a b c d",
                format!("((({} OR {}) OR {}) OR {})", t(0), t(1), t(2), t(3)),
            ),
            // SF-QUERY vector 28: OR with a NOT right operand becomes AND.
            (
                "a OR b OR NOT c",
                format!("(({} OR {}) AND NOT ({}))", t(0), t(1), t(2)),
            ),
            ("a AND b", format!("({} AND {})", t(0), t(1))),
        ] {
            assert_eq!(folded(q).0, sql, "{q}");
        }
        assert_eq!(
            folded("resource:(a OR b OR c)"),
            (
                "((\"resource\" ILIKE %(_qp_0)s OR \"resource\" ILIKE %(_qp_1)s) OR \"resource\" ILIKE %(_qp_2)s)"
                    .to_owned(),
                strings(&["%a%", "%b%", "%c%"])
            )
        );
    }

    /// SF-QUERY item 4 and vectors 19 to 23.
    #[test]
    fn search_fields_must_lead_to_columns() {
        let check = |q: &str| check_fields(&parse(q).unwrap());
        for ok in [
            "resource:alpha",
            "_.region:a",
            "attributes.region:a",
            "_.region:\"eu west\"",
            "_.region:(eu OR us)",
            "_exists_:region",
            "service:Web",
            "service:\"Web App\"",
            "timeout:<60",
            "resource:(a OR b)",
            "disk full",
            "(a OR b)",
            // A listed column whose type does not support the operator
            // keeps the reference's outcome.
            "history:ack",
            "attributes.region:/x/",
            "attributes.tags:x",
        ] {
            assert_eq!(check(ok), Ok(()), "{ok}");
        }
        for (bad, field) in [
            ("unknownfield:disk", "unknownfield"),
            ("rawData:secret", "rawData"),
            ("duplicateCount:[2 TO 5]", "duplicateCount"),
            ("raw_data.x:y", "raw_data.x"),
            ("_.region:/x/", "_.region"),
            ("_.region:eu*", "_.region"),
            ("resource.x:/y/", "resource.x"),
            ("service.x:\"y\"", "service.x"),
            ("_exists_:\"x\"", "_exists_"),
            ("a:b:c", "a"),
            ("nosuch:(x)", "nosuch"),
            ("nosuch:(resource:x)", "nosuch"),
            ("resource:(nosuch:(x))", "nosuch"),
            ("resource:x AND nosuch:y AND other:z", "nosuch"),
        ] {
            assert_eq!(check(bad), Err(field.to_owned()), "{bad}");
        }
    }

    /// pyparsing's `printables` is ASCII 33 to 126: a non-ASCII character
    /// ends a word and cannot start one.
    #[test]
    fn words_are_printable_ascii() {
        assert_eq!(
            reference("résumé"),
            Some(("\"text\" ILIKE %(_qp_0)s".to_owned(), strings(&["%r%"])))
        );
        assert_eq!(reference("ünïcode"), None);
    }

    /// Source-derived from pyparsing 3.3.2 (not in the recorded corpus): a
    /// keyword right after a keyword character is not a keyword.
    #[test]
    fn a_keyword_needs_a_boundary_before_it() {
        assert_eq!(
            reference("d*sAND x"),
            Some((
                "(\"text\" ~* %(_qp_0)s OR \"text\" ILIKE %(_qp_1)s)".to_owned(),
                strings(&["\\yd.*s\\y", "%AND%"])
            ))
        );
    }

    /// Source-derived from pyparsing 3.3.2: tab expansion, the quoted
    /// string's escapes and its end at a carriage return.
    #[test]
    fn quoted_strings_follow_pyparsing() {
        let phrase = |q: &str| reference(q).map(|(_, p)| p);
        assert_eq!(phrase("\"a\tb\""), Some(strings(&["\\ya      b\\y"])));
        assert_eq!(phrase("\"a\\tb\""), Some(strings(&["\\ya\tb\\y"])));
        assert_eq!(
            phrase("\"\\101\\x42\\u0043\""),
            Some(strings(&["\\yABC\\y"]))
        );
        assert_eq!(phrase("\"\\q\\x4\""), Some(strings(&["\\y\\q\\x4\\y"])));
        assert_eq!(phrase("/\\d+/"), Some(strings(&["\\d+"])));
        assert_eq!(reference("\"a\rb\""), None);
        for q in [
            "\"a\\0b\"",
            "\"\\000\"",
            "/\\x00/",
            "\"\\u0000\"",
            "\"\\ud800\"",
        ] {
            assert_eq!(compile(q, None), Err(SearchError::Parse), "{q}");
        }
        // No backslash escape: `\\0` is a backslash, then NUL.
        assert_eq!(compile("\"a\\\\0\"", None), Err(SearchError::Parse));
        assert!(compile("\"\\x0g\"", None).is_ok());
    }

    #[test]
    fn nesting_beyond_the_cap_is_a_parse_error() {
        let nested = |n: usize| format!("{}a{}", "(".repeat(n), ")".repeat(n));
        assert!(parse(&nested(31)).is_some());
        assert_eq!(parse(&nested(32)), None);
        assert!(parse(&format!("{}a", "NOT ".repeat(31))).is_some());
        assert_eq!(parse(&format!("{}a", "NOT ".repeat(32))), None);
        assert!(parse("disk\0 full").is_some());
    }

    #[test]
    fn range_bounds_name_the_compared_column() {
        let compared = |q: &str| {
            let s = translate(&parse(q).unwrap(), None, Chains::Fold);
            s.pieces
                .iter()
                .filter_map(|p| match p {
                    Piece::Param { compare, .. } => Some(compare.clone()),
                    Piece::Text(_) => None,
                })
                .collect::<Vec<_>>()
        };
        let some = |c: &str| Some(c.to_owned());
        assert_eq!(compared("timeout:<60"), vec![some("timeout")]);
        assert_eq!(
            compared("duplicate_count:[2 TO 5]"),
            vec![some("duplicate_count"), some("duplicate_count")]
        );
        assert_eq!(compared("[1 TO 2]"), vec![some("text"), some("text")]);
        assert_eq!(compared("timeout:(>5)"), vec![some("timeout")]);
        // An attribute's value is text (`->>`, `queryparser.py:209-211`):
        // the key, then the bound.
        assert_eq!(compared("_.n:(>5)"), vec![None, None]);
        assert_eq!(compared("resource:x"), vec![None]);
        assert_eq!(cast_for("timeout"), "::integer");
        assert_eq!(cast_for("create_time"), "::timestamp");
        assert_eq!(cast_for("resource"), "");
    }

    /// SF-QUERY items 3 and 7, vectors 15 to 17, 20 and 22.
    #[test]
    fn compile_checks_in_order() {
        let err = |q: &str, df: Option<&str>| compile(q, df).unwrap_err().message();
        assert_eq!(err("(disk", Some("bogus")), "Failed to parse query string.");
        assert_eq!(
            err("nosuch:x", Some("bogus")),
            "Default field 'bogus' not supported."
        );
        assert_eq!(
            err("resource:alpha", Some("bogus")),
            "Default field 'bogus' not supported."
        );
        assert_eq!(
            err("alpha", Some("Resource")),
            "Default field 'Resource' not supported."
        );
        assert_eq!(
            err("disk", Some("resource\" IS NOT NULL OR \"text")),
            "Default field 'resource\" IS NOT NULL OR \"text' not supported."
        );
        assert_eq!(
            err("unknownfield:disk", None),
            "Search field 'unknownfield' not supported."
        );
        assert_eq!(
            err("raw_data.x:y", None),
            "Search field 'raw_data.x' not supported."
        );
        let sql = compile("alpha", Some("resource")).unwrap();
        assert_eq!(sql.reference_text(), "\"resource\" ILIKE %(_qp_0)s");
        let sql = compile("alpha", Some("")).unwrap();
        assert_eq!(sql.reference_text(), "\"text\" ILIKE %(_qp_0)s");
        let sql = compile("a b c", None).unwrap();
        assert_eq!(sql.params, strings(&["%a%", "%b%", "%c%"]));
    }
}
