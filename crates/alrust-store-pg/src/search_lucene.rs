//! `q.mode=lucene` (`spec/search-lucene-mode.md`): an opt-in search that
//! follows the Lucene query syntax Alerta documents for `q`, with the fixes
//! of rules M1 to M17. The default `q` stays [`crate::search`], unchanged.
//!
//! This module has its own parser, so the reference parser (which the
//! recorded corpus guards) is not touched. It writes the same
//! [`SearchSql`] template: every value is bound, and a range bound carries
//! its column's cast.
//!
//! Matching:
//! - a term is a case-insensitive substring, with `%`, `_` and `\` literal
//!   (`ILIKE … ESCAPE '\'`);
//! - a phrase is its literal words, and a wildcard term (`*` any run of
//!   non-space characters, `?` exactly one) matches from the start of a word,
//!   both case-insensitive (`~*`);
//! - an array field matches an element exactly (a term or a phrase,
//!   case-insensitive) or as a whole (a wildcard or a regex);
//! - a negation is `(…) IS NOT TRUE`, so a missing (NULL) field counts as
//!   not matching, never as unknown.

use std::collections::HashMap;
use std::sync::Arc;

use alrust_domain::is_restricted_attribute;
use chrono::{NaiveDate, NaiveDateTime};

use crate::search::{MAX_DEPTH, Piece, SearchError, SearchSql};

/// `q.op`: the operator between clauses written with none (M1).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DefaultOperator {
    And,
    Or,
}

/// One piece of a wildcard term.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Part {
    Literal(String),
    /// `*`.
    AnyRun,
    /// `?`.
    AnyOne,
}

/// A range bound; `None` is `*` (open).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LBound {
    pub value: Option<String>,
    pub inclusive: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LTerm {
    /// Unescaped.
    Word(String),
    Pattern(Vec<Part>),
    /// Unescaped.
    Phrase(String),
    /// As written, with `\/` as `/`.
    Regex(String),
    Range {
        lower: LBound,
        upper: LBound,
    },
    OneSided {
        op: &'static str,
        bound: String,
    },
    /// `field:( … )` or `( … )`.
    Group(Arc<LNode>),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LNode {
    /// `field` as written, unescaped; `None` for the default fields.
    Clause {
        field: Option<String>,
        term: LTerm,
    },
    Not(Arc<LNode>),
    /// Two or more operands.
    And(Vec<Arc<LNode>>),
    /// Two or more operands.
    Or(Vec<Arc<LNode>>),
}

type Parsed = Option<(Arc<LNode>, usize)>;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
enum Rule {
    Or,
    And,
    Unary,
    Clause,
}

/// A word: its unescaped text and, when it has `*` or `?`, its parts.
struct Word {
    text: String,
    parts: Vec<Part>,
    wild: bool,
    escaped: bool,
}

impl Word {
    /// `AND`, `OR` and `NOT`, written plainly, are operators (M4).
    fn is_operator(&self) -> bool {
        !self.escaped && !self.wild && matches!(self.text.as_str(), "AND" | "OR" | "NOT")
    }
}

/// A byte that ends a word: whitespace, a control character or a Lucene
/// special character (`*` and `?` are wildcards, `\` escapes).
fn ends_word(b: u8) -> bool {
    b <= b' '
        || b == 0x7f
        || matches!(
            b,
            b'(' | b')'
                | b'{'
                | b'}'
                | b'['
                | b']'
                | b'^'
                | b'"'
                | b'~'
                | b':'
                | b'/'
                | b'!'
                | b'&'
                | b'|'
        )
}

struct Parser<'a> {
    s: &'a str,
    b: &'a [u8],
    implicit: DefaultOperator,
    depth: usize,
    too_deep: bool,
    unsupported: Option<char>,
    memo: HashMap<(Rule, usize), Parsed>,
}

