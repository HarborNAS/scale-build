import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from scale_build.exceptions import CallError
from scale_build.release_materials import validate_ffmpeg_materials


class FfmpegConsumerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.material = {
            'producer': {'repository': 'https://github.com/HarborNAS/HarborBeacon', 'commit': 'a' * 40},
            'upstream': {'version': '9.1', 'url': 'https://ffmpeg.org/releases/ffmpeg-9.1.tar.xz', 'sha256': 'b' * 64},
            'libopus': {'version': '1.5.2', 'url': 'https://downloads.xiph.org/releases/opus/opus-1.5.2.tar.gz',
                        'sha256': 'c' * 64},
            'source_date_epoch': 1795333456, 'toolchain': {'image': 'debian:trixie@sha256:' + 'd' * 64},
            'configure_flags': ['--enable-libopus'], 'artifacts': {},
        }
        self.pin = {
            'deb_version': '9.1+harboros.1', 'architecture': 'amd64', 'sha256': 'e' * 64,
            'url_template': 'https://update.harboros.ai/deb/harboros-ffmpeg_9.1+harboros.1_linux_amd64.deb',
            'materials': self.material,
        }
        sbom = {'spdxVersion': 'SPDX-2.3', 'dataLicense': 'CC0-1.0', 'packages': []}
        for name, source in [('ffmpeg', self.material['upstream']), ('opus', self.material['libopus'])]:
            sbom['packages'].append({'name': name, 'versionInfo': source['version'],
                                     'downloadLocation': source['url'], 'licenseDeclared': 'LGPL-2.1-or-later',
                                     'checksums': [{'algorithm': 'SHA256', 'checksumValue': source['sha256']}]})
        dependencies = [{'uri': x['url'], 'digest': {'sha256': x['sha256']}}
                        for x in (self.material['upstream'], self.material['libopus'])]
        dependencies.append({'uri': self.material['producer']['repository'], 'digest': {'gitCommit': 'a' * 40}})
        self.provenance = {
            '_type': 'https://in-toto.io/Statement/v1', 'predicateType': 'https://slsa.dev/provenance/v1',
            'subject': [{'name': 'harboros-ffmpeg_9.1+harboros.1_linux_amd64.deb', 'digest': {'sha256': 'e' * 64}}],
            'predicate': {
                'buildDefinition': {'resolvedDependencies': dependencies,
                                    'externalParameters': {'source_date_epoch': self.material['source_date_epoch'],
                                                           'configure_flags': ['--enable-libopus']}},
                'runDetails': {'builder': {'id': self.material['toolchain']['image']}},
            },
        }
        self.rows = []
        self.write('sbom', sbom)
        self.write('provenance', self.provenance)

    def write(self, kind, value):
        raw = json.dumps(value).encode()
        name = 'ffmpeg-' + kind + '.json'
        (self.root / name).write_bytes(raw)
        row = {'kind': 'ffmpeg_' + kind, 'filename': name,
               'sha256': hashlib.sha256(raw).hexdigest(), 'size_bytes': len(raw)}
        self.rows[:] = [x for x in self.rows if x['kind'] != row['kind']] + [row]
        self.material['artifacts'][kind] = {'filename': name, 'sha256': row['sha256']}

    def test_real_material_contract_binds_source_toolchain_and_package(self):
        self.assertEqual(validate_ffmpeg_materials(self.pin, self.root, self.rows), self.material)

    def test_legacy_asset_cannot_be_promoted_by_flipping_eligible(self):
        with self.assertRaises(CallError):
            validate_ffmpeg_materials({'release_eligible': True, 'deb_version': '1.0.0'}, self.root, self.rows)

    def test_package_pin_requires_exact_amd64_https_artifact(self):
        self.assertEqual(validate_ffmpeg_materials(self.pin, self.root, self.rows), self.material)
        for change in (
            {'architecture': 'arm64'},
            {'sha256': 'short'},
            {'url_template': 'https://attacker.invalid/harboros-ffmpeg_9.1+harboros.1_linux_amd64.deb'},
        ):
            with self.subTest(change=change), self.assertRaises(CallError):
                validate_ffmpeg_materials({**self.pin, **change}, self.root, self.rows)

    def test_mismatched_package_or_omitted_opus_is_rejected(self):
        self.provenance['subject'][0]['digest']['sha256'] = '0' * 64
        self.write('provenance', self.provenance)
        with self.assertRaisesRegex(CallError, 'exact Debian'):
            validate_ffmpeg_materials(self.pin, self.root, self.rows)
        self.provenance['subject'][0]['digest']['sha256'] = self.pin['sha256']
        self.provenance['predicate']['buildDefinition']['resolvedDependencies'].pop(1)
        self.write('provenance', self.provenance)
        with self.assertRaisesRegex(CallError, 'source dependency'):
            validate_ffmpeg_materials(self.pin, self.root, self.rows)

    def test_corrupt_evidence_is_rejected(self):
        (self.root / 'ffmpeg-sbom.json').write_text('{}')
        with self.assertRaisesRegex(CallError, 'checksum mismatch'):
            validate_ffmpeg_materials(self.pin, self.root, self.rows)

    def test_evidence_path_cannot_escape_the_artifact_directory(self):
        row = next(row for row in self.rows if row['kind'] == 'ffmpeg_sbom')
        outside = self.root.parent / (self.root.name + '-escaped-sbom.json')
        outside.write_bytes((self.root / row['filename']).read_bytes())
        self.addCleanup(outside.unlink, missing_ok=True)
        escaped = '../' + outside.name
        row['filename'] = escaped
        self.material['artifacts']['sbom']['filename'] = escaped
        with self.assertRaises(CallError):
            validate_ffmpeg_materials(self.pin, self.root, self.rows)


if __name__ == '__main__':
    unittest.main()
