from contextlib import chdir
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scale_build import extensions


class NvidiaInstallerTests(unittest.TestCase):
    def test_installer_copies_default_vulkan_icd_into_the_usr_extension(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'etc/vulkan/icd.d/nvidia_icd.json'
            source.parent.mkdir(parents=True)
            source.write_text('{"ICD": {"library_path": "libGLX_nvidia.so.0"}}')
            driver = root / 'NVIDIA-Linux-x86_64.run'
            driver.write_text('installer')
            builder = extensions.NvidiaExtension('', '', str(root))
            with patch.object(builder, 'download_nvidia_driver', return_value=str(driver)), patch.object(
                builder, 'run'
            ) as command:
                builder.install_nvidia_driver('target-kernel')
            install = command.call_args.args[0]
            self.assertNotIn('--vulkan-icd-dir=/usr/share/vulkan/icd.d', install)
            self.assertIn('--kernel-name=target-kernel', install)
            destination = root / 'usr/share/vulkan/icd.d/nvidia_icd.json'
            self.assertEqual(destination.read_text(), '{"ICD": {"library_path": "libGLX_nvidia.so.0"}}')
            self.assertTrue(source.exists())
            self.assertFalse(driver.exists())

    def test_installer_rejects_a_missing_default_vulkan_icd(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            driver = root / 'NVIDIA-Linux-x86_64.run'
            driver.write_text('installer')
            builder = extensions.NvidiaExtension('', '', str(root))
            with patch.object(builder, 'download_nvidia_driver', return_value=str(driver)), patch.object(
                builder, 'run'
            ), self.assertRaisesRegex(ValueError, 'Vulkan ICD'):
                builder.install_nvidia_driver('target-kernel')

    def test_installer_rejects_an_empty_default_vulkan_icd(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'etc/vulkan/icd.d/nvidia_icd.json'
            source.parent.mkdir(parents=True)
            source.write_bytes(b'')
            driver = root / 'NVIDIA-Linux-x86_64.run'
            driver.write_text('installer')
            builder = extensions.NvidiaExtension('', '', str(root))
            with patch.object(builder, 'download_nvidia_driver', return_value=str(driver)), patch.object(
                builder, 'run'
            ), self.assertRaisesRegex(ValueError, 'regular Vulkan ICD'):
                builder.install_nvidia_driver('target-kernel')

    def test_installer_rejects_a_symlinked_default_vulkan_icd(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            target = root / 'untrusted.json'
            target.write_text('{"ICD": {}}')
            source = root / 'etc/vulkan/icd.d/nvidia_icd.json'
            source.parent.mkdir(parents=True)
            try:
                source.symlink_to(target)
            except OSError as error:
                self.skipTest(f'symlinks are unavailable: {error}')
            driver = root / 'NVIDIA-Linux-x86_64.run'
            driver.write_text('installer')
            builder = extensions.NvidiaExtension('', '', str(root))
            with patch.object(builder, 'download_nvidia_driver', return_value=str(driver)), patch.object(
                builder, 'run'
            ), self.assertRaisesRegex(ValueError, 'regular Vulkan ICD'):
                builder.install_nvidia_driver('target-kernel')

    def test_installer_rejects_an_existing_retained_vulkan_icd(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'etc/vulkan/icd.d/nvidia_icd.json'
            source.parent.mkdir(parents=True)
            source.write_text('{"ICD": {}}')
            destination = root / 'usr/share/vulkan/icd.d/nvidia_icd.json'
            destination.parent.mkdir(parents=True)
            destination.write_text('existing')
            driver = root / 'NVIDIA-Linux-x86_64.run'
            driver.write_text('installer')
            builder = extensions.NvidiaExtension('', '', str(root))
            with patch.object(builder, 'download_nvidia_driver', return_value=str(driver)), patch.object(
                builder, 'run'
            ), self.assertRaisesRegex(ValueError, 'destination already exists'):
                builder.install_nvidia_driver('target-kernel')
            self.assertEqual(destination.read_text(), 'existing')


@unittest.skipUnless(all(shutil.which(tool) for tool in ('rsync', 'mksquashfs', 'unsquashfs')),
                     'Linux extension packaging tools are required')
class ExtensionPayloadTests(unittest.TestCase):
    def test_absolute_chroot_keeps_changed_usr_files_and_links_from_another_cwd(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            base, source, cwd = [root / name for name in ('base', 'source', 'cwd')]
            (base / 'usr/bin').mkdir(parents=True)
            (base / 'usr/bin/unchanged').write_text('same file')
            (base / 'usr/lib/unchanged-directory').mkdir(parents=True)
            (base / 'usr/lib/unchanged-directory/payload').write_text('same directory payload')
            (base / 'usr/lib/unchanged-link').symlink_to('unchanged-directory', target_is_directory=True)
            shutil.copytree(base, source, symlinks=True)
            cwd.mkdir()
            for name in ('make', 'sshpass', 'literal -> name'):
                (source / 'usr/bin' / name).write_text('new payload: ' + name)
            (source / 'usr/lib/new-directory').mkdir(parents=True)
            (source / 'usr/lib/new-directory/payload').write_text('directory payload')
            (source / 'usr/lib/directory-link').symlink_to('new-directory', target_is_directory=True)
            (source / 'usr/bin/file-link').symlink_to('make')
            (source / 'usr/bin/dangling-link').symlink_to('absent-target')
            (source / 'usr/src').mkdir()
            (source / 'usr/src/excluded-header').write_text('exclude source headers')
            (source / 'usr/srcx').mkdir()
            (source / 'usr/srcx/allowed').write_text('similar prefix is allowed')
            (source / 'etc').mkdir()
            (source / 'etc/excluded-config').write_text('exclude configuration')
            extension = root / 'dev-tools.raw'
            with chdir(cwd), patch.dict('os.environ', {'BUILDER_DIR': str(cwd)}):
                extensions.Extension('', str(base), str(source)).build_extension('dev-tools', str(extension))
            unpacked = root / 'unpacked'
            subprocess.run(['unsquashfs', '-dest', str(unpacked), str(extension)], check=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            for name in ('make', 'sshpass', 'literal -> name'):
                self.assertEqual((unpacked / 'usr/bin' / name).read_text(), 'new payload: ' + name)
            self.assertEqual((unpacked / 'usr/lib/directory-link').readlink(), Path('new-directory'))
            self.assertEqual((unpacked / 'usr/bin/file-link').readlink(), Path('make'))
            self.assertEqual((unpacked / 'usr/bin/dangling-link').readlink(), Path('absent-target'))
            self.assertFalse((unpacked / 'usr/bin/unchanged').exists())
            self.assertFalse((unpacked / 'usr/lib/unchanged-link').is_symlink())
            self.assertFalse((unpacked / 'usr/src').exists())
            self.assertEqual((unpacked / 'usr/srcx/allowed').read_text(), 'similar prefix is allowed')
            self.assertFalse((unpacked / 'etc').exists())

    def test_rsync_paths_cannot_escape_the_source_tree(self):
        for name in ('/tmp/outside', '../outside', 'usr/../../outside'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as folder:
                source = Path(folder) / 'source'
                source.mkdir()
                with patch.object(extensions, 'run', return_value=subprocess.CompletedProcess([], 0, name + '\n')):
                    with self.assertRaises(ValueError):
                        extensions.Extension('', folder, str(source)).build_extension('test', folder + '/test.raw')

    def test_descriptor_only_payload_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            base, source = [Path(folder) / name for name in ('base', 'source')]
            base.mkdir()
            (source / 'usr/src').mkdir(parents=True)
            (source / 'usr/src/header').write_text('not extension payload')
            with self.assertRaisesRegex(ValueError, 'payload'):
                extensions.Extension('', str(base), str(source)).build_extension('test', folder + '/test.raw')

    def test_nvidia_payload_requires_driver_and_kernel_modules(self):
        for module in (None, 'nvidia-wmi-ec-backlight.ko', 'nvidia.ko'):
            with self.subTest(module=module), tempfile.TemporaryDirectory() as folder:
                base, source = [Path(folder) / name for name in ('base', 'source')]
                base.mkdir()
                (source / 'usr/bin').mkdir(parents=True)
                (source / 'usr/bin/nvidia-smi').write_text('driver tool')
                if module:
                    (source / 'usr/lib/modules/target').mkdir(parents=True)
                    (source / 'usr/lib/modules/target' / module).write_text('module payload')
                builder = extensions.Extension('', str(base), str(source))
                if module == 'nvidia.ko':
                    builder.build_extension('nvidia', folder + '/nvidia.raw')
                    self.assertTrue(Path(folder + '/nvidia.raw').is_file())
                else:
                    with self.assertRaisesRegex(ValueError, 'kernel module'):
                        builder.build_extension('nvidia', folder + '/nvidia.raw')
