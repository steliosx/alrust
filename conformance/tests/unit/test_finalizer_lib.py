"""Offline controls for the finalizer building blocks.

Private temporary files and directories only; no process or network.
"""
import hashlib
import json
import os
from pathlib import Path

import pytest

from alrust_conformance import finalizer as fz

SECRET = "SYNTHETIC-" + "ab" * 24          # 58 bytes, synthetic only
OTHER = "x-synthetic-signing-key-0123456789"


@pytest.fixture
def root(tmp_path):
    job = tmp_path / "job"
    job.mkdir(mode=0o700)
    return job


def write_registry(root, secrets, schema=fz.SCHEMA_REGISTRY, raw=None):
    data = raw if raw is not None else json.dumps(
        {"schema": schema, "secrets": secrets}).encode()
    path = root / fz.REGISTRY_NAME
    path.write_bytes(data)
    path.chmod(0o600)
    return hashlib.sha256(data).hexdigest()


def test_registry_loads_verified_secrets(root):
    digest = write_registry(root, [SECRET, OTHER])
    assert fz.load_registry(str(root), digest) == [SECRET.encode(), OTHER.encode()]


@pytest.mark.parametrize("secrets,code", [
    (["short-secret"], "registry_secret_too_short"),
    (["postgres"], "registry_secret_too_short"),
    ([], "registry_invalid"),
    ([SECRET, SECRET], "registry_invalid"),
    ([123456789012345678], "registry_invalid"),
    (["y" * (fz.MAX_SECRET + 1)], "registry_invalid"),
])
def test_registry_refusals(root, secrets, code):
    digest = write_registry(root, secrets)
    with pytest.raises(fz.FinalizerError) as error:
        fz.load_registry(str(root), digest)
    assert str(error.value) == code


def test_registry_digest_must_match(root):
    write_registry(root, [SECRET])
    with pytest.raises(fz.FinalizerError) as error:
        fz.load_registry(str(root), "0" * 64)
    assert str(error.value) == "registry_digest_mismatch"


@pytest.mark.parametrize("raw", [
    b'{"schema":"alrust-credential-registry/1","secrets":["' + b"a" * 20
    + b'"],"secrets":["' + b"b" * 20 + b'"]}',
    b'{"schema":"other/1","secrets":["' + b"a" * 20 + b'"]}',
    b'{"schema":"alrust-credential-registry/1","secrets":["' + b"a" * 20
    + b'"],"extra":1}',
    b"not json",
])
def test_registry_malformed(root, raw):
    digest = write_registry(root, None, raw=raw)
    with pytest.raises(fz.FinalizerError) as error:
        fz.load_registry(str(root), digest)
    assert str(error.value) == "registry_invalid"


@pytest.mark.parametrize("kind", ["symlink", "fifo", "missing"])
def test_registry_must_be_a_regular_file(root, tmp_path, kind):
    path = root / fz.REGISTRY_NAME
    if kind == "symlink":
        target = tmp_path / "elsewhere.json"
        target.write_text("{}")
        path.symlink_to(target)
    elif kind == "fifo":
        os.mkfifo(path, 0o600)
    with pytest.raises(fz.FinalizerError) as error:
        fz.load_registry(str(root), "0" * 64)
    assert str(error.value) == "registry_unreadable"


def test_scan_finds_raw_and_json_escaped_secrets():
    tricky = 'quote"and\\backslash-0123456789'
    secrets = [SECRET.encode(), tricky.encode()]
    assert fz.scan_bytes(b"clean text only", secrets) == []
    assert ("secret", 0) in fz.scan_bytes(b"x" + SECRET.encode() + b"y", secrets)
    escaped = json.dumps({"v": tricky}).encode()
    assert tricky.encode() not in escaped
    assert ("secret", 1) in fz.scan_bytes(escaped, secrets)


@pytest.mark.parametrize("data,index", [
    (b"key SYNTHETIC-" + b"0f" * 24, 0),
    (b"url postgresql://user:p4ss@host/db", 1),
    (b"postgres://u:pw@localhost:5432/x", 1),
    (b"authorization: Key abc", 2),
    (b"Bearer eyJabc", 3),
    (b"-----BEGIN PRIVATE KEY-----", 4),
])
def test_generic_patterns(data, index):
    assert ("pattern", index) in fz.scan_bytes(data, [])


