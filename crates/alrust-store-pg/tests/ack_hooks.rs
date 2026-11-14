//! The acknowledgement hook interface (`spec/integration-hooks.md` section
//! 3) against PostgreSQL, with synthetic recording integrations: phase order
//! and call counts, at most one owner, refusal and rollback at every phase,
//! the attribute re-read after staging, and that hooks run only for `ack`;
//! and the receive-time `pre_receive` phase (section 4.3).
//!
//! Each test creates its own database `<prefix>h<n>_<µs>` (prefix as in
//! `reference_layout.rs`) and drops it when it ends, also on panic. Without
//! `DATABASE_URL` the tests are skipped, except under CI (`CI` set), where a
//! missing database is a failure.
#![expect(
    clippy::unwrap_used,
    clippy::print_stderr,
    reason = "integration test: helpers unwrap, skips are printed, and each scenario stays in one function"
)]

use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::{Arc, Mutex};

use alrust_domain::{Alert, HistoryEntry};
use alrust_engine::Transition;
use alrust_store_pg::hooks::{Gate, HookFuture, MULTIPLE_OWNERS, Staged, zabalack};
use alrust_store_pg::{
    AckIntegration, CallerFacts, HookError, NewAlert, Store, StoreConfig, StoreError,
};
use chrono::Utc;
use serde_json::{Map, Value, json};
use sqlx::postgres::{PgConnectOptions, PgPoolOptions};
use sqlx::{Executor, PgConnection, PgPool};

static COUNTER: AtomicU32 = AtomicU32::new(0);

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
        "{prefix}h{}_{}",
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

/// The phase a synthetic integration refuses at, if any.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum FailAt {
    Never,
    PreReceive,
    PreLookup,
    Gate,
    Stage,
    PreCommit,
}

/// A synthetic integration that records every call it receives.
#[derive(Debug)]
struct Fake {
    name: &'static str,
    claims: bool,
    skip: bool,
    fail: FailAt,
    calls: Arc<Mutex<Vec<String>>>,
    /// A second pool: what other sessions see during `pre_commit`.
    outside: PgPool,
    /// What `gate` and `pre_commit` observed, for the assertions.
    seen: Arc<Mutex<Vec<String>>>,
}

impl Fake {
    fn new(name: &'static str, calls: &Arc<Mutex<Vec<String>>>, outside: &PgPool) -> Self {
        Fake {
            name,
            claims: false,
            skip: false,
            fail: FailAt::Never,
            calls: Arc::clone(calls),
            outside: outside.clone(),
            seen: Arc::new(Mutex::new(Vec::new())),
        }
    }

    /// This integration, claiming every alert.
    fn claiming(mut self) -> Self {
        self.claims = true;
        self
    }

    fn record(&self, phase: &str) {
        self.calls
            .lock()
            .unwrap()
            .push(format!("{}:{phase}", self.name));
    }

    fn refuse(&self, at: FailAt) -> Result<(), HookError> {
        if self.fail == at {
            Err(HookError::new(
                409,
                format!("{} refused at {at:?}", self.name),
            ))
        } else {
            Ok(())
        }
    }
}

