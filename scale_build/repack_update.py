"""Rebuild only an update envelope around a pinned, unchanged runtime image."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess

from .config import BUILDER_DIR
from .exceptions import CallError
from .image.manifest import build_manifest, build_release_manifest
from .product_profiles import release_lock
from .utils.run import run


ASSEMBLY_RECEIPT = 'assembly-receipt.json'
NATIVE_RECEIPT = 'build-receipt.json'


def _digest(path, algorithm='sha256'):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, algorithm).hexdigest()


def _read_member(image, name):
    return subprocess.run(['unsquashfs', '-cat', str(image), name],
                          check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout


def _member_digest(image, name):
    hashed = hashlib.sha256()
    with subprocess.Popen(['unsquashfs', '-cat', str(image), name],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
        while data := process.stdout.read(8 * 1024 * 1024):
            hashed.update(data)
        error = process.stderr.read()
        if process.wait():
            raise CallError('Unable to read packaged update member: ' + error.decode(errors='replace'))
    return hashed.hexdigest()


def _git(arguments):
    return subprocess.check_output(['git', '-c', f'safe.directory={BUILDER_DIR}', '-C', BUILDER_DIR,
                                    *arguments], stderr=subprocess.PIPE)


def _installer_from_git(destination):
    commit = _git(['rev-parse', 'HEAD']).decode().strip()
    if _git(['status', '--porcelain', '--untracked-files=no']).strip():
        raise CallError('Installer repack requires a clean committed producer')
    tree = _git(['rev-parse', 'HEAD:truenas_install']).decode().strip()
    for item in _git(['ls-tree', '-r', '-z', 'HEAD', 'truenas_install']).split(b'\0'):
        if not item:
            continue
        header, filename = item.split(b'\t', 1)
        mode, kind, blob = header.decode().split()
        relative = PurePosixPath(filename.decode())
        if (kind != 'blob' or mode not in ('100644', '100755') or relative.is_absolute()
                or '..' in relative.parts or relative.parts[0] != 'truenas_install'):
            raise CallError('Unexpected installer Git tree entry')
        path = destination / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_git(['cat-file', 'blob', blob]))
        path.chmod(int(mode, 8) & 0o777)
    if not (destination / 'truenas_install/harbor_native.py').is_file():
        raise CallError('Committed Harbor native installer is missing')
    return commit, tree


def repack_update(source_update, input_manifest, expected_input_manifest_sha256, output_dir):
    source_update, input_manifest, output_dir = map(Path, (source_update, input_manifest, output_dir))
    if (not re.fullmatch('[0-9a-f]{64}', expected_input_manifest_sha256)
            or input_manifest.is_symlink() or not input_manifest.is_file()
            or _digest(input_manifest) != expected_input_manifest_sha256):
        raise CallError('Repack input manifest SHA256 mismatch')
    expected = json.loads(input_manifest.read_text())
    if expected.get('schema_version') != 1:
        raise CallError('Unsupported repack input manifest')
    for name in ('update_sha256', 'inner_rootfs_sha256', 'native_receipt_sha256', 'release_lock_sha256'):
        if not re.fullmatch('[0-9a-f]{64}', expected[name]):
            raise CallError('Invalid pinned repack fingerprint: ' + name)
    for name in ('update_source_commit', 'inner_rootfs_source_commit', 'runtime_source_commit'):
        if not re.fullmatch('[0-9a-f]{40}', expected[name]):
            raise CallError('Invalid original producer source: ' + name)
        _git(['merge-base', '--is-ancestor', expected[name], 'HEAD'])
    for name in ('update_size_bytes', 'inner_rootfs_size_bytes', 'manifest_size_bytes'):
        if type(expected[name]) is not int or expected[name] <= 0:
            raise CallError('Invalid pinned repack size: ' + name)
    if (source_update.is_symlink() or not source_update.is_file()
            or source_update.stat().st_size != expected['update_size_bytes']
            or _digest(source_update) != expected['update_sha256']):
        raise CallError('Original update SHA256/size mismatch')
    if not output_dir.is_absolute() or output_dir.exists() or output_dir.is_symlink():
        raise CallError('Repack output must be a new absolute directory')
    lock = release_lock(required=True)
    lock_digest = hashlib.sha256(json.dumps(lock, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    if (lock_digest != expected['release_lock_sha256'] or lock['system_version'] != expected['system_version']
            or lock.get('scope', {}).get('service_migration') != expected['service_migration']):
        raise CallError('Pinned repack input differs from the selected immutable release lock')
    metadata = json.loads(_read_member(source_update, 'manifest.json'))
    if (metadata['version'] != expected['system_version'] or metadata['size'] != expected['manifest_size_bytes']
            or metadata['kernel_version'] != expected['kernel_version']
            or metadata.get('harboros', {}).get('service_migration') != expected['service_migration']):
        raise CallError('Original update metadata differs from the pinned repack input')
    native = _read_member(source_update, NATIVE_RECEIPT)
    native_digest = hashlib.sha256(native).hexdigest()
    native_sidecar = _read_member(source_update, NATIVE_RECEIPT + '.sha256')
    receipt = json.loads(native)
    if (native_digest != expected['native_receipt_sha256'] or native_sidecar.decode().strip() != native_digest
            or receipt['release_lock_sha256'] != lock_digest or receipt['system_version'] != metadata['version']
            or receipt['product'] != lock['product'] or receipt['architecture'] != lock['architecture']):
        raise CallError('Original native build receipt differs from its pinned runtime/lock')
    for name, data in ((NATIVE_RECEIPT, native), (NATIVE_RECEIPT + '.sha256', native_sidecar)):
        if hashlib.sha1(data).hexdigest() != metadata['checksums'][name]:
            raise CallError('Original native receipt is not bound to its update manifest')

    stage, release = output_dir / 'update', output_dir / 'release'
    stage.mkdir(parents=True)
    release.mkdir()
    rootfs = stage / 'rootfs.squashfs'
    with rootfs.open('xb') as destination:
        extracted = subprocess.run(['unsquashfs', '-cat', str(source_update), 'rootfs.squashfs'],
                                   stdout=destination, stderr=subprocess.PIPE)
    if (extracted.returncode or rootfs.stat().st_size != expected['inner_rootfs_size_bytes']
            or _digest(rootfs) != expected['inner_rootfs_sha256']
            or _digest(rootfs, 'sha1') != metadata['checksums']['rootfs.squashfs']):
        raise CallError('Original embedded runtime image fingerprint mismatch')
    if (_read_member(rootfs, 'etc/version').decode().strip() != metadata['version']
            or _read_member(rootfs, 'usr/share/harboros/' + NATIVE_RECEIPT) != native
            or _read_member(rootfs, 'usr/share/harboros/' + NATIVE_RECEIPT + '.sha256') != native_sidecar):
        raise CallError('Reused runtime version/native receipt differs from the original envelope')
    kernel_path = 'usr/lib/modules/' + expected['kernel_version']
    listing = subprocess.check_output(['unsquashfs', '-ls', str(rootfs), kernel_path], text=True)
    if 'squashfs-root/' + kernel_path not in listing.splitlines():
        raise CallError('Pinned kernel is missing from the reused runtime image')
    for directory in (stage, release):
        (directory / NATIVE_RECEIPT).write_bytes(native)
        (directory / (NATIVE_RECEIPT + '.sha256')).write_bytes(native_sidecar)
    source, tree = _installer_from_git(output_dir / 'installer-source')
    installer = output_dir / 'installer-source/truenas_install'
    unchanged = all(
        _digest(path, 'sha1') == metadata['checksums'].get('truenas_install/' + path.relative_to(installer).as_posix())
        for path in installer.rglob('*') if path.is_file()
    )
    if unchanged:
        raise CallError('Installer repack contains no committed installer changes')
    assembly = {
        'schema_version': 1, 'update_source_commit': source, 'installer_source_commit': source,
        'installer_git_tree': tree, 'system_version': metadata['version'],
        'runtime_source_commit': expected['runtime_source_commit'],
        'inner_rootfs_source_commit': expected['inner_rootfs_source_commit'],
        'inner_rootfs_sha256': expected['inner_rootfs_sha256'],
        'inner_rootfs_size_bytes': expected['inner_rootfs_size_bytes'],
        'native_receipt_sha256': native_digest, 'native_receipt_reused_unchanged': True,
        'source_update_commit': expected['update_source_commit'],
        'supersedes_update_sha256': expected['update_sha256'], 'release_lock_sha256': lock_digest,
        'manifest_size_bytes': metadata['size'], 'kernel_version': metadata['kernel_version'],
        'service_migration': expected['service_migration'],
        'input_manifest_sha256': expected_input_manifest_sha256,
        'packages_rebuilt': False, 'kernel_rebuilt': False, 'apt_run': False,
    }
    data = (json.dumps(assembly, sort_keys=True, indent=2) + '\n').encode()
    assembly_digest = hashlib.sha256(data).hexdigest()
    for directory in (stage, release):
        (directory / ASSEMBLY_RECEIPT).write_bytes(data)
        (directory / (ASSEMBLY_RECEIPT + '.sha256')).write_text(assembly_digest + '\n')
    build_manifest(update_dir=stage, installer_dir=installer, rootfs_metadata=metadata)
    result = release / ('TrueNAS-SCALE-' + metadata['version'] + '.update')
    run(['mksquashfs', str(stage), str(result), '-noD', '-noappend'])
    result_digest = _digest(result)
    (release / (result.name + '.sha256')).write_text(result_digest)
    build_release_manifest(result, result_digest, update_dir=stage, release_dir=release,
                           assembly_receipt=ASSEMBLY_RECEIPT)
    if (_member_digest(result, 'rootfs.squashfs') != expected['inner_rootfs_sha256']
            or _read_member(result, NATIVE_RECEIPT) != native
            or _read_member(result, NATIVE_RECEIPT + '.sha256') != native_sidecar
            or _read_member(result, ASSEMBLY_RECEIPT) != data):
        raise CallError('Repacked envelope does not contain the exact pinned runtime/new Git installer')
    for path in installer.rglob('*'):
        if path.is_file() and _read_member(
            result, 'truenas_install/' + path.relative_to(installer).as_posix()
        ) != path.read_bytes():
            raise CallError('Repacked installer differs from its committed Git tree')
    # The original pinned image and the checked envelope retain these bytes.
    # Remove only this successful assembly's large duplicate staging member.
    rootfs.unlink()
    return {'update': str(result), 'size_bytes': result.stat().st_size, 'sha256': result_digest,
            'assembly_receipt': str(release / ASSEMBLY_RECEIPT), 'assembly_receipt_sha256': assembly_digest,
            **assembly}
