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
use alrust_store_pg::{NoteBindings, Store, StoreConfig};
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
    app_with(StoreConfig::default()).await
}

async fn app_with(store_cfg: StoreConfig) -> Option<(TestDb, axum::Router)> {
    let db = test_db().await?;
    let store = Store::new(db.pool.clone(), store_cfg);
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

/// One note of an alert, whole, as `GET /alert/{id}/notes` gives it.
async fn note_body(app: &axum::Router, alert_id: &str, note_id: &str) -> Value {
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
        .find(|n| n["id"] == note_id)
        .cloned()
        .unwrap_or(Value::Null)
}

/// 239-P2-RESERVED (`spec/openapi.yaml`, `updateAlertNote`): a body key
/// `id` or `self` is the reference's 500 after the lookups, and nothing is
/// written: text, attributes, user, update time and history stay.
#[tokio::test]
async fn an_update_with_a_reserved_key_changes_nothing() {
    let Some((_db, app)) = app().await else {
        return;
    };
    let a = alert(&app, "res-a", "ra", Some("acme")).await;
    let b = alert(&app, "res-b", "rb", Some("acme")).await;
    let other = alert(&app, "res-o", "ro", Some("other")).await;
    let na = note(&app, &a, ADMIN_KEY, "a's note").await;
    let nb = note(&app, &b, ADMIN_KEY, "b's note").await;
    let no = note(&app, &other, ADMIN_KEY, "other's note").await;
    let before = note_body(&app, &a, &na).await;
    assert_eq!(before["text"], "a's note");
    assert_eq!(before["user"], "admin");
    let history_before = history(&app, &a).await;
    let reserved = [
        json!({"id": "x", "text": "edited"}),
        json!({"self": 1, "text": "edited"}),
        // The key counts, not its value: falsy values too, and alone.
        json!({"id": null}),
        json!({"self": false}),
        json!({"id": 0, "attributes": {"k": "v"}}),
        json!({"self": "", "text": "edited", "attributes": {"k": "v"}}),
        // A reserved key is refused before the types are looked at.
        json!({"id": "x", "text": 5}),
        json!({"id": [], "self": {}, "attributes": "no object"}),
    ];
    for body in &reserved {
        let (code, _, answer) = send(
            &app,
            "PUT",
            &format!("/alert/{a}/note/{na}"),
            ACME_KEY,
            Some(body.clone()),
        )
        .await;
        assert_eq!(
            (code, answer["status"].clone()),
            (StatusCode::INTERNAL_SERVER_ERROR, json!("error")),
            "{body}: {answer}"
        );
        assert_eq!(note_body(&app, &a, &na).await, before, "{body}");
        assert_eq!(history(&app, &a).await, history_before, "{body}");
    }
    // The lookups come first: no alert, an alert the caller cannot see, no
    // such note, and a note of another alert (SF-NOTES) are all 404.
    let b_before = note_body(&app, &b, &nb).await;
    let o_before = note_body(&app, &other, &no).await;
    for path in [
        format!("/alert/nosuch/note/{na}"),
        format!("/alert/{other}/note/{no}"),
        format!("/alert/{a}/note/nosuch"),
        format!("/alert/{a}/note/{nb}"),
        format!("/alert/{a}/note/{no}"),
    ] {
        for body in &reserved {
            let (code, _, answer) = send(&app, "PUT", &path, ACME_KEY, Some(body.clone())).await;
            assert_eq!(
                (code, answer["message"].clone()),
                (StatusCode::NOT_FOUND, json!("not found")),
                "{path} {body}"
            );
        }
    }
    assert_eq!(note_body(&app, &a, &na).await, before);
    assert_eq!(note_body(&app, &b, &nb).await, b_before);
    assert_eq!(note_body(&app, &other, &no).await, o_before);
    // A read-only key is refused before the body is looked at.
    let (code, _, _) = send(
        &app,
        "PUT",
        &format!("/alert/{a}/note/{na}"),
        READER_KEY,
        Some(json!({"id": "x"})),
    )
    .await;
    assert_eq!(code, StatusCode::FORBIDDEN);
    // An ordinary update still works afterwards.
    let (code, _, answer) = send(
        &app,
        "PUT",
        &format!("/alert/{a}/note/{na}"),
        ACME_KEY,
        Some(json!({"text": "edited", "ignored": 1})),
    )
    .await;
    assert_eq!(
        (code, answer["note"]["text"].clone()),
        (StatusCode::OK, json!("edited")),
        "{answer}"
    );
}

/// The reference's order for the other update bodies: falsy is 400 before
/// any lookup; a missing alert or note is 404 before the body's content is
/// refused; and no refusal writes.
#[tokio::test]
async fn update_body_refusals_follow_the_lookups() {
    let Some((_db, app)) = app().await else {
        return;
    };
    let a = alert(&app, "ord-a", "ra", None).await;
    let na = note(&app, &a, ADMIN_KEY, "kept").await;
    let before = note_body(&app, &a, &na).await;
    let own = format!("/alert/{a}/note/{na}");
    let no_note = format!("/alert/{a}/note/nosuch");
    // Falsy bodies: 400 also where the alert or the note does not exist.
    for body in [
        json!(null),
        json!([]),
        json!(0),
        json!(false),
        json!(""),
        json!({}),
    ] {
        for path in [own.as_str(), no_note.as_str(), "/alert/nosuch/note/x"] {
            let (code, _, answer) = send(&app, "PUT", path, ADMIN_KEY, Some(body.clone())).await;
            assert_eq!(
                (code, answer["message"].clone()),
                (StatusCode::BAD_REQUEST, json!("nothing to change")),
                "{path} {body}"
            );
        }
    }
    // Truthy bodies that are refused: 404 first, then their own status.
    for (body, status) in [
        (json!([1]), StatusCode::INTERNAL_SERVER_ERROR),
        (json!("text"), StatusCode::INTERNAL_SERVER_ERROR),
        (json!(5), StatusCode::INTERNAL_SERVER_ERROR),
        (json!(true), StatusCode::INTERNAL_SERVER_ERROR),
        (json!({"text": 5}), StatusCode::BAD_REQUEST),
        (json!({"attributes": [1]}), StatusCode::BAD_REQUEST),
        (
            json!({"text": "new", "attributes": "no object"}),
            StatusCode::BAD_REQUEST,
        ),
    ] {
        for path in [no_note.as_str(), "/alert/nosuch/note/x"] {
            let (code, _, answer) = send(&app, "PUT", path, ADMIN_KEY, Some(body.clone())).await;
            assert_eq!(
                (code, answer["message"].clone()),
                (StatusCode::NOT_FOUND, json!("not found")),
                "{path} {body}"
            );
        }
        let (code, _, answer) = send(&app, "PUT", &own, ADMIN_KEY, Some(body.clone())).await;
        assert_eq!(code, status, "{body}: {answer}");
        assert_eq!(note_body(&app, &a, &na).await, before, "{body}");
    }
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

/// SF-NOTES, the same id again: a note that outlives its alert is not
/// changed or deleted through a later alert with that id, for the same
/// customer and for another one. Nothing is written and no `dismiss` entry
/// is added. The later alert's own notes work.
#[tokio::test]
async fn a_note_of_an_earlier_alert_with_the_same_id_stays_as_it_is() {
    let Some((_db, app)) = app().await else {
        return;
    };
    for (id, first, second) in [
        ("reuse-same", Some("acme"), Some("acme")),
        ("reuse-other", Some("other"), Some("acme")),
    ] {
        let a = alert(&app, id, &format!("{id}-first"), first).await;
        let old = note(&app, &a, ADMIN_KEY, "the earlier alert's note").await;
        let (code, _, _) = send(&app, "DELETE", &format!("/alert/{a}"), ADMIN_KEY, None).await;
        assert_eq!(code, StatusCode::OK, "{id}");
        let b = alert(&app, id, &format!("{id}-second"), second).await;
        assert_eq!(b, a, "{id}: the id is used again");
        // The note is still stored, and the reference's list shows it (C21).
        let before = note_body(&app, &b, &old).await;
        assert_eq!(before["text"], "the earlier alert's note", "{id}");
        let history_before = history(&app, &b).await;
        for body in [
            json!({"text": "hijacked"}),
            json!({"attributes": {"k": "v"}}),
            json!({"text": 5}),
            json!({"id": "x", "text": "hijacked"}),
        ] {
            let (code, _, answer) = send(
                &app,
                "PUT",
                &format!("/alert/{b}/note/{old}"),
                ACME_KEY,
                Some(body.clone()),
            )
            .await;
            assert_eq!(
                (code, answer["message"].clone()),
                (StatusCode::NOT_FOUND, json!("not found")),
                "{id} {body}"
            );
        }
        let (code, _, answer) = send(
            &app,
            "DELETE",
            &format!("/alert/{b}/note/{old}"),
            ACME_KEY,
            None,
        )
        .await;
        assert_eq!(
            (code, answer["message"].clone()),
            (StatusCode::NOT_FOUND, json!("note not found")),
            "{id}"
        );
        assert_eq!(note_body(&app, &b, &old).await, before, "{id}");
        assert_eq!(history(&app, &b).await, history_before, "{id}");
        // The later alert's own note is changed and deleted as usual.
        let new = note(&app, &b, ACME_KEY, "the later alert's note").await;
        let (code, _, _) = send(
            &app,
            "PUT",
            &format!("/alert/{b}/note/{new}"),
            ACME_KEY,
            Some(json!({"text": "edited"})),
        )
        .await;
        assert_eq!(code, StatusCode::OK, "{id}");
        assert_eq!(note_body(&app, &b, &new).await["text"], "edited", "{id}");
        let (code, _, _) = send(
            &app,
            "DELETE",
            &format!("/alert/{b}/note/{new}"),
            ACME_KEY,
            None,
        )
        .await;
        assert_eq!(code, StatusCode::OK, "{id}");
        assert_eq!(note_body(&app, &b, &old).await, before, "{id}");
    }
}

/// SF-NOTES: a note row that no stored alert received (its alert was
/// removed earlier, by any program) is not changed or deleted through an
/// alert with its id.
#[tokio::test]
async fn a_note_row_left_by_an_earlier_deletion_stays_as_it_is() {
    let Some((db, app)) = app().await else {
        return;
    };
    let c = alert(&app, "reuse-orphan", "r-orphan", Some("acme")).await;
    sqlx::query(
        r#"INSERT INTO notes (id, text, "user", attributes, type, create_time, alert, customer)
           VALUES ('orphan-note', 'left behind', 'someone', '{}'::jsonb, 'alert',
                   NOW() AT TIME ZONE 'utc', $1, 'other')"#,
    )
    .bind(&c)
    .execute(&db.pool)
    .await
    .unwrap();
    let before = note_body(&app, &c, "orphan-note").await;
    assert_eq!(before["text"], "left behind");
    let (code, _, _) = send(
        &app,
        "PUT",
        &format!("/alert/{c}/note/orphan-note"),
        ACME_KEY,
        Some(json!({"text": "hijacked"})),
    )
    .await;
    assert_eq!(code, StatusCode::NOT_FOUND);
    let (code, _, _) = send(
        &app,
        "DELETE",
        &format!("/alert/{c}/note/orphan-note"),
        ACME_KEY,
        None,
    )
    .await;
    assert_eq!(code, StatusCode::NOT_FOUND);
    assert_eq!(note_body(&app, &c, "orphan-note").await, before);
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

/// What the database has of the note bindings (`notes::bindings_state`).
async fn bindings_state(pool: &PgPool) -> NoteBindings {
    let mut conn = pool.acquire().await.unwrap();
    alrust_store_pg::notes::bindings_state(&mut conn)
        .await
        .unwrap()
}

/// The problems of a malformed state; panics on another state.
async fn malformed(pool: &PgPool, case: &str) -> String {
    match bindings_state(pool).await {
        NoteBindings::Malformed(problems) => problems.join("\n"),
        other => panic!("{case}: expected a malformed state, found {other:?}"),
    }
}

/// The alert a note is bound to (`alrust_note_bindings`, DECISIONS Y77).
async fn binding(pool: &PgPool, note_id: &str) -> Option<String> {
    sqlx::query_scalar("SELECT alert FROM alrust_note_bindings WHERE note = $1")
        .bind(note_id)
        .fetch_optional(pool)
        .await
        .unwrap()
}

/// The note is not changed and not deleted through the alert, and stays as
/// it was.
async fn refused(app: &axum::Router, alert_id: &str, note_id: &str, case: &str) {
    let before = note_body(app, alert_id, note_id).await;
    let (code, _, answer) = send(
        app,
        "PUT",
        &format!("/alert/{alert_id}/note/{note_id}"),
        ADMIN_KEY,
        Some(json!({"text": "changed"})),
    )
    .await;
    assert_eq!(
        (code, answer["message"].clone()),
        (StatusCode::NOT_FOUND, json!("not found")),
        "{case}"
    );
    let (code, _, answer) = send(
        app,
        "DELETE",
        &format!("/alert/{alert_id}/note/{note_id}"),
        ADMIN_KEY,
        None,
    )
    .await;
    assert_eq!(
        (code, answer["message"].clone()),
        (StatusCode::NOT_FOUND, json!("note not found")),
        "{case}"
    );
    assert_eq!(note_body(app, alert_id, note_id).await, before, "{case}");
}

/// Y77: a note stays its alert's own for as long as the alert is stored,
/// also after the history has dropped the note's entry. With a history
/// limit of 1, the second note's entry pushes the first one's out.
#[tokio::test]
async fn a_note_is_changed_and_deleted_after_the_history_dropped_its_entry() {
    let Some((db, app)) = app_with(StoreConfig {
        history_limit: 1,
        ..StoreConfig::default()
    })
    .await
    else {
        return;
    };
    let a = alert(&app, "evict", "r-evict", Some("acme")).await;
    let first = note(&app, &a, ACME_KEY, "first").await;
    let second = note(&app, &a, ACME_KEY, "second").await;
    let entries: Vec<String> =
        sqlx::query_scalar("SELECT h.id FROM alerts a, unnest(a.history) AS h WHERE a.id = $1")
            .bind(&a)
            .fetch_all(&db.pool)
            .await
            .unwrap();
    assert_eq!(
        entries,
        std::slice::from_ref(&second),
        "only the newest entry is kept"
    );
    assert_eq!(binding(&db.pool, &first).await, Some(a.clone()));
    let (code, _, body) = send(
        &app,
        "PUT",
        &format!("/alert/{a}/note/{first}"),
        ACME_KEY,
        Some(json!({"text": "edited"})),
    )
    .await;
    assert_eq!(code, StatusCode::OK, "{body}");
    assert_eq!(note_body(&app, &a, &first).await["text"], "edited");
    for n in [&first, &second] {
        let (code, _, body) = send(
            &app,
            "DELETE",
            &format!("/alert/{a}/note/{n}"),
            ACME_KEY,
            None,
        )
        .await;
        assert_eq!(code, StatusCode::OK, "{body}");
        assert_eq!(
            binding(&db.pool, n).await,
            None,
            "the binding goes with the note"
        );
        assert_eq!(note_body(&app, &a, n).await, Value::Null);
    }
}

/// Y77: the triggers end a binding when another program, writing SQL,
/// deletes the alert, changes its id or truncates the table; and when an
/// alert with that id is stored while a binding was left in place.
#[tokio::test]
async fn a_write_to_alerts_by_another_program_ends_the_binding() {
    let Some((db, app)) = app().await else {
        return;
    };
    // Deleted by SQL, then the same id again.
    let a = alert(&app, "sql-delete", "r-sql-delete", Some("acme")).await;
    let n = note(&app, &a, ACME_KEY, "kept").await;
    assert_eq!(binding(&db.pool, &n).await, Some(a.clone()));
    sqlx::query("DELETE FROM alerts WHERE id = $1")
        .bind(&a)
        .execute(&db.pool)
        .await
        .unwrap();
    assert_eq!(binding(&db.pool, &n).await, None, "delete");
    let b = alert(&app, "sql-delete", "r-sql-delete-2", Some("acme")).await;
    assert_eq!(b, a);
    refused(&app, &b, &n, "delete").await;

    // The id changed by SQL, then the first id again.
    let a = alert(&app, "sql-move", "r-sql-move", Some("acme")).await;
    let n = note(&app, &a, ACME_KEY, "kept").await;
    sqlx::query("UPDATE alerts SET id = 'sql-moved' WHERE id = $1")
        .bind(&a)
        .execute(&db.pool)
        .await
        .unwrap();
    assert_eq!(binding(&db.pool, &n).await, None, "id change");
    let b = alert(&app, "sql-move", "r-sql-move-2", Some("acme")).await;
    assert_eq!(b, a);
    refused(&app, &b, &n, "id change").await;

    // An update that keeps the id keeps the binding.
    let a = alert(&app, "sql-keep", "r-sql-keep", Some("acme")).await;
    let n = note(&app, &a, ACME_KEY, "kept").await;
    sqlx::query("UPDATE alerts SET id = id, text = 'changed by SQL' WHERE id = $1")
        .bind(&a)
        .execute(&db.pool)
        .await
        .unwrap();
    assert_eq!(binding(&db.pool, &n).await, Some(a.clone()), "same id");
    let (code, _, body) = send(
        &app,
        "PUT",
        &format!("/alert/{a}/note/{n}"),
        ACME_KEY,
        Some(json!({"text": "edited"})),
    )
    .await;
    assert_eq!(code, StatusCode::OK, "{body}");

    // A binding left in place while the delete trigger was off is removed
    // when an alert with that id is stored.
    let a = alert(&app, "sql-stale", "r-sql-stale", Some("acme")).await;
    let n = note(&app, &a, ACME_KEY, "kept").await;
    sqlx::raw_sql(
        "ALTER TABLE alerts DISABLE TRIGGER alrust_note_bindings_delete;
         DELETE FROM alerts WHERE id = 'sql-stale';
         ALTER TABLE alerts ENABLE TRIGGER alrust_note_bindings_delete;",
    )
    .execute(&db.pool)
    .await
    .unwrap();
    assert_eq!(
        binding(&db.pool, &n).await,
        Some(a.clone()),
        "left in place"
    );
    let b = alert(&app, "sql-stale", "r-sql-stale-2", Some("acme")).await;
    assert_eq!(b, a);
    assert_eq!(binding(&db.pool, &n).await, None, "insert");
    refused(&app, &b, &n, "insert").await;

    // Truncated, then the same id again.
    let a = alert(&app, "sql-truncate", "r-sql-truncate", Some("acme")).await;
    let n = note(&app, &a, ACME_KEY, "kept").await;
    sqlx::query("TRUNCATE alerts")
        .execute(&db.pool)
        .await
        .unwrap();
    assert_eq!(binding(&db.pool, &n).await, None, "truncate");
    let bindings: i64 = sqlx::query_scalar("SELECT count(*) FROM alrust_note_bindings")
        .fetch_one(&db.pool)
        .await
        .unwrap();
    assert_eq!(bindings, 0, "truncate removes every binding");
    let b = alert(&app, "sql-truncate", "r-sql-truncate-2", Some("acme")).await;
    assert_eq!(b, a);
    refused(&app, &b, &n, "truncate").await;
}

/// Y77: while the bindings are not in force, notes are added and listed and
/// none is changed or deleted. First with one trigger disabled, then with
/// the table, the function and the triggers absent, as on an adopted
/// reference database.
#[tokio::test]
async fn without_the_bindings_in_force_a_note_is_added_and_listed_but_not_changed() {
    let Some((db, app)) = app().await else {
        return;
    };
    assert_eq!(bindings_state(&db.pool).await, NoteBindings::InForce);
    let a = alert(&app, "not-in-force", "r-not-in-force", Some("acme")).await;
    let bound = note(&app, &a, ACME_KEY, "bound").await;
    sqlx::query("ALTER TABLE alerts DISABLE TRIGGER alrust_note_bindings_update")
        .execute(&db.pool)
        .await
        .unwrap();
    let problems = malformed(&db.pool, "a trigger is disabled").await;
    assert!(
        problems.contains("alrust_note_bindings_update") && problems.contains("(enabled D)"),
        "{problems}"
    );
    refused(&app, &a, &bound, "a trigger is disabled").await;
    let unbound = note(&app, &a, ACME_KEY, "added while a trigger was disabled").await;
    assert_eq!(binding(&db.pool, &unbound).await, None);
    assert_eq!(
        note_body(&app, &a, &unbound).await["text"],
        "added while a trigger was disabled"
    );
    sqlx::query("ALTER TABLE alerts ENABLE TRIGGER alrust_note_bindings_update")
        .execute(&db.pool)
        .await
        .unwrap();
    refused(&app, &a, &unbound, "stored without a binding").await;

    sqlx::raw_sql(
        "DROP TRIGGER alrust_note_bindings_insert ON alerts;
         DROP TRIGGER alrust_note_bindings_delete ON alerts;
         DROP TRIGGER alrust_note_bindings_update ON alerts;
         DROP TRIGGER alrust_note_bindings_truncate ON alerts;
         DROP FUNCTION alrust_note_bindings_clear();
         DROP TABLE alrust_note_bindings;",
    )
    .execute(&db.pool)
    .await
    .unwrap();
    assert_eq!(bindings_state(&db.pool).await, NoteBindings::Absent);
    let plain = note(&app, &a, ACME_KEY, "added with no bindings").await;
    assert_eq!(
        note_body(&app, &a, &plain).await["text"],
        "added with no bindings"
    );
    for (n, case) in [(&bound, "bound earlier"), (&plain, "no bindings")] {
        refused(&app, &a, n, case).await;
    }
    // Alerts are still stored and deleted.
    let other = alert(&app, "not-in-force-2", "r-not-in-force-2", Some("acme")).await;
    let (code, _, _) = send(&app, "DELETE", &format!("/alert/{other}"), ADMIN_KEY, None).await;
    assert_eq!(code, StatusCode::OK);
}

/// Y77: the trigger function runs with its owner's rights, so a role that
/// may delete alerts and has no right on the bindings is not refused, and
/// the binding is removed. Everything runs in one transaction that is
/// rolled back, the role included. Needs a superuser.
#[tokio::test]
async fn a_role_with_no_right_on_the_bindings_can_delete_an_alert() {
    let Some((db, app)) = app().await else {
        return;
    };
    let superuser: bool =
        sqlx::query_scalar("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
            .fetch_one(&db.pool)
            .await
            .unwrap();
    if !superuser {
        eprintln!("NOT_RUN (needs superuser): delete by a role with no right on the bindings");
        return;
    }
    let a = alert(&app, "other-role", "r-other-role", Some("acme")).await;
    let n = note(&app, &a, ACME_KEY, "kept").await;
    let role = format!("alrust_t_role_{}", std::process::id());
    let mut tx = db.pool.begin().await.unwrap();
    sqlx::raw_sql(&format!(
        "CREATE ROLE \"{role}\" NOLOGIN;
         GRANT SELECT, DELETE ON alerts TO \"{role}\";
         SET LOCAL ROLE \"{role}\";"
    ))
    .execute(&mut *tx)
    .await
    .unwrap();
    let rights: (bool, bool) = sqlx::query_as(
        "SELECT has_table_privilege('alrust_note_bindings', 'SELECT'),
                has_table_privilege('alrust_note_bindings', 'DELETE')",
    )
    .fetch_one(&mut *tx)
    .await
    .unwrap();
    assert_eq!(rights, (false, false));
    let may_execute: bool = sqlx::query_scalar(
        "SELECT has_function_privilege('public.alrust_note_bindings_clear()', 'EXECUTE')",
    )
    .fetch_one(&mut *tx)
    .await
    .unwrap();
    assert!(!may_execute, "EXECUTE is revoked from PUBLIC");
    let deleted = sqlx::query("DELETE FROM alerts WHERE id = $1")
        .bind(&a)
        .execute(&mut *tx)
        .await
        .unwrap();
    assert_eq!(deleted.rows_affected(), 1);
    sqlx::query("RESET ROLE").execute(&mut *tx).await.unwrap();
    let left: i64 = sqlx::query_scalar("SELECT count(*) FROM alrust_note_bindings WHERE note = $1")
        .bind(&n)
        .fetch_one(&mut *tx)
        .await
        .unwrap();
    assert_eq!(left, 0);
    tx.rollback().await.unwrap();
    // After the rollback the alert, the note's binding and no role remain.
    assert_eq!(binding(&db.pool, &n).await, Some(a.clone()));
    let (alerts, roles): (i64, i64) = sqlx::query_as(
        "SELECT (SELECT count(*) FROM alerts WHERE id = $1),
                (SELECT count(*) FROM pg_roles WHERE rolname = $2)",
    )
    .bind(&a)
    .bind(&role)
    .fetch_one(&db.pool)
    .await
    .unwrap();
    assert_eq!((alerts, roles), (1, 0));
    assert_eq!(bindings_state(&db.pool).await, NoteBindings::InForce);
}

/// Y77: a binding belongs to one stored version of the note's row. A note
/// that another program deleted and stored again with the same id and
/// alert, or changed, is no longer bound. alrust's own update keeps the
/// note bound.
#[tokio::test]
async fn a_write_to_notes_by_another_program_ends_the_binding() {
    let Some((db, app)) = app().await else {
        return;
    };
    let a = alert(&app, "note-side", "r-note-side", Some("acme")).await;

    // alrust's own updates, twice: still bound.
    let own = note(&app, &a, ACME_KEY, "own").await;
    for text in ["edited once", "edited twice"] {
        let (code, _, body) = send(
            &app,
            "PUT",
            &format!("/alert/{a}/note/{own}"),
            ACME_KEY,
            Some(json!({"text": text})),
        )
        .await;
        assert_eq!(code, StatusCode::OK, "{body}");
    }
    assert_eq!(note_body(&app, &a, &own).await["text"], "edited twice");

    // Deleted and stored again by SQL, with the same id and alert.
    let replaced = note(&app, &a, ACME_KEY, "first").await;
    sqlx::raw_sql(&format!(
        "CREATE TEMP TABLE kept_note AS SELECT * FROM notes WHERE id = '{replaced}';
         DELETE FROM notes WHERE id = '{replaced}';
         UPDATE kept_note SET text = 'written by another program';
         INSERT INTO notes SELECT * FROM kept_note;
         DROP TABLE kept_note;"
    ))
    .execute(&db.pool)
    .await
    .unwrap();
    assert_eq!(
        binding(&db.pool, &replaced).await,
        Some(a.clone()),
        "the row of the binding is left; it no longer counts"
    );
    refused(&app, &a, &replaced, "deleted and stored again").await;
    assert_eq!(
        note_body(&app, &a, &replaced).await["text"],
        "written by another program"
    );

    // Changed by SQL.
    let changed = note(&app, &a, ACME_KEY, "first").await;
    sqlx::query("UPDATE notes SET text = 'changed by another program' WHERE id = $1")
        .bind(&changed)
        .execute(&db.pool)
        .await
        .unwrap();
    refused(&app, &a, &changed, "changed by SQL").await;

    // Truncated by SQL, then the same ids again.
    let gone = note(&app, &a, ACME_KEY, "first").await;
    sqlx::raw_sql(&format!(
        "CREATE TEMP TABLE kept_note AS SELECT * FROM notes WHERE id = '{gone}';
         TRUNCATE notes;
         INSERT INTO notes SELECT * FROM kept_note;
         DROP TABLE kept_note;"
    ))
    .execute(&db.pool)
    .await
    .unwrap();
    refused(&app, &a, &gone, "truncated and stored again").await;
}

/// Y77: a part of the bindings is not the bindings. With the table gone, or
/// another table under its name, the state is reported with its difference,
/// no note is changed or deleted, notes are still added, and writes to
/// `alerts` still succeed.
#[tokio::test]
async fn a_malformed_installation_is_reported_and_not_used() {
    // The table gone, the function and the triggers left.
    let Some((db, app)) = app().await else {
        return;
    };
    let a = alert(&app, "partial", "r-partial", Some("acme")).await;
    let n = note(&app, &a, ACME_KEY, "bound").await;
    sqlx::query("DROP TABLE alrust_note_bindings")
        .execute(&db.pool)
        .await
        .unwrap();
    let problems = malformed(&db.pool, "table dropped").await;
    assert!(
        problems.contains("the table public.alrust_note_bindings is missing"),
        "{problems}"
    );
    refused(&app, &a, &n, "table dropped").await;
    note(&app, &a, ACME_KEY, "added with no table").await;
    // The triggers still fire and do nothing: alerts are stored, changed
    // and deleted.
    let other = alert(&app, "partial-2", "r-partial-2", Some("acme")).await;
    sqlx::raw_sql(
        "UPDATE alerts SET id = 'partial-3' WHERE id = 'partial-2';
         DELETE FROM alerts WHERE id = 'partial-3';",
    )
    .execute(&db.pool)
    .await
    .unwrap();
    assert_eq!(other, "partial-2");

    // A table of another shape under the name.
    sqlx::query("CREATE TABLE alrust_note_bindings (note text, alert text, note_xmin xid)")
        .execute(&db.pool)
        .await
        .unwrap();
    let problems = malformed(&db.pool, "another table").await;
    assert!(
        problems.contains("the columns of public.alrust_note_bindings are")
            && problems.contains("no valid primary key"),
        "{problems}"
    );
    refused(&app, &a, &n, "another table").await;
}

/// Y77: a trigger or a function of another definition under alrust's name
/// is reported, and the bindings are not used while it is there.
#[tokio::test]
async fn a_redefined_trigger_or_function_is_reported_and_not_used() {
    let Some((db, app)) = app().await else {
        return;
    };
    let a = alert(&app, "redefined", "r-redefined", Some("acme")).await;
    let n = note(&app, &a, ACME_KEY, "bound").await;
    for (case, name, definition) in [
        ("no WHEN", "update", "AFTER UPDATE ON alerts FOR EACH ROW"),
        (
            "another WHEN",
            "update",
            "AFTER UPDATE ON alerts FOR EACH ROW WHEN (OLD.id IS DISTINCT FROM NEW.resource)",
        ),
        (
            "a column list",
            "update",
            "AFTER UPDATE OF text ON alerts FOR EACH ROW WHEN (OLD.id IS DISTINCT FROM NEW.id)",
        ),
        (
            "a longer WHEN",
            "update",
            "AFTER UPDATE ON alerts FOR EACH ROW WHEN (OLD.id IS DISTINCT FROM NEW.id AND OLD.text IS DISTINCT FROM NEW.text)",
        ),
        (
            "another event",
            "delete",
            "AFTER INSERT ON alerts FOR EACH ROW",
        ),
        (
            "for each statement",
            "insert",
            "AFTER INSERT ON alerts FOR EACH STATEMENT",
        ),
    ] {
        sqlx::raw_sql(&format!(
            "DROP TRIGGER alrust_note_bindings_{name} ON alerts;
             CREATE TRIGGER alrust_note_bindings_{name} {definition}
                 EXECUTE FUNCTION alrust_note_bindings_clear();"
        ))
        .execute(&db.pool)
        .await
        .unwrap();
        let problems = malformed(&db.pool, case).await;
        assert!(
            problems.contains(&format!(
                "the trigger alrust_note_bindings_{name} on public.alerts is not the one"
            )),
            "{case}: {problems}"
        );
        refused(&app, &a, &n, case).await;
        // Put the trigger of 0003 back.
        let original = match name {
            "update" => "AFTER UPDATE ON alerts FOR EACH ROW WHEN (OLD.id IS DISTINCT FROM NEW.id)",
            "delete" => "AFTER DELETE ON alerts FOR EACH ROW",
            _ => "AFTER INSERT ON alerts FOR EACH ROW",
        };
        sqlx::raw_sql(&format!(
            "DROP TRIGGER alrust_note_bindings_{name} ON alerts;
             CREATE TRIGGER alrust_note_bindings_{name} {original}
                 EXECUTE FUNCTION alrust_note_bindings_clear();"
        ))
        .execute(&db.pool)
        .await
        .unwrap();
        assert_eq!(
            bindings_state(&db.pool).await,
            NoteBindings::InForce,
            "{case}: restored"
        );
    }
}

/// Y77: an extra trigger with alrust's prefix and a function of another
/// body are reported; the function refuses to run on another table.
#[tokio::test]
async fn an_extra_trigger_or_another_function_is_reported_and_not_used() {
    let Some((db, app)) = app().await else {
        return;
    };
    let a = alert(&app, "extra", "r-extra", Some("acme")).await;
    let n = note(&app, &a, ACME_KEY, "bound").await;

    // A fifth trigger with alrust's prefix.
    sqlx::query(
        "CREATE TRIGGER alrust_note_bindings_extra AFTER DELETE ON alerts
             FOR EACH ROW EXECUTE FUNCTION alrust_note_bindings_clear()",
    )
    .execute(&db.pool)
    .await
    .unwrap();
    let problems = malformed(&db.pool, "extra trigger").await;
    assert!(
        problems.contains("unknown trigger alrust_note_bindings_extra"),
        "{problems}"
    );
    sqlx::query("DROP TRIGGER alrust_note_bindings_extra ON alerts")
        .execute(&db.pool)
        .await
        .unwrap();

    // The function on another table: it refuses to run there.
    let wrong = sqlx::raw_sql(
        "CREATE TRIGGER alrust_note_bindings_delete AFTER DELETE ON notes
             FOR EACH ROW EXECUTE FUNCTION alrust_note_bindings_clear();
         DELETE FROM notes;",
    )
    .execute(&db.pool)
    .await
    .unwrap_err();
    assert!(
        wrong
            .to_string()
            .contains("runs only as an AFTER trigger on public.alerts"),
        "{wrong}"
    );
    sqlx::query("DROP TRIGGER IF EXISTS alrust_note_bindings_delete ON notes")
        .execute(&db.pool)
        .await
        .unwrap();

    // The function with another body, same name and settings.
    sqlx::query(
        "CREATE OR REPLACE FUNCTION public.alrust_note_bindings_clear() RETURNS trigger
             LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
         AS $$ BEGIN RETURN NULL; END $$",
    )
    .execute(&db.pool)
    .await
    .unwrap();
    let problems = malformed(&db.pool, "another body").await;
    assert!(
        problems.contains("alrust_note_bindings_clear() is not the one of migration 0003"),
        "{problems}"
    );
    refused(&app, &a, &n, "another body").await;
}
