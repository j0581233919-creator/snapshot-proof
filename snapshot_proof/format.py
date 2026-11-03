"""Strict format validation, streaming extraction, independent hashes and SQLite/witness checks."""

from contextlib import closing
import hashlib
import os
from pathlib import PurePosixPath
import re
import sqlite3
import stat
import time
import zipfile

from .protocol import PROTOCOL
from .safety import (MAX_BYTES, ProofError, VerificationError, exact_keys, parse_json,
                     regular, relative, sha256, sync_directory)


def witness_spec(value):
    exact_keys(value, ("table", "key_column", "key_value", "artifact_column", "hash_column", "artifact", "sha256"))
    for field in ("table", "key_column", "artifact_column", "hash_column"):
        if not isinstance(value[field], str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", value[field]):
            raise ProofError("Witness requires simple SQL identifiers")
    key = value["key_value"]
    if not ((type(key) is int and -(2**63) <= key < 2**63)
            or (isinstance(key, str) and 1 <= len(key) <= 128)):
        raise ProofError("Witness key must be a bounded string or SQLite integer")
    relative(value["artifact"])
    sha256(value["sha256"])
    return value


def paths_check(database, artifacts):
    relative(database)
    if not isinstance(artifacts, list) or not 1 <= len(artifacts) <= 100:
        raise ProofError("Select 1–100 explicit artifact files")
    for path in artifacts:
        relative(path)
    paths = [database, *artifacts]
    if len(set(paths)) != len(paths):
        raise ProofError("Duplicate database/artifact paths")
    for path in paths:
        if any(str(parent) in paths for parent in PurePosixPath(path).parents if str(parent) != "."):
            raise ProofError("Conflicting file/directory paths")
    if any(path in (database + suffix for suffix in ("-wal", "-shm", "-journal")) for path in artifacts):
        raise ProofError("SQLite sidecars cannot be artifacts")


def configuration(value):
    exact_keys(value, ("version", "database", "artifacts", "witness"))
    if type(value["version"]) is not int or value["version"] != 1:
        raise ProofError("Unsupported configuration version")
    paths_check(value["database"], value["artifacts"])
    witness_spec(value["witness"])
    if value["witness"]["artifact"] not in value["artifacts"]:
        raise ProofError("Witness artifact must be explicitly selected")
    return value


def manifest_spec(value):
    exact_keys(value, ("format", "version", "snapshot_id", "created_at", "consistency", "database", "files", "witness"))
    if (value["format"] != "snapshot-proof" or type(value["version"]) is not int or value["version"] != 1
            or value["consistency"] != PROTOCOL):
        raise ProofError("Unsupported snapshot format or consistency protocol")
    if not isinstance(value["snapshot_id"], str) or not re.fullmatch(r"[a-f0-9]{32}", value["snapshot_id"]):
        raise ProofError("Invalid snapshot identity")
    if type(value["created_at"]) is not int or not 0 <= value["created_at"] < 2**63:
        raise ProofError("Invalid creation timestamp")
    if not isinstance(value["files"], list) or not 2 <= len(value["files"]) <= 101:
        raise ProofError("Invalid file inventory")
    total = 0
    artifacts, databases = [], []
    for item in value["files"]:
        exact_keys(item, ("path", "size", "sha256", "kind"))
        relative(item["path"])
        sha256(item["sha256"])
        if type(item["size"]) is not int or not 0 <= item["size"] <= MAX_BYTES:
            raise ProofError("Invalid file size")
        total += item["size"]
        if item["kind"] == "database":
            databases.append(item["path"])
        elif item["kind"] == "artifact":
            artifacts.append(item["path"])
        else:
            raise ProofError("Unsupported file kind")
    if total > MAX_BYTES or databases != [value["database"]]:
        raise ProofError("Snapshot requires one database and at most 64 MiB total")
    configuration(dict(version=1, database=value["database"], artifacts=artifacts, witness=value["witness"]))
    return value


def database_check(root, manifest):
    """Only builtin SQL; no embedded callbacks, arbitrary SQL or archive-supplied code."""
    database = root / manifest["database"]
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=2)) as conn:
        conn.execute("PRAGMA trusted_schema=OFF")
        deadline = time.monotonic() + 5

        def progress():
            return int(time.monotonic() > deadline)
        conn.set_progress_handler(progress, 1000)
        if conn.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise VerificationError("SQLite integrity check failed")
        if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise VerificationError("SQLite foreign key check failed")
        witness = manifest["witness"]
        table = witness["table"]
        if conn.execute("SELECT type FROM sqlite_schema WHERE name=?", (table,)).fetchone() != ("table",):
            raise VerificationError("Historical witness must reference a real table")
        columns = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
        if any(witness[field] not in columns for field in ("key_column", "artifact_column", "hash_column")):
            raise VerificationError("Historical witness column is missing")
        statement = (f'SELECT "{witness["artifact_column"]}","{witness["hash_column"]}" '
                     f'FROM "{table}" WHERE "{witness["key_column"]}"=? LIMIT 2')
        rows = conn.execute(statement, (witness["key_value"],)).fetchall()
        if rows != [(witness["artifact"], witness["sha256"])]:
            raise VerificationError("Known historical row does not match artifact identity/hash")
    artifact = regular(root / witness["artifact"])
    with artifact.open("rb") as handle:
        if hashlib.file_digest(handle, "sha256").hexdigest() != witness["sha256"]:
            raise VerificationError("Historical artifact does not match the known witness")


