//! The grouped alert reads through the router, against PostgreSQL:
//! `GET /environments`, `/services`, `/alerts/groups` and `/alerts/tags`
//! (reference `views/alerts.py:486-593`, `postgres/base.py:637-741`;
//! `spec/openapi.yaml`; customer-mapping C20; SF-QUERY item 5 and vector 25).
//!
//! Each test creates its own database `<prefix>g<n>_<µs>` and drops it when
//! it ends, also on panic. Without `DATABASE_URL` the tests are skipped,
//! except under CI (`CI` set), where a missing database is a failure. Keys
//! and alerts are synthetic.
#![expect(
    clippy::unwrap_used,
    clippy::print_stderr,
    reason = "integration test: helpers unwrap, skips are printed, and each scenario stays in one function"
)]

use std::sync::Arc;
use std::sync::atomic::{AtomicU32, Ordering};

use alrust_api::{ApiConfig, AppState, router};
use alrust_domain::ApiKey;
use alrust_store_pg::{Store, StoreConfig};
use axum::body::Body;
use axum::http::{Request, StatusCode};
use chrono::Utc;
use serde_json::{Value, json};
use sqlx::Executor as _;
use sqlx::postgres::{PgConnectOptions, PgPool, PgPoolOptions};
use tower::ServiceExt as _;

static COUNTER: AtomicU32 = AtomicU32::new(0);

const ADMIN_KEY: &str = "synthetic-admin-key-000000000000000000000";
const ACME_KEY: &str = "synthetic-acme-key-0000000000000000000000";

struct TestDb {
    base: PgConnectOptions,
    name: String,
    pool: PgPool,
}

impl Drop for TestDb {
    fn drop(&mut self) {
        let admin = self.base.clone();
        let name = self.name.clone();
        let outcome = std::thread::spawn(move || -> Result<(), String> {
            let rt = tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()
                .map_err(|e| e.to_string())?;
            rt.block_on(async move {
                let pool = PgPoolOptions::new()
                    .max_connections(1)
                    .acquire_timeout(std::time::Duration::from_secs(10))
                    .connect_with(
                        admin.options([("statement_timeout", "20s"), ("lock_timeout", "10s")]),
                    )
                    .await
                    .map_err(|e| e.to_string())?;
                sqlx::query(&format!("DROP DATABASE IF EXISTS \"{name}\" WITH (FORCE)"))
                    .execute(&pool)
                    .await
                    .map_err(|e| e.to_string())?;
                pool.close().await;
                Ok(())
            })
        })
        .join()
        .unwrap_or_else(|_| Err("cleanup thread panicked".into()));
        if let Err(e) = outcome {
            let msg = format!("cleanup of test database {} failed: {e}", self.name);
            if std::thread::panicking() {
                eprintln!("{msg}");
            } else {
                panic!("{msg}");
            }
        }
    }
}

async fn test_db() -> Option<TestDb> {
    let url = match std::env::var("DATABASE_URL") {
        Ok(u) if !u.is_empty() => u,
        _ if std::env::var_os("CI").is_some() => panic!("DATABASE_URL is required under CI"),
        _ => {
            eprintln!("skipped: DATABASE_URL is not set");
            return None;
        }
    };
    let base: PgConnectOptions = url.parse().expect("DATABASE_URL");
    let prefix = std::env::var("ALRUST_TEST_DB_PREFIX")
        .unwrap_or_else(|_| format!("alrust_t_{}_", std::process::id()));
    assert!(
        prefix.starts_with("alrust_t_"),
        "bad ALRUST_TEST_DB_PREFIX {prefix:?}"
    );
    let name = format!(
        "{prefix}g{}_{}",
        COUNTER.fetch_add(1, Ordering::SeqCst),
        Utc::now().timestamp_micros()
    );
    {
        let admin = PgPoolOptions::new()
            .max_connections(1)
            .connect_with(base.clone())
            .await
            .expect("connect to DATABASE_URL");
        admin
            .execute(format!("CREATE DATABASE \"{name}\"").as_str())
            .await
            .expect("create test database");
        admin.close().await;
    }
    let pool = PgPoolOptions::new().max_connections(4).connect_lazy_with(
        base.clone()
            .options([("search_path", alrust_store_pg::SEARCH_PATH)])
            .database(&name),
    );
    Some(TestDb { base, name, pool })
}

const READER_KEY: &str = "synthetic-reader-key-00000000000000000000";
const HEARTBEAT_KEY: &str = "synthetic-heartbeat-key-00000000000000000";

