//! The top-N lists through the router, against PostgreSQL:
//! `GET /alerts/top10/…` and `/alerts/topn/…` for `count`, `flapping` and
//! `standing` (reference `views/alerts.py:405-483`, `postgres/base.py:556-634`;
//! `spec/security-fixes.md` SF-QUERY alrust item 2, vectors 6 to 12, on its
//! `TN` store).
//!
//! Each test creates its own database `<prefix>n<n>_<µs>` and drops it when
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
        "{prefix}n{}_{}",
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

/// SF-QUERY's `TN` store: three alerts in `Production`, service `svc`, no
/// customer, `create_time` T0. Each history has the stated number of
/// `severity` entries plus one `status` entry, which the filter excludes.
/// `t1`: `res-1`, `ev-a`, `grp-x`, `duplicate_count` 2, one severity entry,
/// `last_receive_time` T0 + 10 min; `t2`: `res-2`, `ev-a`, `grp-x`, 0, two,
/// T0 + 60 min; `t3`: `res-3`, `ev-b`, `grp-y`, 5, one, T0 + 30 min. All
/// have `tags` `{a}` (t3: NULL) and `repeat` false.
const TN: &str = r#"
INSERT INTO alerts (id, resource, event, environment, severity, status, service, "group", tags,
                    customer, duplicate_count, repeat, create_time, last_receive_time, history) VALUES
 ('t1', 'res-1', 'ev-a', 'Production', 'major', 'open', ARRAY['svc'], 'grp-x', ARRAY['a'], NULL, 2, false,
  '2026-11-01 00:00:00', '2026-11-01 00:10:00',
  ARRAY[ROW('t1', 'ev-a', 'major', 'open', NULL, 'new', 'severity', '2026-11-01 00:00:00', NULL, NULL)::history,
        ROW('t1', 'ev-a', 'major', 'ack', NULL, 'ack', 'status', '2026-11-01 00:05:00', NULL, NULL)::history]),
 ('t2', 'res-2', 'ev-a', 'Production', 'major', 'open', ARRAY['svc'], 'grp-x', ARRAY['a'], NULL, 0, false,
  '2026-11-01 00:00:00', '2026-11-01 01:00:00',
  ARRAY[ROW('t2', 'ev-a', 'minor', 'open', NULL, 'new', 'severity', '2026-11-01 00:00:00', NULL, NULL)::history,
        ROW('t2', 'ev-a', 'major', 'open', NULL, 'up', 'severity', '2026-11-01 00:30:00', NULL, NULL)::history,
        ROW('t2', 'ev-a', 'major', 'ack', NULL, 'ack', 'status', '2026-11-01 00:40:00', NULL, NULL)::history]),
 ('t3', 'res-3', 'ev-b', 'Production', 'major', 'open', ARRAY['svc'], 'grp-y', NULL, NULL, 5, false,
  '2026-11-01 00:00:00', '2026-11-01 00:30:00',
  ARRAY[ROW('t3', 'ev-b', 'major', 'open', NULL, 'new', 'severity', '2026-11-01 00:00:00.123456', NULL, NULL)::history,
        ROW('t3', 'ev-b', 'major', 'ack', NULL, 'ack', 'status', '2026-11-01 00:10:00', NULL, NULL)::history])
"#;

