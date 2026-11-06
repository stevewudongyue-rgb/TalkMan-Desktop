"""更新边界与真实替换脚本验证，全部使用隔离目录。"""
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import zipfile

import talkman_updates as u


def release(version='0.1.34', system='windows-x64'):
    tag = 'v' + version + '-test'
    name = (f'TalkMan-Windows-Portable-{version}.zip' if system == 'windows-x64'
            else f'TalkMan-Mac-AppleSilicon-{version}-test.dmg')
    return dict(tag_name=tag, draft=False, assets=[dict(name=name, size=20, digest='sha256:'+'a'*64,
                browser_download_url=u.DOWNLOAD_PREFIX+tag+'/'+name)])


class UpdateTests(unittest.TestCase):
    def test_mac_stable_identity_and_no_signed_to_adhoc_downgrade(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        def output(team):
            return SimpleNamespace(stdout='',stderr='TeamIdentifier='+team+'\ndesignated => identifier "com.cololight.talkman" and anchor apple generic\n')
        with patch.object(u.subprocess,'run',side_effect=[output('ABCDEFGHIJ'),output('ABCDEFGHIJ'),output('ABCDEFGHIJ')]) as run:
            self.assertTrue(u.verify_mac_signing_continuity(Path('old.app'),Path('new.app')))
            self.assertIn('-R',run.call_args.args[0])
        for next_team in ('KLMNOPQRST','not set'):
            with patch.object(u.subprocess,'run',side_effect=[output('ABCDEFGHIJ'),output(next_team)]):
                with self.assertRaises(u.UpdateError):
                    u.verify_mac_signing_continuity(Path('old.app'),Path('new.app'))
        with patch.object(u.subprocess,'run',return_value=output('not set')):
            self.assertFalse(u.verify_mac_signing_continuity(Path('old.app'),Path('new.app')))

    def test_version_order_tests_and_no_downgrade(self):
        payload = json.dumps([release('0.1.9'), release('0.1.34'), release('0.1.100')]).encode()
        self.assertEqual(u.parse_releases(payload, '0.1.33', 'windows-x64').version, '0.1.100')
        self.assertIsNone(u.parse_releases(payload, '0.1.100', 'windows-x64'))
        r = release(); r['draft'] = True
        self.assertIsNone(u.parse_releases(json.dumps([r]), '0.1.33', 'windows-x64'))
        r = release(); r['tag_name'] = 'v0.1.28.52'
        self.assertIsNone(u.parse_releases(json.dumps([r]), '0.1.33', 'windows-x64'))
        self.assertIsNone(u.parse_releases(json.dumps([release()]), '0.1.33', 'mac-arm64'))
        self.assertEqual(u.parse_releases(json.dumps([release(system='mac-arm64')]), '0.1.33', 'mac-arm64').platform, 'mac-arm64')

    def test_release_asset_must_have_pinned_repository_size_digest(self):
        for field, value in [('browser_download_url','https://evil.test/new.zip'), ('size',True),
                             ('size',u.MAX_PACKAGE+1), ('digest',None), ('digest','sha256:123')]:
            with self.subTest(field=field,value=value):
                r=release(); r['assets'][0][field]=value
                with self.assertRaises(u.UpdateError): u.parse_releases(json.dumps([r]),'0.1.33','windows-x64')
        r=release(); r['assets'] *= 2
        with self.assertRaises(u.UpdateError): u.parse_releases(json.dumps([r]),'0.1.33','windows-x64')

    def test_redirect_rejects_insecure_or_unrelated_host(self):
        for value in ['http://github.com/x','https://github.com.evil.test/x','https://u:p@github.com/x']:
            self.assertFalse(u._valid_https(value))
        self.assertTrue(u._valid_https('https://release-assets.githubusercontent.com/x?signature=abc'))

    def test_download_integrity_and_cancel(self):
        content=b'verified-package'
        info=u.UpdateInfo('0.1.34','windows-x64',release()['assets'][0]['browser_download_url'],
                          hashlib.sha256(content).hexdigest(),len(content))
        opener=Mock()
        opener.open.return_value=io.BytesIO(content)
        progress=Mock()
        package=u.download_update(info,opener=opener,progress=progress)
        self.assertEqual(package.read_bytes(),content)
        progress.assert_called_once_with(len(content),len(content))
        u.discard_prepared_update(package)
        for body in [b'',content+b'x',content[:-1]+b'X']:
            opener.open.return_value=io.BytesIO(body)
            with self.assertRaises(u.UpdateError):u.download_update(info,opener=opener)
        flag=threading.Event();flag.set();opener.open.return_value=io.BytesIO(content)
        with self.assertRaises(u.UpdateError):u.download_update(info,opener=opener,cancelled=flag)

    def make_zip(self, path, extra=None):
        with zipfile.ZipFile(path,'w') as archive:
            for name in u.WINDOWS_FILES:
                archive.writestr('TalkMan/'+(name+'/runtime.dll' if name=='_internal' else name), b'MZnew' if name=='TalkMan.exe' else b'new')
            if extra:
                name, content = extra
                if isinstance(name, str):
                    entry = zipfile.ZipInfo()
                    entry.filename = name  # 不让 Windows 写 ZIP 时提前规范化反斜线。
                    name = entry
                archive.writestr(name, content)

    def test_zip_preserves_target_and_rejects_paths_duplicates_symlinks(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);target=root/'原程序';target.mkdir()
            (target/'my-notes.txt').write_text('keep')
            package=root/'update.zip'
            self.make_zip(package)
            stage=u.prepare_windows_update(package,target)
            self.assertEqual((stage/'TalkMan.exe').read_bytes(),b'MZnew')
            self.assertEqual((target/'my-notes.txt').read_text(),'keep')
            shutil.rmtree(stage.parent)
            link=zipfile.ZipInfo('TalkMan/_internal/link');link.external_attr=0o120777<<16
            for name in ['../escape','TalkMan/../escape','TalkMan/_internal/../../escape',
                         'TalkMan/_internal\\escape','TalkMan/TalkMan.exe','TalkMan/unknown.txt',
                         'TalkMan/_internal/CON:stream',link]:
                self.make_zip(package,(name,b'unsafe'))
                with self.assertRaises(u.UpdateError):u.prepare_windows_update(package,target)
                self.assertEqual((target/'my-notes.txt').read_text(),'keep')
                self.assertEqual(list(root.glob('.talkman-desktop-update-*')),[])

    @unittest.skipUnless(sys.platform=='win32','Windows helper')
    def test_real_windows_swap_and_rollback_with_chinese_and_apostrophe_paths(self):
        with tempfile.TemporaryDirectory(prefix="TalkMan-测试's-") as folder:
            root=Path(folder);target=root/'我的程序';target.mkdir()
            for fail in (False,True):
                with self.subTest(rollback=fail):
                    (target/'TalkMan.exe').write_text('old')
                    (target/'_internal').mkdir(exist_ok=True)
                    (target/'_internal/data').write_text('old-runtime')
                    (target/'草稿.txt').write_text('keep',encoding='utf-8')
                    package=root/'update.zip';self.make_zip(package)
                    stage=u.prepare_windows_update(package,target)
                    if fail:(stage/'_internal/runtime.dll').unlink();(stage/'_internal').rmdir()
                    ready=root/'ready';ready.touch()
                    config=root/'config.json';result=root/'result.txt'
                    config.write_text(json.dumps(dict(pid=2147483647,stage=str(stage),target=str(target),ready=str(ready),
                        result=str(result),receipt=str(root/'started'),restart=False,files=u.WINDOWS_FILES)),encoding='utf-8')
                    helper=root/'helper.ps1';helper.write_text(u.WINDOWS_HELPER,encoding='utf-8-sig')
                    run=subprocess.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',str(helper),
                                        '-Config',str(config)],capture_output=True,timeout=20,creationflags=subprocess.CREATE_NO_WINDOW)
                    self.assertEqual(run.returncode,1 if fail else 0,run.stderr)
                    self.assertEqual((target/'TalkMan.exe').read_bytes(),b'old' if fail else b'MZnew')
                    self.assertEqual((target/'草稿.txt').read_text(encoding='utf-8'),'keep')
                    self.assertIn('恢复原版' if fail else '更新成功',result.read_text(encoding='utf-8-sig'))
                    self.assertTrue((stage.parent/'previous').is_dir())
                    shutil.rmtree(stage.parent)

    @unittest.skipUnless(sys.platform=='darwin','Mac helper')
    def test_mac_script_swap_and_open_failure_rolls_back(self):
        with tempfile.TemporaryDirectory(prefix="TalkMan-测试's-") as folder:
            root=Path(folder)
            for fail in (False,True):
                with self.subTest(rollback=fail):
                    target=root/'TalkMan.app';target.mkdir(exist_ok=True);(target/'value').write_text('old')
                    stage_root=Path(tempfile.mkdtemp(prefix='.talkman-desktop-update-',dir=root))
                    stage=stage_root/'TalkMan.app';stage.mkdir();(stage/'value').write_text('new')
                    ready=root/'ready';ready.touch();receipt=root/'started';receipt.touch();result=root/'result'
                    helper=root/'helper.sh'
                    helper.write_text(u.MAC_HELPER.replace('/usr/bin/open','/usr/bin/false' if fail else '/usr/bin/true'))
                    subprocess.run(['/bin/sh','-n',str(helper)],check=True)
                    run=subprocess.run(['/bin/sh',str(helper),'2147483647',str(stage),str(target),str(ready),str(result),str(receipt)],timeout=10)
                    self.assertEqual(run.returncode,1 if fail else 0)
                    self.assertEqual((target/'value').read_text(),'old' if fail else 'new')
                    shutil.rmtree(stage_root)

    @unittest.skipUnless(sys.platform=='darwin','Mac DMG staging')
    def test_real_ci_dmg_stage_and_codesign(self):
        from talkman_version import VERSION
        package=Path('dist-mac/TalkMan-Mac-AppleSilicon-测试版.dmg').resolve()
        if not package.exists():self.skipTest('需要先生成 DMG')
        with tempfile.TemporaryDirectory(prefix='TalkMan-dmg-stage-') as folder:
            root=Path(folder);copy=root/package.name;shutil.copyfile(package,copy)
            installed=root/'TalkMan.app'
            shutil.copytree(Path('dist-mac/TalkMan.app'), installed, symlinks=True)
            stage=u.prepare_mac_update(copy,VERSION,installed)
            u._verify_mac_app(stage,VERSION)
            self.assertTrue(stage.is_dir())


if __name__=='__main__':unittest.main()
