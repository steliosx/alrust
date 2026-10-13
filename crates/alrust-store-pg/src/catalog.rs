//! The reference catalog check (ADR-0005 decision 2).
//!
//! `reference-catalog.json` describes what the pinned reference `schema.sql`
//! creates on PostgreSQL 18.6: the `history` type and the ten tables, with
//! their columns in order, constraints and indexes. Start-up compares the
//! live database with it and refuses to serve on any difference. Excluded,
//! by design: `_sqlx_migrations`, other schemas, ownership and privileges,
//! and non-internal triggers (integrations attach their own).

use serde::{Deserialize, Serialize};
use sqlx::{PgConnection, Row};
use std::collections::BTreeMap;

/// The reference's tables, in `schema.sql` order.
pub const TABLES: [&str; 10] = [
    "alerts",
    "notes",
    "blackouts",
    "customers",
    "heartbeats",
    "keys",
    "metrics",
    "perms",
    "users",
    "groups",
];

/// The composite type the integrations read as `history[1]`.
pub const HISTORY_TYPE: &str = "history";

const EXPECTED_JSON: &str = include_str!("../reference-catalog.json");

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Column {
    pub name: String,
    #[serde(rename = "type")]
    pub data_type: String,
    pub not_null: bool,
    pub default: Option<String>,
    /// `pg_attribute.attidentity` and `attgenerated` ("" when unset).
    pub identity: String,
    pub generated: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Constraint {
    pub name: String,
    pub kind: String,
    pub definition: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Index {
    pub name: String,
    pub definition: String,
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct Table {
    /// In `attnum` order, dropped columns skipped.
    pub columns: Vec<Column>,
    /// Sorted by name.
    pub constraints: Vec<Constraint>,
    /// Sorted by name.
    pub indexes: Vec<Index>,
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct Catalog {
    /// The `history` type's attributes in order (`None` when absent).
    pub history_type: Option<Vec<Column>>,
    /// The reference tables that exist, by name.
    pub tables: BTreeMap<String, Table>,
}

/// The committed expected catalog.
pub fn expected() -> Catalog {
    serde_json::from_str(EXPECTED_JSON).expect("reference-catalog.json is valid")
}

/// Read the catalog of the reference objects in schema `public`.
///
/// Runs `SET LOCAL search_path` so that `format_type` and the definition
/// functions qualify names the same way everywhere; call it inside a
/// transaction.
pub async fn read(conn: &mut PgConnection) -> Result<Catalog, sqlx::Error> {
    sqlx::query("SET LOCAL search_path TO public, pg_catalog")
        .execute(&mut *conn)
        .await?;
    let mut names: Vec<&str> = TABLES.to_vec();
    names.push(HISTORY_TYPE);
    let rows = sqlx::query(
        "SELECT c.relname, c.relkind::text AS relkind, a.attname,
                format_type(a.atttypid, a.atttypmod) AS data_type, a.attnotnull,
                pg_get_expr(d.adbin, d.adrelid) AS def,
                a.attidentity::text AS ident, a.attgenerated::text AS gen
           FROM pg_class c
           JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
           LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
          WHERE c.relnamespace = 'public'::regnamespace AND c.relname = ANY($1)
            AND c.relkind IN ('r', 'c')
          ORDER BY c.relname, a.attnum",
    )
    .bind(&names)
    .fetch_all(&mut *conn)
    .await?;
    let mut cat = Catalog::default();
    for r in rows {
        let relname: String = r.try_get("relname")?;
        let relkind: String = r.try_get("relkind")?;
        let col = Column {
            name: r.try_get("attname")?,
            data_type: r.try_get("data_type")?,
            not_null: r.try_get("attnotnull")?,
            default: r.try_get("def")?,
            identity: r.try_get("ident")?,
            generated: r.try_get("gen")?,
        };
        match (relkind.as_str(), relname.as_str()) {
            ("c", HISTORY_TYPE) => cat.history_type.get_or_insert_with(Vec::new).push(col),
            ("r", name) if TABLES.contains(&name) => {
                cat.tables.entry(relname).or_default().columns.push(col)
            }
            _ => {}
        }
    }
    let rows = sqlx::query(
        "SELECT c.relname, con.conname, con.contype::text AS kind,
                pg_get_constraintdef(con.oid) AS def
           FROM pg_constraint con JOIN pg_class c ON c.oid = con.conrelid
          WHERE c.relnamespace = 'public'::regnamespace AND c.relkind = 'r' AND c.relname = ANY($1)
          ORDER BY c.relname, con.conname",
    )
    .bind(&TABLES[..])
    .fetch_all(&mut *conn)
    .await?;
    for r in rows {
        let relname: String = r.try_get("relname")?;
        cat.tables
            .entry(relname)
            .or_default()
            .constraints
            .push(Constraint {
                name: r.try_get("conname")?,
                kind: r.try_get("kind")?,
                definition: r.try_get("def")?,
            });
    }
    let rows = sqlx::query(
        "SELECT c.relname, i.relname AS iname, pg_get_indexdef(i.oid) AS def
           FROM pg_index x
           JOIN pg_class i ON i.oid = x.indexrelid
           JOIN pg_class c ON c.oid = x.indrelid
          WHERE c.relnamespace = 'public'::regnamespace AND c.relkind = 'r' AND c.relname = ANY($1)
          ORDER BY c.relname, i.relname",
    )
    .bind(&TABLES[..])
    .fetch_all(&mut *conn)
    .await?;
    for r in rows {
        let relname: String = r.try_get("relname")?;
        cat.tables.entry(relname).or_default().indexes.push(Index {
            name: r.try_get("iname")?,
            definition: r.try_get("def")?,
        });
    }
    Ok(cat)
}

/// Non-internal triggers on the reference tables, as "table: trigger".
/// They are allowed (integrations attach their own) and only logged.
pub async fn triggers(conn: &mut PgConnection) -> Result<Vec<String>, sqlx::Error> {
    sqlx::query_scalar(
        "SELECT c.relname || ': ' || t.tgname
           FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid
          WHERE NOT t.tgisinternal AND c.relnamespace = 'public'::regnamespace
            AND c.relname = ANY($1)
          ORDER BY 1",
    )
    .bind(&TABLES[..])
    .fetch_all(&mut *conn)
    .await
}

/// Every difference between the expected and the actual catalog, one line
/// each. Empty means exactly equal.
pub fn diff(expected: &Catalog, actual: &Catalog) -> Vec<String> {
    let mut out = Vec::new();
    match (&expected.history_type, &actual.history_type) {
        (Some(_), None) => out.push("type history is missing".to_string()),
        (None, Some(_)) => out.push("type history is unexpected".to_string()),
        (Some(e), Some(a)) => diff_columns("type history", e, a, &mut out),
        (None, None) => {}
    }
    for name in TABLES {
        match (expected.tables.get(name), actual.tables.get(name)) {
            (Some(_), None) => out.push(format!("table {name} is missing")),
            (None, Some(_)) => out.push(format!("table {name} is unexpected")),
            (Some(e), Some(a)) => {
                diff_columns(&format!("table {name}"), &e.columns, &a.columns, &mut out);
                diff_named(
                    &format!("table {name} constraint"),
                    e.constraints.iter().map(|c| (&c.name, c)),
                    a.constraints.iter().map(|c| (&c.name, c)),
                    &mut out,
                );
                diff_named(
                    &format!("table {name} index"),
                    e.indexes.iter().map(|i| (&i.name, i)),
                    a.indexes.iter().map(|i| (&i.name, i)),
                    &mut out,
                );
            }
            (None, None) => {}
        }
    }
    out
}

fn diff_columns(what: &str, e: &[Column], a: &[Column], out: &mut Vec<String>) {
    if e == a {
        return;
    }
    let names = |cs: &[Column]| cs.iter().map(|c| c.name.clone()).collect::<Vec<_>>();
    if names(e) != names(a) {
        out.push(format!(
            "{what}: columns are [{}], expected [{}]",
            names(a).join(", "),
            names(e).join(", ")
        ));
        return;
    }
    for (ec, ac) in e.iter().zip(a) {
        if ec != ac {
            out.push(format!(
                "{what} column {}: is {ac:?}, expected {ec:?}",
                ec.name
            ));
        }
    }
}

fn diff_named<'a, T: PartialEq + std::fmt::Debug + 'a>(
    what: &str,
    e: impl Iterator<Item = (&'a String, &'a T)>,
    a: impl Iterator<Item = (&'a String, &'a T)>,
    out: &mut Vec<String>,
) {
    let e: BTreeMap<_, _> = e.collect();
    let a: BTreeMap<_, _> = a.collect();
    for (name, ev) in &e {
        match a.get(name) {
            None => out.push(format!("{what} {name} is missing")),
            Some(av) if av != ev => out.push(format!("{what} {name}: is {av:?}, expected {ev:?}")),
            Some(_) => {}
        }
    }
    for name in a.keys() {
        if !e.contains_key(name) {
            out.push(format!("{what} {name} is unexpected"));
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn expected_catalog_is_complete() {
        let c = expected();
        assert_eq!(c.history_type.as_ref().map(Vec::len), Some(10));
        for t in TABLES {
            assert!(c.tables.contains_key(t), "{t}");
        }
        let alerts = &c.tables["alerts"];
        assert_eq!(alerts.columns.len(), 28);
        assert_eq!(alerts.columns[26].name, "history");
        assert_eq!(alerts.columns[27].name, "update_time");
        assert_eq!(c.tables["heartbeats"].columns[8].name, "attributes");
        assert!(diff(&c, &c).is_empty());
    }

    #[test]
    fn diff_reports_order_and_extra_index() {
        let e = expected();
        let mut a = e.clone();
        a.tables.get_mut("alerts").unwrap().columns.swap(26, 27);
        a.tables.get_mut("alerts").unwrap().indexes.push(Index {
            name: "x".into(),
            definition: "CREATE INDEX x ON public.alerts USING btree (status)".into(),
        });
        a.tables.remove("groups");
        let d = diff(&e, &a);
        assert_eq!(d.len(), 3, "{d:#?}");
        assert!(d[0].starts_with("table alerts: columns are"));
        assert_eq!(d[1], "table alerts index x is unexpected");
        assert_eq!(d[2], "table groups is missing");
    }
}