impl Parser<'_> {
    fn ws(&self, mut p: usize) -> usize {
        while p < self.b.len() && matches!(self.b[p], b' ' | b'\t' | b'\n' | b'\r') {
            p += 1;
        }
        p
    }

    fn literal(&self, p: usize, lit: &str) -> Option<usize> {
        let p = self.ws(p);
        self.b[p..]
            .starts_with(lit.as_bytes())
            .then_some(p + lit.len())
    }

    /// An upper-case keyword, not followed by more word characters.
    fn keyword(&self, p: usize, kw: &str) -> Option<usize> {
        let end = self.literal(p, kw)?;
        match self.b.get(end) {
            Some(&b) if !ends_word(b) => None,
            _ => Some(end),
        }
    }

    fn char_at(&self, i: usize) -> Option<char> {
        self.s.get(i..)?.chars().next()
    }

    fn word(&self, p: usize) -> Option<(Word, usize)> {
        let start = self.ws(p);
        let mut i = start;
        let mut word = Word {
            text: String::new(),
            parts: Vec::new(),
            wild: false,
            escaped: false,
        };
        let mut literal = String::new();
        while let Some(&c) = self.b.get(i) {
            if c == b'\\' {
                let ch = self.char_at(i + 1)?;
                literal.push(ch);
                word.text.push(ch);
                word.escaped = true;
                i += 1 + ch.len_utf8();
                continue;
            }
            if c == b'*' || c == b'?' {
                if !literal.is_empty() {
                    word.parts.push(Part::Literal(std::mem::take(&mut literal)));
                }
                word.parts.push(if c == b'*' {
                    Part::AnyRun
                } else {
                    Part::AnyOne
                });
                word.text.push(char::from(c));
                word.wild = true;
                i += 1;
                continue;
            }
            let operator_start = i == start && matches!(c, b'=' | b'<' | b'>' | b'+' | b'-');
            if ends_word(c) || operator_start {
                break;
            }
            let ch = self.char_at(i)?;
            literal.push(ch);
            word.text.push(ch);
            i += ch.len_utf8();
        }
        if i == start {
            return None;
        }
        if !literal.is_empty() {
            word.parts.push(Part::Literal(literal));
        }
        Some((word, i))
    }

    /// `"…"` (`\` escapes the next character) or `/…/` (`\/` is `/`; other
    /// backslashes stay, for the regex).
    fn quoted(&self, p: usize, q: u8) -> Option<(String, usize)> {
        let start = self.ws(p);
        if self.b.get(start) != Some(&q) {
            return None;
        }
        let mut out = String::new();
        let mut i = start + 1;
        loop {
            let c = *self.b.get(i)?;
            if c == q {
                return Some((out, i + 1));
            }
            if c == b'\\' {
                let ch = self.char_at(i + 1)?;
                if q == b'/' && ch != '/' {
                    out.push('\\');
                }
                out.push(ch);
                i += 1 + ch.len_utf8();
                continue;
            }
            let ch = self.char_at(i)?;
            out.push(ch);
            i += ch.len_utf8();
        }
    }

    /// M16: `~` or `^` right after a term or a phrase.
    fn after_term(&mut self, end: usize) {
        if let Some(&c @ (b'~' | b'^')) = self.b.get(end) {
            self.unsupported.get_or_insert(char::from(c));
        }
    }

    /// A range bound: `*`, a phrase, or a word without wildcards.
    fn bound(&self, p: usize) -> Option<(Option<String>, usize)> {
        if let Some(e) = self.literal(p, "*")
            && self
                .b
                .get(e)
                .is_none_or(|&c| c <= b' ' || c == b']' || c == b'}')
        {
            return Some((None, e));
        }
        if let Some((ph, e)) = self.quoted(p, b'"') {
            return Some((Some(ph), e));
        }
        let (w, e) = self.word(p)?;
        (!w.wild && !w.is_operator()).then_some((Some(w.text), e))
    }

    fn range(&self, p: usize) -> Option<(LTerm, usize)> {
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
        let lower = LBound {
            value: low,
            inclusive: inclusive_low,
        };
        let upper = LBound {
            value: high,
            inclusive: inclusive_high,
        };
        Some((LTerm::Range { lower, upper }, p))
    }

    fn one_sided(&self, p: usize) -> Option<(LTerm, usize)> {
        let (op, after) = [">=", ">", "<=", "<"]
            .iter()
            .find_map(|op| self.literal(p, op).map(|e| (*op, e)))?;
        if let Some((bound, e)) = self.quoted(after, b'"') {
            return Some((LTerm::OneSided { op, bound }, e));
        }
        let (w, e) = self.word(after)?;
        if w.wild || w.is_operator() {
            return None;
        }
        Some((LTerm::OneSided { op, bound: w.text }, e))
    }

    fn term(&mut self, p: usize) -> Option<(LTerm, usize)> {
        if let Some(r) = self.range(p) {
            return Some(r);
        }
        if let Some(r) = self.one_sided(p) {
            return Some(r);
        }
        if let Some((r, e)) = self.quoted(p, b'/') {
            return Some((LTerm::Regex(r), e));
        }
        if let Some((ph, e)) = self.quoted(p, b'"') {
            self.after_term(e);
            return Some((LTerm::Phrase(ph), e));
        }
        if let Some((w, e)) = self.word(p) {
            if w.is_operator() {
                return None;
            }
            self.after_term(e);
            let term = if w.wild {
                LTerm::Pattern(w.parts)
            } else {
                LTerm::Word(w.text)
            };
            return Some((term, e));
        }
        let open = self.literal(p, "(")?;
        let (inner, e) = self.query(open)?;
        let e = self.literal(e, ")")?;
        Some((LTerm::Group(inner), e))
    }

    fn field(&self, p: usize) -> Option<(String, usize)> {
        let (w, e) = self.word(p)?;
        if w.wild || w.is_operator() {
            return None;
        }
        let colon = self.literal(e, ":")?;
        Some((w.text, colon))
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
            let (field, p) = match s.field(p) {
                Some((f, e)) => (Some(f), e),
                None => (None, p),
            };
            let (term, e) = s.term(p)?;
            Some((Arc::new(LNode::Clause { field, term }), e))
        })
    }

    /// `NOT`, `!` and `-` negate (M3); `+` requires, which is the clause
    /// itself.
    fn unary(&mut self, p: usize) -> Parsed {
        self.memo(Rule::Unary, p, |s, p| {
            let negation = s
                .keyword(p, "NOT")
                .or_else(|| s.literal(p, "!"))
                .or_else(|| s.literal(p, "-"));
            if let Some(after) = negation {
                let (inner, e) = s.deeper(|s| s.unary(after))?;
                return Some((Arc::new(LNode::Not(inner)), e));
            }
            if let Some(after) = s.literal(p, "+") {
                return s.deeper(|s| s.unary(after));
            }
            s.clause(p)
        })
    }

    fn and(&mut self, p: usize) -> Parsed {
        self.memo(Rule::And, p, |s, p| {
            let (first, mut end) = s.unary(p)?;
            let mut items = vec![first];
            loop {
                let explicit = s.keyword(end, "AND").or_else(|| s.literal(end, "&&"));
                let after = match (explicit, s.implicit) {
                    (Some(a), _) => a,
                    (None, DefaultOperator::And)
                        if s.keyword(end, "OR").is_none() && s.literal(end, "||").is_none() =>
                    {
                        end
                    }
                    (None, _) => break,
                };
                match s.unary(after) {
                    Some((next, e)) => {
                        items.push(next);
                        end = e;
                    }
                    // `a AND` lacks an operand (M4).
                    None if explicit.is_some() => return None,
                    None => break,
                }
            }
            Some((chain(items, LNode::And), end))
        })
    }

    fn or(&mut self, p: usize) -> Parsed {
        self.memo(Rule::Or, p, |s, p| {
            let (first, mut end) = s.and(p)?;
            let mut items = vec![first];
            loop {
                let explicit = s.keyword(end, "OR").or_else(|| s.literal(end, "||"));
                let after = match (explicit, s.implicit) {
                    (Some(a), _) => a,
                    (None, DefaultOperator::Or) => end,
                    (None, DefaultOperator::And) => break,
                };
                match s.and(after) {
                    Some((next, e)) => {
                        items.push(next);
                        end = e;
                    }
                    None if explicit.is_some() => return None,
                    None => break,
                }
            }
            Some((chain(items, LNode::Or), end))
        })
    }

    fn query(&mut self, p: usize) -> Parsed {
        self.deeper(|s| s.or(p))
    }
}

