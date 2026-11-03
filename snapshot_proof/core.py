"""Online backup → independent verification and restore drill → atomic publication."""

from contextlib import closing
import hashlib
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import uuid
import zipfile

from .format import configuration, database_check, manifest_spec, unpack_verified
from .protocol import PROTOCOL, boundary, initialize
from .safety import (MAX_BYTES, ConsistencyError, DestinationError, ProofError, VerificationError,
                     destination, digest, json_bytes, private_directory, publish_directory,
                     regular, sync_directory, write_new)


def _summary(manifest, operation):
    return dict(ok=True, operation=operation, snapshot_id=manifest["snapshot_id"],
                format_version=1, files=len(manifest["files"]),
                bytes=sum(item["size"] for item in manifest["files"]),
                sqlite_integrity="ok", foreign_keys="ok", historical_witness_verified=True)


def verify(archive):
    """Recompute every hash and check SQLite plus the declared historical witness."""
    with tempfile.TemporaryDirectory(prefix="snapshot-proof-verify-") as temporary:
        manifest = unpack_verified(archive, Path(temporary))
    return _summary(manifest, "verify")


def _write_drill(root, manifest):
    """Real writes in a disposable restore only; never mutate live or caller-restored data."""
    nonce = uuid.uuid4().hex
    table = "sp_probe_" + nonce
    artifact = "probe-" + nonce + ".txt"
    raw = b"New work after isolated restoration.\n"
    write_new(root / artifact, raw)
    sync_directory(root)
    sha = hashlib.sha256(raw).hexdigest()
    with closing(sqlite3.connect((root / manifest["database"]).as_uri() + "?mode=rw", uri=True)) as conn:
        conn.execute("PRAGMA trusted_schema=OFF")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute(f'CREATE TABLE "{table}"(id INTEGER PRIMARY KEY,artifact TEXT NOT NULL,sha256 TEXT NOT NULL)')
        conn.execute(f'INSERT INTO "{table}" VALUES(1,?,?)', (artifact, sha))
        conn.commit()
    with closing(sqlite3.connect((root / manifest["database"]).as_uri() + "?mode=ro", uri=True)) as conn:
        row = conn.execute(f'SELECT artifact,sha256 FROM "{table}" WHERE id=1').fetchone()
    if row != (artifact, sha) or digest(root / artifact) != sha:
        raise VerificationError("New work did not persist in isolated restore")
    database_check(root, manifest)  # Historical row/artifact and integrity must still hold.
    return dict(new_database_write_verified=True, new_artifact_verified=True,
                historical_data_preserved=True, application_boot_tested=False)


def drill(archive):
    with tempfile.TemporaryDirectory(prefix="snapshot-proof-drill-") as temporary:
        manifest = unpack_verified(archive, Path(temporary))
        result = _write_drill(Path(temporary), manifest)
    return _summary(manifest, "drill") | result


def restore(archive, target):
    target = destination(target)  # Nonexistent, including no existing empty directory.
    with tempfile.TemporaryDirectory(prefix=".snapshot-proof-stage-", dir=target.parent) as temporary:
        staged = Path(temporary) / "application"
        staged.mkdir(mode=0o700)
        manifest = unpack_verified(archive, staged)
        initialize(staged, writers_integrated=True)  # Fresh local lock, not archived authority/config.
        publish_directory(staged, target)
    return _summary(manifest, "restore") | {"destination": str(target)}


def _online_database(source, target):
    write_new(target, b"")
    with closing(sqlite3.connect(source.as_uri() + "?mode=rw", uri=True, timeout=2,
                                isolation_level=None)) as guard:
        guard.execute("PRAGMA trusted_schema=OFF")
        try:
            guard.execute("BEGIN IMMEDIATE")  # Also blocks SQLite writers outside the application flock.
        except sqlite3.OperationalError as exc:
            raise ConsistencyError("SQLite is busy; snapshot boundary could not be established") from exc
        try:
            with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=2)) as reader:
                page_size = reader.execute("PRAGMA page_size").fetchone()[0]
                deadline = time.monotonic() + 10

                def progress(status, remaining, total):
                    if time.monotonic() > deadline:
                        raise ConsistencyError("Online backup deadline exceeded")
                    if total * page_size > MAX_BYTES:
                        raise ProofError("Database exceeds 64 MiB")
                with closing(sqlite3.connect(target)) as writer:
                    reader.backup(writer, pages=128, progress=progress, sleep=0.01)
                    writer.execute("PRAGMA journal_mode=DELETE")
                    writer.commit()
        finally:
            guard.rollback()
    with target.open("rb") as handle:
        os.fsync(handle.fileno())