async fn store(db: &TestDb) -> Store {
    let store = Store::new(db.pool.clone(), StoreConfig::default());
    store.prepare().await.unwrap();
    for (id, key, user, scopes, customer) in [
        ("k-admin", ADMIN_KEY, "admin", vec!["admin"], None),
        (
            "k-acme",
            ACME_KEY,
            "acme-reader",
            vec!["read"],
            Some("acme"),
        ),
        ("k-reader", READER_KEY, "reader", vec!["read"], None),
        ("k-hb", HEARTBEAT_KEY, "hb", vec!["read:heartbeats"], None),
    ] {
        store
            .ensure_key(&ApiKey {
                id: id.into(),
                key: key.into(),
                user: user.into(),
                scopes: Some(scopes.into_iter().map(str::to_owned).collect()),
                text: "synthetic".into(),
                expire_time: Some(Utc::now() + chrono::Duration::days(1)),
                count: 0,
                last_used_time: None,
                customer: customer.map(str::to_owned),
            })
            .await
            .unwrap();
    }
    store
}

/// Seven synthetic alerts, seeded directly. `q1`-`q6` follow the SF-QUERY
/// `QF` store's environments (`Production` five times, `Prod'uction` once);
/// `q5` belongs to customer `acme`, and `c1` to customer `other` in an
/// environment and service that no other alert uses (customer-mapping C20).
const SEED: &str = r#"
INSERT INTO alerts (id, resource, event, environment, severity, status, service, "group", tags, customer) VALUES
 ('q1', 'res-alpha',   'ev-1', 'Production',  'major',    'open',   ARRAY['svc'],       'grp-x', ARRAY['t1','t2'],  NULL),
 ('q2', 'res-bravo',   'ev-2', 'Production',  'major',    'open',   ARRAY['svc'],       'grp-x', ARRAY['t1'],       NULL),
 ('q3', 'res-charlie', 'ev-3', 'Production',  'minor',    'ack',    ARRAY['svc','db'],  'grp-y', NULL,               NULL),
 ('q4', 'res-delta',   'ev-4', 'Production',  'major',    'open',   ARRAY['svc'],       NULL,    ARRAY[]::text[],    NULL),
 ('q5', 'res-echo',    'ev-5', 'Production',  'critical', 'open',   ARRAY['svc'],       'grp-y', ARRAY['t2'],       'acme'),
 ('q6', 'res-foxtrot', 'ev-6', 'Prod''uction', 'major',   'open',   ARRAY['svc'],       'grp-x', ARRAY['t1'],       NULL),
 ('c1', 'res-golf',    'ev-7', 'OtherOnly',   'critical', 'closed', ARRAY['other-svc'], 'grp-z', ARRAY['t9'],       'other')
"#;

async fn seeded() -> Option<(TestDb, axum::Router)> {
    seeded_with(SEED).await
}

async fn seeded_with(sql: &str) -> Option<(TestDb, axum::Router)> {
    let db = test_db().await?;
    let store = store(&db).await;
    db.pool.execute(sql).await.unwrap();
    let app = router(Arc::new(AppState {
        store,
        cfg: ApiConfig {
            auth_required: true,
            ..ApiConfig::default()
        },
    }));
    Some((db, app))
}

async fn get(app: &axum::Router, uri: &str, key: &str) -> (StatusCode, Value) {
    let req = Request::builder()
        .method("GET")
        .uri(uri)
        .header("Authorization", format!("Key {key}"))
        .body(Body::empty())
        .unwrap();
    let resp = app.clone().oneshot(req).await.unwrap();
    let status = resp.status();
    let bytes = axum::body::to_bytes(resp.into_body(), usize::MAX)
        .await
        .unwrap();
    (
        status,
        serde_json::from_slice(&bytes).unwrap_or(Value::Null),
    )
}

/// The item of `list` whose `key` fields equal `want`.
fn item<'a>(list: &'a Value, want: &[(&str, Value)]) -> &'a Value {
    list.as_array()
        .unwrap()
        .iter()
        .find(|i| want.iter().all(|(k, v)| &i[*k] == v))
        .unwrap_or_else(|| panic!("no item {want:?} in {list}"))
}

fn names(list: &Value, key: &str) -> Vec<Value> {
    list.as_array()
        .unwrap()
        .iter()
        .map(|i| i[key].clone())
        .collect()
}

