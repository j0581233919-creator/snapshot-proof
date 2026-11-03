# Provenance and extraction decision

Static inspection on 2026-11-03. [Source fingerprints](source-manifest.json) identify
the 13 inspected implementation/metadata inputs by relative source path, byte size
and SHA-256. No original Python/Node modules were imported, started, repaired,
rebuilt or modified, and no original databases, credentials or private runtime
configuration were read. Reports were not accepted as substitutes for source.

## Completed utility inventory

The four completed standalone delivery repositories were inventoried read-only:

| Project | Actual scope | Snapshot/restore-proof overlap |
| --- | --- | --- |
| Agent Delegation & Verified Tasks | Durable task delegation, verified deterministic effects | No database/artifact snapshot utility |
| Message Shield | Durable webhook deduplication/outbox with honest delivery uncertainty | No snapshot/restore drill |
| AI Budget Guard | SQLite usage/cost reservations and settlement | No application snapshot utility |
| Tenant Event Stream | Tenant-isolated event history and SSE replay | No database/artifact restore proof |

Their README descriptions and file inventories were checked; no snapshot/backup/
restore implementation was found by scoped filename inspection. No files or Git
history in those repositories were modified. Local inventory commit identities
are recorded in [completed-projects.json](completed-projects.json), without remote
URLs, absolute workstation paths or copied implementation.

## Actual preserved behavior

| Source | Verified implementation | Limitation for this utility |
| --- | --- | --- |
| NUSOOR delivery `DEPLOYMENT/state_snapshot.py` | Explicit component paths, symlink refusal, SQLite `Connection.backup`, integrity check, hashes, refusal to restore over existing state | Cross-component consistency depends on `writers_quiesced:true` operator assertion; copies into final directory before verification; restore copies/checks hashes and explicitly leaves database import/application restart separate; no linked historical witness or subsequent-write drill |
| `DEPLOYMENT/backup_local_runtime.py` plus backup/restore shell entrypoints | Real orchestration code to stop private-profile writers, take logical dumps and call the snapshot script | Coupled to platform/runtime/container infrastructure; never executed; outside this extraction's scope |
| NUSOOR `TenantExecutionStore` | SQLite reservations, immediate transactions, FULL synchronization and preserved uncertainty | Platform configuration coupling; not a database-plus-artifact consistency boundary or snapshot tool |
| NUSOOR `current_no_backup_policy.py` | Returns a policy dictionary with backup/restore disabled | Descriptive policy output, not an executing backup implementation |
| NEXUS `shared/durable-store.js` and `executionLedger.ts` | Synchronous state transactions, bounded directory-lock wait, exclusive temporary file, fsync, rename, directory fsync; unresolved records preserved | Single JSON store transaction, not a SQLite/artifact group snapshot; process death can leave a lock requiring operator recovery; not a restore drill |
| NEXUS `src/utils/db.ts` | Optional PostgreSQL calls when configured; disabled persistence when absent and caught errors | External database coupling; not in-scope SQLite snapshot behavior |
| ATHEER integration `index.js` | Messaging session persistence delegated to `whatsapp-web.js` LocalAuth under a configured session path | No independently implemented SQLite/artifact backup proof; third-party/browser session data and credentials must not be extracted |

The useful concepts are supported SQLite backup, explicit scope, hash inventories,
private staging, non-overwrite restore, bounded locks and preserving uncertain
state. The original deployment script is related functionality, but it is embedded
in the preserved platform release, is not one of the completed utilities and lacks
the narrow restore-and-new-work acceptance contract. A separate minimal tool is
useful; no general novelty or superiority over established backup tools is claimed.

## What was reused and rewritten

**Original code, tests, scripts, configuration, data, assets, bundles and history
copied: none.** All implementation, tests, examples and documentation were newly
authored with AI assistance. Only the behavior/concepts above informed the design.
The lock plus durable dirty marker, strict versioned ZIP, independent verification,
historical witness and isolated new-write drill were implemented for this project.
The new source is not presented as a legally independent clean-room process; the
inputs were inspected first, and no original implementation fragments were reused.

The supported APIs/protocols were checked against primary references:

- [SQLite online backup](https://www.sqlite.org/backup.html) and
  [Python Connection.backup](https://docs.python.org/3/library/sqlite3.html#sqlite3.Connection.backup)
- [Linux flock advisory locking](https://man7.org/linux/man-pages/man2/flock.2.html)
- [Linux renameat2 / RENAME_NOREPLACE](https://man7.org/linux/man-pages/man2/rename.2.html)

No reference code examples or third-party implementation were copied. Linux-specific
libc calls are made with the Python standard-library ctypes interface; no binary
is bundled. Consistency requires cooperative writers; it is not inferred from the
fact that SQLite's backup completed or that a ZIP checksum exists.

## Licensing verdict

The inspected NUSOOR project metadata establishes no general redistribution license.
NEXUS and ATHEER package metadata label their packages ISC with no author identified;
this is insufficient evidence of ownership/permission for every preserved fragment.
No original platform is relicensed, and no permission to copy private implementation
is assumed. Including none of those implementations avoids that licensing uncertainty.

The newly authored project uses the standard [SPDX MIT license](https://spdx.org/licenses/MIT.html)
with neutral `Snapshot Proof contributors` attribution, not an invented personal or
company identity. No third-party runtime/test Python packages or vendored code are
included. Python/its standard library keep their
[PSF and accompanying licenses](https://docs.python.org/3/license.html);
[SQLite is public domain](https://www.sqlite.org/copyright.html). Neither runtime
is distributed here. Optional building uses [setuptools, MIT](https://github.com/pypa/setuptools/blob/main/LICENSE)
and build tooling outside the runtime lock; no dependency code is shipped. No
additional third-party notice is required for unbundled material. This records an
engineering provenance review, not a claim to the platforms' legal title.

The release ZIP must contain only new source, tests, synthetic examples and docs.
Runtime snapshots, databases, staging, credentials, private absolute paths and old
Git history are excluded. Actual verification outcomes, commit/tag and release
archive hash are recorded in the delivery-area engineering report.
