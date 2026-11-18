//! Blackout routes through the router, against PostgreSQL: `POST /blackout`,
//! `GET`/`PUT`/`DELETE /blackout/{id}`, `GET /blackouts` (reference
//! `views/blackouts.py`, `models/blackout.py`; `spec/openapi.yaml` Blackout;
//! `spec/security-fixes.md` SF-BLACKOUTS; customer-mapping C19).
//!
//! Each test creates its own database `<prefix>k<n>_<µs>` and drops it when
//! it ends, also on panic. Without `DATABASE_URL` the tests are skipped,
//! except under CI (`CI` set), where a missing database is a failure. Keys
//! and blackouts are synthetic.
#![expect(
    clippy::unwrap_used,
    clippy::print_stderr,
    clippy::too_many_lines,
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
const READER_KEY: &str = "synthetic-reader-key-00000000000000000000";
const BLK_ADMIN_KEY: &str = "synthetic-blkadmin-key-000000000000000000";

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
        "{prefix}k{}_{}",
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

async fn app() -> Option<(TestDb, axum::Router)> {
    let db = test_db().await?;
    let store = Store::new(db.pool.clone(), StoreConfig::default());
    store.prepare().await.unwrap();
    for (id, key, user, scopes, customer) in [
        ("k-admin", ADMIN_KEY, "admin", vec!["admin"], None),
        (
            "k-acme",
            ACME_KEY,
            "acme-writer",
            vec!["write:blackouts"],
            Some("acme"),
        ),
        ("k-reader", READER_KEY, "reader", vec!["read"], None),
        (
            "k-blk",
            BLK_ADMIN_KEY,
            "blk-admin",
            vec!["admin:blackouts"],
            Some("acme"),
        ),
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
    let app = router(Arc::new(AppState {
        store,
        cfg: ApiConfig {
            auth_required: true,
            ..ApiConfig::default()
        },
    }));
    Some((db, app))
}

async fn send(
    app: &axum::Router,
    method: &str,
    uri: &str,
    key: &str,
    body: Option<Value>,
) -> (StatusCode, Option<String>, Value) {
    let req = Request::builder()
        .method(method)
        .uri(uri)
        .header("Authorization", format!("Key {key}"));
    let req = match body {
        Some(b) => req
            .header("content-type", "application/json")
            .body(Body::from(b.to_string())),
        None => req.body(Body::empty()),
    }
    .unwrap();
    let resp = app.clone().oneshot(req).await.unwrap();
    let status = resp.status();
    let location = resp
        .headers()
        .get("location")
        .map(|v| v.to_str().unwrap().to_owned());
    let bytes = axum::body::to_bytes(resp.into_body(), usize::MAX)
        .await
        .unwrap();
    (
        status,
        location,
        serde_json::from_slice(&bytes).unwrap_or(Value::Null),
    )
}

/// The time `days` after the second `base`, in the reference format.
/// Every expected state (active, pending, expired) follows from the clock
/// of the run, not from a fixed date.
fn at(base: i64, days: i64) -> String {
    alrust_domain::iso8601(&chrono::DateTime::from_timestamp(base + days * 86_400, 0).unwrap())
}

/// `days` from now.
fn days(days: i64) -> String {
    at(Utc::now().timestamp(), days)
}

/// 30 days ago.
fn past() -> String {
    days(-30)
}

/// In 365 days.
fn soon() -> String {
    days(365)
}

/// A second store on the test database, for calls below the router.
fn store_of(db: &TestDb) -> Store {
    Store::new(db.pool.clone(), StoreConfig::default())
}

/// Create a blackout with `key`; returns the response body.
async fn create(app: &axum::Router, key: &str, body: Value) -> Value {
    let (code, _, out) = send(app, "POST", "/blackout", key, Some(body)).await;
    assert_eq!(code, StatusCode::CREATED, "{out}");
    out
}

fn id_of(created: &Value) -> String {
    created["id"].as_str().unwrap().to_owned()
}

#[tokio::test]
async fn a_blackout_is_created_and_read_back() {
    let Some((_db, app)) = app().await else {
        return;
    };
    let now = Utc::now().timestamp();
    let (past, soon) = (at(now, -30), at(now, 365));
    let (code, location, out) = send(
        &app,
        "POST",
        "/blackout",
        ADMIN_KEY,
        Some(json!({"environment": "Production", "resource": "r1", "startTime": past, "endTime": soon})),
    )
    .await;
    assert_eq!(code, StatusCode::CREATED, "{out}");
    let id = id_of(&out);
    assert_eq!(
        location.as_deref(),
        Some(format!("http://localhost:8080/blackout/{id}").as_str())
    );
    let b = &out["blackout"];
    let seconds = 395 * 86400; // 30 days before the run to 365 days after it
    assert_eq!(b["duration"], seconds);
    assert_eq!(b["priority"], 2);
    assert_eq!(b["status"], "active");
    assert_eq!(b["user"], "admin");
    // On creation `remaining` is the integer duration (`RETURNING *, duration AS remaining`).
    assert_eq!(b["remaining"], seconds);
    assert_eq!(
        (b["startTime"].clone(), b["endTime"].clone()),
        (json!(past), json!(soon))
    );
    assert_eq!(b["href"], format!("http://localhost:8080/blackout/{id}"));
    let (code, _, read) = send(&app, "GET", &format!("/blackout/{id}"), READER_KEY, None).await;
    assert_eq!(code, StatusCode::OK, "{read}");
    assert_eq!(read["total"], 1);
    // On reads it is the database's numeric, as text.
    assert!(read["blackout"]["remaining"].is_string(), "{read}");
    // Only environment: the default duration, priority 0.
    let plain = create(&app, ADMIN_KEY, json!({"environment": "Production"})).await;
    let b = &plain["blackout"];
    assert_eq!(
        (b["duration"].clone(), b["priority"].clone()),
        (json!(3600), json!(0))
    );
    assert_eq!(b["remaining"], 3600);
    let (code, _, out) = send(&app, "GET", "/blackout/nosuch", ADMIN_KEY, None).await;
    assert_eq!(
        (code, out["message"].clone()),
        (StatusCode::NOT_FOUND, json!("not found"))
    );
}

#[tokio::test]
async fn priority_status_and_the_creation_rules() {
    let Some((_db, app)) = app().await else {
        return;
    };
    for (extra, priority) in [
        (json!({"origin": "o", "resource": "r"}), 8),
        (json!({"service": ["s"]}), 3),
        (json!({"event": "e"}), 4),
        (json!({"group": "g"}), 5),
        (json!({"resource": "r", "event": "e"}), 6),
        (json!({"tags": ["t"]}), 7),
    ] {
        let mut body = json!({"environment": "Production"});
        body.as_object_mut()
            .unwrap()
            .extend(extra.as_object().unwrap().clone());
        assert_eq!(
            create(&app, ADMIN_KEY, body).await["blackout"]["priority"],
            priority,
            "{extra}"
        );
    }
    let pending = create(
        &app,
        ADMIN_KEY,
        json!({"environment": "Production", "startTime": days(180), "endTime": soon()}),
    )
    .await;
    assert_eq!(pending["blackout"]["status"], "pending");
    let expired = create(
        &app,
        ADMIN_KEY,
        json!({"environment": "Production", "startTime": days(-730), "endTime": days(-365)}),
    )
    .await;
    assert_eq!(expired["blackout"]["status"], "expired");
    let (_, _, read) = send(
        &app,
        "GET",
        &format!("/blackout/{}", id_of(&expired)),
        ADMIN_KEY,
        None,
    )
    .await;
    assert_eq!(read["blackout"]["remaining"], "0");
    for (body, message) in [
        (json!({}), "'environment'"),
        (
            json!({"environment": ""}),
            "Missing mandatory value for \"environment\"",
        ),
        (
            json!({"environment": "P", "service": "x"}),
            "service must be a list",
        ),
        (
            json!({"environment": "P", "tags": 5}),
            "tags must be a list",
        ),
        (
            json!({"environment": "P", "resource": ""}),
            "resource can not be an empty string",
        ),
        (
            json!({"environment": "P", "startTime": "soon"}),
            "dates must be ISO 8601 date format YYYY-MM-DDThh:mm:ss.sssZ",
        ),
    ] {
        let (code, _, out) = send(&app, "POST", "/blackout", ADMIN_KEY, Some(body.clone())).await;
        assert_eq!(
            (code, out["message"].clone()),
            (StatusCode::BAD_REQUEST, json!(message)),
            "{body}"
        );
    }
    let (code, _, _) = send(
        &app,
        "POST",
        "/blackout",
        READER_KEY,
        Some(json!({"environment": "P"})),
    )
    .await;
    assert_eq!(code, StatusCode::FORBIDDEN);
}

/// SF-BLACKOUTS vectors and the `PUT` lookup rule.
#[tokio::test]
async fn an_update_keeps_the_customer_unless_it_names_one() {
    let Some((_db, app)) = app().await else {
        return;
    };
    let acme = id_of(
        &create(
            &app,
            ADMIN_KEY,
            json!({"environment": "Production", "customer": "acme"}),
        )
        .await,
    );
    // 1: unscoped caller, no customer in the body: kept (the reference clears it).
    let (code, _, out) = send(
        &app,
        "PUT",
        &format!("/blackout/{acme}"),
        ADMIN_KEY,
        Some(json!({"text": "edited"})),
    )
    .await;
    assert_eq!(code, StatusCode::OK, "{out}");
    assert_eq!(
        (
            out["blackout"]["customer"].clone(),
            out["blackout"]["text"].clone()
        ),
        (json!("acme"), json!("edited"))
    );
    // 2: scoped caller, no customer: its own.
    let (code, _, out) = send(
        &app,
        "PUT",
        &format!("/blackout/{acme}"),
        ACME_KEY,
        Some(json!({"text": "again"})),
    )
    .await;
    assert_eq!(
        (code, out["blackout"]["customer"].clone()),
        (StatusCode::OK, json!("acme"))
    );
    assert_eq!(out["blackout"]["user"], "acme-writer");
    // 3: with customer: the reference's assign_customer rules.
    let (code, _, out) = send(
        &app,
        "PUT",
        &format!("/blackout/{acme}"),
        ADMIN_KEY,
        Some(json!({"customer": "other"})),
    )
    .await;
    assert_eq!(
        (code, out["blackout"]["customer"].clone()),
        (StatusCode::OK, json!("other"))
    );
    // The lookup: a scoped key sees only its customers' blackouts ...
    let (code, _, out) = send(
        &app,
        "PUT",
        &format!("/blackout/{acme}"),
        ACME_KEY,
        Some(json!({"text": "x"})),
    )
    .await;
    assert_eq!(
        (code, out["message"].clone()),
        (StatusCode::NOT_FOUND, json!("not found"))
    );
    // ... and admin:blackouts sees every blackout.
    let (code, _, _) = send(
        &app,
        "PUT",
        &format!("/blackout/{acme}"),
        BLK_ADMIN_KEY,
        Some(json!({"text": "x"})),
    )
    .await;
    assert_eq!(code, StatusCode::OK);
}

/// What `PUT` writes and refuses.
#[tokio::test]
async fn an_update_writes_what_the_reference_writes() {
    let Some((_db, app)) = app().await else {
        return;
    };
    let acme = id_of(
        &create(
            &app,
            ADMIN_KEY,
            json!({"environment": "Production", "customer": "acme"}),
        )
        .await,
    );
    // Duration only: written, but reads recompute it from the times.
    let (_, _, out) = send(
        &app,
        "PUT",
        &format!("/blackout/{acme}"),
        ADMIN_KEY,
        Some(json!({"duration": 5})),
    )
    .await;
    assert_eq!(out["blackout"]["duration"], 3600);
    for (body, message) in [
        (json!({}), "nothing to change"),
        (json!({"id": "x"}), "id cannot be changed"),
        (
            json!({"startTime": ""}),
            "dates must be ISO 8601 date format YYYY-MM-DDThh:mm:ss.sssZ",
        ),
        (
            json!({"startTime": "soon"}),
            "dates must be ISO 8601 date format YYYY-MM-DDThh:mm:ss.sssZ",
        ),
    ] {
        let (code, _, out) = send(
            &app,
            "PUT",
            &format!("/blackout/{acme}"),
            ADMIN_KEY,
            Some(body.clone()),
        )
        .await;
        assert_eq!(
            (code, out["message"].clone()),
            (StatusCode::BAD_REQUEST, json!(message)),
            "{body}"
        );
    }
}

/// customer-mapping C19, kept: per-id `GET` and `DELETE` take no customer
/// filter.
#[tokio::test]
async fn per_id_get_and_delete_take_no_customer_filter() {
    let Some((_db, app)) = app().await else {
        return;
    };
    let other = id_of(
        &create(
            &app,
            ADMIN_KEY,
            json!({"environment": "Production", "customer": "other"}),
        )
        .await,
    );
    let (code, _, out) = send(&app, "GET", &format!("/blackout/{other}"), ACME_KEY, None).await;
    assert_eq!(
        (code, out["blackout"]["customer"].clone()),
        (StatusCode::OK, json!("other"))
    );
    let (code, _, out) = send(
        &app,
        "DELETE",
        &format!("/blackout/{other}"),
        ACME_KEY,
        None,
    )
    .await;
    assert_eq!((code, out), (StatusCode::OK, json!({"status": "ok"})));
    let (code, _, _) = send(
        &app,
        "DELETE",
        &format!("/blackout/{other}"),
        ACME_KEY,
        None,
    )
    .await;
    assert_eq!(code, StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn the_list_filters_sorts_and_pages() {
    let Some((_db, app)) = app().await else {
        return;
    };
    create(&app, ADMIN_KEY, json!({"environment": "Production", "resource": "web01", "service": ["s"], "tags": ["t"], "customer": "acme", "startTime": past(), "endTime": soon()})).await;
    create(&app, ADMIN_KEY, json!({"environment": "Production", "origin": "o", "customer": "other", "startTime": days(180), "endTime": soon()})).await;
    create(&app, ADMIN_KEY, json!({"environment": "Development", "event": "e", "startTime": days(-730), "endTime": days(-365)})).await;
    let list = |uri: &str, key: &'static str| {
        let app = app.clone();
        let uri = uri.to_owned();
        async move { send(&app, "GET", &uri, key, None).await }
    };
    let (code, _, out) = list("/blackouts", ADMIN_KEY).await;
    assert_eq!(
        (code, out["total"].clone()),
        (StatusCode::OK, json!(3)),
        "{out}"
    );
    let (_, _, out) = list("/blackouts", ACME_KEY).await;
    assert_eq!(out["total"], 1);
    for (uri, total) in [
        ("/blackouts?status=active", 1),
        ("/blackouts?status=pending&status=expired", 2),
        ("/blackouts?environment=Development", 1),
        ("/blackouts?environment!=Development", 2),
        ("/blackouts?resource=~EB0", 1),
        ("/blackouts?priority=8", 1),
        ("/blackouts?priority=8&priority=9999", 1),
        // The exclusion list applies to the normalized name.
        ("/blackouts?id!=x&page.x=y", 3),
        // `service` is an overlap, also with an empty value among several.
        ("/blackouts?service=s&service=", 1),
        // `tag` and `service!` compare with one array literal, as the
        // reference's SQL does.
        ("/blackouts?tag=%7Bt%7D", 1),
        ("/blackouts?tag!=%7Bt%7D", 2),
        ("/blackouts?service!=%7Bs%7D", 2),
        // An empty first sort-by means no order was asked for.
        ("/blackouts?sort-by=&sort-by=bogus", 3),
        ("/blackouts?q=", 3),
        // A page or a size that is not an integer gives the default.
        ("/blackouts?page=abc&page-size=x&limit=y", 3),
    ] {
        let (code, _, out) = list(uri, ADMIN_KEY).await;
        assert_eq!(
            (code, out["total"].clone()),
            (StatusCode::OK, json!(total)),
            "{uri}: {out}"
        );
    }
    let (_, _, out) = list("/blackouts?sort-by=-priority", ADMIN_KEY).await;
    let priorities: Vec<Value> = out["blackouts"]
        .as_array()
        .unwrap()
        .iter()
        .map(|b| b["priority"].clone())
        .collect();
    assert_eq!(priorities, vec![json!(8), json!(4), json!(2)]);
    let (_, _, out) = list("/blackouts?page-size=2&page=2", ADMIN_KEY).await;
    assert_eq!(
        (
            out["pages"].clone(),
            out["more"].clone(),
            out["blackouts"].as_array().unwrap().len()
        ),
        (json!(2), json!(false), 1)
    );
    let (code, _, out) = list("/blackouts?page-size=2&page=3", ADMIN_KEY).await;
    assert_eq!(
        (code, out["message"].clone()),
        (
            StatusCode::RANGE_NOT_SATISFIABLE,
            json!("page out of range: 1-2")
        )
    );
    for (uri, message) in [
        ("/blackouts?bogus=1", "Invalid filter parameter: bogus"),
        (
            "/blackouts?tag=t",
            "a filter value is not valid for the type of its field",
        ),
        (
            "/blackouts?service!=s",
            "a filter value is not valid for the type of its field",
        ),
        (
            "/blackouts?priority=abc",
            "a filter value is not valid for the type of its field",
        ),
        (
            "/blackouts?startTime=never-o-clock",
            "a filter value is not valid for the type of its field",
        ),
        (
            "/blackouts?tag=%7Bt%7D&tag=%7Bu%7D",
            "Filtering by 'tag' field not supported.",
        ),
        (
            "/blackouts?tag=~t",
            "Filtering by 'tag' field not supported.",
        ),
        (
            "/blackouts?remaining=5",
            "Filtering by 'remaining' field not supported.",
        ),
        (
            "/blackouts?status!=active",
            "Filtering by 'status!' field not supported.",
        ),
        (
            "/blackouts?resource=a&resource=",
            "Filtering by 'resource' field not supported.",
        ),
        (
            "/blackouts?sort-by=priority&sort-by=",
            "Sorting by '' field not supported.",
        ),
        ("/blackouts?q=web01", "q queries are not supported yet"),
        (
            "/blackouts?page-size=-1",
            "page-size must be greater than 0",
        ),
        (
            "/blackouts?priority=~8",
            "Filtering by 'priority' field not supported.",
        ),
        (
            "/blackouts?sort-by=status",
            "Sorting by 'status' field not supported.",
        ),
        (
            "/blackouts?sort-by=id",
            "Sorting by 'id' field not supported.",
        ),
    ] {
        let (code, _, out) = list(uri, ADMIN_KEY).await;
        assert_eq!(
            (code, out["message"].clone()),
            (StatusCode::BAD_REQUEST, json!(message)),
            "{uri}"
        );
    }
    // A page below 1 is 416, as in the reference, also when nothing matches.
    for (uri, message) in [
        ("/blackouts?page=0", "page out of range: 1-1"),
        ("/blackouts?page=-3&page-size=2", "page out of range: 1-2"),
        (
            "/blackouts?page=0&environment=Nowhere",
            "page out of range: 1-0",
        ),
    ] {
        let (code, _, out) = list(uri, ADMIN_KEY).await;
        assert_eq!(
            (code, out["message"].clone()),
            (StatusCode::RANGE_NOT_SATISFIABLE, json!(message)),
            "{uri}"
        );
    }
    // A page size of 0 is the default size.
    let (code, _, out) = list("/blackouts?page-size=0", ADMIN_KEY).await;
    assert_eq!(
        (code, out["pageSize"].clone()),
        (
            StatusCode::OK,
            json!(ApiConfig::default().default_page_size)
        ),
        "{out}"
    );
    let (code, _, out) = list("/blackouts?environment=Nowhere", READER_KEY).await;
    assert_eq!(
        (code, out["message"].clone(), out["total"].clone()),
        (StatusCode::OK, json!("not found"), json!(0))
    );
}

/// `sort-by=remaining` orders by the number (default descending), not by
/// its text.
#[tokio::test]
async fn remaining_sorts_as_a_number() {
    let Some((_db, app)) = app().await else {
        return;
    };
    let near = id_of(
        &create(
            &app,
            ADMIN_KEY,
            json!({"environment": "Production", "startTime": past(), "endTime": soon()}),
        )
        .await,
    );
    let far = id_of(
        &create(
            &app,
            ADMIN_KEY,
            json!({"environment": "Production", "startTime": past(), "endTime": days(730)}),
        )
        .await,
    );
    let soonest = id_of(
        &create(
            &app,
            ADMIN_KEY,
            json!({"environment": "Production", "duration": 9}),
        )
        .await,
    );
    let (_, _, out) = send(&app, "GET", "/blackouts?sort-by=remaining", ADMIN_KEY, None).await;
    let ids: Vec<Value> = out["blackouts"]
        .as_array()
        .unwrap()
        .iter()
        .map(|b| b["id"].clone())
        .collect();
    assert_eq!(ids, vec![json!(far), json!(near), json!(soonest)]);
}

/// The customer rule is part of the write: a blackout that is not one of
/// the caller's customers' at the moment of the `UPDATE` is not changed,
/// its `user` included.
#[tokio::test]
async fn the_update_itself_carries_the_customer_rule() {
    use alrust_store_pg::StoreError;
    use alrust_store_pg::blackouts::{BlackoutUpdate, Change};
    let Some((db, app)) = app().await else {
        return;
    };
    let store = store_of(&db);
    let id = id_of(
        &create(
            &app,
            ADMIN_KEY,
            json!({"environment": "Production", "customer": "acme", "text": "first"}),
        )
        .await,
    );
    let edit = |text: &str| BlackoutUpdate {
        text: Change::Set(Some(text.to_owned())),
        user: Some("intruder".into()),
        ..BlackoutUpdate::default()
    };
    let read = || async {
        let (_, _, out) = send(&app, "GET", &format!("/blackout/{id}"), ADMIN_KEY, None).await;
        (
            out["blackout"]["text"].clone(),
            out["blackout"]["user"].clone(),
            out["blackout"]["customer"].clone(),
        )
    };
    // No lookup precedes this call: the statement itself refuses.
    let other = ["other".to_owned()];
    let refused = store
        .update_blackout(&id, Some(&other), edit("stolen"))
        .await;
    assert!(matches!(refused, Err(StoreError::NotFound)), "{refused:?}");
    assert_eq!(
        read().await,
        (json!("first"), json!("admin"), json!("acme"))
    );

    // A reassignment that commits while the write waits for the row: the
    // write sees the new customer, matches no row and changes nothing.
    let mut tx = db.pool.begin().await.unwrap();
    sqlx::query("UPDATE blackouts SET customer = 'other' WHERE id = $1")
        .bind(&id)
        .execute(&mut *tx)
        .await
        .unwrap();
    let waiting = {
        let (store, id) = (store.clone(), id.clone());
        tokio::spawn(async move {
            let acme = ["acme".to_owned()];
            store.update_blackout(&id, Some(&acme), edit("late")).await
        })
    };
    tokio::time::sleep(std::time::Duration::from_millis(500)).await;
    assert!(!waiting.is_finished(), "the write did not wait for the row");
    tx.commit().await.unwrap();
    let late = waiting.await.unwrap();
    assert!(matches!(late, Err(StoreError::NotFound)), "{late:?}");
    assert_eq!(
        read().await,
        (json!("first"), json!("admin"), json!("other"))
    );

    // No customer limit, and the matching customer: written.
    let done = store
        .update_blackout(&id, None, edit("open"))
        .await
        .unwrap();
    assert_eq!(done.text.as_deref(), Some("open"));
    let done = store
        .update_blackout(&id, Some(&other), edit("mine"))
        .await
        .unwrap();
    assert_eq!(
        (done.text.as_deref(), done.user.as_deref()),
        (Some("mine"), Some("intruder"))
    );
}

/// The reference's order on `PUT`: a falsy body is 400, then a blackout
/// the caller cannot see is 404, and only then is the body checked.
#[tokio::test]
async fn an_update_looks_the_blackout_up_before_it_checks_the_body() {
    let Some((_db, app)) = app().await else {
        return;
    };
    let other = id_of(
        &create(
            &app,
            ADMIN_KEY,
            json!({"environment": "Production", "customer": "other", "text": "kept"}),
        )
        .await,
    );
    for body in [
        json!({"startTime": "soon"}),
        json!({"customer": "elsewhere"}),
        json!({"customer": 5}),
        json!({"id": "x"}),
        json!({"environment": ""}),
        json!({"service": "not-a-list"}),
        json!([1]),
        json!("text"),
    ] {
        for uri in [format!("/blackout/{other}"), "/blackout/nosuch".to_owned()] {
            let (code, _, out) = send(&app, "PUT", &uri, ACME_KEY, Some(body.clone())).await;
            assert_eq!(
                (code, out["message"].clone()),
                (StatusCode::NOT_FOUND, json!("not found")),
                "{uri} {body}"
            );
        }
    }
    // A falsy body is refused first, whatever the id.
    for body in [
        json!({}),
        json!([]),
        json!(0),
        json!(""),
        json!(false),
        Value::Null,
    ] {
        let (code, _, out) = send(
            &app,
            "PUT",
            "/blackout/nosuch",
            ACME_KEY,
            Some(body.clone()),
        )
        .await;
        assert_eq!(
            (code, out["message"].clone()),
            (StatusCode::BAD_REQUEST, json!("nothing to change")),
            "{body}"
        );
    }
    // For a blackout the caller can see, the body is checked: the
    // customer first, then the fields. Nothing is written.
    let mine = id_of(&create(&app, ACME_KEY, json!({"environment": "Production"})).await);
    for (key, id, body, message) in [
        (
            ACME_KEY,
            &mine,
            json!({"customer": "elsewhere", "startTime": "soon"}),
            "not allowed to set customer to 'elsewhere'",
        ),
        (
            ADMIN_KEY,
            &other,
            json!({"customer": 5, "id": "x"}),
            "customer must be a string",
        ),
        (
            ADMIN_KEY,
            &other,
            json!({"id": "x"}),
            "id cannot be changed",
        ),
        (
            ADMIN_KEY,
            &other,
            json!({"environment": "", "text": "changed"}),
            "Missing mandatory value for \"environment\"",
        ),
        (
            ADMIN_KEY,
            &other,
            json!([1]),
            "blackout must be a JSON object",
        ),
    ] {
        let (code, _, out) = send(
            &app,
            "PUT",
            &format!("/blackout/{id}"),
            key,
            Some(body.clone()),
        )
        .await;
        assert_eq!(
            (code, out["message"].clone()),
            (StatusCode::BAD_REQUEST, json!(message)),
            "{body}"
        );
    }
    let (_, _, out) = send(&app, "GET", &format!("/blackout/{other}"), ADMIN_KEY, None).await;
    assert_eq!(
        (
            out["blackout"]["text"].clone(),
            out["blackout"]["user"].clone(),
            out["blackout"]["customer"].clone(),
            out["blackout"]["environment"].clone()
        ),
        (
            json!("kept"),
            json!("admin"),
            json!("other"),
            json!("Production")
        )
    );
}

/// `customer` in a body: absent, Python-falsy, a string, or refused. A
/// value of another type is never read as "absent".
#[tokio::test]
async fn a_customer_that_is_not_a_string_is_not_read_as_absent() {
    let Some((_db, app)) = app().await else {
        return;
    };
    // Create. A falsy value of any type is "none wanted": the scoped
    // caller gets its own customer, the admin none.
    for falsy in [
        json!(""),
        json!(0),
        json!(false),
        json!([]),
        json!({}),
        Value::Null,
    ] {
        let made = create(
            &app,
            ACME_KEY,
            json!({"environment": "Production", "customer": falsy}),
        )
        .await;
        assert_eq!(made["blackout"]["customer"], "acme", "{falsy}");
        let made = create(
            &app,
            ADMIN_KEY,
            json!({"environment": "Production", "customer": falsy}),
        )
        .await;
        assert_eq!(made["blackout"]["customer"], Value::Null, "{falsy}");
    }
    for truthy in [
        json!(5),
        json!(true),
        json!(["acme"]),
        json!({"a": 1}),
        json!(1.5),
    ] {
        for key in [ACME_KEY, ADMIN_KEY, BLK_ADMIN_KEY] {
            let (code, _, out) = send(
                &app,
                "POST",
                "/blackout",
                key,
                Some(json!({"environment": "Production", "customer": truthy})),
            )
            .await;
            assert_eq!(
                (code, out["message"].clone()),
                (StatusCode::BAD_REQUEST, json!("customer must be a string")),
                "{truthy}"
            );
        }
    }
    // Update. A malformed value does not clear the stored customer.
    let id = id_of(
        &create(
            &app,
            ADMIN_KEY,
            json!({"environment": "Production", "customer": "acme"}),
        )
        .await,
    );
    let put = |key: &'static str, body: Value| {
        let (app, uri) = (app.clone(), format!("/blackout/{id}"));
        async move { send(&app, "PUT", &uri, key, Some(body)).await }
    };
    for truthy in [json!(5), json!(true), json!(["acme"]), json!({"a": 1})] {
        for key in [ADMIN_KEY, ACME_KEY] {
            let (code, _, out) = put(key, json!({"customer": truthy, "text": "x"})).await;
            assert_eq!(
                (code, out["message"].clone()),
                (StatusCode::BAD_REQUEST, json!("customer must be a string")),
                "{truthy}"
            );
        }
    }
    let (_, _, out) = send(&app, "GET", &format!("/blackout/{id}"), ADMIN_KEY, None).await;
    assert_eq!(
        (
            out["blackout"]["customer"].clone(),
            out["blackout"]["text"].clone()
        ),
        (json!("acme"), Value::Null)
    );
    // A scoped caller that names a falsy customer keeps its own.
    let (code, _, out) = put(ACME_KEY, json!({"customer": ""})).await;
    assert_eq!(
        (code, out["blackout"]["customer"].clone()),
        (StatusCode::OK, json!("acme"))
    );
    // An unscoped caller that names null clears it, as in the reference
    // (SF-BLACKOUTS covers only a body without the key).
    let (code, _, out) = put(ADMIN_KEY, json!({"customer": null})).await;
    assert_eq!(
        (code, out["blackout"]["customer"].clone()),
        (StatusCode::OK, Value::Null)
    );
}
