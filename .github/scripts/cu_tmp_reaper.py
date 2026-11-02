"""pytest plugin for the Forgehold lane's tmpfs mode (FH-ALRUST-CONF-UNIT-1).

conformance-unit.sh loads it with `-p cu_tmp_reaper` only when CU_TMP=tmpfs.
After each test's teardown it removes that test's own tmp_path directory, so
the shard's peak use of /dev/shm stays near the largest single test's files
rather than the whole shard's (rest-fz keeps about 74 MB; the lane guest's
/dev/shm ran out). Directories made through tmp_path_factory or shared by
fixtures are left alone, so peak use is not strictly one test's files.

It never changes a test, a fixture value or an outcome; it only deletes a
directory pytest created for a test that has finished, and only inside the
--basetemp the script passes (it does nothing without one). The tmp_path must
be a real directory strictly below the real basetemp with no symlink anywhere
in its own path; otherwise it is left alone. Before removal every real
directory inside it is made owner-writable, top-down and never through a
symlink (symlinks are removed as links, their targets untouched). A tmp_path
that is already gone is a no-op; any other failure propagates as a teardown
error.
"""

import os
import shutil
import stat
import sys
from pathlib import Path

import pytest


def _owned(tmp, base):
    """The test's tmp_path as a real path, if it is a strict descendant of the
    real basetemp and no component of it is a symlink; else None."""
    lexical = os.path.abspath(tmp)
    real = os.path.realpath(tmp)
    root = os.path.realpath(base)
    if lexical != real or real == root:
        return None
    try:
        Path(real).relative_to(root)
    except ValueError:
        return None
    return real


def _open_up(path):
    """Give every real directory in the tree owner rwx, top-down, never
    through a symlink, so that the removal below cannot be refused."""
    os.chmod(path, stat.S_IRWXU)
    with os.scandir(path) as entries:
        subdirs = [e.path for e in entries if e.is_dir(follow_symlinks=False)]
    for sub in subdirs:
        _open_up(sub)


def remove_tmp_path(tmp, base):
    """Remove tmp (a test's tmp_path) if it is a real directory owned inside
    base. Returns whether anything was removed."""
    root = _owned(tmp, base)
    if root is None or not os.path.isdir(root):
        return False

    def missing_ok(func, path, exc):
        if not isinstance(exc, FileNotFoundError):
            raise exc

    _open_up(root)
    if sys.version_info >= (3, 12):
        shutil.rmtree(root, onexc=missing_ok)
    else:  # pragma: no cover - the lane and CI pin Python 3.12
        shutil.rmtree(root, onerror=lambda f, p, ei: missing_ok(f, p, ei[1]))
    return True


@pytest.hookimpl(trylast=True)
def pytest_runtest_teardown(item):
    tmp = getattr(item, "funcargs", {}).get("tmp_path")
    base = item.config.option.basetemp
    if tmp is None or not base:
        return
    remove_tmp_path(tmp, base)