async fn seeded() -> Option<(TestDb, axum::Router)> {
    seeded_with(TN).await
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

fn names(list: &Value, key: &str) -> Vec<Value> {
    list.as_array()
        .unwrap()
        .iter()
        .map(|i| i[key].clone())
        .collect()
}

fn top(body: &Value) -> &Value {
    &body["top10"]
}

fn resource_ids(entry: &Value) -> Vec<Value> {
    let mut ids = names(&entry["resources"], "id");
    ids.sort_by_key(ToString::to_string);
    ids
}

/// Vectors 6, 7 and 8: the default `event` grouping of each list.
#[tokio::test]
async fn count_flapping_and_standing_by_event() {
    let Some((_db, app)) = seeded().await else {
        return;
    };
    // Vector 6: one row per alert and service.
    let (code, body) = get(&app, "/alerts/top10/count", READER_KEY).await;
    assert_eq!(code, StatusCode::OK, "{body}");
    assert_eq!(body["total"], 2);
    let first = &top(&body)[0];
    assert_eq!(
        (
            first["event"].clone(),
            first["count"].clone(),
            first["duplicateCount"].clone()
        ),
        (json!("ev-a"), json!(2), json!(2))
    );
    assert_eq!(resource_ids(first), vec![json!("t1"), json!("t2")]);
    assert_eq!(first["environments"], json!(["Production"]));
    assert_eq!(first["services"], json!(["svc"]));
    let second = &top(&body)[1];
    assert_eq!(
        (
            second["event"].clone(),
            second["count"].clone(),
            second["duplicateCount"].clone()
        ),
        (json!("ev-b"), json!(1), json!(5))
    );
    // Vectors 7 and 8: one row per severity entry; standing orders by the
    // summed lifetimes (130 and 30 minutes), which the answer does not hold.
    for path in ["/alerts/top10/flapping", "/alerts/top10/standing"] {
        let (code, body) = get(&app, path, READER_KEY).await;
        assert_eq!(code, StatusCode::OK, "{path}: {body}");
        let got: Vec<_> = top(&body)
            .as_array()
            .unwrap()
            .iter()
            .map(|e| {
                (
                    e["event"].clone(),
                    e["count"].clone(),
                    e["duplicateCount"].clone(),
                )
            })
            .collect();
        assert_eq!(
            got,
            vec![
                (json!("ev-a"), json!(3), json!(2)),
                (json!("ev-b"), json!(1), json!(5))
            ],
            "{path}"
        );
    }
}

/// Vectors 9 to 12: `group-by`.
#[tokio::test]
async fn group_by_takes_exact_column_names() {
    let Some((_db, app)) = seeded().await else {
        return;
    };
    // Vector 9: res-2 first (two severity entries), then res-1 and res-3.
    let (code, body) = get(&app, "/alerts/topn/flapping?group-by=resource", READER_KEY).await;
    assert_eq!(code, StatusCode::OK, "{body}");
    let entries = top(&body).as_array().unwrap();
    assert_eq!(
        (entries[0]["resource"].clone(), entries[0]["count"].clone()),
        (json!("res-2"), json!(2))
    );
    // The two with count 1 may come in either order: compare as a set.
    let mut rest: Vec<String> = entries[1..]
        .iter()
        .map(|e| format!("{}={}", e["resource"], e["count"]))
        .collect();
    rest.sort();
    assert_eq!(rest, vec![r#""res-1"=1"#, r#""res-3"=1"#]);
    assert!(entries[0].get("event").is_none());
    // Vector 10: `group` is a reserved word; alrust quotes it.
    let (code, body) = get(&app, "/alerts/top10/count?group-by=group", READER_KEY).await;
    assert_eq!(code, StatusCode::OK, "{body}");
    assert_eq!(
        names(top(&body), "group"),
        vec![json!("grp-x"), json!("grp-y")]
    );
    assert_eq!(names(top(&body), "count"), vec![json!(2), json!(1)]);
    // Vectors 11 and 12: anything but an exact column name is refused.
    for (path, value) in [
        (
            "/alerts/top10/standing?group-by=event,resource",
            "event,resource",
        ),
        ("/alerts/top10/count?group-by=Resource", "Resource"),
    ] {
        let (code, body) = get(&app, path, READER_KEY).await;
        assert_eq!(code, StatusCode::BAD_REQUEST, "{path}: {body}");
        assert_eq!(
            body["message"],
            format!("Grouping by '{value}' field not supported."),
            "{path}"
        );
    }
    // The first group-by value counts.
    let (_, body) = get(
        &app,
        "/alerts/top10/count?group-by=group&group-by=bogus",
        READER_KEY,
    )
    .await;
    assert_eq!(
        names(top(&body), "group"),
        vec![json!("grp-x"), json!("grp-y")]
    );
    // The restricted-attribute guard (hook spec 4.2) refuses group-by=attributes first.
    let (code, _) = get(&app, "/alerts/top10/count?group-by=attributes", READER_KEY).await;
    assert_eq!(code, StatusCode::BAD_REQUEST);
}

/// Values as the reference's database adapter and JSON encoder write them.
#[tokio::test]
async fn grouped_values_keep_the_reference_format() {
    let Some((_db, app)) = seeded().await else {
        return;
    };
    let (_, body) = get(&app, "/alerts/top10/count?group-by=create_time", ADMIN_KEY).await;
    assert_eq!(top(&body)[0]["create_time"], "2026-11-01T00:00:00.000Z");
    assert_eq!(top(&body)[0]["count"], 3);
    let (_, body) = get(&app, "/alerts/top10/count?group-by=repeat", ADMIN_KEY).await;
    assert_eq!(top(&body)[0]["repeat"], false);
    let (_, body) = get(&app, "/alerts/top10/count?group-by=tags", ADMIN_KEY).await;
    let tags: Vec<_> = top(&body)
        .as_array()
        .unwrap()
        .iter()
        .map(|e| (e["tags"].clone(), e["count"].clone()))
        .collect();
    assert_eq!(
        tags,
        vec![(json!(["a"]), json!(2)), (Value::Null, json!(1))]
    );
    // history: psycopg2's named tuples, a list of 10-field lists.
    let (_, body) = get(
        &app,
        "/alerts/top10/count?group-by=history&resource=res-3",
        ADMIN_KEY,
    )
    .await;
    assert_eq!(
        top(&body)[0]["history"],
        json!([
            [
                "t3",
                "ev-b",
                "major",
                "open",
                null,
                "new",
                "severity",
                "2026-11-01T00:00:00.123Z",
                null,
                null
            ],
            [
                "t3",
                "ev-b",
                "major",
                "ack",
                null,
                "ack",
                "status",
                "2026-11-01T00:10:00.000Z",
                null,
                null
            ]
        ])
    );
    // resources carry an href for each alert.
    let (_, body) = get(&app, "/alerts/topn/count?resource=res-1", ADMIN_KEY).await;
    assert_eq!(
        top(&body)[0]["resources"],
        json!([{"id": "t1", "resource": "res-1", "href": "http://localhost:8080/alert/t1"}])
    );
}

/// Paging, scope, customers and the empty answer.
#[tokio::test]
async fn paging_scope_customers_and_not_found() {
    let Some((_db, app)) = seeded().await else {
        return;
    };
    // `Page.from_params(args, 1)`: page-size limits, a later page is 416.
    let (_, body) = get(&app, "/alerts/top10/count?page-size=1", READER_KEY).await;
    assert_eq!(names(top(&body), "event"), vec![json!("ev-a")]);
    let (code, body) = get(&app, "/alerts/top10/count?page=2", READER_KEY).await;
    assert_eq!(code, StatusCode::RANGE_NOT_SATISFIABLE, "{body}");
    assert_eq!(body["message"], "page out of range: 1-1");
    // Both spellings are the same list.
    let (_, ten) = get(&app, "/alerts/top10/standing", READER_KEY).await;
    let (_, n) = get(&app, "/alerts/topn/standing", READER_KEY).await;
    assert_eq!(ten, n);
    // read:alerts is required.
    for path in [
        "/alerts/top10/count",
        "/alerts/topn/flapping",
        "/alerts/top10/standing",
    ] {
        let (code, _) = get(&app, path, HEARTBEAT_KEY).await;
        assert_eq!(code, StatusCode::FORBIDDEN, "{path}");
    }
    // A customer caller counts only its own alerts: here none.
    let (code, body) = get(&app, "/alerts/top10/count", ACME_KEY).await;
    assert_eq!(code, StatusCode::OK);
    assert_eq!(
        body,
        json!({"status": "ok", "message": "not found", "top10": [], "total": 0})
    );
}
