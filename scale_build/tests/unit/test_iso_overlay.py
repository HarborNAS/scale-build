from pathlib import Path
from contextlib import ExitStack
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch


@unittest.skipUnless(shutil.which('rsync') and hasattr(os, 'symlink'), 'Linux rsync is required')
class IsoOverlayTests(unittest.TestCase):
    def test_cd_files_preserve_merged_usr_aliases_with_different_directory_modes(self):
        source = Path(__file__).resolve().parents[3] / 'conf/cd-files'
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            overlay, chroot = root / 'overlay', root / 'chroot'
            shutil.copytree(source, overlay, symlinks=True)
            for directory, dirs, _ in os.walk(overlay):
                os.chmod(directory, 0o775)
            for name in ('usr/lib', 'usr/sbin'):
                path = chroot / name
                path.mkdir(parents=True, exist_ok=True)
                os.chmod(path, 0o755)
            (chroot / 'lib').symlink_to('usr/lib', target_is_directory=True)
            (chroot / 'sbin').symlink_to('usr/sbin', target_is_directory=True)
            copied = subprocess.run(['rsync', '-aKv', str(overlay) + '/', str(chroot) + '/'],
                                    capture_output=True, text=True)
            self.assertEqual(copied.returncode, 0, copied.stdout + copied.stderr)
            self.assertEqual((chroot / 'lib').readlink(), Path('usr/lib'))
            self.assertEqual((chroot / 'sbin').readlink(), Path('usr/sbin'))
            for name in ('getty@.service', 'serial-getty@.service', 'mount-cd.service'):
                self.assertEqual((chroot / 'usr/lib/systemd/system' / name).read_bytes(),
                                 (overlay / 'usr/lib/systemd/system' / name).read_bytes())
            link = chroot / 'usr/lib/systemd/system/multi-user.target.wants/mount-cd.service'
            self.assertEqual(link.readlink(), Path('../mount-cd.service'))
            executable = chroot / 'usr/sbin/mount-cd'
            self.assertEqual(executable.read_bytes(), (overlay / 'usr/sbin/mount-cd').read_bytes())
            self.assertEqual(executable.stat().st_mode & 0o777, 0o755)

    def test_absolute_build_directories_mount_inside_the_installer_chroot(self):
        from scale_build.image import iso

        class MountsPrepared(Exception):
            pass

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            chroot, builder = root / 'chroot', root / 'builder'
            for name in ('etc', 'boot', 'packages'):
                (chroot / name).mkdir(parents=True)
            for name in ('initrd.img', 'vmlinuz'):
                (chroot / name).write_bytes(b'boot fixture')
            for name in ('release', 'packages', 'tmp', 'cd-files'):
                (builder / name).mkdir(parents=True)
            update = builder / 'release/test.update'
            update.write_bytes(b'unchanged update fixture')
            mounts, unmounts = [], []

            def run(command, **kwargs):
                if isinstance(command, list):
                    if command[0] == 'mksquashfs':
                        Path(command[2]).write_bytes(b'CD rootfs fixture')
                    elif command[:2] == ['mount', '--bind']:
                        mounts.append((Path(command[2]), Path(command[3])))
                    elif command[:2] == ['umount', '-f']:
                        unmounts.append(Path(command[2]))

            values = {'CHROOT_BASEDIR': str(chroot), 'RELEASE_DIR': str(builder / 'release'),
                      'CD_DIR': str(builder / 'cdrom'), 'TMP_DIR': str(builder / 'tmp'),
                      'PKG_DIR': str(builder / 'packages'), 'CD_FILES_DIR': str(builder / 'cd-files')}
            with ExitStack() as stack:
                for name, value in values.items():
                    stack.enter_context(patch.object(iso, name, value))
                stack.enter_context(patch.object(iso, 'run', side_effect=run))
                stack.enter_context(patch.object(iso, 'run_in_chroot', side_effect=MountsPrepared))
                stack.enter_context(patch.object(iso, 'get_image_version', return_value='test-version'))
                stack.enter_context(patch.object(iso, 'update_file_path', return_value=str(update)))
                stack.enter_context(patch.object(iso, 'pruning_cd_basedir_contents', return_value=[]))
                with self.assertRaises(MountsPrepared):
                    iso.make_iso_file()
            self.assertEqual(len(mounts), 3)
            for source, target in mounts[:2]:
                self.assertTrue(target.is_relative_to(chroot), (source, target))
                self.assertNotEqual(source, target)
                self.assertEqual(Path('/') / target.relative_to(chroot), source)
                self.assertIn(target, unmounts)
            self.assertEqual(update.read_bytes(), b'unchanged update fixture')
