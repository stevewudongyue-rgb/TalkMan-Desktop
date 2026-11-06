"""使用隔离数据和假录音验证学习入口，禁止调用真实服务。"""
from test_talkman_license_support import licensed_app
import gc
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from talkman_app import TalkManApp
from talkman_qwen import QwenSettings, TextEdit, TextMove
from test_talkman_app import FakeVoice, FakeClient


class Revision25Tests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix='TalkMan-revision25-')
        self.app = licensed_app(Path(self.folder.name), session_factory=FakeVoice,
            file_session_factory=FakeVoice, client_factory=FakeClient, desktop_services=False)
        self.app.settings = QwenSettings(api_key='sk-offline-test')
        self.app.after_cancel(self.app.poll_job)
        self.app.poll_job = None
        self.app.run_worker = Mock()
        self.app.geometry('900x640+20+20')
        self.app.update()
        self.permission = patch('talkman_app.prepare_microphone_permission', return_value=True)
        self.permission.start()

    def tearDown(self):
        self.app.close_app()
        self.app = None
        self.permission.stop()
        FakeVoice.sessions.clear()
        gc.collect()
        self.folder.cleanup()

    def select(self, start, end):
        self.app.text.tag_remove('sel', '1.0', 'end')
        self.app.text.tag_add('sel', f'1.0 + {start} chars', f'1.0 + {end} chars')
        self.app.update_meta()

    def voice_fix(self, prefix='品牌：'):
        a = self.app
        a.set_text(prefix+'可乐派。')
        self.select(len(prefix), len(prefix)+3)
        self.assertTrue(a.start_voice('replace'))
        a.show_voice_text('Colo')
        a.show_voice_text('Colo Play')
        a.finish_voice('Colo Play')
        a.cancel_auto_timer()

    def test_three_voice_corrections_prompt_without_stealing_focus_or_writing_terms(self):
        a = self.app
        for prefix in ['第一轮：', '第二轮：', '第三轮：']:
            self.voice_fix(prefix)
        a.text.focus_force()
        a.refresh_convention_hint(force=True)
        a.update()
        self.assertTrue(a.learning_hint.winfo_ismapped())
        self.assertEqual(a.focus_get(), a.text)
        self.assertEqual(a.conventions.items, [])
        a.review_learned_word()
        a.update()
        self.assertEqual(a.term_form['word'].get(), 'Colo Play')
        self.assertEqual(a.term_form['aliases'].get(), '可乐派')
        before = a.current_text()
        a.term_save()
        a.refresh_convention_hint(force=True)
        self.assertFalse(a.learning_hint.winfo_ismapped())
        self.assertEqual(a.current_text(), before)
        self.assertEqual(a.conventions.snapshot().apply('可乐派'), 'Colo Play')

    def test_native_selection_typing_groups_intermediate_changes(self):
        a = self.app
        a.set_text('品牌：可乐拍。')
        self.select(3, 6)
        a.text.delete('sel.first', 'sel.last')
        a.on_text_modified()
        a.text.insert('1.3', '可乐')
        a.on_text_modified()
        self.assertEqual(a.correction_memory.items, [])
        a.text.insert('1.5', '派')
        a.on_text_modified()
        a.flush_manual_learning()
        item = a.correction_memory.candidates()[0]
        self.assertEqual((item['old'], item['new'], item['count']), ('可乐拍', '可乐派', 1))
        a.capture_edit()
        a.undo()
        a.redo()
        self.assertEqual(a.correction_memory.candidates()[0]['count'], 1)

    def test_cancelled_voice_and_unconfirmed_instruction_are_not_evidence(self):
        a = self.app
        a.set_text('可乐派')
        self.select(0, 3)
        a.start_voice('replace')
        a.show_voice_text('Colo Play')
        a.cancel_operation()
        self.assertEqual(a.correction_memory.items, [])
        a.preview_ai_var.set(True)
        a.start_voice('rewrite')
        a.review_voice_result('Colo Play')
        self.assertEqual(a.correction_memory.items, [])
        a.ai_review_finish(False)
        a.start_voice('rewrite')
        a.review_voice_result('Colo Play')
        a.ai_review_finish(True)
        self.assertEqual(a.correction_memory.candidates()[0]['count'], 1)
        a.undo()
        self.select(0, 3)
        a.start_voice('rewrite')
        a.review_voice_result('Colo Play')
        a.ai_review_finish(True)
        self.assertEqual(a.correction_memory.candidates()[0]['count'], 1)

    def test_automatic_organizing_restore_and_insert_do_not_learn(self):
        a = self.app
        a.set_text('可乐派')
        a.apply_ai_result('可乐派', 'Colo Play', '整理')
        a.set_text('其他草稿')
        a.undo()
        a.redo()
        a.text.tag_remove('sel', '1.0', 'end')
        a.start_voice('insert')
        a.finish_voice('可乐派')
        self.assertEqual(a.correction_memory.items, [])

    def test_observed_alias_merges_existing_and_cancel_is_noop(self):
        a = self.app
        a.conventions.put('Colo Play', ['旧别名'])
        self.voice_fix()
        a.remember_selection('Colo Play')
        a.update()
        self.assertEqual(a.term_form['aliases'].get(), '旧别名，可乐派')
        self.assertEqual(a.focus_get(), a.term_form['aliases_entry'])
        a.close_convention()
        self.assertEqual(a.conventions.items[0]['aliases'], ['旧别名'])

    def test_grouped_reminder_prefills_multiple_aliases_and_conflict_stays_reviewable(self):
        a = self.app
        for i, alias in enumerate(['可乐派', '科洛派', '可乐派']):
            a.correction_memory.observe(alias, 'Colo Play', str(i))
        a.refresh_convention_hint(force=True)
        self.assertIn('2 种误写', a.learning_hint_label.cget('text'))
        a.review_learned_word()
        self.assertIn('可乐派', a.term_form['aliases'].get())
        self.assertIn('科洛派', a.term_form['aliases'].get())
        a.conventions.put('其他名字', ['已占用的读法'])
        a.term_form['aliases'].set('可乐派，已占用的读法')
        a.term_save()
        a.update()
        self.assertIn('已指向“其他名字”', a.term_feedback.get())
        self.assertIsNotNone(a.convention_panel)
        self.assertEqual(len(a.conventions.items), 1)
        self.assertLessEqual(a.convention_panel.winfo_y()+a.convention_panel.winfo_height(), a.winfo_height())
        a.term_form['aliases'].set('可乐派，科洛派')
        a.term_save()
        self.assertEqual(a.conventions.snapshot().apply('科洛派和可乐派'), 'Colo Play和Colo Play')

    def test_settings_toggle_and_hint_wait_while_busy_or_compact(self):
        a = self.app
        for i in range(3):
            a.correction_memory.observe('可乐派', 'Colo Play', str(i))
        for attr in ['busy', 'auto_running', 'history_open']:
            setattr(a, attr, True)
            a.refresh_convention_hint(force=True)
            self.assertFalse(a.learning_hint.winfo_ismapped())
            setattr(a, attr, False)
        a.window_controller.compact = True
        a.refresh_convention_hint(force=True)
        self.assertFalse(a.learning_hint.winfo_ismapped())
        a.window_controller.compact = False
        a.open_settings('conventions')
        a.learning_switch.invoke()
        self.assertFalse(a.correction_memory.enabled)
        a.update()
        tab = a.settings_tabs['conventions']
        self.assertLessEqual(tab.winfo_reqheight(), tab.winfo_height()+4)


if __name__ == '__main__':
    unittest.main()
