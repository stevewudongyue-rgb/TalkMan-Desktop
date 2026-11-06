"""记录页签、约定、延迟整理与下发后清空：隔离数据、假录音、假目标。"""
from test_talkman_license_support import licensed_app
import gc
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from talkman_app import TalkManApp
from talkman_qwen import QwenSettings
from talkman_target import FillReceipt
from test_talkman_app import FakeVoice, FakeClient


class Revision24Tests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix='TalkMan-revision24-')
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
        self.app.target_filling = False
        self.app.close_app()
        self.app = None
        self.permission.stop()
        FakeVoice.sessions.clear()
        gc.collect()
        self.folder.cleanup()

    def receipt(self, value, status='已填入'):
        return FillReceipt('测试填入成功', value, '测试 Agent', '测试对话', '2026-10-31 12:00:00', status,
                           send_token='verified' if status == '已填入' else '')

    def prepare_delivery(self, value='准备转移的正文'):
        a = self.app
        a.clear_after_fill_var.set(True)
        a.set_text(value)
        a._delivery_snapshot = (a.revision, value)
        return self.receipt(value)

    def test_delivery_clears_only_after_history_saved_and_is_undoable(self):
        a = self.app
        receipt = self.prepare_delivery()
        a.finish_target_fill('ok', receipt)
        self.assertEqual(a.current_text(), '')
        self.assertEqual(a.deliveries.items[0]['text'], receipt.text)
        self.assertEqual(a.deliveries.items[0]['title'], '测试对话')
        a.undo()
        self.assertEqual(a.current_text(), receipt.text)
        a.redo()
        self.assertEqual(a.current_text(), '')

    def test_failed_delivery_or_failed_history_never_clears(self):
        a = self.app
        receipt = self.prepare_delivery()
        a.finish_target_fill('error', '无法定位')
        self.assertEqual(a.current_text(), receipt.text)
        receipt = self.prepare_delivery()
        with patch.object(a.deliveries, 'add', side_effect=OSError('只读')):
            a.finish_target_fill('ok', receipt)
        self.assertEqual(a.current_text(), receipt.text)
        self.assertEqual(a.deliveries.items, [])

    def test_new_edits_even_if_restored_to_same_text_are_not_cleared(self):
        a = self.app
        for new in ['新增文字', '准备转移的正文']:
            receipt = self.prepare_delivery()
            a.set_text('临时改变')
            a.set_text(new)
            a.finish_target_fill('ok', receipt)
            self.assertEqual(a.current_text(), new)

    def test_clear_is_opt_in_and_unmatched_receipt_does_not_clear(self):
        a = self.app
        self.assertFalse(a.clear_after_fill_var.get())
        a.set_text('原文')
        a._delivery_snapshot = (a.revision, '原文')
        a.finish_target_fill('ok', self.receipt('原文'))
        self.assertEqual(a.current_text(), '原文')
        receipt = self.prepare_delivery('原文')
        a.finish_target_fill('ok', self.receipt('另一份内容'))
        self.assertEqual(a.current_text(), '原文')

    def test_fill_invalidates_inflight_ai_result(self):
        a = self.app
        a.set_text('原文')
        a.apply_auto()
        _, token, _ = a.run_worker.call_args.args
        a.target_controller = Mock()
        a.fill_to_target()
        a._finish_ai(token, 'ok', '晚到的整理')
        self.assertEqual(a.current_text(), '原文')
        self.assertIsNone(a.ai_review)
        self.assertFalse(a.auto_running)

    def test_sync_send_clears_without_syncing_empty_back_to_target(self):
        a = self.app
        a.target_controller = Mock()
        a.sync_mode_var.set(True)
        a.clear_after_fill_var.set(True)
        a.set_text('同步正文')
        a.schedule_auto()
        a.live_sync.flush(send=True)
        self.assertTrue(a.target_filling)
        self.assertIsNone(a.auto_job)
        a.live_sync.finish((a.live_sync.epoch, '同步正文', True), 'ok', self.receipt('同步正文', '已触发下发'))
        self.assertEqual(a.current_text(), '')
        self.assertTrue(a.live_sync.paused)
        a.live_sync.flush()
        self.assertEqual(a.target_controller.sync.call_count, 1)

    def test_delay_resets_on_typing_and_is_not_used_by_manual_organize(self):
        a = self.app
        self.assertEqual(a.auto_delay_var.get(), 5)
        a.set_text('输入')
        with patch.object(a, 'after', wraps=a.after) as after:
            a.schedule_auto()
            first = a.auto_job
            self.assertEqual(after.call_args.args[0], 5000)
            a.text.insert('end', '继续')
            a.on_text_modified()
            second = a.auto_job
            self.assertNotEqual(first, second)
            self.assertNotIn(first, a.tk.call('after', 'info'))
            self.assertEqual(after.call_args.args[0], 5000)
        a.organize_once()
        self.assertEqual(a.run_worker.call_args.args[0], 'ai_once')
        self.assertIsNone(a.auto_job)

    def test_drag_and_selection_pause_auto_and_late_result_cannot_overwrite(self):
        a = self.app
        a.set_text('苹果和香蕉')
        a.apply_auto()
        _, token, _ = a.run_worker.call_args.args
        a.editor_clicked(SimpleNamespace(widget=a.text))
        a._finish_ai(token, 'ok', '梨和香蕉')
        self.assertFalse(a.auto_running)
        self.assertEqual(a.current_text(), '苹果和香蕉')
        a.text.tag_add('sel', '1.0', '1.2')
        a.editor_selection_finished()
        a.update()
        self.assertFalse(a._editor_selecting)
        self.assertIsNone(a.auto_job)
        a.text.tag_remove('sel', '1.0', 'end')
        a.update_meta()
        self.assertIsNotNone(a.auto_job)

    def test_remember_selection_keeps_document_and_cancel_preserves_range(self):
        a = self.app
        a.set_text('Colo Play 新品牌')
        a.text.tag_add('sel', '1.0', '1.9')
        a.remember_selection()
        a.update()
        self.assertIsNotNone(a.convention_panel)
        self.assertEqual(a.convention_panel.winfo_toplevel(), a)
        self.assertGreater(a.convention_panel.winfo_height(), 200)
        self.assertLessEqual(a.convention_panel.winfo_rooty()+a.convention_panel.winfo_height(), a.winfo_rooty()+a.winfo_height())
        a.term_form['aliases'].set('可乐派，Coloplay')
        a.term_save()
        self.assertEqual(a.current_text(), 'Colo Play 新品牌')
        self.assertEqual(a.get_selection()[2], 'Colo Play')
        self.assertEqual(a.conventions.snapshot().apply('可乐派'), 'Colo Play')
        a.remember_selection()
        a.cancel_operation()
        self.assertIsNone(a.convention_panel)
        self.assertEqual(a.get_selection()[2], 'Colo Play')

    def test_voice_remember_command_opens_confirmation_without_model_call(self):
        a = self.app
        a.set_text('Colo Play')
        a.text.tag_add('sel', '1.0', 'end-1c')
        self.assertTrue(a.start_voice('rewrite'))
        a.finish_voice('把选中的词记为约定')
        a.run_worker.assert_not_called()
        self.assertIsNotNone(a.convention_panel)
        self.assertEqual(a.conventions.items, [])
        a.term_save()
        self.assertEqual(a.conventions.items[0]['word'], 'Colo Play')

    def test_recording_uses_start_snapshot_and_preserves_undo(self):
        a = self.app
        key = a.conventions.put('Colo Play', ['可乐派'])
        a.set_text('品牌：')
        self.assertTrue(a.start_voice('insert'))
        a.conventions.put('新写法', ['可乐派'], identifier=key)
        a.finish_voice('可乐派')
        self.assertEqual(a.current_text(), '品牌：Colo Play')
        a.undo()
        self.assertEqual(a.current_text(), '品牌：')

    def test_record_tabs_keep_selected_item_preview_and_document(self):
        a = self.app
        a.saved_history = [dict(id=str(i), time='2026-10-31 12:00', text='长文\n' * 90 + str(i)) for i in range(12)]
        a.deliveries.add('已填入内容', '测试 Agent', '测试对话', '2026-10-31 12:00', '已填入')
        a.set_text('编辑中的正文')
        a.records_button.invoke()
        a.update()
        a.history_list.selection_set('5')
        a.preview_history()
        a.update()
        a.preview_widget.yview_moveto(.5)
        scroll = a.preview_widget.yview()[0]
        a.delivery_button.invoke()
        a.update()
        a.history_list.selection_set(a.deliveries.items[0]['id'])
        a.preview_history()
        a.history_button.invoke()
        a.update()
        self.assertEqual(a.history_list.selection(), ('5',))
        self.assertAlmostEqual(a.preview_widget.yview()[0], scroll, places=2)
        self.assertEqual(a.current_text(), '编辑中的正文')
        rowheight = int(a.history_list.tk.call('ttk::style', 'lookup', 'History.Treeview', '-rowheight') or 20)
        self.assertGreaterEqual(a.history_list.winfo_height(), rowheight * 5)
        self.assertEqual(a.history_panel.winfo_toplevel(), a)
        self.assertEqual(a.records_button.cget('text'), '收起记录')

    def test_settings_persist_delay_clear_and_conventions(self):
        a = self.app
        a.open_settings('text')
        a.auto_delay_var.set(10)
        a.auto_delay_choice.event_generate('<<ComboboxSelected>>')
        a.settings_notebook.select(a.settings_tabs['target'])
        a.clear_after_fill_switch.invoke()
        a.settings_notebook.select(a.settings_tabs['conventions'])
        a.convention_settings_new()
        a.convention_settings_form['word'].set('Colo Play')
        a.convention_settings_form['aliases'].set('可乐派')
        a.convention_settings_save()
        a.update()
        saved = json.loads((Path(self.folder.name) / 'interface.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['auto_delay'], 10)
        self.assertTrue(saved['clear_after_fill'])
        self.assertEqual(a.conventions.snapshot().apply('可乐派'), 'Colo Play')
        for name in ['text', 'conventions', 'target']:
            a.settings_notebook.select(a.settings_tabs[name])
            a.update()
            tab = a.settings_tabs[name]
            self.assertLessEqual(tab.winfo_reqheight(), tab.winfo_height() + 4, name)


if __name__ == '__main__':
    unittest.main()
