"""pytest plugin for the Forgehold lane's tmpfs mode (FH-ALRUST-CONF-UNIT-1).

conformance-unit.sh loads it with `-p cu_tmp_reaper` only when CU_TMP=tmpfs.
After each test's teardown it removes that test's own tmp_path directory, so
the shard's peak use of /dev/shm is about one test's files rather than the
whole shard's (rest-fz: about 74 MB kept, while the lane guest's /dev/shm ran
out). It never changes a test, a fixture value or an outcome; it only deletes
a directory pytest created for a test that has finished, and only inside
the --basetemp the script passes (it does nothing without one). Directories
from tmp_path_factory are left alone.
"""

import os
import shutil
import stat
from pathlib import Path

import pytest


def _writable(func, path, _exc):
    os.chmod(path, stat.S_IRWXU)
    func(path)


@pytest.hookimpl(trylast=True)
def pytest_runtest_teardown(item):
    tmp = getattr(item, "funcargs", {}).get("tmp_path")
    if tmp is None:
        return
    base = item.config.option.basetemp
    if not base:
        return
    base = Path(base)
    try:
        tmp.resolve().relative_to(base.resolve())
    except ValueError:
        return
    for root, dirs, _files in os.walk(tmp):
        for d in dirs:
            try:
                os.chmod(os.path.join(root, d), stat.S_IRWXU)
            except OSError:
                pass
    shutil.rmtree(tmp, onerror=_writable)
