"""Mac 钥匙串 API 边界离线测试；全部使用假凭证和系统桩。"""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import talkman_credentials as credentials


FAKE_SECRET = b"offline-private-credential"


def security_api():
    names = ("kSecClass", "kSecClassGenericPassword", "kSecAttrService", "kSecAttrAccount",
             "kSecAttrSynchronizable", "kSecReturnData", "kSecMatchLimit", "kSecMatchLimitOne",
             "kSecValueData", "kSecAttrLabel", "kSecUseAuthenticationUI", "kSecUseAuthenticationUIFail",
             "kSecUseAuthenticationUIAllow")
    api = SimpleNamespace(**{name: name for name in names}, errSecSuccess=0,
                          errSecItemNotFound=-25300, errSecDuplicateItem=-25299)
    api.SecItemCopyMatching = Mock(return_value=(0, FAKE_SECRET))
    api.SecItemUpdate = Mock(return_value=0)
    api.SecItemAdd = Mock(return_value=(0, None))
    api.SecItemDelete = Mock(return_value=0)
    return api


class KeychainTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="TalkMan-keychain-test-")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.api = security_api()
        backend = patch.object(credentials, "_security", return_value=self.api)
        backend.start()
        self.addCleanup(backend.stop)

    def test_reads_only_exact_local_item_and_requests_data(self):
        self.assertEqual(credentials.load_keychain_secret(self.directory, "qwen"), FAKE_SECRET)
        query, output = self.api.SecItemCopyMatching.call_args.args
        self.assertIsNone(output)
        self.assertEqual(query[self.api.kSecClass], self.api.kSecClassGenericPassword)
        self.assertEqual(query[self.api.kSecAttrService], credentials._SERVICE)
        self.assertFalse(query[self.api.kSecAttrSynchronizable])
        self.assertTrue(query[self.api.kSecReturnData])
        self.assertEqual(query[self.api.kSecMatchLimit], self.api.kSecMatchLimitOne)
        self.assertEqual(query[self.api.kSecUseAuthenticationUI], self.api.kSecUseAuthenticationUIFail)
        self.assertTrue(query[self.api.kSecAttrAccount].startswith("qwen:"))
        self.assertNotIn(FAKE_SECRET.decode(), repr(query))

    def test_absent_item_returns_none(self):
        self.api.SecItemCopyMatching.return_value = (-25300, None)
        self.assertIsNone(credentials.load_keychain_secret(self.directory, "asr"))

    def test_user_initiated_read_allows_system_authorization(self):
        self.assertEqual(credentials.load_keychain_secret(self.directory, "qwen", allow_interaction=True), FAKE_SECRET)
        query = self.api.SecItemCopyMatching.call_args.args[0]
        self.assertEqual(query[self.api.kSecUseAuthenticationUI], self.api.kSecUseAuthenticationUIAllow)

    def test_update_existing_item_never_adds_or_writes_file(self):
        credentials.save_keychain_secret(self.directory, "qwen", FAKE_SECRET)
        query, changes = self.api.SecItemUpdate.call_args.args
        self.assertEqual(changes, {self.api.kSecValueData: FAKE_SECRET})
        self.assertNotIn(self.api.kSecValueData, query)
        self.api.SecItemAdd.assert_not_called()
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_new_item_adds_secret_without_reading_it_first(self):
        self.api.SecItemUpdate.return_value = -25300
        credentials.save_keychain_secret(self.directory, "asr", FAKE_SECRET)
        attributes, output = self.api.SecItemAdd.call_args.args
        self.assertIsNone(output)
        self.assertEqual(attributes[self.api.kSecValueData], FAKE_SECRET)
        self.assertEqual(attributes[self.api.kSecAttrLabel], "TalkMan 语音服务凭证")
        self.assertFalse(attributes[self.api.kSecAttrSynchronizable])
        self.api.SecItemCopyMatching.assert_not_called()

    def test_duplicate_on_add_retries_update_once(self):
        self.api.SecItemUpdate.side_effect = [-25300, 0]
        self.api.SecItemAdd.return_value = (-25299, None)
        credentials.save_keychain_secret(self.directory, "qwen", FAKE_SECRET)
        self.assertEqual(self.api.SecItemUpdate.call_count, 2)
        self.assertEqual(self.api.SecItemAdd.call_count, 1)

    def test_qwen_and_asr_and_user_directories_have_separate_items(self):
        queries = [credentials._query(self.api, self.directory, "qwen"),
                   credentials._query(self.api, self.directory, "asr"),
                   credentials._query(self.api, self.directory / "other", "qwen")]
        self.assertEqual(len({query[self.api.kSecAttrAccount] for query in queries}), 3)

    def test_delete_accepts_missing_item_and_has_no_secret_payload(self):
        for status in (0, -25300):
            with self.subTest(status=status):
                self.api.SecItemDelete.return_value = status
                credentials.delete_keychain_secret(self.directory, "qwen")
                self.assertNotIn(self.api.kSecValueData, self.api.SecItemDelete.call_args.args[0])

    def test_rejection_is_sanitized_and_never_adds_after_failed_update(self):
        for status in (-128, -25293, -25308, -25291):
            for method, call in (
                (self.api.SecItemCopyMatching, lambda: credentials.load_keychain_secret(self.directory, "qwen")),
                (self.api.SecItemUpdate, lambda: credentials.save_keychain_secret(self.directory, "qwen", FAKE_SECRET)),
                (self.api.SecItemDelete, lambda: credentials.delete_keychain_secret(self.directory, "qwen")),
            ):
                with self.subTest(status=status, method=method):
                    method.return_value = (status, FAKE_SECRET) if method is self.api.SecItemCopyMatching else status
                    with self.assertRaises(credentials.CredentialError) as caught:
                        call()
                    self.assertNotIn(FAKE_SECRET.decode(), str(caught.exception))
                    self.assertNotIn(str(status), str(caught.exception))
        self.api.SecItemAdd.assert_not_called()

    def test_native_exceptions_never_echo_private_data(self):
        for method, call in (
            (self.api.SecItemCopyMatching, lambda: credentials.load_keychain_secret(self.directory, "qwen")),
            (self.api.SecItemUpdate, lambda: credentials.save_keychain_secret(self.directory, "qwen", FAKE_SECRET)),
            (self.api.SecItemDelete, lambda: credentials.delete_keychain_secret(self.directory, "qwen")),
        ):
            with self.subTest(method=method):
                method.side_effect = RuntimeError(FAKE_SECRET.decode())
                with self.assertRaises(credentials.CredentialError) as caught:
                    call()
                self.assertNotIn(FAKE_SECRET.decode(), str(caught.exception))

    def test_malformed_native_read_results_are_rejected(self):
        for result in (0, (0,), (0, None), (0, b""), (0, FAKE_SECRET, b"extra"),
                       (0, b"a" * (credentials._MAX_SECRET_BYTES + 1)), (True, FAKE_SECRET)):
            with self.subTest(result_type=type(result).__name__):
                self.api.SecItemCopyMatching.return_value = result
                with self.assertRaises(credentials.CredentialError):
                    credentials.load_keychain_secret(self.directory, "qwen")

    def test_invalid_secret_is_rejected_before_native_update(self):
        for secret in (b"", "plaintext", None, b"x" * (credentials._MAX_SECRET_BYTES + 1)):
            with self.subTest(secret_type=type(secret).__name__):
                with self.assertRaises(credentials.CredentialError):
                    credentials.save_keychain_secret(self.directory, "qwen", secret)
        self.api.SecItemUpdate.assert_not_called()

    def test_support_detection_does_not_access_keychain(self):
        with patch.object(credentials.sys, "platform", "darwin"), \
                patch.object(credentials.importlib.util, "find_spec", return_value=object()):
            self.assertTrue(credentials.secure_storage_supported())
        with patch.object(credentials.sys, "platform", "darwin"), \
                patch.object(credentials.importlib.util, "find_spec", return_value=None):
            self.assertFalse(credentials.secure_storage_supported())
        self.api.SecItemCopyMatching.assert_not_called()
        self.api.SecItemUpdate.assert_not_called()
        self.api.SecItemDelete.assert_not_called()


if __name__ == "__main__":
    unittest.main()
