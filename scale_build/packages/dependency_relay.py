"""Opt-in, checksum-bound transport for dependencies fetched inside build chroots.

The original fetch scripts and HTTPS origins are preserved. Only this chroot's
Git network operations and explicitly listed HTTP downloads use the local cache.
"""
import configparser
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import time
from urllib.parse import urljoin, urlsplit


CACHE = '/opt/harbor-build-dependency-cache'
TOOLS = '/opt/harbor-build-dependency-tools'
MANIFEST = 'transport-manifest.json'
AUDIT = '/var/log/harbor-build-dependency-relay.jsonl'
CONFIG_KEYS = {
    'core.repositoryformatversion', 'core.filemode', 'core.bare', 'core.symlinks', 'core.ignorecase',
    'core.logallrefupdates', 'core.precomposeunicode', 'remote.origin.url', 'remote.origin.fetch',
}


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def safe_path(root, relative):
    path = PurePosixPath(relative)
    if path.is_absolute() or '\\' in relative or any(part in ('', '.', '..') for part in relative.split('/')):
        raise ValueError('Unsafe dependency cache path')
    result = Path(root) / relative
    for current in (result, *result.parents):
        if current.is_symlink():
            raise ValueError('Linked dependency cache path')
        if current == Path(root):
            break
    return result


def clean_git_env():
    env = dict(os.environ)
    if env.get('GIT_CONFIG_PARAMETERS'):
        raise ValueError('Unreviewed inherited Git configuration')
    count = int(env.get('GIT_CONFIG_COUNT', '0'))
    if not 0 <= count <= 64:
        raise ValueError('Invalid inherited Git configuration count')
    for index in range(count):
        if env.get(f'GIT_CONFIG_KEY_{index}', '').lower() != 'safe.directory':
            raise ValueError('Unreviewed inherited Git configuration')
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT='0',
               GIT_ALLOW_PROTOCOL='file', GIT_NO_LAZY_FETCH='1')
    return env


def git_output(args):
    return subprocess.check_output(['git', *args], env=clean_git_env(), text=True, stderr=subprocess.PIPE).strip()


def load_manifest(root, expected):
    if not re.fullmatch('[0-9a-f]{64}', expected):
        raise ValueError('Dependency relay requires a pinned manifest SHA256')
    path = safe_path(root, MANIFEST)
    if not path.is_file() or digest(path) != expected:
        raise ValueError('Dependency relay manifest SHA256 mismatch')
    data = json.loads(path.read_text())
    if data.get('schema_version') != 1 or not data.get('allowed_packages') or not data.get('files'):
        raise ValueError('Invalid dependency relay manifest')
    return data


def configured(package):
    root = os.environ.get('HARBOROS_BUILD_DEPENDENCY_CACHE')
    expected = os.environ.get('HARBOROS_BUILD_DEPENDENCY_MANIFEST_SHA256')
    if not root and not expected:
        return None
    if not root or not expected:
        raise ValueError('Dependency relay cache and manifest SHA256 must be configured together')
    root = Path(root)
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise ValueError('Dependency relay cache must be an absolute real directory')
    data = load_manifest(root, expected)
    return (root, expected, data) if package in data['allowed_packages'] else None


