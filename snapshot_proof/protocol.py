"""Cooperating writers hold flock and persist DIRTY before any database/file mutation."""

from contextlib import contextmanager
import fcntl
import os
import time
import uuid

from .safety import (ConsistencyError, ProofError, json_bytes, parse_json, private_directory,
                     regular, sync_directory, write_new)

PROTOCOL = "snapshot-proof.flock-dirty.v1"


def _state(directory, status):
    pending = directory / ("state-" + uuid.uuid4().hex)
    write_new(pending, json_bytes({"version": 1, "status": status}))
    os.replace(pending, directory / "state.json")
    sync_directory(directory)


def initialize(root, *, writers_integrated=False):
    """Only while all writers are stopped and the existing application is consistent."""
    if writers_integrated is not True:
        raise ConsistencyError("All writers must be stopped, consistent and integrated with the protocol")
    root = private_directory(root)
    directory = root / ".snapshot-proof"
    directory.mkdir(mode=0o700, exist_ok=False)
    write_new(directory / "lock", b"")
    _state(directory, "CLEAN")
    sync_directory(root)
    return {"ok": True, "operation": "init", "protocol": PROTOCOL, "initialized": True}


@contextmanager
def boundary(root, *, timeout=5):
    if type(timeout) not in (int, float) or not 0.01 <= timeout <= 30:
        raise ProofError("Lock timeout must be between 0.01 and 30 seconds")
    root = private_directory(root)
    try:
        directory = private_directory(root / ".snapshot-proof")
        lock = regular(directory / "lock")
        fd = os.open(lock, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
    except (OSError, ProofError) as exc:
        raise ConsistencyError("Application is not enrolled in the lock protocol") from exc
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ConsistencyError("Application busy; bounded lock wait ended")
                time.sleep(0.01)
        if os.fstat(fd).st_ino != lock.stat().st_ino:
            raise ConsistencyError("Application lock identity changed")
        try:
            path = regular(directory / "state.json")
            if path.stat().st_size > 1024:
                raise ProofError("Oversized protocol state")
            state = parse_json(path.read_bytes())
        except (OSError, ProofError) as exc:
            raise ConsistencyError("Application protocol state is incomplete or malformed") from exc
        if state != {"version": 1, "status": "CLEAN"} or type(state.get("version")) is not int:
            raise ConsistencyError("Application dirty or incomplete; reconcile it before snapshotting")
        yield directory
    finally:
        os.close(fd)  # Kernel releases the lock, including after process termination.


@contextmanager
def application_write(root, *, timeout=5):
    """Caller must commit SQLite, fsync artifacts/directories and close connections before exit.

    Any exception or process death leaves DIRTY, even if a database transaction rolled back.
    No automatic 'repair' or clearing of uncertain application state is attempted.
    """
    with boundary(root, timeout=timeout) as directory:
        _state(directory, "DIRTY")
        yield
        _state(directory, "CLEAN")
