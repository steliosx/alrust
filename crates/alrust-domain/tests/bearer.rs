//! Bearer verification and conversion (spec/oidc-caller-facts.md sections
//! 4 to 6 and 9; DECISIONS Y50). These tests sit outside the crate, so they
//! can reach a `VerifiedToken` only through `verify_bearer`, with tokens made
//! by the small HS256 signer below from a synthetic key.

use alrust_domain::bearer::{BearerError, HOOK_CUSTOMERS_CLAIM, verify_bearer};
use base64::Engine as _;
use base64::engine::general_purpose::{URL_SAFE, URL_SAFE_NO_PAD};
use hmac::{Hmac, Mac};
use serde_json::{Value, json};
use sha2::Sha256;

const KEY: &[u8] = b"synthetic-test-key-not-a-secret-0123456789";
const AUD: &str = "alrust-test-audience";
const NOW: i64 = 1_800_000_000;
const PROVIDER: &str = "keycloak-test";

fn sign(signing_input: &str, key: &[u8]) -> String {
    let mut mac = Hmac::<Sha256>::new_from_slice(key).expect("HMAC takes any key length");
    mac.update(signing_input.as_bytes());
    URL_SAFE_NO_PAD.encode(mac.finalize().into_bytes())
}

fn mint_raw(header: &Value, claims: &Value, key: &[u8]) -> String {
    let input = format!(
        "{}.{}",
        URL_SAFE_NO_PAD.encode(header.to_string()),
        URL_SAFE_NO_PAD.encode(claims.to_string())
    );
    let sig = sign(&input, key);
    format!("{input}.{sig}")
}

fn mint(claims: &Value) -> String {
    mint_raw(&json!({"alg": "HS256", "typ": "JWT"}), claims, KEY)
}

fn base() -> Value {
    json!({
        "sub": "user-1",
        "preferred_username": "ops@example.test",
        "provider": PROVIDER,
        "scope": "read write",
        "aud": AUD,
        "exp": NOW + 3600,
        "iat": NOW - 10,
        "nbf": NOW - 10,
        HOOK_CUSTOMERS_CLAIM: ["b", "a", "a"],
    })
}

fn with(key: &str, value: Value) -> Value {
    let mut c = base();
    c[key] = value;
    c
}

fn without(key: &str) -> Value {
    let mut c = base();
    c.as_object_mut().expect("object").remove(key);
    c
}

fn verify(token: &str) -> Result<alrust_domain::bearer::VerifiedToken, BearerError> {
    verify_bearer(token, KEY, AUD, NOW)
}

fn err(claims: &Value) -> BearerError {
    verify(&mint(claims)).expect_err("refused")
}

fn ok(claims: &Value) -> alrust_domain::bearer::VerifiedToken {
    verify(&mint(claims)).expect("accepted")
}

#[test]
fn valid_token_gives_global_and_hook_facts() {
    let t = ok(&base());
    assert_eq!(t.login(), Some("ops@example.test"));
    assert_eq!(t.scopes(), vec!["read".to_owned(), "write".to_owned()]);
    let f = t.operator_facts(PROVIDER).expect("facts");
    assert_eq!(f.user_id(), "user-1");
    assert_eq!(f.login(), "ops@example.test");
    assert_eq!(f.auth_type(), "bearer:keycloak-test");
    assert_eq!(f.customers(), ["a".to_owned(), "b".to_owned()]);
}

#[test]
fn error_messages_are_the_three_fixed_strings() {
    assert_eq!(BearerError::Invalid.message(), "Token is invalid");
    assert_eq!(BearerError::Expired.message(), "Token has expired");
    assert_eq!(BearerError::Audience.message(), "Invalid audience");
    assert_eq!(BearerError::Expired.to_string(), "Token has expired");
}

#[test]
fn nothing_from_the_token_appears_in_errors_or_debug() {
    let token = mint(&with("exp", json!(NOW)));
    let e = verify(&token).expect_err("expired");
    assert!(!format!("{e:?}{e}").contains("ops@example.test"));
    let t = ok(&base());
    assert_eq!(format!("{t:?}"), "VerifiedToken { .. }");
}