#[tokio::test]
async fn environments_list_every_name_and_count_the_matching_alerts() {
    let Some((_db, app)) = seeded().await else {
        return;
    };
    let (code, body) = get(&app, "/environments", ADMIN_KEY).await;
    assert_eq!(code, StatusCode::OK, "{body}");
    assert_eq!(body["total"], 3);
    assert_eq!(
        (body["page"].clone(), body["pages"].clone()),
        (json!(1), json!(1))
    );
    assert_eq!(body["more"], false);
    let envs = &body["environments"];
    assert_eq!(envs.as_array().unwrap().len(), 3);
    let prod = item(envs, &[("environment", json!("Production"))]);
    assert_eq!(prod["count"], 5);
    assert_eq!(
        prod["severityCounts"],
        json!({"major": 3, "minor": 1, "critical": 1})
    );
    assert_eq!(prod["statusCounts"], json!({"open": 4, "ack": 1}));
    // SF-QUERY vector 25: a name with a quote is listed and counted.
    let quoted = item(envs, &[("environment", json!("Prod'uction"))]);
    assert_eq!(quoted["count"], 1);
    assert_eq!(quoted["severityCounts"], json!({"major": 1}));
    // Ascending order (the quoted name's place depends on the collation).
    let order = names(envs, "environment");
    let pos = |n: &str| order.iter().position(|v| v == n).unwrap();
    assert!(pos("OtherOnly") < pos("Production"), "{order:?}");

    // A filter narrows the counts and `total`, not the names.
    let (_, body) = get(&app, "/environments?severity=major", ADMIN_KEY).await;
    assert_eq!(body["total"], 2);
    let envs = &body["environments"];
    assert_eq!(envs.as_array().unwrap().len(), 3);
    let prod = item(envs, &[("environment", json!("Production"))]);
    assert_eq!(prod["count"], 3);
    assert_eq!(prod["statusCounts"], json!({"open": 3}));
    let other = item(envs, &[("environment", json!("OtherOnly"))]);
    assert_eq!(other["count"], 0);
    assert_eq!(other["severityCounts"], json!({}));
}

#[tokio::test]
async fn a_customer_caller_sees_every_name_but_only_its_own_counts() {
    let Some((_db, app)) = seeded().await else {
        return;
    };
    // customer-mapping C20: names come from all alerts.
    let (code, body) = get(&app, "/environments", ACME_KEY).await;
    assert_eq!(code, StatusCode::OK, "{body}");
    assert_eq!(body["total"], 1);
    let envs = &body["environments"];
    assert_eq!(envs.as_array().unwrap().len(), 3);
    let prod = item(envs, &[("environment", json!("Production"))]);
    assert_eq!(prod["count"], 1);
    assert_eq!(prod["severityCounts"], json!({"critical": 1}));
    let other = item(envs, &[("environment", json!("OtherOnly"))]);
    assert_eq!(other["count"], 0);

    let (_, body) = get(&app, "/services", ACME_KEY).await;
    assert_eq!(body["total"], 4);
    let service_items = &body["services"];
    let own = item(
        service_items,
        &[
            ("environment", json!("Production")),
            ("service", json!("svc")),
        ],
    );
    assert_eq!(own["count"], 1);
    let foreign = item(
        service_items,
        &[
            ("environment", json!("OtherOnly")),
            ("service", json!("other-svc")),
        ],
    );
    assert_eq!(foreign["count"], 0);
    assert_eq!(foreign["statusCounts"], json!({}));

    // Groups and tags are filtered like the counts.
    let (_, body) = get(&app, "/alerts/groups", ACME_KEY).await;
    assert_eq!(
        body["groups"],
        json!([{"environment": "Production", "group": "grp-y", "count": 1}])
    );
    assert_eq!(body["total"], 1);
    let (_, body) = get(&app, "/alerts/tags", ACME_KEY).await;
    assert_eq!(
        body["tags"],
        json!([{"environment": "Production", "tag": "t2", "count": 1}])
    );
}

#[tokio::test]
async fn environment_pages_follow_the_reference_page_rules() {
    let Some((_db, app)) = seeded().await else {
        return;
    };
    let (_, first) = get(&app, "/environments?page-size=2", READER_KEY).await;
    assert_eq!(
        (first["pages"].clone(), first["more"].clone()),
        (json!(2), json!(true))
    );
    assert_eq!(first["environments"].as_array().unwrap().len(), 2);
    let (_, second) = get(&app, "/environments?page-size=2&page=2", READER_KEY).await;
    assert_eq!(
        (second["page"].clone(), second["more"].clone()),
        (json!(2), json!(false))
    );
    assert_eq!(second["environments"].as_array().unwrap().len(), 1);
    let mut all = names(&first["environments"], "environment");
    all.extend(names(&second["environments"], "environment"));
    all.sort_by_key(ToString::to_string);
    assert_eq!(
        all,
        vec![
            json!("OtherOnly"),
            json!("Prod'uction"),
            json!("Production")
        ]
    );
    // `utils/paging.py`: a page after the last is 416.
    let (code, body) = get(&app, "/environments?page-size=2&page=3", READER_KEY).await;
    assert_eq!(code, StatusCode::RANGE_NOT_SATISFIABLE, "{body}");
    assert_eq!(body["message"], "page out of range: 1-2");
}

