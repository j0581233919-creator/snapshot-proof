# Snapshot Proof

Snapshot a small SQLite application and explicitly selected artifact files, then
**restore and perform new writes before accepting the snapshot**. The tool uses
SQLite's online backup API, strict manifests, independent hash/integrity checks,
and a cooperative application lock with a persistent dirty marker.

Linux, Python 3.11+ and SQLite. Runtime and tests use only the standard library.
This is one local application's snapshot, not a backup service or application
restart system. Applications must integrate the documented consistency protocol.

## Quick start

From the checkout or the extracted release's `snapshot-proof/` directory:

```sh
python3 -m snapshot_proof demo
python3 -m unittest discover -v
```

The demo creates a private directory in OS temporary storage, prints its path and
leaves the synthetic application, snapshot and restored application for inspection.
It backs up a real historical database row and text artifact, verifies the archive,
restores to a new directory, adds another row/artifact to the **same application
table**, reopens SQLite, and independently checks both records and files. An
unselected synthetic `.env` file must be absent from the snapshot and restoration.
Use `demo --directory PRIVATE_DIRECTORY` to choose persistent local storage.

The working source needs no installation. For optional installation into your own
virtual environment, `python3 -m pip install .` installs the `snapshot-proof` command;
building requires setuptools 68+ and its build tooling. No third-party runtime
packages are required. `uv.lock` records the empty runtime graph, not optional build
tools; uv is not needed. No PyPI publication or network service is part of this release.

## Real CLI example

Use a private local POSIX filesystem that supports permissions, flock, fsync, hard
links and Linux `renameat2(RENAME_NOREPLACE)`. Permission-less shared mounts, network
filesystems and symlinked roots are unsupported. The source checkout itself may be
elsewhere; the application and output parent directories must be owned and private.

```sh
case_dir=$(mktemp -d -t snapshot-proof-example-XXXXXX)
python3 -m snapshot_proof sample-init "$case_dir/application" "$case_dir/config.json"
python3 -m snapshot_proof backup "$case_dir/application" "$case_dir/config.json" "$case_dir/snapshot.zip"
python3 -m snapshot_proof verify "$case_dir/snapshot.zip"
python3 -m snapshot_proof drill "$case_dir/snapshot.zip"
python3 -m snapshot_proof restore "$case_dir/snapshot.zip" "$case_dir/restored"
python3 -m snapshot_proof sample-add "$case_dir/restored" new-002 'New work after restoration.'
```

Every operation returns JSON on stdout on success; semantic and operational errors
return JSON on stderr. The sample commands create a tiny deterministic application;
they are not migrations or adapters for arbitrary platforms. No original platform
or user database is accessed by the demo/tests. Each destination must be nonexistent,
including no pre-created empty directory. Existing data is never overwritten.

| Exit | Meaning |
| --- | --- |
| 0 | Operation passed its documented checks |
| 2 | Invalid configuration, unsupported source path/permissions or CLI usage |
| 3 | Missing/dirty application protocol, busy lock/database or backup deadline |
| 4 | Malformed/unsupported/corrupt snapshot, database or historical witness failure |
| 5 | Existing destination or snapshot destination inside the application root |
| 6 | Filesystem/SQLite operational failure (for example missing input or disk failure) |

Argument-parser usage errors use argparse's normal stderr text with exit 2.
An error after final publication (for example parent fsync failure) is an uncertain
result: inspect and verify the named destination before retrying. Retrying never
overwrites it. Reports can be redirected to a **separate** operator-selected file.

## Integrate one existing application

Stop **all** writers, confirm the database/artifact set is already consistent, and
integrate every database/artifact writer with `application_write(root)` before
enrollment. Initial enrollment is an explicit operator attestation, not automatic
discovery of application correctness:

```sh
python3 -m snapshot_proof init PRIVATE_APPLICATION_ROOT --writers-integrated
```