def verify_cache(root, data):
    observed = set()
    for folder in ('repos', 'assets'):
        for path in safe_path(root, folder).rglob('*'):
            if path.is_symlink() or not (path.is_dir() or stat.S_ISREG(path.lstat().st_mode)):
                raise ValueError('Non-regular dependency cache entry')
            if path.is_file():
                relative = path.relative_to(root).as_posix()
                expected = data['files'].get(relative)
                if not expected or path.stat().st_size != expected['size_bytes'] or digest(path) != expected['sha256']:
                    raise ValueError('Dependency cache file SHA256 mismatch: ' + relative)
                observed.add(relative)
    if observed != set(data['files']):
        raise ValueError('Dependency cache file inventory differs')
    repositories = {row['repository']: row for row in data['repositories']}
    for url, row in repositories.items():
        if not re.fullmatch(r'https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', url):
            raise ValueError('Unexpected dependency Git origin')
        repo = safe_path(root, row['cache_path'])
        prefix = ['--git-dir', str(repo)]
        if git_output(prefix + ['rev-parse', '--is-bare-repository']) != 'true':
            raise ValueError('Dependency Git cache is not bare')
        config = git_output(prefix + ['config', '--no-includes', '--local', '--null', '--list'])
        if any(item.split('\n', 1)[0].lower() not in CONFIG_KEYS for item in config.rstrip('\0').split('\0')):
            raise ValueError('Unreviewed dependency Git configuration')
        if git_output(prefix + ['config', '--local', '--get', 'remote.origin.url']) != url:
            raise ValueError('Dependency Git origin differs')
        refs = dict(line.split() for line in
                    git_output(prefix + ['for-each-ref', '--format=%(refname) %(objectname)']).splitlines())
        # Annotated tags are compared by their peeled commit, without rewriting the tag.
        if set(refs) != set(row['refs']):
            raise ValueError('Dependency Git refs differ')
        for ref, commit in row['refs'].items():
            if git_output(prefix + ['rev-parse', ref + '^{commit}']) != commit:
                raise ValueError('Dependency Git commit differs')
        for commit, record in row['commits'].items():
            if git_output(prefix + ['rev-parse', commit + '^{tree}']) != record['tree']:
                raise ValueError('Dependency Git tree differs')
            links = {line.split('\t', 1)[1]: line.split()[2] for line in
                     git_output(prefix + ['ls-tree', '-r', commit]).splitlines() if line.startswith('160000 ')}
            if links != record['gitlinks']:
                raise ValueError('Dependency Git links differ')
            if links:
                modules = configparser.ConfigParser(interpolation=None)
                modules.read_string(git_output(prefix + ['show', commit + ':.gitmodules']))
                urls = {modules[section]['path']: modules[section]['url'] for section in modules.sections()}
                for path, target in links.items():
                    child = urljoin(url + '/', urls[path]) if urls[path].startswith('.') else urls[path]
                    if target not in repositories.get(child.removesuffix('.git'), {}).get('commits', {}):
                        raise ValueError('Dependency submodule object closure is incomplete')
        if any(path.name in ('alternates', 'http-alternates') or path.suffix == '.promisor'
               for path in repo.rglob('*')):
            raise ValueError('Dependency Git cache is not self-contained')
        if any(path.is_file() and not path.name.endswith('.sample') for path in (repo / 'hooks').glob('*')):
            raise ValueError('Dependency Git hooks are forbidden')
        git_output(prefix + ['fsck', '--full', '--strict'])
    for asset in data['assets']:
        if data['files'].get(asset['path'], {}).get('sha256') != asset['sha256']:
            raise ValueError('Dependency asset differs from its file receipt')


def prepare(package, chroot, logs, run):
    settings = configured(package)
    if settings is None:
        return
    root, expected, data = settings
    verify_cache(root, data)
    target = Path(chroot) / CACHE.lstrip('/')
    target.mkdir(parents=True, exist_ok=False)
    run(['mount', '--bind', str(root), str(target)])
    run(['mount', '-o', 'remount,bind,ro', str(target)])
    tools = Path(chroot) / TOOLS.lstrip('/')
    tools.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(__file__, tools / 'transport.py')
    (tools / 'settings.json').write_text(json.dumps({'manifest_sha256': expected}))
    # Git's local upload-pack does not retain the client's command-scope config.
    # Use an isolated chroot-only config for these verified, read-only bare repos.
    (tools / 'gitconfig').write_text('[safe]\n' + ''.join(
        '    directory = ' + CACHE + '/' + row['cache_path'] + '\n' for row in data['repositories']
    ))
    for name in ('git', 'curl', 'wget'):
        path = tools / name
        path.write_text(f'#!/bin/sh\nexec /usr/bin/python3 {TOOLS}/transport.py {name} "$@"\n')
        path.chmod(0o755)
    receipt = {'package': package, 'manifest_sha256': expected, 'verified_files': len(data['files']),
               'repositories': data['repositories'], 'gitlink_edges': data['gitlink_edges'], 'assets': data['assets']}
    (Path(logs) / (package + '.dependency-relay-inputs.json')).write_text(json.dumps(receipt, sort_keys=True))


def environment(package):
    if configured(package):
        return {'PATH': TOOLS + ':/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'}
    return {}


def save_audit(package, chroot, logs):
    path = Path(chroot) / AUDIT.lstrip('/')
    if configured(package) and path.exists():
        shutil.copyfile(path, Path(logs) / (package + '.dependency-relay.jsonl'))


def git_command(args):
    index = 0
    while index < len(args):
        if args[index] in ('-C', '-c', '--git-dir', '--work-tree', '--namespace', '--config-env'):
            index += 2
        elif args[index].startswith('-'):
            index += 1
        else:
            return args[index]
    return None


