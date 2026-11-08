from datetime import datetime, timezone
import json
import os
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest.mock import patch

from scale_build.exceptions import CallError
from scale_build.packages import build
from scale_build.product_profiles import source_date_epoch, validate_release_lock
from scale_build.tests.unit.test_product_profiles import fixture


class LockedEpochTests(unittest.TestCase):
    def test_formal_lock_requires_a_positive_integer_epoch(self):
        for value in (None, 0, -1, True, '1793836800'):
            lock = fixture()
            lock['source_date_epoch'] = value
            with self.assertRaisesRegex(CallError, 'positive integer'):
                validate_release_lock(lock)

    def test_supplied_epoch_must_match_without_silently_replacing_the_lock(self):
        lock = fixture()
        for value in ('', 'not-a-time', str(lock['source_date_epoch'] + 1)):
            with patch.dict(os.environ, {'SOURCE_DATE_EPOCH': value}):
                with self.assertRaisesRegex(CallError, 'differs from the immutable release lock'):
                    source_date_epoch(lock)
        with patch.dict(os.environ, {'SOURCE_DATE_EPOCH': str(lock['source_date_epoch'])}):
            self.assertEqual(source_date_epoch(lock), lock['source_date_epoch'])

    def test_config_and_generated_deb_version_ignore_wall_clock_for_the_same_lock(self):
        lock = fixture()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'lock.json'
            path.write_text(json.dumps(lock), encoding='utf-8')
            environment = {'HARBOROS_RELEASE_MANIFEST': str(path), 'HARBOROS_PRODUCT': 'nexus',
                           'HARBOROS_ARCHITECTURE': 'amd64', 'SOURCE_DATE_EPOCH': str(lock['source_date_epoch']),
                           'TRUENAS_VERSION': lock['system_version']}
            results = []
            with patch.dict(os.environ, environment):
                for now in (100, 2000000000):
                    with patch('time.time', return_value=now):
                        config = runpy.run_path(str(Path(__file__).parents[2] / 'config.py'))
                    results.append((config['BUILD_TIME'], config['VERSION'], config['BUILD_TIME_OBJ']))
                with patch('scale_build.packages.build.datetime', wraps=datetime) as clock:
                    clock.today.side_effect = AssertionError('Formal builds must not read wall clock for versions')
                    first = build.package_version_timestamp()
                    second = build.package_version_timestamp()
                builder = build.BuildPackageMixin()
                builder.secret_env = []
                with patch('scale_build.packages.build.get_secret_env', return_value={'PRIVATE': 'not recorded'}):
                    self.assertEqual(builder._get_chroot_env()['SOURCE_DATE_EPOCH'], str(lock['source_date_epoch']))
            self.assertEqual(results[0], results[1])
            self.assertEqual(results[0][0], lock['source_date_epoch'])
            self.assertEqual(results[0][1], lock['system_version'])
            self.assertEqual(results[0][2].tzinfo, timezone.utc)
            self.assertEqual(first, second)
            expected = datetime.fromtimestamp(lock['source_date_epoch'], timezone.utc).strftime('%Y%m%d%H%M%S')
            self.assertEqual(first, expected)

    def test_conflicting_system_version_is_rejected_for_formal_builds(self):
        lock = fixture()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'lock.json'
            path.write_text(json.dumps(lock), encoding='utf-8')
            with patch.dict(os.environ, {'HARBOROS_RELEASE_MANIFEST': str(path),
                                         'SOURCE_DATE_EPOCH': str(lock['source_date_epoch']),
                                         'TRUENAS_VERSION': '26.04.0-OTHER'}):
                with self.assertRaisesRegex(CallError, 'TRUENAS_VERSION differs'):
                    runpy.run_path(str(Path(__file__).parents[2] / 'config.py'))

    def test_unlocked_legacy_build_keeps_its_existing_wall_clock_behavior(self):
        with patch.dict(os.environ, {'HARBOROS_RELEASE_MANIFEST': '', 'SOURCE_DATE_EPOCH': 'bad-legacy-value',
                                     'TRUENAS_VERSION': ''}):
            with patch('time.time', return_value=1234):
                config = runpy.run_path(str(Path(__file__).parents[2] / 'config.py'))
            self.assertEqual(config['BUILD_TIME'], 1234)
            self.assertIsNone(config['FORMAL_SOURCE_DATE_EPOCH'])
            with patch('scale_build.packages.build.datetime') as clock:
                clock.today.return_value = datetime(2020, 2, 3, 4, 5, 6)
                self.assertEqual(build.package_version_timestamp(), '20200203040506')

    def test_each_workflow_stage_carries_the_reviewed_epoch(self):
        import yaml
        for filename in ('buildiso.yml', 'buildiso-test.yml'):
            workflow = yaml.safe_load((Path('.github/workflows') / filename).read_text(encoding='utf-8'))
            for step in workflow['jobs']['build-iso']['steps']:
                if 'make ' in step.get('run', ''):
                    self.assertIn('SOURCE_DATE_EPOCH', step['run'])
                    expected = '$' + '{{ steps.set-version.outputs.SOURCE_DATE_EPOCH }}'
                    self.assertEqual(step['env']['SOURCE_DATE_EPOCH'], expected)


if __name__ == '__main__':
    unittest.main()
