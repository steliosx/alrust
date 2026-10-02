"""Finalizer building blocks: credential registry, final-byte scanner and
guarded removal of owned resources.

Contract: docs/ci-launch-attestation-contract.md, sections 8 and 9. This
head holds library pieces only: no orchestration, no workflow wiring, and
nothing that runs on import. The orchestrator (quiescence gate, staging,
terminal receipt, manifest and seal) is a separate head built on these.

- The registry is the private list of every actual credential of the job.
  It is verified against the digest that its stage journal recorded, and
  every secret must be at least MIN_SECRET bytes, so that a weak value such
  as a default database password cannot turn the scan into noise. Python
  cannot guarantee that memory is erased; the registry is never written
  anywhere after loading.
- The scanner looks for each secret in its raw form and its JSON-escaped
  form, plus a fixed list of generic credential patterns. It reports the
  file and the kind of match, never the matched bytes.
- Guarded removal deletes an owned resource only when its path lies inside
  the job root and its device and inode equal those recorded when it was
  created; a directory tree is removed with the symlink-safe rmtree. Any
  mismatch leaves the resource untouched and UNPROVEN.
"""
import hashlib
import json
import os
import re
import shutil
import stat

from .stage_journal import JournalError, read_bounded, strict_json

# The removal below relies on rmtree not following symlinks inside a tree.
assert shutil.rmtree.avoids_symlink_attacks, "symlink-safe rmtree required"

SCHEMA_REGISTRY = "alrust-credential-registry/1"
REGISTRY_NAME = "credential-registry.json"
MAX_REGISTRY = 65536
MAX_SECRETS = 256
MIN_SECRET = 16
MAX_SECRET = 4096
MAX_SCANNED_FILE = 16 << 20

# Generic credential shapes, matched on raw bytes. Indexes are reported,
# never the matched text.
GENERIC_PATTERNS = (
    re.compile(rb"SYNTHETIC-[0-9a-f]{48}"),
    re.compile(rb"postgres(?:ql)?://[^\s\"']+:[^\s\"'@]+@"),
    re.compile(rb"Authorization:\s*\S", re.IGNORECASE),
    re.compile(rb"Bearer\s+\S"),
    re.compile(rb"-----BEGIN"),
)


class FinalizerError(ValueError):
    """Fixed codes only."""


def _require(condition, code):
    if not condition:
        raise FinalizerError(code)


def load_registry(job_root, expected_sha256):
    """Return the job's secrets as bytes, from job_root/credential-registry.json.

    The file must be a bounded regular file whose sha256 equals the digest
    its stage journal recorded. It must hold exactly
    {"schema": SCHEMA_REGISTRY, "secrets": [str, ...]}, with each secret a
    unique string of MIN_SECRET to MAX_SECRET UTF-8 bytes.
    """
    _require(type(expected_sha256) is str and len(expected_sha256) == 64
             and not expected_sha256.strip("0123456789abcdef"), "registry_digest_invalid")
    try:
        data = read_bounded(os.path.join(job_root, REGISTRY_NAME), MAX_REGISTRY)
    except JournalError:
        raise FinalizerError("registry_unreadable") from None
    except OSError:
        raise FinalizerError("registry_unreadable") from None
    _require(hashlib.sha256(data).hexdigest() == expected_sha256, "registry_digest_mismatch")
    try:
        record = strict_json(data, "registry_invalid")
    except JournalError:
        raise FinalizerError("registry_invalid") from None
    _require(type(record) is dict and set(record) == {"schema", "secrets"}
             and record["schema"] == SCHEMA_REGISTRY
             and type(record["secrets"]) is list
             and 1 <= len(record["secrets"]) <= MAX_SECRETS, "registry_invalid")
    secrets = []
    for value in record["secrets"]:
        _require(type(value) is str, "registry_invalid")
        try:
            encoded = value.encode("utf-8")
        except UnicodeError:
            # For example an escaped unpaired surrogate: valid JSON text,
            # but not a UTF-8 string. Refused without echoing it.
            raise FinalizerError("registry_invalid") from None
        _require(MIN_SECRET <= len(encoded) <= MAX_SECRET, "registry_secret_too_short"
                 if len(encoded) < MIN_SECRET else "registry_invalid")
        secrets.append(encoded)
    _require(len(set(secrets)) == len(secrets), "registry_invalid")
    return secrets


def secret_forms(secret):
    """The byte forms searched for one secret.

    Supported forms: the raw UTF-8 bytes; the JSON string form with ASCII
    escaping (json.dumps default, \\uXXXX for non-ASCII); and the JSON
    string form without ASCII escaping (ensure_ascii=False, UTF-8). Other
    encodings (for example base64 or URL encoding) are not searched; the
    generic patterns and the export allowlist must not rely on them.
    """
    text = secret.decode("utf-8")
    return {secret,
            json.dumps(text)[1:-1].encode("utf-8"),
            json.dumps(text, ensure_ascii=False)[1:-1].encode("utf-8")}