def test_generic_pattern_precision():
    # An image name or a plain DSN without a password is not a finding.
    assert fz.scan_bytes(b"image postgres:16, dsn postgresql://host/db", []) == []
    # "Bearer" followed by any word IS a finding, even in prose. That is a
    # deliberate fail-closed false positive: the export is refused, never
    # passed.
    assert fz.scan_bytes(b"the Bearer of news", []) == [("pattern", 3)]


def test_scan_reports_no_matched_bytes():
    findings = fz.scan_bytes(SECRET.encode(), [SECRET.encode()])
    assert all(len(f) == 2 and type(f[1]) is int for f in findings)
    assert SECRET not in repr(findings)


def test_scan_file_unreadable_is_a_finding(root, tmp_path):
    fifo = root / "pipe"
    os.mkfifo(fifo, 0o600)
    assert fz.scan_file(str(fifo), []) == [("unreadable", 0)]
    assert fz.scan_file(str(root / "missing"), []) == [("unreadable", 0)]
    big = root / "big"
    big.write_bytes(b"x" * (fz.MAX_SCANNED_FILE + 1))
    assert fz.scan_file(str(big), []) == [("unreadable", 0)]
    clean = root / "clean"
    clean.write_bytes(b"nothing here")
    assert fz.scan_file(str(clean), [SECRET.encode()]) == []


def ident(root):
    info = os.lstat(root)
    return (info.st_dev, info.st_ino)


def record(path, kind):
    info = os.lstat(path)
    return {"path": str(path), "dev": info.st_dev, "ino": info.st_ino, "type": kind}


def test_remove_file_and_tree(root):
    f = root / "settings.py"
    f.write_text("x")
    tree = root / "venv"
    (tree / "lib" / "deep").mkdir(parents=True)
    (tree / "lib" / "deep" / "m.py").write_text("y")
    assert fz.remove_owned(str(root), record(f, "file"), ident(root))["outcome"] == "REMOVED"
    out = fz.remove_owned(str(root), record(tree, "dir"), ident(root))
    assert (out["outcome"], out["reason"]) == ("REMOVED", "removed_and_verified_absent")
    assert not f.exists() and not tree.exists()


