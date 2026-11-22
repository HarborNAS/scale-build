import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scale_build import repack_update as repack
from scale_build.exceptions import CallError
from scale_build.image import manifest


def digest(path, algorithm='sha256'):
    return hashlib.new(algorithm, Path(path).read_bytes()).hexdigest()


@unittest.skipUnless(all(shutil.which(tool) for tool in ('git', 'mksquashfs', 'unsquashfs')),
                     'Linux update packaging tools are required')
class InstallerRepackTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / 'producer'
        self.repo.mkdir()
        self.git('init', '-q')
        self.git('config', 'user.name', 'Packaging fixture')
        self.git('config', 'user.email', 'fixture@example.invalid')
        installer = self.repo / 'truenas_install'
        installer.mkdir()
        (installer / '__init__.py').write_bytes(b'')
        (installer / 'harbor_native.py').write_bytes(b'old installer\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'Original installer fixture')
        self.original_source = self.git('rev-parse', 'HEAD').strip()
        self.lock = {'system_version': 'test-version', 'product': 'nexus', 'architecture': 'amd64',
                     'scope': {'service_migration': 'native-packages-v1'}}
        lock_digest = hashlib.sha256(json.dumps(self.lock, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        self.native = (json.dumps({'system_version': 'test-version', 'product': 'nexus', 'architecture': 'amd64',
                                  'release_lock_sha256': lock_digest}) + '\n').encode()
        native_digest = hashlib.sha256(self.native).hexdigest()
        inner = self.root / 'inner'
        (inner / 'etc').mkdir(parents=True)
        (inner / 'etc/version').write_text('test-version\n')
        (inner / 'usr/lib/modules/test-kernel').mkdir(parents=True)
        (inner / 'usr/lib/modules/test-kernel/runtime.ko').write_bytes(b'unchanged runtime fixture')
        native_dir = inner / 'usr/share/harboros'
        native_dir.mkdir(parents=True)
        (native_dir / 'build-receipt.json').write_bytes(self.native)
        (native_dir / 'build-receipt.json.sha256').write_text(native_digest + '\n')
        outer = self.root / 'original-payload'
        outer.mkdir()
        self.squash(inner, outer / 'rootfs.squashfs')
        (outer / 'build-receipt.json').write_bytes(self.native)
        (outer / 'build-receipt.json.sha256').write_text(native_digest + '\n')
        shutil.copytree(installer, outer / 'truenas_install')
        checksums = {p.relative_to(outer).as_posix(): digest(p, 'sha1') for p in outer.rglob('*') if p.is_file()}
        metadata = {'version': 'test-version', 'size': 1048576, 'kernel_version': 'test-kernel',
                    'checksums': checksums, 'harboros': {'service_migration': 'native-packages-v1'}}
        (outer / 'manifest.json').write_text(json.dumps(metadata))
        self.original = self.root / 'original.update'
        self.squash(outer, self.original)
        self.expected = {
            'schema_version': 1, 'update_source_commit': self.original_source,
            'runtime_source_commit': self.original_source, 'inner_rootfs_source_commit': self.original_source,
            'system_version': 'test-version', 'update_sha256': digest(self.original),
            'update_size_bytes': self.original.stat().st_size,
            'inner_rootfs_sha256': digest(outer / 'rootfs.squashfs'),
            'inner_rootfs_size_bytes': (outer / 'rootfs.squashfs').stat().st_size,
            'native_receipt_sha256': native_digest, 'release_lock_sha256': lock_digest,
            'manifest_size_bytes': metadata['size'], 'kernel_version': 'test-kernel',
            'service_migration': 'native-packages-v1',
        }
        self.original_digest = digest(self.original)
        (installer / 'harbor_native.py').write_bytes(b'committed ownership fix\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'Installer-only fixture fix')
        self.new_source = self.git('rev-parse', 'HEAD').strip()

    def git(self, *arguments):
        return subprocess.check_output(['git', '-C', str(self.repo), *arguments],
                                       stderr=subprocess.PIPE).decode()

    def squash(self, directory, image):
        subprocess.run(['mksquashfs', str(directory), str(image), '-noappend', '-processors', '1'],
                       check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def build(self, name='repacked'):
        specification = self.root / (name + '-input.json')
        specification.write_text(json.dumps(self.expected))
        with (patch.object(repack, 'BUILDER_DIR', str(self.repo)),
              patch.object(repack, 'release_lock', return_value=self.lock),
              patch.object(manifest, 'release_lock', return_value=self.lock)):
            return repack.repack_update(self.original, specification, digest(specification), self.root / name)

    def test_new_outer_keeps_original_runtime_receipt_and_git_installer_lineage(self):
        result = self.build()
        image = Path(result['update'])
        self.assertEqual(digest(self.original), self.original_digest)
        self.assertEqual(repack._member_digest(image, 'rootfs.squashfs'), self.expected['inner_rootfs_sha256'])
        self.assertEqual(repack._read_member(image, 'build-receipt.json'), self.native)
        self.assertEqual(repack._read_member(image, 'truenas_install/harbor_native.py'), b'committed ownership fix\n')
        self.assertEqual(result['update_source_commit'], self.new_source)
        self.assertEqual(result['runtime_source_commit'], self.original_source)
        self.assertEqual(result['native_receipt_sha256'], self.expected['native_receipt_sha256'])
        self.assertEqual(result['supersedes_update_sha256'], self.original_digest)
        self.assertFalse(result['apt_run'])
        self.assertFalse(result['packages_rebuilt'])
        self.assertFalse(result['kernel_rebuilt'])
        published = json.loads((image.parent / 'manifest.json').read_text())
        self.assertEqual(published['checksum'], digest(image))
        self.assertEqual(published['assembly_receipt']['sha256'], result['assembly_receipt_sha256'])
        metadata = json.loads(repack._read_member(image, 'manifest.json'))
        self.assertEqual(metadata['size'], self.expected['manifest_size_bytes'])
        self.assertEqual(metadata['harboros']['service_migration'], 'native-packages-v1')
        self.assertFalse((image.parent.parent / 'update/rootfs.squashfs').exists())

    def test_wrong_original_update_is_rejected_before_output_creation(self):
        self.expected['update_sha256'] = '0' * 64
        with self.assertRaisesRegex(CallError, 'Original update'):
            self.build()
        self.assertFalse((self.root / 'repacked').exists())
        self.assertEqual(digest(self.original), self.original_digest)

    def test_runtime_and_native_receipt_fingerprint_mismatches_are_rejected(self):
        for field in ('inner_rootfs_sha256', 'native_receipt_sha256'):
            with self.subTest(field=field):
                original = self.expected[field]
                self.expected[field] = '0' * 64
                with self.assertRaises(CallError):
                    self.build(field)
                self.expected[field] = original
                self.assertFalse(list((self.root / field).glob('release/*.update')))
        self.assertEqual(digest(self.original), self.original_digest)

    def test_metadata_and_release_lock_changes_are_rejected(self):
        self.expected['manifest_size_bytes'] += 1
        with self.assertRaisesRegex(CallError, 'metadata'):
            self.build('metadata')
        self.expected['manifest_size_bytes'] -= 1
        self.expected['release_lock_sha256'] = '0' * 64
        with self.assertRaisesRegex(CallError, 'release lock'):
            self.build('lock')
        self.assertFalse((self.root / 'metadata').exists())
        self.assertFalse((self.root / 'lock').exists())
