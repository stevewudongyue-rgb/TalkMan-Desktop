"""位置默认记忆、跨对话恢复与主动选择边界；不读取真实窗口或正文。"""
import json
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch
from types import SimpleNamespace
import unittest

from talkman_target import TargetController, TargetError, TargetSelection
from talkman_target_windows import WindowsTargetBackend, TargetWindow
from test_talkman_input_positions import MacPositionTests
from test_talkman_input_positions import PositionTransactionTests
from test_talkman_target import wait_result
from test_talkman_position_ui import PositionUItests


class PositionMemoryTests(unittest.TestCase):
    setUp = PositionTransactionTests.setUp
    tearDown = PositionTransactionTests.tearDown
    callback = PositionTransactionTests.callback
    begin = PositionTransactionTests.begin
    command = PositionTransactionTests.command
    choose_second = PositionTransactionTests.choose_second

    def main_support(self):
        b = self.backend
        b.windows = [replace(b.windows[0], app='ChatGPT', identity='ChatGPT.exe')]
        self.controller.select(b.windows[0])
        b.validate_window = lambda target: next((w for w in b.windows if w.id == target.id), None)
        b.input_position_role = lambda target, element: ('id', 'prompt-textarea') if element.startswith('main') else None
        def resolve(target, role=None):
            found = [e for e in b.inputs if b.input_position_role(target, e)
                     and (role is None or b.input_position_role(target, e) == role)]
            if len(found) != 1:
                raise TargetError('主输入框不能唯一确认')
            return found[0]
        b.resolve_main_input = Mock(side_effect=resolve)

    def fill(self, text='追加'):
        self.controller.fill_with_receipt(text, self.callback)
        return wait_result(self.results)

    def test_reselect_same_agent_keeps_custom_input_without_opening_picker(self):
        self.choose_second()
        for _ in range(2):
            self.controller.select(self.backend.windows[0])
            self.assertFalse(self.controller.choosing_position)
            self.assertEqual(self.fill()[0], 'ok')
        self.assertEqual(self.backend.texts['answer'], '回答原文追加追加')
        self.assertEqual(self.backend.texts['main'], '主框原文')

    def test_switch_between_agents_restores_each_custom_position(self):
        self.choose_second()
        original = self.backend.windows[0]
        other = replace(original, hwnd=22, pid=82, app='另一个Agent', identity='other.exe')
        self.controller.select(other)
        self.begin()
        self.command('confirm')
        self.assertEqual(self.fill('第二应用')[0], 'ok')
        self.controller.select(original)
        self.assertEqual(self.fill('第一应用')[0], 'ok')
        self.assertEqual(self.backend.texts['main'], '主框原文第二应用')
        self.assertEqual(self.backend.texts['answer'], '回答原文第一应用')

    def test_new_conversation_defaults_to_main_then_old_custom_is_restored(self):
        self.main_support()
        self.choose_second()
        original = self.backend.windows[0]
        self.backend.windows = [replace(original, title='新对话')]
        self.assertEqual(self.fill('新的')[0], 'ok')
        self.backend.windows = [original]
        self.assertEqual(self.fill('旧的')[0], 'ok')
        self.assertEqual(self.backend.texts, dict(main='主框原文新的', answer='回答原文旧的'))
        self.assertFalse(self.controller.choosing_position)

    def test_conversation_preferences_survive_rebinding_main_and_returning(self):
        self.main_support()
        original = self.backend.windows[0]
        self.begin(); self.command('confirm')
        self.backend.windows = [replace(original, title='问卷对话')]
        self.begin(); self.command('next'); self.command('confirm')
        self.backend.windows = [original]
        self.assertEqual(self.fill('主框')[0], 'ok')
        self.backend.windows = [replace(original, title='问卷对话')]
        self.assertEqual(self.fill('问卷')[0], 'ok')
        self.assertEqual(self.backend.texts, dict(main='主框原文主框', answer='回答原文问卷'))

    def test_main_preference_survives_restart_and_rebuilt_node(self):
        self.main_support()
        self.begin(); self.command('confirm')
        self.controller.close()
        self.backend.inputs = ['main-rebuilt', 'answer']
        self.backend.texts['main-rebuilt'] = ''
        self.controller = TargetController(Path(self.temp.name), backend=self.backend)
        self.assertEqual(self.fill('重启后')[0], 'ok')
        self.assertEqual(self.backend.texts['main-rebuilt'], '重启后')
        saved = json.loads((Path(self.temp.name) / 'target.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['target']['input_role'], ['id', 'prompt-textarea'])
        self.assertNotIn('hwnd', str(saved))
        self.assertNotIn('主框原文', str(saved))

    def test_main_restart_can_find_new_conversation_without_selecting_again(self):
        self.main_support()
        self.begin(); self.command('confirm')
        self.controller.close()
        self.backend.windows = [replace(self.backend.windows[0], title='重启后新对话', hwnd=42, pid=88)]
        self.controller = TargetController(Path(self.temp.name), backend=self.backend)
        self.assertEqual(self.fill()[0], 'ok')
        self.assertFalse(self.controller.choosing_position)

    def test_saved_custom_preference_is_not_overridden_by_another_main(self):
        self.main_support()
        self.choose_second()
        original = self.backend.windows[0]
        self.backend.windows = [replace(original, title='另一对话')]
        self.begin(); self.command('confirm')
        self.backend.windows = [original]
        self.backend.inputs.remove('answer')
        self.assertEqual(self.fill()[0], 'error')
        self.assertEqual(self.backend.texts['main'], '主框原文')
        self.assertFalse(self.controller.choosing_position)

    def test_multiple_main_inputs_do_not_guess_or_open_picker(self):
        self.main_support()
        self.choose_second()
        self.backend.windows = [replace(self.backend.windows[0], title='新对话')]
        self.backend.inputs.append('main-second')
        self.assertEqual(self.fill()[0], 'error')
        self.assertEqual(self.backend.texts['main'], '主框原文')
        self.assertFalse(self.controller.choosing_position)

    def test_same_title_different_window_cannot_reuse_native_custom_element(self):
        self.choose_second()
        other = replace(self.backend.windows[0], hwnd=99, pid=99)
        self.controller.select(other)
        self.assertEqual(self.fill()[0], 'error')
        self.assertFalse(any(call[0] == 'paste' for call in self.backend.calls))

    def test_window_move_and_resize_keep_same_native_input(self):
        self.choose_second()
        self.backend.validate_input_position = lambda target, element: (840, 120, 480, 140)
        self.assertEqual(self.fill()[0], 'ok')
        self.assertFalse(self.controller.needs_position_relocation)
        self.assertEqual(self.backend.texts['answer'], '回答原文追加')

    def test_disappearing_selected_input_requests_relocation_before_paste(self):
        self.choose_second()
        self.backend.inputs.remove('answer')
        self.assertEqual(self.fill()[0], 'error')
        self.assertTrue(self.controller.needs_position_relocation)
        self.assertEqual(self.backend.texts['main'], '主框原文')
        self.assertFalse(any(call[0] == 'paste' for call in self.backend.calls))

    def test_ambiguous_new_conversation_requests_relocation(self):
        self.main_support()
        self.choose_second()
        self.backend.windows = [replace(self.backend.windows[0], title='新对话')]
        self.backend.inputs.append('main-second')
        self.assertEqual(self.fill()[0], 'error')
        self.assertTrue(self.controller.needs_position_relocation)

    def test_new_choice_invalidates_previous_send_confirmation(self):
        self.main_support()
        self.begin(); self.command('confirm')
        state, receipt = self.fill()
        self.assertEqual(state, 'ok')
        self.choose_second()
        self.controller.send_filled(receipt.send_token, self.callback)
        self.assertEqual(wait_result(self.results)[0], 'error')
        self.assertEqual(self.backend.sent, 0)

    def test_cancelled_selection_does_not_change_memory(self):
        self.choose_second()
        before = self.controller.selection.positions[:]
        self.begin(); self.command('cancel')
        self.assertEqual(self.controller.selection.positions, before)
        self.controller.select(self.backend.windows[0])
        self.assertEqual(self.fill()[0], 'ok')
        self.assertTrue(self.backend.texts['answer'].endswith('追加'))

    def test_memory_is_bounded_and_roundtrips_without_native_handles(self):
        for index in range(28):
            target = replace(self.backend.windows[0], title=f'对话{index}')
            self.controller.select(target)
            self.begin(); self.command('confirm')
        self.assertEqual(len(self.controller._position_bindings), 24)
        loaded = TargetSelection.load(Path(self.temp.name))
        self.assertEqual(len(loaded.positions), 24)
        self.assertEqual(loaded.positions[-1]['title'], '对话27')

    def test_save_failure_preserves_previous_memory(self):
        self.choose_second()
        before = self.controller.selection.positions[:]
        self.begin()
        with patch.object(self.controller.selection, 'save', side_effect=OSError('隔离失败')):
            self.assertEqual(self.command('confirm')[0], 'error')
        self.assertEqual(self.controller.selection.positions, before)
        self.command('cancel')
        self.assertEqual(self.fill()[0], 'ok')
        self.assertTrue(self.backend.texts['answer'].endswith('追加'))


class OptInPickerTests(unittest.TestCase):
    setUp = PositionUItests.setUp
    tearDown = PositionUItests.tearDown

    def test_switching_target_never_opens_position_picker(self):
        from talkman_target import TargetWindow
        target = TargetWindow(11, 42, 'ChatGPT', '对话', 'chatgpt')
        self.app.target_windows = {target.id: target}
        before = self.app.current_text(), self.app.get_selection(), self.app.geometry()
        for _ in range(2):
            self.app.choose_target(target.id)
        self.assertFalse(self.picker.active)
        self.service.begin_positions.assert_not_called()
        self.assertEqual(before, (self.app.current_text(), self.app.get_selection(), self.app.geometry()))

    def test_failed_fill_opens_picker_only_when_position_was_invalidated(self):
        self.app.target_controller.needs_position_relocation = True
        self.app.events.put(('target_fill', None, 'error', '输入框已消失'))
        self.app.poll_events()
        self.assertTrue(self.picker.active)
        self.service.begin_positions.assert_called_once()
        self.assertEqual(self.app.current_text(), '保留原文和已选择的内容。')


class OverlayLayoutTests(unittest.TestCase):
    setUp = PositionUItests.setUp
    tearDown = PositionUItests.tearDown

    def test_all_candidates_are_clickable_without_hiding_main_window(self):
        self.app.position_picker.open()
        cb = self.service.begin_positions.call_args.args[0]
        choices = [dict(index=0, rect=(500, 400, 200, 60)),
                   dict(index=1, rect=(760, 400, 200, 60))]
        cb('ok', dict(index=0, count=2, rect=choices[0]['rect'],
                      title='测试窗口', choices=choices))
        self.app.poll_events()
        picker = self.app.position_picker
        self.assertEqual(self.app.state(), 'normal')
        self.assertFalse(hasattr(picker, 'window'))
        self.assertEqual(len(picker.marks), 12)
        self.assertTrue(all(mark.overrideredirect() for mark in picker.marks))
        self.assertIn('取消选位', self.app.position_selector.cget('text'))

    def test_inspect_reuses_overlay_when_rectangles_are_unchanged(self):
        picker = self.app.position_picker
        picker.open()
        cb = self.service.begin_positions.call_args.args[0]
        choices = [dict(index=0, rect=(500, 400, 200, 60))]
        cb('ok', dict(index=0, count=1, rect=choices[0]['rect'],
                      title='测试窗口', choices=choices))
        self.app.poll_events()
        before = picker.marks[:]
        picker.show_marks(choices)
        self.assertEqual(before, picker.marks)

    def test_overlay_disappears_behind_another_window(self):
        picker = self.app.position_picker
        picker.open()
        choices = [dict(index=0, rect=(500, 400, 200, 60))]
        state = dict(index=0, count=1, rect=choices[0]['rect'], title='测试窗口', choices=choices)
        picker.finish(picker.generation, 'ok', state)
        self.assertTrue(picker.marks)
        with patch.object(picker, 'target_foreground', return_value=False):
            picker.finish(picker.generation, 'inspect', state)
        self.assertEqual(picker.marks, [])
        picker.finish(picker.generation, 'inspect', state)
        self.assertTrue(picker.marks)

    @unittest.skipUnless(sys.platform == 'win32', '需要 Windows 原生命中测试')
    def test_overlay_interior_receives_pointer_hit(self):
        import ctypes
        from ctypes import wintypes
        from talkman_windows_coordinates import physical_screen_coordinates

        picker = self.app.position_picker
        picker.open()
        picker.show_marks([dict(index=0, rect=(500, 400, 200, 60))])
        self.app.update()
        user = ctypes.windll.user32
        user.WindowFromPoint.argtypes = [wintypes.POINT]
        user.WindowFromPoint.restype = wintypes.HWND
        user.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        user.GetAncestor.restype = wintypes.HWND
        with physical_screen_coordinates():
            hit = user.WindowFromPoint(wintypes.POINT(600, 430))
        self.assertEqual(user.GetAncestor(hit, 2), user.GetAncestor(picker.marks[0].winfo_id(), 2))


class MainInputPriorityTests(unittest.TestCase):
    def test_windows_focused_temporary_field_does_not_replace_known_main(self):
        backend = WindowsTargetBackend()
        target = TargetWindow(100, 42, 'ChatGPT', '对话', 'ChatGPT.exe')
        main, temporary = Mock(), object()
        focused = [temporary]
        main.SetFocus.side_effect = lambda: focused.__setitem__(0, main)
        backend.validate_window = Mock(return_value=target)
        backend._bound_input = Mock(return_value=None)
        backend.input_position_role = lambda _target, element: ('id', 'prompt-textarea') if element is main else None
        backend._find_editable = lambda _target, focused_only=False: focused[0] if focused_only else main
        backend._uia = SimpleNamespace(CompareElements=lambda a, b: a is b)
        backend._send_key, backend._paste_unicode = Mock(), Mock()
        user = SimpleNamespace(IsIconic=lambda _: False, SetForegroundWindow=Mock(), GetForegroundWindow=lambda: 100)
        with patch('talkman_target_windows._win_api', return_value=user), patch('talkman_target_windows.time.sleep'):
            backend.fill(target, '应进主框')
        main.SetFocus.assert_called_once()
        backend._paste_unicode.assert_called_once()

    def test_mac_focused_temporary_field_does_not_replace_known_main(self):
        fixture = MacPositionTests()
        fixture.setUp()
        fixture.first.attributes['AXIdentifier'] = 'prompt-textarea'
        fixture.api.focus(fixture.target.pid, fixture.window, fixture.second)
        fixture.backend.fill(fixture.target, '应进主框')
        self.assertIs(fixture.root.attributes['AXFocusedUIElement'], fixture.first)
        self.assertIn(('clipboard', '应进主框'), fixture.api.actions)


if __name__ == '__main__':
    unittest.main()
