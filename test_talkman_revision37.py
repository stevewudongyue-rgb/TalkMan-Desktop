"""换对话重新定位、窗口让位、外观轮换和兑换入口；不操作真实 Agent。"""
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch
import unittest

import test_talkman_input_positions as positions
import test_talkman_position_ui as position_ui
import test_talkman_window as windows
from test_talkman_target import wait_result
from talkman_target import TargetError
from talkman_input_positions import main_input_role
from talkman_qwen import QwenSettings


class RecoveryTests(unittest.TestCase):
    setUp = positions.PositionTransactionTests.setUp
    tearDown = positions.PositionTransactionTests.tearDown
    callback = positions.PositionTransactionTests.callback
    begin = positions.PositionTransactionTests.begin
    command = positions.PositionTransactionTests.command

    def choose_main(self):
        b = self.backend
        b.windows = [replace(b.windows[0], app='ChatGPT', identity='ChatGPT.exe')]
        self.controller.select(b.windows[0])
        b.validate_window = lambda target: b.windows[0]
        b.input_position_role = lambda target, e: ('id', 'prompt-textarea') if e.startswith('main') else None
        def resolve(target, role):
            candidates = [e for e in b.inputs if b.input_position_role(target, e) == role]
            if len(candidates) != 1:
                raise TargetError('需要重新选择')
            return candidates[0]
        b.resolve_main_input = Mock(side_effect=resolve)
        self.begin()
        self.assertEqual(self.command('confirm')[0], 'confirmed')

    def new_conversation(self, title='另一对话'):
        self.backend.windows = [replace(self.backend.windows[0], title=title)]
        self.backend.inputs = ['main-new', 'answer']
        self.backend.texts['main-new'] = '新对话原文'

    def test_new_title_and_rebuilt_control_fill_once_at_current_main(self):
        self.choose_main()
        self.new_conversation()
        self.controller.fill_with_receipt('追加', self.callback)
        state, receipt = wait_result(self.results)
        self.assertEqual(state, 'ok', receipt)
        self.assertEqual(self.backend.texts['main-new'], '新对话原文追加')
        self.assertEqual(self.backend.texts['main'], '主框原文')
        self.assertEqual(self.backend.texts['answer'], '回答原文')
        self.assertEqual(self.controller.position_label, '主输入框')
        self.assertEqual(receipt.title, '另一对话')

    def test_same_title_rebuilt_main_is_resolved(self):
        self.choose_main()
        self.new_conversation(self.backend.windows[0].title)
        self.controller.fill('新内容', self.callback)
        self.assertEqual(wait_result(self.results)[0], 'ok')
        self.assertTrue(self.backend.texts['main-new'].endswith('新内容'))

    def test_multiple_candidates_or_changed_process_never_fallback(self):
        self.choose_main()
        self.backend.inputs = ['main-new', 'main-second', 'answer']
        self.controller.fill('不应写入', self.callback)
        self.assertEqual(wait_result(self.results)[0], 'error')
        self.backend.inputs.remove('main-second')
        self.backend.windows = [replace(self.backend.windows[0], process_started='new-process')]
        self.controller.fill('不应写入', self.callback)
        self.assertEqual(wait_result(self.results)[0], 'error')
        self.assertFalse(any(call[0] == 'paste' for call in self.backend.calls))

    def test_cancel_during_resolution_cannot_write(self):
        self.choose_main()
        self.backend.inputs = ['main-rebuilt', 'answer']
        self.backend.resolve_main_input.side_effect = lambda *args: (self.controller.stop_sync(), 'main')[1]
        self.controller.fill('不应写入', self.callback)
        self.assertEqual(wait_result(self.results)[0], 'error')
        self.assertFalse(any(call[0] == 'paste' for call in self.backend.calls))

    def test_old_send_and_active_sync_do_not_follow_new_conversation(self):
        self.choose_main()
        self.controller.fill_with_receipt('旧内容', self.callback)
        state, receipt = wait_result(self.results)
        self.assertEqual(state, 'ok')
        self.new_conversation()
        self.controller.refresh(self.callback)
        self.assertEqual(wait_result(self.results)[0], 'ok')
        self.controller.send_filled(receipt.send_token, self.callback)
        self.assertEqual(wait_result(self.results)[0], 'error')
        self.backend.resolve_main_input.reset_mock()
        self.controller.sync('不应同步', self.callback)
        self.assertEqual(wait_result(self.results)[0], 'error')
        self.backend.resolve_main_input.assert_not_called()
        self.assertEqual(self.backend.sent, 0)
        self.assertEqual(self.backend.texts['main-new'], '新对话原文')

    def test_role_requires_known_purpose_not_generic_editable_label(self):
        target = replace(self.backend.windows[0], app='ChatGPT', identity='ChatGPT.exe')
        self.assertEqual(main_input_role(target, 'prompt-textarea', []), ('id', 'prompt-textarea'))
        self.assertIsNone(main_input_role(target, 'editor', ['输入', '代码编辑器', '请输入回答']))
        self.assertIsNone(main_input_role(replace(target, app='Notepad', identity='notepad.exe'), 'prompt-textarea', []))


