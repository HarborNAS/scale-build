import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from scale_build import build_receipts as receipts
from scale_build.exceptions import CallError
from scale_build.image import update
from scale_build.packages.build import BuildPackageMixin
from scale_build.tests.unit.test_product_profiles import fixture


class BuildReceiptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.packages = self.root / 'packages'
        self.packages.mkdir()
        self.lock = fixture()
        self.manifest = {'sources': [
            {'name': 'truenas_webui'}, {'name': 'truenas', 'subpackages': [{'name': 'middlewared'}]},
        ]}
        self.fields = {}
        self.targets = {'truenas_webui': ('truenas_webui', 'truenas-webui', 'all'),
                        'truenas': ('truenas', 'truenas', 'amd64'),
                        'middlewared': ('truenas', 'middlewared', 'amd64')}
        self.build_inventory = [dict(package='dpkg', version='1.22', architecture='amd64',
                                     source='dpkg', depends='libc6 (>= 2.36)', pre_depends='')]
        self.inventory = []
        self.patcher = patch.object(receipts, 'package_fields', side_effect=lambda path: self.fields[Path(path).name])
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        for target, (source, package, architecture) in self.targets.items():
            filename = package + '.deb'
            (self.packages / filename).write_bytes(('actual bytes for ' + package).encode())
            self.fields[filename] = dict(package=package, version='26.04.0~build1', architecture=architecture,
                                         source=package, depends='libc6 (>= 2.36)', pre_depends='', built_using='')
            self.inventory.append({key: value for key, value in self.fields[filename].items() if key != 'built_using'})
            self.record(target)
        for package, pin in {**self.lock['packages'], **self.lock['runtime']['packages']}.items():
            self.inventory.append(dict(package=package, version=pin.get('version', pin.get('deb_version')),
                                       architecture=pin['architecture'], source=package,
                                       depends='libc6', pre_depends=''))

    def record(self, target):
        source, package, _ = self.targets[target]
        pin = self.lock['components'][{'truenas_webui': 'webui', 'truenas': 'middleware'}[source]]
        with patch.object(receipts, 'installed_packages', return_value=self.build_inventory), patch.object(
            receipts, 'build_tool_versions', return_value={'node': 'v24.14.1', 'yarn': '4.9.2'}
        ):
            receipts.record_source_build(target, source, {'sha': pin['commit'], 'url': pin['repository']},
                                         [package + '.deb'], self.packages, self.root, self.lock['system_version'],
                                         {'debhelper', 'nodejs'}, 'dpkg-src', self.lock['source_date_epoch'])

    def validate(self):
        return receipts.validate_source_builds(self.lock, self.manifest, self.packages, self.inventory)

    def test_actual_deb_fields_source_and_build_inventory_reach_rootfs_and_candidate_outputs(self):
        image = self.root / 'image'
        candidate = self.root / 'candidate'
        with patch.object(receipts, 'installed_packages', return_value=self.inventory):
            result = receipts.write_rootfs_build_receipt(image, self.packages, candidate, self.lock, self.manifest)
        installed, digest = receipts.read_checked_json(image / 'usr/share/harboros/build-receipt.json')
        output, output_digest = receipts.read_checked_json(candidate / 'build-receipt.json')
        self.assertEqual(installed, output)
        self.assertEqual(digest, output_digest)
        self.assertEqual(result['rootfs_packages'], self.inventory)
        self.assertEqual(set(result['locked_external_packages']),
                         {'harboros-beacon', 'harboros-im-gate', 'harborlink', 'harboros-ffmpeg'})
        self.assertEqual(len(result['source_builds']), 3)
        self.assertEqual(result['source_builds'][0]['build_dependencies'], self.build_inventory)
        self.assertEqual(result['source_builds'][0]['build_tools']['node'], 'v24.14.1')
        self.assertNotIn('secret_env', json.dumps(result))
        self.assertNotIn('SENTRY_AUTH_TOKEN', json.dumps(result))

    def test_missing_shared_middleware_target_receipt_fails_closed(self):
        (self.packages / ('middlewared' + receipts.SOURCE_RECEIPT_SUFFIX)).unlink()
        with self.assertRaisesRegex(CallError, 'Missing or invalid build receipt'):
            self.validate()

    def test_changed_deb_bytes_and_changed_receipt_bytes_are_rejected(self):
        package = self.packages / 'truenas-webui.deb'
        original = package.read_bytes()
        package.write_bytes(b'tampered package')
        with self.assertRaisesRegex(CallError, 'SHA256 mismatch'):
            self.validate()
        package.write_bytes(original)
        receipt = self.packages / ('truenas_webui' + receipts.SOURCE_RECEIPT_SUFFIX)
        receipt.write_text(receipt.read_text() + ' ', encoding='utf-8')
        with self.assertRaisesRegex(CallError, 'receipt checksum mismatch'):
            self.validate()

    def test_wrong_source_or_build_version_fails_even_with_recomputed_receipt_checksum(self):
        path = self.packages / ('truenas_webui' + receipts.SOURCE_RECEIPT_SUFFIX)
        original, _ = receipts.read_checked_json(path)
        for field in ('source', 'system_version', 'source_date_epoch'):
            changed = copy.deepcopy(original)
            if field == 'source':
                changed[field]['commit'] = 'b' * 40
            elif field == 'source_date_epoch':
                changed[field] = self.lock['source_date_epoch'] + 1
            else:
                changed[field] = '26.04.0-WRONG'
            receipts.write_checked_json(path, changed)
            with self.assertRaisesRegex(CallError, 'differs from the release lock'):
                self.validate()

    def test_wrong_artifact_architecture_and_installed_version_fail(self):
        self.fields['truenas-webui.deb']['architecture'] = 'riscv64'
        self.record('truenas_webui')
        with self.assertRaisesRegex(CallError, 'fields/architecture'):
            self.validate()
        self.fields['truenas-webui.deb']['architecture'] = 'all'
        self.record('truenas_webui')
        self.inventory[0]['version'] = 'old version'
        with self.assertRaisesRegex(CallError, 'Installed package differs'):
            self.validate()

    def test_missing_locked_runtime_package_is_not_hidden_by_the_input_lock(self):
        inventory = [row for row in self.inventory if row['package'] != 'harboros-ffmpeg']
        with patch.object(receipts, 'installed_packages', return_value=inventory):
            with self.assertRaisesRegex(CallError, 'locked external package: harboros-ffmpeg'):
                receipts.write_rootfs_build_receipt(self.root / 'image', self.packages, self.root / 'out',
                                                    self.lock, self.manifest)

    def test_locked_cached_target_rebuilds_when_epoch_or_receipt_is_missing(self):
        from unittest.mock import PropertyMock
        from scale_build.packages.package import Package
        package = Package('middlewared', 'branch', 'repo', source_name='truenas')
        (self.root / 'middlewared.hash').write_text('a' * 40)
        with patch('scale_build.packages.package.HASH_DIR', str(self.root)), patch(
            'scale_build.packages.package.PKG_DIR', str(self.packages)
        ), patch('scale_build.packages.package.release_lock', return_value=self.lock), patch.object(
            Package, 'source_hash', new_callable=PropertyMock, return_value='a' * 40
        ), patch('scale_build.packages.package.run', return_value=Mock(returncode=0)):
            self.assertFalse(package.hash_changed)
            self.lock['source_date_epoch'] += 1
            self.assertTrue(package.hash_changed)
            self.lock['source_date_epoch'] -= 1
            (self.packages / ('middlewared' + receipts.SOURCE_RECEIPT_SUFFIX)).unlink()
            self.assertTrue(package.hash_changed)

    def test_dependency_probe_reads_actual_dpkg_relationships_and_excludes_removed_configs(self):
        output = ('libc6\t2.41\tamd64\tinstall ok installed\tglibc\tlibgcc-s1 (>= 3.0)\t\n'
                  'old\t1\tamd64\tdeinstall ok config-files\told\t\t\n')
        with patch.object(receipts, 'run', return_value=Mock(stdout=output)) as command:
            result = receipts.installed_packages('/image')
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['depends'], 'libgcc-s1 (>= 3.0)')
        self.assertEqual(command.call_args.args[0][:4], ['chroot', '/image', 'dpkg-query', '-W'])

    def test_tool_probe_is_whitelisted_network_disabled_and_does_not_capture_environment(self):
        with patch.object(receipts, 'run', return_value=Mock(stdout='version 1\n', returncode=0)) as command:
            result = receipts.build_tool_versions('/image', 'dpkg-src')
        self.assertEqual(set(result), {'node', 'yarn', 'python3', 'dpkg-deb', 'gcc'})
        for call in command.call_args_list:
            self.assertIn('COREPACK_ENABLE_NETWORK=0', call.args[0][-1])
            self.assertFalse(call.kwargs['log'])
            self.assertFalse(call.kwargs['check'])


