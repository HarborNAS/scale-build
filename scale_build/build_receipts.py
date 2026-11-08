"""Checksum-bound evidence for the sources and packages used by an appliance build."""
import email.parser
import hashlib
import json
from pathlib import Path
import re
import shlex

from scale_build.exceptions import CallError
from scale_build.product_profiles import SOURCE_NAMES
from scale_build.utils.run import run

RECEIPT_NAME = 'build-receipt.json'
SOURCE_RECEIPT_SUFFIX = '.build-receipt.json'
LEGAL_NOTICE_PACKAGES = frozenset(('harboros-beacon', 'harboros-im-gate', 'harborlink', 'harboros-ffmpeg'))
CONTROL_FIELDS = ('Package', 'Version', 'Architecture', 'Source', 'Depends', 'Pre-Depends', 'Built-Using')
INVENTORY_FIELDS = ('Package', 'Version', 'Architecture', 'Status', 'source:Package', 'Depends', 'Pre-Depends')
QUERY_FORMAT = '\t'.join('$' + '{' + field + '}' for field in INVENTORY_FIELDS) + '\n'


def checksum(path):
    with Path(path).open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def package_fields(path):
    fields = email.parser.Parser().parsestr(run(['dpkg-deb', '--field', str(path)], log=False).stdout)
    result = {field.lower().replace('-', '_'): fields.get(field, '') for field in CONTROL_FIELDS}
    if not all(result[field] for field in ('package', 'version', 'architecture')):
        raise CallError(f'Missing Debian identity fields: {Path(path).name}')
    return result


def installed_packages(root):
    output = run(['chroot', str(root), 'dpkg-query', '-W', '--showformat=' + QUERY_FORMAT], log=False).stdout
    packages = []
    for line in output.splitlines():
        fields = line.split('\t')
        if len(fields) != len(INVENTORY_FIELDS):
            raise CallError('Cannot read the actual dpkg package/dependency inventory')
        package, version, architecture, status, source, depends, pre_depends = fields
        if status != 'install ok installed':
            continue
        if not all((package, version, architecture)):
            raise CallError('Incomplete installed Debian package identity')
        packages.append(dict(package=package, version=version, architecture=architecture,
                             source=source, depends=depends, pre_depends=pre_depends))
    if not packages:
        raise CallError('The actual dpkg package inventory is empty')
    keys = [(item['package'], item['architecture']) for item in packages]
    if len(keys) != len(set(keys)):
        raise CallError('Ambiguous installed Debian package identity')
    return sorted(packages, key=lambda item: (item['package'], item['architecture']))


def build_tool_versions(root, source_directory):
    commands = {
        'node': ['node', '--version'],
        'yarn': ['yarn', '--version'],
        'python3': ['python3', '--version'],
        'dpkg-deb': ['dpkg-deb', '--version'],
        'gcc': ['gcc', '-dumpfullversion', '-dumpversion'],
    }
    versions = {}
    for name, command in commands.items():
        script = ('cd ' + shlex.quote('/' + source_directory.lstrip('/'))
                  + ' && exec env COREPACK_ENABLE_NETWORK=0 ' + shlex.join(command))
        result = run(['chroot', str(root), '/bin/sh', '-c', script], log=False, check=False)
        lines = (result.stdout or '').splitlines()
        versions[name] = lines[0].strip()[:500] if result.returncode == 0 and lines else None
    return versions


def write_checked_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(data, sort_keys=True, indent=2) + '\n').encode()
    path.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    Path(str(path) + '.sha256').write_text(digest + '\n', encoding='utf-8')
    return digest


def read_checked_json(path):
    path = Path(path)
    digest_path = Path(str(path) + '.sha256')
    try:
        if path.is_symlink() or digest_path.is_symlink() or path.stat().st_size > 16 * 1024 * 1024:
            raise CallError(f'Invalid build receipt file: {path.name}')
        expected = digest_path.read_text(encoding='utf-8').strip()
        if not re.fullmatch(r'[0-9a-f]{64}', expected) or checksum(path) != expected:
            raise CallError(f'Build receipt checksum mismatch: {path.name}')
        data = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(data, dict):
            raise CallError(f'Invalid build receipt payload: {path.name}')
        return data, expected
    except (OSError, ValueError) as error:
        raise CallError(f'Missing or invalid build receipt: {path.name}') from error


def record_source_build(target, source_name, observed, filenames, package_dir, build_root, version, declared,
                        source_directory, source_epoch=None):
    if source_name not in SOURCE_NAMES:
        return
    if not re.fullmatch(r'[0-9a-f]{40}', str(observed.get('sha', ''))) or not observed.get('url'):
        raise CallError('A build receipt requires the actual source repository and full commit')
    artifacts = []
    for filename in sorted(filenames):
        if Path(filename).name != filename:
            raise CallError('Build artifact filenames must not contain paths')
        path = Path(package_dir) / filename
        if path.is_symlink() or not path.is_file():
            raise CallError(f'Missing regular build artifact: {filename}')
        artifacts.append(dict(filename=filename, sha256=checksum(path), fields=package_fields(path)))
    if not artifacts:
        raise CallError(f'No built Debian artifacts for locked source target {target}')
    receipt = dict(schema_version=1, build_target=target, system_version=version, source_date_epoch=source_epoch,
                   source=dict(name=source_name, commit=observed['sha'], repository=observed['url']),
                   declared_build_dependencies=sorted(declared),
                   build_dependencies=installed_packages(build_root),
                   build_tools=build_tool_versions(build_root, source_directory), artifacts=artifacts)
    return write_checked_json(Path(package_dir) / (target + SOURCE_RECEIPT_SUFFIX), receipt)


