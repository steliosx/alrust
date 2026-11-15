//! Blackout suppression on receive: the reference's default blackout plugin
//! (`plugins/blackout.py`) with `NOTIFICATION_BLACKOUT = False` and
//! `BLACKOUT_ACCEPT = []` (`settings.py:282-283`) drops an alert whose create
//! time falls inside a matching blackout period (`spec/lifecycle.md` R1
//! step 3).
//!
//! The match is `is_blackout_period` (`postgres/base.py:784-801`) as
//! written: environment equal; resource, event, group and origin equal or
//! unset; the blackout's `service` and `tags` unset, empty, or contained in
//! the alert's. `CUSTOMER_VIEWS` is off in alrust (`GET /config` reports
//! it), so, as in the reference, no customer predicate applies: a blackout
//! of any customer suppresses a matching alert of any customer.

use sqlx::Row as _;

use crate::{NewAlert, Store, StoreError, naive};

const IS_BLACKOUT_PERIOD: &str = r#"SELECT EXISTS (
    SELECT 1 FROM blackouts
     WHERE start_time <= $1 AND end_time > $1
       AND environment = $2
       AND (resource IS NULL OR resource = $3)
       AND (service IS NULL OR service = '{}' OR service <@ $4)
       AND (event IS NULL OR event = $5)
       AND ("group" IS NULL OR "group" = $6)
       AND (tags IS NULL OR tags = '{}' OR tags <@ $7)
       AND (origin IS NULL OR origin = $8)
) AS hit"#;

impl Store {
    /// Whether a blackout period covers the alert's create time.
    ///
    /// # Errors
    ///
    /// The database error.
    pub async fn in_blackout(&self, n: &NewAlert) -> Result<bool, StoreError> {
        let row = sqlx::query(IS_BLACKOUT_PERIOD)
            .bind(naive(n.create_time))
            .bind(&n.environment)
            .bind(&n.resource)
            .bind(&n.service)
            .bind(&n.event)
            .bind(&n.group)
            .bind(&n.tags)
            .bind(&n.origin)
            .fetch_one(&self.pool)
            .await?;
        Ok(row.try_get("hit")?)
    }
}
