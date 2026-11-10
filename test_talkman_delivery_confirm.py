"""普通填入后的二次确认：假输入框与假回车，不操作真实 Agent。"""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from talkman_target import TargetController, TargetWindow, TargetError
from test_talkman_target import FakeBackend, wait_result


class ConfirmBackend(FakeBackend):
    def __init__(self):
        super().__init__()
        self.text = '前后'
        self.sent = 0
        self.fail_after_send = False
    def open_sync(self, target): return 'main-input'
    def read_sync(self, target, handle): return self.text
    def fill(self, target, text, token):
        self.calls.append(('paste', text))
        self.text = self.text[:1]+text+self.text[1:]
        return '已填入'
    def send_sync(self, target, handle, text, current):
        if not current() or text != self.text: raise TargetError('内容变化')
        self.sent += 1
        if self.fail_after_send: raise TargetError('结果未知')
        return '已按回车'


class DeliveryConfirmTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.backend = ConfirmBackend()
        self.controller = TargetController(Path(self.folder.name), backend=self.backend)
        self.controller.select(self.backend.windows[0])
        self.results = []

    def tearDown(self):
        self.controller.close()
        self.folder.cleanup()

    def callback(self, state, value): self.results.append((state, value))

    def fill(self):
        self.controller.fill_with_receipt('正文', self.callback)
        state, receipt = wait_result(self.results)
        self.assertEqual(state, 'ok')
        self.assertTrue(receipt.send_token)
        return receipt

    def test_fill_then_send_preserves_prefix_and_suffix_and_pastes_only_once(self):
        receipt = self.fill()
        self.assertEqual(self.backend.text, '前正文后')
        self.assertEqual(self.backend.sent, 0)
        self.controller.send_filled(receipt.send_token, self.callback)
        state, value = wait_result(self.results)
        self.assertEqual(state, 'ok')
        self.assertEqual(value.text, '前正文后')
        self.assertEqual(value.status, '已触发下发')
        self.assertEqual(self.backend.sent, 1)
        self.assertEqual([x for x in self.backend.calls if x[0]=='paste'], [('paste','正文')])
        self.controller.send_filled(receipt.send_token, self.callback)
        self.assertEqual(wait_result(self.results)[0], 'error')
        self.assertEqual(self.backend.sent, 1)

    def test_external_edits_or_target_switch_or_timeout_invalidate_send(self):
        for mode in ['external', 'target', 'expired']:
            with self.subTest(mode=mode):
                receipt = self.fill()
                if mode == 'external': self.backend.text += '目标里的修改'
                elif mode == 'target': self.controller.select(self.backend.windows[0])
                else:
                    p = self.controller._pending_send
                    self.controller._pending_send = (*p[:-1], 0)
                self.controller.send_filled(receipt.send_token, self.callback)
                self.assertEqual(wait_result(self.results)[0], 'error')
                self.assertEqual(self.backend.sent, 0)

    def test_unknown_send_result_consumes_confirmation(self):
        receipt = self.fill()
        self.backend.fail_after_send = True
        for _ in range(2):
            self.controller.send_filled(receipt.send_token, self.callback)
            self.assertEqual(wait_result(self.results)[0], 'error')
        self.assertEqual(self.backend.sent, 1)

    def test_unreadable_target_still_fills_without_arming_send(self):
        self.backend.open_sync = lambda target: (_ for _ in ()).throw(TargetError('不能读回'))
        self.controller.fill_with_receipt('正文', self.callback)
        state, receipt = wait_result(self.results)
        self.assertEqual(state, 'ok')
        self.assertFalse(receipt.send_token)
        self.assertIn('手动发送', receipt.message)
        self.assertEqual(self.backend.text, '前正文后')

    def test_readback_must_prove_exact_insertion(self):
        self.backend.fill = lambda *args: setattr(self.backend, 'text', '前正文后额外文字')
        with patch('talkman_target.time.sleep'):
            self.controller.fill_with_receipt('正文', self.callback)
            state, receipt = wait_result(self.results)
        self.assertEqual(state, 'ok')
        self.assertFalse(receipt.send_token)

    def test_transient_editor_read_before_paste_recovers_then_sends_once(self):
        read = self.backend.read_sync
        attempts = [0]
        def unstable(target, handle):
            attempts[0] += 1
            if attempts[0] == 1:
                raise TargetError('目标主输入框已改变，同步已暂停。')
            return read(target, handle)
        self.backend.read_sync = unstable
        with patch('talkman_target.time.sleep'):
            receipt = self.fill()
            self.controller.send_filled(receipt.send_token, self.callback)
            self.assertEqual(wait_result(self.results)[0], 'ok')
        self.assertEqual(self.backend.sent, 1)
        self.assertEqual([x for x in self.backend.calls if x[0] == 'paste'], [('paste', '正文')])

    def test_transient_editor_read_after_paste_recovers_without_repeating_paste(self):
        read = self.backend.read_sync
        attempts = [0]
        def unstable(target, handle):
            attempts[0] += 1
            if attempts[0] == 2:
                raise TargetError('目标主输入框已改变，同步已暂停。')
            return read(target, handle)
        self.backend.read_sync = unstable
        with patch('talkman_target.time.sleep'):
            receipt = self.fill()
        self.assertTrue(receipt.send_token)
        self.assertEqual(self.backend.sent, 0)
        self.assertEqual([x for x in self.backend.calls if x[0] == 'paste'], [('paste', '正文')])

    def test_recreated_editor_after_fill_and_before_send_is_rechecked(self):
        generation = [0]
        def open_delivery(target):
            return generation[0]
        def read_delivery(target, handle):
            if handle != generation[0]:
                raise TargetError('编辑控件已重建')
            return self.backend.text
        def fill(target, text, token):
            self.backend.calls.append(('paste', text))
            self.backend.text = self.backend.text[:1] + text + self.backend.text[1:]
            generation[0] += 1
            return '已填入'
        def send_delivery(target, handle, text, current):
            self.assertEqual(handle, generation[0])
            self.assertTrue(current())
            self.backend.sent += 1
        self.backend.open_delivery = open_delivery
        self.backend.read_delivery = read_delivery
        self.backend.send_delivery = send_delivery
        self.backend.fill = fill
        receipt = self.fill()
        generation[0] += 1
        self.controller.send_filled(receipt.send_token, self.callback)
        self.assertEqual(wait_result(self.results)[0], 'ok')
        self.assertEqual(self.backend.sent, 1)
        self.assertEqual([x for x in self.backend.calls if x[0] == 'paste'], [('paste', '正文')])

    def test_recreated_editor_with_changed_text_never_sends(self):
        receipt = self.fill()
        self.backend.text += '目标中又有新字'
        self.controller.send_filled(receipt.send_token, self.callback)
        self.assertEqual(wait_result(self.results)[0], 'error')
        self.assertEqual(self.backend.sent, 0)

    def test_permanently_changed_editor_after_paste_never_arms_send(self):
        read = self.backend.read_sync
        def changed(target, handle):
            if any(x[0] == 'paste' for x in self.backend.calls):
                raise TargetError('目标输入框已改变')
            return read(target, handle)
        self.backend.read_sync = changed
        with patch('talkman_target.time.sleep'):
            self.controller.fill_with_receipt('正文', self.callback)
            state, receipt = wait_result(self.results)
        self.assertEqual(state, 'ok')
        self.assertFalse(receipt.send_token)
        self.assertEqual(self.backend.sent, 0)

    def test_repeated_prefix_still_confirms_exact_insertion(self):
        self.backend.text = '前后'
        self.controller.fill_with_receipt('后前后', self.callback)
        state, receipt = wait_result(self.results)
        self.assertEqual(state, 'ok')
        self.assertEqual(self.backend.text, '前后前后后')
        self.assertTrue(receipt.send_token)
        self.controller.send_filled(receipt.send_token, self.callback)
        self.assertEqual(wait_result(self.results)[0], 'ok')
        self.assertEqual(self.backend.sent, 1)

    def test_queued_fill_does_not_follow_newly_selected_target(self):
        epoch = self.controller._sync_epoch
        self.controller.select(TargetWindow(99, 42, '其他', '其他', 'other.exe'))
        self.controller._fill(self.callback, '正文', receipt=True, requested_epoch=epoch)
        self.assertEqual(wait_result(self.results)[0], 'error')
        self.assertEqual(self.backend.text, '前后')


if __name__ == '__main__': unittest.main()
