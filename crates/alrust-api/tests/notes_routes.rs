//! Alert notes through the router, against PostgreSQL: `PUT /alert/{id}/note`,
//! `GET /alert/{id}/notes`, `PUT` and `DELETE /alert/{id}/note/{note_id}`
//! (reference `views/alerts.py:596-723`; `spec/security-fixes.md` SF-NOTES
//! and its five vectors; customer-mapping C21 for the list).
//!
//! Each test creates its own database `<prefix>o<n>_<µs>` and drops it when
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
const READER_KEY: &str = "synthetic-reader-key-00000000000000000000";

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
        "{prefix}o{}_{}",
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
            vec!["write:alerts"],
            Some("acme"),
        ),
        ("k-reader", READER_KEY, "reader", vec!["read"], None),
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

/// An alert stored through `POST /alert` by the admin key, so its history
/// and fields are real. Returns its id.
async fn alert(app: &axum::Router, id: &str, resource: &str, customer: Option<&str>) -> String {
    let (code, _, body) = send(
        app,
        "POST",
        "/alert",
        ADMIN_KEY,
        Some(json!({
            "id": id, "resource": resource, "event": "NoteTest", "environment": "Production",
            "service": ["svc"], "severity": "major", "customer": customer
        })),
    )
    .await;
    assert_eq!(code, StatusCode::CREATED, "{body}");
    body["id"].as_str().unwrap().to_owned()
}

async fn note(app: &axum::Router, alert_id: &str, key: &str, text: &str) -> String {
    let (code, _, body) = send(
        app,
        "PUT",
        &format!("/alert/{alert_id}/note"),
        key,
        Some(json!({"text": text})),
    )
    .await;
    assert_eq!(code, StatusCode::CREATED, "{body}");
    body["id"].as_str().unwrap().to_owned()
}

async fn notes_of(app: &axum::Router, alert_id: &str) -> Vec<(String, String)> {
    let (_, _, body) = send(
        app,
        "GET",
        &format!("/alert/{alert_id}/notes"),
        ADMIN_KEY,
        None,
    )
    .await;
    body["notes"]
        .as_array()
        .unwrap()
        .iter()
        .map(|n| {
            (
                n["id"].as_str().unwrap().to_owned(),
                n["text"].as_str().unwrap_or_default().to_owned(),
            )
        })
        .collect()
}

async fn history(app: &axum::Router, alert_id: &str) -> Value {
    let (_, _, body) = send(app, "GET", &format!("/alert/{alert_id}"), ADMIN_KEY, None).await;
    body["alert"]["history"].clone()
}

/// The history entry of `kind` with the given id. Looked up by content:
/// the body lists history oldest first, and two entries can share a
/// millisecond.
async fn history_entry(app: &axum::Router, alert_id: &str, kind: &str, id: &str) -> Value {
    history(app, alert_id)
        .await
        .as_array()
        .unwrap()
        .iter()
        .find(|h| h["type"] == kind && h["id"] == id)
        .cloned()
        .unwrap_or(Value::Null)
}

#[tokio::test]
async fn a_note_is_added_listed_and_recorded_in_history() {
    let Some((_db, app)) = app().await else {
        return;
    };
    let a = alert(&app, "note-a", "r1", None).await;
    let (code, location, body) = send(
        &app,
        "PUT",
        &format!("/alert/{a}/note"),
        ADMIN_KEY,
        Some(json!({"text": "first"})),
    )
    .await;
    assert_eq!(code, StatusCode::CREATED, "{body}");
    let id = body["id"].as_str().unwrap().to_owned();
    assert_eq!(body["note"]["id"], id.as_str());
    assert_eq!(
        location.as_deref(),
        Some(format!("http://localhost:8080/alert/{a}/note/{id}").as_str())
    );
    let n = &body["note"];
    assert_eq!(n["text"], "first");
    assert_eq!(n["user"], "admin");
    assert_eq!(n["type"], "alert");
    assert_eq!(
        n["attributes"],
        json!({"resource": "r1", "event": "NoteTest", "environment": "Production",
               "severity": "major", "status": "open"})
    );
    assert_eq!(n["href"], format!("http://localhost:8080/note/{id}"));
    assert_eq!(n["related"], json!({"alert": a}));
    assert_eq!(
        n["_links"],
        json!({"alert": format!("http://localhost:8080/alert/{a}")})
    );
    assert_eq!(n["updateTime"], Value::Null);
    assert_eq!(n["customer"], Value::Null);
    // The note entry carries the note's id, not the alert's.
    let h = history_entry(&app, &a, "note", &id).await;
    assert_eq!(
        (h["text"].clone(), h["user"].clone()),
        (json!("first"), json!("admin")),
        "{h}"
    );
    assert_eq!(notes_of(&app, &a).await, vec![(id, "first".to_owned())]);
}