def git_environment(args, data, root):
    env = clean_git_env()
    env['GIT_CONFIG_GLOBAL'] = TOOLS + '/gitconfig'
    command = git_command(args)
    if command in ('clone', 'fetch', 'ls-remote', 'submodule'):
        allowed = {url for row in data['repositories'] for url in (row['repository'], row['repository'] + '.git')}
        explicit = [arg for arg in args if '://' in arg]
        if any(url not in allowed for url in explicit):
            raise ValueError('Unlisted dependency Git URL')
        position = args.index(command)
        prefix, remaining = args[:position], args[position + 1:]
        if command == 'clone' and len(explicit) != 1:
            raise ValueError('Dependency clone requires an exact listed HTTPS URL')
        if command in ('fetch', 'ls-remote') and not explicit:
            if any(arg.startswith('-') for arg in remaining):
                raise ValueError('Unsupported named-remote dependency transport option')
            remote = remaining[0] if remaining else 'origin'
            result = subprocess.run(['/usr/bin/git', *prefix, 'remote', 'get-url', '--all', remote],
                                    env=env, capture_output=True, text=True)
            if result.returncode or not result.stdout.strip() or any(
                url not in allowed for url in result.stdout.splitlines()
            ):
                raise ValueError('Unlisted dependency Git remote')
        if command == 'submodule' and 'update' in remaining:
            result = subprocess.run(['/usr/bin/git', *prefix, 'config', '--file', '.gitmodules',
                                     '--get-regexp', r'^submodule\..*\.url$'], env=env, capture_output=True, text=True)
            if result.returncode or any(line.split(None, 1)[1] not in allowed for line in result.stdout.splitlines()):
                raise ValueError('Unlisted dependency submodule URL')
        index = int(env.get('GIT_CONFIG_COUNT', '0'))
        for row in data['repositories']:
            local = safe_path(root, row['cache_path']).as_uri()
            for original in (row['repository'], row['repository'] + '.git'):
                env[f'GIT_CONFIG_KEY_{index}'] = 'url.' + local + '.insteadOf'
                env[f'GIT_CONFIG_VALUE_{index}'] = original
                index += 1
        env['GIT_CONFIG_COUNT'] = str(index)
    return env


def download_request(tool, args, data, root):
    urls = [value for value in args if value.startswith(('https://', 'http://'))]
    matches = [row for row in data['assets'] if urls == [row['url']]]
    if len(matches) != 1:
        raise ValueError('Unlisted dependency download')
    asset = matches[0]
    source = safe_path(root, asset['path'])
    if digest(source) != asset['sha256']:
        raise ValueError('Dependency download SHA256 mismatch')
    output = None
    index = 0
    while index < len(args):
        arg = args[index]
        if ((arg in ('-o', '--output') and tool == 'curl') or
                (arg in ('-O', '--output-document') and tool == 'wget')):
            index += 1
            output = args[index]
        elif arg == asset['url']:
            pass
        elif (not re.fullmatch(r'-[sSfL]+', arg)) if tool == 'curl' else (arg != '-q'):
            raise ValueError('Unsupported cached dependency download option')
        index += 1
    if tool == 'wget' and output is None:
        output = Path(urlsplit(asset['url']).path).name
        if Path(output).exists():
            raise ValueError('Refusing to replace an existing dependency download')
    return source, output, asset


def transport(tool, args):
    expected = json.loads((Path(TOOLS) / 'settings.json').read_text())['manifest_sha256']
    data = load_manifest(Path(CACHE), expected)
    record = {'tool': tool, 'manifest_sha256': expected, 'epoch': time.time()}
    if tool == 'git':
        env = git_environment(args, data, Path(CACHE))
        record['operation'] = git_command(args)
        command = ['/usr/bin/git', *args]
    else:
        source, output, asset = download_request(tool, args, data, Path(CACHE))
        record.update(url=asset['url'], sha256=asset['sha256'])
    with open(AUDIT, 'a') as stream:
        stream.write(json.dumps(record) + '\n')
    if tool == 'git':
        return subprocess.call(command, env=env)
    if output in (None, '-'):
        with source.open('rb') as stream:
            shutil.copyfileobj(stream, sys.stdout.buffer)
    else:
        with source.open('rb') as stream, open(output, 'wb') as destination:
            shutil.copyfileobj(stream, destination)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(transport(sys.argv[1], sys.argv[2:]))
    except (ValueError, KeyError, IndexError) as error:
        print('Dependency relay refused request: ' + str(error), file=sys.stderr)
        raise SystemExit(1)