impl AckIntegration for Fake {
    // Records every call, so the action tests' exact call lists also prove
    // that no action path calls pre_receive. Strips with Zabalack's ported
    // filter, as its adapter will.
    fn pre_receive<'a>(
        &'a self,
        _conn: &'a mut PgConnection,
        _caller: &'a CallerFacts,
        incoming: &'a mut NewAlert,
    ) -> HookFuture<'a, ()> {
        Box::pin(async move {
            self.record("pre_receive");
            self.refuse(FailAt::PreReceive)?;
            zabalack::strip_reserved(&mut incoming.attributes);
            Ok(())
        })
    }

    fn pre_lookup<'a>(
        &'a self,
        _conn: &'a mut PgConnection,
        _caller: &'a CallerFacts,
    ) -> HookFuture<'a, ()> {
        Box::pin(async move {
            self.record("pre_lookup");
            self.refuse(FailAt::PreLookup)
        })
    }

    fn claims<'a>(
        &'a self,
        _conn: &'a mut PgConnection,
        _alert: &'a Alert,
    ) -> HookFuture<'a, bool> {
        Box::pin(async move {
            self.record("claims");
            Ok(self.claims)
        })
    }

    fn gate<'a>(
        &'a self,
        conn: &'a mut PgConnection,
        _alert: &'a Alert,
        caller: &'a CallerFacts,
        transition: &'a Transition,
    ) -> HookFuture<'a, Gate> {
        Box::pin(async move {
            self.record("gate");
            let iso: String = sqlx::query_scalar("SHOW transaction_isolation")
                .fetch_one(&mut *conn)
                .await
                .unwrap();
            self.seen.lock().unwrap().push(format!(
                "gate isolation={iso} transition={} login={:?}",
                transition.status, caller.login
            ));
            self.refuse(FailAt::Gate)?;
            if self.skip {
                let attributes: Map<String, Value> =
                    serde_json::from_value(json!({"sync": "skipped", "intent": null})).unwrap();
                Ok(Gate::Skip { attributes })
            } else {
                Ok(Gate::Proceed)
            }
        })
    }

    fn stage<'a>(
        &'a self,
        conn: &'a mut PgConnection,
        alert: &'a Alert,
        _caller: &'a CallerFacts,
        text: &'a str,
    ) -> HookFuture<'a, Staged> {
        Box::pin(async move {
            self.record("stage");
            // Like Zabalack's stage_generation: staging SQL itself writes the
            // row's attributes inside the acknowledgement transaction.
            sqlx::query(
                "UPDATE alerts SET attributes = attributes || '{\"by_stage_sql\": \"yes\"}' WHERE id = $1",
            )
            .bind(&alert.id)
            .execute(&mut *conn)
            .await
            .unwrap();
            self.refuse(FailAt::Stage)?;
            let attributes: Map<String, Value> =
                serde_json::from_value(json!({"sync": "pending", "intent": {"v": 1}})).unwrap();
            Ok(Staged {
                text: Some(format!("{text}\n[synthetic:v1 marker]")),
                attributes,
            })
        })
    }

    fn pre_commit<'a>(
        &'a self,
        conn: &'a mut PgConnection,
        alert: &'a Alert,
        _entry: &'a HistoryEntry,
    ) -> HookFuture<'a, ()> {
        Box::pin(async move {
            self.record("pre_commit");
            let inside: (String, String) =
                sqlx::query_as("SELECT status, (history[1]).type FROM alerts WHERE id = $1")
                    .bind(&alert.id)
                    .fetch_one(&mut *conn)
                    .await
                    .unwrap();
            let outside: String = sqlx::query_scalar("SELECT status FROM alerts WHERE id = $1")
                .bind(&alert.id)
                .fetch_one(&self.outside)
                .await
                .unwrap();
            self.seen.lock().unwrap().push(format!(
                "pre_commit inside={}/{} outside={outside}",
                inside.0, inside.1
            ));
            self.refuse(FailAt::PreCommit)
        })
    }
}

fn hook(f: Fake) -> Arc<dyn AckIntegration> {
    Arc::new(f)
}

fn new_alert(id: &str) -> NewAlert {
    NewAlert {
        id: id.into(),
        resource: format!("res-{id}"),
        event: "HttpError".into(),
        environment: "Production".into(),
        severity: "major".into(),
        correlate: vec![],
        status: "open".into(),
        service: vec!["Web".into()],
        group: "Web".into(),
        value: None,
        text: String::new(),
        tags: vec![],
        attributes: serde_json::from_value(json!({"keep": 1})).unwrap(),
        origin: "test".into(),
        event_type: "exceptionAlert".into(),
        create_time: Utc::now(),
        timeout: 86_400,
        raw_data: None,
        customer: None,
    }
}

