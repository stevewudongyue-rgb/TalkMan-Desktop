"""同步事务离线测试：不用真实 Agent，发送仅计数。"""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock
from types import SimpleNamespace
from talkman_sync import SyncLease
from talkman_target import TargetError, TargetController, TargetWindow, FillReceipt
from test_talkman_target import wait_result


class MemoryBackend:
    def __init__(self, text=''):
        self.text, self.sent, self.writes = text, 0, []
    def open_sync(self, target): return 'composer'
    def read_sync(self, target, handle): return self.text
    def write_sync(self, target, handle, text, expected, current):
        if not current() or self.text != expected:
            raise TargetError('同步已取消')
        self.text = text
        self.writes.append(text)
    def send_sync(self, target, handle, text, current):
        self.sent += 1
        return '已触发下发'
    def close(self): pass


class SyncTests(unittest.TestCase):
    def test_insert_replace_delete_are_mirrored_without_send(self):
        b = MemoryBackend()
        lease = SyncLease(b, 'test', '')
        for text in ('苹', '苹果', '梨', ''):
            lease.update(text)
            self.assertEqual(b.text, text)
        self.assertEqual(b.sent, 0)

    def test_existing_text_is_kept_and_only_owned_suffix_changes(self):
        b = MemoryBackend('他处的草稿')
        lease = SyncLease(b, 'test', '本机草稿')
        for text in ('本机草稿', '修改后的草稿', '', '苹果'):
            lease.update(text)
            self.assertEqual(b.text, '他处的草稿'+text)
        b.text = '用户在目标里改过'
        with self.assertRaises(TargetError): lease.update('梨')
        self.assertEqual(b.text, '用户在目标里改过')

    def test_whitespace_reconnect_and_send_preserve_prefix_without_duplicate(self):
        for prefix in ('', ' \n', '原文\n'):
            with self.subTest(prefix=prefix):
                b = MemoryBackend(prefix)
                lease = SyncLease(b, 'test', '新稿')
                lease.update('新稿')
                self.assertEqual(b.text, prefix+'新稿')
                lease = SyncLease(b, 'test', '新稿')
                lease.update('改稿😀')
                lease.send('最终稿')
                self.assertEqual(b.text, prefix+'最终稿')
                self.assertEqual(b.sent, 1)

    def test_target_sending_emptying_input_is_a_conflict(self):
        b = MemoryBackend()
        lease = SyncLease(b, 'test', '')
        lease.update('原消息')
        b.text = ''
        with self.assertRaises(TargetError): lease.update('原消息新增')
        self.assertEqual(b.text, '')

    def test_stale_session_does_not_write(self):
        b = MemoryBackend()
        lease = SyncLease(b, 'test', '')
        with self.assertRaises(TargetError): lease.update('旧请求', lambda: False)
        self.assertEqual(b.writes, [])

    def test_send_checks_latest_text_and_never_retries(self):
        b = MemoryBackend()
        lease = SyncLease(b, 'test', '')
        lease.update('旧句')
        lease.send('最终句')
        self.assertEqual(b.text, '最终句')
        self.assertEqual(b.sent, 1)
        with self.assertRaises(TargetError): lease.send('最终句')
        self.assertEqual(b.sent, 1)

    def test_unconfirmed_write_pauses(self):
        b = MemoryBackend()
        b.write_sync = Mock()
        lease = SyncLease(b, 'test', '')
        with self.assertRaises(TargetError): lease.update('新文字')
        self.assertTrue(lease.closed)

    def test_worker_serializes_and_returns_receipt_only_for_explicit_send(self):
        with TemporaryDirectory() as folder:
            b = MemoryBackend('目标原文：')
            c = TargetController(Path(folder), backend=b)
            self.addCleanup(c.close)
            c.select(TargetWindow(1, 1, '测试', '测试对话', 'test'))
            results = []
            callback = lambda state, value: results.append((state, value))
            c.sync('测试正文', callback)
            self.assertEqual(wait_result(results), ('ok', '测试正文'))
            self.assertEqual(b.sent, 0)
            c.sync('测试正文', callback, send=True)
            state, value = wait_result(results)
            self.assertEqual(state, 'ok')
            self.assertIsInstance(value, FillReceipt)
            self.assertEqual(value.text, '目标原文：测试正文')
            self.assertEqual(b.sent, 1)
            c.sync('不应再次发送', callback, send=True)
            self.assertEqual(wait_result(results)[0], 'error')
            self.assertEqual(b.sent, 1)

    def test_changing_target_invalidates_queued_request(self):
        with TemporaryDirectory() as folder:
            b = MemoryBackend()
            c = TargetController(Path(folder), backend=b)
            self.addCleanup(c.close)
            target = TargetWindow(1, 1, '测试', '测试对话', 'test')
            c.select(target)
            old = (c._sync_epoch, target, '不能写入', False)
            c.select(TargetWindow(2, 2, '另一个', '新对话', 'other'))
            results = []
            c._sync(lambda state, value: results.append((state, value)), old)
            self.assertEqual(results[0][0], 'error')
            self.assertEqual(b.writes, [])