fn chain(mut items: Vec<Arc<LNode>>, make: fn(Vec<Arc<LNode>>) -> LNode) -> Arc<LNode> {
    if items.len() == 1 {
        items.remove(0)
    } else {
        Arc::new(make(items))
    }
}

/// The whole of `q` (M5), with `op` between clauses written with none.
///
/// # Errors
///
/// [`SearchError::Operator`] for `~` or `^` after a term or a phrase,
/// otherwise [`SearchError::Parse`] for input that does not parse whole or
/// nests beyond [`MAX_DEPTH`].
pub fn parse(q: &str, op: DefaultOperator) -> Result<Arc<LNode>, SearchError> {
    let mut p = Parser {
        s: q,
        b: q.as_bytes(),
        implicit: op,
        depth: 0,
        too_deep: false,
        unsupported: None,
        memo: HashMap::new(),
    };
    let parsed = p.query(0);
    if let Some(c) = p.unsupported {
        return Err(SearchError::Operator(c));
    }
    match parsed {
        Some((node, end)) if !p.too_deep && p.ws(end) == q.len() => Ok(node),
        _ => Err(SearchError::Parse),
    }
}

// ---------------------------------------------------------------- fields

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Kind {
    Int,
    Bool,
    Time,
}

/// Where a field's values are.
#[derive(Debug, Clone, PartialEq, Eq)]
enum Target {
    Text(&'static str),
    Array(&'static str),
    Typed(&'static str, Kind),
    Attribute(String),
    Exists,
}

/// M12: the reference's API names (`VALID_PARAMS`, `postgres/utils.py:108-137`)
/// and the column names. `attributes` and `history` are not searchable (M13).
fn column(name: &str) -> Option<Target> {
    use Target::{Array, Exists, Text, Typed};
    Some(match name {
        "id" => Text("id"),
        "resource" => Text("resource"),
        "event" => Text("event"),
        "environment" => Text("environment"),
        "severity" => Text("severity"),
        "correlate" => Array("correlate"),
        "status" => Text("status"),
        "service" => Array("service"),
        "group" => Text("group"),
        "value" => Text("value"),
        "text" => Text("text"),
        "tag" | "tags" => Array("tags"),
        "origin" => Text("origin"),
        "type" => Text("type"),
        "createTime" | "create_time" => Typed("create_time", Kind::Time),
        "timeout" => Typed("timeout", Kind::Int),
        "rawData" | "raw_data" => Text("raw_data"),
        "customer" => Text("customer"),
        "duplicateCount" | "duplicate_count" => Typed("duplicate_count", Kind::Int),
        "repeat" => Typed("repeat", Kind::Bool),
        "previousSeverity" | "previous_severity" => Text("previous_severity"),
        "trendIndication" | "trend_indication" => Text("trend_indication"),
        "receiveTime" | "receive_time" => Typed("receive_time", Kind::Time),
        "lastReceiveId" | "last_receive_id" => Text("last_receive_id"),
        "lastReceiveTime" | "last_receive_time" => Typed("last_receive_time", Kind::Time),
        "updateTime" | "update_time" => Typed("update_time", Kind::Time),
        "_exists_" => Exists,
        _ => return None,
    })
}

fn resolve(field: &str) -> Option<Target> {
    match field.split_once('.') {
        Some(("attributes" | "_", key)) if !key.is_empty() => {
            Some(Target::Attribute(key.to_owned()))
        }
        Some(_) => None,
        None => column(field),
    }
}

/// M11: a clause without a field matches if any of these does.
fn default_fields() -> Vec<Target> {
    use Target::{Array, Text};
    vec![
        Text("text"),
        Text("resource"),
        Text("event"),
        Array("service"),
        Array("tags"),
        Text("group"),
        Text("value"),
    ]
}

/// Calls `f` with every leaf clause and the field it applies to (its own,
/// or its group's).
fn visit<'n>(
    node: &'n LNode,
    inherited: Option<&'n str>,
    f: &mut impl FnMut(Option<&'n str>, &'n LTerm),
) {
    match node {
        LNode::Clause { field, term } => {
            let field = field.as_deref().or(inherited);
            match term {
                LTerm::Group(inner) => visit(inner, field, f),
                _ => f(field, term),
            }
        }
        LNode::Not(inner) => visit(inner, inherited, f),
        LNode::And(items) | LNode::Or(items) => {
            for item in items {
                visit(item, inherited, f);
            }
        }
    }
}

/// Whether any attribute key the query names, after unescaping, is a
/// restricted one (hook 4.2; the raw-text guard cannot see through escapes).
fn names_restricted_key(node: &LNode) -> bool {
    let mut hit = false;
    visit(node, None, &mut |field, term| {
        hit |= match field.and_then(resolve) {
            Some(Target::Attribute(key)) => is_restricted_attribute(&key),
            Some(Target::Exists) => match term {
                LTerm::Word(k) | LTerm::Phrase(k) => is_restricted_attribute(k),
                _ => false,
            },
            _ => false,
        };
    });
    hit
}

// ---------------------------------------------------------------- translation

struct Out {
    params: Vec<String>,
}

impl Out {
    fn param(&mut self, v: String) -> Piece {
        self.params.push(v);
        Piece::Param {
            index: self.params.len() - 1,
            compare: None,
        }
    }

    fn compare(&mut self, v: String, column: &str) -> Piece {
        self.params.push(v);
        Piece::Param {
            index: self.params.len() - 1,
            compare: Some(column.to_owned()),
        }
    }
}

fn text(t: impl Into<String>) -> Piece {
    Piece::Text(t.into())
}

fn col(c: &str) -> String {
    format!("\"{c}\"")
}

/// `%`, `_` and `\` literal in an `ILIKE … ESCAPE '\'` pattern.
fn like_literal(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for c in s.chars() {
        if matches!(c, '\\' | '%' | '_') {
            out.push('\\');
        }
        out.push(c);
    }
    out
}

/// Every character literal in a PostgreSQL regular expression: a backslash
/// before anything that is not a letter, a digit or a space.
fn regex_literal(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for c in s.chars() {
        if !c.is_alphanumeric() && c != ' ' {
            out.push('\\');
        }
        out.push(c);
    }
    out
}

const WORD_START: &str = "(^|[^[:alnum:]_])";
const WORD_END: &str = "([^[:alnum:]_]|$)";

fn pattern_body(parts: &[Part]) -> String {
    let mut out = String::new();
    for p in parts {
        match p {
            Part::Literal(l) => out.push_str(&regex_literal(l)),
            Part::AnyRun => out.push_str("[^[:space:]]*"),
            Part::AnyOne => out.push_str("[^[:space:]]"),
        }
    }
    out
}

/// M9: a wildcard from the start of a word; the end is a word boundary
/// unless the pattern ends with `*`.
fn word_pattern(parts: &[Part]) -> String {
    let end = if matches!(parts.last(), Some(Part::AnyRun)) {
        ""
    } else {
        WORD_END
    };
    format!("{WORD_START}{}{end}", pattern_body(parts))
}

/// M8: the literal words, bounded by the start or end of the value or a
/// character that is not a letter, a digit or `_`.
fn phrase_pattern(ph: &str) -> String {
    format!("{WORD_START}{}{WORD_END}", regex_literal(ph))
}

fn valid(kind: Kind, value: &str) -> bool {
    match kind {
        Kind::Int => value.parse::<i64>().is_ok(),
        Kind::Bool => matches!(value.to_ascii_lowercase().as_str(), "true" | "false"),
        Kind::Time => {
            [
                "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%dT%H:%M:%S%.f",
                "%Y-%m-%dT%H:%M:%SZ",
                "%Y-%m-%dT%H:%M:%S%.fZ",
                "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d %H:%M:%S%.f",
                "%Y-%m-%dT%H:%M",
                "%Y-%m-%d %H:%M",
            ]
            .iter()
            .any(|f| NaiveDateTime::parse_from_str(value, f).is_ok())
                || NaiveDate::parse_from_str(value, "%Y-%m-%d").is_ok()
        }
    }
}

/// `expr op bound` pieces for a range or one-sided range; `column` names
/// the cast of a typed column.
fn range_pieces(expr: &[Piece], term: &LTerm, column: Option<&str>, o: &mut Out) -> Vec<Piece> {
    let bound = |v: &str, o: &mut Out| match column {
        Some(c) => o.compare(v.to_owned(), c),
        None => o.param(v.to_owned()),
    };
    let mut out = vec![text("(")];
    match term {
        LTerm::OneSided { op, bound: b } => {
            out.extend(expr.iter().cloned());
            out.push(text(format!(" {op} ")));
            out.push(bound(b, o));
        }
        LTerm::Range { lower, upper } => {
            for (side, ops, gap) in [(lower, (">=", ">"), " AND "), (upper, ("<=", "<"), "")] {
                match &side.value {
                    None => out.push(text("true")),
                    Some(v) => {
                        let op = if side.inclusive { ops.0 } else { ops.1 };
                        out.extend(expr.iter().cloned());
                        out.push(text(format!(" {op} ")));
                        out.push(bound(v, o));
                    }
                }
                out.push(text(gap));
            }
        }
        _ => {}
    }
    out.push(text(")"));
    out
}

/// A text-valued expression (a text column, or an attribute's value).
fn text_predicate(expr: &[Piece], term: &LTerm, o: &mut Out) -> Vec<Piece> {
    let mut out: Vec<Piece> = expr.to_vec();
    match term {
        LTerm::Word(w) => {
            out.push(text(" ILIKE "));
            out.push(o.param(format!("%{}%", like_literal(w))));
            out.push(text(" ESCAPE '\\'"));
        }
        LTerm::Pattern(parts) => {
            out.push(text(" ~* "));
            out.push(o.param(word_pattern(parts)));
        }
        LTerm::Phrase(ph) => {
            out.push(text(" ~* "));
            out.push(o.param(phrase_pattern(ph)));
        }
        LTerm::Regex(r) => {
            out.push(text(" ~* "));
            out.push(o.param(r.clone()));
        }
        LTerm::Range { .. } | LTerm::OneSided { .. } => return range_pieces(expr, term, None, o),
        LTerm::Group(_) => {}
    }
    out
}

/// M14: an array matches an element.
fn array_predicate(column: &str, term: &LTerm, o: &mut Out) -> Vec<Piece> {
    let element = [text("e.v")];
    let mut out = vec![text(format!(
        "EXISTS (SELECT 1 FROM unnest({}) AS e(v) WHERE ",
        col(column)
    ))];
    match term {
        LTerm::Word(w) | LTerm::Phrase(w) => {
            out.push(text("e.v ILIKE "));
            out.push(o.param(like_literal(w)));
            out.push(text(" ESCAPE '\\'"));
        }
        LTerm::Pattern(parts) => {
            out.push(text("e.v ~* "));
            out.push(o.param(format!("^{}$", pattern_body(parts))));
        }
        LTerm::Regex(r) => {
            out.push(text("e.v ~* "));
            out.push(o.param(r.clone()));
        }
        LTerm::Range { .. } | LTerm::OneSided { .. } => {
            out.extend(range_pieces(&element, term, None, o));
        }
        LTerm::Group(_) => {}
    }
    out.push(text(")"));
    out
}

fn value_error(field: &str, value: &str) -> SearchError {
    SearchError::Value {
        field: field.to_owned(),
        value: value.to_owned(),
    }
}

/// M15: a typed column compares with a typed value.
fn typed_predicate(
    column: &str,
    kind: Kind,
    term: &LTerm,
    written: &str,
    o: &mut Out,
) -> Result<Vec<Piece>, SearchError> {
    let check = |v: &str| {
        if valid(kind, v) {
            Ok(())
        } else {
            Err(value_error(written, v))
        }
    };
    let expr = [text(col(column))];
    match term {
        LTerm::Word(v) | LTerm::Phrase(v) => {
            check(v)?;
            Ok(vec![
                text(format!("({} = ", col(column))),
                o.compare(v.clone(), column),
                text(")"),
            ])
        }
        LTerm::OneSided { bound, .. } => {
            check(bound)?;
            Ok(range_pieces(&expr, term, Some(column), o))
        }
        LTerm::Range { lower, upper } => {
            for v in [&lower.value, &upper.value].into_iter().flatten() {
                check(v)?;
            }
            Ok(range_pieces(&expr, term, Some(column), o))
        }
        LTerm::Pattern(_) => Err(value_error(written, "*")),
        LTerm::Regex(r) => Err(value_error(written, r)),
        LTerm::Group(_) => Ok(Vec::new()),
    }
}

fn predicate(
    target: &Target,
    term: &LTerm,
    written: &str,
    o: &mut Out,
) -> Result<Vec<Piece>, SearchError> {
    match target {
        Target::Text(c) => Ok(text_predicate(&[text(col(c))], term, o)),
        Target::Array(c) => Ok(array_predicate(c, term, o)),
        Target::Typed(c, kind) => typed_predicate(c, *kind, term, written, o),
        Target::Attribute(key) => {
            if is_restricted_attribute(key) {
                return Err(SearchError::Restricted);
            }
            let expr = [
                text("(\"attributes\" ->> "),
                o.param(key.clone()),
                text(")"),
            ];
            Ok(text_predicate(&expr, term, o))
        }
        Target::Exists => match term {
            LTerm::Word(k) | LTerm::Phrase(k) => {
                if is_restricted_attribute(k) {
                    return Err(SearchError::Restricted);
                }
                Ok(vec![
                    text("(\"attributes\" ? "),
                    o.param(k.clone()),
                    text(")"),
                ])
            }
            _ => Err(value_error(written, "*")),
        },
    }
}

fn join(op: &str, parts: Vec<Vec<Piece>>) -> Vec<Piece> {
    if parts.len() == 1 {
        return parts.into_iter().next().unwrap_or_default();
    }
    let mut out = vec![text("(")];
    for (i, p) in parts.into_iter().enumerate() {
        if i > 0 {
            out.push(text(format!(" {op} ")));
        }
        out.extend(p);
    }
    out.push(text(")"));
    out
}

fn clause_sql(
    field: Option<&str>,
    term: &LTerm,
    defaults: &[Target],
    o: &mut Out,
) -> Result<Vec<Piece>, SearchError> {
    let targets = match field {
        None => defaults.to_vec(),
        Some(f) => vec![resolve(f).ok_or_else(|| SearchError::Field(f.to_owned()))?],
    };
    if let LTerm::Group(inner) = term {
        return node_sql(inner, &targets, o);
    }
    // A range without a field compares with the first default field only.
    let ranged = matches!(term, LTerm::Range { .. } | LTerm::OneSided { .. });
    let used = if ranged && field.is_none() {
        &targets[..1]
    } else {
        &targets[..]
    };
    let written = field.unwrap_or("text");
    let mut parts = Vec::with_capacity(used.len());
    for t in used {
        parts.push(predicate(t, term, written, o)?);
    }
    Ok(join("OR", parts))
}

fn node_sql(node: &LNode, defaults: &[Target], o: &mut Out) -> Result<Vec<Piece>, SearchError> {
    match node {
        LNode::Clause { field, term } => clause_sql(field.as_deref(), term, defaults, o),
        LNode::Not(inner) => {
            let mut out = vec![text("(")];
            out.extend(node_sql(inner, defaults, o)?);
            out.push(text(" IS NOT TRUE)"));
            Ok(out)
        }
        LNode::And(items) => {
            let mut parts = Vec::with_capacity(items.len());
            for item in items {
                parts.push(node_sql(item, defaults, o)?);
            }
            Ok(join("AND", parts))
        }
        LNode::Or(items) => or_group(items, defaults, o),
    }
}

/// M3: in an `OR` group a negated clause excludes from the whole group,
/// as Lucene's `MUST_NOT` does (and as Alerta's `a OR NOT b` already reads,
/// F4): `a OR b OR -c` is `(a OR b) AND NOT c`, and a group of negations
/// only is their conjunction. Parameters keep the written order.
fn or_group(
    items: &[Arc<LNode>],
    defaults: &[Target],
    o: &mut Out,
) -> Result<Vec<Piece>, SearchError> {
    let mut positives = Vec::new();
    let mut negations = Vec::new();
    for item in items {
        let sql = node_sql(item, defaults, o)?;
        if matches!(**item, LNode::Not(_)) {
            negations.push(sql);
        } else {
            positives.push(sql);
        }
    }
    if negations.is_empty() {
        return Ok(join("OR", positives));
    }
    let mut parts = Vec::with_capacity(negations.len() + 1);
    if !positives.is_empty() {
        parts.push(join("OR", positives));
    }
    parts.extend(negations);
    Ok(join("AND", parts))
}

/// `q` in `q.mode=lucene`, with its `q.df` and `q.op`: the mode's checks in
/// order (`q.op`, syntax, restricted keys, `q.df`, fields and values), then
/// the translation.
///
/// # Errors
///
/// The first failed check.
pub fn compile(
    q: &str,
    default_field: Option<&str>,
    op: Option<&str>,
) -> Result<SearchSql, SearchError> {
    let op = match op.filter(|v| !v.is_empty()) {
        None | Some("AND") => DefaultOperator::And,
        Some("OR") => DefaultOperator::Or,
        Some(other) => return Err(SearchError::DefaultOperator(other.to_owned())),
    };
    let node = parse(q, op)?;
    if names_restricted_key(&node) {
        return Err(SearchError::Restricted);
    }
    let defaults = match default_field.filter(|d| !d.is_empty()) {
        None => default_fields(),
        Some(df) => match resolve(df) {
            Some(Target::Attribute(k)) if is_restricted_attribute(&k) => {
                return Err(SearchError::Restricted);
            }
            Some(t) if t != Target::Exists => vec![t],
            _ => return Err(SearchError::DefaultField(df.to_owned())),
        },
    };
    let mut o = Out { params: Vec::new() };
    let pieces = node_sql(&node, &defaults, &mut o)?;
    if o.params.iter().any(|p| p.contains('\0')) {
        return Err(SearchError::Parse);
    }
    Ok(SearchSql {
        pieces,
        params: o.params,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn w(s: &str) -> Arc<LNode> {
        Arc::new(LNode::Clause {
            field: None,
            term: LTerm::Word(s.to_owned()),
        })
    }

    fn and(items: Vec<Arc<LNode>>) -> Arc<LNode> {
        Arc::new(LNode::And(items))
    }

    fn or(items: Vec<Arc<LNode>>) -> Arc<LNode> {
        Arc::new(LNode::Or(items))
    }

    fn not(inner: Arc<LNode>) -> Arc<LNode> {
        Arc::new(LNode::Not(inner))
    }

    fn p(q: &str) -> Result<Arc<LNode>, SearchError> {
        parse(q, DefaultOperator::And)
    }

    fn term(q: &str) -> LTerm {
        match &*p(q).unwrap() {
            LNode::Clause { term, .. } => term.clone(),
            other => panic!("{q}: {other:?}"),
        }
    }

    fn sql(q: &str) -> (String, Vec<String>) {
        let s = compile(q, None, None).unwrap();
        (s.reference_text(), s.params)
    }

    /// M1, M2: the implicit operator has the precedence of the one it
    /// stands for; every operand is kept.
    #[test]
    fn implicit_operator_and_precedence() {
        assert_eq!(p("disk full").unwrap(), and(vec![w("disk"), w("full")]));
        assert_eq!(
            parse("disk full", DefaultOperator::Or).unwrap(),
            or(vec![w("disk"), w("full")])
        );
        assert_eq!(
            p("a OR b c").unwrap(),
            or(vec![w("a"), and(vec![w("b"), w("c")])])
        );
        assert_eq!(
            parse("a AND b c", DefaultOperator::Or).unwrap(),
            or(vec![and(vec![w("a"), w("b")]), w("c")])
        );
        assert_eq!(
            p("a b c d").unwrap(),
            and(vec![w("a"), w("b"), w("c"), w("d")])
        );
        assert_eq!(p("NOT a OR b").unwrap(), or(vec![not(w("a")), w("b")]));
    }

    /// M3: `-` and `!` and `NOT` negate, `+` requires.
    #[test]
    fn negation_and_requirement() {
        for q in ["-disk", "!disk", "NOT disk"] {
            assert_eq!(p(q).unwrap(), not(w("disk")), "{q}");
        }
        assert_eq!(p("+disk").unwrap(), w("disk"));
        assert_eq!(
            p("+disk -server").unwrap(),
            and(vec![w("disk"), not(w("server"))])
        );
        assert_eq!(p("a-b").unwrap(), w("a-b"));
        assert_eq!(p("-"), Err(SearchError::Parse));
    }

    /// M4, M5: operators are never terms, and the whole input must parse.
    #[test]
    fn operators_are_not_terms_and_nothing_is_dropped() {
        for q in [
            "disk AND", "AND", "OR disk", "disk )", "(disk", "", "  ", "a:",
        ] {
            assert_eq!(p(q), Err(SearchError::Parse), "{q:?}");
        }
        assert_eq!(
            p("disk and full").unwrap(),
            and(vec![w("disk"), w("and"), w("full")])
        );
        assert_eq!(p("ANDROID").unwrap(), w("ANDROID"));
        assert_eq!(p("\\AND").unwrap(), w("AND"));
    }

    /// M6, M9: escapes and wildcard terms; non-ASCII letters are term text.
    #[test]
    fn escapes_wildcards_and_letters() {
        assert_eq!(term("web\\-01"), LTerm::Word("web-01".into()));
        assert_eq!(term("a\\:b"), LTerm::Word("a:b".into()));
        assert_eq!(term("\\*"), LTerm::Word("*".into()));
        assert_eq!(term("résumé"), LTerm::Word("résumé".into()));
        assert_eq!(term("10.0.0.1"), LTerm::Word("10.0.0.1".into()));
        assert_eq!(
            term("Dis*"),
            LTerm::Pattern(vec![Part::Literal("Dis".into()), Part::AnyRun])
        );
        assert_eq!(
            term("d?sk"),
            LTerm::Pattern(vec![
                Part::Literal("d".into()),
                Part::AnyOne,
                Part::Literal("sk".into())
            ])
        );
        assert_eq!(p("a\\"), Err(SearchError::Parse));
        match &*p("attributes.zabbix_ack_sync_inten\\t:x").unwrap() {
            LNode::Clause { field, .. } => {
                assert_eq!(field.as_deref(), Some("attributes.zabbix_ack_sync_intent"));
            }
            other => panic!("{other:?}"),
        }
    }

    /// M8, M10, M15, M16: phrases, regexes, quoted bounds and the refused
    /// Lucene operators.
    #[test]
    fn phrases_regexes_bounds_and_refused_operators() {
        assert_eq!(term("\"a \\\"b\\\" c\""), LTerm::Phrase("a \"b\" c".into()));
        assert_eq!(term("/a\\/b\\d/"), LTerm::Regex("a/b\\d".into()));
        assert_eq!(
            term("lastReceiveTime:>\"2026-11-01T00:30:00\""),
            LTerm::OneSided {
                op: ">",
                bound: "2026-11-01T00:30:00".into()
            }
        );
        assert_eq!(
            term("timeout:[* TO 60}"),
            LTerm::Range {
                lower: LBound {
                    value: None,
                    inclusive: true
                },
                upper: LBound {
                    value: Some("60".into()),
                    inclusive: false
                },
            }
        );
        assert_eq!(p("disk~2"), Err(SearchError::Operator('~')));
        assert_eq!(p("disk^2"), Err(SearchError::Operator('^')));
        assert_eq!(p("\"disk full\"~3"), Err(SearchError::Operator('~')));
        assert!(p("\"a~b^c\"").is_ok());
        assert!(p("/a~b/").is_ok());
    }

    /// M7, M11, M14: a term is an escaped substring on the default text
    /// fields and an exact element of the default arrays; a negation is
    /// `IS NOT TRUE`, so NULL fields do not hide an alert.
    #[test]
    fn a_term_on_the_default_fields() {
        let (text, params) = sql("disk");
        let like = |n: usize, c: &str| format!("\"{c}\" ILIKE %(_qp_{n})s ESCAPE '\\'");
        let element = |n: usize, c: &str| {
            format!(
                "EXISTS (SELECT 1 FROM unnest(\"{c}\") AS e(v) WHERE e.v ILIKE %(_qp_{n})s ESCAPE '\\')"
            )
        };
        assert_eq!(
            text,
            format!(
                "({} OR {} OR {} OR {} OR {} OR {} OR {})",
                like(0, "text"),
                like(1, "resource"),
                like(2, "event"),
                element(3, "service"),
                element(4, "tags"),
                like(5, "group"),
                like(6, "value")
            )
        );
        assert_eq!(
            params,
            [
                "%disk%", "%disk%", "%disk%", "disk", "disk", "%disk%", "%disk%"
            ]
        );
        let (text, _) = sql("-disk");
        assert!(
            text.starts_with("((") && text.ends_with(") IS NOT TRUE)"),
            "{text}"
        );
        let one = compile("disk", Some("resource"), None).unwrap();
        assert_eq!(
            one.reference_text(),
            "\"resource\" ILIKE %(_qp_0)s ESCAPE '\\'"
        );
    }

    /// M7 to M9: what the patterns are.
    #[test]
    fn patterns_are_literal_and_word_anchored() {
        let first = |q: &str| compile(q, Some("text"), None).unwrap().params.remove(0);
        assert_eq!(first("100%"), "%100\\%%");
        assert_eq!(first("a_b"), "%a\\_b%");
        // An escaped backslash is a literal backslash, escaped again for ILIKE.
        assert_eq!(first("c\\\\x"), "%c\\\\x%");
        assert_eq!(
            first("d?sk"),
            "(^|[^[:alnum:]_])d[^[:space:]]sk([^[:alnum:]_]|$)"
        );
        assert_eq!(first("dis*"), "(^|[^[:alnum:]_])dis[^[:space:]]*");
        assert_eq!(
            first("\"disk.full\""),
            "(^|[^[:alnum:]_])disk\\.full([^[:alnum:]_]|$)"
        );
        assert_eq!(first("/^disk$/"), "^disk$");
    }

    /// M12, M13, M15: field names, typed values, unsearchable fields.
    #[test]
    fn fields_and_typed_values() {
        let s = compile("duplicateCount:>1", None, None).unwrap();
        assert_eq!(s.reference_text(), "(\"duplicate_count\" > %(_qp_0)s)");
        assert!(s.pieces.iter().any(|p| matches!(
            p,
            Piece::Param { compare: Some(c), .. } if c == "duplicate_count"
        )));
        assert_eq!(sql("timeout:30").0, "(\"timeout\" = %(_qp_0)s)");
        assert_eq!(
            sql("lastReceiveTime:>\"2026-11-01T00:30:00\"").0,
            "(\"last_receive_time\" > %(_qp_0)s)"
        );
        assert_eq!(
            sql("_.region:eu"),
            (
                "(\"attributes\" ->> %(_qp_0)s) ILIKE %(_qp_1)s ESCAPE '\\'".into(),
                vec!["region".into(), "%eu%".into()]
            )
        );
        assert_eq!(sql("_exists_:region").0, "(\"attributes\" ? %(_qp_0)s)");
        assert!(
            sql("service:SVC")
                .0
                .starts_with("EXISTS (SELECT 1 FROM unnest(\"service\")")
        );
        assert!(compile("rawData:x", None, None).is_ok());
        for (q, err) in [
            ("history:ack", SearchError::Field("history".into())),
            ("attributes:x", SearchError::Field("attributes".into())),
            ("nosuch:x", SearchError::Field("nosuch".into())),
            ("raw.x:y", SearchError::Field("raw.x".into())),
            (
                "timeout:abc",
                SearchError::Value {
                    field: "timeout".into(),
                    value: "abc".into(),
                },
            ),
            (
                "duplicateCount:[1 TO x]",
                SearchError::Value {
                    field: "duplicateCount".into(),
                    value: "x".into(),
                },
            ),
            (
                "createTime:2026-13-01",
                SearchError::Value {
                    field: "createTime".into(),
                    value: "2026-13-01".into(),
                },
            ),
        ] {
            assert_eq!(compile(q, None, None), Err(err), "{q}");
        }
    }

    /// Hook 4.2 after parsing: escapes cannot hide a restricted key.
    #[test]
    fn restricted_keys_are_refused_after_unescaping() {
        for q in [
            "attributes.zabbix_ack_sync_inten\\t:x",
            "_.zabbix_ack_sync_inten\\t:x",
            "_exists_:zabbix_ack_sync_inten\\t",
            "_exists_:\"zabbix_ack_sync_intent\"",
            "attributes.zabbix_ack_sync_intent:(a OR b)",
            "disk OR -attributes.zabbix_ack_sync_intent:x",
        ] {
            assert_eq!(compile(q, None, None), Err(SearchError::Restricted), "{q}");
        }
        assert_eq!(
            compile("x", Some("attributes.zabbix_ack_sync_intent"), None),
            Err(SearchError::Restricted)
        );
        assert!(compile("attributes.zabbix_ack_sync:x", None, None).is_ok());
        assert_eq!(
            crate::search::compile("_exists_:zabbix_ack_sync_intent", None),
            Err(SearchError::Restricted)
        );
    }

    /// The order of the checks and the surface.
    #[test]
    fn checks_in_order() {
        use crate::search::compile_mode;
        assert_eq!(
            compile_mode("disk", None, Some("bogus"), Some("XOR")),
            Err(SearchError::Mode("bogus".into()))
        );
        assert_eq!(
            compile_mode("(disk", None, Some("lucene"), Some("XOR")),
            Err(SearchError::DefaultOperator("XOR".into()))
        );
        assert_eq!(
            compile_mode("(disk", Some("bogus"), Some("lucene"), None),
            Err(SearchError::Parse)
        );
        assert_eq!(
            compile_mode("nosuch:x", Some("bogus"), Some("lucene"), None),
            Err(SearchError::DefaultField("bogus".into()))
        );
        assert_eq!(
            compile_mode("x", Some("_exists_"), Some("lucene"), None),
            Err(SearchError::DefaultField("_exists_".into()))
        );
        // The default mode ignores `q.op`, and an empty mode is the default.
        let default = crate::search::compile("disk full", None).unwrap();
        assert_eq!(
            compile_mode("disk full", None, None, Some("XOR")),
            Ok(default.clone())
        );
        assert_eq!(
            compile_mode("disk full", None, Some(""), None),
            Ok(default.clone())
        );
        assert_eq!(
            compile_mode("disk full", None, Some("alerta"), None),
            Ok(default)
        );
        assert_eq!(compile("\"a\u{0}b\"", None, None), Err(SearchError::Parse));
    }

    /// M3: in an `OR` group a negation excludes from the whole group.
    #[test]
    fn negations_exclude_from_an_or_group() {
        let t = |n: usize| format!("\"text\" ILIKE %(_qp_{n})s ESCAPE '\\'");
        let not = |n: usize| format!("({} IS NOT TRUE)", t(n));
        let text =
            |q: &str, op: Option<&str>| compile(q, Some("text"), op).unwrap().reference_text();
        assert_eq!(
            text("full -disk", Some("OR")),
            format!("({} AND {})", t(0), not(1))
        );
        assert_eq!(
            text("a b -c", Some("OR")),
            format!("(({} OR {}) AND {})", t(0), t(1), not(2))
        );
        assert_eq!(
            text("-a -b", Some("OR")),
            format!("({} AND {})", not(0), not(1))
        );
        assert_eq!(
            text("a OR NOT b", None),
            format!("({} AND {})", t(0), not(1))
        );
        assert_eq!(text("a OR b", None), format!("({} OR {})", t(0), t(1)));
    }

    #[test]
    fn nesting_is_capped() {
        let nested = |n: usize| format!("{}a{}", "(".repeat(n), ")".repeat(n));
        assert!(p(&nested(31)).is_ok());
        assert_eq!(p(&nested(32)), Err(SearchError::Parse));
    }
}
