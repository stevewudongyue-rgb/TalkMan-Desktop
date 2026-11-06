"""多输入位置事务、原生控件绑定与故障边界；不操作真实 Agent。"""
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from talkman_target import TargetController, TargetError, TargetSelection
from talkman_input_positions import navigation_instruction
from test_talkman_delivery_confirm import ConfirmBackend
from test_talkman_target import wait_result
from test_talkman_target_macos import FakeAPI
from talkman_target_macos import MacTargetBackend
from talkman_target_windows import WindowsTargetBackend


class MultiBackend(ConfirmBackend):
    def __init__(self):
        super().__init__()
        self.inputs = ['main', 'answer']
        self.texts = dict(main='主框原文', answer='回答原文')
        self.bound = None
        self.previews = []

    def list_input_positions(self, target): return self.inputs[:]
    def validate_input_position(self, target, element):
        if element not in self.inputs:
            raise TargetError('输入框已消失')
        return (100, 100 + self.inputs.index(element)*100, 350, 90)
    def preview_input_position(self, target, element): self.previews.append(element)
    def set_input_binding(self, target, element):
        if element is not None: self.validate_input_position(target, element)
        self.bound = element
    def open_sync(self, target): return self.bound or 'main'
    def read_sync(self, target, handle): return self.texts[handle]
    def fill(self, target, text, token):
        field = self.bound or 'main'
        self.calls.append(('paste', field, text))
        self.texts[field] += text
        return '已填入'
    def send_sync(self, target, handle, text, current):
        assert current() and self.texts[handle] == text
        self.sent += 1
        self.calls.append(('send', handle))


class PositionTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.backend = MultiBackend()
        self.controller = TargetController(Path(self.temp.name), backend=self.backend)
        self.controller.select(self.backend.windows[0])
        self.results = []
    def tearDown(self):
        self.controller.close()
        self.temp.cleanup()
    def callback(self, state, value): self.results.append((state, value))
    def command(self, action, index=None):
        self.controller.position_action(action, self.callback, index)
        return wait_result(self.results)
    def begin(self):
        self.controller.begin_positions(self.callback)
        state, value = wait_result(self.results)
        self.assertEqual(state, 'ok')
        return value
    def choose_second(self):
        self.begin()
        self.assertEqual(self.command('next')[1]['index'], 1)
        self.assertEqual(self.command('confirm')[0], 'confirmed')

    def test_choose_unnamed_second_input_and_fill_send_same_control(self):
        self.choose_second()
        self.controller.fill_with_receipt('测试文字', self.callback)
        state, receipt = wait_result(self.results)
        self.assertEqual(state, 'ok')
        self.assertEqual(self.backend.texts, dict(main='主框原文', answer='回答原文测试文字'))
        self.assertTrue(receipt.send_token)
        self.controller.send_filled(receipt.send_token, self.callback)
        self.assertEqual(wait_result(self.results)[0], 'ok')
        self.assertIn(('send', 'answer'), self.backend.calls)
        self.assertNotIn(('send', 'main'), self.backend.calls)

    def test_click_choice_commits_the_exact_control(self):
        preview = self.begin()
        self.assertEqual([item['index'] for item in preview['choices']], [0, 1])
        self.assertEqual(self.command('choose', 1)[0], 'confirmed')
        self.controller.fill_with_receipt('点选结果', self.callback)
        self.assertEqual(wait_result(self.results)[0], 'ok')
        self.assertEqual(self.backend.texts['main'], '主框原文')
        self.assertEqual(self.backend.texts['answer'], '回答原文点选结果')

    def test_click_choice_revalidates_before_remembering(self):
        self.begin()
        self.backend.inputs.remove('answer')
        self.assertEqual(self.command('choose', 1)[0], 'error')
        self.assertEqual(self.controller.selection.metadata.get('input_mode'), None)
        self.command('cancel')
        self.assertFalse(any(call[0] == 'paste' for call in self.backend.calls))

    def test_preview_cancel_keeps_previous_binding_and_never_pastes(self):
        self.choose_second()
        self.begin()
        self.assertEqual(self.command('cancel')[0], 'cancelled')
        self.assertEqual(self.controller.position_label, '位置 2')
        self.assertFalse(any(isinstance(call, tuple) and call[0] == 'paste' for call in self.backend.calls))
        self.controller.fill_with_receipt('保留位置', self.callback)
        self.assertEqual(wait_result(self.results)[0], 'ok')
        self.assertEqual(self.backend.texts['main'], '主框原文')
        self.assertTrue(self.backend.texts['answer'].endswith('保留位置'))

    def test_filling_is_blocked_until_confirmation(self):
        self.begin()
        self.controller.fill_with_receipt('不应写入', self.callback)
        state, message = wait_result(self.results)
        self.assertEqual(state, 'error')
        self.assertIn('确认或取消', message)
        self.assertEqual(self.backend.texts['main'], '主框原文')

    def test_disappearing_input_never_falls_back(self):
        self.choose_second()
        self.backend.inputs.remove('answer')
        self.controller.fill_with_receipt('不应写入', self.callback)
        self.assertEqual(wait_result(self.results)[0], 'error')
        self.assertEqual(self.backend.texts['main'], '主框原文')

    def test_refresh_cannot_authorize_old_control_in_new_conversation(self):
        self.choose_second()
        original = self.controller.selected
        self.backend.windows = [replace(original, title='新的会话')]
        self.controller.refresh(self.callback)
        self.assertEqual(wait_result(self.results)[0], 'ok')
        self.controller.fill_with_receipt('不应写入', self.callback)
        state, message = wait_result(self.results)
        self.assertEqual(state, 'error')
        self.assertIn('重新选择', message)
        self.assertEqual(self.backend.texts, dict(main='主框原文', answer='回答原文'))

    def test_post_paste_validation_failure_does_not_claim_no_paste(self):
        self.choose_second()
        self.backend.input_allows_submit = Mock(side_effect=TargetError('界面重绘'))
        self.controller.fill_with_receipt('只填一次', self.callback)
        state, receipt = wait_result(self.results)
        self.assertEqual(state, 'ok')
        self.assertFalse(receipt.send_token)
        self.assertEqual(self.backend.texts['answer'], '回答原文只填一次')
        self.assertEqual(sum(call[0] == 'paste' for call in self.backend.calls), 1)

    def test_new_process_requires_reselection_not_opaque_handle_reuse(self):
        self.choose_second()
        self.assertEqual(TargetSelection.load(Path(self.temp.name)).metadata['input_mode'], 'explicit')
        self.controller.close()
        self.controller = TargetController(Path(self.temp.name), backend=self.backend)
        self.controller.fill_with_receipt('不应写入', self.callback)
        state, message = wait_result(self.results)
        self.assertEqual(state, 'error')
        self.assertIn('跨重启', message)

    def test_cancel_invalidates_slow_scan_before_callback_or_preview(self):
        started, resume = threading.Event(), threading.Event()
        def scan(target):
            started.set()
            resume.wait(2)
            return self.backend.inputs[:]
        self.backend.list_input_positions = scan
        self.controller.begin_positions(self.callback)
        self.assertTrue(started.wait(1))
        self.controller.position_action('cancel', self.callback)
        resume.set()
        self.assertEqual(wait_result(self.results)[0], 'cancelled')
        self.assertEqual(self.backend.previews, [])

    def test_unknown_submit_does_not_arm_or_send(self):
        self.backend.input_allows_submit = lambda: self.backend.bound != 'answer'
        self.choose_second()
        self.controller.fill_with_receipt('回答', self.callback)
        state, receipt = wait_result(self.results)
        self.assertEqual(state, 'ok')
        self.assertFalse(receipt.send_token)
        self.assertIn('仅填入', receipt.message)
        self.assertEqual(self.backend.sent, 0)

    def test_invalid_index_and_no_inputs_preserve_saved_target(self):
        metadata = self.controller.selection.metadata.copy()
        self.begin()
        self.assertEqual(self.command('index', 90)[0], 'error')
        self.command('cancel')
        self.backend.inputs.clear()
        self.controller.begin_positions(self.callback)
        self.assertEqual(wait_result(self.results)[0], 'error')
        self.assertEqual(metadata, self.controller.selection.metadata)

    def test_navigation_commands_are_exact_and_ordinary_editing_is_untouched(self):
        self.assertEqual(navigation_instruction('请选择第二个输入框'), None)
        self.assertEqual(navigation_instruction('选择第二个输入框。'), ('index', 1))
        self.assertEqual(navigation_instruction('切换到下一个输入框'), ('next', None))
        self.assertEqual(navigation_instruction('确认这个位置'), ('confirm', None))
        for text in ('把第二个输入框改成第三个', '把这句话改为选择输入位置', '全部删掉', '下一个'):
            self.assertIsNone(navigation_instruction(text))


class MacPositionTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI()
        self.app, self.root = self.api.add_app()
        self.window = self.api.add_window(self.root)
        self.first = self.api.add_input(self.window, label='主消息输入')
        self.second = self.api.add_input(self.window, label='')
        for index, element in enumerate((self.first, self.second)):
            element.attributes.update(AXPosition=(100, 100+100*index), AXSize=(350, 90), AXValue='原文')
        self.backend = MacTargetBackend(api=self.api)
        self.target = self.backend.list_windows()[0]

    def test_unlabelled_input_can_be_selected_and_read_without_primary_scan(self):
        positions = self.backend.list_input_positions(self.target)
        self.assertEqual(positions, [self.first, self.second])
        self.backend.set_input_binding(self.target, self.second)
        handle = self.backend.open_delivery(self.target)
        self.assertIs(handle[0], self.second)
        self.assertEqual(self.backend.read_delivery(self.target, handle), '原文')
        self.api.focus(self.target.pid, self.window, self.first)
        self.backend.fill(self.target, '只填第二框')
        self.assertIs(self.root.attributes['AXFocusedUIElement'], self.second)
        self.assertIn(('clipboard', '只填第二框'), self.api.actions)
        self.assertFalse(self.backend.input_allows_submit())

    def test_disappearing_readonly_and_window_change_block_paste(self):
        self.backend.set_input_binding(self.target, self.second)
        for change in ('readonly', 'hidden', 'detached', 'title'):
            with self.subTest(change=change):
                attrs, writable = dict(self.second.attributes), set(self.second.writable)
                if change == 'readonly': self.second.writable.remove('AXValue')
                if change == 'hidden': self.second.attributes['AXHidden'] = True
                if change == 'detached': self.second.attributes['AXParent'] = None
                if change == 'title': self.window.attributes['AXTitle'] = '另一对话'
                with self.assertRaises(TargetError):
                    self.backend.fill(self.target, '不应填入')
                self.second.attributes, self.second.writable = attrs, writable
                self.window.attributes['AXTitle'] = '对话'
        self.assertNotIn(('paste',), self.api.actions)

    def test_hidden_password_and_readonly_inputs_are_excluded(self):
        self.second.attributes['AXSubrole'] = 'AXSecureTextField'
        self.assertEqual(self.backend.list_input_positions(self.target), [self.first])
        self.second.attributes.pop('AXSubrole')
        self.second.attributes['AXHidden'] = True
        self.assertEqual(self.backend.list_input_positions(self.target), [self.first])


class WindowsPositionTests(unittest.TestCase):
    def setUp(self):
        self.backend = WindowsTargetBackend()
        self.target = SimpleNamespace(id=9, pid=7, identity='agent.exe', title='对话')
        self.main = SimpleNamespace(CurrentBoundingRectangle=(10, 10, 200, 40))
        self.answer = SimpleNamespace(CurrentBoundingRectangle=(10, 70, 200, 40), CurrentAutomationId='answer')
        self.backend.validate_window = Mock(return_value=self.target)
        self.backend._window_element = Mock(return_value='root')
        self.backend._is_descendant_of = Mock(return_value=True)
        self.backend._is_editable = Mock(return_value=True)
        self.backend._uia = SimpleNamespace(GetFocusedElement=lambda: self.main, CompareElements=lambda a,b: a is b)

    def test_pinned_input_overrides_main_scan_and_rejects_other_focus(self):
        self.backend.set_input_binding(self.target, self.answer)
        self.assertIs(self.backend._find_editable(self.target), self.answer)
        self.assertIsNone(self.backend._find_editable(self.target, focused_only=True))
        self.assertFalse(self.backend.input_allows_submit())
        self.backend._is_descendant_of.return_value = False
        with self.assertRaises(TargetError): self.backend._find_editable(self.target)

    def test_window_change_never_rebinds_by_rectangle_or_name(self):
        self.backend.set_input_binding(self.target, self.answer)
        changed = SimpleNamespace(id=9, pid=7, identity='agent.exe', title='另一个会话')
        with self.assertRaises(TargetError): self.backend._bound_input(changed)


if __name__ == '__main__':
    unittest.main()