#[test]
fn malformed_tokens_are_invalid() {
    let good = mint(&base());
    let (input, _) = good.rsplit_once('.').expect("three segments");
    let header_ok = URL_SAFE_NO_PAD.encode(json!({"alg": "HS256"}).to_string());
    let payload_ok = URL_SAFE_NO_PAD.encode(base().to_string());
    let cases = [
        String::new(),
        "abc".to_owned(),
        "a.b".to_owned(),
        format!("{input}."),
        format!("{input}.!!!"),
        // header not base64, not JSON, not an object
        format!("%%%.{payload_ok}.x"),
        {
            let i = format!("{}.{payload_ok}", URL_SAFE_NO_PAD.encode("not json"));
            format!("{i}.{}", sign(&i, KEY))
        },
        {
            let i = format!("{}.{payload_ok}", URL_SAFE_NO_PAD.encode("[1]"));
            format!("{i}.{}", sign(&i, KEY))
        },
        // payload not an object, not JSON
        {
            let i = format!("{header_ok}.{}", URL_SAFE_NO_PAD.encode("[1]"));
            format!("{i}.{}", sign(&i, KEY))
        },
        {
            let i = format!("{header_ok}.{}", URL_SAFE_NO_PAD.encode("{"));
            format!("{i}.{}", sign(&i, KEY))
        },
        // a character outside the URL-safe alphabet
        format!("{input}.+{}", &good[input.len() + 2..]),
    ];
    for token in &cases {
        assert_eq!(verify(token).err(), Some(BearerError::Invalid), "{token}");
    }
}

#[test]
fn bad_signature_or_key_is_invalid() {
    let token = mint(&base());
    assert_eq!(
        verify_bearer(&token, b"another-key", AUD, NOW).err(),
        Some(BearerError::Invalid)
    );
    let mut tampered = token;
    tampered.push('A');
    assert_eq!(verify(&tampered).err(), Some(BearerError::Invalid));
    let other = mint_raw(
        &json!({"alg": "HS256"}),
        &with("sub", json!("user-2")),
        b"x",
    );
    assert_eq!(verify(&other).err(), Some(BearerError::Invalid));
}

#[test]
fn only_hs256_from_the_header_is_accepted() {
    for alg in [
        json!("none"),
        json!("HS512"),
        json!("hs256"),
        json!(["HS256"]),
        json!(null),
    ] {
        let t = mint_raw(&json!({"alg": alg}), &base(), KEY);
        assert_eq!(verify(&t).err(), Some(BearerError::Invalid), "{alg}");
    }
    let t = mint_raw(&json!({"typ": "JWT"}), &base(), KEY);
    assert_eq!(verify(&t).err(), Some(BearerError::Invalid));
}

#[test]
fn header_kid_crit_and_b64_follow_pyjwt() {
    let accepted = [
        json!({"alg": "HS256", "kid": "k1"}),
        json!({"alg": "HS256", "crit": ["b64"], "b64": true}),
        json!({"alg": "HS256", "b64": true}),
    ];
    for h in &accepted {
        assert!(verify(&mint_raw(h, &base(), KEY)).is_ok(), "{h}");
    }
    let refused = [
        json!({"alg": "HS256", "kid": 7}),
        json!({"alg": "HS256", "crit": []}),
        json!({"alg": "HS256", "crit": "b64", "b64": true}),
        json!({"alg": "HS256", "crit": ["exp"], "exp": 1}),
        json!({"alg": "HS256", "crit": ["b64"]}),
        json!({"alg": "HS256", "crit": [1], "b64": true}),
        json!({"alg": "HS256", "b64": false}),
    ];
    for h in &refused {
        assert_eq!(
            verify(&mint_raw(h, &base(), KEY)).err(),
            Some(BearerError::Invalid),
            "{h}"
        );
    }
}

#[test]
fn padded_segments_are_accepted() {
    let input = format!(
        "{}.{}",
        URL_SAFE.encode(json!({"alg": "HS256"}).to_string()),
        URL_SAFE.encode(with("sub", json!("u")).to_string())
    );
    let mut mac = Hmac::<Sha256>::new_from_slice(KEY).expect("key");
    mac.update(input.as_bytes());
    let token = format!("{input}.{}", URL_SAFE.encode(mac.finalize().into_bytes()));
    assert!(token.contains('='));
    assert!(verify(&token).is_ok());
}

#[test]
fn exp_is_required_and_expires_at_now() {
    // Y50 O3b.
    assert_eq!(err(&without("exp")), BearerError::Invalid);
    assert_eq!(err(&with("exp", json!(NOW))), BearerError::Expired);
    assert_eq!(err(&with("exp", json!(NOW - 1))), BearerError::Expired);
    assert!(verify(&mint(&with("exp", json!(NOW + 1)))).is_ok());
}

