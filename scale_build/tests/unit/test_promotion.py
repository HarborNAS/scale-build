import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scale_build.exceptions import CallError
from scale_build.promotion import validate_promotion
from scale_build.tests.unit.test_product_profiles import fixture
from scale_build.tests.unit.test_driver_materials import MATERIAL
from scale_build.product_profiles import apply_release_lock


class PromotionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.lock = fixture()
        self.lock['status'] = 'stable'
        self.lock['scope'] = {'external_publication': True}
        self.lock['drivers'] = {'nvidia': dict(MATERIAL)}
        for pin in [*self.lock['packages'].values(), *self.lock['runtime']['packages'].values()]:
            pin.update(release_eligible=True, blocking_reasons=[])
        self.lock['acceptance'] = {key: 'passed' for key in
                                   ('new_install', 'native_upgrade', 'boot_environment_rollback', 'reboot')}
        self.rows = []
        self.iso_name = 'TrueNAS-SCALE-' + self.lock['system_version'] + '-HarborOS.iso'
        self.update_name = 'TrueNAS-SCALE-' + self.lock['system_version'] + '.update'
        self.add('iso', self.iso_name, b'ISO')
        self.add('update', self.update_name, b'UPDATE')
        lock_sha = hashlib.sha256(json.dumps(self.lock, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        self.add('build_receipt', 'build-receipt.json', json.dumps({
            'product': 'nexus', 'architecture': 'amd64', 'system_version': self.lock['system_version'],
            'schema_version': 1, 'source_date_epoch': self.lock['source_date_epoch'],
            'release_lock_sha256': lock_sha,
            'locked_external_packages': {
                **self.lock['packages'], **self.lock['runtime']['packages'],
            }}).encode())
        self.add('artifact_readback', 'readback.json', json.dumps({
            'source_commit': 'a' * 40, 'runtime_source_commit': 'a' * 40,
            'iso': {'sha256': self.rows[0]['sha256'], 'exact_update_payload_match': True,
                    'embedded_update_sha256': self.rows[1]['sha256'], 'source_commit': 'a' * 40},
            'update': {'sha256': self.rows[1]['sha256'],
                       'nvidia_extension_sha256': 'd' * 64,
                       'nvidia_extension_size_bytes': 1234}}).encode())
        self.add('gpu_driver_materials', 'nvidia-materials.json', json.dumps({
            'material': MATERIAL, 'payload_verified': True, 'kernel_release': 'test-kernel',
            'hardware_recognition_tested': False, 'inference_qualified': False,
            'artifact': {'filename': 'nvidia.raw', 'sha256': 'd' * 64, 'size_bytes': 1234}}).encode())
        self.acceptance = {}
        for kind in self.lock['acceptance']:
            self.acceptance[kind] = kind + '.json'
            self.add('acceptance_evidence', kind + '.json', json.dumps({
                'kind': kind, 'status': 'passed', 'system_version': self.lock['system_version']}).encode())
        self.gpus = {}
        for vendor, driver in [('NVIDIA', 'nvidia'), ('Intel', 'xe'), ('AMD', 'amdgpu')]:
            self.gpus[vendor] = vendor + '.json'
            self.add('gpu_recognition', vendor + '.json', json.dumps({
                'vendor': vendor, 'driver': driver, 'kernel_release': 'test-kernel',
                'pci_id': '0000:03:00.0', 'recognized': True, 'hardware_api_enumerated': True,
                'pci_vendor_id': {'NVIDIA': '10de', 'Intel': '8086', 'AMD': '1002'}[vendor],
                'pci_device_id': '1234', 'discrete': True, 'vram_bytes': 8 * 1024 ** 3,
                'software_device': False, 'service_user_access': True,
                'reset_or_probe_errors': False, 'device_nodes': ['/dev/nvidia0', '/dev/nvidiactl']
                if vendor == 'NVIDIA' else ['/dev/dri/renderD128']}).encode())
        lineage = {key: 'a' * 40 for key in
                   ('update_producer_commit', 'iso_producer_commit', 'runtime_producer_commit', 'tag_commit')}
        self.manifest = {'schema_version': 1, 'product': 'nexus', 'architecture': 'amd64', 'channel': 'stable',
                         'system_version': self.lock['system_version'], 'release_lock_sha256': lock_sha,
                         'kernel_release': 'test-kernel', 'lineage': lineage,
                         'artifacts': self.rows, 'acceptance': self.acceptance,
                         'gpu_recognition': self.gpus}
        self.add('checksum', self.iso_name + '.sha256', self.rows[0]['sha256'].encode())
        self.add('checksum', self.update_name + '.sha256', self.rows[1]['sha256'].encode())

    def add(self, kind, name, payload):
        (self.root / name).write_bytes(payload)
        self.rows.append({'kind': kind, 'filename': name, 'size_bytes': len(payload),
                          'sha256': hashlib.sha256(payload).hexdigest()})

    def validate(self):
        with patch('scale_build.promotion._verify_lineage'), patch('scale_build.promotion.validate_ffmpeg_materials'):
            return validate_promotion(self.manifest, self.lock, self.root)

    def test_verified_files_and_explicit_acceptance_can_be_promoted(self):
        self.assertTrue(self.validate()['publishable'])

    def test_internal_or_pending_lock_is_not_promoted(self):
        self.lock['acceptance']['new_install'] = 'pending'
        with self.assertRaises(CallError):
            self.validate()

    def test_changed_file_or_missing_real_gpu_is_rejected(self):
        (self.root / self.iso_name).write_bytes(b'other')
        with self.assertRaisesRegex(CallError, 'artifact'):
            self.validate()
        (self.root / self.iso_name).write_bytes(b'ISO')
        self.manifest['gpu_recognition'].pop('Intel')
        with self.assertRaises(CallError):
            self.validate()

    def test_unsafe_path_and_fabricated_readback_are_rejected(self):
        self.manifest['artifacts'][0]['filename'] = '../image.iso'
        with self.assertRaises(CallError):
            self.validate()
        self.manifest['artifacts'][0]['filename'] = self.iso_name
        p = self.root / 'readback.json'
        data = json.loads(p.read_text())
        data['iso']['embedded_update_sha256'] = '0' * 64
        payload = json.dumps(data).encode()
        p.write_bytes(payload)
        self.rows[3].update(size_bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest())
        with self.assertRaisesRegex(CallError, 'readback'):
            self.validate()

    def test_stable_network_writes_are_preceded_by_the_gate(self):
        import yaml
        for name, write_name in [('buildiso.yml', 'Upload ISO and update to stable storage'),
                                 ('update_release.yml', 'update releases.json')]:
            workflow = yaml.safe_load((Path('.github/workflows') / name).read_text())
            steps = workflow['jobs']['build-iso']['steps']
            checkout = next(row for row in steps if str(row.get('uses', '')).startswith('actions/checkout@'))
            self.assertEqual(checkout.get('with', {}).get('fetch-depth'), 0)
            self.assertEqual(
                checkout['uses'],
                'actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683',
            )
            guard = next(i for i, row in enumerate(steps) if row['name'] == 'Validate stable publication evidence')
            write = next(i for i, row in enumerate(steps) if row['name'] == write_name)
            self.assertLess(guard, write)
            self.assertIn('scale_build.promotion', steps[guard]['run'])
            self.assertIn('--expected-version', steps[guard]['run'])
            self.assertIn('--artifacts-dir "$GITHUB_WORKSPACE/tmp/release"', steps[guard]['run'])
            if name == 'buildiso.yml':
                upload = next(row for row in steps if str(row.get('uses', '')).startswith('actions/upload-artifact@'))
                self.assertEqual(
                    upload['uses'],
                    'actions/upload-artifact@65c4c4a1ddee5b72f698fdd19549f0f0fb45cf08',
                )
                self.assertEqual(steps[guard].get('if'), steps[write].get('if'))
                self.assertIn('promotionManifest', steps[write]['if'])
            else:
                triggers = workflow.get('on', workflow.get(True))
                self.assertNotIn('artifactDirectory', triggers['workflow_dispatch']['inputs'])

    def test_uploader_filename_and_checksum_are_bound(self):
        row = self.rows[0]
        original = row['filename']
        row['filename'] = 'another.iso'
        (self.root / 'another.iso').write_bytes(b'ISO')
        with self.assertRaisesRegex(CallError, 'upload filename'):
            self.validate()
        row['filename'] = original
        (self.root / (self.iso_name + '.sha256')).write_text('0' * 64)
        with self.assertRaises(CallError):
            self.validate()

    def test_driver_and_ffmpeg_producer_pins_reach_the_actual_build_manifest(self):
        pin = self.lock['runtime']['packages']['harboros-ffmpeg']
        pin['materials'] = {'producer': {'commit': 'b' * 40}}
        with patch('scale_build.product_profiles.release_lock', return_value=self.lock):
            result = apply_release_lock({'sources': []})
        self.assertEqual(result['extensions']['nvidia'], MATERIAL)
        self.assertEqual(result['external-packages']['harboros-ffmpeg']['component_pin']['source_commit'], 'b' * 40)

    def test_receipt_and_embedded_nvidia_bytes_must_match_the_release_lock(self):
        receipt = self.root / 'build-receipt.json'
        data = json.loads(receipt.read_text())
        data['locked_external_packages']['harboros-ffmpeg']['deb_version'] = 'wrong'
        payload = json.dumps(data).encode()
        receipt.write_bytes(payload)
        row = next(row for row in self.rows if row['kind'] == 'build_receipt')
        row.update(size_bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest())
        with self.assertRaisesRegex(CallError, 'build receipt'):
            self.validate()

        data['locked_external_packages'] = {
            **self.lock['packages'], **self.lock['runtime']['packages'],
        }
        payload = json.dumps(data).encode()
        receipt.write_bytes(payload)
        row.update(size_bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest())
        proof = self.root / 'readback.json'
        readback = json.loads(proof.read_text())
        readback['update']['nvidia_extension_sha256'] = 'e' * 64
        payload = json.dumps(readback).encode()
        proof.write_bytes(payload)
        row = next(row for row in self.rows if row['kind'] == 'artifact_readback')
        row.update(size_bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest())
        with self.assertRaisesRegex(CallError, 'NVIDIA'):
            self.validate()


if __name__ == '__main__':
    unittest.main()
