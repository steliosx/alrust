"""pytest plugin for the Forgehold lane's tmpfs mode (FH-ALRUST-CONF-UNIT-1).

conformance-unit.sh loads it with `-p cu_tmp_reaper` only when CU_TMP=tmpfs.
After each test's teardown it removes that test's own tmp_path directory, so
the shard's peak use of /dev/shm stays near the largest single test's files
rather than the whole shard's (rest-fz keeps about 74 MB; the lane guest's
/dev/shm ran out). Directories made through tmp_path_factory or shared by
fixtures are left alone, so peak use is not strictly one test's files.

It never changes a test, a fixture value or an outcome; it only deletes a
directory pytest created for a test that has finished, and only inside the
--basetemp the script passes (it does nothing without one). Symlinks are
removed as links and never followed: permissions are repaired only on real
directories inside that tmp_path. A tmp_path that is already gone is a no-op;
any other removal failure propagates as a teardown error.
"""

import os
import shutil
import stat
import sys
from pathlib import Path

import pytest


def _inside(path, root):
    try:
        Path(os.path.abspath(path)).relative_to(root)
    except ValueError:
        return False
    return True


def _repair(path, root):
    # Owner rwx on a real directory inside root; never through a symlink.
    if _inside(path, root) and os.path.isdir(path) and not os.path.islink(path):
        os.chmod(path, stat.S_IRWXU)
        return True
    return False


def remove_tmp_path(tmp, base):
    """Remove tmp (a test's tmp_path) if it lies inside base. Returns whether
    anything was removed."""
    root = Path(os.path.abspath(tmp))
    if not _inside(root, Path(os.path.abspath(base))) or os.path.islink(root):
        return False
    if not os.path.lexists(root):
        return False
    _repair(root, root)

    def on_error(func, path, exc):
        if isinstance(exc, FileNotFoundError):
            return
        target = os.path.dirname(path) if func in (os.unlink, os.rmdir) else path
        if func in (os.open, os.scandir, os.listdir):
            target = path
        if not _repair(target, root):
            raise exc
        func(path)

    if sys.version_info >= (3, 12):
        shutil.rmtree(root, onexc=on_error)
    else:  # pragma: no cover - the lane and CI pin Python 3.12
        shutil.rmtree(root, onerror=lambda f, p, ei: on_error(f, p, ei[1]))
    return True


@pytest.hookimpl(trylast=True)
def pytest_runtest_teardown(item):
    tmp = getattr(item, "funcargs", {}).get("tmp_path")
    base = item.config.option.basetemp
    if tmp is None or not base:
        return
    remove_tmp_path(tmp, base)
