from datetime import datetime
import hashlib
import json
import os
import shutil
import subprocess

from scale_build.build_receipts import RECEIPT_NAME, read_checked_json
from scale_build.exceptions import CallError
from scale_build.product_profiles import release_lock
from scale_build.utils.kernel import get_kernel_version
from scale_build.utils.paths import BUILDER_DIR, CHROOT_BASEDIR, RELEASE_DIR, UPDATE_DIR


RELEASE_MANIFEST = os.path.join(RELEASE_DIR, 'manifest.json')


def get_version():
    with open(os.path.join(CHROOT_BASEDIR, 'etc/version')) as f:
        return f.read().strip()


def build_manifest(*, update_dir=None, installer_dir=None, rootfs_metadata=None):
    update_dir = update_dir or UPDATE_DIR
    version = rootfs_metadata['version'] if rootfs_metadata is not None else get_version()

    size = rootfs_metadata['size'] if rootfs_metadata is not None else int(int(subprocess.run(
        ['du', '--block-size', '1', '-d', '0', '-x', CHROOT_BASEDIR],
        check=True, stdout=subprocess.PIPE, encoding='utf-8', errors='ignore',
    ).stdout.split()[0]) * 1.1)

    shutil.copytree(
        installer_dir or os.path.join(BUILDER_DIR, 'truenas_install'),
        os.path.join(update_dir, 'truenas_install'),
    )

    checksums = {}
    for root, dirs, files in os.walk(update_dir):
        for file in files:
            abspath = os.path.join(root, file)
            with open(abspath, 'rb') as f:
                # FIXME: before we release 25.04.0 proper, change this to sha256
                checksums[os.path.relpath(abspath, update_dir)] = hashlib.file_digest(f, 'sha1').hexdigest()

    manifest = {
        'date': datetime.utcnow().isoformat(),
        'version': version,
        'size': size,
        'checksums': checksums,
        'kernel_version': (rootfs_metadata['kernel_version'] if rootfs_metadata is not None else
                           get_kernel_version(CHROOT_BASEDIR)),
    }
    lock = release_lock()
    service_migration = (lock or {}).get('scope', {}).get('service_migration')
    if service_migration is not None:
        if service_migration != 'native-packages-v1':
            raise CallError('Unsupported Harbor service migration policy')
        manifest['harboros'] = {'service_migration': service_migration}
    with open(os.path.join(update_dir, 'manifest.json'), "w") as f:
        f.write(json.dumps(manifest))

    return version


def build_release_manifest(update_file, update_file_checksum, *, update_dir=None, release_dir=None,
                           assembly_receipt=None):
    release_manifest = os.path.join(release_dir, 'manifest.json') if release_dir is not None else RELEASE_MANIFEST
    update_dir = update_dir or UPDATE_DIR
    release_dir = release_dir or RELEASE_DIR
    with open(os.path.join(update_dir, 'manifest.json')) as f:
        manifest = json.load(f)

    _, receipt_digest = read_checked_json(os.path.join(release_dir, RECEIPT_NAME))
    result = {
        'filename': os.path.basename(update_file),
        'version': manifest['version'],
        'date': manifest['date'],
        'changelog': '',
        'checksum': update_file_checksum,
        'filesize': os.path.getsize(update_file),
        'build_receipt': {'filename': RECEIPT_NAME, 'sha256': receipt_digest},
    }
    if assembly_receipt is not None:
        _, assembly_digest = read_checked_json(os.path.join(release_dir, assembly_receipt))
        result['assembly_receipt'] = {'filename': assembly_receipt, 'sha256': assembly_digest}
    with open(release_manifest, 'w') as f:
        json.dump(result, f)


def get_image_version(vendor=None):
    if not os.path.exists(RELEASE_MANIFEST):
        raise CallError(f'{RELEASE_MANIFEST!r} does not exist')

    with open(RELEASE_MANIFEST) as f:
        vendor = f'-{vendor}' if vendor else ''
        return f"{json.load(f)['version']}{vendor}"


def update_file_path(version=None):
    return os.path.join(RELEASE_DIR, f'TrueNAS-SCALE-{version or get_image_version()}.update')


def update_file_checksum_path(version=None):
    return f'{update_file_path(version)}.sha256'
