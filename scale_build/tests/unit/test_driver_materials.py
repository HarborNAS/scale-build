import json
import hashlib
from pathlib import Path
import tempfile
import unittest

from scale_build.driver_materials import validate_nvidia_material, verify_nvidia_download, verify_nvidia_payload
from scale_build.exceptions import CallError


VERSION = '595.104.02'
KERNEL = '6.18.13-production+truenas'
MATERIAL = {
    'current': VERSION,
    'source_url': (f'https://us.download.nvidia.com/XFree86/Linux-x86_64/{VERSION}/'
                   f'NVIDIA-Linux-x86_64-{VERSION}-no-compat32.run'),
    'sha256': '2300803474f9f9c210cbf26b151bf5b671243733dc451ddfb4430290c4ffca64',
}


class NvidiaMaterialsTests(unittest.TestCase):
    def info(self, path, field):
        return {'vermagic': KERNEL + ' SMP preempt', 'version': VERSION, 'license': 'Dual MIT/GPL'}[field]

    def test_material_requires_matching_official_source_and_hash(self):
        self.assertEqual(validate_nvidia_material(MATERIAL), MATERIAL)
        for change in ({'sha256': ''}, {'source_url': MATERIAL['source_url'].replace(VERSION, '590.44.01')},
                       {'source_url': MATERIAL['source_url'] + '?token=private'}):
            with self.subTest(change=change), self.assertRaises(CallError):
                validate_nvidia_material({**MATERIAL, **change})

    def test_download_bytes_must_match_the_reviewed_digest(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'driver.run'
            path.write_bytes(b'exact source bytes')
            material = {**MATERIAL, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
            verify_nvidia_download(path, material)
            path.write_bytes(b'corrupted download')
            with self.assertRaisesRegex(CallError, 'SHA256'):
                verify_nvidia_download(path, material)

    def make_payload(self, root):
        elf = b'\x7fELF\x02\x01' + b'\0' * 10 + b'\x03\x00\x3e\x00'
        files = ['usr/bin/nvidia-smi', f'usr/lib/libcuda.so.{VERSION}',
                 f'usr/lib/libnvidia-ml.so.{VERSION}', 'usr/lib/libGLX_nvidia.so.0',
                 f'usr/lib/firmware/nvidia/{VERSION}/gsp_tu10x.bin']
        files += [f'usr/lib/modules/{KERNEL}/video/{name}.ko'
                  for name in ('nvidia', 'nvidia-uvm', 'nvidia-drm', 'nvidia-modeset')]
        for name in files:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(elf)
        path = root / 'usr/share/vulkan/icd.d/nvidia_icd.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'ICD': {'library_path': 'libGLX_nvidia.so.0', 'api_version': '1.3.0'}}))

    def test_qualified_payload_requires_matched_kernel_firmware_userspace_and_icd(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.make_payload(root)
            quality = verify_nvidia_payload(root, KERNEL, MATERIAL, self.info)
            self.assertTrue(quality['payload_verified'])
            self.assertFalse(quality['hardware_recognition_tested'])
            self.assertEqual(len(quality['modules']), 4)

            def wrong_vermagic(path, field):
                return 'another-kernel SMP' if field == 'vermagic' else self.info(path, field)

            with self.assertRaisesRegex(CallError, 'vermagic'):
                verify_nvidia_payload(root, KERNEL, MATERIAL, wrong_vermagic)
            with self.assertRaisesRegex(CallError, 'versioned open driver'):
                verify_nvidia_payload(root, KERNEL, MATERIAL,
                                      lambda path, field: '590.44.01' if field == 'version' else self.info(path, field))

    def test_incomplete_or_non_elf_payload_is_rejected(self):
        for name in ('usr/bin/nvidia-smi', f'usr/lib/libcuda.so.{VERSION}',
                     f'usr/lib/firmware/nvidia/{VERSION}/gsp_tu10x.bin',
                     'usr/share/vulkan/icd.d/nvidia_icd.json'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                self.make_payload(root)
                (root / name).unlink()
                with self.assertRaises(CallError):
                    verify_nvidia_payload(root, KERNEL, MATERIAL, self.info)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.make_payload(root)
            (root / 'usr/bin/nvidia-smi').write_text('#!/bin/sh\nexit 0\n')
            with self.assertRaisesRegex(CallError, 'ELF'):
                verify_nvidia_payload(root, KERNEL, MATERIAL, self.info)

    def test_payload_cannot_escape_through_a_symlinked_parent(self):
        with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryDirectory() as outside_folder:
            root = Path(folder)
            outside = Path(outside_folder)
            self.make_payload(root)
            (outside / 'nvidia-smi').write_bytes((root / 'usr/bin/nvidia-smi').read_bytes())
            (root / 'usr/bin/nvidia-smi').unlink()
            (root / 'usr/bin').rmdir()
            try:
                (root / 'usr/bin').symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest('Symlink creation requires local privilege')
            with self.assertRaises(CallError):
                verify_nvidia_payload(root, KERNEL, MATERIAL, self.info)


if __name__ == '__main__':
    unittest.main()