fn caller() -> CallerFacts {
    CallerFacts {
        login: Some("op".into()),
        scopes: vec!["write:alerts".into()],
        auth_required: true,
        ..CallerFacts::default()
    }
}

/// A prepared store with these integrations and one open alert `a1`, whose
/// attributes also hold a JSON null (removed by alrust's own action path).
async fn setup(db: &TestDb, hooks: Vec<Arc<dyn AckIntegration>>) -> Store {
    let plain = Store::new(db.pool.clone(), StoreConfig::default());
    plain.prepare().await.unwrap();
    plain.receive(new_alert("a1"), "src").await.unwrap();
    sqlx::query("UPDATE alerts SET attributes = attributes || '{\"gone\": null}' WHERE id = 'a1'")
        .execute(&db.pool)
        .await
        .unwrap();
    plain.with_integrations(hooks)
}

async fn snapshot(pool: &PgPool) -> (String, Value, i64) {
    sqlx::query_as(
        "SELECT status, attributes, cardinality(history)::bigint FROM alerts WHERE id = 'a1'",
    )
    .fetch_one(pool)
    .await
    .unwrap()
}

fn calls(c: &Arc<Mutex<Vec<String>>>) -> Vec<String> {
    c.lock().unwrap().clone()
}

#[tokio::test]
async fn phases_run_once_in_order_and_the_owner_stages() {
    let Some(db) = test_db().await else { return };
    let log = Arc::new(Mutex::new(Vec::new()));
    let owner = Arc::new(Fake::new("A", &log, &db.pool).claiming());
    let other = Arc::new(Fake::new("B", &log, &db.pool));
    let (h1, h2) = (Arc::clone(&owner), Arc::clone(&other));
    let hooks: Vec<Arc<dyn AckIntegration>> = vec![h1, h2];
    let s = setup(&db, hooks).await;

    let a = s
        .take_action_with("a1", None, "ack", "seen", None, &caller())
        .await
        .unwrap();
    assert_eq!(
        calls(&log),
        [
            "A:pre_lookup",
            "B:pre_lookup",
            "A:claims",
            "B:claims",
            "A:gate",
            "A:stage",
            "A:pre_commit"
        ]
    );
    assert_eq!(
        owner.seen.lock().unwrap().clone(),
        [
            "gate isolation=read committed transition=ack login=Some(\"op\")",
            // After alrust's UPDATE, before COMMIT: visible inside, not outside.
            "pre_commit inside=ack/ack outside=open",
        ]
    );
    // The re-read kept the staging SQL's write; the stage's own writes and
    // alrust's own change (the null removed) were applied on top.
    let (status, attributes, _) = snapshot(&db.pool).await;
    assert_eq!(status, "ack");
    assert_eq!(
        attributes,
        json!({"keep": 1, "by_stage_sql": "yes", "sync": "pending", "intent": {"v": 1}})
    );
    assert_eq!(a.history[0].change_type, "ack");
    assert_eq!(
        a.history[0].text.as_deref(),
        Some("seen\n[synthetic:v1 marker]")
    );
    assert_eq!(a.history[0].user.as_deref(), Some("op"));
    let stored = s.get_alert("a1", None).await.unwrap();
    assert_eq!(stored.history[0], a.history[0]);
}

#[tokio::test]
async fn two_claimants_refuse_with_503_and_change_nothing() {
    let Some(db) = test_db().await else { return };
    let log = Arc::new(Mutex::new(Vec::new()));
    let s = setup(
        &db,
        vec![
            hook(Fake::new("A", &log, &db.pool).claiming()),
            hook(Fake::new("B", &log, &db.pool).claiming()),
        ],
    )
    .await;
    let before = snapshot(&db.pool).await;
    let e = s
        .take_action_with("a1", None, "ack", "seen", None, &caller())
        .await
        .unwrap_err();
    assert!(
        matches!(&e, StoreError::Hook(h) if h.status == 503 && h.message == MULTIPLE_OWNERS),
        "{e}"
    );
    assert_eq!(
        calls(&log),
        ["A:pre_lookup", "B:pre_lookup", "A:claims", "B:claims"]
    );
    assert_eq!(snapshot(&db.pool).await, before);
}

