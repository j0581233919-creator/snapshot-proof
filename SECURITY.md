# Security

Snapshot Proof v0.1.0 is a local Linux utility for a cooperating application's
SQLite database and explicitly selected artifact files. It does not retrofit safe
snapshotting into an arbitrary application. No original platform is operated by it.

## Trust and consistency

All database and artifact writers must take the same exclusive flock and obey the
persistent DIRTY/CLEAN protocol in README. Initialization attests that writers are
integrated, stopped and the application is consistent. The tool cannot independently
discover hidden writers or validate an operator's attestation. Unsupported or
uncooperative applications are outside the consistency guarantee. Do not initialize
or manually mark a partially updated application CLEAN to force a successful backup.
There is no automatic dirty-state reset; application-specific offline reconciliation
is required after writer failure. Never replace the fixed lock inode while in use.

Use owned private directories on a local Linux filesystem with real permissions,
flock, fsync, hard links and no-replace rename support. Shared mounts without POSIX
permissions and network filesystems are unsupported. This is not an isolation
boundary against the same local UID, root, malicious filesystem implementations
or hostile directory replacement by their owner. Symlinks and hard links in selected
source paths, SQLite sidecars and incoming archive paths are refused. Source roots
and output parents must not traverse symlinks. Runtime files are private on supported
filesystems; database/free pages, WAL or temp files may still contain sensitive data.

## Archive handling

Only one database and explicitly selected regular artifact files are captured.
There is no environment, credential, recursive-directory or home-directory discovery.
That does not redact a chosen database or artifact: **snapshots are unencrypted** and
contain their selected contents. Keep production snapshots and verification staging
out of Git, public uploads, issues and shared directories. The source release contains
no runtime snapshots. The demo uses synthetic records only.

The verifier accepts bounded versioned manifests and regular ZIP_STORED entries,
validates paths, rejects missing/extra/duplicate members and recomputes sizes/hashes
before accepting restored state. Restores occur only under a new private staging
directory and are published with atomic no-overwrite rename. Archive code, arbitrary
SQL and application startup are never executed. The drill does issue built-in SQL
against an isolated copy and writes a probe table and linked artifact there. Keep
Python/SQLite updated when handling untrusted databases; this tool is not a sandbox
for exploiting native SQLite parsers.

SHA-256 manifests detect corruption against trusted metadata, not adversarial
replacement of the archive. Keep the returned archive SHA-256 separately through a
trusted channel. The tool offers no signatures, encryption or origin authentication.
Witness checks verify one declared historical relationship, not every application
invariant or missing artifact reference. The operator defines the recovery scope.

Interrupted operations may leave private staging containing database contents.
Staging is never a valid completed backup, never automatically reused and not
automatically swept after a later restart. Investigate it privately. An fsync failure
after publication can leave an uncertain completed destination; inspect/verify before
retrying. Existing destinations, including empty directories and links, are always
refused. The tool does not delete or overwrite caller data to recover.

## Disclosure

Use the hosting repository's private vulnerability reporting channel if enabled.
Otherwise request a private maintainer contact before sharing sensitive details;
no mailbox is invented here. Provide version, synthetic reproduction and expected
versus observed behavior. Do not attach real databases, snapshots, credentials or
customer files. The release tests are engineering evidence, not security certification.
