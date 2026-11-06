"""公开版功能状态回归，不访问真实服务、不录音或投递。"""
import base64
import gc
import json
from pathlib import Path
import tempfile
import sys
import time
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from talkman_license import LicenseClient, LicenseError, verify_grant
from test_talkman_license_support import TestEntitlements


class SignatureTests(unittest.TestCase):
    def setUp(self):
        self.key = Ed25519PrivateKey.generate()
        self.public = self.key.public_key().public_bytes(serialization.Encoding.PEM,
                                                       serialization.PublicFormat.SubjectPublicKeyInfo)
        self.device = 'b' * 64
        self.now = int(time.time())
        self.grant = dict(product='talkman-desktop', version=1, license='a'*32,
                          device=self.device, features=['records','conventions','translation'],
                          issued_at=self.now, expires_at=self.now+86400)

    def envelope(self):
        raw = json.dumps(self.grant).encode()
        return dict(payload=base64.b64encode(raw).decode(),
                    signature=base64.b64encode(self.key.sign(raw)).decode(), refresh='r'*43)

    def test_signed_grant_device_expiry_and_tampering(self):
        value = self.envelope()
        self.assertEqual(verify_grant(value,self.device,public_key=self.public)['features'],self.grant['features'])
        for device, now in [('c'*64,self.now), (self.device,self.now+86400), (self.device,self.now-301)]:
            with self.assertRaises(LicenseError):
                verify_grant(value,device,public_key=self.public,now=now)
        raw=json.loads(base64.b64decode(value['payload']))
        raw['expires_at'] += 365*86400
        value['payload'] = base64.b64encode(json.dumps(raw).encode()).decode()
        with self.assertRaises(LicenseError):
            verify_grant(value,self.device,public_key=self.public)
        self.grant['expires_at'] = self.now+31*86400
        with self.assertRaises(LicenseError):
            verify_grant(self.envelope(),self.device,public_key=self.public)

    def test_client_always_exposes_complete_feature_set_without_network(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp)
            client=LicenseClient(folder)
            for feature in ('records', 'conventions', 'translation'):
                self.assertTrue(client.has(feature))
            (folder/'desktop-license.json').write_text(json.dumps({'premium':True,'features':['records'],
                                                                  'grant':self.envelope()}))
            reloaded = LicenseClient(folder)
            self.assertTrue(reloaded.has('records'))
            self.assertEqual(client.redeem('任意字符串'), client.info())
            with patch('talkman_license.request_license', side_effect=AssertionError('公开版不应访问授权服务')):
                self.assertEqual(reloaded.refresh(), reloaded.info())
            self.assertFalse(reloaded.due())

    def test_legacy_signed_grant_helpers_remain_isolated(self):
        value = self.envelope()
        with tempfile.TemporaryDirectory() as temp:
            client = LicenseClient(temp)
            with patch('talkman_license.verify_grant', return_value=self.grant):
                self.assertEqual(client.accept(value), client.info())
            self.assertTrue(client.has('translation'))


class BasicAppTests(unittest.TestCase):
    def setUp(self):
        from talkman_app import TalkManApp
        from test_talkman_app import FakeVoice,FakeClient
        self.temp=tempfile.TemporaryDirectory()
        self.folder=Path(self.temp.name)
        self.entitlements=TestEntitlements(False)
        self.original=json.dumps([dict(id='old',text='原有草稿不能删除',time='2026-11-01 10:00:00')],ensure_ascii=False)
        (self.folder/'history.json').write_text(self.original,encoding='utf-8')
        self.app=TalkManApp(self.folder,session_factory=FakeVoice,client_factory=FakeClient,
                            desktop_services=False,license_client=self.entitlements)
        self.app.withdraw()
        self.app.update()

    def tearDown(self):
        if self.app is not None:
            self.app.close_app()
        self.app=None
        gc.collect()
        self.temp.cleanup()

    def test_public_build_keeps_all_editing_and_record_features_available(self):
        a=self.app
        a.set_text('公开版正文')
        a.open_history()
        self.assertTrue(a.history_open)
        self.assertTrue(a.save_current())
        a.remember_selection('Colo Play')
        a.learn_correction('错词','正确词')
        # “记住这个词”先打开确认面板，用户确认保存后才写入约定词。
        self.assertIsNotNone(a.convention_panel)
        self.assertEqual(a.records_button.winfo_manager(),'pack')
        self.assertEqual(a.translation_tools.winfo_manager(),'pack')
        self.assertTrue(a.persist_current_text())
        self.assertEqual((self.folder/'current-text.txt').read_text(encoding='utf-8'),'公开版正文')
        a.copy_all()
        self.assertEqual(a.clipboard_get(),'公开版正文')

    def test_legacy_entitlement_changes_do_not_hide_public_features(self):
        a=self.app
        a.set_text('正在编辑的正文')
        a.text.tag_add('sel','1.0','1.2')
        a.refresh_license_ui()
        self.assertEqual(a.records_button.winfo_manager(),'pack')
        self.assertEqual(a.saved_history[0]['text'],'原有草稿不能删除')
        a.open_history()
        self.assertTrue(a.history_open)
        self.entitlements.enabled=False
        a.refresh_license_ui()
        self.assertTrue(a.history_open)
        self.assertEqual(a.current_text(),'正在编辑的正文')
        self.assertTrue(a.write_history([]))
        self.assertEqual(json.loads((self.folder/'history.json').read_text(encoding='utf-8')),[])

    def test_public_fill_clear_arms_second_send_and_records_delivery_history(self):
        from talkman_target import FillReceipt
        a=self.app
        a.set_text('只填入测试桩')
        a.clear_after_fill_var.set(True)
        a._delivery_snapshot=(a.revision,a.current_text())
        receipt=FillReceipt(text=a.current_text(),app='测试',title='测试输入框',time='2026-11-03',message='已填入',send_token='fake-token')
        a.finish_normal_fill('ok',receipt)
        self.assertEqual(a.current_text(),'')
        self.assertIsNotNone(a.pending_delivery)
        delivery_path=self.folder/'delivery-history.jsonl'
        self.assertTrue(delivery_path.exists())
        self.assertEqual(json.loads(delivery_path.read_text(encoding='utf-8').splitlines()[0])['text'],'只填入测试桩')

    def test_basic_restore_single_current_text_on_reopen(self):
        self.app.set_text('退出前只留当前正文')
        self.app.close_app()
        self.app=None
        from talkman_app import TalkManApp
        from test_talkman_app import FakeVoice,FakeClient
        self.app=TalkManApp(self.folder,session_factory=FakeVoice,client_factory=FakeClient,
                            desktop_services=False,license_client=self.entitlements)
        self.assertEqual(self.app.current_text(),'退出前只留当前正文')
        history=json.loads((self.folder/'history.json').read_text(encoding='utf-8'))
        self.assertTrue(any(item['text']=='退出前只留当前正文' for item in history))


if __name__=='__main__':
    unittest.main()
