import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from scale_build.packages import dependency_relay as relay


class DependencyRelayTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.cache = self.root / 'cache'
        (self.cache / 'assets').mkdir(parents=True)
        (self.cache / 'repos').mkdir()
        self.payload = self.cache / 'assets/archive.tar.gz'
        self.payload.write_bytes(b'verified archive')
        self.url = 'https://github.com/example/source/releases/download/v1/archive.tar.gz'
        self.asset = {'url': self.url, 'path': 'assets/archive.tar.gz', 'sha256': relay.digest(self.payload)}
        self.data = {'schema_version': 1, 'allowed_packages': ['target'], 'repositories': [], 'gitlink_edges': [],
                     'assets': [self.asset], 'files': {}}
        self.enterContext(patch.dict(os.environ, {}, clear=True))
        self.write_manifest()

    def write_manifest(self):
        self.data['files'] = {path.relative_to(self.cache).as_posix(): {
            'sha256': relay.digest(path), 'size_bytes': path.stat().st_size,
        } for folder in ('repos', 'assets') for path in (self.cache / folder).rglob('*') if path.is_file()}
        (self.cache / relay.MANIFEST).write_text(json.dumps(self.data))
        self.expected = relay.digest(self.cache / relay.MANIFEST)

    def configure(self):
        os.environ.update(HARBOROS_BUILD_DEPENDENCY_CACHE=str(self.cache),
                          HARBOROS_BUILD_DEPENDENCY_MANIFEST_SHA256=self.expected)

    def test_disabled_does_not_change_environment_or_mount(self):
        run = Mock()
        relay.prepare('target', self.root / 'chroot', self.root, run)
        self.assertEqual(relay.environment('target'), {})
        run.assert_not_called()

    def test_partial_configuration_and_manifest_change_fail_closed(self):
        os.environ['HARBOROS_BUILD_DEPENDENCY_CACHE'] = str(self.cache)
        with self.assertRaisesRegex(ValueError, 'configured together'):
            relay.configured('target')
        self.configure()
        (self.cache / relay.MANIFEST).write_text('{}')
        with self.assertRaisesRegex(ValueError, 'SHA256'):
            relay.configured('target')

    def test_unlisted_package_retains_default_behavior(self):
        self.configure()
        self.assertEqual(relay.environment('unlisted'), {})

    def test_cache_tampering_or_extra_file_is_rejected_before_mount(self):
        self.configure()
        run = Mock()
        self.payload.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'SHA256'):
            relay.prepare('target', self.root / 'chroot', self.root, run)
        run.assert_not_called()
        self.payload.write_bytes(b'verified archive')
        (self.cache / 'assets/extra').write_bytes(b'extra')
        with self.assertRaisesRegex(ValueError, 'SHA256'):
            relay.verify_cache(self.cache, self.data)

    def test_symlink_and_path_escape_are_rejected(self):
        for path in ('../outside', '/absolute', 'assets/../outside', r'assets\outside'):
            with self.assertRaises(ValueError):
                relay.safe_path(self.cache, path)
        if os.name != 'nt':
            self.payload.unlink()
            self.payload.symlink_to(self.root / 'missing')
            with self.assertRaisesRegex(ValueError, 'Non-regular'):
                relay.verify_cache(self.cache, self.data)

    def test_enabled_package_gets_readonly_bind_and_private_path(self):
        self.configure()
        run = Mock()
        chroot = self.root / 'chroot'
        relay.prepare('target', chroot, self.root, run)
        target = str(chroot / relay.CACHE.lstrip('/'))
        self.assertEqual(run.call_args_list[0].args[0], ['mount', '--bind', str(self.cache), target])
        self.assertEqual(run.call_args_list[1].args[0], ['mount', '-o', 'remount,bind,ro', target])
        self.assertTrue(relay.environment('target')['PATH'].startswith(relay.TOOLS + ':'))
        receipt = json.loads((self.root / 'target.dependency-relay-inputs.json').read_text())
        self.assertEqual(receipt['manifest_sha256'], self.expected)

    def test_git_rewrites_are_limited_to_network_commands(self):
        data = {'repositories': [{'repository': 'https://github.com/example/repo', 'cache_path': 'repos/repo.git'}]}
        ordinary = relay.git_environment(['remote', 'get-url', 'origin'], data, self.cache)
        self.assertNotIn('GIT_CONFIG_COUNT', ordinary)
        clone = relay.git_environment(['-C', '/work', 'clone', 'https://github.com/example/repo'], data, self.cache)
        self.assertEqual(clone['GIT_ALLOW_PROTOCOL'], 'file')
        self.assertEqual(clone['GIT_CONFIG_COUNT'], '2')
        self.assertEqual(clone['GIT_CONFIG_GLOBAL'], relay.TOOLS + '/gitconfig')
        self.assertEqual(clone['GIT_CONFIG_VALUE_0'], 'https://github.com/example/repo')
        self.assertEqual(relay.git_command(['--git-dir=/repo', 'fetch', 'origin']), 'fetch')

    def test_git_url_prefix_and_path_traversal_do_not_match_allowlist(self):
        data = {'repositories': [{'repository': 'https://github.com/example/repo', 'cache_path': 'repos/repo.git'}]}
        for url in ('https://github.com/example/repo-unlisted', 'https://github.com/example/repo/../other.git',
                    'file:///unlisted/cache', 'https://github.com/unlisted/repo'):
            with self.assertRaisesRegex(ValueError, 'Unlisted'):
                relay.git_environment(['clone', url, '/tmp/output'], data, self.cache)
        with patch.object(relay.subprocess, 'run', return_value=Mock(
            returncode=0, stdout='https://github.com/example/repo-unlisted\n',
        )):
            with self.assertRaisesRegex(ValueError, 'remote'):
                relay.git_environment(['fetch', 'origin'], data, self.cache)
        with patch.object(relay.subprocess, 'run', return_value=Mock(
            returncode=0, stdout='submodule.child.url https://github.com/example/repo/../other.git\n',
        )):
            with self.assertRaisesRegex(ValueError, 'submodule'):
                relay.git_environment(['submodule', 'update', '--init'], data, self.cache)

    def test_inherited_unreviewed_git_configuration_is_rejected(self):
        os.environ.update(GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='url.file:///unexpected.insteadOf',
                          GIT_CONFIG_VALUE_0='https://github.com')
        with self.assertRaisesRegex(ValueError, 'Unreviewed'):
            relay.clean_git_env()

    def test_original_curl_shapes_and_wget_filename_are_preserved(self):
        source, output, asset = relay.download_request('curl', ['-LsSf', self.url], self.data, self.cache)
        self.assertEqual((source, output, asset), (self.payload, None, self.asset))
        self.assertEqual(relay.download_request(
            'curl', ['-sSfL', self.url, '-o', '/tmp/input'], self.data, self.cache,
        )[1], '/tmp/input')
        self.assertEqual(relay.download_request('wget', ['-q', self.url], self.data, self.cache)[1], 'archive.tar.gz')

    def test_unlisted_url_unreviewed_option_and_changed_asset_fail_closed(self):
        for args in (['https://github.com/unlisted/repo'], ['-H', 'Authorization: secret', self.url]):
            with self.assertRaises(ValueError):
                relay.download_request('curl', args, self.data, self.cache)
        self.payload.write_bytes(b'tampered')
        with self.assertRaisesRegex(ValueError, 'SHA256'):
            relay.download_request('curl', [self.url], self.data, self.cache)


