from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import copy
import hashlib
import json
import os
from pathlib import Path
import selectors
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile

from snapshot_proof import (ConsistencyError, DestinationError, ProofError, VerificationError,
                            application_write, backup, drill, initialize, restore, verify)
from snapshot_proof.safety import MAX_BYTES, json_bytes, parse_json, write_new
from snapshot_proof.sample import add_record, create_sample, run_demo

ROOT = Path(__file__).resolve().parents[1]


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="snapshot-proof-test-")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.root = self.directory / "application"
        self.config_file = self.directory / "config.json"
        self.config = create_sample(self.root, self.config_file)
        self.archive = self.directory / "snapshot.zip"

    def capture(self):
        return backup(self.root, self.config, self.archive)

    def changed_archive(self, *, change_manifest=None, change_entries=None, compression=zipfile.ZIP_STORED):
        with zipfile.ZipFile(self.archive) as source:
            entries = {name: source.read(name) for name in source.namelist()}
        manifest = json.loads(entries["manifest.json"])
        if change_manifest:
            change_manifest(manifest)
            entries["manifest.json"] = json_bytes(manifest)
        if change_entries:
            change_entries(entries)
        path = self.directory / ("changed-" + str(time.monotonic_ns()) + ".zip")
        with zipfile.ZipFile(path, "x", compression=compression) as target:
            for name, raw in entries.items():
                target.writestr(name, raw)
        return path

    def child(self, mode, *args):
        child = subprocess.Popen([sys.executable, "-m", "tests.process_ops", mode, *map(str, args)],
                                 cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True,
                                 env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        def stop():
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)
        self.addCleanup(stop)
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            self.assertTrue(selector.select(timeout=10), "Child failed to reach real-operation barrier")
        line = child.stdout.readline()
        if line != "ready\n":
            self.fail("Child failed: " + child.communicate(timeout=5)[1])
        return child

    def cli(self, *args):
        return subprocess.run([sys.executable, "-m", "snapshot_proof", *map(str,args)],cwd=ROOT,
                              env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
                              capture_output=True,text=True,timeout=20)

    def test_backup_independently_verifies_and_drills_before_acceptance(self):
        report = self.capture()
        self.assertTrue(report["restore_drill"]["new_database_write_verified"])
        self.assertTrue(report["restore_drill"]["new_artifact_verified"])
        self.assertTrue(report["restore_drill"]["historical_data_preserved"])
        self.assertFalse(report["restore_drill"]["application_boot_tested"])
        self.assertEqual(report["archive_sha256"],hashlib.sha256(self.archive.read_bytes()).hexdigest())
        self.assertTrue(verify(self.archive)["historical_witness_verified"])
        self.assertEqual(stat.S_IMODE(self.archive.stat().st_mode),0o600)

    def test_real_restore_and_application_new_work_preserve_history(self):
        self.capture()
        target = self.directory / "restored"
        restore(self.archive, target)
        add_record(target,"new-002","Real new work.\n")
        with closing(sqlite3.connect(target/"application.sqlite3")) as conn:
            rows = conn.execute("SELECT id,artifact,sha256 FROM documents ORDER BY id").fetchall()
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchall(),[("ok",)])
        self.assertEqual([r[0] for r in rows],["historical-001","new-002"])
        for row in rows:
            self.assertEqual(hashlib.sha256((target/row[1]).read_bytes()).hexdigest(),row[2])
        self.assertEqual((target/self.config["artifacts"][0]).read_bytes(),
                         (self.root/self.config["artifacts"][0]).read_bytes())
        self.assertFalse((target/".env").exists())

    def test_online_backup_includes_committed_wal_content(self):
        database = self.root / "application.sqlite3"
        with closing(sqlite3.connect(database)) as live:
            live.execute("PRAGMA journal_mode=WAL")
            live.execute("PRAGMA wal_autocheckpoint=0")
            with application_write(self.root):
                live.execute("CREATE TABLE wal_only(message TEXT)")
                live.execute("INSERT INTO wal_only VALUES('committed only in WAL')")
                live.commit()
            self.assertGreater(Path(str(database)+"-wal").stat().st_size,0)
            self.capture()
            target = self.directory / "restored"
            restore(self.archive,target)
            with closing(sqlite3.connect(target/"application.sqlite3")) as conn:
                self.assertEqual(conn.execute("SELECT message FROM wal_only").fetchone(),("committed only in WAL",))
        with zipfile.ZipFile(self.archive) as z:
            self.assertFalse(any(name.endswith(("-wal","-shm","-journal")) for name in z.namelist()))

    def test_only_explicit_paths_are_included(self):
        outside = self.directory / "private-outside.txt"
        outside.write_bytes(b"SYNTHETIC-OUTSIDE-MUST-NOT-BE-CAPTURED")
        (self.root/"artifacts/unselected.txt").write_bytes(b"UNSELECTED-ARTIFACT")
        self.capture()
        with zipfile.ZipFile(self.archive) as z:
            self.assertEqual(set(z.namelist()),{"manifest.json","payload/application.sqlite3",
                                              "payload/artifacts/historical-001.txt"})
            all_bytes = b"".join(z.read(name) for name in z.namelist())
            self.assertNotIn(outside.read_bytes(), all_bytes)
            self.assertNotIn(b"UNSELECTED-ARTIFACT", all_bytes)
            self.assertNotIn(b"DEMO_ONLY_NOT_A_SECRET", all_bytes)
            self.assertNotIn(str(self.root).encode(), z.read("manifest.json"))

    def test_unenrolled_application_refused(self):
        unregistered = self.directory / "unregistered"
        unregistered.mkdir(mode=0o700)
        with self.assertRaises(ConsistencyError):
            backup(unregistered,self.config,self.archive)
        self.assertFalse(self.archive.exists())
        with self.assertRaises(ConsistencyError):
            initialize(unregistered)

    def test_failed_writer_leaves_dirty_and_prevents_snapshot(self):
        with self.assertRaises(RuntimeError):
            with application_write(self.root):
                raise RuntimeError("Synthetic interrupted application operation")
        with self.assertRaises(ConsistencyError):
            self.capture()
        with self.assertRaises(ConsistencyError):
            with application_write(self.root): pass
        self.assertFalse(self.archive.exists())
        with self.assertRaises(FileExistsError):
            initialize(self.root,writers_integrated=True)

    def test_killed_writer_does_not_erase_dirty_marker(self):
        child=self.child("writer",self.root)
        child.kill()
        child.communicate(timeout=5)
        with self.assertRaises(ConsistencyError): self.capture()
        self.assertFalse(self.archive.exists())

    def test_bounded_wait_for_busy_writer(self):
        child=self.child("writer",self.root)
        start=time.monotonic()
        with self.assertRaises(ConsistencyError):
            backup(self.root,self.config,self.archive,timeout=0.05)
        self.assertLess(time.monotonic()-start,1)
        child.communicate(input="continue\n",timeout=5)
        self.assertEqual(child.returncode,0)
        self.assertTrue(self.capture()["ok"])

    def test_concurrent_process_writer_and_consistent_database_artifact_boundary(self):
        raw=b"0\n"
        (self.root/"artifacts/counter.txt").write_bytes(raw)
        with closing(sqlite3.connect(self.root/"application.sqlite3")) as conn:
            conn.execute("CREATE TABLE counter(n INTEGER,sha256 TEXT)")
            conn.execute("INSERT INTO counter VALUES(0,?)",(hashlib.sha256(raw).hexdigest(),))
            conn.commit()
        self.config["artifacts"].append("artifacts/counter.txt")
        writer=self.child("counter",self.root)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(self.capture)
            time.sleep(0.05)
            self.assertFalse(future.done(),"Snapshot must wait through the partial logical application update")
            writer.communicate(input="commit\n",timeout=5)
            self.assertTrue(future.result(timeout=10)["ok"])
        target=self.directory/"restored"
        restore(self.archive,target)
        with closing(sqlite3.connect(target/"application.sqlite3")) as conn:
            n,sha=conn.execute("SELECT n,sha256 FROM counter").fetchone()
        self.assertEqual(n,1)
        self.assertEqual((target/"artifacts/counter.txt").read_bytes(),b"1\n")
        self.assertEqual(hashlib.sha256((target/"artifacts/counter.txt").read_bytes()).hexdigest(),sha)

    def test_busy_uncooperative_sqlite_transaction_is_rejected(self):
        with closing(sqlite3.connect(self.root/"application.sqlite3")) as conn:
            conn.execute("BEGIN IMMEDIATE")
            with self.assertRaises(ConsistencyError): self.capture()
        self.assertFalse(self.archive.exists())

    def test_interrupted_online_backup_never_publishes_partial_and_retry_works(self):
        child=self.child("interrupt-backup",self.root,self.config_file,self.archive)
        child.kill()
        child.communicate(timeout=5)
        self.assertFalse(self.archive.exists())
        self.assertTrue(list(self.directory.glob(".snapshot-proof-stage-*")))
        self.assertTrue(self.capture()["ok"])
        self.assertTrue(verify(self.archive)["ok"])

    def test_interrupted_restore_has_no_public_destination_and_retry_works(self):
        self.capture()
        target=self.directory/"restored"
        child=self.child("interrupt-restore",self.archive,target)
        child.kill()
        child.communicate(timeout=5)
        self.assertFalse(target.exists())
        self.assertTrue(restore(self.archive,target)["ok"])

    def test_existing_backup_and_restore_destinations_are_never_overwritten(self):
        self.capture()
        original=self.archive.read_bytes()
        with self.assertRaises(DestinationError): self.capture()
        self.assertEqual(self.archive.read_bytes(),original)
        for name,kind in (("empty","dir"),("occupied","dir"),("file","file")):
            target=self.directory/name
            if kind=="dir": target.mkdir(mode=0o700)
            else: target.write_bytes(b"keep")
            if name=="occupied": (target/"data").write_bytes(b"keep")
            with self.subTest(kind=name),self.assertRaises(DestinationError):restore(self.archive,target)
        self.assertEqual((self.directory/"occupied/data").read_bytes(),b"keep")

    def test_competing_restores_publish_exactly_one_destination(self):
        self.capture()
        target=self.directory/"restored"
        def perform(_):
            try: restore(self.archive,target);return "restored"
            except DestinationError:return "refused"
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(perform,range(2))),["refused","restored"])

    def test_competing_backups_cannot_replace_a_completed_archive(self):
        def perform(_):
            try:self.capture();return "backed-up"
            except DestinationError:return "refused"
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(perform,range(2))),["backed-up","refused"])
        self.assertTrue(verify(self.archive)["ok"])

    def test_wrong_historical_witness_refuses_backup(self):
        self.config["witness"]["sha256"]="0"*64
        with self.assertRaises(VerificationError): self.capture()
        self.assertFalse(self.archive.exists())

    def test_wrong_witness_column_rejected(self):
        self.config["witness"]["artifact_column"]="absent"
        with self.assertRaises(VerificationError): self.capture()

    def test_source_symlink_and_hardlink_artifacts_refused(self):
        outside=self.directory/"outside.txt"
        outside.write_bytes(b"synthetic private data")
        for name,link in (("linked.txt",os.symlink),("hard.txt",os.link)):
            path=self.root/"artifacts"/name
            link(outside,path)
            config=copy.deepcopy(self.config)
            config["artifacts"].append("artifacts/"+name)
            with self.subTest(name=name),self.assertRaises(ProofError):backup(self.root,config,self.archive)
        self.assertFalse(self.archive.exists())

    def test_symlinked_root_database_sidecar_and_output_refused(self):
        alias=self.directory/"alias"
        alias.symlink_to(self.root,target_is_directory=True)
        with self.assertRaises(ProofError):backup(alias,self.config,self.archive)
        sidecar=Path(str(self.root/"application.sqlite3")+"-wal")
        outside=self.directory/"outside-wal"
        outside.write_bytes(b"not state")
        sidecar.symlink_to(outside)
        with self.assertRaises(ProofError):self.capture()
        link=self.directory/"restore-link"
        link.symlink_to(self.root,target_is_directory=True)
        with self.assertRaises(DestinationError):restore(self.archive,link)

    def test_source_paths_and_sqlite_sidecars_cannot_escape_selection(self):
        for bad in ("../private", "/etc/passwd", "a/../b", "a\\b", ".env", "a//b", "application.sqlite3-wal"):
            config=copy.deepcopy(self.config)
            config["artifacts"].append(bad)
            with self.subTest(path=bad),self.assertRaises(ProofError):backup(self.root,config,self.archive)
        with self.assertRaises(DestinationError):backup(self.root,self.config,self.root/"snapshot.zip")

    def test_additional_sqlite_databases_are_not_raw_copied(self):
        other=self.root/"other.sqlite3"
        with closing(sqlite3.connect(other)) as conn:
            conn.execute("CREATE TABLE other(id INTEGER)")
        self.config["artifacts"].append("other.sqlite3")
        with self.assertRaises(ProofError):self.capture()

    def test_size_bound_rejects_oversized_artifact(self):
        artifact=self.root/self.config["artifacts"][0]
        with artifact.open("wb") as handle:handle.truncate(MAX_BYTES+1)
        with self.assertRaises(ProofError):self.capture()

    def test_corrupted_archive_and_modified_payload_are_rejected(self):
        self.capture()
        truncated=self.directory/"truncated.zip"
        truncated.write_bytes(self.archive.read_bytes()[:-30])
        with self.assertRaises(VerificationError):verify(truncated)
        name="payload/artifacts/historical-001.txt"
        changed=self.changed_archive(change_entries=lambda e:e.__setitem__(name,b"x"*len(e[name])))
        with self.assertRaises(VerificationError):verify(changed)
        target=self.directory/"restore-corrupt"
        with self.assertRaises(VerificationError):restore(changed,target)
        self.assertFalse(target.exists())

    def test_corrupt_database_rejected_even_when_manifest_hash_matches(self):
        self.capture()
        def change(entries):
            entries["payload/application.sqlite3"]=b"corrupt database"
            manifest=json.loads(entries["manifest.json"])
            for item in manifest["files"]:
                if item["kind"]=="database":
                    item.update(size=16,sha256=hashlib.sha256(b"corrupt database").hexdigest())
            entries["manifest.json"]=json_bytes(manifest)
        with self.assertRaises(VerificationError):verify(self.changed_archive(change_entries=change))

    def test_missing_extra_and_duplicate_zip_entries_rejected(self):
        self.capture()
        for change in (lambda e:e.pop("payload/artifacts/historical-001.txt"),lambda e:e.__setitem__("payload/private.txt",b"unlisted")):
            with self.assertRaises(VerificationError):verify(self.changed_archive(change_entries=change))
        duplicate=self.directory/"duplicate.zip"
        import warnings
        with zipfile.ZipFile(self.archive) as source,zipfile.ZipFile(duplicate,"x") as target:
            for name in source.namelist():target.writestr(name,source.read(name))
            with warnings.catch_warnings():
                warnings.simplefilter("ignore",UserWarning)
                target.writestr("manifest.json",source.read("manifest.json"))
        with self.assertRaises(VerificationError):verify(duplicate)

    def test_malicious_manifest_paths_are_rejected(self):
        self.capture()
        for bad in ("../outside", "/absolute", "a/../../escape", "a\\..\\b", "a//b", ".snapshot-proof/lock"):
            def change(manifest):manifest["files"][0]["path"]=bad
            with self.subTest(path=bad),self.assertRaises(VerificationError):verify(self.changed_archive(change_manifest=change))
        self.assertFalse((self.directory/"outside").exists())

    def test_zip_symlink_and_traversal_entries_rejected(self):
        self.capture()
        malicious=self.directory/"symlink.zip"
        with zipfile.ZipFile(self.archive) as source,zipfile.ZipFile(malicious,"x") as target:
            for name in source.namelist():
                if name.endswith("historical-001.txt"):
                    info=zipfile.ZipInfo(name)
                    info.create_system=3
                    info.external_attr=(stat.S_IFLNK|0o777)<<16
                    target.writestr(info,b"/etc/passwd")
                else:target.writestr(name,source.read(name))
        with self.assertRaises(VerificationError):verify(malicious)
        extra=self.changed_archive(change_entries=lambda e:e.__setitem__("../../outside",b"no"))
        with self.assertRaises(VerificationError):verify(extra)

    def test_malformed_unsupported_and_ambiguous_manifests_rejected(self):
        self.capture()
        for change in (lambda m:m.update(version=2),lambda m:m.update(version=True),
                       lambda m:m.update(consistency="operator-attested"),lambda m:m.update(extra="unknown"),
                       lambda m:m["files"].append(m["files"][0]),lambda m:m["files"][0].update(size=-1)):
            with self.assertRaises(VerificationError):verify(self.changed_archive(change_manifest=change))
        for raw in (b"not json", b'{"version":1,"version":2}', b'{"bad":NaN}'):
            with self.assertRaises(VerificationError):
                verify(self.changed_archive(change_entries=lambda e:e.__setitem__("manifest.json",raw)))
        with self.assertRaises(VerificationError):verify(self.changed_archive(compression=zipfile.ZIP_DEFLATED))

    def test_database_foreign_key_failure_is_rejected(self):
        with closing(sqlite3.connect(self.root/"application.sqlite3")) as conn:
            conn.execute("CREATE TABLE parent(id INTEGER PRIMARY KEY)")
            conn.execute("CREATE TABLE child(parent_id INTEGER REFERENCES parent(id))")
            conn.execute("INSERT INTO child VALUES(7)")
            conn.commit()
        with self.assertRaises(VerificationError):self.capture()
        self.assertFalse(self.archive.exists())

    def test_private_runtime_directory_is_required(self):
        self.root.chmod(0o755)
        with self.assertRaises(ProofError):self.capture()
        self.directory.chmod(0o755)
        with self.assertRaises(ProofError):restore(self.archive,self.directory/"new")

    def test_cli_real_acceptance_json_and_meaningful_exit_codes(self):
        root=self.directory/"cli-app"
        config=self.directory/"cli.json"
        archive=self.directory/"cli.zip"
        target=self.directory/"cli-restored"
        commands=[("sample-init",root,config),("backup",root,config,archive),("verify",archive),
                  ("drill",archive),("restore",archive,target),("sample-add",target,"new-002","new work")]
        for args in commands:
            result=self.cli(*args)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertTrue(json.loads(result.stdout)["ok"])
        self.assertEqual(self.cli("restore",archive,target).returncode,5)
        self.assertEqual(self.cli("backup",root,config,archive).returncode,5)
        archive.write_bytes(b"invalid ZIP")
        self.assertEqual(self.cli("verify",archive).returncode,4)
        self.assertEqual(self.cli("verify",self.directory/"missing.zip").returncode,6)
        bad=copy.deepcopy(self.config);bad["version"]=2
        config.write_bytes(json_bytes(bad))
        self.assertEqual(self.cli("backup",root,config,self.directory/"bad.zip").returncode,2)
        writer=self.child("writer",self.root)
        writer.kill();writer.communicate(timeout=5)
        self.assertEqual(self.cli("backup",self.root,self.config_file,self.directory/"dirty.zip").returncode,3)

    def test_real_demo_checks_application_rows_and_artifacts(self):
        result=run_demo(self.directory/"demo")
        self.assertTrue(result["historical_record_verified"])
        self.assertEqual(result["application_rows_after_new_work"],2)
        self.assertTrue(result["unselected_configuration_excluded"])


if __name__=="__main__":unittest.main()
