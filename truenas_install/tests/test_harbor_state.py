import os
import json
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from truenas_install.harbor_state import (
    ALLOWED_LINKS, ENABLEMENT_PATHS, HF_REPOSITORIES, NGINX_DROPIN, NGINX_HELPER, NGINX_WATCHER, OPS_ROOT, RECEIPT_PATH,
    HarborMigrationError, HarborStateMigration, inventory, prepare_harbor_migration,
    require_quiescent, validate_mounts, validate_roots,
)


def mounts(root, dataset):
    return [SimpleNamespace(mountpoint=str(root / child), mount_source=dataset + ("/" + child if child else ""),
                            fs_type="zfs") for child in ("", "etc", "var/lib", "data", "mnt", "usr")]


class HarborStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.old = Path(self.temp.name) / "old"
        self.new = Path(self.temp.name) / "new"
        self.old.mkdir()
        self.new.mkdir()
        self.old_ds, self.new_ds = "boot-pool/ROOT/old", "boot-pool/ROOT/new"
        self.paths = ("var/lib/harboros-beacon", "etc/default/harboros-im-gate")
        self.mounts = mounts(self.old, self.old_ds) + mounts(self.new, self.new_ds)
        self.put(self.old, "var/lib/harboros-beacon/admin-console.json", "private config")
        self.put(self.old, "var/lib/harboros-beacon/admin-console.document-turns/turn/document.pdf", "pdf bytes")
        self.put(self.old, "var/lib/harboros-beacon/admin-console.image-turns/turn/photo.jpg", "image bytes")
        self.put(self.old, "var/lib/harboros-beacon/task-api-conversations.json", "conversation bytes")
        self.put(self.old, "etc/default/harboros-im-gate", "test credential never logged")
        self.put(self.old, "data/sessions/_inbound.json", "delivery ledger")
        # Native installer clones data before the explicit Harbor migration.
        shutil.copytree(self.old / "data", self.new / "data")

    def put(self, root, relative, text):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def copy_command(self, command):
        self.assertEqual(command[0], "rsync")
        source, target = Path(command[-2]), Path(command[-1])
        if source.is_dir():
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(source, target, symlinks=True)
        else:
            shutil.copy2(source, target, follow_symlinks=False)
        return SimpleNamespace(stdout="")

    def migration(self, run=None):
        return HarborStateMigration(self.old, self.old_ds, self.paths, {}, run or self.copy_command, self.mounts)

    def test_copy_preserves_turns_sessions_credentials_and_old_be(self):
        before = inventory(self.old, self.paths + ("data/sessions",))
        migration = self.migration()
        migration.copy(self.new, self.new_ds, self.mounts)
        self.assertEqual(inventory(self.new, self.paths + ("data/sessions",)), before)
        self.assertEqual(inventory(self.old, self.paths + ("data/sessions",)), before)
        # Rollback discards the new BE only: the stopped source remains intact.
        shutil.rmtree(self.new)
        self.assertEqual(inventory(self.old, self.paths + ("data/sessions",)), before)

    def test_state_change_during_copy_fails_without_touching_original(self):
        def mutate(command):
            self.copy_command(command)
            self.put(self.old, "var/lib/harboros-beacon/task-api-conversations.json", "new turn")
        with self.assertRaisesRegex(HarborMigrationError, "Source Harbor state changed"):
            self.migration(mutate).copy(self.new, self.new_ds, self.mounts)
        self.assertEqual((self.old / "var/lib/harboros-beacon/task-api-conversations.json").read_text(), "new turn")

    def test_missing_or_corrupt_session_clone_fails(self):
        self.put(self.new, "data/sessions/_inbound.json", "stale ledger")
        with self.assertRaisesRegex(HarborMigrationError, "does not match"):
            self.migration().copy(self.new, self.new_ds, self.mounts)

    def test_destination_write_after_copy_fails(self):
        migration = self.migration()
        migration.copy(self.new, self.new_ds, self.mounts)
        self.put(self.new, "var/lib/harboros-beacon/admin-console.json", "unexpected config")
        with self.assertRaisesRegex(HarborMigrationError, "does not match"):
            migration.verify(self.mounts, data_permissions_restricted=False)

    @unittest.skipIf(os.name == "nt", "POSIX modes are checked on the Linux build host")
    def test_native_session_permission_tightening_is_expected(self):
        migration = self.migration()
        migration.copy(self.new, self.new_ds, self.mounts)
        for path in (self.new / "data/sessions", *list((self.new / "data/sessions").rglob("*"))):
            path.chmod(stat.S_IMODE(path.stat().st_mode) & 0o700)
        migration.verify(self.mounts)

    def test_wrong_be_identity_and_nested_mount_fail_before_copy(self):
        wrong = mounts(self.new, self.old_ds)
        with self.assertRaisesRegex(HarborMigrationError, "declared boot environment"):
            self.migration().copy(self.new, self.new_ds, mounts(self.old, self.old_ds) + wrong)
        nested = SimpleNamespace(mountpoint=str(self.old / self.paths[0] / "remote"),
                                 mount_source="tank/shared", fs_type="zfs")
        with self.assertRaisesRegex(HarborMigrationError, "Unexpected mount"):
            validate_mounts(self.old, self.old_ds, self.paths, self.mounts + [nested])

    def test_identical_and_nested_roots_are_rejected(self):
        for target in (self.old, self.old / "nested"):
            with self.assertRaises(HarborMigrationError):
                validate_roots(self.old, target)
        with self.assertRaisesRegex(HarborMigrationError, "datasets are identical"):
            self.migration().copy(self.new, self.old_ds, self.mounts)

    def test_active_service_blocks_migration(self):
        with self.assertRaisesRegex(HarborMigrationError, "Stop Harbor"):
            require_quiescent(lambda cmd: SimpleNamespace(stdout="ActiveState=active\nMainPID=10\nControlPID=0\n"))
        with self.assertRaisesRegex(HarborMigrationError, "Stop Harbor"):
            require_quiescent(lambda cmd: SimpleNamespace(stdout="ActiveState=inactive\nMainPID=10\nControlPID=0\n"))

        def stopped_unit(cmd):
            # Path units do not expose Service MainPID/ControlPID properties.
            return SimpleNamespace(stdout="ActiveState=inactive\n" +
                                   ("MainPID=0\nControlPID=0\n" if cmd[2].endswith(".service") else ""))
        require_quiescent(stopped_unit)

    def symlink(self, target, link, directory=False):
        try:
            link.symlink_to(target, target_is_directory=directory)
        except OSError as exc:
            self.skipTest(f"Symlink creation unavailable: {exc.errno}")

    def test_unreviewed_source_and_parent_links_rejected(self):
        link = self.old / "var/lib/harboros-beacon/unreviewed"
        self.symlink(self.new, link, directory=True)
        with self.assertRaisesRegex(HarborMigrationError, "Unreviewed symlink"):
            self.migration()
        with self.assertRaisesRegex(HarborMigrationError, "Symlink in"):
            inventory(self.old, ("var/lib/harboros-beacon/unreviewed/anything",))

    def test_approved_link_is_preserved_without_reading_target(self):
        link = self.old / "etc/default/approved"
        self.symlink("/external/never-read", link)
        snapshot = inventory(self.old, ("etc/default/approved",),
                             {"etc/default/approved": ("/external/never-read",)})
        self.assertEqual(snapshot["etc/default/approved"].kind, "link")

    def test_destination_parent_link_rejected_without_external_write(self):
        (self.new / "etc").mkdir()
        self.symlink(self.old / "etc/default", self.new / "etc/default", directory=True)
        before = inventory(self.old, self.paths)
        with self.assertRaisesRegex(HarborMigrationError, "Symlink in"):
            self.migration().copy(self.new, self.new_ds, self.mounts)
        self.assertEqual(before, inventory(self.old, self.paths))

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO requires POSIX")
    def test_fifo_is_rejected_without_opening(self):
        os.mkfifo(self.old / "var/lib/harboros-beacon/fifo")
        with self.assertRaisesRegex(HarborMigrationError, "Special file"):
            self.migration()

    def test_fresh_install_and_non_harbor_upgrade_are_noops(self):
        self.assertIsNone(prepare_harbor_migration(None, None, None, []))
        self.assertIsNone(prepare_harbor_migration(self.new, self.new_ds, None, []))

    @unittest.skipIf(os.name == "nt", "POSIX permissions and rsync required")
    @unittest.skipUnless(shutil.which("rsync"), "rsync required")
    def test_real_rsync_preserves_modes_and_enablement(self):
        unit = "etc/systemd/system/harboros-beacon.service"
        link = "etc/systemd/system/multi-user.target.wants/harboros-beacon.service"
        self.put(self.old, unit, "[Service]\nExecStart=/usr/bin/true\n")
        (self.old / link).parent.mkdir(parents=True)
        (self.old / link).symlink_to("/" + unit)
        (self.old / self.paths[0] / "admin-console.json").chmod(0o600)
        self.paths += (unit,) + ENABLEMENT_PATHS
        migration = HarborStateMigration(self.old, self.old_ds, self.paths, ALLOWED_LINKS,
                                         lambda cmd: subprocess.run(cmd, check=True, capture_output=True), self.mounts)
        migration.copy(self.new, self.new_ds, self.mounts)
        self.assertEqual(os.readlink(self.new / link), "/" + unit)
        self.assertEqual(stat.S_IMODE((self.new / self.paths[0] / "admin-console.json").stat().st_mode), 0o600)
        for path in (self.new / "data/sessions", *list((self.new / "data/sessions").rglob("*"))):
            path.chmod(0o700 if path.is_dir() else 0o600)
        migration.write_receipt(self.mounts)
        receipt = json.loads((self.new / RECEIPT_PATH).read_text())
        self.assertEqual(receipt["source_dataset"], self.old_ds)
        self.assertEqual(receipt["target_dataset"], self.new_ds)
        self.assertNotIn("test credential never logged", json.dumps(receipt))

    def test_invalid_and_traversing_scope_rejected(self):
        for path in ("/etc/shadow", "../old", "etc/../shadow", "etc/./shadow", "etc//shadow"):
            with self.subTest(path=path), self.assertRaises(HarborMigrationError):
                inventory(self.old, (path,))

    def test_only_reviewed_boot_mnt_root_is_copied(self):
        path = "mnt/software/harborbeacon-agent-ci"
        self.put(self.old, path + "/rag-test-docs/sample.pdf", "knowledge document")
        self.put(self.old, path + "/model-store/model.bin", "model bytes")
        self.put(self.old, "mnt/software/unrelated/private", "unrelated")
        self.paths += (path,)
        self.migration().copy(self.new, self.new_ds, self.mounts)
        self.assertTrue((self.new / path / "rag-test-docs/sample.pdf").is_file())
        self.assertFalse((self.new / "mnt/software/unrelated").exists())
        wrong = [SimpleNamespace(mountpoint=mount.mountpoint, fs_type=mount.fs_type,
                                 mount_source="tank/software" if mount.mountpoint == str(self.old / "mnt")
                                 else mount.mount_source) for mount in self.mounts]
        with self.assertRaisesRegex(HarborMigrationError, "declared boot environment"):
            validate_mounts(self.old, self.old_ds, self.paths, wrong)
        hidden = SimpleNamespace(mountpoint=str(self.old / "mnt/software"), mount_source="tank/software", fs_type="zfs")
        with self.assertRaisesRegex(HarborMigrationError, "hidden by another mount"):
            validate_mounts(self.old, self.old_ds, self.paths, self.mounts + [hidden])

    def hf_fixture(self, filename="config.json"):
        repository = list(HF_REPOSITORIES)[1]
        blob = repository + "/blobs/" + "b" * 40
        relative = repository + "/snapshots/" + "a" * 40 + "/" + filename
        self.put(self.old, blob, "model cache content")
        (self.old / relative).parent.mkdir(parents=True, exist_ok=True)
        target = ("../../../" if filename.startswith("onnx/") else "../../") + "blobs/" + "b" * 40
        self.symlink(target, self.old / relative)
        return blob, relative

    def test_reviewed_hf_links_preserve_target_hash_without_following_links(self):
        blob, relative = self.hf_fixture()
        _, nested = self.hf_fixture("onnx/model.onnx")
        root = "mnt/software/harborbeacon-agent-ci"
        self.paths += (root,)
        self.migration().copy(self.new, self.new_ds, self.mounts)
        expected = inventory(self.old, (root,))
        self.assertEqual(inventory(self.new, (root,)), expected)
        self.assertEqual(expected[relative].kind, "link")
        self.assertEqual(expected[nested].kind, "link")
        self.assertEqual(expected[blob].kind, "file")
        with self.assertRaisesRegex(HarborMigrationError, "outside the copied"):
            inventory(self.old, (relative,))

    def test_hf_escape_broken_chain_and_directory_target_rejected(self):
        blob, relative = self.hf_fixture()
        root = "mnt/software/harborbeacon-agent-ci"
        link, target = self.old / relative, self.old / blob
        link.unlink()
        self.symlink("../../../../etc/shadow", link)
        with self.assertRaisesRegex(HarborMigrationError, "Unexpected model-cache"):
            inventory(self.old, (root,))
        link.unlink()
        self.symlink("../../blobs/" + "b" * 40, link)
        target.unlink()
        with self.assertRaisesRegex(HarborMigrationError, "Broken internal"):
            inventory(self.old, (root,))
        self.symlink("/etc/shadow", target)
        with self.assertRaises(HarborMigrationError):
            inventory(self.old, (root,))
        target.unlink()
        target.mkdir()
        with self.assertRaisesRegex(HarborMigrationError, "not a regular"):
            inventory(self.old, (root,))

    def test_app_absolute_current_resolves_in_new_be_not_running_host(self):
        release = OPS_ROOT + "/releases/harborops-test"
        self.put(self.old, release + "/app.bin", "app release")
        self.symlink("/" + release, self.old / OPS_ROOT / "current", directory=True)
        self.paths += (OPS_ROOT,)
        migration = self.migration()
        migration.copy(self.new, self.new_ds, self.mounts)
        self.assertEqual(os.readlink(self.new / OPS_ROOT / "current"), "/" + release)
        self.put(self.new, release + "/app.bin", "tampered new release")
        with self.assertRaisesRegex(HarborMigrationError, "does not match"):
            migration.verify(self.mounts, data_permissions_restricted=False)

    def test_app_current_escape_and_release_link_rejected(self):
        release = OPS_ROOT + "/releases/harborops-test"
        self.put(self.old, release + "/app.bin", "app release")
        current = self.old / OPS_ROOT / "current"
        self.symlink("/tmp/outside", current, directory=True)
        with self.assertRaisesRegex(HarborMigrationError, "Unexpected app release"):
            inventory(self.old, (OPS_ROOT,))
        current.unlink()
        self.symlink("/" + release, current, directory=True)
        shutil.rmtree(self.old / release)
        self.symlink(self.new, self.old / release, directory=True)
        with self.assertRaisesRegex(HarborMigrationError, "not a regular"):
            inventory(self.old, (OPS_ROOT,))

    def test_pending_other_bootfs_is_not_overwritten(self):
        with patch("truenas_install.harbor_state.Path.resolve", return_value=Path("/")):
            with self.assertRaisesRegex(HarborMigrationError, "differs from the selected bootfs"):
                prepare_harbor_migration(self.old, self.old_ds, None, self.mounts, bootfs=self.new_ds)

    @unittest.skipUnless(os.name == "posix" and getattr(os, "geteuid", lambda: -1)() == 0,
                         "New helper must be installed as root on Linux")
    def test_compatibility_transform_preserves_source_and_records_new_helper(self):
        from truenas_install.harbor_demo_nginx import MODE, RELEASE, RELEASE_ROOT
        original = ("[Unit]\nAfter=nginx.service harbor-gate-webui-edge.service\n[Service]\nType=oneshot\n"
                    "ExecStart=/usr/bin/python3 " + RELEASE_ROOT +
                    "/deploy_photo_demo.py reconcile-nginx --release " + RELEASE + "\n")
        self.put(self.old, NGINX_WATCHER, original)
        self.paths += (NGINX_WATCHER,)
        migration = self.migration()
        migration.copy(self.new, self.new_ds, self.mounts)
        migration.install_nginx_compatibility(self.mounts)
        self.assertEqual((self.old / NGINX_WATCHER).read_text(), original)
        self.assertIn(MODE, (self.new / NGINX_WATCHER).read_text())
        self.assertTrue((self.new / NGINX_HELPER).is_file())
        self.assertIn("--render-only", (self.new / NGINX_DROPIN).read_text())
        self.assertEqual(stat.S_IMODE((self.new / NGINX_DROPIN).stat().st_mode), 0o644)
        self.assertEqual(migration.compatibility_mode, MODE)
        self.assertFalse((self.old / NGINX_DROPIN).exists())
        self.assertFalse((self.old / NGINX_HELPER).exists())
        self.assertEqual((self.new / NGINX_HELPER).stat().st_uid, 0)
        self.assertEqual(stat.S_IMODE((self.new / NGINX_HELPER).stat().st_mode), 0o755)
        self.put(self.new, NGINX_HELPER, "unexpected helper modification")
        with self.assertRaisesRegex(HarborMigrationError, "does not match"):
            migration.verify(self.mounts, data_permissions_restricted=False)


if __name__ == "__main__":
    unittest.main()