#[test]
fn nbf_and_iat_in_the_future_are_invalid() {
    for name in ["nbf", "iat"] {
        assert_eq!(
            err(&with(name, json!(NOW + 1))),
            BearerError::Invalid,
            "{name}"
        );
        assert!(verify(&mint(&with(name, json!(NOW)))).is_ok(), "{name}");
        assert!(verify(&mint(&with(name, json!(NOW - 1)))).is_ok(), "{name}");
        assert!(verify(&mint(&without(name))).is_ok(), "{name}");
    }
}

#[test]
fn time_claims_convert_like_python_int() {
    let accepted = [
        json!((NOW + 1).to_string()),
        json!(format!(" +{} \n", NOW + 1)),
        json!("1_800_000_001"),
        json!("0001800000001"),
        json!(1.8e9 + 1.9),
        json!(1e30),
        json!("99999999999999999999999999999999999999999999"),
    ];
    for v in &accepted {
        assert!(verify(&mint(&with("exp", v.clone()))).is_ok(), "{v}");
    }
    // Truncation toward zero and booleans make these expire.
    for v in [json!(1.8e9 + 0.9), json!(true), json!(false), json!("-5")] {
        assert_eq!(err(&with("exp", v.clone())), BearerError::Expired, "{v}");
    }
    let refused = [
        json!(null),
        json!([]),
        json!({}),
        json!(""),
        json!("-"),
        json!("1.5e9"),
        json!("1_"),
        json!("_1"),
        json!("1__0"),
        json!("0x10"),
        json!("\u{0661}\u{0662}"),
    ];
    for name in ["exp", "nbf", "iat"] {
        for v in &refused {
            assert_eq!(
                err(&with(name, v.clone())),
                BearerError::Invalid,
                "{name} {v}"
            );
        }
    }
}

#[test]
fn checks_run_in_the_reference_order() {
    // Required exp before everything else in the payload; iat before exp;
    // exp before aud; aud before sub and the later checks.
    let mut c = without("exp");
    c["aud"] = json!("someone-else");
    assert_eq!(err(&c), BearerError::Invalid);
    let mut c = with("iat", json!(NOW + 5));
    c["exp"] = json!(NOW - 5);
    assert_eq!(err(&c), BearerError::Invalid);
    let mut c = with("exp", json!(NOW - 5));
    c["aud"] = json!("someone-else");
    assert_eq!(err(&c), BearerError::Expired);
    let mut c = with("aud", json!("someone-else"));
    c["sub"] = json!(5);
    assert_eq!(err(&c), BearerError::Audience);
}

#[test]
fn audience_must_be_present_and_contain_ours() {
    for aud in [
        Value::Null,
        json!(""),
        json!([]),
        json!({}),
        json!(0),
        json!(false),
        json!("someone-else"),
        json!(["someone-else"]),
        json!([AUD, 5]),
        json!(5),
        json!({"a": AUD}),
    ] {
        assert_eq!(
            err(&with("aud", aud.clone())),
            BearerError::Audience,
            "{aud}"
        );
    }
    assert_eq!(err(&without("aud")), BearerError::Audience);
    assert!(verify(&mint(&with("aud", json!(["x", AUD])))).is_ok());
}

#[test]
fn sub_and_jti_must_be_strings_when_present() {
    for name in ["sub", "jti"] {
        for v in [json!(5), json!(null), json!([]), json!({})] {
            assert_eq!(
                err(&with(name, v.clone())),
                BearerError::Invalid,
                "{name} {v}"
            );
        }
    }
    assert!(verify(&mint(&with("jti", json!("j-1")))).is_ok());
}

#[test]
fn scope_keeps_empty_items_and_refuses_non_strings() {
    assert_eq!(ok(&without("scope")).scopes(), vec![String::new()]);
    assert_eq!(ok(&with("scope", json!(""))).scopes(), vec![String::new()]);
    assert_eq!(
        ok(&with("scope", json!("write:keys  read"))).scopes(),
        vec!["write:keys".to_owned(), String::new(), "read".to_owned()]
    );
    assert_eq!(
        ok(&with("scope", json!("write:keys  read")))
            .scopes()
            .join(","),
        "write:keys,,read"
    );
    for v in [json!(null), json!(5), json!(["read"]), json!({})] {
        assert_eq!(err(&with("scope", v.clone())), BearerError::Invalid, "{v}");
    }
}

