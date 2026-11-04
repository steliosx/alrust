//! Operator Bearer tokens: the trusted boundary of `spec/oidc-caller-facts.md`
//! (sections 4 to 6) with the owner's choices of DECISIONS Y50 and Y51.
//!
//! [`verify_bearer`] is the only way to obtain a [`VerifiedToken`]; its claims
//! are private, so no request body, query or other header can add to them.
//! It reads no file, environment or clock: the key, the audience and the
//! current time are passed in. Errors carry one of three fixed messages and
//! never any part of the token.
//!
//! Verification follows the reference's `PyJWT` 2.12.1 order (`api_jws.py`
//! `_load` and `_verify_signature`, then `api_jwt.py` `_validate_claims`),
//! then the reference's own `Jwt.parse` (`alerta/models/token.py:42-76`).
//! Decoding follows the spec's decoding profile (Y51): URL-safe base64url and
//! UTF-8 JSON as `serde_json` parses it; anything else is refused.

use std::fmt;

use base64::Engine as _;
use base64::alphabet::URL_SAFE;
use base64::engine::{DecodePaddingMode, GeneralPurpose, GeneralPurposeConfig};
use hmac::{Hmac, Mac};
use serde_json::{Map, Value};
use sha2::Sha256;

/// The claim holding an operator's customers for the integration hooks only
/// (spec section 6, item 4; never global filtering).
pub const HOOK_CUSTOMERS_CLAIM: &str = "alrust_hook_customers";

/// The only accepted `alg` header value.
const ALGORITHM: &str = "HS256";

/// URL-safe base64 with the profile's two allowances, both as `PyJWT`'s
/// `base64url_decode` behaves (`jwt/utils.py:25-33`): canonical padding may
/// be present or absent, and unused trailing bits are ignored. Any other
/// character, non-canonical padding or data after padding is refused (Y51).
const BASE64URL: GeneralPurpose = GeneralPurpose::new(
    &URL_SAFE,
    GeneralPurposeConfig::new()
        .with_decode_padding_mode(DecodePaddingMode::Indifferent)
        .with_decode_allow_trailing_bits(true),
);

/// Why a recognised Bearer token was refused. Each maps to one 401 message
/// (SF-MGMT item 3; DECISIONS Y50).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BearerError {
    /// 401 `Token is invalid`.
    Invalid,
    /// 401 `Token has expired`.
    Expired,
    /// 401 `Invalid audience`.
    Audience,
}

impl BearerError {
    /// The response message, fixed per error.
    pub fn message(self) -> &'static str {
        match self {
            BearerError::Invalid => "Token is invalid",
            BearerError::Expired => "Token has expired",
            BearerError::Audience => "Invalid audience",
        }
    }
}

impl fmt::Display for BearerError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.message())
    }
}

impl std::error::Error for BearerError {}

/// A token that passed [`verify_bearer`]. Only that function builds one.
pub struct VerifiedToken {
    claims: Map<String, Value>,
}

impl fmt::Debug for VerifiedToken {
    /// Opaque: claims are never printed.
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("VerifiedToken { .. }")
    }
}

/// The facts a verified operator gives the integration hooks (spec section
/// 6). Built only by [`VerifiedToken::operator_facts`]; all or nothing.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OperatorFacts {
    user_id: String,
    login: String,
    auth_type: String,
    customers: Vec<String>,
}

impl OperatorFacts {
    /// The token's `sub` (A1).
    pub fn user_id(&self) -> &str {
        &self.user_id
    }

    /// The token's `preferred_username` (A1).
    pub fn login(&self) -> &str {
        &self.login
    }

    /// `bearer:<provider>` (A1).
    pub fn auth_type(&self) -> &str {
        &self.auth_type
    }

    /// The hook-customers claim, sorted and de-duplicated; may be empty (A2).
    pub fn customers(&self) -> &[String] {
        &self.customers
    }
}