`init` refuses an existing protocol directory. Do not initialize a dirty application
as a shortcut to bypass recovery. Only integrate your own application; this utility
does not modify or retrofit the NUSOOR, NEXUS or ATHEER platforms.

```python
from snapshot_proof import application_write

with application_write(root):
    # Keep all mutations of both database and artifact files inside this context.
    # Write/fsync artifacts and parent directories, commit SQLite, and close all
    # database connections before leaving. Use synchronous=FULL for SQLite.
    perform_application_update()
```

`perform_application_update` denotes the integrating application's operation, not a
bundled API. See the actual implementation in `snapshot_proof/sample.py:add_record`
for working file creation, fsync and SQLite commit code.

Protocol `snapshot-proof.flock-dirty.v1`:

1. Initialization creates a fixed `.snapshot-proof/lock` inode and `state.json`
   with exactly `{"version":1,"status":"CLEAN"}`. Never replace/delete the lock.
2. Each writer takes exclusive flock on that file. Before any mutation it atomically
   persists `DIRTY` and fsyncs the protocol directory. A writer must first see CLEAN.
3. The writer commits its logical update, fsyncs artifacts/directories, closes SQLite
   connections, then atomically writes/fsyncs CLEAN **while still holding the lock**.
4. Snapshot takes that same exclusive lock, requires CLEAN, captures the database
   through `Connection.backup`, and copies selected artifacts before releasing it.
   SQLite `BEGIN IMMEDIATE` additionally blocks database writers during database
   capture. It does not police arbitrary filesystem writers.
5. Writer exceptions/process death leave DIRTY even if some work rolled back. New
   writers and snapshots refuse it. Application-specific reconciliation must happen
   offline; no force/clear/automatic repair command is provided.

Lock waits are bounded (default 5 seconds, configurable 0.01–30). Database lock wait
is 2 seconds; online backup has a 10-second progress deadline. These are software
bounds, not hard real-time guarantees for hung storage. All application participants
must follow the same protocol. The tool **cannot guarantee cross-file consistency
when any writer bypasses it**, lies about enrollment, or marks CLEAN too early.
Artifact hash/stat comparisons detect some violations but do not establish safety
for uncooperative applications. A SQLite transaction alone does not freeze artifacts.

## Configuration, manifest and library

A config file outside the application root selects one database and 1–100 individual
artifact files; no directory walk/glob or credential/configuration discovery occurs.
Paths are relative to the application root and use bounded ASCII components without
dot prefixes. No symlink/hardlink paths or database sidecars are accepted; a second
SQLite database cannot be included as a raw artifact. Example shape:

```json
{
  "version": 1,
  "database": "application.sqlite3",
  "artifacts": ["artifacts/historical-001.txt"],
  "witness": {
    "table": "documents",
    "key_column": "id",
    "key_value": "historical-001",
    "artifact_column": "artifact",
    "hash_column": "sha256",
    "artifact": "artifacts/historical-001.txt",
    "sha256": "REPLACE_WITH_THE_KNOWN_64_CHARACTER_LOWERCASE_SHA256"
  }
}
```

The shape above is illustrative: use the executable `sample-init` command to
generate a runnable config with a real hash. The witness must identify exactly one
historical row whose artifact path/hash matches both the known expected identity
and actual selected file. It uses validated identifiers and a parameterized key;
no arbitrary SQL, callbacks or embedded archive code is executed. The database and
its foreign keys must pass integrity checks. The operator must select **all files
needed for the intended application recovery**; the tool does not infer database-to-
file relationships beyond the explicit historical witness. Unselected referenced
files and custom schema invariants are not verified.

```python
from snapshot_proof import backup, verify, restore, drill

report = backup(root, config_dict, new_archive_path)
verification = verify(new_archive_path)
restore_report = restore(new_archive_path, new_destination_path)
drill_report = drill(new_archive_path)
```