#[tokio::test]
async fn services_groups_and_tags_count_the_matching_alerts() {
    let Some((_db, app)) = seeded().await else {
        return;
    };
    let (code, body) = get(&app, "/services", READER_KEY).await;
    assert_eq!(code, StatusCode::OK, "{body}");
    assert_eq!(body["total"], 4);
    let service_items = &body["services"];
    let web = item(
        service_items,
        &[
            ("environment", json!("Production")),
            ("service", json!("svc")),
        ],
    );
    assert_eq!(web["count"], 5);
    assert_eq!(web["statusCounts"], json!({"open": 4, "ack": 1}));
    let db = item(
        service_items,
        &[
            ("environment", json!("Production")),
            ("service", json!("db")),
        ],
    );
    assert_eq!(
        (db["count"].clone(), db["severityCounts"].clone()),
        (json!(1), json!({"minor": 1}))
    );

    let (_, body) = get(&app, "/alerts/groups", READER_KEY).await;
    assert_eq!(body["total"], 5);
    let groups = &body["groups"];
    for (env, group, n) in [
        ("Production", json!("grp-x"), 2),
        ("Production", json!("grp-y"), 2),
        ("Production", Value::Null, 1),
        ("Prod'uction", json!("grp-x"), 1),
        ("OtherOnly", json!("grp-z"), 1),
    ] {
        let g = item(groups, &[("environment", json!(env)), ("group", group)]);
        assert_eq!(g["count"], n, "{g}");
    }

    // NULL and empty tags add nothing.
    let (_, body) = get(&app, "/alerts/tags", READER_KEY).await;
    assert_eq!(body["total"], 4);
    let tags = &body["tags"];
    for (env, tag, n) in [
        ("Production", "t1", 2),
        ("Production", "t2", 2),
        ("Prod'uction", "t1", 1),
        ("OtherOnly", "t9", 1),
    ] {
        assert_eq!(
            item(tags, &[("environment", json!(env)), ("tag", json!(tag))])["count"],
            n
        );
    }

    // No match: an empty list with the reference's message.
    let (code, body) = get(&app, "/alerts/groups?environment=Nowhere", READER_KEY).await;
    assert_eq!(code, StatusCode::OK);
    assert_eq!(
        body,
        json!({"status": "ok", "message": "not found", "groups": [], "total": 0})
    );
}

#[tokio::test]
async fn the_grouped_reads_need_read_alerts_and_an_empty_store_is_not_found() {
    let Some(db) = test_db().await else {
        return;
    };
    let store = store(&db).await;
    let app = router(Arc::new(AppState {
        store,
        cfg: ApiConfig {
            auth_required: true,
            ..ApiConfig::default()
        },
    }));
    for uri in [
        "/environments",
        "/services",
        "/alerts/groups",
        "/alerts/tags",
    ] {
        let (code, body) = get(&app, uri, HEARTBEAT_KEY).await;
        assert_eq!(code, StatusCode::FORBIDDEN, "{uri}: {body}");
    }
    let (code, body) = get(&app, "/environments", READER_KEY).await;
    assert_eq!(code, StatusCode::OK);
    assert_eq!(body["environments"], json!([]));
    assert_eq!(body["message"], "not found");
    assert_eq!(
        (body["total"].clone(), body["pages"].clone()),
        (json!(0), json!(0))
    );
    assert_eq!(body["more"], false);
    let (_, body) = get(&app, "/services", READER_KEY).await;
    assert_eq!(
        body,
        json!({"status": "ok", "message": "not found", "services": [], "total": 0})
    );
}