class AssemblyIntegrationTests(unittest.TestCase):
    def test_finished_debs_are_saved_before_receipt_and_overlay_cleanup(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'source'
            source.mkdir()
            (source / 'source.txt').write_text('source')
            overlay = root / 'overlay'
            overlay.mkdir()
            (overlay / 'middlewared.deb').write_bytes(b'built output')
            packages = root / 'packages'
            packages.mkdir()
            builder = BuildPackageMixin()
            builder.name = 'middlewared'
            builder.source_name = 'truenas'
            builder.source_path = str(source)
            builder.dpkg_overlay = str(overlay)
            builder.dpkg_overlay_packages_path = str(root / 'repo')
            builder.subdir = None
            builder.prebuildcmd = []
            builder.buildcmd = ['true']
            builder.generate_version = False
            builder.build_depends = {'python3'}
            builder.pkglist_hash_file_path = str(root / 'pkglist')
            builder.hash_path = str(root / 'hash')
            builder.source_hash = 'a' * 40
            builder.logger = Mock()
            builder.retrieve_current_remote_origin_and_sha = Mock(return_value={'sha': 'a' * 40, 'url': 'repo'})
            builder.setup_ccache = Mock()
            builder.execute_pre_depends_commands = Mock()
            builder.run_in_chroot = Mock()
            builder.delete_overlayfs = Mock()

            def record(*args):
                self.assertEqual(args[:2], ('middlewared', 'truenas'))
                self.assertEqual((packages / 'middlewared.deb').read_bytes(), b'built output')
                builder.delete_overlayfs.assert_not_called()

            with patch('scale_build.packages.build.PKG_DIR', str(packages)), patch(
                'scale_build.packages.build.get_manifest', return_value={'debian_release': 'trixie'}
            ), patch('scale_build.packages.build.verify_source_pin') as verify, patch(
                'scale_build.packages.build.record_source_build', side_effect=record
            ) as emit:
                builder._build_impl()
            verify.assert_called_once_with('truenas', 'a' * 40, 'repo')
            emit.assert_called_once()
            builder.delete_overlayfs.assert_called_once()

    def test_dirty_locked_source_is_rejected_before_copying_or_building(self):
        builder = BuildPackageMixin()
        builder.name = 'middlewared'
        builder.source_name = 'truenas'
        builder.source_path = '/fixture/source'
        builder.retrieve_current_remote_origin_and_sha = Mock(return_value={'sha': 'a' * 40, 'url': 'repo'})
        with patch('scale_build.packages.build.source_pin', return_value={'commit': 'a' * 40}), patch(
            'scale_build.packages.build.verify_source_pin'
        ), patch('scale_build.packages.build.run', return_value=Mock(stdout=' M changed.py')), patch(
            'scale_build.packages.build.shutil.copytree'
        ) as copy_source:
            with self.assertRaisesRegex(CallError, 'modified immutable component sources'):
                builder._build_impl()
            copy_source.assert_not_called()

    def test_package_initialization_keeps_shared_source_targets_available_to_receipt_validation(self):
        from scale_build.utils.package import get_packages
        manifest = {'sources': [{'name': 'truenas', 'repo': 'repo', 'branch': 'branch',
                                 'subpackages': [{'name': 'middlewared'}]}]}
        original = copy.deepcopy(manifest)
        with patch('scale_build.utils.package.get_manifest', return_value=manifest):
            self.assertEqual([package.name for package in get_packages()], ['truenas', 'middlewared'])
        self.assertEqual(manifest, original)

    def test_invalid_receipts_block_image_assembly_before_any_squashfs_or_boot_work(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            lock = fixture()
            with patch.object(update, 'CHROOT_BASEDIR', str(root / 'image')), patch.object(
                update, 'RELEASE_DIR', str(root / 'release')
            ), patch.object(update, 'UPDATE_DIR', str(root / 'update')), patch.object(
                update, 'get_version', return_value=lock['system_version']
            ), patch.object(update, 'release_lock', return_value=lock), patch.object(
                update, 'get_manifest', return_value={}
            ), patch.object(update, 'write_rootfs_build_receipt', side_effect=CallError('bad receipt')), patch.object(
                update, 'run'
            ) as command:
                with self.assertRaisesRegex(CallError, 'bad receipt'):
                    update.build_rootfs_image()
                command.assert_not_called()

    def test_candidate_manifest_binds_the_exported_build_receipt_digest(self):
        from scale_build.image import manifest
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            inner = root / 'inner'
            inner.mkdir()
            (inner / 'manifest.json').write_text(json.dumps({'version': 'fixture', 'date': 'today'}))
            image = root / 'fixture.update'
            image.write_bytes(b'fixture, not an actual image')
            digest = receipts.write_checked_json(root / receipts.RECEIPT_NAME, {'schema_version': 1})
            output = root / 'candidate.json'
            with patch.object(manifest, 'UPDATE_DIR', str(inner)), patch.object(
                manifest, 'RELEASE_DIR', str(root)
            ), patch.object(manifest, 'RELEASE_MANIFEST', str(output)):
                manifest.build_release_manifest(str(image), 'image-checksum')
            self.assertEqual(json.loads(output.read_text())['build_receipt'],
                             {'filename': receipts.RECEIPT_NAME, 'sha256': digest})

    def test_cleanup_preserves_only_component_notices_and_keeps_other_pruning(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            documentation = root / 'usr/share/doc'
            for name in [*receipts.LEGAL_NOTICE_PACKAGES, 'unrelated']:
                directory = documentation / name
                directory.mkdir(parents=True)
                (directory / 'copyright').write_text('license notice')
            (documentation / 'loose-file').write_text('remove')
            for relative in ('var/cache/apt', 'var/lib/apt/lists', 'var/trash', 'etc/ssh'):
                directory = root / relative
                directory.mkdir(parents=True)
                (directory / 'old-file').write_text('old')
            (root / 'etc/ssh/ssh_host_rsa_key').write_text('device key')
            with patch.object(update, 'CHROOT_BASEDIR', folder), patch.object(
                update, 'get_manifest', return_value={'base-prune': ['unwanted']}
            ), patch.object(update, 'run_in_chroot') as command:
                update.clean_rootfs()
            self.assertEqual({entry.name for entry in documentation.iterdir()}, receipts.LEGAL_NOTICE_PACKAGES)
            for name in receipts.LEGAL_NOTICE_PACKAGES:
                self.assertEqual((documentation / name / 'copyright').read_text(), 'license notice')
            for relative in ('var/cache/apt', 'var/lib/apt/lists', 'var/trash'):
                self.assertEqual(list((root / relative).iterdir()), [])
            self.assertFalse((root / 'etc/ssh/ssh_host_rsa_key').exists())
            self.assertTrue((root / 'etc/ssh/old-file').exists())
            self.assertEqual(command.call_args_list[0].args[0], ['apt', 'remove', '-y', 'unwanted'])
            self.assertEqual(command.call_args_list[1].args[0], ['apt', 'autoremove', '-y'])


if __name__ == '__main__':
    unittest.main()
