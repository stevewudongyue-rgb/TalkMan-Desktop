"""应用指令与正文改写分流、持久化、确认及授权检查。"""
import gc
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch, Mock

from talkman_commands import Command, parse_command
from talkman_qwen import QwenSettings
from talkman_asr import ASRSettings
from talkman_records import DeliveryHistory
from test_talkman_app import FakeVoice, FakeClient
from test_talkman_license_support import licensed_app


class CommandParsingTests(unittest.TestCase):
    def test_common_commands_and_preserved_word_spaces(self):
        examples = {
            '请打开自动标点。': Command('option', ('自动标点', True)),
            '把 AI 分行排版关掉': Command('option', ('AI分行排版', False)),
            '把上一条历史放入编辑框': Command('restore', 'delivery'),
            '把指令的快捷键设置成 Ctrl 加 Alt 加 J': Command('shortcut', ('rewrite','Ctrl+Alt+J')),
            '记住约定词 Colo Play，不是可乐派': Command('convention', ('Colo Play', '可乐派')),
            '把可乐派记为Colo Play': Command('convention', ('Colo Play', '可乐派')),
            '记录到草稿': Command('save'),
            '记录到历史': Command('manual_history'),
            '把自动整理延迟改成15秒': Command('delay',15),
        }
        for text, expected in examples.items():
            with self.subTest(text=text):
                self.assertEqual(parse_command(text), expected)

    def test_ordinary_rewrite_never_becomes_an_app_action(self):
        for text in ('把这句话改成打开自动标点', '把苹果改成梨', '把选中的文字移到全文最后',
                     '给这句话加标点', '不要打开自动标点', '保存到草稿簿然后发送给所有人'):
            with self.subTest(text=text):
                self.assertIsNone(parse_command(text))


class CommandUITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='TalkMan-command-')
        self.folder = Path(self.temp.name)
        self.app = licensed_app(self.folder, session_factory=FakeVoice, file_session_factory=FakeVoice,
                                client_factory=FakeClient, desktop_services=False)
        self.app.settings = QwenSettings(api_key='sk-test-no-network')
        self.app.after_cancel(self.app.poll_job)
        self.app.poll_job = None
        self.app.set_text('这里是原正文。')
        self.app.cancel_auto_timer()
        self.commands = self.app.app_commands

    def tearDown(self):
        self.app.busy = False
        self.app.close_app()
        gc.collect()
        self.temp.cleanup()

    def say(self, text):
        a = self.app
        with patch('talkman_app.prepare_microphone_permission', return_value=True):
            self.assertTrue(a.start_voice('rewrite'))
        with patch.object(a, 'make_text_client', wraps=a.make_text_client) as client:
            a.finish_voice(text)
            return client.call_count

    def test_voice_from_button_focus_routes_settings_without_changing_document(self):
        a = self.app
        a.rewrite_button.focus_force()
        original = a.current_text()
        self.assertEqual(self.say('关闭自动标点'), 0)
        self.assertFalse(a.punct_var.get())
        self.assertFalse(json.loads(a.ui_path.read_text(encoding='utf-8'))['auto_punct'])
        self.assertEqual(a.current_text(), original)
        self.assertEqual(self.say('打开自动标点'), 0)
        self.assertTrue(a.punct_var.get())

    def test_asr_only_can_control_app_without_a_text_model(self):
        a = self.app
        a.settings.api_key = ''
        a.free_mode_var.set(True)
        with patch.object(type(a.free_service), 'configured', new_callable=lambda: property(lambda self: True)):
            self.assertEqual(self.say('关闭自动标点'), 0)
        self.assertFalse(a.punct_var.get())

    def test_restore_history_requires_confirmation_saves_previous_and_rejects_stale(self):
        a, c = self.app, self.commands
        a.deliveries.add('上一条已填入正文', '测试 Agent', '测试对话')
        original = a.current_text()
        self.say('把上一条历史放入编辑框')
        self.assertTrue(c.active)
        self.assertEqual(a.current_text(), original)
        self.say('确认执行')
        self.assertEqual(a.current_text(), '上一条已填入正文')
        self.assertTrue(any(x['text'] == original for x in a.saved_history))
        c.handle('把上一条历史放入编辑框')
        a.set_text('确认前新写的内容')
        c.handle('确认执行')
        self.assertEqual(a.current_text(), '确认前新写的内容')
        self.assertIn('已变化', a.status_var.get())

    def test_manual_history_has_honest_status_and_does_not_fill(self):
        a = self.app
        with patch.object(a, 'fill_to_target') as fill:
            self.commands.handle('记录到历史')
        fill.assert_not_called()
        item = DeliveryHistory(self.folder).items[0]
        self.assertEqual(item['status'], '手动留存')
        self.assertEqual(item['title'], '手动记录')
        self.assertEqual(item['text'], a.current_text())

    def test_shortcut_confirmation_conflict_and_persistence(self):
        a, c = self.app, self.commands
        old = a.shortcuts.copy()
        c.handle('把指令快捷键改成Ctrl加Alt加J')
        self.assertEqual(a.shortcuts, old)
        self.say('确认执行')
        self.assertEqual(a.shortcuts['rewrite'], 'Control+Alt+J')
        self.assertEqual(json.loads((self.folder/'shortcuts.json').read_text())['rewrite'], 'Control+Alt+J')
        before = a.shortcuts.copy()
        c.handle('把指令快捷键改成F8')
        self.assertFalse(c.active)
        self.assertEqual(a.shortcuts, before)
        c.handle('把指令快捷键改成Ctrl加C')
        self.assertFalse(c.active)
        self.assertEqual(a.shortcuts, before)

    def test_conventions_merge_aliases_and_reject_cross_word_conflicts(self):
        a, c = self.app, self.commands
        c.handle('记住约定词 Colo Play，不是可乐派')
        self.assertEqual(a.conventions.items, [])
        c.handle('确认执行')
        c.handle('记住约定词 Colo Play，不是可乐拍')
        c.handle('确认执行')
        self.assertEqual(len(a.conventions.items), 1)
        self.assertEqual(set(a.conventions.items[0]['aliases']), {'可乐派','可乐拍'})
        c.handle('记住约定词 Other，不是可乐派')
        c.handle('确认执行')
        self.assertEqual(len(a.conventions.items), 1)
        self.assertIn('已指向', a.status_var.get())

    def test_commands_respect_feature_entitlements(self):
        a = self.app
        with patch.object(a, 'require_feature', return_value=False):
            for command in ('保存到草稿簿', '记录到历史', '把上一条历史放入编辑框',
                            '记住约定词Colo Play，不是可乐派'):
                self.commands.handle(command)
        self.assertEqual(a.saved_history, [])
        self.assertEqual(a.deliveries.items, [])
        self.assertEqual(a.conventions.items, [])
        self.assertFalse(self.commands.active)

    def test_clear_voice_confirm_and_cancel_are_undoable(self):
        a = self.app
        original = a.current_text()
        self.say('清空编辑区')
        self.assertEqual(a.current_text(), original)
        self.say('取消操作')
        self.assertEqual(a.current_text(), original)
        self.say('清空编辑区')
        self.say('确认执行')
        self.assertEqual(a.current_text(), '')
        self.say('上一步')
        self.assertEqual(a.current_text(), original)

    def test_fill_command_never_sends_and_voice_keeps_send_receipt(self):
        a, c = self.app, self.commands
        pending = dict(token='test', expires=time.monotonic()+60, revision=a.revision,
                       text=a.current_text(), ready_at=0)
        a.pending_delivery = pending
        with patch.object(a, 'fill_to_target') as fill:
            c.handle('填入Agent')
            fill.assert_not_called()
            self.say('下发')
            self.assertTrue(c.active)
            fill.assert_not_called()
            self.assertEqual(a.pending_delivery, pending)
            self.say('确认执行')
            fill.assert_called_once()

    def test_save_failure_does_not_flip_in_memory_option(self):
        a = self.app
        a.punct_var.set(True)
        with patch.object(a, 'save_ui_preferences', side_effect=OSError()):
            self.commands.handle('关闭自动标点')
        self.assertTrue(a.punct_var.get())

    def test_ai_preview_can_be_confirmed_by_voice_and_unknown_command_keeps_readonly(self):
        a = self.app
        a.preview_ai_var.set(True)
        apply = Mock()
        a.review_result(a.current_text(), 'AI 新文本', '改写', apply)
        self.assertTrue(a.window_controller.command_overlay_open())
        self.say('改成别的')
        self.assertIsNotNone(a.ai_review)
        self.assertEqual(a.text.cget('state'), 'disabled')
        apply.assert_not_called()
        self.say('确认执行')
        apply.assert_called_once()
        self.assertIsNone(a.ai_review)

    def test_target_voice_waits_for_input_position_and_rejects_ambiguous_or_late_result(self):
        a, c = self.app, self.commands
        target = object()
        rows = [dict(id=7, app='ChatGPT', title='一个对话', label='ChatGPT · 一个对话')]
        a.target_windows = {7:target}
        with patch.object(a, 'refresh_target_windows') as scan, patch.object(a.position_picker, 'open') as picker, patch.object(a, 'choose_target') as choose:
            c.handle('选择Agent ChatGPT')
            callback = scan.call_args.args[0]
            callback('ok', rows+rows)
            picker.assert_not_called()
            c.handle('选择Agent ChatGPT')
            callback = scan.call_args.args[0]
            callback('ok', rows)
            picker.assert_called_once_with(target)
            choose.assert_not_called()
            picker.reset_mock()
            c.handle('选择Agent ChatGPT')
            callback = scan.call_args.args[0]
            c.handle('取消操作')
            callback('ok', rows)
            picker.assert_not_called()


if __name__ == '__main__':
    unittest.main()
