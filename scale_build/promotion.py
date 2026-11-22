"""Offline stable-promotion validation. No upload, signing, or appliance action."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

import jsonschema

from .exceptions import CallError
from .product_profiles import assert_publishable, validate_release_lock
from .driver_materials import validate_nvidia_material
from .release_materials import validate_ffmpeg_materials


SCHEMA = Path(__file__).resolve().parent.parent / 'conf/promotion-manifest.schema.json'


def _digest(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def _verify_lineage(lineage):
    try:
        head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
    except (OSError, subprocess.CalledProcessError) as error:
        raise CallError('Promotion lineage cannot read the checked-out commit') from error
    if head != lineage['tag_commit']:
        raise CallError('Promotion must run from its exact tag commit')
    for key in ('update_producer_commit', 'iso_producer_commit', 'runtime_producer_commit'):
        result = subprocess.run(['git', 'merge-base', '--is-ancestor', lineage[key], lineage['tag_commit']],
                                capture_output=True)
        if result.returncode:
            raise CallError(f'Promotion tag does not contain the actual {key}')


def validate_promotion(manifest, lock, directory):
    try:
        jsonschema.validate(manifest, json.loads(SCHEMA.read_text(encoding='utf-8')))
    except jsonschema.ValidationError as error:
        raise CallError('Invalid promotion manifest: ' + error.message) from error
    validate_release_lock(lock)
    assert_publishable(lock)
    if lock.get('status') != 'stable' or lock.get('scope', {}).get('external_publication') is not True:
        raise CallError('Stable promotion requires an explicitly publishable stable release lock')
    lock_sha = hashlib.sha256(json.dumps(lock, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    if manifest['release_lock_sha256'] != lock_sha or manifest['system_version'] != lock['system_version']:
        raise CallError('Promotion identity differs from the immutable release lock')
    root = Path(directory).resolve()
    rows = manifest['artifacts']
    by_name = {row['filename']: row for row in rows}
    if len(by_name) != len(rows):
        raise CallError('Duplicate promotion artifact filename')
    for row in rows:
        path = root / row['filename']
        if (path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root)
                or path.stat().st_size != row['size_bytes'] or _digest(path) != row['sha256']):
            raise CallError('Promotion artifact differs from its exact bytes: ' + row['filename'])

    def document(filename, kind):
        row = by_name.get(filename, {})
        if row.get('kind') != kind or row['size_bytes'] > 16 * 1024 * 1024:
            raise CallError('Missing bounded promotion evidence: ' + str(filename))
        try:
            result = json.loads((root / filename).read_text())
            if not isinstance(result, dict):
                raise ValueError('not an object')
            return result
        except (OSError, ValueError) as error:
            raise CallError('Invalid promotion evidence: ' + filename) from error

    def only(kind):
        found = [row for row in rows if row['kind'] == kind]
        if len(found) != 1:
            raise CallError('Promotion requires exactly one artifact: ' + kind)
        return found[0]

    image, update = only('iso'), only('update')
    expected_prefix = 'TrueNAS-SCALE-' + lock['system_version']
    if image['filename'] != expected_prefix + '-HarborOS.iso' or update['filename'] != expected_prefix + '.update':
        raise CallError('Stable upload filename differs from its verified artifact')
    for row in (image, update):
        sidecar = by_name.get(row['filename'] + '.sha256', {})
        if (sidecar.get('kind') != 'checksum'
                or (root / sidecar['filename']).read_text().strip() != row['sha256']):
            raise CallError('Stable upload checksum sidecar differs from its verified artifact')
    receipt = document(only('build_receipt')['filename'], 'build_receipt')
    expected_external = {**lock['packages'], **lock.get('runtime', {}).get('packages', {})}
    if (receipt.get('schema_version') != 1 or receipt.get('release_lock_sha256') != lock_sha
            or receipt.get('system_version') != lock['system_version']
            or receipt.get('source_date_epoch') != lock['source_date_epoch']
            or receipt.get('product') != 'nexus' or receipt.get('architecture') != 'amd64'
            or receipt.get('locked_external_packages') != expected_external):
        raise CallError('Native build receipt differs from the promotion lock')
    proof = document(only('artifact_readback')['filename'], 'artifact_readback')
    if (proof.get('update', {}).get('sha256') != update['sha256']
            or proof.get('iso', {}).get('sha256') != image['sha256']
            or proof.get('iso', {}).get('embedded_update_sha256') != update['sha256']
            or proof.get('iso', {}).get('exact_update_payload_match') is not True
            or proof.get('source_commit') != manifest['lineage']['update_producer_commit']
            or proof.get('iso', {}).get('source_commit') != manifest['lineage']['iso_producer_commit']
            or proof.get('runtime_source_commit') != manifest['lineage']['runtime_producer_commit']):
        raise CallError('Actual artifact readback does not bind the delivery bytes and producer lineage')
    for kind, filename in manifest['acceptance'].items():
        evidence = document(filename, 'acceptance_evidence')
        if (evidence.get('kind') != kind or evidence.get('status') != 'passed'
                or evidence.get('system_version') != lock['system_version']):
            raise CallError('Missing successful appliance acceptance: ' + kind)
    drivers = {'NVIDIA': {'nvidia'}, 'Intel': {'i915', 'xe'}, 'AMD': {'amdgpu'}}
    for vendor, filename in manifest['gpu_recognition'].items():
        evidence = document(filename, 'gpu_recognition')
        if (evidence.get('vendor') != vendor or evidence.get('driver') not in drivers[vendor]
                or evidence.get('kernel_release') != manifest['kernel_release']
                or not re.fullmatch(r'[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]', str(evidence.get('pci_id', '')))
                or evidence.get('pci_vendor_id') != {'NVIDIA': '10de', 'Intel': '8086', 'AMD': '1002'}[vendor]
                or not re.fullmatch(r'[0-9a-f]{4}', str(evidence.get('pci_device_id', '')))
                or evidence.get('discrete') is not True or type(evidence.get('vram_bytes')) is not int
                or evidence['vram_bytes'] <= 0 or not evidence.get('device_nodes')
                or any(evidence.get(key) is not True for key in
                       ('recognized', 'hardware_api_enumerated', 'service_user_access'))
                or any(evidence.get(key) is not False for key in ('software_device', 'reset_or_probe_errors'))):
            raise CallError('Missing physical GPU recognition evidence: ' + vendor)
        nodes = evidence['device_nodes']
        character = any(re.fullmatch(r'/dev/nvidia[0-9]+', str(node)) for node in nodes)
        render = any(re.fullmatch(r'/dev/dri/renderD[0-9]+', str(node)) for node in nodes)
        if (vendor == 'NVIDIA' and ('/dev/nvidiactl' not in nodes or not character)
                or vendor != 'NVIDIA' and not render):
            raise CallError('GPU recognition evidence lacks the vendor device nodes: ' + vendor)
    material = validate_nvidia_material(lock.get('drivers', {}).get('nvidia', {}))
    qualification = document(only('gpu_driver_materials')['filename'], 'gpu_driver_materials')
    driver_artifact = qualification.get('artifact', {})
    if (qualification.get('material') != material or qualification.get('payload_verified') is not True
            or qualification.get('kernel_release') != manifest['kernel_release']
            or qualification.get('hardware_recognition_tested') is not False
            or qualification.get('inference_qualified') is not False
            or driver_artifact.get('filename') != 'nvidia.raw'
            or not re.fullmatch(r'[0-9a-f]{64}', str(driver_artifact.get('sha256', '')))
            or type(driver_artifact.get('size_bytes')) is not int or driver_artifact['size_bytes'] <= 0
            or proof.get('update', {}).get('nvidia_extension_sha256') != driver_artifact['sha256']
            or proof.get('update', {}).get('nvidia_extension_size_bytes') != driver_artifact['size_bytes']):
        raise CallError('NVIDIA payload qualification differs from the pinned driver material')
    validate_ffmpeg_materials(lock['runtime']['packages']['harboros-ffmpeg'], root, rows)
    _verify_lineage(manifest['lineage'])
    return {'schema_version': 1, 'publishable': True, 'channel': 'stable',
            'system_version': lock['system_version'], 'release_lock_sha256': lock_sha,
            'lineage': dict(manifest['lineage']), 'artifacts_verified': len(rows),
            'inference_qualification_claimed': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release-lock', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--artifacts-dir', type=Path, required=True)
    parser.add_argument('--expected-version', required=True)
    args = parser.parse_args()
    try:
        result = validate_promotion(json.loads(args.manifest.read_text()), json.loads(args.release_lock.read_text()),
                                    args.artifacts_dir)
        if result['system_version'] != args.expected_version:
            raise CallError('Requested publication version differs from the verified artifacts')
    except (CallError, OSError, ValueError) as error:
        parser.exit(1, str(error) + '\n')
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
