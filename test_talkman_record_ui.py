"""草稿簿、投递历史与 AI 分行的真 Tk 回归；模型和目标均为假数据。"""
from test_talkman_license_support import licensed_app
import gc
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from talkman_app import TalkManApp
from talkman_target import FillReceipt
from talkman_qwen import QwenSettings
from test_talkman_app import FakeVoice, FakeClient


class RecordUITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="TalkMan-records-test-")
        self.folder = Path(self.temp.name)
        (self.folder / "history.json").write_text(json.dumps([
            dict(id="old", time="2026-10-22 10:00:00", text="旧草稿")]), encoding="utf-8")
        self.app = licensed_app(self.folder, session_factory=FakeVoice, client_factory=FakeClient)
        self.app.geometry("900x640")
        self.app.update()
        self.app.after_cancel(self.app.poll_job)
        self.app.poll_job = None

    def tearDown(self):
        self.app.close_app()
        self.app = None
        gc.collect()
        self.temp.cleanup()

    def pump(self):
        self.app.poll_events()
        self.app.after_cancel(self.app.poll_job)
        self.app.poll_job = None

    def test_notebook_migration_delivery_preview_restore_and_delete_are_independent(self):
        a = self.app
        self.assertEqual(a.saved_history[0]["text"], "旧草稿")
        self.assertEqual(a.deliveries.items, [])
        a.set_text("现在草稿")
        a.finish_target_fill("ok", FillReceipt("已填入", "实际填入", "豆包", "需求评审", "2026-10-24T12:00:00+08:00"))
        self.assertEqual(a.current_text(), "现在草稿")
        a.open_history("delivery")
        identifier = a.deliveries.items[0]["id"]
        a.history_list.selection_set(identifier)
        a.preview_history()
        self.assertEqual(a.preview_text, "实际填入")
        self.assertIn("需求评审", a.history_status.get())
        self.assertIn("已填入", a.history_status.get())
        a.confirm_history()
        self.assertEqual(a.current_text(), "实际填入")
        self.assertIn("现在草稿", [x["text"] for x in a.saved_history])
        a.open_history("delivery")
        a.history_list.selection_set(identifier)
        a.delete_history()
        self.assertEqual(len(a.deliveries.items), 1)
        a.confirm_delete_history()
        self.assertEqual(a.deliveries.items, [])
        a.open_history("notebook")
        self.assertIn("old", a.history_list.get_children())

    def test_failed_delivery_copy_and_draft_save_do_not_create_delivery_records(self):
        a = self.app
        a.set_text("待发文字")
        a.save_current()
        with patch.object(a, "clipboard_clear"), patch.object(a, "clipboard_append"):
            a.copy_all()
        a.finish_target_fill("error", "未定位输入框")
        self.assertEqual(a.deliveries.items, [])
        a.deliveries.add = Mock(side_effect=OSError("只读"))
        a.finish_target_fill("ok", FillReceipt("已填入", "待发文字", "豆包", "", "现在"))
        self.assertIn("文字已填入，但历史记录保存失败", a.status_var.get())
        self.assertEqual(a.deliveries.items, [])

    def test_draft_additions_update_but_rewrites_deletions_and_moves_fork(self):
        a = self.app
        a.set_text('苹果。梨。😀')
        self.assertTrue(a.save_current())
        original_id = a.draft_id
        original_count = len(a.saved_history)
        for text in ('苹果。梨。😀新句', '苹果。中间补充。梨。😀新句'):
            a.set_text(text)
            self.assertTrue(a.save_current())
            self.assertEqual(a.draft_id, original_id)
            self.assertEqual(len(a.saved_history), original_count)
        previous = a.current_text()
        for text in ('苹果。中间改写。梨。😀新句', '苹果。梨。😀新句', '梨。苹果。😀新句'):
            prior_id = a.draft_id
            a.set_text(text)
            self.assertTrue(a.save_current())
            self.assertNotEqual(a.draft_id, prior_id)
            self.assertEqual(a.saved_history[0]['parent_id'], prior_id)
            self.assertEqual(next(x['text'] for x in a.saved_history if x['id'] == prior_id), previous)
            previous = text
        count = len(a.saved_history)
        self.assertTrue(a.save_current())
        self.assertEqual(len(a.saved_history), count)

    def test_restored_draft_uses_its_lineage_and_save_failure_keeps_records(self):
        a = self.app
        a.set_text('当前对话')
        a.save_current()
        a.open_history('notebook')
        a.history_list.selection_set('old')
        a.preview_history()
        a.confirm_history()
        self.assertEqual(a.draft_id, 'old')
        a.set_text('旧草稿继续补充')
        a.save_current()
        self.assertEqual(a.draft_id, 'old')
        self.assertEqual(a.saved_history[0]['text'], '旧草稿继续补充')
        records = [dict(item) for item in a.saved_history]
        a.set_text('完全改写')
        with patch.object(Path, 'replace', side_effect=OSError('只读目录')):
            self.assertFalse(a.save_current())
            self.assertEqual(a.draft_id, 'old')
            self.assertEqual(a.saved_history, records)
            self.assertEqual(a.save_button.cget('text'), '')
            self.assertEqual(a.save_button.feedback_icon, 'save')
            self.assertIn('保存失败', a.status_var.get())
            a.close_app()
            self.assertFalse(a.closing)
            self.assertEqual(a.current_text(), '完全改写')

    def test_feedback_survives_sync_refresh_and_returns_to_latest_label(self):
        a = self.app
        a.button_feedback(a.target_button, '已填入')
        a.sync_mode_var.set(True)
        a.live_sync.refresh()
        self.assertEqual(a.target_button.cget('text'), '已填入')
        # 模拟 Tk 反馈定时器到时，无须等待真实时间。
        job, label = a.feedback_jobs[a.target_button]
        self.assertEqual(label, '下发')
        script = a.tk.call('after', 'info', job)[0]
        a.tk.call('after', 'cancel', job)
        a.tk.eval(script)
        self.assertEqual(a.target_button.cget('text'), '下发')
        a.live_sync.changed()
        scheduled = a.live_sync.job
        a.live_sync.flush()
        self.assertNotIn(scheduled, a.tk.call('after', 'info'))

    def test_delivery_export_contains_time_and_destination(self):
        a = self.app
        a.finish_target_fill("ok", FillReceipt("已填入", "正文😀\n", "豆包", "需求", "2026-10-24T12:00:00+08:00"))
        a.open_history("delivery")
        a.history_list.selection_set(a.deliveries.items[0]["id"])
        path = self.folder / "导出.txt"
        with patch("talkman_app.filedialog.asksaveasfilename", return_value=str(path)):
            a.export_history()
        self.assertIn("豆包 · 需求", path.read_text(encoding="utf-8"))
        self.assertTrue(path.read_text(encoding="utf-8").endswith("正文😀\n"))

    def test_pending_fill_uses_request_snapshot_and_prevents_duplicate_or_early_exit(self):
        a = self.app
        a.set_text("请求时正文")
        pending = []
        a.target_controller.fill_with_receipt = lambda text, callback: pending.append((text, callback))
        a.fill_to_target()
        a.fill_to_target()
        a.close_app()
        self.assertFalse(a.closing)
        self.assertEqual(len(pending), 1)
        a.set_text("已经继续编辑")
        value, callback = pending[0]
        callback("ok", FillReceipt("已填入", value, "豆包", "对话", "时间"))
        self.pump()
        self.assertFalse(a.target_filling)
        self.assertEqual(a.current_text(), "已经继续编辑")
        self.assertEqual(a.deliveries.items[0]["text"], "请求时正文")

    def test_layout_respects_selection_and_discards_late_results(self):
        a = self.app
        a.settings = QwenSettings(api_key="fake-offline")
        a.layout_var.set(True)
        original = "一、苹果。二、开会。"
        a.set_text(original)
        a.text.tag_add("sel", "1.0", "1.2")
        worker = Mock()
        a.run_worker = worker
        a.apply_auto()
        worker.assert_called_once()
        self.assertEqual(a.get_selection()[2], "一、")
        kind, token, _work = worker.call_args.args
        a.set_text("用户的新稿")
        a.events.put((kind, token, "ok", "一、苹果。\n二、开会。"))
        self.pump()
        self.assertEqual(a.current_text(), "用户的新稿")

    def test_minimum_window_buttons_visible_and_theme_switch_safe(self):
        a = self.app
        for _ in range(2):
            for widget in (a.one_filler_button, a.target_button):
                self.assertTrue(widget.winfo_viewable())
                self.assertLessEqual(widget.winfo_rootx() + widget.winfo_width(), a.winfo_rootx() + a.winfo_width())
            a.open_history("delivery")
            a.update()
            for widget in (a.history_restore_button, a.history_delete_button):
                self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(), a.history_panel.winfo_rooty()+a.history_panel.winfo_height())
            a.close_history()
            a.toggle_theme()
            a.update()

    def test_both_drawers_show_five_complete_records_without_shrinking_text(self):
        a = self.app
        a.write_history([dict(id=str(n), time="2026-10-24 10:00:00", text=f"第{n}条草稿")
                         for n in range(12)])
        for n in range(12):
            a.deliveries.add(f"第{n}次填入的完整正文", "豆包", "Talk Man 产品需求评审与交互设计讨论")
        a.set_text("当前草稿与选区应保持不变")
        a.text.tag_add("sel", "1.0", "1.4")
        original_selection = a.get_selection()
        for theme in range(2):
            for dimensions in ("900x640", "1180x780", "1500x900"):
                a.geometry(dimensions)
                a.update()
                editor_size = (a.text.winfo_width(), a.text.winfo_height())
                for mode in ("notebook", "delivery"):
                    with self.subTest(theme=theme, dimensions=dimensions, mode=mode):
                        a.open_history(mode)
                        identifiers = a.history_list.get_children()
                        a.history_list.selection_set(identifiers[0])
                        a.preview_history()
                        a.update()
                        visible = [bounds for identifier in identifiers
                                   if (bounds := a.history_list.bbox(identifier))
                                   and bounds[1] >= 0
                                   and bounds[1] + bounds[3] <= a.history_list.winfo_height()]
                        self.assertGreaterEqual(len(visible), 5)
                        self.assertGreaterEqual(visible[0][3], 58)
                        self.assertGreater(a.preview_widget.winfo_width(), 250)
                        self.assertGreaterEqual(a.preview_widget.winfo_height(), 5 * visible[0][3])
                        self.assertEqual((a.text.winfo_width(), a.text.winfo_height()), editor_size)
                        a.close_history()
                        a.update()
                        self.assertEqual(a.current_text(), "当前草稿与选区应保持不变")
                        self.assertEqual(a.get_selection(), original_selection)
            a.toggle_theme()
            a.update()

    def test_layout_only_uses_full_text_is_undoable_and_rejects_model_rewriting(self):
        a = self.app
        a.filler_var.set(False)
        a.punct_var.set(False)
        a.rewrite_var.set(False)
        a.settings = QwenSettings(api_key="fake-offline")
        original = "一、买苹果。二、买梨。三、开会。"
        formatted = "一、买苹果。\n二、买梨。\n三、开会。"
        client = Mock()
        client.transform.return_value = formatted
        a.client_factory = lambda settings: client
        a.layout_var.set(True)
        a.set_text(original)
        a.apply_auto()
        for _ in range(100):
            self.pump()
            if not a.auto_running:
                break
            time.sleep(0.01)
        self.assertEqual(a.current_text(), formatted)
        self.assertIn("普通叙述", client.transform.call_args.args[1])
        a.undo()
        self.assertEqual(a.current_text(), original)
        client.transform.return_value = "模型偷偷改写了正文"
        a.apply_auto()
        for _ in range(100):
            self.pump()
            if not a.auto_running:
                break
            time.sleep(0.01)
        self.assertEqual(a.current_text(), original)
        self.assertIn("改变了正文", a.status_var.get())

    def test_record_previews_use_available_width_and_resizing_preserves_content_and_selection(self):
        a = self.app
        original = ('今天需要确认桌面版本的交互流程，保留 Colo Play 品牌名称。\n' * 60) + '完整结尾😀'
        a.write_history([dict(id=str(i), time='2026-11-02 09:30:00', text=original)
                         for i in range(12)])
        a.set_text('仍在编辑的正文')
        a.open_history('notebook')
        a.update()
        self.assertEqual(a.history_list.selection(), ('0',))
        a.history_list.selection_set('6')
        a.preview_history()
        a.update()
        a.history_list.yview_moveto(.25)
        a.preview_widget.yview_moveto(.4)
        small = a.history_list.item('6', 'text').splitlines()
        self.assertEqual(len(small), 3)
        self.assertGreater(len(''.join(small[:2])), 12)
        self.assertTrue(small[1].endswith('…'))
        a.geometry('1500x900')
        a.update()
        large = a.history_list.item('6', 'text').splitlines()
        self.assertGreater(len(''.join(large[:2])), len(''.join(small[:2])))
        self.assertGreaterEqual(a.history_list.winfo_width(), a.preview_widget.winfo_width()*.9)
        self.assertLessEqual(a.history_list.winfo_width(), a.preview_widget.winfo_width()*1.1)
        self.assertEqual(a.history_list.selection(), ('6',))
        self.assertGreater(a.preview_widget.yview()[0], 0)
        self.assertEqual(a.preview_widget.get('1.0','end-1c'), original)
        self.assertEqual(a.current_text(), '仍在编辑的正文')

    def test_empty_record_tabs_keep_actions_disabled_and_delivery_summary_names_target(self):
        a = self.app
        a.open_history('delivery')
        a.update()
        self.assertFalse(a.history_list.get_children())
        self.assertIsNone(a.preview_text)
        self.assertTrue(a.history_restore_button.instate(['disabled']))
        item = a.deliveries.add('第一行正文\n第二行正文', '豆包', '项目需求')
        a.close_history()
        a.open_history('delivery')
        a.update()
        self.assertEqual(a.history_list.selection(), (item['id'],))
        self.assertIn('豆包', a.history_list.item(item['id'],'text').splitlines()[-1])
        self.assertEqual(a.preview_text, item['text'])


if __name__ == "__main__":
    unittest.main()
