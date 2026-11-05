import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from scale_build.product_profiles import (selected_profile, apply_release_lock,
    release_lock, validate_release_lock, verify_download, verify_source_pin, verify_unprovisioned_image, assert_publishable, github_asset_request)
from scale_build.exceptions import CallError


def fixture():
    names={"beacon":"Bean-Harbor-Engineering/HarborBeacon", "gate":"Bean-Harbor-Engineering/HarborGate",
        "webui":"HarborNAS/webui", "middleware":"HarborNAS/middleware",
        "harborlink":"Bean-Harbor-Engineering/HarborLink", "runtime":"Bean-Harbor-Engineering/HarborBeacon"}
    result={"schema_version":1,"product":"nexus","architecture":"amd64","system_version":"26.04.0-NEXUS-RC1",
        "components":{name:{"repository":"https://github.com/"+repository,"commit":"a"*40,"ref":"26.04.0"}
            for name,repository in names.items()},"runtime":{"mode":"embedded","packages":{"harboros-ffmpeg":{"deb_version":"1.0.0","architecture":"amd64","sha256":"c"*64,"url_template":"https://update.harboros.ai/deb/harboros-ffmpeg_1.0.0_linux_amd64.deb"}}},"packages":{}}
    for package in ("harboros-beacon","harboros-im-gate","harborlink"):
        result["packages"][package]={"version":"1.1.0+nexus.amd64.20261105.1",
            "architecture":"amd64","source_commit":"a"*40,"sha256":"b"*64,
            "product_profile":"nexus-amd64",
            "url":"https://github.com/Bean-Harbor-Engineering/"+{'harboros-beacon':'HarborBeacon','harboros-im-gate':'HarborGate','harborlink':'HarborLink'}[package]+"/releases/download/v1.1.0+nexus.amd64.20261105.1/"+package+"_1.1.0+nexus.amd64.20261105.1_linux_amd64.deb"}
    return result