/// Verifies an HS256 Bearer token signed with `key` for `audience`, at
/// `now` in whole seconds since the epoch (the current time rounded down).
///
/// # Errors
///
/// [`BearerError::Expired`] when `exp <= now`; [`BearerError::Audience`] when
/// `aud` is missing, empty, malformed or does not contain `audience`;
/// otherwise [`BearerError::Invalid`] for any other failure (spec section 4,
/// DECISIONS Y50), checked in the reference's order.
pub fn verify_bearer(
    token: &str,
    key: &[u8],
    audience: &str,
    now: i64,
) -> Result<VerifiedToken, BearerError> {
    use BearerError::{Audience, Expired, Invalid};

    // api_jws.py _load: rsplit once for the signature, then split once.
    let (signing_input, signature) = token.rsplit_once('.').ok_or(Invalid)?;
    let (header, payload) = signing_input.split_once('.').ok_or(Invalid)?;
    let header = json_object(&BASE64URL.decode(header).map_err(|_| Invalid)?)?;
    let payload = BASE64URL.decode(payload).map_err(|_| Invalid)?;
    let signature = BASE64URL.decode(signature).map_err(|_| Invalid)?;

    // decode_complete: header checks (api_jws.py:240-250, :373-397), then
    // _verify_signature, where the algorithm is fixed, never taken from the
    // header (:342-348).
    if !header_ok(&header) || header.get("alg").and_then(Value::as_str) != Some(ALGORITHM) {
        return Err(Invalid);
    }
    let mut mac = Hmac::<Sha256>::new_from_slice(key).map_err(|_| Invalid)?;
    mac.update(signing_input.as_bytes());
    mac.verify_slice(&signature).map_err(|_| Invalid)?;

    let claims = json_object(&payload)?;
    let now = i128::from(now);

    // _validate_claims order (api_jwt.py:394-419): required (exp, Y50 O3b),
    // iat, nbf, exp, aud, sub, jti.
    let exp = claims.get("exp").ok_or(Invalid)?;
    for name in ["iat", "nbf"] {
        if let Some(v) = claims.get(name)
            && claim_int(v)? > now
        {
            return Err(Invalid);
        }
    }
    if claim_int(exp)? <= now {
        return Err(Expired);
    }
    check_audience(claims.get("aud"), audience).ok_or(Audience)?;
    for name in ["sub", "jti"] {
        if claims.get(name).is_some_and(|v| !v.is_string()) {
            return Err(Invalid);
        }
    }

    // Jwt.parse: a present non-string scope fails `.split` (token.py:71).
    if claims.get("scope").is_some_and(|v| !v.is_string()) {
        return Err(Invalid);
    }
    // Y50 O3a part 2: a login that is neither text nor null is refused.
    if claims
        .get("preferred_username")
        .is_some_and(|v| !v.is_string() && !v.is_null())
    {
        return Err(Invalid);
    }
    Ok(VerifiedToken { claims })
}

impl VerifiedToken {
    /// `Caller.login`: `preferred_username` as text, including `""`; `None`
    /// when absent or null (spec section 5).
    pub fn login(&self) -> Option<&str> {
        self.claims
            .get("preferred_username")
            .and_then(Value::as_str)
    }

    /// `Caller.scopes`: `scope` split on every single space, empty items
    /// kept; absent gives `[""]` (spec section 5, `token.py:71`).
    pub fn scopes(&self) -> Vec<String> {
        self.claims
            .get("scope")
            .and_then(Value::as_str)
            .unwrap_or("")
            .split(' ')
            .map(str::to_owned)
            .collect()
    }

    /// The hook-only facts for the configured OIDC `provider`, or `None` when
    /// any check of spec section 6 fails. `None` never refuses the request.
    pub fn operator_facts(&self, provider: &str) -> Option<OperatorFacts> {
        let user_id = self.non_empty("sub")?;
        let login = self.non_empty("preferred_username")?;
        if provider.is_empty()
            || self.claims.get("provider").and_then(Value::as_str) != Some(provider)
        {
            return None;
        }
        let mut customers = Vec::new();
        for item in self.claims.get(HOOK_CUSTOMERS_CLAIM)?.as_array()? {
            match item.as_str() {
                Some(c) if !c.is_empty() => customers.push(c.to_owned()),
                _ => return None,
            }
        }
        customers.sort();
        customers.dedup();
        Some(OperatorFacts {
            user_id: user_id.to_owned(),
            login: login.to_owned(),
            auth_type: format!("bearer:{provider}"),
            customers,
        })
    }

    fn non_empty(&self, name: &str) -> Option<&str> {
        self.claims
            .get(name)
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
    }
}

