from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from truenas_install.harbor_native import _old_environment
from truenas_install.harbor_state import HarborMigrationError


class NativeEnvironmentTests(unittest.TestCase):
    unit = "harboros-im-gate.service"

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.migration = SimpleNamespace(old_root=self.root, before={})

    def put(self, name, text, tracked=True):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        if tracked:
            self.migration.before[name] = None
        return path

    def test_packaged_native_unit_retains_workspace_and_platform_environment(self):
        self.put("usr/lib/systemd/system/" + self.unit,
                 "[Service]\nEnvironment=HARBORGATE_RUNTIME_PROFILE=nexus\n"
                 "EnvironmentFile=-/etc/default/harboros-im-gate\n", tracked=False)
        self.put("etc/default/harboros-im-gate",
                 "HARBOR_WORKSPACE_ID=existing-workspace\nFEISHU_MAIL_ENABLED=1\nFEISHU_APP_ID=test-app\n")
        result = _old_environment(self.migration, self.unit)
        self.assertEqual(result["HARBOR_WORKSPACE_ID"], "existing-workspace")
        self.assertEqual(result["FEISHU_MAIL_ENABLED"], "1")
        self.assertEqual(result["FEISHU_APP_ID"], "test-app")
        self.assertEqual(result["HARBORGATE_RUNTIME_PROFILE"], "nexus")

    def test_legacy_etc_unit_overrides_packaged_unit_and_keeps_dropins(self):
        self.put("usr/lib/systemd/system/" + self.unit, "[Service]\nEnvironment=VALUE=vendor\n", tracked=False)
        self.put("etc/systemd/system/" + self.unit,
                 "[Service]\nEnvironment=VALUE=legacy\nEnvironmentFile=/etc/default/harboros-beacon-gate\n")
        self.put("etc/default/harboros-beacon-gate", "LEGACY_VALUE=preserved\n")
        self.put("etc/systemd/system/" + self.unit + ".d/99-local.conf", "[Service]\nEnvironment=VALUE=local\n")
        self.assertEqual(_old_environment(self.migration, self.unit),
                         {"VALUE": "local", "LEGACY_VALUE": "preserved"})

    def test_native_dropins_honor_basename_precedence_and_environment_file_reset(self):
        self.put("usr/lib/systemd/system/" + self.unit,
                 "[Service]\nEnvironmentFile=/etc/default/harboros-beacon-gate\n", tracked=False)
        self.put("usr/lib/systemd/system/" + self.unit + ".d/20-profile.conf",
                 "[Service]\nEnvironment=VALUE=vendor\nEnvironment=VENDOR_ONLY=ignored\n", tracked=False)
        self.put("etc/systemd/system/" + self.unit + ".d/20-profile.conf",
                 "[Service]\nEnvironment=VALUE=local\nEnvironmentFile=\n"
                 "EnvironmentFile=-/etc/default/harboros-im-gate\n")
        self.put("etc/default/harboros-beacon-gate", "SHARED_VALUE=ignored\n")
        self.put("etc/default/harboros-im-gate", "HARBOR_WORKSPACE_ID=preserved\n")
        self.assertEqual(_old_environment(self.migration, self.unit),
                         {"VALUE": "local", "HARBOR_WORKSPACE_ID": "preserved"})

    def test_native_unit_rejects_unreviewed_environment_file(self):
        self.put("usr/lib/systemd/system/" + self.unit,
                 "[Service]\nEnvironmentFile=/unreviewed/credentials\n", tracked=False)
        with self.assertRaisesRegex(HarborMigrationError, "Unreviewed"):
            _old_environment(self.migration, self.unit)

    def test_native_vendor_dropin_is_read_without_local_override(self):
        self.put("usr/lib/systemd/system/" + self.unit, "[Service]\n", tracked=False)
        self.put("usr/lib/systemd/system/" + self.unit + ".d/20-profile.conf",
                 "[Service]\nEnvironment=VENDOR_VALUE=preserved\n", tracked=False)
        self.assertEqual(_old_environment(self.migration, self.unit), {"VENDOR_VALUE": "preserved"})

    def test_packaged_native_unit_symlink_is_rejected_without_reading_target(self):
        target = self.put("unreviewed-unit", "[Service]\nEnvironment=VALUE=outside\n", tracked=False)
        path = self.root / "usr/lib/systemd/system" / self.unit
        path.parent.mkdir(parents=True)
        try:
            path.symlink_to(target)
        except OSError:
            self.skipTest("Symlink creation requires local privilege")
        with self.assertRaisesRegex(HarborMigrationError, "Unsafe"):
            _old_environment(self.migration, self.unit)