def scan_bytes(data, secrets):
    """Findings in DATA as fixed tuples: ("secret", index) for the registry
    entry at index, or ("pattern", index) for GENERIC_PATTERNS[index]."""
    findings = []
    for index, secret in enumerate(secrets):
        if any(form in data for form in secret_forms(secret)):
            findings.append(("secret", index))
    for index, pattern in enumerate(GENERIC_PATTERNS):
        if pattern.search(data):
            findings.append(("pattern", index))
    return findings


def scan_file(path, secrets):
    """Scan one bounded regular file's final bytes. An unreadable file is a
    finding too (("unreadable", 0)), never a clean result."""
    try:
        data = read_bounded(path, MAX_SCANNED_FILE)
    except (JournalError, OSError):
        return [("unreadable", 0)]
    return scan_bytes(data, secrets)


def _inside(job_root, path):
    return (type(path) is str and os.path.isabs(path)
            and os.path.normpath(path) == path
            and path.startswith(job_root + os.sep))


def _open_dir(name, dir_fd=None):
    return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                   dir_fd=dir_fd)


def remove_owned(job_root, resource, root_identity):
    """Remove one owned resource recorded as {"path", "dev", "ino", "type"}.

    Every step is anchored to directory descriptors (F1465-R3):
    - the job root is opened without following symlinks, and must have
      root_identity (its dev and ino from the core identity), the caller's
      uid and mode 0700;
    - each parent component is opened relative to the previous descriptor
      with O_NOFOLLOW, so a symlinked component is refused;
    - the final entry is inspected, removed (unlink, or the symlink-safe
      rmtree with dir_fd) and verified absent relative to the held parent
      descriptor. Replacing an ancestor with a symlink after it was opened
      cannot redirect any of these operations out of the verified hierarchy.
    Limitation, stated rather than claimed away: another process of the same
    uid can still substitute the final entry inside the verified parent
    between its inspection and its removal. That stays inside the job's own
    hierarchy; it cannot reach outside it.

    Returns {"path", "type", "outcome", "reason"}: REMOVED, ABSENT (already
    gone; not a proven removal) or UNPROVEN, with a fixed reason.
    """
    result = {"path": None, "type": None, "outcome": "UNPROVEN"}
    if not (type(resource) is dict and set(resource) == {"path", "dev", "ino", "type"}
            and resource["type"] in ("file", "dir")
            and type(resource["dev"]) is int and type(resource["ino"]) is int):
        return dict(result, reason="resource_record_invalid")
    path, kind = resource["path"], resource["type"]
    result.update(path=path if type(path) is str else None, type=kind)
    if not (type(root_identity) is tuple and len(root_identity) == 2
            and all(type(v) is int for v in root_identity)):
        return dict(result, reason="root_identity_invalid")
    if not _inside(job_root, path):
        return dict(result, reason="outside_job_root")
    parts = path[len(job_root) + 1:].split(os.sep)
    if any(part in ("", ".", "..") for part in parts):
        return dict(result, reason="outside_job_root")
    opened = []
    try:
        try:
            fd = _open_dir(job_root)
        except OSError:
            return dict(result, reason="root_unavailable")
        opened.append(fd)
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) != root_identity or info.st_uid != os.getuid() \
                or stat.S_IMODE(info.st_mode) != 0o700:
            return dict(result, reason="root_identity_mismatch")
        for part in parts[:-1]:
            try:
                fd = _open_dir(part, dir_fd=fd)
            except OSError:
                # Missing, not a directory, or a symlinked component.
                return dict(result, reason="ancestor_unavailable")
            opened.append(fd)
        name = parts[-1]
        try:
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            return dict(result, outcome="ABSENT", reason="already_absent")
        except OSError:
            return dict(result, reason="inspection_failed")
        expected_type = stat.S_ISDIR if kind == "dir" else stat.S_ISREG
        if not expected_type(info.st_mode):
            return dict(result, reason="type_mismatch")
        if (info.st_dev, info.st_ino) != (resource["dev"], resource["ino"]):
            return dict(result, reason="identity_mismatch")
        if info.st_uid != os.getuid():
            return dict(result, reason="owner_mismatch")
        try:
            if kind == "dir":
                shutil.rmtree(name, dir_fd=fd)
            else:
                os.unlink(name, dir_fd=fd)
        except OSError:
            return dict(result, reason="removal_failed")
        try:
            os.stat(name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            return dict(result, outcome="REMOVED", reason="removed_and_verified_absent")
        except OSError:
            return dict(result, reason="verification_failed")
        return dict(result, reason="still_present")
    finally:
        for descriptor in reversed(opened):
            os.close(descriptor)
