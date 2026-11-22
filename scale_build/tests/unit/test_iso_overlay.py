from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest


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