#[test]
fn login_outcomes_per_shape() {
    // Y50 O3a part 2: present and neither text nor null is refused.
    for v in [json!(5), json!(true), json!(["a"]), json!({"a": 1})] {
        assert_eq!(
            err(&with("preferred_username", v.clone())),
            BearerError::Invalid,
            "{v}"
        );
    }
    let absent = ok(&without("preferred_username"));
    assert_eq!(absent.login(), None);
    assert!(absent.operator_facts(PROVIDER).is_none());
    let null = ok(&with("preferred_username", Value::Null));
    assert_eq!(null.login(), None);
    assert!(null.operator_facts(PROVIDER).is_none());
    let empty = ok(&with("preferred_username", json!("")));
    assert_eq!(empty.login(), Some(""));
    assert!(empty.operator_facts(PROVIDER).is_none());
}

#[test]
fn missing_or_empty_sub_withholds_hook_facts_only() {
    for c in [without("sub"), with("sub", json!(""))] {
        let t = ok(&c);
        assert_eq!(t.login(), Some("ops@example.test"));
        assert!(t.operator_facts(PROVIDER).is_none());
    }
}

#[test]
fn provider_must_equal_the_configured_one() {
    for c in [
        without("provider"),
        with("provider", Value::Null),
        with("provider", json!(5)),
        with("provider", json!("basic")),
        with("provider", json!("ldap")),
        with("provider", json!("other")),
        with("provider", json!("")),
    ] {
        assert!(ok(&c).operator_facts(PROVIDER).is_none(), "{c}");
    }
    assert!(
        ok(&with("provider", json!("")))
            .operator_facts("")
            .is_none()
    );
    assert!(ok(&base()).operator_facts("").is_none());
}

#[test]
fn hook_customers_claim_is_all_or_nothing() {
    for v in [
        Value::Null,
        json!("a"),
        json!([1]),
        json!([""]),
        json!(["a", ""]),
        json!({"a": 1}),
    ] {
        assert!(
            ok(&with(HOOK_CUSTOMERS_CLAIM, v.clone()))
                .operator_facts(PROVIDER)
                .is_none(),
            "{v}"
        );
    }
    assert!(
        ok(&without(HOOK_CUSTOMERS_CLAIM))
            .operator_facts(PROVIDER)
            .is_none()
    );
    let empty = ok(&with(HOOK_CUSTOMERS_CLAIM, json!([]))).operator_facts(PROVIDER);
    assert!(empty.expect("facts").customers().is_empty());
}

#[test]
fn the_customers_claim_is_not_the_hook_claim() {
    // A reference-style `customers` claim gives no hook facts on its own.
    let mut c = without(HOOK_CUSTOMERS_CLAIM);
    c["customers"] = json!(["a"]);
    assert!(ok(&c).operator_facts(PROVIDER).is_none());
}

#[test]
fn non_canonical_trailing_bits_are_accepted() {
    // PyJWT's decoder ignores the unused low bits of the last character;
    // so does alrust's.
    const ALPHABET: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
    let token = mint(&base());
    let last = token.bytes().last().expect("signature");
    let index = ALPHABET.iter().position(|&c| c == last).expect("url-safe");
    let flipped = char::from(ALPHABET[index ^ 1]);
    let mut changed = token[..token.len() - 1].to_owned();
    changed.push(flipped);
    assert_ne!(changed, token);
    assert!(verify(&changed).is_ok());
}

#[test]
fn an_empty_aud_string_never_matches_even_an_empty_audience() {
    let token = mint(&with("aud", json!("")));
    assert_eq!(
        verify_bearer(&token, KEY, "", NOW).err(),
        Some(BearerError::Audience)
    );
    // A non-empty list is truthy in the reference, so `[""]` can match.
    let token = mint(&with("aud", json!([""])));
    assert!(verify_bearer(&token, KEY, "", NOW).is_ok());
}

#[test]
fn booleans_and_signs_convert_exactly() {
    // At now = 0: true is 1 (not expired), false is 0 (expired), "-5" is
    // negative (expired), "+5" is positive.
    let at_zero = |exp: Value| {
        let mut c = without("iat");
        c.as_object_mut().expect("object").remove("nbf");
        c["exp"] = exp;
        verify_bearer(&mint(&c), KEY, AUD, 0).err()
    };
    assert_eq!(at_zero(json!(true)), None);
    assert_eq!(at_zero(json!(false)), Some(BearerError::Expired));
    assert_eq!(at_zero(json!("-5")), Some(BearerError::Expired));
    assert_eq!(at_zero(json!(" -5 ")), Some(BearerError::Expired));
    assert_eq!(at_zero(json!("+5")), None);
    assert_eq!(at_zero(json!(-0.5)), Some(BearerError::Expired));
}

