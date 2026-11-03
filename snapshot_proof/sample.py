"""Tiny cooperating application used only for synthetic CLI examples and restore acceptance."""

from contextlib import closing
import hashlib
from pathlib import Path
import re
import sqlite3
import tempfile
import uuid

from .core import backup, drill, restore, verify
from .protocol import application_write, initialize
from .safety import ProofError, digest, json_bytes, private_directory, sync_directory, write_new


def add_record(root, key, text):
    root = private_directory(root)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", key):
        raise ProofError("Sample record ID requires 1–64 ASCII letters, digits, _ or -")
    raw = text.encode("utf-8")
    name = f"artifacts/{key}.txt"
    with application_write(root):
        write_new(root / name, raw)
        sync_directory(root / "artifacts")
        with closing(sqlite3.connect((root / "application.sqlite3").as_uri() + "?mode=rw", uri=True)) as conn:
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("INSERT INTO documents VALUES(?,?,?)", (key, name, hashlib.sha256(raw).hexdigest()))
            conn.commit()
    return dict(ok=True, record_id=key, artifact=name, sha256=hashlib.sha256(raw).hexdigest())


def create_sample(root, config_file):
    root = Path(root).absolute()
    root.mkdir(mode=0o700, exist_ok=False)
    (root / "artifacts").mkdir(mode=0o700)
    write_new(root / "application.sqlite3", b"")
    with closing(sqlite3.connect(root / "application.sqlite3")) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE documents(id TEXT PRIMARY KEY,artifact TEXT NOT NULL,sha256 TEXT NOT NULL)")
        conn.commit()
    initialize(root, writers_integrated=True)
    record = add_record(root, "historical-001", "Historical work must survive restoration.\n")
    # Synthetic private configuration proves the snapshot does not walk the application tree.
    write_new(root / ".env", b"DEMO_ONLY_NOT_A_SECRET=excluded\n")
    config = dict(version=1, database="application.sqlite3", artifacts=[record["artifact"]],
                  witness=dict(table="documents", key_column="id", key_value=record["record_id"],
                               artifact_column="artifact", hash_column="sha256",
                               artifact=record["artifact"], sha256=record["sha256"]))
    write_new(Path(config_file), json_bytes(config))
    sync_directory(root)
    return config


def run_demo(directory=None):
    if directory is None:
        directory = tempfile.mkdtemp(prefix="snapshot-proof-demo-")
    directory = Path(directory).absolute() / ("case-" + uuid.uuid4().hex[:12])
    directory.mkdir(parents=True, mode=0o700)
    live = directory / "application"
    restored = directory / "restored"
    archive = directory / "snapshot.zip"
    config = create_sample(live, directory / "snapshot-config.json")
    result = backup(live, config, archive)
    verification = verify(archive)
    restore_result = restore(archive, restored)
    add_record(restored, "new-002", "New application work after restoration.\n")
    # Independently inspect actual application rows and files, not a tool-owned probe table.
    with closing(sqlite3.connect((restored / "application.sqlite3").as_uri() + "?mode=ro", uri=True)) as conn:
        rows = conn.execute("SELECT id,artifact,sha256 FROM documents ORDER BY id").fetchall()
        integrity = conn.execute("PRAGMA integrity_check").fetchall()
    if ([row[0] for row in rows] != ["historical-001", "new-002"] or integrity != [("ok",)]
            or any(digest(restored / row[1]) != row[2] for row in rows)
            or (restored / ".env").exists()
            or (restored / config["witness"]["artifact"]).read_bytes()
                != (live / config["witness"]["artifact"]).read_bytes()):
        raise ProofError("Independent application restore acceptance failed")
    return dict(ok=True, directory=str(directory), archive=str(archive),
                archive_sha256=result["archive_sha256"], historical_record_verified=True,
                application_rows_after_new_work=len(rows), sqlite_integrity="ok",
                unselected_configuration_excluded=True, backup_restore_drill=result["restore_drill"],
                independent_verify=verification["ok"], restored=restore_result["ok"],
                repeat_isolated_drill=drill(archive)["historical_data_preserved"])
