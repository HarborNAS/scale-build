import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from scale_build.exceptions import CallError
from scale_build.image import update


class ComponentCacheTests(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(folder)
        self.cache = self.root / 'cache'
        self.cache.mkdir()
        self.chroot = self.root / 'chroot'
        (self.chroot / 'tmp').mkdir(parents=True)
        self.filename = 'component_1_linux_amd64.deb'
        self.cached = self.cache / self.filename
        self.payload = b'verified component payload'
        self.digest = hashlib.sha256(self.payload).hexdigest()
        self.url = 'https://github.com/example/component/releases/download/v1/' + self.filename
        self.enterContext(patch.dict(os.environ, {'HARBOROS_COMPONENT_CACHE': str(self.cache)}))
        self.enterContext(patch.object(update, 'CHROOT_BASEDIR', str(self.chroot)))
        self.resolve = self.enterContext(patch.object(update, 'github_asset_request', return_value=(self.url, {})))
        response = Mock()
        response.iter_content.return_value = [self.payload]
        self.download = self.enterContext(patch.object(update.requests, 'get', return_value=response))
        self.install = self.enterContext(patch.object(update, 'run_in_chroot'))

    def stage(self, **options):
        args = {'expected_sha256': self.digest, 'stage_only': True}
        args.update(options)
        return update.download_and_install_deb_package('component', self.url, self.filename, **args)

    def assert_no_external_effects(self):
        self.resolve.assert_not_called()
        self.download.assert_not_called()
        self.install.assert_not_called()
        self.assertEqual(list((self.chroot / 'tmp').iterdir()), [])

    def test_cache_hit_still_checks_component_before_staging_without_network(self):
        self.cached.write_bytes(self.payload)
        pin = {'source_commit': 'a' * 40}

        def verify(path, name, supplied_pin):
            self.assertEqual(path.read_bytes(), self.payload)
            self.assertEqual(name, 'component')
            self.assertIs(supplied_pin, pin)
            self.assertEqual(list((self.chroot / 'tmp').iterdir()), [])

        with patch.object(update, 'verify_debian_component', side_effect=verify) as contract:
            self.assertEqual(self.stage(component_pin=pin), '/tmp/' + self.filename)
            contract.assert_called_once()
        self.resolve.assert_not_called()
        self.download.assert_not_called()
        self.install.assert_not_called()
        self.assertEqual((self.chroot / 'tmp' / self.filename).read_bytes(), self.payload)
        self.assertEqual(self.cached.read_bytes(), self.payload)

    def test_bad_cached_hash_fails_without_falling_back_to_network(self):
        self.cached.write_bytes(b'wrong bytes')
        with self.assertRaisesRegex(CallError, 'SHA256'):
            self.stage()
        self.assert_no_external_effects()

    def test_cached_component_contract_failure_does_not_stage_or_download(self):
        self.cached.write_bytes(self.payload)
        with patch.object(update, 'verify_debian_component', side_effect=CallError('Invalid component contract')):
            with self.assertRaisesRegex(CallError, 'component contract'):
                self.stage(component_pin={'source_commit': 'a' * 40})
        self.assert_no_external_effects()

    def test_missing_cache_entry_uses_existing_download_path(self):
        self.assertEqual(self.stage(), '/tmp/' + self.filename)
        self.resolve.assert_called_once_with(self.url, update.requests.get)
        self.download.assert_called_once()
        self.assertEqual((self.chroot / 'tmp' / self.filename).read_bytes(), self.payload)

    def test_unpinned_package_ignores_cache_and_uses_network(self):
        self.cached.write_bytes(b'untrusted unpinned bytes')
        self.stage(expected_sha256=None)
        self.download.assert_called_once()
        self.assertEqual((self.chroot / 'tmp' / self.filename).read_bytes(), self.payload)

    def test_directory_cache_entry_is_rejected(self):
        self.cached.mkdir()
        with self.assertRaisesRegex(CallError, 'regular file'):
            self.stage()
        self.assert_no_external_effects()

    def test_symlink_cache_entry_is_rejected_even_when_dangling(self):
        for present in (False, True):
            with self.subTest(target_exists=present):
                target = self.root / 'target.deb'
                if present:
                    target.write_bytes(self.payload)
                try:
                    self.cached.symlink_to(target)
                except OSError as error:
                    self.skipTest(f'Symlinks unavailable: {error}')
                try:
                    with self.assertRaisesRegex(CallError, 'regular file'):
                        self.stage()
                    self.assert_no_external_effects()
                finally:
                    self.cached.unlink()

    def test_invalid_hash_and_non_basename_cache_keys_are_rejected(self):
        self.cached.write_bytes(self.payload)
        with self.assertRaisesRegex(CallError, 'SHA256'):
            self.stage(expected_sha256='not-a-sha256')
        self.assert_no_external_effects()
        self.filename = '../outside.deb'
        with self.assertRaisesRegex(CallError, 'filename'):
            self.stage()
        self.assert_no_external_effects()


if __name__ == '__main__':
    unittest.main()
