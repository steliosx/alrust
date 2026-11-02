#!/usr/bin/env python3
"""Regression cases for cu_tmp_reaper.py (FH-ALRUST-CONF-UNIT-1).

Run with the conformance venv's Python (the plugin imports pytest):
    "$RUNNER_TEMP/conformance-venv/bin/python" .github/scripts/test-cu-tmp-reaper.py
Everything happens inside one fresh temporary directory, which is removed at
the end. Exit status 0 means every case passed.
"""

import os
import stat
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cu_tmp_reaper as reaper  # noqa: E402


def mode(path):
    return stat.S_IMODE(os.lstat(path).st_mode)


def check(name, condition):
    print(("ok   " if condition else "FAIL ") + name)
    return condition


def main():
    ok = True
    with tempfile.TemporaryDirectory() as top:
        top = Path(top)
        base = top / "base"
        outside = top / "outside"
        base.mkdir()
        outside.mkdir()
        (outside / "keep").write_text("x")
        outside.chmod(0o500)

        # 1. Read-only nested directories and files are removed.
        t = base / "t1"
        (t / "a" / "b").mkdir(parents=True)
        (t / "a" / "b" / "f").write_text("x")
        (t / "a" / "b" / "f").chmod(0o400)
        (t / "a" / "b").chmod(0o500)
        (t / "a").chmod(0o500)
        ok &= check("read-only tree removed", reaper.remove_tmp_path(t, base) and not t.exists())

        # 2. A tmp_path that is already gone is a no-op.
        ok &= check("missing tmp_path is a no-op", reaper.remove_tmp_path(base / "gone", base) is False)

        # 3. A read-only tmp_path root with content is removed.
        t = base / "t3"
        t.mkdir()
        (t / "f").write_text("x")
        t.chmod(0o500)
        ok &= check("read-only root removed", reaper.remove_tmp_path(t, base) and not t.exists())

        # 4. A symlink inside tmp_path to a directory outside basetemp: the
        #    link goes, the target and its mode stay.
        t = base / "t4"
        t.mkdir()
        (t / "link").symlink_to(outside)
        (t / "sub").mkdir()
        (t / "sub" / "link2").symlink_to(outside)
        t.chmod(0o500)
        removed = reaper.remove_tmp_path(t, base)
        ok &= check("tree with outside symlinks removed", removed and not t.exists())
        ok &= check("symlink target kept", (outside / "keep").exists())
        ok &= check("symlink target mode unchanged (0500)", mode(outside) == 0o500)

        # 5. A tmp_path outside basetemp is not touched.
        t = top / "elsewhere"
        t.mkdir()
        ok &= check("outside basetemp untouched", reaper.remove_tmp_path(t, base) is False and t.exists())

        # 6. A tmp_path that is itself a symlink is not followed or removed.
        t = base / "t6"
        t.symlink_to(outside)
        ok &= check("symlinked tmp_path untouched", reaper.remove_tmp_path(t, base) is False
                    and t.is_symlink() and (outside / "keep").exists() and mode(outside) == 0o500)
        t.unlink()

        # 6b. A tmp_path reached through a symlinked ancestor is not touched,
        #     nor is anything at the link's target.
        (outside_victim := top / "outside2" / "victim").mkdir(parents=True)
        (outside_victim / "data").write_text("keep")
        outside_victim.chmod(0o500)
        (base / "anc").symlink_to(top / "outside2")
        t = base / "anc" / "victim"
        ok &= check("tmp_path under a symlinked ancestor untouched",
                    reaper.remove_tmp_path(t, base) is False
                    and (outside_victim / "data").read_text() == "keep"
                    and mode(outside_victim) == 0o500)
        (base / "anc").unlink()
        outside_victim.chmod(0o700)

        # 6c. basetemp itself is never removed.
        ok &= check("basetemp itself untouched", reaper.remove_tmp_path(base, base) is False and base.exists())

        # 6d. Nested directories with mode 0000 (and a 0000 root) are removed.
        t = base / "t6d"
        (t / "a" / "b").mkdir(parents=True)
        (t / "a" / "b" / "f").write_text("x")
        (t / "a" / "b").chmod(0o000)
        (t / "a").chmod(0o000)
        t.chmod(0o000)
        ok &= check("nested 0000 directories removed", reaper.remove_tmp_path(t, base) and not t.exists())

        # 7. The hook removes a finished test's tmp_path and ignores tests
        #    without one or runs without --basetemp.
        t = base / "t7"
        t.mkdir()
        cfg = SimpleNamespace(option=SimpleNamespace(basetemp=str(base)))
        reaper.pytest_runtest_teardown(SimpleNamespace(funcargs={"tmp_path": t}, config=cfg))
        ok &= check("hook removes tmp_path", not t.exists())
        t.mkdir()
        reaper.pytest_runtest_teardown(SimpleNamespace(funcargs={}, config=cfg))
        no_base = SimpleNamespace(option=SimpleNamespace(basetemp=None))
        reaper.pytest_runtest_teardown(SimpleNamespace(funcargs={"tmp_path": t}, config=no_base))
        ok &= check("hook leaves other cases alone", t.exists())

        # 8. A removal failure that is not a missing path is raised.
        t = base / "t8"
        t.mkdir()
        (t / "f").write_text("x")
        real_unlink = os.unlink

        def failing_unlink(path, *args, **kwargs):
            raise PermissionError(13, "simulated", str(path))

        os.unlink = failing_unlink
        try:
            reaper.remove_tmp_path(t, base)
            raised = False
        except OSError:
            raised = True
        finally:
            os.unlink = real_unlink
        ok &= check("unexpected failure propagates", raised)

        outside.chmod(0o700)
        for p in base.rglob("*"):
            if p.is_dir() and not p.is_symlink():
                p.chmod(0o700)
    print("PASS" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