#[test]
fn an_out_of_range_json_number_is_invalid_json() {
    // serde_json refuses a number that overflows f64, so no infinite time
    // claim can reach the conversion.
    let header = URL_SAFE_NO_PAD.encode(r#"{"alg":"HS256"}"#);
    let payload = URL_SAFE_NO_PAD.encode(format!(r#"{{"sub":"u","aud":"{AUD}","exp":1e400}}"#));
    let input = format!("{header}.{payload}");
    let token = format!("{input}.{}", sign(&input, KEY));
    assert_eq!(verify(&token).err(), Some(BearerError::Invalid));
}

// Decoding profile (spec section 4; DECISIONS Y51). Each token below is
// signed over its own encoded segments exactly as written; nothing is
// re-encoded before verification.

fn signed(header_seg: &str, payload_seg: &str) -> String {
    let input = format!("{header_seg}.{payload_seg}");
    let sig = sign(&input, KEY);
    format!("{input}.{sig}")
}

fn hs256() -> String {
    URL_SAFE_NO_PAD.encode(r#"{"alg":"HS256"}"#)
}

fn payload_bytes(bytes: &[u8]) -> String {
    signed(&hs256(), &URL_SAFE_NO_PAD.encode(bytes))
}

/// `base()` as JSON text with `extra` spliced in before the closing brace.
fn payload_text(extra: &str) -> String {
    let text = base().to_string();
    let body = text.strip_suffix('}').expect("object");
    payload_bytes(format!("{body},{extra}}}").as_bytes())
}

#[test]
fn segments_outside_the_url_safe_alphabet_are_refused() {
    let header = hs256();
    let payload = URL_SAFE_NO_PAD.encode(base().to_string());
    let (left, right) = payload.split_at(8);
    let cases = [
        signed(&header, &format!("{left}+{right}")),
        signed(&header, &format!("{left}/{right}")),
        signed(&header, &format!("{left}!{right}")),
        signed(&header, &format!("{left}\u{e9}{right}")),
        signed(&header, &format!("{left} {right}")),
        // A further `.` stays inside the payload segment and is refused.
        signed(&header, &format!("{left}.{right}")),
        // Non-canonical padding; padding and trailing data together.
        signed(&format!("{header}="), &payload),
        signed(
            &URL_SAFE.encode(r#"{"alg":"HS256","x":1}"#),
            &format!("{payload}==xy"),
        ),
    ];
    for token in &cases {
        assert_eq!(verify(token).err(), Some(BearerError::Invalid), "{token}");
    }
    // The same claims in canonical form are accepted.
    assert!(verify(&signed(&header, &payload)).is_ok());
}

#[test]
fn data_after_canonical_padding_is_refused() {
    let padded = URL_SAFE.encode(r#"{"alg":"HS256","x":12}"#);
    assert!(padded.ends_with("=="));
    let payload = URL_SAFE_NO_PAD.encode(base().to_string());
    // Canonical padding alone is accepted ...
    assert!(verify(&signed(&padded, &payload)).is_ok());
    // ... but data after it is refused.
    let token = signed(&format!("{padded}xy"), &payload);
    assert_eq!(verify(&token).err(), Some(BearerError::Invalid));
}

#[test]
fn json_outside_the_profile_is_refused() {
    let json = base().to_string();
    let mut bom = vec![0xEF, 0xBB, 0xBF];
    bom.extend_from_slice(json.as_bytes());
    let mut utf16 = vec![0xFF, 0xFE];
    for unit in json.encode_utf16() {
        utf16.extend_from_slice(&unit.to_le_bytes());
    }
    let mut utf32 = vec![0xFF, 0xFE, 0, 0];
    for c in json.chars() {
        utf32.extend_from_slice(&u32::from(c).to_le_bytes());
    }
    let mut cases = vec![
        payload_bytes(&bom),
        payload_bytes(&utf16),
        payload_bytes(&utf32),
    ];
    for extra in [
        r#""x":NaN"#,
        r#""x":Infinity"#,
        r#""x":-Infinity"#,
        r#""x":"\ud800""#,
        r#""x":1e400"#,
    ] {
        cases.push(payload_text(extra));
    }
    cases.push(payload_text(&format!(r#""x":1{}"#, "0".repeat(400))));
    // The payload object plus 127 arrays is 128 levels.
    cases.push(payload_text(&format!(
        r#""x":{}1{}"#,
        "[".repeat(127),
        "]".repeat(127)
    )));
    for token in &cases {
        assert_eq!(verify(token).err(), Some(BearerError::Invalid));
    }
}

#[test]
fn json_inside_the_profile_is_accepted() {
    // 127 levels (the object plus 126 arrays), a 300-digit integer, and an
    // integer beyond 64 bits read as f64.
    for extra in [
        format!(r#""x":{}1{}"#, "[".repeat(126), "]".repeat(126)),
        format!(r#""x":1{}"#, "0".repeat(299)),
        r#""x":123456789012345678901234567890"#.to_owned(),
    ] {
        assert!(verify(&payload_text(&extra)).is_ok(), "{extra}");
    }
}

#[test]
fn duplicate_keys_keep_the_last_value() {
    let expired_last = payload_text(&format!(r#""exp":{NOW}"#));
    assert_eq!(verify(&expired_last).err(), Some(BearerError::Expired));
    let mut c = base();
    c["exp"] = json!(NOW);
    let text = c.to_string();
    let body = text.strip_suffix('}').expect("object");
    let valid_last = payload_bytes(format!("{body},\"exp\":{}}}", NOW + 60).as_bytes());
    assert!(verify(&valid_last).is_ok());
}

#[test]
fn time_strings_follow_the_profile() {
    // Non-ASCII decimal digits: Arabic-Indic, fullwidth, mathematical bold.
    for v in ["\u{0661}\u{0662}", "\u{FF11}\u{FF12}", "\u{1D7CF}\u{1D7D0}"] {
        assert_eq!(err(&with("exp", json!(v))), BearerError::Invalid, "{v}");
    }
    // At most 4300 digits, as Python 3.12's int() allows.
    assert!(verify(&mint(&with("exp", json!("9".repeat(4300))))).is_ok());
    assert_eq!(
        err(&with("exp", json!("9".repeat(4301)))),
        BearerError::Invalid
    );
    // Underscores do not count as digits.
    let underscored = format!("{}9", "9_".repeat(4299));
    assert!(verify(&mint(&with("exp", json!(underscored)))).is_ok());
    // Whitespace that int() strips, including non-ASCII spaces.
    for v in [
        " 1800000001\n",
        "\u{a0}1800000001\u{3000}",
        "\t+1800000001\u{2008}",
    ] {
        assert!(verify(&mint(&with("exp", json!(v)))).is_ok(), "{v:?}");
    }
    // ASCII separators that int() does not strip.
    assert_eq!(
        err(&with("exp", json!("\u{1c}1800000001"))),
        BearerError::Invalid
    );
}

#[test]
fn numbers_that_underflow_read_as_zero() {
    // `1e-400` and `-1e-400` underflow to 0.0 (not refused), truncate to 0,
    // and so are expired at any real `now`; the same in `nbf` is valid.
    for v in ["1e-400", "-1e-400"] {
        let mut c = base();
        c.as_object_mut().expect("object").remove("exp");
        let text = c.to_string();
        let body = text.strip_suffix('}').expect("object");
        let token = payload_bytes(format!("{body},\"exp\":{v}}}").as_bytes());
        assert_eq!(verify(&token).err(), Some(BearerError::Expired), "{v}");
        let token = payload_text(&format!(r#""x":{v}"#));
        assert!(verify(&token).is_ok(), "{v}");
    }
    let mut c = without("nbf");
    c.as_object_mut().expect("object").remove("iat");
    let text = c.to_string();
    let body = text.strip_suffix('}').expect("object");
    let token = payload_bytes(format!("{body},\"nbf\":1e-400}}").as_bytes());
    assert!(verify(&token).is_ok());
}

#[test]
fn ordinary_unicode_claim_text_is_allowed() {
    // The decoding profile constrains encoding only, not claim text.
    let mut c = base();
    c["sub"] = json!("Ünïcødé-ид-用户");
    c["preferred_username"] = json!("opérateur@例え.test");
    c[HOOK_CUSTOMERS_CLAIM] = json!(["Kunde-Ä", "客户"]);
    c["scope"] = json!("read write:alerts");
    let t = ok(&c);
    assert_eq!(t.login(), Some("opérateur@例え.test"));
    let f = t.operator_facts(PROVIDER).expect("facts");
    assert_eq!(f.user_id(), "Ünïcødé-ид-用户");
    assert_eq!(f.login(), "opérateur@例え.test");
    assert_eq!(f.customers(), ["Kunde-Ä".to_owned(), "客户".to_owned()]);
}