class NativeRoleTests(unittest.TestCase):
    def test_mac_recreated_main_uniqueness_and_hidden_candidates(self):
        fixture = positions.MacPositionTests()
        fixture.setUp()
        b, api = fixture.backend, fixture.api
        target = fixture.target
        fixture.first.attributes['AXIdentifier'] = 'prompt-textarea'
        role = b.input_position_role(target, fixture.first)
        self.assertIsNotNone(role)
        fixture.first.attributes['AXHidden'] = True
        fixture.window.attributes['AXTitle'] = '新对话'
        new = api.add_input(fixture.window, label='')
        new.attributes.update(AXIdentifier='prompt-textarea', AXPosition=(1, 2), AXSize=(300, 50))
        target = replace(target, title='新对话')
        self.assertIs(b.resolve_main_input(target, role), new)
        fixture.first.attributes['AXHidden'] = False
        with self.assertRaises(TargetError):
            b.resolve_main_input(target, role)


class PickerVisibilityTests(unittest.TestCase):
    setUp = position_ui.PositionUItests.setUp
    tearDown = position_ui.PositionUItests.tearDown
    reply = position_ui.PositionUItests.reply
    pump = position_ui.PositionUItests.pump
    snapshot = position_ui.PositionUItests.snapshot

    def test_topmost_editor_yields_and_cancel_restores_geometry_selection(self):
        a, p = self.app, self.picker
        a.topmost_var.set(True)
        a.window_controller.apply_preferences()
        a.update()
        before = self.snapshot()
        p.open(); self.reply()
        self.assertEqual(a.state(), 'normal')
        self.assertFalse(a.attributes('-topmost'))
        a.window_controller.trigger('global_show')
        self.assertEqual(a.state(), 'normal')
        a.window_controller.apply_preferences()
        self.assertFalse(a.attributes('-topmost'))
        p.cancel(); a.update()
        self.assertTrue(a.attributes('-topmost'))
        self.assertEqual(self.snapshot(), before)

    def test_navigation_voice_hotkey_stays_on_picker(self):
        a, p = self.app, self.picker
        a.settings = QwenSettings(api_key='sk-offline-test')
        p.open(); self.reply()
        with patch('talkman_app.prepare_microphone_permission', return_value=True):
            a.window_controller.trigger('global_rewrite')
        self.assertTrue(a.busy)
        self.assertEqual(a.state(), 'normal')
        a.finish_voice('下一个输入框')
        self.assertEqual(self.service.position_action.call_args.args[0], 'next')
        self.assertEqual(a.current_text(), '保留原文和已选择的内容。')
        self.assertEqual(a.state(), 'normal')

    def test_unknown_navigation_speech_cannot_edit_hidden_document(self):
        a, p = self.app, self.picker
        a.settings = QwenSettings(api_key='sk-offline-test')
        p.open(); self.reply()
        with patch('talkman_app.prepare_microphone_permission', return_value=True):
            a.start_voice('rewrite')
        with patch.object(a, 'make_text_client') as model:
            a.finish_voice('全部删掉')
        model.assert_not_called()
        self.assertEqual(a.current_text(), '保留原文和已选择的内容。')


class ThemeLicenseTests(unittest.TestCase):
    setUp = windows.WindowTests.setUp
    tearDown = windows.WindowTests.tearDown

    def test_theme_button_cycles_all_three_without_menu_or_selection_loss(self):
        a = self.app
        a.set_theme('dark')
        a.set_text('保留正文')
        a.text.tag_add('sel', '1.0', '1.2')
        selection = a.get_selection()
        for theme in ('light', 'swiss', 'dark'):
            a.theme_button.invoke(); a.update()
            self.assertEqual(a.theme_key, theme)
            self.assertEqual(a.get_selection(), selection)
        a.geometry('1100x820+40+40'); a.set_theme('swiss'); a.update()
        # macOS 的 CI 屏幕可能约束实际窗口；按真正可用的尺寸核验响应布局。
        roomy = a.winfo_height() >= 680 and a.winfo_width() >= 850
        self.assertEqual(a.wordmark.winfo_height(), 104 if roomy else 40)
        self.assertTrue(a.replace_button.find_withtag('section'))
        a.geometry('760x520+40+40'); a.update()
        self.assertLessEqual(a.wordmark.winfo_height(), 40)
        self.assertGreater(a.text.winfo_height(), 60)

    def test_legacy_license_link_opens_public_version_page_without_activation(self):
        a = self.app
        a.open_settings('license'); a.update()
        self.assertEqual(a.settings_notebook.select(), str(a.settings_tabs['about']))
        self.assertFalse(hasattr(a, 'license_code_form'))
        self.assertFalse(hasattr(a, 'license_change_button'))
        for feature in ('records', 'conventions', 'translation'):
            self.assertTrue(a.has_feature(feature))


if __name__ == '__main__':
    unittest.main()