def unpack_verified(archive, root):
    """Extract only a complete validated inventory into a new, private staging directory."""
    try:
        archive = regular(archive)
        if archive.stat().st_size > MAX_BYTES + 1024 * 1024:
            raise VerificationError("Archive exceeds 65 MiB")
        with zipfile.ZipFile(archive) as z:
            infos = z.infolist()
            names = [item.filename for item in infos]
            if not 3 <= len(infos) <= 102 or len(set(names)) != len(names) or z.comment:
                raise VerificationError("Invalid archive inventory")
            for item in infos:
                mode = item.external_attr >> 16
                if (item.orig_filename != item.filename or item.is_dir() or item.flag_bits & 1
                        or item.compress_type != zipfile.ZIP_STORED or stat.S_IFMT(mode) not in (0, stat.S_IFREG)
                        or item.file_size != item.compress_size):
                    raise VerificationError("Only regular, unencrypted, stored ZIP files are supported")
            info = z.getinfo("manifest.json")
            if info.file_size > 65536:
                raise VerificationError("Manifest exceeds 64 KiB")
            manifest = manifest_spec(parse_json(z.read(info)))
            expected = {"payload/" + item["path"] for item in manifest["files"]} | {"manifest.json"}
            if set(names) != expected:
                raise VerificationError("Missing or unlisted archive entries")
            for item in manifest["files"]:
                info = z.getinfo("payload/" + item["path"])
                if info.file_size != item["size"]:
                    raise VerificationError("Archived size does not match manifest")
                path = root / item["path"]
                path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                count = 0
                hasher = hashlib.sha256()
                with os.fdopen(fd, "wb") as target, z.open(info) as source:
                    while chunk := source.read(65536):
                        count += len(chunk)
                        if count > item["size"]:
                            raise VerificationError("Expanded file exceeds manifest bound")
                        hasher.update(chunk)
                        target.write(chunk)
                    target.flush()
                    os.fsync(target.fileno())
                if count != item["size"] or hasher.hexdigest() != item["sha256"]:
                    raise VerificationError("Snapshot content hash mismatch")
            database_check(root, manifest)
            for path in sorted((p for p in root.rglob("*") if p.is_dir()), reverse=True):
                sync_directory(path)
            sync_directory(root)
            return manifest
    except VerificationError:
        raise
    except (ProofError, ValueError, TypeError, KeyError, RecursionError, sqlite3.Error, zipfile.BadZipFile) as exc:
        raise VerificationError("Malformed, unsupported or corrupt snapshot") from exc