class ProductProfileTests(unittest.TestCase):
    def test_correct_canonical_repositories_and_versions_reach_manifest(self):
        lock=fixture()
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/"release.json";path.write_text(json.dumps(lock),encoding="utf-8")
            with patch.dict(os.environ,{"HARBOROS_PRODUCT":"nexus","HARBOROS_ARCHITECTURE":"amd64","HARBOROS_RELEASE_MANIFEST":str(path)}):
                original={"sources":[{"name":"truenas_webui","repo":"old","branch":"master"}],"external-packages":{}}
                result=apply_release_lock(original)
                self.assertEqual(result['sources'][0]['repo'],'https://github.com/HarborNAS/webui')
                self.assertEqual(result['sources'][0]['branch'],'26.04.0')
                self.assertIn('/HarborBeacon/releases/',result['external-packages']['harboros-beacon']['url_template'])
                self.assertIn('/HarborGate/releases/',result['external-packages']['harboros-im-gate']['url_template'])
                self.assertEqual(result['external-packages']['harboros-im-gate']['sha256'],'b'*64)
                self.assertEqual(original['external-packages'],{})
                verify_source_pin('truenas_webui','a'*40,'https://github.com/HarborNAS/webui.git')
                with self.assertRaises(CallError):verify_source_pin('truenas_webui','c'*40,'https://github.com/HarborNAS/webui')

    def test_wrong_arch_old_mutable_version_and_unpinned_sources_fail(self):
        for mutate in (lambda x:x.update(architecture='riscv64'),
            lambda x:x['packages']['harboros-beacon'].update(version='1.0.0'),
            lambda x:x['packages']['harboros-im-gate'].update(version='1.1.0+n2.riscv64.1'),
            lambda x:x['components']['beacon'].update(commit='master'),
            lambda x:x['packages']['harboros-beacon'].update(sha256=None),
            lambda x:x['packages']['harboros-beacon'].update(source_commit='b'*40),
            lambda x:x['runtime'].update(packages={})):
            value=fixture();mutate(value)
            with self.assertRaises(CallError):validate_release_lock(value)

    def test_runtime_package_cannot_overwrite_a_component_or_guess_harborlink(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'release.json'
            with patch.dict(os.environ,{'HARBOROS_PRODUCT':'nexus','HARBOROS_ARCHITECTURE':'amd64','HARBOROS_RELEASE_MANIFEST':str(path)}):
                value=fixture();value['runtime']['packages']={'harboros-beacon':{'sha256':'c'*64,'deb_version':'1.0.0','url_template':'https://github.com/old/pkg.deb'}}
                path.write_text(json.dumps(value))
                with self.assertRaises(CallError):apply_release_lock({'sources':[]})
                value=fixture();value['packages']['harborlink'].pop('url');path.write_text(json.dumps(value))
                with self.assertRaises(CallError):apply_release_lock({'sources':[]})
                value=fixture();value['packages']['harborlink']['product_profile']='n2-riscv64';path.write_text(json.dumps(value))
                with self.assertRaises(CallError):apply_release_lock({'sources':[]})

    def test_pinned_checkout_reproduces_commit_after_branch_moves(self):
        from scale_build.product_profiles import checkout_pinned_source
        import subprocess
        with tempfile.TemporaryDirectory() as folder:
            repository=Path(folder)/'origin';repository.mkdir()
            def git(*args):return subprocess.check_output(['git','-C',str(repository),*args],stderr=subprocess.DEVNULL,text=True).strip()
            git('init');git('config','user.name','Fixture');git('config','user.email','fixture@example.invalid')
            path=repository/'source.txt';path.write_text('old');git('add','.');git('commit','-m','first');first=git('rev-parse','HEAD')
            path.write_text('new');git('commit','-am','second')
            checkout=Path(folder)/'build'
            checkout_pinned_source(str(checkout),str(repository),first,['git'])
            self.assertEqual((checkout/'source.txt').read_text(),'old')
            (checkout/'source.txt').write_text('local changes')
            with self.assertRaises(CallError):checkout_pinned_source(str(checkout),str(repository),first,['git'])

    def test_download_hash_fails_before_any_install(self):
        import hashlib
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'package.deb';path.write_bytes(b'actual downloaded bytes')
            verify_download(path,hashlib.sha256(path.read_bytes()).hexdigest())
            with self.assertRaises(CallError):verify_download(path,'0'*64)

    def test_missing_lock_and_cross_product_build_are_rejected(self):
        with patch.dict(os.environ,{"HARBOROS_PRODUCT":"nexus","HARBOROS_ARCHITECTURE":"amd64","HARBOROS_RELEASE_MANIFEST":""}):
            with self.assertRaises(CallError):release_lock(required=True)
        with patch.dict(os.environ,{"HARBOROS_PRODUCT":"n2","HARBOROS_ARCHITECTURE":"riscv64"}):
            with self.assertRaisesRegex(CallError,'not scale-build'):selected_profile()

    def test_image_credentials_are_not_silently_reused_across_devices(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            verify_unprovisioned_image(root)
            auth=root/'etc/harboros/service-auth';auth.mkdir(parents=True)
            (auth/'gate-to-beacon.send').write_text('per-device-key')
            with self.assertRaises(CallError):verify_unprovisioned_image(root)
            (auth/'gate-to-beacon.send').unlink()
            env=root/'etc/default/harboros-beacon';env.parent.mkdir(parents=True);env.write_text('HARBOR_MODEL_API_TOKEN=baked-secret\n')
            with self.assertRaises(CallError):verify_unprovisioned_image(root)
            env.write_text('HARBOR_MODEL_API_BACKEND=candle\n')
            verify_unprovisioned_image(root)
            link=root/'var/lib/harborlink/secrets';link.mkdir(parents=True)
            (link/'mediamtx-api.pass').write_text('device-private')
            with self.assertRaises(CallError):verify_unprovisioned_image(root)
            (link/'mediamtx-api.pass').unlink()
            legacy=root/'etc/default/harboros-beacon-gate';legacy.write_text('GATE_TO_BEACON_TOKEN=legacy-shared-secret\n')
            with self.assertRaises(CallError):verify_unprovisioned_image(root)

    def test_internal_candidates_need_appliance_and_package_evidence_to_publish(self):
        value=fixture()
        with self.assertRaises(CallError):assert_publishable(value)
        for row in [*value['packages'].values(), *value['runtime']['packages'].values()]:row['release_eligible']=True
        value['acceptance']={key:'passed' for key in ('new_install','native_upgrade','boot_environment_rollback','reboot')}
        assert_publishable(value)
        value['runtime']['packages']['harboros-ffmpeg']['release_eligible']=False
        with self.assertRaises(CallError):assert_publishable(value)
        value['runtime']['packages']['harboros-ffmpeg']['release_eligible']=True
        value['packages']['harborlink']['release_eligible']=False
        with self.assertRaises(CallError):assert_publishable(value)

    def test_private_asset_token_is_only_sent_to_github_api(self):
        from unittest.mock import Mock
        call=Mock();call.return_value.json.return_value={'assets':[{'name':'pkg_1.1.deb','url':'https://api.github.com/repos/org/repo/releases/assets/42'}]}
        with patch.dict(os.environ,{'HARBOROS_COMPONENT_READ_TOKEN':'scoped-read-only-token'}):
            url,headers=github_asset_request('https://github.com/org/repo/releases/download/v1.1/pkg_1.1.deb',call)
            self.assertEqual(url,'https://api.github.com/repos/org/repo/releases/assets/42')
            self.assertEqual(headers['Authorization'],'Bearer scoped-read-only-token')
            self.assertEqual(github_asset_request('https://update.harboros.ai/deb/pkg.deb',call),('https://update.harboros.ai/deb/pkg.deb',{}))
            call.return_value.json.return_value={'assets':[{'name':'pkg_1.1.deb','url':'https://attacker.invalid/asset'}]}
            with self.assertRaises(CallError):github_asset_request('https://github.com/org/repo/releases/download/v1.1/pkg_1.1.deb',call)

    def test_workflow_does_not_publish_by_default(self):
        import yaml
        for filename in ('buildiso.yml','buildiso-test.yml'):
            data=yaml.safe_load((Path('.github/workflows')/filename).read_text(encoding='utf-8'))
            self.assertFalse(data['on']['workflow_dispatch']['inputs']['publishArtifacts']['default'])
            if filename=='buildiso-test.yml':self.assertFalse(data['on']['workflow_dispatch']['inputs']['releaseUpdate']['default'])
            self.assertEqual(data['env']['HARBOROS_ARCHITECTURE'],'${{ inputs.architecture }}')
            steps=data['jobs']['build-iso']['steps']
            for row in steps:
                if 'make ' in row.get('run',''):
                    self.assertIn('HARBOROS_RELEASE_MANIFEST',row['run'])
                    self.assertIn('TRUENAS_VERSION',row['run'])
                    self.assertEqual(row['env']['TRUENAS_VERSION'],'${{ steps.set-version.outputs.HarborNAS_VERSION }}')

if __name__=='__main__':unittest.main()
