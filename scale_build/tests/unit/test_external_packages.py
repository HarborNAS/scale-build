import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from scale_build.exceptions import CallError
from scale_build.image import update


class ExternalPackageTests(unittest.TestCase):
    def test_locked_gate_is_available_before_distro_webui_dependencies(self):
        locked = {'harboros-beacon', 'harboros-im-gate', 'harborlink', 'harboros-ffmpeg'}
        manifest = {
            'base-packages': [{'name': 'truenas', 'install_recommends': False}],
            'additional-packages': [],
            'external-packages': {name: {} for name in locked | {'truesearch'}},
        }
        lock = {'packages': {name: {} for name in locked - {'harboros-ffmpeg'}},
                'runtime': {'packages': {'harboros-ffmpeg': {}}}}
        installed = set()

        def install_external(package_names=None):
            installed.update(manifest['external-packages'] if package_names is None else package_names)

        def install_distro(command):
            if command[:2] == ['apt', 'install'] and 'truenas' in command:
                if 'harboros-im-gate' not in installed:
                    raise CallError('truenas-webui Depends harboros-im-gate but it is not installable')
                self.assertNotIn('truesearch', installed)

        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / 'etc/apt').mkdir(parents=True)
            with (
                patch.object(update, 'CHROOT_BASEDIR', folder),
                patch.object(update, 'get_manifest', return_value=manifest),
                patch.object(update, 'release_lock', return_value=lock),
                patch.object(update, 'install_external_packages', side_effect=install_external),
                patch.object(update, 'run_in_chroot', side_effect=install_distro),
                patch.object(update, 'custom_rootfs_setup'),
                patch.object(update, 'clean_rootfs'),
                patch.object(update, 'build_extensions'),
                patch.object(update, 'get_apt_sources', return_value=[]),
                patch.object(update, 'post_rootfs_setup'),
            ):
                update.install_rootfs_packages_impl()
        self.assertEqual(installed, locked)

    def test_all_verified_packages_install_in_one_offline_transaction(self):
        packages = {
            name: {'deb_version': '1.1', 'url_template': 'https://update.harboros.ai/deb/' +
                   name + '_1.1_linux_amd64.deb', 'sha256': 'a' * 64}
            for name in ('harboros-beacon', 'harboros-im-gate', 'harborlink')
        }
        packages['harborlink']['post_install_commands'] = [['systemctl', 'disable', 'example']]
        selected_names = set(packages)
        packages['late-extra'] = dict(packages['harboros-beacon'])
        staged = []
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'tmp').mkdir()

            def stage(name, url, filename, **options):
                self.assertTrue(options['stage_only'])
                self.assertEqual(options['expected_sha256'], 'a' * 64)
                self.assertFalse(install.called)
                (root / 'tmp' / filename).write_bytes(b'verified package')
                staged.append('/tmp/' + filename)
                return staged[-1]
            with (
                patch.object(update, 'CHROOT_BASEDIR', folder),
                patch.object(update, 'get_manifest', return_value={'external-packages': packages}),
                patch.object(update, 'get_debian_arch', return_value='amd64'),
                patch.object(update, 'download_and_install_deb_package', side_effect=stage),
                patch.object(update, 'run_in_chroot') as install,
            ):
                update.install_external_packages(package_names=selected_names)
                self.assertEqual(len(staged), len(selected_names))
                self.assertEqual(
                    install.call_args_list[0].args[0], [
                        'env', 'SYSTEMD_OFFLINE=1', 'apt-get', 'install', '-y', *staged])
                self.assertEqual(
                    install.call_args_list[1].args[0], [
                        'env', 'SYSTEMD_OFFLINE=1', 'systemctl', 'disable', 'example'])
            self.assertEqual(list((root / 'tmp').iterdir()), [])

    def test_hash_failure_runs_no_maintainer_script_and_cleans_staged_files(self):
        payload = b'actual downloaded package bytes'
        response = Mock()
        response.iter_content.return_value = [payload]
        packages = {
            'good': {
                'deb_version': '1', 'url_template': 'https://update.harboros.ai/deb/good_1_linux_amd64.deb',
                'sha256': hashlib.sha256(payload).hexdigest(),
            },
            'bad': {
                'deb_version': '1', 'url_template': 'https://update.harboros.ai/deb/bad_1_linux_amd64.deb',
                'sha256': '0' * 64,
            },
        }
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'tmp').mkdir()
            with (
                patch.object(update, 'CHROOT_BASEDIR', folder),
                patch.object(update, 'get_manifest', return_value={'external-packages': packages}),
                patch.object(update.requests, 'get', return_value=response),
                patch.object(update, 'run_in_chroot') as install,
            ):
                with self.assertRaisesRegex(CallError, 'SHA256'):
                    update.install_external_packages()
                install.assert_not_called()
            self.assertEqual(list((root / 'tmp').iterdir()), [])


if __name__ == '__main__':
    unittest.main()
