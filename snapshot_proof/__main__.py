"""JSON CLI results; nonzero exit codes distinguish refusal, corruption and I/O failure."""

import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys

from . import backup, drill, initialize, restore, verify
from .safety import ProofError, parse_json, regular
from .sample import add_record, create_sample, run_demo


def main():
    parser = argparse.ArgumentParser(description="Snapshot Proof: SQLite + explicitly selected artifact files")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Enroll a consistent application while all writers are stopped")
    init.add_argument("root", type=Path)
    init.add_argument("--writers-integrated", action="store_true", required=True)
    capture = commands.add_parser("backup", help="Capture, verify, restore-drill, then publish a new ZIP")
    capture.add_argument("root", type=Path)
    capture.add_argument("config", type=Path)
    capture.add_argument("archive", type=Path)
    capture.add_argument("--lock-timeout", type=float, default=5)
    for command in ("verify", "drill"):
        item = commands.add_parser(command)
        item.add_argument("archive", type=Path)
    item = commands.add_parser("restore")
    item.add_argument("archive", type=Path)
    item.add_argument("destination", type=Path)
    item = commands.add_parser("demo")
    item.add_argument("--directory", type=Path, help="Private local POSIX directory; defaults to OS temporary storage")
    item = commands.add_parser("sample-init", help="Create a new synthetic cooperating sample application")
    item.add_argument("root", type=Path)
    item.add_argument("config", type=Path)
    item = commands.add_parser("sample-add", help="Perform real new work in the synthetic sample application")
    item.add_argument("root", type=Path)
    item.add_argument("record_id")
    item.add_argument("text")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.command == "init":
            result = initialize(args.root, writers_integrated=args.writers_integrated)
        elif args.command == "backup":
            if regular(args.config).stat().st_size > 65536:
                raise ProofError("Configuration exceeds 64 KiB")
            config = parse_json(args.config.read_bytes())
            result = backup(args.root, config, args.archive, timeout=args.lock_timeout)
        elif args.command == "verify":
            result = verify(args.archive)
        elif args.command == "restore":
            result = restore(args.archive, args.destination)
        elif args.command == "drill":
            result = drill(args.archive)
        elif args.command == "demo":
            result = run_demo(args.directory)
        elif args.command == "sample-init":
            create_sample(args.root, args.config)
            result = dict(ok=True, operation="sample-init")
        else:
            result = add_record(args.root, args.record_id, args.text)
        print(json.dumps(result, sort_keys=True))
        return 0
    except ProofError as exc:
        print(json.dumps(dict(ok=False, exit_code=exc.exit_code, error=str(exc))), file=sys.stderr)
        return exc.exit_code
    except (OSError, sqlite3.Error):
        print(json.dumps(dict(ok=False, exit_code=6, error="Filesystem or SQLite operation failed")), file=sys.stderr)
        return 6


if __name__ == "__main__":
    raise SystemExit(main())
