"""Consumer contract for the independently owned HarborBeacon FFmpeg producer."""
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import unquote, urlparse

from .exceptions import CallError


def validate_ffmpeg_materials(pin, directory, artifacts):
    root = Path(directory).resolve()
    version = str(pin.get('deb_version', ''))
    package_url = urlparse(str(pin.get('url_template', '')))
    expected_filename = f'harboros-ffmpeg_{version}_linux_amd64.deb'
    if (pin.get('architecture') != 'amd64'
            or not version or not re.fullmatch(r'[0-9][0-9A-Za-z.+~:-]*', version)
            or not re.fullmatch(r'[0-9a-f]{64}', str(pin.get('sha256', '')))
            or package_url.scheme != 'https'
            or package_url.hostname not in ('github.com', 'update.harboros.ai')
            or package_url.username or package_url.password or package_url.query or package_url.fragment
            or unquote(package_url.path.rsplit('/', 1)[-1]) != expected_filename):
        raise CallError('FFmpeg requires an exact versioned amd64 HTTPS Debian artifact')
    material = pin.get('materials', {})
    producer, upstream, opus = (material.get(key, {}) for key in ('producer', 'upstream', 'libopus'))
    repository = urlparse(str(producer.get('repository', '')))
    if (not re.fullmatch(r'[0-9a-f]{40}', str(producer.get('commit', '')))
            or repository.scheme != 'https' or repository.hostname != 'github.com'
            or repository.username or repository.password or repository.query or repository.fragment
            or not re.fullmatch(r'/[^/]+/HarborBeacon(?:\.git)?', repository.path)
            or upstream.get('version') != '9.1'
            or upstream.get('url') != 'https://ffmpeg.org/releases/ffmpeg-9.1.tar.xz'
            or type(material.get('source_date_epoch')) is not int or material['source_date_epoch'] <= 0
            or not re.fullmatch(r'.+@sha256:[0-9a-f]{64}', str(material.get('toolchain', {}).get('image', '')))
            or not material.get('configure_flags') or '--enable-libopus' not in material['configure_flags']):
        raise CallError('FFmpeg 9.1 requires immutable producer, upstream, libopus, toolchain and configure materials')
    for source in (upstream, opus):
        url = urlparse(str(source.get('url', '')))
        if (url.scheme != 'https' or url.username or url.password or url.query or url.fragment
                or not source.get('version') or not re.fullmatch(r'[0-9a-f]{64}', str(source.get('sha256', '')))):
            raise CallError('FFmpeg source materials require credential-free HTTPS URLs and SHA256')
    rows = {row['filename']: row for row in artifacts}
    documents = {}
    for kind in ('sbom', 'provenance'):
        expected = material.get('artifacts', {}).get(kind, {})
        filename = expected.get('filename')
        if (filename not in rows or rows[filename]['kind'] != 'ffmpeg_' + kind
                or rows[filename]['sha256'] != expected.get('sha256')):
            raise CallError(f'FFmpeg requires a checksum-bound {kind} artifact')
        path = root / filename
        try:
            safe = (isinstance(filename, str) and Path(filename).name == filename
                    and path.resolve(strict=True).is_relative_to(root))
        except (OSError, RuntimeError):
            safe = False
        if (not safe or path.is_symlink() or not path.is_file()
                or path.stat().st_size > 16 * 1024 * 1024):
            raise CallError(f'Invalid FFmpeg {kind} file')
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected['sha256']:
            raise CallError(f'FFmpeg {kind} checksum mismatch')
        documents[kind] = json.loads(raw)
    sbom = documents['sbom']
    if sbom.get('spdxVersion') != 'SPDX-2.3' or sbom.get('dataLicense') != 'CC0-1.0':
        raise CallError('FFmpeg SBOM requires SPDX-2.3 and CC0-1.0 document metadata')
    for name, source in (('ffmpeg', upstream), ('opus', opus)):
        matches = [row for row in sbom.get('packages', []) if row.get('name', '').lower() == name]
        if len(matches) != 1:
            raise CallError(f'FFmpeg SBOM requires one {name} source package')
        row = matches[0]
        if (row.get('versionInfo') != source['version'] or row.get('downloadLocation') != source['url']
                or row.get('licenseDeclared') in (None, '', 'NOASSERTION')
                or not any(item.get('algorithm') == 'SHA256' and item.get('checksumValue') == source['sha256']
                           for item in row.get('checksums', []))):
            raise CallError(f'FFmpeg SBOM {name} source/license differs from its materials')
    provenance = documents['provenance']
    subject = f'harboros-ffmpeg_{version}_linux_amd64.deb'
    if (provenance.get('_type') != 'https://in-toto.io/Statement/v1'
            or provenance.get('predicateType') != 'https://slsa.dev/provenance/v1'
            or not any(row.get('name') == subject and row.get('digest', {}).get('sha256') == pin.get('sha256')
                       for row in provenance.get('subject', []))):
        raise CallError('FFmpeg provenance does not bind the exact Debian artifact')
    predicate = provenance.get('predicate', {})
    definition = predicate.get('buildDefinition', {})
    dependencies = definition.get('resolvedDependencies', [])
    for source in (upstream, opus):
        if not any(row.get('uri') == source['url'] and row.get('digest', {}).get('sha256') == source['sha256']
                   for row in dependencies):
            raise CallError('FFmpeg provenance source dependency differs from its materials')
    source_bound = any(row.get('uri') == producer['repository']
                       and row.get('digest', {}).get('gitCommit') == producer['commit'] for row in dependencies)
    if (not source_bound
            or predicate.get('runDetails', {}).get('builder', {}).get('id') != material['toolchain']['image']
            or definition.get('externalParameters', {}).get('source_date_epoch') != material['source_date_epoch']
            or definition.get('externalParameters', {}).get('configure_flags') != material['configure_flags']):
        raise CallError('FFmpeg provenance producer/toolchain/configuration differs from its materials')
    return material
