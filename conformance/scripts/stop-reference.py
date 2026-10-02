#!/usr/bin/env python3
"""Stop a supervised reference server and report a cleanup verdict.

Contract: docs/ci-launch-attestation-contract.md, section 5.
Usage: `python3 -I -S -B stop-reference.py RECEIPT SHA256`.

The helper signals one process only: the supervisor P named by the launch
receipt. It does so only after opening a pidfd for P and confirming,
through that handle, P's generation (start_ticks, uid and boot_id). It
uses pidfd_send_signal with flags 0, and it never signals a group, a
numeric PID or any other process.

Output: one JSON line on stdout. Verdicts:

| Verdict | Exit | Condition |
|---|---|---|
| COMPLETE | 0 | a tree_empty terminal receipt of P's generation, and P has exited |
| FAILED | 1 | a tree_not_empty terminal receipt |
| UNPROVEN | 2 | anything else, including bad input and unsupported syscalls |
"""
import hashlib
import json
import os
import select
import signal
import stat
import sys
import time

SCHEMA_LAUNCH = "alrust-reference-launch/1"
SCHEMA_TERMINAL = "alrust-reference-terminal/1"
EXIT_WAIT_SECONDS = 20.0
MAX_RECEIPT = 65536
EXIT = {"COMPLETE": 0, "FAILED": 1, "UNPROVEN": 2}


def verdict(name, reason, **extra):
    record = {"verdict": name, "reason": reason}
    record.update(extra)
    sys.stdout.write(json.dumps(record, sort_keys=True) + "\n")
    sys.stdout.flush()
    return EXIT[name]


