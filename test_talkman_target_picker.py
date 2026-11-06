"""离线验证主界面目标选择，不访问真实 Agent、不录音或调用模型。"""
from test_talkman_license_support import licensed_app
import gc
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from talkman_app import TalkManApp
from talkman_target import TargetError, TargetSelection, TargetWindow


class FakeTargets:
    def __init__(self, path):
        self.selection = TargetSelection.load(path)
        self.selected = None
        self.label = '未选择目标输入框'
        self.refresh_note = ''
        self.callbacks = []
        self.fill_with_receipt = Mock()

    def refresh(self, callback):
        self.callbacks.append(callback)

    def select(self, target):
        self.selection.set(target)
        self.selection.save()
        self.selected = target

    def clear(self):
        self.selected = None
        self.selection.clear()
        self.selection.save()

    def close(self):
        pass

    def stop_sync(self):
        pass


class TargetPickerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='TalkMan-target-picker-')
        self.service = FakeTargets(Path(self.temp.name))
        with patch('talkman_app.TargetController', return_value=self.service):
            self.app = licensed_app(Path(self.temp.name), desktop_services=False)
        self.app.only_agents_var.set(False)
        self.app.window_controller.hotkeys.close()
        self.app.geometry('1000x700+50+50')
        self.app.after_cancel(self.app.poll_job)
        self.app.poll_job = None
        self.app.update()
        self.picker = self.app.target_picker
        self.windows = [TargetWindow(11, 42, 'ChatGPT', '项目讨论', 'chatgpt'),
                        TargetWindow(12, 43, '豆包', '新对话', 'doubao')]
        self.app.set_text('请把苹果拿过来。')
        self.app.text.mark_set('insert', '1.4')
        self.app.text.tag_add('sel', '1.2', '1.4')

    def tearDown(self):
        self.app.target_filling = False
        self.app.busy = False
        self.app.close_app()
        self.app = None
        gc.collect()
        self.temp.cleanup()

    def pump(self):
        self.app.poll_events()
        self.app.after_cancel(self.app.poll_job)
        self.app.poll_job = None
        self.app.update()

    def open(self, compact=False):
        button = self.app.compact_target_selector if compact else self.app.target_selector
        button.invoke()
        self.app.update()

    def reply(self, state='ok', value=None, callback=None):
        callback = callback or self.service.callbacks.pop(0)
        callback(state, self.windows if value is None else value)
        self.pump()

    def test_switch_preserves_document_selection_caret_and_never_fills(self):
        a = self.app
        before = (a.current_text(), a.get_selection(), a.text.index('insert'), a.timeline.items[:])
        self.open()
        self.reply()
        self.picker.choose('1')
        self.assertEqual(self.service.selected.id, 12)
        self.assertEqual(TargetSelection.load(Path(self.temp.name)).metadata['app'], '豆包')
        self.assertIsNone(self.picker.panel)
        self.assertEqual(before, (a.current_text(), a.get_selection(), a.text.index('insert'), a.timeline.items[:]))
        self.assertIn('豆包', a.target_selector.cget('text'))
        self.assertIn('豆包', a.compact_target_selector.cget('text'))
        self.service.fill_with_receipt.assert_not_called()

    def test_async_reply_after_dismiss_does_not_reopen_or_replace_new_list(self):
        self.open()
        old = self.service.callbacks.pop(0)
        self.picker.dismiss()
        self.open()
        self.reply(callback=old)
        self.assertTrue(self.picker.refreshing)
        self.assertEqual(self.picker.listing.get_children(), ())
        self.reply(value=[self.windows[1]])
        self.assertEqual(len(self.picker.rows), 1)
        self.assertEqual(self.picker.rows['0']['id'], 12)

    def test_empty_error_and_retry_are_inline_and_do_not_erase_target(self):
        self.app.target_windows = {11: self.windows[0]}
        self.app.choose_target(11)
        self.open()
        self.reply(state='error', value='请开启辅助功能')
        self.assertEqual(self.picker.feedback.get(), '请开启辅助功能')
        self.assertEqual(self.service.selected.id, 11)
        self.assertFalse(self.picker.refresh_button.instate(['disabled']))
        self.picker.refresh()
        self.reply(value=[])
        self.assertIn('未发现', self.picker.feedback.get())
        self.picker.refresh()
        self.reply()
        self.assertEqual(len(self.picker.rows), 2)

    def test_escape_only_dismisses_picker_and_returns_to_editor(self):
        self.open()
        self.reply()
        self.app.auto_running = True
        self.picker.listing.focus_force()
        self.app.update()
        self.picker.listing.event_generate('<Escape>')
        self.app.update()
        self.assertIsNone(self.picker.panel)
        self.assertTrue(self.app.auto_running)
        self.assertEqual(self.app.focus_get(), self.app.text)

    def test_keyboard_selection_and_existing_target_highlight(self):
        self.app.target_windows = {12: self.windows[1]}
        self.app.choose_target(12)
        self.open()
        self.reply()
        self.assertEqual(self.picker.listing.selection(), ('1',))
        self.picker.listing.selection_set('0')
        self.picker.listing.focus_force()
        self.app.update()
        self.picker.listing.event_generate('<Return>')
        self.app.update()
        self.assertEqual(self.service.selected.id, 11)
        self.service.fill_with_receipt.assert_not_called()

    def test_stale_target_cannot_select_or_fill(self):
        self.open()
        self.reply()
        self.app.target_windows.clear()
        self.picker.choose('0')
        self.assertIsNotNone(self.picker.panel)
        self.assertIn('失效', self.picker.feedback.get())
        self.service.fill_with_receipt.assert_not_called()

    def test_busy_and_filling_lock_both_selectors_until_complete(self):
        self.open()
        self.app.set_busy(True)
        self.assertIsNone(self.picker.panel)
        self.assertTrue(self.app.target_selector.instate(['disabled']))
        self.assertTrue(self.app.compact_target_selector.instate(['disabled']))
        self.app.set_busy(False)
        self.app.fill_to_target()
        self.assertTrue(self.app.target_selector.instate(['disabled']))
        self.assertTrue(self.app.compact_fill.instate(['disabled']))
        self.app.events.put(('target_fill', None, 'error', '离线测试'))
        self.pump()
        self.assertFalse(self.app.target_selector.instate(['disabled']))
        self.assertFalse(self.app.compact_fill.instate(['disabled']))

    def test_settings_selection_and_clear_sync_both_labels(self):
        self.app.target_windows = {11: self.windows[0]}
        self.app.choose_target(11)
        self.assertIn('ChatGPT', self.app.target_selector.cget('text'))
        self.assertIn('ChatGPT', self.app.compact_target_selector.cget('text'))
        self.app.clear_target()
        for button in (self.app.target_selector, self.app.compact_target_selector):
            self.assertEqual(button.cget('text'), '选择目标 ▾')

    def test_long_labels_and_both_themes_fit_full_and_compact_windows(self):
        a = self.app
        for theme in ('dark', 'light'):
            if a.theme_key != theme:
                a.toggle_theme()
            for compact in (False, True):
                self.picker.close()
                a.minsize(480, 330) if compact else a.minsize(900, 640)
                a.set_compact_layout(compact)
                a.geometry('480x330+50+50' if compact else '900x640+50+50')
                a.target_label_var.set('测试非常长的目标窗口标题' * 30)
                a.update()
                selector = a.compact_target_selector if compact else a.target_selector
                fill = a.compact_fill if compact else a.target_button
                position = a.compact_position_selector if compact else a.position_selector
                self.assertGreaterEqual(selector.winfo_rootx(), a.winfo_rootx())
                self.assertAlmostEqual(position.winfo_rootx() - selector.winfo_rootx() - selector.winfo_width(), 8, delta=1)
                self.assertAlmostEqual(fill.winfo_rootx() - position.winfo_rootx() - position.winfo_width(), 8, delta=1)
                self.assertLessEqual(fill.winfo_rootx() + fill.winfo_width(), a.winfo_rootx() + a.winfo_width())
                # Mac 中文字体与图标按钮高度不同，pack 按垂直中心对齐。
                self.assertAlmostEqual(selector.winfo_rooty() + selector.winfo_height() / 2,
                                       fill.winfo_rooty() + fill.winfo_height() / 2, delta=1)
                self.assertGreaterEqual(a.text.winfo_height(), 75)
                self.open(compact)
                self.reply(value=[TargetWindow(i, i, 'ChatGPT', f'测试对话 {i}', 'chatgpt') for i in range(20)])
                panel = self.picker.panel
                self.assertEqual(panel.winfo_toplevel(), a)
                self.assertGreaterEqual(panel.winfo_x(), 0)
                self.assertGreaterEqual(panel.winfo_y(), 0)
                self.assertLessEqual(panel.winfo_x() + panel.winfo_width(), a.winfo_width())
                self.assertLessEqual(panel.winfo_y() + panel.winfo_height(), selector.winfo_rooty() - a.winfo_rooty())
                self.assertGreaterEqual(self.picker.listing.winfo_height(), 72)
                self.assertTrue(self.picker.scrollbar.winfo_ismapped())


if __name__ == '__main__':
    unittest.main()
