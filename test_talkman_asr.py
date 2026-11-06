"""统一配置的凭证边界与供应商分发；不联网或使用真实密钥。"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from talkman_asr import ASRSettings, KEYS_FILE, PROVIDERS, SETTINGS_FILE, load_asr_settings, save_asr_settings, transcriber, validate
from talkman_qwen import QwenError
import talkman_asr as asr
from talkman_credentials import CredentialError


class ASRSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="TalkMan-asr-")
        self.directory = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_default_and_provider_limits(self):
        self.assertEqual(load_asr_settings(self.directory).provider,"qwen")
        self.assertEqual(PROVIDERS["xunfei"].seconds,55)
        self.assertGreaterEqual(PROVIDERS["xunfei"].timeout,75)
        self.assertEqual(PROVIDERS["baidu"].seconds,60)

    def test_selected_credentials_required_other_profiles_may_be_incomplete(self):
        validate(ASRSettings("baidu",{"baidu":{"api_key":"offline-baidu"},"xunfei":{"api_key":"partial"}}))
        with self.assertRaises(QwenError):
            validate(ASRSettings("xunfei",{"xunfei":{"api_key":"partial"}}))
        with self.assertRaises(QwenError):
            validate(ASRSettings("unknown"))

    def test_control_characters_rejected_without_echoing_key(self):
        key="private-offline-key\x00"
        with self.assertRaises(QwenError) as error:
            validate(ASRSettings("baidu",{"baidu":{"api_key":key}}))
        self.assertNotIn(key,str(error.exception))

    def test_new_and_old_volc_credentials(self):
        validate(ASRSettings("volc",{"volc":{"api_key":"offline"}}))
        validate(ASRSettings("volc",{"volc":{"app_id":"123","access_key":"offline"}}))
        with self.assertRaises(QwenError):
            validate(ASRSettings("volc",{"volc":{"app_id":"123"}}))

    def test_session_keys_never_written_to_plain_config(self):
        settings=ASRSettings("baidu",{"baidu":{"api_key":"private-baidu","secret_key":"private-secret","dev_pid":"1537"}})
        save_asr_settings(self.directory,settings,False)
        content=(self.directory/SETTINGS_FILE).read_text(encoding="utf-8")
        self.assertNotIn("private",content)
        self.assertNotIn("private",repr(settings))
        self.assertFalse((self.directory/KEYS_FILE).exists())
        loaded=load_asr_settings(self.directory)
        self.assertEqual(loaded.credentials()["dev_pid"],"1537")
        self.assertEqual(loaded.credentials()["api_key"],"")

    def test_encrypted_profiles_roundtrip_and_forget(self):
        settings=ASRSettings("tencent",{"tencent":{"app_id":"123","secret_id":"offline-id","secret_key":"offline-secret"}})
        def crypt(content,*,decrypt):
            return content[6:] if decrypt else b"cipher"+content
        with patch("talkman_asr._dpapi",side_effect=crypt),patch("talkman_asr.os.name","nt"):
            save_asr_settings(self.directory,settings,True)
            loaded=load_asr_settings(self.directory)
            self.assertEqual(loaded.credentials(),settings.credentials())
            save_asr_settings(self.directory,settings,False)
        self.assertFalse((self.directory/KEYS_FILE).exists())
        public=json.loads((self.directory/SETTINGS_FILE).read_text(encoding="utf-8"))
        self.assertEqual(public["profiles"]["tencent"],{"app_id":"123","engine_type":"16k_zh"})

    def test_encryption_failure_does_not_write_credentials(self):
        settings=ASRSettings("baidu",{"baidu":{"api_key":"offline-secret"}})
        with patch("talkman_asr._dpapi",side_effect=QwenError("加密失败")):
            with self.assertRaises(QwenError):
                save_asr_settings(self.directory,settings,True)
        self.assertEqual(list(self.directory.iterdir()),[])

    def test_dispatch_snapshots_credentials(self):
        settings=ASRSettings("baidu",{"baidu":{"api_key":"offline-original"}})
        with patch("talkman_asr_volc_baidu.transcribe_baidu",return_value="测试") as adapter:
            recognize=transcriber(settings)
            settings.profiles["baidu"]["api_key"]="offline-new"
            self.assertEqual(recognize(b"fake-audio"),"测试")
            self.assertEqual(adapter.call_args.args[1]["api_key"],"offline-original")


class MacASRSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="TalkMan-asr-mac-test-")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        platform = patch.object(asr.sys, "platform", "darwin")
        platform.start()
        self.addCleanup(platform.stop)
        self.settings = ASRSettings("baidu", {"baidu": {"api_key": "offline-private-key", "dev_pid": "1537"}})

    def test_mac_profiles_roundtrip_uses_keychain_and_never_plain_config(self):
        with patch.object(asr, "save_keychain_secret") as save, patch.object(asr, "_dpapi") as dpapi:
            save_asr_settings(self.directory, self.settings, True)
        data_dir, group, secret_bytes = save.call_args.args
        self.assertEqual((data_dir, group), (self.directory, "asr"))
        self.assertEqual(json.loads(secret_bytes)["baidu"]["api_key"], "offline-private-key")
        self.assertFalse((self.directory / KEYS_FILE).exists())
        self.assertNotIn("offline-private-key", (self.directory / SETTINGS_FILE).read_text())
        dpapi.assert_not_called()
        with patch.object(asr, "load_keychain_secret", return_value=secret_bytes) as load:
            self.assertTrue(asr.asr_keys_remembered(self.directory))
            load.assert_not_called()
            self.assertEqual(load_asr_settings(self.directory).credentials(), self.settings.credentials())

    def test_mac_missing_saved_item_or_session_has_empty_secrets(self):
        with patch.object(asr, "save_keychain_secret"):
            save_asr_settings(self.directory, self.settings, True)
        with patch.object(asr, "load_keychain_secret", return_value=None):
            self.assertEqual(load_asr_settings(self.directory).credentials()["api_key"], "")
        with patch.object(asr, "delete_keychain_secret"):
            save_asr_settings(self.directory, self.settings, False)
        with patch.object(asr, "load_keychain_secret") as load:
            self.assertFalse(asr.asr_keys_remembered(self.directory))
            self.assertEqual(load_asr_settings(self.directory).credentials()["api_key"], "")
        load.assert_not_called()

    def test_user_initiated_load_explicitly_allows_keychain_prompt(self):
        (self.directory / SETTINGS_FILE).write_text(json.dumps({"remember_credentials": True}), encoding="utf-8")
        with patch.object(asr, "load_keychain_secret", return_value=b'{"baidu": {"api_key": "offline-private-key"}}') as load:
            settings = load_asr_settings(self.directory, allow_keychain_prompt=True)
        self.assertEqual(settings.credentials("baidu")["api_key"], "offline-private-key")
        load.assert_called_once_with(self.directory, "asr", allow_interaction=True)

    def test_public_loader_never_prompts_even_when_explicit_prompt_option_is_true(self):
        (self.directory / SETTINGS_FILE).write_text(json.dumps({"remember_credentials": True}), encoding="utf-8")
        with patch.object(asr, "load_keychain_secret") as load:
            self.assertEqual(load_asr_settings(self.directory, read_credentials=False, allow_keychain_prompt=True).provider, "qwen")
        load.assert_not_called()

    def test_mac_uncheck_deletes_only_asr_group(self):
        with patch.object(asr, "save_keychain_secret"):
            save_asr_settings(self.directory, self.settings, True)
        with patch.object(asr, "delete_keychain_secret") as delete:
            save_asr_settings(self.directory, self.settings, False)
        delete.assert_called_once_with(self.directory, "asr")
        self.assertFalse(asr.asr_keys_remembered(self.directory))

    def test_mac_failed_keychain_save_or_delete_preserves_config(self):
        config = self.directory / SETTINGS_FILE
        previous = b'{"provider": "qwen", "remember_credentials": true}'
        config.write_bytes(previous)
        for remember, method in ((True, "save_keychain_secret"), (False, "delete_keychain_secret")):
            with self.subTest(remember=remember), patch.object(asr, method, side_effect=CredentialError("系统钥匙串未允许保存凭证")):
                with self.assertRaises(QwenError):
                    save_asr_settings(self.directory, self.settings, remember)
            self.assertEqual(config.read_bytes(), previous)
            self.assertEqual(list(self.directory.iterdir()), [config])

    def test_mac_denied_or_invalid_saved_profiles_preserve_config(self):
        config = self.directory / SETTINGS_FILE
        previous = b'{"provider": "qwen", "remember_credentials": true}'
        config.write_bytes(previous)
        for secret in (b"not-json", b"[]", b'{"baidu": []}'):
            with self.subTest(secret_type=type(secret).__name__), patch.object(asr, "load_keychain_secret", return_value=secret):
                with self.assertRaises(QwenError):
                    load_asr_settings(self.directory)
            self.assertEqual(config.read_bytes(), previous)
        with patch.object(asr, "load_keychain_secret", side_effect=CredentialError("系统钥匙串未允许读取凭证")):
            with self.assertRaises(QwenError):
                load_asr_settings(self.directory)
        self.assertEqual(config.read_bytes(), previous)

    def test_mac_missing_bridge_allows_session_only_without_saved_marker(self):
        with patch.object(asr, "secure_storage_supported", return_value=False), \
                patch.object(asr, "delete_keychain_secret") as delete:
            save_asr_settings(self.directory, self.settings, False)
        delete.assert_not_called()
        self.assertFalse(asr.asr_keys_remembered(self.directory))

    def test_mac_configuration_write_failure_reports_possible_keychain_change(self):
        config = self.directory / SETTINGS_FILE
        previous = b'{"remember_credentials": false}'
        config.write_bytes(previous)
        with patch.object(asr, "save_keychain_secret") as save, \
                patch.object(asr, "_atomic_write", side_effect=OSError("offline-private-key")):
            with self.assertRaisesRegex(QwenError, "钥匙串可能已更新") as caught:
                save_asr_settings(self.directory, self.settings, True)
        save.assert_called_once()
        self.assertNotIn("offline-private-key", str(caught.exception))
        self.assertEqual(config.read_bytes(), previous)

    def test_public_loader_preserves_provider_and_public_profiles_without_reading_secret(self):
        (self.directory / SETTINGS_FILE).write_text(json.dumps({
            "provider": "tencent", "remember_credentials": True,
            "profiles": {"tencent": {"app_id": "123", "engine_type": "16k_en"}},
        }), encoding="utf-8")
        (self.directory / KEYS_FILE).write_bytes(b"existing-cipher")
        with patch.object(asr, "load_keychain_secret") as keychain, patch.object(asr, "_dpapi") as dpapi:
            loaded = load_asr_settings(self.directory, read_credentials=False)
        self.assertEqual(loaded.provider, "tencent")
        self.assertEqual(loaded.credentials()["app_id"], "123")
        self.assertEqual(loaded.credentials()["engine_type"], "16k_en")
        self.assertEqual(loaded.credentials()["secret_key"], "")
        self.assertTrue(asr.asr_keys_remembered(self.directory))
        keychain.assert_not_called()
        dpapi.assert_not_called()


if __name__=="__main__":
    unittest.main()
