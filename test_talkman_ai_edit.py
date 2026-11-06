"""离线预览事务回归：不录音、不联网、不访问真实目标。"""
from test_talkman_license_support import licensed_app
import gc
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace
from talkman_app import TalkManApp
from talkman_ai_edit import mapped_offset, review_changes
from talkman_qwen import QwenSettings, TextEdit, TextMove
from talkman_window import Rect
from test_talkman_app import FakeVoice, FakeClient


class AIReviewTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix='TalkMan-review-')
        self.app = licensed_app(Path(self.folder.name), session_factory=FakeVoice,
                              file_session_factory=FakeVoice, client_factory=FakeClient, desktop_services=False)
        self.app.settings = QwenSettings(api_key='sk-offline-test')
        self.app.after_cancel(self.app.poll_job)
        self.app.poll_job = None
        self.app.geometry('900x640+40+40')
        self.app.update()
        self.app.text.focus_force()
        self.app.preview_ai_var.set(True)
        self.app.run_worker = Mock()
        self.permission = patch('talkman_app.prepare_microphone_permission', return_value=True)
        self.permission.start()

    def tearDown(self):
        self.app.close_app()
        self.app = None
        self.permission.stop()
        FakeVoice.sessions.clear()
        gc.collect()
        self.folder.cleanup()

    def pump(self):
        self.app.poll_events()
        self.app.after_cancel(self.app.poll_job)
        self.app.poll_job = None
        self.app.update()

    def result(self, kind='ai_once', value='请把梨拿过来。'):
        a = self.app
        a.set_text('请把苹果拿过来。')
        a.text.tag_add('sel', '1.2', '1.4')
        a.organize_once() if kind == 'ai_once' else a.apply_auto()
        self.before_review_timeline = (a.timeline.items[:], a.timeline.index)
        event, token, _ = a.run_worker.call_args.args
        self.assertEqual(event, kind)
        a.events.put((event, token, 'ok', value))
        self.pump()
        return a

    def test_changing_options_releases_once_button_and_rejects_stale_result(self):
        a = self.app
        a.set_text('请把苹果拿过来。')
        a.organize_once()
        kind, token, _ = a.run_worker.call_args.args
        self.assertTrue(a.one_filler_button.instate(['disabled']))
        a.rewrite_var.set(False)
        a.auto_changed()
        self.assertFalse(a.one_filler_button.instate(['disabled']))
        self.assertEqual(a.one_filler_button.cget('text'), 'AI 整理一次')
        a.events.put((kind, token, 'ok', '迟到的结果'))
        self.pump()
        self.assertEqual(a.current_text(), '请把苹果拿过来。')
        self.assertIsNone(a.ai_review)
        a.organize_once()
        self.assertTrue(a.auto_running)

    def test_once_and_auto_require_confirmation_and_support_undo(self):
        for kind in ('ai_once', 'auto'):
            with self.subTest(kind=kind):
                a = self.result(kind)
                self.assertIsNotNone(a.ai_review)
                self.assertEqual(a.current_text(), '请把苹果拿过来。')
                self.assertEqual(a.get_selection()[2], '苹果')
                self.assertEqual(a.ai_review_text.get('1.0', 'end-1c'), '请把梨拿过来。')
                self.assertEqual(a.ai_review_text.cget('fg'), a.palette['text'])
                self.assertEqual(a.ai_review_text.get(*a.ai_review_text.tag_ranges('review_added')), '梨')
                self.assertEqual(a.text.get(*a.text.tag_ranges('review_removed')), '苹果')
                self.assertEqual(a.ai_review_summary.get(), '替换 1 处。')
                self.assertEqual((a.timeline.items, a.timeline.index), self.before_review_timeline)
                a.ai_review_finish(True)
                self.assertEqual(a.current_text(), '请把梨拿过来。')
                self.assertEqual(a.get_selection()[2], '梨')
                self.assertIsNone(a.auto_job)
                a.undo()
                self.assertEqual(a.current_text(), '请把苹果拿过来。')
                a.redo()
                self.assertEqual(a.current_text(), '请把梨拿过来。')

    def test_cancel_escape_and_close_keep_selection_and_history(self):
        for action in ('cancel', 'escape', 'close'):
            with self.subTest(action=action):
                a = self.result()
                before = (a.timeline.items[:], a.timeline.index, a.revision)
                if action == 'escape':
                    a.ai_review_text.event_generate('<Escape>')
                    a.update()
                elif action == 'close':
                    a.ai_review_cancel.invoke()
                else:
                    a.ai_review_finish()
                self.assertIsNone(a.ai_review)
                self.assertEqual(a.current_text(), '请把苹果拿过来。')
                self.assertEqual(a.get_selection()[2], '苹果')
                self.assertEqual((a.timeline.items, a.timeline.index, a.revision), before)
                self.assertEqual(a.auto_block_revision, a.revision)

    def test_voice_instruction_and_empty_deletion_require_confirmation(self):
        a = self.app
        for replacement in ('梨', ''):
            with self.subTest(replacement=replacement):
                a.set_text('请拿苹果😀过来')
                a.text.tag_add('sel', '1.2', '1.4')
                self.assertTrue(a.start_voice('rewrite'))
                FakeVoice.sessions[-1].emit('done', '修改所选文字')
                self.pump()
                kind, token, _ = a.run_worker.call_args.args
                a.events.put((kind, token, 'ok', replacement))
                self.pump()
                self.assertFalse(a.busy)
                self.assertEqual(a.current_text(), '请拿苹果😀过来')
                self.assertEqual(a.get_selection()[2], '苹果')
                a.ai_review_finish(True)
                self.assertEqual(a.current_text(), '请拿'+replacement+'😀过来')
                a.undo()
                self.assertEqual(a.current_text(), '请拿苹果😀过来')

    def test_caret_instruction_cancel_preserves_cursor_and_blocks_voice(self):
        a = self.app
        a.set_text('😀这里，后面')
        a.text.mark_set('insert', '1.0 + 3 chars')
        a.text.focus_force()
        a.update()
        self.assertTrue(a.start_voice('rewrite'))
        a.review_voice_result(TextEdit(3, 3, '新增'))
        self.assertEqual(a.current_text(), '😀这里，后面')
        self.assertEqual(len(a.text.get('1.0', 'insert')), 3)
        self.assertFalse(a.start_voice('insert'))
        a.ai_review_finish()
        self.assertEqual(a.current_text(), '😀这里，后面')
        self.assertEqual(len(a.text.get('1.0', 'insert')), 3)

    def test_switch_persists_without_key_and_off_applies_directly(self):
        a = self.app
        self.assertFalse(a.ui_preferences.get('preview_ai', False))
        a.settings = QwenSettings()
        a.preview_preference_changed()
        self.assertTrue(json.loads(a.ui_path.read_text(encoding='utf-8'))['preview_ai'])
        a.settings = QwenSettings(api_key='sk-offline-test')
        a.preview_ai_var.set(False)
        a.preview_preference_changed()
        self.result()
        self.assertIsNone(a.ai_review)
        self.assertEqual(a.current_text(), '请把梨拿过来。')
        self.assertFalse(json.loads(a.ui_path.read_text(encoding='utf-8'))['preview_ai'])

    def test_stale_confirmation_and_late_result_never_overwrite(self):
        a = self.result()
        a.set_text('已经是新稿')
        a.ai_review_finish(True)
        self.assertIsNotNone(a.ai_review)
        self.assertEqual(a.current_text(), '已经是新稿')
        a.ai_review_finish()
        a.organize_once()
        kind, token, _ = a.run_worker.call_args.args
        a.set_text('更新的新稿')
        a.events.put((kind, token, 'ok', '迟到结果'))
        self.pump()
        self.assertIsNone(a.ai_review)
        self.assertEqual(a.current_text(), '更新的新稿')

    def test_unconfirmed_text_never_syncs_or_fills_and_double_confirm_is_safe(self):
        a = self.result()
        a.target_controller = Mock()
        a.sync_mode_var.set(True)
        a.live_sync.flush()
        a.live_sync.flush(send=True)
        a.fill_to_target()
        a.target_controller.sync.assert_not_called()
        a.target_controller.fill_with_receipt.assert_not_called()
        self.assertFalse(a.start_voice('insert'))
        self.assertTrue(a.start_voice('rewrite'))
        a.finish_voice('改成别的')
        self.assertIsNotNone(a.ai_review)
        self.assertEqual(a.text.cget('state'), 'disabled')
        a.target_controller.sync.assert_not_called()
        finish = a.ai_review_finish
        finish(True)
        items = a.timeline.items[:]
        finish(True)
        self.assertEqual(a.timeline.items, items)
        a.live_sync.flush()
        self.assertEqual(a.target_controller.sync.call_args.args[0], '请把梨拿过来。')

    def test_review_is_inside_same_window_with_original_left_and_result_right(self):
        a = self.app
        for size in ('900x640', '1180x780', '1500x900'):
            a.geometry(size)
            a.update()
            # 先完成工作区适配，再比较对照面板打开／关闭后的编辑宽度。
            a.window_controller.fit_to_screen(force=True)
            a.update()
            original_width = a.text.winfo_width()
            self.result()
            a.update()
            self.assertEqual(a.ai_review.winfo_toplevel(), a)
            self.assertEqual(a.ai_review_text.winfo_toplevel(), a)
            self.assertLess(a.text.winfo_rootx()+a.text.winfo_width(), a.ai_review_text.winfo_rootx())
            self.assertLessEqual(a.ai_review.winfo_rootx()+a.ai_review.winfo_width(), a.winfo_rootx()+a.winfo_width())
            self.assertGreater(a.ai_review_text.winfo_height(), 100)
            self.assertGreater(a.ai_review_text.winfo_width(), 250)
            a.toggle_theme()
            a.update()
            a.ai_review_finish()
            a.update()
            self.assertEqual(a.text.winfo_width(), original_width)

    def test_move_is_previewed_then_committed_as_one_undo_step(self):
        a = self.app
        original = '第一段。\n要移动😀。\n最后一段。'
        a.set_text(original)
        start, end = original.index('要'), original.index('最后')
        a.text.tag_add('sel', f'1.0 + {start} chars', f'1.0 + {end} chars')
        self.assertTrue(a.start_voice('rewrite'))
        a.review_voice_result(TextMove(start, end, 0))
        self.assertEqual(a.current_text(), original)
        a.ai_review_finish(True)
        self.assertEqual(a.current_text(), '要移动😀。\n第一段。\n最后一段。')
        self.assertEqual(a.get_selection()[2], '要移动😀。\n')
        a.undo()
        self.assertEqual(a.current_text(), original)

    def test_settings_preview_switch_is_visible_and_saved_without_key_validation(self):
        a = self.app
        a.settings = QwenSettings()
        a.open_settings('text')
        win = a.settings_window
        win.update()
        width, height = win.minsize()
        win.geometry(f'{width}x{height}')
        win.update()
        for widget in (a.preview_ai_switch, a.filler_switch, a.punct_switch, a.layout_switch, a.rewrite_switch):
            a.settings_viewport.see_focus(SimpleNamespace(widget=widget))
            win.update_idletasks()
            self.assertTrue(widget.winfo_ismapped())
            self.assertGreaterEqual(widget.winfo_rooty(), win.winfo_rooty())
            self.assertLessEqual(widget.winfo_rootx()+widget.winfo_width(), win.winfo_rootx()+win.winfo_width())
            self.assertLessEqual(widget.winfo_rooty()+widget.winfo_height(), win.winfo_rooty()+win.winfo_height())
        a.preview_ai_direct.invoke()
        self.assertFalse(a.preview_ai_var.get())
        self.assertFalse(json.loads((a.data_dir/'interface.json').read_text(encoding='utf-8'))['preview_ai'])
        self.assertEqual(a.settings.api_key, '')

    def test_only_differences_are_marked_with_emoji_and_theme_switch(self):
        a = self.app
        before, after = '😀请把苹果拿来。', '😀请把梨拿来。\n谢谢！'
        a.set_text(before)
        a.review_result(before, after, 'AI 整理', Mock())
        a.update()
        def marked(widget, tag):
            ranges = widget.tag_ranges(tag)
            return [widget.get(ranges[i], ranges[i+1]) for i in range(0, len(ranges), 2)]
        self.assertEqual(marked(a.text, 'review_removed'), ['苹果'])
        self.assertEqual(marked(a.ai_review_text, 'review_added'), ['梨', '\n谢谢！'])
        self.assertEqual(a.ai_review_summary.get(), '替换 1 处、新增 1 处。')
        old_size = a.text.cget('font')
        a.toggle_theme()
        a.update()
        self.assertEqual(a.ai_review_text.cget('fg'), a.palette['text'])
        self.assertEqual(a.text.cget('font'), old_size)
        a.ai_review_finish()
        self.assertNotIn('review_removed', a.text.tag_names())
        self.assertEqual(a.current_text(), before)

    def test_deletion_is_marked_in_original_only(self):
        a = self.app
        a.set_text('要删除的文字')
        a.review_result(a.current_text(), '', 'AI 整理', Mock())
        self.assertEqual(a.text.get(*a.text.tag_ranges('review_removed')), '要删除的文字')
        self.assertFalse(a.ai_review_text.tag_ranges('review_added'))
        self.assertEqual(a.ai_review_summary.get(), '删除 1 处。')

    def test_chinese_emoji_and_unchanged_boundaries_map_correctly(self):
        for before, after, offset, expected in (('苹果', '苹果。', 2, 2),
                ('😀苹果', '😀梨', 1, 1), ('😀苹果', '😀梨', 3, 2),
                ('苹果', '', 2, 0), ('苹果', '新苹果', 0, 1)):
            self.assertEqual(mapped_offset(before, after, offset), expected)

    def test_unchanged_and_error_result_keep_original_without_preview(self):
        a = self.app
        a.set_text('原文')
        for state, result in (('ok', '原文'), ('error', '离线模拟错误')):
            a.organize_once()
            kind, token, _ = a.run_worker.call_args.args
            a.events.put((kind, token, state, result))
            self.pump()
            self.assertIsNone(a.ai_review)
            self.assertEqual(a.current_text(), '原文')


class ReviewSummaryTests(unittest.TestCase):
    def test_summary_distinguishes_word_changes_from_punctuation_and_layout(self):
        for before, after, expected in (
            ('原文', '原文', '文字没有变化。'),
            ('一、苹果 二、梨', '一、苹果\n二、梨', '仅调整换行或空格，用词未变。'),
            ('你好 今天下雨', '你好，今天下雨。', '仅调整标点或排版，用词未变。'),
            ('不要删除', '', '删除 1 处。'),
            ('苹果', '梨', '替换 1 处。'),
            ('C++', 'C', '删除 1 处。'),
        ):
            with self.subTest(before=before, after=after):
                changes, summary = review_changes(before, after)
                self.assertEqual(summary, expected)
                self.assertEqual(bool(changes), before != after)


if __name__ == '__main__':
    unittest.main()