/// The text rules of `PUT /alert/{id}/note`, a missing alert and scopes.
#[tokio::test]
async fn note_text_rules_missing_alerts_and_scopes() {
    let Some((_db, app)) = app().await else {
        return;
    };
    let a = alert(&app, "note-t", "r3", None).await;
    let first = note(&app, &a, ADMIN_KEY, "first").await;
    assert_ne!(first, "");
    // `text`, else `note`, with Python's `or`.
    let (code, _, body) = send(
        &app,
        "PUT",
        &format!("/alert/{a}/note"),
        ADMIN_KEY,
        Some(json!({"text": "", "note": "via note"})),
    )
    .await;
    assert_eq!(
        (code, body["note"]["text"].clone()),
        (StatusCode::CREATED, json!("via note"))
    );
    for (bad, message) in [
        (json!({}), "must supply 'note' text"),
        (json!({"text": ""}), "must supply 'note' text"),
        (json!({"text": 5}), "text must be a string"),
    ] {
        let (code, _, body) = send(
            &app,
            "PUT",
            &format!("/alert/{a}/note"),
            ADMIN_KEY,
            Some(bad.clone()),
        )
        .await;
        assert_eq!(
            (code, body["message"].clone()),
            (StatusCode::BAD_REQUEST, json!(message)),
            "{bad}"
        );
    }
    let (code, _, body) = send(
        &app,
        "PUT",
        "/alert/nosuch/note",
        ADMIN_KEY,
        Some(json!({"text": "x"})),
    )
    .await;
    assert_eq!(
        (code, body["message"].clone()),
        (StatusCode::NOT_FOUND, json!("not found"))
    );
    // read:alerts lists, but cannot write.
    let (code, _, _) = send(
        &app,
        "PUT",
        &format!("/alert/{a}/note"),
        READER_KEY,
        Some(json!({"text": "x"})),
    )
    .await;
    assert_eq!(code, StatusCode::FORBIDDEN);
    let (code, _, body) = send(&app, "GET", &format!("/alert/{a}/notes"), READER_KEY, None).await;
    assert_eq!((code, body["total"].clone()), (StatusCode::OK, json!(2)));
}

#[tokio::test]
async fn a_note_is_updated_and_dismissed() {
    let Some((_db, app)) = app().await else {
        return;
    };
    let a = alert(&app, "note-b", "r2", Some("acme")).await;
    let id = note(&app, &a, ADMIN_KEY, "draft").await;
    let (code, _, body) = send(
        &app,
        "PUT",
        &format!("/alert/{a}/note/{id}"),
        ACME_KEY,
        Some(json!({"text": "edited", "attributes": {"k": "v"}, "ignored": 1})),
    )
    .await;
    assert_eq!(code, StatusCode::OK, "{body}");
    let n = &body["note"];
    assert_eq!(n["text"], "edited");
    assert_eq!(n["user"], "acme-writer");
    assert_eq!(n["attributes"]["k"], "v");
    assert_eq!(n["attributes"]["resource"], "r2");
    assert_eq!(n["customer"], "acme");
    assert!(n["updateTime"].is_string(), "{n}");
    let (code, _, body) = send(
        &app,
        "PUT",
        &format!("/alert/{a}/note/{id}"),
        ACME_KEY,
        Some(json!({})),
    )
    .await;
    assert_eq!(
        (code, body["message"].clone()),
        (StatusCode::BAD_REQUEST, json!("nothing to change"))
    );

    let (code, _, body) = send(
        &app,
        "DELETE",
        &format!("/alert/{a}/note/{id}"),
        ACME_KEY,
        None,
    )
    .await;
    assert_eq!((code, body), (StatusCode::OK, json!({"status": "ok"})));
    let h = history_entry(&app, &a, "dismiss", &id).await;
    assert_eq!(
        (h["text"].clone(), h["user"].clone()),
        (json!("note dismissed"), json!("acme-writer")),
        "{h}"
    );
    assert_eq!(notes_of(&app, &a).await, Vec::<(String, String)>::new());
    let (code, _, body) = send(&app, "DELETE", "/alert/nosuch/note/x", ADMIN_KEY, None).await;
    assert_eq!(
        (code, body["message"].clone()),
        (StatusCode::NOT_FOUND, json!("alert not found"))
    );
    let (code, _, body) = send(
        &app,
        "DELETE",
        &format!("/alert/{a}/note/{id}"),
        ACME_KEY,
        None,
    )
    .await;
    assert_eq!(
        (code, body["message"].clone()),
        (StatusCode::NOT_FOUND, json!("note not found"))
    );
}