def test_symlink_inside_tree_is_not_followed(root, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("must survive")
    tree = root / "venv"
    tree.mkdir()
    (tree / "link").symlink_to(outside)
    (tree / "file-link").symlink_to(outside / "keep.txt")
    out = fz.remove_owned(str(root), record(tree, "dir"), ident(root))
    assert out["outcome"] == "REMOVED"
    assert (outside / "keep.txt").read_text() == "must survive"


def test_identity_mismatch_leaves_the_resource(root):
    f = root / "settings.py"
    f.write_text("x")
    rec = record(f, "file")
    f.unlink()
    f.write_text("replacement")  # a new inode at the same path
    rec_now = record(f, "file")
    if rec_now["ino"] == rec["ino"]:
        pytest.skip("filesystem reused the inode")
    out = fz.remove_owned(str(root), rec, ident(root))
    assert (out["outcome"], out["reason"]) == ("UNPROVEN", "identity_mismatch")
    assert f.read_text() == "replacement"


@pytest.mark.parametrize("variant,reason", [
    ("outside", "outside_job_root"),
    ("dotdot", "outside_job_root"),
    ("symlinked_parent", "ancestor_unavailable"),
    ("type", "type_mismatch"),
    ("record", "resource_record_invalid"),
    ("bool_ino", "resource_record_invalid"),
])
def test_removal_refusals(root, tmp_path, variant, reason):
    victim = tmp_path / "victim"
    victim.write_text("keep")
    f = root / "a.txt"
    f.write_text("x")
    rec = record(f, "file")
    if variant == "outside":
        rec = record(victim, "file")
    elif variant == "dotdot":
        rec["path"] = str(root) + "/../victim"
    elif variant == "symlinked_parent":
        (root / "sub").symlink_to(tmp_path)
        rec["path"] = str(root / "sub" / "victim")
    elif variant == "type":
        rec["type"] = "dir"
    elif variant == "record":
        rec["extra"] = 1
    else:
        rec["ino"] = True
    out = fz.remove_owned(str(root), rec, ident(root))
    assert (out["outcome"], out["reason"]) == ("UNPROVEN", reason)
    assert victim.read_text() == "keep"


def test_absent_is_not_a_proven_removal(root):
    f = root / "gone.txt"
    f.write_text("x")
    rec = record(f, "file")
    f.unlink()
    out = fz.remove_owned(str(root), rec, ident(root))
    assert (out["outcome"], out["reason"]) == ("ABSENT", "already_absent")



# F1465-R1: a secret with non-ASCII plus quote and backslash, in all three
# supported forms.
def test_mixed_secret_found_in_all_supported_forms():
    secret = '\u00e9"and\\backslash-synthetic-012345'
    for data in (secret.encode(), json.dumps({"v": secret}).encode(),
                 json.dumps({"v": secret}, ensure_ascii=False).encode()):
        assert ("secret", 0) in fz.scan_bytes(data, [secret.encode()]), data


# F1465-R2: an escaped unpaired surrogate is a fixed registry_invalid.
def test_registry_surrogate_is_fixed_error(root):
    bad = json.dumps({"schema": fz.SCHEMA_REGISTRY,
                      "secrets": ["x" * 20 + "\ud800"]}).encode()
    digest = write_registry(root, None, raw=bad)
    with pytest.raises(fz.FinalizerError) as error:
        fz.load_registry(str(root), digest)
    assert str(error.value) == "registry_invalid"


# F1465-R3: an ancestor replaced by a symlink AT the mutation boundary.
def test_ancestor_swap_at_unlink_cannot_escape(root, tmp_path, monkeypatch):
    parent = root / "owned"
    parent.mkdir()
    item = parent / "item"
    item.write_bytes(b"owned-synthetic")
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "item"
    victim.write_bytes(b"foreign-synthetic")
    rec = record(item, "file")
    real_unlink = os.unlink

    def swap_then_unlink(path, *args, **kwargs):
        parent.rename(root / "original-parent")
        parent.symlink_to(outside, target_is_directory=True)
        return real_unlink(path, *args, **kwargs)
    monkeypatch.setattr(fz.os, "unlink", swap_then_unlink)
    out = fz.remove_owned(str(root), rec, ident(root))
    monkeypatch.setattr(fz.os, "unlink", real_unlink)
    assert victim.read_bytes() == b"foreign-synthetic"
    assert not (root / "original-parent" / "item").exists()
    assert (out["outcome"], out["reason"]) == ("REMOVED", "removed_and_verified_absent")


def test_ancestor_swap_at_rmtree_cannot_escape(root, tmp_path, monkeypatch):
    parent = root / "owned"
    (parent / "venv" / "lib").mkdir(parents=True)
    (parent / "venv" / "lib" / "m.py").write_text("owned")
    outside = tmp_path / "outside"
    (outside / "venv").mkdir(parents=True)
    (outside / "venv" / "keep.txt").write_text("foreign")
    rec = record(parent / "venv", "dir")
    real_rmtree = fz.shutil.rmtree

    def swap_then_rmtree(path, *args, **kwargs):
        parent.rename(root / "original-parent")
        parent.symlink_to(outside, target_is_directory=True)
        return real_rmtree(path, *args, **kwargs)
    monkeypatch.setattr(fz.shutil, "rmtree", swap_then_rmtree)
    out = fz.remove_owned(str(root), rec, ident(root))
    monkeypatch.setattr(fz.shutil, "rmtree", real_rmtree)
    assert (outside / "venv" / "keep.txt").read_text() == "foreign"
    assert not (root / "original-parent" / "venv").exists()
    assert out["outcome"] == "REMOVED"


def test_symlinked_ancestor_before_open_is_refused(root, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "item").write_text("foreign")
    (root / "owned").symlink_to(outside, target_is_directory=True)
    info = os.lstat(outside / "item")
    rec = {"path": str(root / "owned" / "item"), "dev": info.st_dev,
           "ino": info.st_ino, "type": "file"}
    out = fz.remove_owned(str(root), rec, ident(root))
    assert (out["outcome"], out["reason"]) == ("UNPROVEN", "ancestor_unavailable")
    assert (outside / "item").read_text() == "foreign"


@pytest.mark.parametrize("variant,reason", [
    ("other_root", "root_identity_mismatch"),
    ("public_root", "root_identity_mismatch"),
    ("bad_identity", "root_identity_invalid"),
])
def test_root_identity_is_required(root, tmp_path, variant, reason):
    f = root / "a.txt"
    f.write_text("x")
    identity = ident(root)
    if variant == "other_root":
        other = tmp_path / "other"
        other.mkdir()
        identity = ident(other)
    elif variant == "public_root":
        root.chmod(0o755)
    else:
        identity = (True, 1)
    out = fz.remove_owned(str(root), record(f, "file"), identity)
    assert (out["outcome"], out["reason"]) == ("UNPROVEN", reason)
    assert f.exists()