/// Codex 7dda6a95 follow-up: a stored NULL or empty severity or status
/// counts as `normal` or `open`, together with the literal values, and
/// `/environments` agrees with `/alerts/count` on the same rows.
#[tokio::test]
async fn null_and_empty_severity_and_status_count_as_normal_and_open() {
    let Some((_db, app)) = seeded_with(
        r"
INSERT INTO alerts (id, resource, event, environment, severity, status, service) VALUES
 ('n1', 'r1', 'e', 'Production', NULL,     NULL,   ARRAY['svc']),
 ('n2', 'r2', 'e', 'Production', '',       '',     ARRAY['svc']),
 ('n3', 'r3', 'e', 'Production', 'normal', 'open', ARRAY['svc'])
",
    )
    .await
    else {
        return;
    };
    let (code, body) = get(&app, "/environments", READER_KEY).await;
    assert_eq!(code, StatusCode::OK, "{body}");
    let prod = item(
        &body["environments"],
        &[("environment", json!("Production"))],
    );
    assert_eq!(prod["count"], 3);
    assert_eq!(prod["severityCounts"], json!({"normal": 3}));
    assert_eq!(prod["statusCounts"], json!({"open": 3}));
    let (_, count) = get(&app, "/alerts/count", READER_KEY).await;
    assert_eq!(count["severityCounts"], prod["severityCounts"]);
    assert_eq!(count["statusCounts"], prod["statusCounts"]);
    let (_, body) = get(&app, "/services", READER_KEY).await;
    let svc = item(
        &body["services"],
        &[
            ("environment", json!("Production")),
            ("service", json!("svc")),
        ],
    );
    assert_eq!(
        (svc["count"].clone(), svc["severityCounts"].clone()),
        (json!(3), json!({"normal": 3}))
    );
}

/// Codex 7dda6a95 follow-up: the 1000-row limit applies to the grouped
/// SQL rows, not to alerts or names. Rows are kept in name order, so a cut
/// can leave a name with some counts and a zero total, and the names after
/// it with nothing.
#[tokio::test]
async fn the_grouped_row_limit_cuts_in_name_order() {
    // e000 has two severities: 6 cube rows (2 pairs, 2 severities, 1
    // status, 1 total). e001-e248 have 4 each (992). That leaves 2 rows for
    // e249 (its pair and its severity) and none for e250.
    let Some((_db, app)) = seeded_with(
        r"
INSERT INTO alerts (id, resource, event, environment, severity, status, service)
SELECT 'a' || i, 'r', 'e', 'e' || lpad(i::text, 3, '0'), 'major', 'open', ARRAY['svc']
FROM generate_series(0, 250) AS i
UNION ALL SELECT 'extra', 'r', 'e2', 'e000', 'minor', 'open', ARRAY['svc']
",
    )
    .await
    else {
        return;
    };
    let (code, body) = get(&app, "/environments", READER_KEY).await;
    assert_eq!(code, StatusCode::OK, "{body}");
    assert_eq!(body["total"], 251);
    let envs = &body["environments"];
    assert_eq!(envs.as_array().unwrap().len(), 251);
    let e000 = item(envs, &[("environment", json!("e000"))]);
    assert_eq!(e000["count"], 2);
    assert_eq!(e000["severityCounts"], json!({"major": 1, "minor": 1}));
    let e248 = item(envs, &[("environment", json!("e248"))]);
    assert_eq!(e248["count"], 1);
    let e249 = item(envs, &[("environment", json!("e249"))]);
    assert_eq!(e249["count"], 0);
    assert_eq!(e249["severityCounts"], json!({"major": 1}));
    assert_eq!(e249["statusCounts"], json!({}));
    let e250 = item(envs, &[("environment", json!("e250"))]);
    assert_eq!(e250["count"], 0);
    assert_eq!(e250["severityCounts"], json!({}));
}

/// The same limit on `/alerts/groups`: 1001 groups give the first 1000.
#[tokio::test]
async fn alert_groups_keep_the_first_1000_rows() {
    let Some((_db, app)) = seeded_with(
        r#"
INSERT INTO alerts (id, resource, event, environment, severity, status, service, "group")
SELECT 'g' || i, 'r' || i, 'e', 'Production', 'major', 'open', ARRAY['svc'], 'g' || lpad(i::text, 4, '0')
FROM generate_series(0, 1000) AS i
"#,
    )
    .await
    else {
        return;
    };
    let (code, body) = get(&app, "/alerts/groups", READER_KEY).await;
    assert_eq!(code, StatusCode::OK, "{body}");
    assert_eq!(body["total"], 1000);
    let groups = names(&body["groups"], "group");
    assert_eq!(groups.first(), Some(&json!("g0000")));
    assert_eq!(groups.last(), Some(&json!("g0999")));
    assert!(!groups.contains(&json!("g1000")));
}
