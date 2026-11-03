"""Small local-filesystem boundary; Linux only, no overwrite fallback."""

import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import stat

MAX_BYTES = 64 * 1024 * 1024


class ProofError(ValueError):
    exit_code = 2


class ConsistencyError(ProofError):
    exit_code = 3


class VerificationError(ProofError):
    exit_code = 4


class DestinationError(ProofError):
    exit_code = 5


def exact_keys(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ProofError("Unexpected or missing object fields")


def json_bytes(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()


def parse_json(raw):
    def reject_constant(_):
        raise ProofError("Non-finite JSON")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ProofError("Duplicate JSON fields")
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=reject_constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ProofError("Invalid JSON") from exc


def relative(value):
    if (not isinstance(value, str) or len(value) > 240 or not value
            or any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", part)
                   for part in value.split("/"))):
        raise ProofError("Paths require bounded relative ASCII components, without dot prefixes")
    return value


def sha256(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise ProofError("Invalid SHA-256")
    return value


def clean_path(path):
    path = Path(path).absolute()
    if path.resolve() != path:
        raise ProofError("Symlinks and non-canonical paths are refused")
    return path


def private_directory(path):
    path = clean_path(path)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ProofError("Directory must be owned and private (0700)")
    return path


def regular(path):
    path = clean_path(path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ProofError("Only regular files without symlinks or hard links are accepted")
    return path


def destination(path):
    path = Path(path).absolute()
    private_directory(path.parent)
    if os.path.lexists(path):
        raise DestinationError("Destination already exists; nothing will be overwritten")
    return path


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_new(path, raw):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())


def digest(path):
    with regular(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def publish_directory(staged, target):
    """Linux RENAME_NOREPLACE refuses even an existing empty directory, atomically."""
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        rename = libc.renameat2
    except AttributeError as exc:
        raise ProofError("Linux renameat2 support is required; no unsafe fallback") from exc
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(staged), -100, os.fsencode(target), 1) != 0:
        error = ctypes.get_errno()
        if error == 17:
            raise DestinationError("Destination appeared concurrently; nothing overwritten")
        raise OSError(error, "Atomic no-overwrite directory publication failed")
    sync_directory(target.parent)
