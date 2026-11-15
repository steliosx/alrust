//! Scopes and the built-in roles (reference `models/enums.py:55-86`,
//! `:144`; `settings.py:64-75`).
//!
//! alrust has no `CUSTOM_SCOPES`, `ADMIN_USERS`, `DELETE_SCOPES` or role-name
//! settings, so they keep the reference's defaults: no custom scopes, no
//! admin users, no delete scopes, and the roles `admin`, `user` and `guest`.

/// `Scope.find_all()`: the built-in scopes, in definition order.
pub const KNOWN_SCOPES: [&str; 29] = [
    "read",
    "write",
    "admin",
    "read:alerts",
    "write:alerts",
    "delete:alerts",
    "admin:alerts",
    "read:blackouts",
    "write:blackouts",
    "admin:blackouts",
    "read:heartbeats",
    "write:heartbeats",
    "admin:heartbeats",
    "write:users",
    "admin:users",
    "read:groups",
    "admin:groups",
    "read:perms",
    "admin:perms",
    "read:customers",
    "admin:customers",
    "read:keys",
    "write:keys",
    "admin:keys",
    "write:webhooks",
    "read:oembed",
    "read:management",
    "admin:management",
    "read:userinfo",
];

/// Whether `scope` is one of [`KNOWN_SCOPES`].
pub fn is_known_scope(scope: &str) -> bool {
    KNOWN_SCOPES.contains(&scope)
}

/// `ADMIN_SCOPES`.
pub const ADMIN_SCOPES: [&str; 3] = ["admin", "read", "write"];

/// `DEFAULT_ADMIN_ROLE`, the only `ADMIN_ROLES` entry.
pub const ADMIN_ROLE: &str = "admin";
/// `DEFAULT_USER_ROLE`, the only `USER_ROLES` entry.
pub const USER_ROLE: &str = "user";
/// `DEFAULT_GUEST_ROLE`, the only `GUEST_ROLES` entry.
pub const GUEST_ROLE: &str = "guest";

/// `USER_DEFAULT_SCOPES`.
pub const USER_DEFAULT_SCOPES: [&str; 2] = ["read", "write"];
/// `GUEST_DEFAULT_SCOPES`.
pub const GUEST_DEFAULT_SCOPES: [&str; 1] = ["read:alerts"];

/// The role names a permission row may not take
/// (`views/permissions.py:26-31`).
pub const RESERVED_ROLES: [&str; 3] = [ADMIN_ROLE, USER_ROLE, GUEST_ROLE];