The ZIP contains only `manifest.json` and `payload/<relative path>` entries. Format
`snapshot-proof`, integer version `1`, snapshot UUID, UTC epoch creation time and
consistency protocol identify it. Each file has path, size, SHA-256 and database/
artifact kind. The manifest stores relative identities, not source absolute paths
or original configuration. Total payload is limited to 64 MiB; the manifest to
64 KiB. v1 accepts only unencrypted, uncompressed ZIP_STORED regular entries.
Extra/missing/duplicate entries, unknown fields/formats, path traversal, links,
conflicting paths, hash/size mismatches, corruption and failed witnesses are refused.

## What is proved

Before `backup` publishes the final archive, a fresh temporary extraction recomputes
every file hash, checks SQLite integrity/foreign keys and reads the known historical
row and matching artifact. The isolated drill then creates a new random probe table
and linked artifact, commits, reopens and verifies that work, and rechecks history.
Probe mutations occur **only in a disposable restore**, not the archive, live app
or caller's restored directory. Reports explicitly say `application_boot_tested:false`.
The synthetic demo additionally performs new work in the application's own table.
The drill proves this declared recovery contract, not all business invariants,
application startup, external dependencies or authenticity of a malicious archive.

`verify` independently validates content and the historical witness without new
writes; `drill` also proves new writes. `restore` verifies into a private staging
directory, creates fresh local protocol files and atomically publishes the whole
directory with Linux no-replace rename. It does not alter payload contents or run
the application. It always rejects existing destinations, even empty ones and links.

## Safety and limitations

- The source database is captured through SQLite's supported backup API, including
  committed WAL content. Raw active database files and sidecars are not copied.
  Application logical data is not changed by capture; SQLite locking/checkpoint
  activity can change physical database/sidecar state.
- Staging is private and on the destination filesystem. Files/directories are
  fsynced; ZIP publication uses atomic exclusive hard linking, and directory
  publication uses `RENAME_NOREPLACE`. No overwrite fallback exists. Atomicity
  and durability still depend on filesystem/kernel/hardware guarantees.
- A killed snapshot/restore can leave private `.snapshot-proof-stage-*` directories.
  They are never accepted as snapshots or automatically reused/purged. Retry with
  the same still-absent final destination; inspect orphaned staging privately.
  Only an invocation's own temporary files are normally cleaned up.
- Runtime data needs a private local Linux filesystem with real permissions and
  reliable locking/fsync. Unsupported filesystems fail closed. The demo defaults
  to OS temporary storage because shared source mounts may not enforce permissions.
- Snapshots are **unencrypted** and contain the complete selected database,
  including any sensitive rows/free-page content already in it. Explicit artifacts
  may contain secrets. Selection is not redaction. No credentials are discovered
  automatically, but the operator must review the database and chosen files.
- SHA-256 detects accidental corruption against a trusted manifest. An attacker
  able to replace both payload and manifest can forge a coherent snapshot; retain
  the returned whole-archive hash through a separate trusted channel. No signatures,
  encryption, remote storage, compression, retention service or distributed snapshot
  claim is made. Use updated Python/SQLite when processing untrusted files.
- Source roots and output parents are trusted, owned, private directories. This
  tool does not defend against an adversary with the same local UID/root access or
  hostile path replacement within those directories. Restore normalizes file
  permissions to 0600/directories to 0700, not original ownership/modes/timestamps.

## Provenance and license

The implementation, tests and examples were newly authored with AI assistance.
Only verified concepts from the preserved NUSOOR backup/persistence and NEXUS
durable-state code informed the design; no original code, private configuration,
customer data, third-party bundles or Git histories were copied. ATHEER's inspected
session storage is delegated to a third-party client, not a standalone restore proof.
The four completed standalone projects were inventoried and were not modified.
See [provenance](docs/PROVENANCE.md), [source fingerprints](docs/source-manifest.json)
and [SECURITY.md](SECURITY.md). The new project is licensed under [MIT](LICENSE),
SPDX `MIT`, with neutral Snapshot Proof contributor attribution. The preserved
platforms are not relicensed. Release test evidence is recorded in the separate
delivery-area engineering report.
