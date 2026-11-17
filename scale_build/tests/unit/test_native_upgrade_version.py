"""Check locked versions against the selected middleware's actual update policy.

Set HARBOROS_MIDDLEWARE_UPDATE_UTILS to the reviewed middleware update_/utils.py.
The comparison implementation is imported whole; no local copy or replacement
of can_update is used. Without that source this compatibility check is skipped.
"""
import importlib.util
import os
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

from scale_build.product_profiles import validate_release_lock
from scale_build.tests.unit.test_product_profiles import fixture


class NativeUpgradeVersionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = os.environ.get('HARBOROS_MIDDLEWARE_UPDATE_UTILS')
        if not path:
            raise unittest.SkipTest('Set HARBOROS_MIDDLEWARE_UPDATE_UTILS to run the real middleware version gate')
        source = Path(path).resolve(strict=True)
        spec = importlib.util.spec_from_file_location('locked_release_update_compatibility', source)
        module = importlib.util.module_from_spec(spec)
        # Only the unrelated upload-directory constant needs an environment
        # stand-in when this unit suite runs outside a middleware installation.
        middleware = types.ModuleType('middlewared')
        middleware.__path__ = []
        utils = types.ModuleType('middlewared.utils')
        utils.MIDDLEWARE_RUN_DIR = '/run/middleware'
        with patch.dict(sys.modules, {'middlewared': middleware, 'middlewared.utils': utils}):
            spec.loader.exec_module(module)
        cls.update_utils = module

    def test_master_accepts_standard_locked_version_without_policy_changes(self):
        lock = fixture()
        lock['system_version'] = '26.04.0-20261105-000000'
        validate_release_lock(lock)
        old = '26.04.0-MASTER-20261101-120000'
        self.assertTrue(self.update_utils.can_update(old, lock['system_version']))
        self.assertFalse(self.update_utils.can_update(old, '26.04.0-NEXUS-RC1'))

    def test_same_version_and_standard_downgrades_remain_rejected(self):
        current = '26.04.0-20261105-000000'
        self.assertFalse(self.update_utils.can_update(current, current))
        self.assertFalse(self.update_utils.can_update(current, '26.04.0-20261104-235959'))
        self.assertTrue(self.update_utils.can_update(current, '26.04.0-20261105-000001'))

    def test_selected_installed_version_accepts_the_reviewed_candidate(self):
        old = os.environ.get('HARBOROS_INSTALLED_SYSTEM_VERSION')
        candidate = os.environ.get('HARBOROS_CANDIDATE_SYSTEM_VERSION')
        if not old or not candidate:
            self.skipTest('Set installed and candidate system versions for the appliance-specific check')
        self.assertTrue(self.update_utils.can_update(old, candidate), f'{old} -> {candidate}')


if __name__ == '__main__':
    unittest.main()