#[tokio::test]
async fn a_refusal_at_any_phase_rolls_everything_back_and_stops() {
    for (at, expected) in [
        (FailAt::Gate, vec!["A:pre_lookup", "A:claims", "A:gate"]),
        (
            FailAt::Stage,
            vec!["A:pre_lookup", "A:claims", "A:gate", "A:stage"],
        ),
        (
            FailAt::PreCommit,
            vec![
                "A:pre_lookup",
                "A:claims",
                "A:gate",
                "A:stage",
                "A:pre_commit",
            ],
        ),
    ] {
        let Some(db) = test_db().await else { return };
        let log = Arc::new(Mutex::new(Vec::new()));
        let mut fake = Fake::new("A", &log, &db.pool).claiming();
        fake.fail = at;
        let s = setup(&db, vec![hook(fake)]).await;
        let before = snapshot(&db.pool).await;
        let e = s
            .take_action_with("a1", None, "ack", "seen", None, &caller())
            .await
            .unwrap_err();
        assert!(
            matches!(&e, StoreError::Hook(h) if h.status == 409 && h.message == format!("A refused at {at:?}")),
            "{at:?}: {e}"
        );
        assert_eq!(calls(&log), expected, "{at:?}");
        // Including the staging SQL's own write and alrust's UPDATE.
        assert_eq!(snapshot(&db.pool).await, before, "{at:?}");
    }
}

#[tokio::test]
async fn pre_lookup_refuses_before_the_alert_is_looked_up() {
    let Some(db) = test_db().await else { return };
    let log = Arc::new(Mutex::new(Vec::new()));
    let mut fake = Fake::new("A", &log, &db.pool).claiming();
    fake.fail = FailAt::PreLookup;
    let s = setup(&db, vec![hook(fake)]).await;
    // An id that does not exist: the refusal, not "not found".
    let e = s
        .take_action_with("no-such-alert", None, "ack", "x", None, &caller())
        .await
        .unwrap_err();
    assert!(matches!(&e, StoreError::Hook(h) if h.status == 409), "{e}");
    assert_eq!(calls(&log), ["A:pre_lookup"]);
}

#[tokio::test]
async fn a_declining_gate_skips_staging_and_carries_its_writes() {
    let Some(db) = test_db().await else { return };
    let log = Arc::new(Mutex::new(Vec::new()));
    let mut fake = Fake::new("A", &log, &db.pool).claiming();
    fake.skip = true;
    let s = setup(&db, vec![hook(fake)]).await;
    let a = s
        .take_action_with("a1", None, "ack", "seen", None, &caller())
        .await
        .unwrap();
    assert_eq!(
        calls(&log),
        ["A:pre_lookup", "A:claims", "A:gate", "A:pre_commit"]
    );
    assert_eq!(a.history[0].text.as_deref(), Some("seen"));
    let (status, attributes, _) = snapshot(&db.pool).await;
    assert_eq!(status, "ack");
    assert_eq!(attributes, json!({"keep": 1, "sync": "skipped"}));
}

#[tokio::test]
async fn unclaimed_alerts_and_other_actions_run_as_without_hooks() {
    let Some(db) = test_db().await else { return };
    let log = Arc::new(Mutex::new(Vec::new()));
    let s = setup(&db, vec![hook(Fake::new("A", &log, &db.pool))]).await;
    let a = s
        .take_action_with("a1", None, "ack", "seen", None, &caller())
        .await
        .unwrap();
    assert_eq!(calls(&log), ["A:pre_lookup", "A:claims"]);
    assert_eq!(
        (a.status.as_str(), a.history[0].text.as_deref()),
        ("ack", Some("seen"))
    );
    let (_, attributes, _) = snapshot(&db.pool).await;
    assert_eq!(attributes, json!({"keep": 1}));

    // Any action but ack calls no hook at all, even for a claimed alert.
    let log2 = Arc::new(Mutex::new(Vec::new()));
    let s2 = s
        .clone()
        .with_integrations(vec![hook(Fake::new("B", &log2, &db.pool).claiming())]);
    s2.take_action_with("a1", None, "unack", "back", None, &caller())
        .await
        .unwrap();
    assert!(calls(&log2).is_empty(), "{:?}", calls(&log2));
}

