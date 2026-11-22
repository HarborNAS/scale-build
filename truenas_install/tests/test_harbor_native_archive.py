import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest

from truenas_install.harbor_native import BACKUP, CORE, ENV_FILES
from truenas_install.harbor_state import (
    ALLOWED_LINKS, NATIVE_MODE, HarborMigrationError, HarborStateMigration, inventory,
)


@unittest.skipUnless(os.name == "posix" and getattr(os, "geteuid", lambda: -1)() == 0
                     and all(shutil.which(tool) for tool in ("mount", "umount", "rsync")),
                     "Disposable privileged Linux container with rsync is required")
class NativeArchiveOwnershipTests(unittest.TestCase):
    def test_real_cross_filesystem_archive_preserves_numeric_metadata_and_source(self):
        temporary = tempfile.TemporaryDirectory(prefix="harbor-native-archive-test-")
        self.addCleanup(temporary.cleanup)
        old, new = [Path(temporary.name) / name for name in ("old", "new")]
        old.mkdir()
        varlib = new / "var/lib"
        varlib.mkdir(parents=True)
        mounted = subprocess.run(["mount", "-t", "tmpfs", "-o", "size=8m", "tmpfs", str(varlib)],
                                 capture_output=True, text=True)
        if mounted.returncode:
            self.skipTest("An isolated tmpfs mount requires CAP_SYS_ADMIN")
        self.addCleanup(lambda: subprocess.run(["umount", str(varlib)], check=True,
                                               capture_output=True, text=True))

        def put(root, name, content, uid=0, gid=0, mode=0o644):
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            os.chown(path, uid, gid)
            path.chmod(mode)
            return path

        paths = ["var/lib/harboros-beacon"]
        put(old, paths[0] + "/admin-console.json", "{}", mode=0o600)
        for name in ENV_FILES:
            put(old, name, "", mode=0o600)
            paths.append(name)
        for unit in CORE:
            relative = "etc/systemd/system/" + unit
            put(old, relative, "[Service]\nExecStart=/old-demo-binary\n", 1001, 1001)
            link = "etc/systemd/system/multi-user.target.wants/" + unit
            (old / link).parent.mkdir(parents=True, exist_ok=True)
            (old / link).symlink_to("/" + relative)
            os.lchown(old / link, 1006, 1007)
            paths.extend((relative, link))
        dropin = "etc/systemd/system/" + CORE[0] + ".d"
        put(old, dropin + "/20-demo.conf", "[Unit]\nDescription=old override\n", 1004, 1005, 0o640)
        os.chown(old / dropin, 1002, 1003)
        (old / dropin).chmod(0o750)
        paths.append(dropin)
        before = inventory(old, paths, ALLOWED_LINKS)

        put(new, "etc/passwd", "harborbeacon:x:132:139::/var/lib/harboros-beacon:/usr/sbin/nologin\n")
        for unit, profile in zip(CORE, ("HARBOR_PRODUCT_PROFILE=nexus-amd64", "HARBORGATE_RUNTIME_PROFILE=nexus")):
            put(new, "usr/lib/systemd/system/" + unit,
                "[Service]\nEnvironment=" + profile + "\nExecStart=/usr/bin/" + unit.removesuffix(".service") + "\n")
        (varlib / "harboros-im-gate").mkdir(mode=0o700)
        self.assertNotEqual((new / "etc").stat().st_dev, varlib.stat().st_dev,
                            "The fixture must exercise real EXDEV, not a mocked move")
        # The scope table is the installer's ZFS identity fixture. The real
        # backing filesystems here are deliberately root filesystem + tmpfs.
        mounts = [SimpleNamespace(mountpoint=str(root / child), fs_type="zfs",
                                  mount_source=dataset + ("/" + child if child else ""))
                  for root, dataset in ((old, "boot-pool/ROOT/old"), (new, "boot-pool/ROOT/new"))
                  for child in ("", "etc", "var/lib", "data", "mnt", "usr")]
        migration = HarborStateMigration(
            old, "boot-pool/ROOT/old", paths, ALLOWED_LINKS,
            lambda command: subprocess.run(command, check=True, capture_output=True, text=True),
            mounts, service_migration=NATIVE_MODE,
        )
        migration.copy(new, "boot-pool/ROOT/new", mounts)
        self.assertEqual(inventory(old, paths, ALLOWED_LINKS), before)
        archived = inventory(new, (BACKUP,), migration.allowed_links)
        for relative, entry in before.items():
            if relative.startswith("etc/"):
                self.assertEqual(archived[BACKUP + "/" + relative], entry, relative)
        # Restoring metadata is required; the existing strict verifier remains
        # responsible for rejecting later target ownership corruption.
        target = new / BACKUP / "etc/systemd/system" / CORE[0]
        os.chown(target, 0, 0)
        with self.assertRaisesRegex(HarborMigrationError, "does not match"):
            migration.verify(mounts, data_permissions_restricted=False)
