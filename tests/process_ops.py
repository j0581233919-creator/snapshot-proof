"""Real child processes and explicit crash barriers, used only by tests."""

from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3
import sys

from snapshot_proof import application_write, backup, restore
from snapshot_proof.safety import parse_json, sync_directory


if __name__ == "__main__":
    mode = sys.argv[1]
    root = Path(sys.argv[2])
    if mode in ("writer", "counter"):
        with application_write(root):
            if mode == "counter":
                raw = b"1\n"
                with (root / "artifacts/counter.txt").open("wb") as handle:
                    handle.write(raw)
                    handle.flush()
                    import os
                    os.fsync(handle.fileno())
                sync_directory(root / "artifacts")
            print("ready", flush=True)
            sys.stdin.readline()
            if mode == "counter":
                with closing(sqlite3.connect(root / "application.sqlite3")) as conn:
                    conn.execute("UPDATE counter SET n=1,sha256=?", (hashlib.sha256(raw).hexdigest(),))
                    conn.commit()
    elif mode in ("interrupt-backup", "interrupt-restore"):
        # Freeze during a genuine SQLite backup callback or immediately before restore publication.
        # No fake bytes, injected success, or production test hook is used.
        barrier = "progress" if mode == "interrupt-backup" else "publish_directory"
        def trace(frame, event, arg):
            if event == "call" and frame.f_code.co_name == barrier:
                print("ready", flush=True)
                sys.stdin.readline()
            return trace
        sys.settrace(trace)
        if mode == "interrupt-backup":
            backup(root, parse_json(Path(sys.argv[3]).read_bytes()), Path(sys.argv[4]))
        else:
            restore(root, Path(sys.argv[3]))
