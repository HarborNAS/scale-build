import io
import json
from pathlib import Path
import tarfile
import unittest
from unittest.mock import Mock, patch

from scale_build.exceptions import CallError
from scale_build.product_profiles import verify_debian_component


class DebianComponentTests(unittest.TestCase):
    def check(self, source='a' * 40, profile='nexus-amd64', control=None, eligible=None, declared_eligible=False):
        contract = {
            'schema_version': 1,
            'package': 'harboros-beacon',
            'source_commit': source,
            'product_profile': profile}
        if eligible is not None:
            contract['release_eligible'] = eligible
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode='w') as archive:
            data = json.dumps(contract).encode()
            member = tarfile.TarInfo('./usr/share/harboros/component-contracts/harboros-beacon.json')
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
        stream.seek(0)
        process = Mock()
        process.stdout = stream
        process.wait.return_value = 0
        manager = Mock()
        manager.__enter__ = Mock(return_value=process)
        manager.__exit__ = Mock(return_value=False)
        pin = {
            'version': '1.1.0+nexus.amd64.1',
            'architecture': 'amd64',
            'source_commit': 'a' * 40,
            'release_eligible': declared_eligible}
        fields = control or 'Package: harboros-beacon\nVersion: 1.1.0+nexus.amd64.1\nArchitecture: amd64\n'
        with patch('subprocess.check_output', return_value=fields), patch('subprocess.Popen', return_value=manager):
            verify_debian_component(Path('component.deb'), 'harboros-beacon', pin)

    def test_matching_identity_is_accepted(self): self.check()

    def test_debian_architecture_source_and_product_must_match(self):
        for options in ({'source': 'b' * 40}, {'profile': 'n2-riscv64'},
                        {'control': 'Package: harboros-beacon\nVersion: 1.0.0\nArchitecture: riscv64\n'}):
            with self.assertRaises(CallError):
                self.check(**options)

    def test_embedded_release_blocker_cannot_be_overridden(self):
        with self.assertRaises(CallError):
            self.check(eligible=False, declared_eligible=True)


if __name__ == '__main__':
    unittest.main()