/// An incoming alert carrying reserved keys next to ordinary ones.
fn with_reserved(id: &str) -> NewAlert {
    let mut n = new_alert(id);
    n.resource = "res-recv".into();
    n.attributes = serde_json::from_value(json!({
        "keep": 1,
        "zabalack_x": "forged",
        "zabbix_ack_sync_intent": {"forged": true},
        "ack_proof": "forged",
        "Zabalack_case": "kept"
    }))
    .unwrap();
    n
}

async fn stored_attributes(pool: &PgPool) -> Vec<Value> {
    sqlx::query_scalar("SELECT attributes FROM alerts WHERE resource = 'res-recv' ORDER BY id")
        .fetch_all(pool)
        .await
        .unwrap()
}

#[tokio::test]
async fn pre_receive_runs_for_every_integration_before_the_alert_is_stored() {
    let Some(db) = test_db().await else { return };
    let log = Arc::new(Mutex::new(Vec::new()));
    let s = setup(
        &db,
        vec![
            hook(Fake::new("A", &log, &db.pool)),
            hook(Fake::new("B", &log, &db.pool).claiming()),
        ],
    )
    .await;
    let (a, kind) = s
        .receive_with(with_reserved("r1"), &caller())
        .await
        .unwrap();
    assert_eq!(kind, alrust_store_pg::ReceiveKind::Created);
    assert_eq!(calls(&log), ["A:pre_receive", "B:pre_receive"]);
    let kept = json!({"keep": 1, "Zabalack_case": "kept"});
    assert_eq!(Value::Object(a.attributes.clone()), kept);
    assert_eq!(
        stored_attributes(&db.pool).await,
        std::slice::from_ref(&kept)
    );
    // A duplicate of the same alert is stripped too.
    let (_, kind) = s
        .receive_with(with_reserved("r2"), &caller())
        .await
        .unwrap();
    assert_eq!(kind, alrust_store_pg::ReceiveKind::Duplicate);
    assert_eq!(calls(&log).len(), 4);
    assert_eq!(stored_attributes(&db.pool).await, [kept]);
}

#[tokio::test]
async fn a_pre_receive_refusal_stores_nothing() {
    let Some(db) = test_db().await else { return };
    let log = Arc::new(Mutex::new(Vec::new()));
    let mut fake = Fake::new("A", &log, &db.pool);
    fake.fail = FailAt::PreReceive;
    let s = setup(&db, vec![hook(fake)]).await;
    let e = s
        .receive_with(with_reserved("r1"), &caller())
        .await
        .unwrap_err();
    assert!(matches!(&e, StoreError::Hook(h) if h.status == 409), "{e}");
    assert_eq!(calls(&log), ["A:pre_receive"]);
    assert_eq!(
        stored_attributes(&db.pool).await,
        Vec::<serde_json::Value>::new()
    );
}

// Nothing strips unconditionally: with no integration registered, reserved
// keys are stored exactly as received (and hidden on read by section 4.2).
#[tokio::test]
async fn without_an_integration_attributes_are_stored_as_received() {
    let Some(db) = test_db().await else { return };
    let s = setup(&db, vec![]).await;
    s.receive_with(with_reserved("r1"), &caller())
        .await
        .unwrap();
    assert_eq!(
        stored_attributes(&db.pool).await,
        [
            json!({"keep": 1, "zabalack_x": "forged", "zabbix_ack_sync_intent": {"forged": true},
                "ack_proof": "forged", "Zabalack_case": "kept"})
        ]
    );
}