@unittest.skipUnless(shutil.which('git'), 'Git is needed for object-closure validation')
class DependencyRelayGitTests(unittest.TestCase):
    def test_real_git_cache_and_origin_tampering(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            (root / 'assets').mkdir()
            repo = root / 'repos/example.git'
            repo.mkdir(parents=True)
            env = dict(os.environ, GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull)

            def git(*args, input_text=None):
                return subprocess.check_output(['git', '--git-dir', str(repo), *args], input=input_text,
                                               env=env, text=True, stderr=subprocess.PIPE).strip()

            git('init', '--bare')
            git('config', 'user.name', 'test')
            git('config', 'user.email', 'test@example.invalid')
            tree = git('mktree', input_text='')
            commit = git('commit-tree', tree, '-m', 'fixture')
            git('config', '--unset', 'user.name')
            git('config', '--unset', 'user.email')
            git('update-ref', 'refs/heads/main', commit)
            git('symbolic-ref', 'HEAD', 'refs/heads/main')
            git('remote', 'add', 'origin', 'https://github.com/example/repo')
            data = {'repositories': [{'repository': 'https://github.com/example/repo',
                                      'cache_path': 'repos/example.git',
                                      'refs': {'refs/heads/main': commit},
                                      'commits': {commit: {'tree': tree, 'gitlinks': {}}}}], 'assets': []}

            def snapshot():
                data['files'] = {path.relative_to(root).as_posix(): {
                    'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'size_bytes': path.stat().st_size,
                } for path in repo.rglob('*') if path.is_file()}

            snapshot()
            relay.verify_cache(root, data)
            git('remote', 'set-url', 'origin', 'https://github.com/wrong/repo')
            snapshot()
            with self.assertRaisesRegex(ValueError, 'origin differs'):
                relay.verify_cache(root, data)