/// SF-NOTES vectors: a note is changed or deleted only through its own alert.
#[tokio::test]
async fn sf_notes_vectors() {
    let Some((_db, app)) = app().await else {
        return;
    };
    let a = alert(&app, "sf-a", "ra", Some("acme")).await;
    let b = alert(&app, "sf-b", "rb", Some("acme")).await;
    let other = alert(&app, "sf-o", "ro", Some("other")).await;
    let na = note(&app, &a, ADMIN_KEY, "a's note").await;
    let nb = note(&app, &b, ADMIN_KEY, "b's note").await;
    let no = note(&app, &other, ADMIN_KEY, "other's note").await;
    let edit = || Some(json!({"text": "hijacked"}));
    // 1: an alert the caller can see, a note of another alert, same customer.
    let (code, _, body) = send(
        &app,
        "PUT",
        &format!("/alert/{a}/note/{nb}"),
        ACME_KEY,
        edit(),
    )
    .await;
    assert_eq!(
        (code, body["message"].clone()),
        (StatusCode::NOT_FOUND, json!("not found"))
    );
    assert_eq!(
        notes_of(&app, &b).await,
        vec![(nb.clone(), "b's note".to_owned())]
    );
    // 2: the note's alert belongs to another customer.
    let (code, _, _) = send(
        &app,
        "PUT",
        &format!("/alert/{a}/note/{no}"),
        ACME_KEY,
        edit(),
    )
    .await;
    assert_eq!(code, StatusCode::NOT_FOUND);
    assert_eq!(
        notes_of(&app, &other).await,
        vec![(no.clone(), "other's note".to_owned())]
    );
    // 3: DELETE with a note of another alert; nothing deleted, no history.
    let before = history(&app, &a).await;
    let (code, _, body) = send(
        &app,
        "DELETE",
        &format!("/alert/{a}/note/{nb}"),
        ACME_KEY,
        None,
    )
    .await;
    assert_eq!(
        (code, body["message"].clone()),
        (StatusCode::NOT_FOUND, json!("note not found"))
    );
    assert_eq!(notes_of(&app, &b).await.len(), 1);
    assert_eq!(history(&app, &a).await, before);
    // a's own note is untouched by all of the above.
    assert_eq!(notes_of(&app, &a).await, vec![(na, "a's note".to_owned())]);
}

/// SF-NOTES vectors 4 and 5: the alert's own note, and an alert the caller
/// cannot see.
#[tokio::test]
async fn sf_notes_own_note_and_invisible_alert() {
    let Some((_db, app)) = app().await else {
        return;
    };
    let a = alert(&app, "sf-a", "ra", Some("acme")).await;
    let other = alert(&app, "sf-o", "ro", Some("other")).await;
    let na = note(&app, &a, ADMIN_KEY, "a's note").await;
    let no = note(&app, &other, ADMIN_KEY, "other's note").await;
    let edit = || Some(json!({"text": "hijacked"}));
    // 5: an alert the caller cannot see.
    let (code, _, body) = send(
        &app,
        "PUT",
        &format!("/alert/{other}/note/{no}"),
        ACME_KEY,
        edit(),
    )
    .await;
    assert_eq!(
        (code, body["message"].clone()),
        (StatusCode::NOT_FOUND, json!("not found"))
    );
    let (code, _, body) = send(
        &app,
        "DELETE",
        &format!("/alert/{other}/note/{no}"),
        ACME_KEY,
        None,
    )
    .await;
    assert_eq!(
        (code, body["message"].clone()),
        (StatusCode::NOT_FOUND, json!("alert not found"))
    );
    // 4: the alert's own note.
    let (code, _, _) = send(
        &app,
        "PUT",
        &format!("/alert/{a}/note/{na}"),
        ACME_KEY,
        edit(),
    )
    .await;
    assert_eq!(code, StatusCode::OK);
    let (code, _, _) = send(
        &app,
        "DELETE",
        &format!("/alert/{a}/note/{na}"),
        ACME_KEY,
        None,
    )
    .await;
    assert_eq!(code, StatusCode::OK);
}

/// customer-mapping C21, kept: the list matches `alert ~* <id>`, so a short
/// client-set id also lists notes of an alert whose id contains it.
#[tokio::test]
async fn the_list_matches_the_alert_id_as_a_regular_expression() {
    let Some((_db, app)) = app().await else {
        return;
    };
    let short = alert(&app, "zz-a", "s1", None).await;
    let long = alert(&app, "zz-ab", "s2", None).await;
    let n_short = note(&app, &short, ADMIN_KEY, "on zz-a").await;
    let n_long = note(&app, &long, ADMIN_KEY, "on zz-ab").await;
    let mut ids: Vec<String> = notes_of(&app, &short)
        .await
        .into_iter()
        .map(|(id, _)| id)
        .collect();
    ids.sort();
    let mut want = vec![n_short, n_long.clone()];
    want.sort();
    assert_eq!(ids, want);
    assert_eq!(
        notes_of(&app, &long).await,
        vec![(n_long, "on zz-ab".to_owned())]
    );
}