/// `PyJWT`'s header checks: a present `kid` is a string; a present `crit` is a
/// non-empty list naming only `b64`, which must then be present; and `b64`
/// is not `false` (a detached payload, which the reference never supplies).
fn header_ok(header: &Map<String, Value>) -> bool {
    let kid_ok = header.get("kid").is_none_or(Value::is_string);
    let crit_ok = header.get("crit").is_none_or(|crit| {
        crit.as_array().is_some_and(|items| {
            !items.is_empty()
                && items
                    .iter()
                    .all(|e| e.as_str() == Some("b64") && header.contains_key("b64"))
        })
    });
    let b64_ok = header.get("b64") != Some(&Value::Bool(false));
    kid_ok && crit_ok && b64_ok
}

/// Decoded bytes as a JSON object; anything else is invalid.
fn json_object(bytes: &[u8]) -> Result<Map<String, Value>, BearerError> {
    match serde_json::from_slice(bytes) {
        Ok(Value::Object(map)) => Ok(map),
        _ => Err(BearerError::Invalid),
    }
}

/// `aud` as `PyJWT` 2.12.1 checks it, non-strict (`api_jwt.py:510-558`), with
/// a missing or empty value as an audience failure (Y50 O3a part 1).
///
/// `PyJWT` first treats a missing or falsy `aud` as missing. Of the falsy
/// JSON values only `""` could still match (when `audience` is empty), so it
/// is refused here explicitly; `null`, `false`, `0`, `[]` and `{}` fail the
/// shape or membership checks below with the same error.
fn check_audience(aud: Option<&Value>, audience: &str) -> Option<()> {
    let claims = match aud? {
        Value::String(s) if !s.is_empty() => vec![s.as_str()],
        Value::Array(items) => items
            .iter()
            .map(Value::as_str)
            .collect::<Option<Vec<_>>>()?,
        _ => return None,
    };
    claims.contains(&audience).then_some(())
}

/// A time claim as Python's `int()` converts it in `PyJWT`'s checks
/// (`api_jwt.py:471-473`, `:486-488`, `:500-502`). Values beyond `i128`
/// saturate, which keeps every comparison with a real `now` unchanged.
///
/// Accepted: integers; finite floats, truncated toward zero; booleans (0 or
/// 1); strings of at most [`MAX_DIGITS`] ASCII decimal digits with an
/// optional sign, surrounding whitespace and single underscores between
/// digits. Refused, as the reference refuses them: other strings, null,
/// arrays and objects. Refused under the decoding profile (DECISIONS Y51),
/// though Python accepts them: non-ASCII decimal digits in a string.
fn claim_int(v: &Value) -> Result<i128, BearerError> {
    match v {
        Value::Bool(b) => Ok(i128::from(*b)),
        Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                Ok(i128::from(i))
            } else if let Some(u) = n.as_u64() {
                Ok(i128::from(u))
            } else {
                n.as_f64()
                    .filter(|f| f.is_finite())
                    .map(saturating_trunc)
                    .ok_or(BearerError::Invalid)
            }
        }
        Value::String(s) => python_int(s).ok_or(BearerError::Invalid),
        Value::Null | Value::Array(_) | Value::Object(_) => Err(BearerError::Invalid),
    }
}

#[expect(
    clippy::as_conversions,
    clippy::cast_possible_truncation,
    reason = "f64 to i128 `as` truncates toward zero and saturates, which is the intended conversion"
)]
fn saturating_trunc(f: f64) -> i128 {
    f as i128
}

/// Python 3.12's default `int()` limit on decimal digits
/// (`sys.get_int_max_str_digits()`); the reference refuses longer strings.
const MAX_DIGITS: usize = 4300;

/// Python `int(s)` in base 10 for ASCII digits (see [`claim_int`]).
fn python_int(s: &str) -> Option<i128> {
    let s = s.trim();
    let (negative, digits) = match s.as_bytes().first() {
        Some(b'-') => (true, &s[1..]),
        Some(b'+') => (false, &s[1..]),
        _ => (false, s),
    };
    let mut value: i128 = 0;
    let mut count = 0;
    let mut previous_digit = false;
    for b in digits.bytes() {
        match b {
            b'0'..=b'9' => {
                let d = i128::from(b - b'0');
                value = value.saturating_mul(10).saturating_add(d);
                count += 1;
                previous_digit = true;
            }
            b'_' if previous_digit => previous_digit = false,
            _ => return None,
        }
    }
    (previous_digit && count <= MAX_DIGITS).then_some(if negative { -value } else { value })
}