def read_bounded(path):
    """Read a small regular file without following links or blocking.

    O_NONBLOCK makes opening a FIFO or device return at once; the fstat of
    the opened descriptor then refuses anything but a regular file, before
    any read can block.
    """
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
                 | os.O_NONBLOCK | os.O_NOCTTY)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("not a regular file")
        if info.st_size > MAX_RECEIPT:
            raise ValueError("receipt too large")
        chunks, size = [], 0
        while size <= MAX_RECEIPT:
            chunk = os.read(fd, MAX_RECEIPT + 1 - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        data = b"".join(chunks)
    finally:
        os.close(fd)
    if len(data) > MAX_RECEIPT:
        raise ValueError("receipt too large")
    return data


def read_generation(pid):
    """(start_ticks, uid) of PID from /proc, or None if PID is absent."""
    try:
        with open("/proc/%d/stat" % pid, "rb") as f:
            data = f.read(4096)
        uid = os.stat("/proc/%d" % pid).st_uid
    except (FileNotFoundError, ProcessLookupError):
        return None
    rest = data[data.rindex(b")") + 2:].split()
    return int(rest[19]), uid


def boot_id():
    with open("/proc/sys/kernel/random/boot_id") as f:
        return f.read().strip()


def read_terminal(path, pid, start_ticks, boot):
    """The terminal result, None if absent, or 'invalid'."""
    try:
        record = json.loads(read_bounded(path))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return "invalid"
    if not isinstance(record, dict) or record.get("schema") != SCHEMA_TERMINAL:
        return "invalid"
    owner = record.get("supervisor")
    if not isinstance(owner, dict) or type(owner.get("pid")) is not int \
            or type(owner.get("start_ticks")) is not int:
        return "invalid"
    if owner["pid"] != pid or owner["start_ticks"] != start_ticks \
            or record.get("boot_id") != boot:
        return "invalid"
    if record.get("result") not in ("tree_empty", "tree_not_empty"):
        return "invalid"
    return record["result"]


def wait_exit(fd, seconds):
    """True once the process behind pidfd FD has exited."""
    deadline = time.monotonic() + seconds
    poller = select.poll()
    poller.register(fd, select.POLLIN)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        if poller.poll(int(remaining * 1000) + 1):
            return True


def conclude(terminal, exited):
    if terminal == "tree_empty" and exited:
        return verdict("COMPLETE", "tree_empty")
    if terminal == "tree_not_empty":
        return verdict("FAILED", "tree_not_empty")
    if terminal == "tree_empty":
        return verdict("UNPROVEN", "supervisor_did_not_exit")
    if terminal == "invalid":
        return verdict("UNPROVEN", "terminal_receipt_invalid")
    return verdict("UNPROVEN", "no_terminal_receipt")


def main(argv):
    if len(argv) != 2 or not os.path.isabs(argv[0]) \
            or len(argv[1]) != 64 or argv[1].strip("0123456789abcdef"):
        return verdict("UNPROVEN", "usage")
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        return verdict("UNPROVEN", "pidfd_unsupported")
    try:
        data = read_bounded(argv[0])
    except (OSError, ValueError):
        return verdict("UNPROVEN", "receipt_unreadable")
    if hashlib.sha256(data).hexdigest() != argv[1]:
        return verdict("UNPROVEN", "receipt_hash_mismatch")
    try:
        receipt = json.loads(data)
        if receipt["schema"] != SCHEMA_LAUNCH:
            raise ValueError("schema")
        sup = receipt["supervisor"]
        pid, start_ticks, uid = sup["pid"], sup["start_ticks"], sup["uid"]
        terminal_path = receipt["terminal_receipt"]
        boot = receipt["boot_id"]
        if not all(type(v) is int and v > 1 for v in (pid, start_ticks)) \
                or type(uid) is not int or not isinstance(terminal_path, str) \
                or not os.path.isabs(terminal_path) or sup["pgid"] != pid:
            raise ValueError("fields")
    except (KeyError, TypeError, ValueError):
        return verdict("UNPROVEN", "receipt_malformed")

    try:
        own = {os.getpid(), os.getppid(), os.getpgid(0), os.getsid(0)}
    except OSError:
        return verdict("UNPROVEN", "self_unreadable")
    if pid in own:
        return verdict("UNPROVEN", "refused_own_process")
    try:
        if boot_id() != boot:
            return verdict("UNPROVEN", "boot_mismatch")
    except OSError:
        return verdict("UNPROVEN", "boot_unreadable")

    try:
        fd = os.pidfd_open(pid, 0)
    except ProcessLookupError:
        # P is gone. Only an already written terminal receipt can prove
        # anything; the PID itself identifies nothing any more.
        return conclude(read_terminal(terminal_path, pid, start_ticks, boot),
                        exited=True)
    except OSError as error:
        return verdict("UNPROVEN", "pidfd_open_failed", errno=error.errno)
    try:
        # The pidfd names whatever held PID when it was opened. A matching
        # generation read afterwards proves that the receipt's process
        # still held PID then, so the pidfd refers to it.
        try:
            current = read_generation(pid)
        except (OSError, ValueError):
            return verdict("UNPROVEN", "generation_unreadable")
        if current is None or current[0] != start_ticks:
            # No process of the receipt's generation holds PID any more (on
            # this boot): P has exited, and PID may now be another process,
            # which is never signalled.
            return conclude(read_terminal(terminal_path, pid, start_ticks,
                                          boot), exited=True)
        if current[1] != uid:
            # Same generation, different owner: an ownership mismatch, not
            # an exit. Never signal, never conclude.
            return verdict("UNPROVEN", "uid_mismatch")
        terminal = read_terminal(terminal_path, pid, start_ticks, boot)
        if terminal is None:
            try:
                signal.pidfd_send_signal(fd, signal.SIGTERM, None, 0)
            except ProcessLookupError:
                pass
            except OSError as error:
                return verdict("UNPROVEN", "signal_failed", errno=error.errno)
        exited = wait_exit(fd, EXIT_WAIT_SECONDS)
        return conclude(read_terminal(terminal_path, pid, start_ticks, boot),
                        exited)
    finally:
        os.close(fd)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
