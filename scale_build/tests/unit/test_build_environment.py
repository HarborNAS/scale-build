import os
import unittest
from unittest.mock import patch

from scale_build.packages.build import BuildPackageMixin


class BuildEnvironmentTests(unittest.TestCase):
    def test_proxy_environment_is_preserved_inside_debuild(self):
        builder = BuildPackageMixin()
        builder.name = 'fixture'
        builder.secret_env = []
        builder.buildcmd = None
        builder.deoptions = None
        builder.jobs = 12
        proxy_environment = {
            'http_proxy': 'http://127.0.0.1:8118',
            'https_proxy': 'http://127.0.0.1:8118',
            'no_proxy': '127.0.0.1,localhost',
        }

        with patch.dict(os.environ, proxy_environment, clear=True), \
                patch('scale_build.packages.build.get_secret_env', return_value={}), \
                patch('scale_build.packages.build.dependency_relay.environment', return_value={}):
            chroot_environment = builder._get_chroot_env()
            command = builder.build_command[0]

        for name, value in proxy_environment.items():
            self.assertEqual(chroot_environment[name], value)
            self.assertIn(f'-e{name}', command)


if __name__ == '__main__':
    unittest.main()
