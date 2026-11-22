"""Fail-closed build-material checks; these do not qualify a physical GPU."""
import hashlib
import json
from pathlib import Path
import re

from .exceptions import CallError


def validate_nvidia_material(material):
    version = str(material.get('current', ''))
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', version):
        raise CallError('NVIDIA requires an exact driver version')
    source = (f'https://us.download.nvidia.com/XFree86/Linux-x86_64/{version}/'
              f'NVIDIA-Linux-x86_64-{version}-no-compat32.run')
    if material.get('source_url') != source or not re.fullmatch(r'[0-9a-f]{64}', str(material.get('sha256', ''))):
        raise CallError('NVIDIA requires the exact official versioned source URL and SHA256')
    return material


def verify_nvidia_download(path, material):
    validate_nvidia_material(material)
    if Path(path).is_symlink() or not Path(path).is_file():
        raise CallError('NVIDIA installer must be a regular file')
    with Path(path).open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    if digest != material['sha256']:
        raise CallError('NVIDIA installer SHA256 differs from its reviewed material')


def _inside_payload(root, path):
    try:
        if not Path(path).resolve(strict=True).is_relative_to((Path(root) / 'usr').resolve(strict=True)):
            raise CallError(f'NVIDIA payload escapes staged /usr: {Path(path).name}')
    except (OSError, RuntimeError) as error:
        raise CallError(f'NVIDIA payload path cannot be resolved: {Path(path).name}') from error


def _elf(path, root):
    _inside_payload(root, path)
    if path.is_symlink() or not path.is_file():
        raise CallError(f'NVIDIA payload requires a regular ELF file: {path.name}')
    with path.open('rb') as stream:
        header = stream.read(20)
    if (len(header) < 20 or header[:6] != b'\x7fELF\x02\x01' or header[18:20] != b'\x3e\x00'
            or header[16:18] not in (b'\x02\x00', b'\x03\x00')):
        raise CallError(f'NVIDIA payload is not ELF64 amd64: {path.name}')


def verify_nvidia_payload(root, kernel, material, modinfo):
    """Inspect staged files and modinfo output without loading any driver."""
    validate_nvidia_material(material)
    root = Path(root)
    version = material['current']
    _elf(root / 'usr/bin/nvidia-smi', root)
    modules = {}
    for name in ('nvidia', 'nvidia-uvm', 'nvidia-modeset', 'nvidia-drm'):
        matches = [path for suffix in ('.ko', '.ko.xz', '.ko.gz', '.ko.zst')
                   for path in (root / 'usr/lib/modules' / kernel).rglob(name + suffix)]
        if len(matches) != 1 or matches[0].is_symlink() or not matches[0].is_file():
            raise CallError(f'NVIDIA requires one target-kernel module: {name}')
        _inside_payload(root, matches[0])
        observed = modinfo(matches[0], 'vermagic').strip()
        if not observed or observed.split()[0] != kernel:
            raise CallError(f'NVIDIA module vermagic differs from target kernel: {name}')
        module_version = modinfo(matches[0], 'version').strip()
        license_name = modinfo(matches[0], 'license').strip()
        if module_version != version or license_name != 'Dual MIT/GPL':
            raise CallError(f'NVIDIA module differs from the versioned open driver: {name}')
        modules[name] = {'path': matches[0].relative_to(root).as_posix(), 'vermagic': observed,
                         'version': module_version, 'license': license_name}
    firmware = sorted((root / 'usr/lib/firmware/nvidia' / version).glob('gsp_*.bin'))
    if (not firmware or any(path.is_symlink() or not path.is_file() or not path.stat().st_size
                            for path in firmware)):
        raise CallError('NVIDIA open modules require nonempty version-matched GSP firmware')
    for path in firmware:
        _inside_payload(root, path)
    libraries = []
    for name in ('libcuda.so.', 'libnvidia-ml.so.'):
        matches = [path for base in ('usr/lib', 'usr/lib64') for path in (root / base).rglob(name + version)]
        if not matches:
            raise CallError(f'NVIDIA requires version-matched userspace: {name}{version}')
        for path in matches:
            _elf(path, root)
            libraries.append(path.relative_to(root).as_posix())
    icd = root / 'usr/share/vulkan/icd.d/nvidia_icd.json'
    try:
        if icd.is_symlink():
            raise ValueError('symlink')
        descriptor = json.loads(icd.read_text())['ICD']
        library = descriptor['library_path']
        if Path(library).name != library or 'nvidia' not in library or not descriptor['api_version']:
            raise ValueError('invalid ICD')
        targets = [path for base in ('usr/lib', 'usr/lib64') for path in (root / base).rglob(library)]
        if not targets:
            raise ValueError('ICD library missing')
        # The versioned libraries above are checked as regular ELF files. A loader
        # alias may be a symlink, but it must not escape the staged /usr tree.
        for path in targets:
            target = path
            for _ in range(8):
                if not target.is_symlink():
                    break
                link = target.readlink()
                target = root / str(link).lstrip('/') if link.is_absolute() else target.parent / link
            target = target.resolve()
            if not target.is_relative_to((root / 'usr').resolve()):
                raise ValueError('ICD library escapes image')
            _elf(target, root)
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
        raise CallError('NVIDIA requires a retained /usr Vulkan ICD and matching library') from error
    return {'schema_version': 1, 'vendor': 'NVIDIA', 'driver_version': version,
            'material': dict(material), 'kernel_release': kernel, 'modules': modules,
            'firmware': [path.relative_to(root).as_posix() for path in firmware],
            'userspace_libraries': libraries, 'vulkan_icd': icd.relative_to(root).as_posix(),
            'payload_verified': True, 'hardware_recognition_tested': False,
            'inference_qualified': False}