class NativeSyncTests(unittest.TestCase):
    def test_chatgpt_ambiguous_editor_label_is_never_appended_as_sync_prefix(self):
        from talkman_target_windows import WindowsTargetBackend
        b = WindowsTargetBackend()
        window = TargetWindow(1, 2, 'ChatGPT', 'ChatGPT', 'ChatGPT.exe')
        element = SimpleNamespace(CurrentClassName='ProseMirror', CurrentName='Work with ChatGPT')
        pattern = SimpleNamespace(CurrentIsReadOnly=False, CurrentValue='Work with ChatGPT\n', SetValue=Mock())
        b.validate_window = Mock(return_value=window)
        b._find_editable = Mock(return_value=element)
        b._pattern = Mock(return_value=pattern)
        b._is_editable = Mock(return_value=True)
        b._is_descendant_of = Mock(return_value=True)
        b._window_element = Mock(return_value=object())
        b._ensure_uia = Mock(return_value=SimpleNamespace(CompareElements=lambda a, z: a is z))
        with self.assertRaisesRegex(TargetError, '关闭同步'):
            SyncLease(b, window, '新正文')
        pattern.SetValue.assert_not_called()
        self.assertEqual(pattern.CurrentValue, 'Work with ChatGPT\n')
        pattern.CurrentValue = '用户主动写下 Work with ChatGPT'
        lease = SyncLease(b, window, '新正文')
        self.assertEqual(lease.prefix, pattern.CurrentValue)

    def test_windows_value_sync_does_not_focus_or_use_clipboard(self):
        from talkman_target_windows import WindowsTargetBackend
        b = WindowsTargetBackend()
        window = TargetWindow(1, 2, '测试', '原对话', 'test')
        element = Mock()
        pattern = SimpleNamespace(CurrentIsReadOnly=False, CurrentValue='')
        pattern.SetValue = lambda value: setattr(pattern, 'CurrentValue', value)
        b.validate_window = Mock(return_value=window)
        b._find_editable = Mock(return_value=element)
        b._pattern = Mock(return_value=pattern)
        b._is_editable = Mock(return_value=True)
        b._is_descendant_of = Mock(return_value=True)
        b._window_element = Mock(return_value=object())
        b._ensure_uia = Mock(return_value=SimpleNamespace(CompareElements=lambda a, z: a is z))
        lease = SyncLease(b, window, '')
        lease.update('后台更新')
        self.assertEqual(pattern.CurrentValue, '后台更新')
        element.SetFocus.assert_not_called()
        b.validate_window.return_value = TargetWindow(1, 2, '测试', '换了对话', 'test')
        with self.assertRaises(TargetError): lease.update('不能覆盖')
        self.assertEqual(pattern.CurrentValue, '后台更新')

    def test_mac_value_sync_is_background_and_rejects_new_conversation(self):
        from test_talkman_target_macos import FakeAPI
        from talkman_target_macos import MacTargetBackend
        api = FakeAPI()
        _, root = api.add_app()
        window = api.add_window(root)
        element = api.add_input(window)
        element.attributes['AXValue'] = ''
        b = MacTargetBackend(api=api)
        target = b.list_windows()[0]
        lease = SyncLease(b, target, '')
        lease.update('后台文字')
        self.assertEqual(element.attributes['AXValue'], '后台文字')
        self.assertIsNone(api.front)
        self.assertFalse(any(item[0] in ('activate', 'paste', 'clipboard') for item in api.actions))
        window.attributes['AXTitle'] = '另一段对话'
        with self.assertRaises(TargetError): lease.update('不能覆盖')
        self.assertEqual(element.attributes['AXValue'], '后台文字')