def backup(root, config, target, *, timeout=5):
    config = configuration(config)
    root = private_directory(root)
    target = destination(target)
    if target.is_relative_to(root):
        raise DestinationError("Snapshots must be outside the application root")
    with tempfile.TemporaryDirectory(prefix=".snapshot-proof-stage-", dir=target.parent) as temporary:
        stage = Path(temporary)
        payload = stage / "payload"
        payload.mkdir(mode=0o700)
        with boundary(root, timeout=timeout):
            database = regular(root / config["database"])
            for suffix in ("-wal", "-shm", "-journal"):
                sidecar = Path(str(database) + suffix)
                if os.path.lexists(sidecar):
                    regular(sidecar)  # SQLite must not follow sidecar links outside the allowed root.
            observations = {}
            for name in config["artifacts"]:
                path = regular(root / name)
                if path.stat().st_size > MAX_BYTES:
                    raise ProofError("Artifact exceeds snapshot size limit")
                with path.open("rb") as handle:
                    if handle.read(16) == b"SQLite format 3\0":
                        raise ProofError("An additional SQLite database cannot be a raw artifact")
                observations[name] = (path.stat(), digest(path))
            artifact_bytes = sum(item[0].st_size for item in observations.values())
            if artifact_bytes > MAX_BYTES:
                raise ProofError("Selected artifacts exceed 64 MiB")
            copied_db = payload / config["database"]
            copied_db.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            _online_database(database, copied_db)
            if copied_db.stat().st_size + artifact_bytes > MAX_BYTES:
                raise ProofError("Database and selected artifacts exceed 64 MiB")
            for name in config["artifacts"]:
                path = regular(root / name)
                copied = payload / name
                copied.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
                with path.open("rb") as source:
                    fd = os.open(copied, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                    with os.fdopen(fd, "wb") as writer:
                        # Size and source were bounded above; protocol prohibits concurrent file mutation.
                        count = 0
                        while chunk := source.read(65536):
                            count += len(chunk)
                            if count > MAX_BYTES:
                                raise ConsistencyError("Artifact changed beyond snapshot size limit")
                            writer.write(chunk)
                        writer.flush()
                        os.fsync(writer.fileno())
                before, expected = observations[name]
                after = regular(path).stat()
                if ((before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                        != (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                        or digest(path) != expected or digest(copied) != expected):
                    raise ConsistencyError("Artifact changed during snapshot; consistency refused")
            inventory = []
            for name in sorted([config["database"], *config["artifacts"]]):
                path = payload / name
                inventory.append(dict(path=name, size=path.stat().st_size, sha256=digest(path),
                                      kind="database" if name == config["database"] else "artifact"))
            manifest = manifest_spec(dict(format="snapshot-proof", version=1, snapshot_id=uuid.uuid4().hex,
                                          created_at=int(time.time()), consistency=PROTOCOL,
                                          database=config["database"], files=inventory, witness=config["witness"]))
            database_check(payload, manifest)
        # Captured bytes are now immutable private staging files; writers may resume.
        archive = stage / "snapshot.zip"
        with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_STORED) as z:
            z.writestr("manifest.json", json_bytes(manifest))
            for item in inventory:
                z.write(payload / item["path"], "payload/" + item["path"])
        archive.chmod(0o600)
        with archive.open("rb") as handle:
            os.fsync(handle.fileno())
        proof = drill(archive)  # Separate extraction, hashes, SQLite check and durable new writes.
        archive_hash = digest(archive)
        try:
            os.link(archive, target, follow_symlinks=False)  # Atomic no-overwrite file publication.
        except FileExistsError as exc:
            raise DestinationError("Destination appeared concurrently; nothing overwritten") from exc
        sync_directory(target.parent)
    return _summary(manifest, "backup") | {"archive_sha256": archive_hash, "restore_drill": proof}