def source_build_receipt_matches(target, source_name, commit, package_dir, lock):
    try:
        receipt, _ = read_checked_json(Path(package_dir) / (target + SOURCE_RECEIPT_SUFFIX))
    except CallError:
        return False
    source = receipt.get('source', {})
    pin = lock['components'][SOURCE_NAMES[source_name]]
    return (source.get('commit') == pin['commit']
            and source.get('repository', '').removesuffix('.git') == pin['repository'].removesuffix('.git')
            and receipt.get('schema_version') == 1 and receipt.get('build_target') == target
            and source.get('name') == source_name and source.get('commit') == commit
            and receipt.get('system_version') == lock['system_version']
            and receipt.get('source_date_epoch') == lock['source_date_epoch'])


def locked_build_targets(manifest):
    targets = {}
    for source in manifest['sources']:
        if source['name'] not in SOURCE_NAMES:
            continue
        targets[source['name']] = source['name']
        for child in source.get('subpackages', []):
            targets[child['name']] = source['name']
    if set(targets.values()) != set(SOURCE_NAMES):
        raise CallError('Both locked WebUI and middleware sources require build receipts')
    return targets


def validate_source_builds(lock, manifest, package_dir, inventory):
    receipts = []
    installed = {(item['package'], item['architecture']): item for item in inventory}
    observed_packages = set()
    for target, source_name in sorted(locked_build_targets(manifest).items()):
        path = Path(package_dir) / (target + SOURCE_RECEIPT_SUFFIX)
        receipt, digest = read_checked_json(path)
        source = receipt.get('source', {})
        pin = lock['components'][SOURCE_NAMES[source_name]]
        if (receipt.get('schema_version') != 1 or receipt.get('build_target') != target
                or receipt.get('system_version') != lock['system_version']
                or receipt.get('source_date_epoch') != lock['source_date_epoch'] or source.get('name') != source_name
                or source.get('commit') != pin['commit']
                or source.get('repository', '').removesuffix('.git') != pin['repository'].removesuffix('.git')):
            raise CallError(f'Build receipt source/version differs from the release lock: {target}')
        if not isinstance(receipt.get('build_dependencies'), list) or not receipt['build_dependencies']:
            raise CallError(f'Missing actual build dependency inventory: {target}')
        artifacts = receipt.get('artifacts')
        if not isinstance(artifacts, list) or not artifacts:
            raise CallError(f'Missing built Debian artifacts: {target}')
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                raise CallError(f'Invalid built artifact receipt: {target}')
            filename = artifact.get('filename', '')
            if not filename or Path(filename).name != filename:
                raise CallError(f'Invalid build artifact filename: {target}')
            deb = Path(package_dir) / filename
            if deb.is_symlink() or not deb.is_file() or checksum(deb) != artifact.get('sha256'):
                raise CallError(f'Built Debian SHA256 mismatch: {filename}')
            fields = package_fields(deb)
            if fields != artifact.get('fields') or fields['architecture'] not in (lock['architecture'], 'all'):
                raise CallError(f'Built Debian fields/architecture differ from receipt: {filename}')
            actual = installed.get((fields['package'], fields['architecture']))
            if actual:
                if actual['version'] != fields['version']:
                    raise CallError(f'Installed package differs from built artifact: {fields["package"]}')
                observed_packages.add(fields['package'])
        receipts.append(dict(receipt_sha256=digest, **receipt))
    required = {'truenas-webui', 'truenas'}
    if 'middlewared' in locked_build_targets(manifest):
        required.add('middlewared')
    if not required.issubset(observed_packages):
        raise CallError('Rootfs is missing locked built packages: ' + ', '.join(sorted(required - observed_packages)))
    return receipts


def write_rootfs_build_receipt(root, package_dir, release_dir, lock, manifest):
    inventory = installed_packages(root)
    sources = validate_source_builds(lock, manifest, package_dir, inventory)
    installed = {(item['package'], item['architecture']): item for item in inventory}
    external = {}
    for name, pin in {**lock['packages'], **lock.get('runtime', {}).get('packages', {})}.items():
        version = pin.get('version', pin.get('deb_version'))
        actual = installed.get((name, pin['architecture']))
        if actual is None or actual['version'] != version:
            raise CallError(f'Rootfs does not contain the locked external package: {name}')
        external[name] = dict(pin)
    lock_digest = hashlib.sha256(json.dumps(lock, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    receipt = dict(schema_version=1, product=lock['product'], architecture=lock['architecture'],
                   system_version=lock['system_version'], source_date_epoch=lock['source_date_epoch'],
                   release_lock_sha256=lock_digest,
                   source_builds=sources, locked_external_packages=external, rootfs_packages=inventory)
    output = Path(root) / 'usr/share/harboros' / RECEIPT_NAME
    write_checked_json(output, receipt)
    write_checked_json(Path(release_dir) / RECEIPT_NAME, receipt)
    return receipt
