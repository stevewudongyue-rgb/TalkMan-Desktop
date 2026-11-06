"""真实 Tk 导航条集成检查；目标使用隔离替身，不读取用户窗口。"""
import gc
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from test_talkman_license_support import licensed_app
from test_talkman_target_picker import FakeTargets
from test_talkman_app import FakeClient, FakeVoice
from talkman_target import TargetWindow


class PositionUItests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='TalkMan-position-ui-')
        self.service = FakeTargets(self.temp.name)
        self.service.begin_positions = Mock()
        self.service.position_action = Mock()
        self.service.position_label = '输入位置'
        with patch('talkman_app.TargetController', return_value=self.service):
            self.app = licensed_app(self.temp.name, desktop_services=False,
                session_factory=FakeVoice, client_factory=FakeClient)
        self.app.after_cancel(self.app.poll_job)
        self.app.poll_job = None
        self.app.geometry('900x640+60+60')
        self.app.update()
        self.app.set_text('保留原文和已选择的内容。')
        self.app.text.tag_add('sel', '1.2', '1.5')
        self.app.text.mark_set('insert', '1.5')
        self.app.cancel_auto_timer()
        self.picker = self.app.position_picker

    def tearDown(self):
        self.app.busy = False
        self.app.voice = None
        self.app.close_app()
        gc.collect()
        self.temp.cleanup()

    def pump(self):
        self.app.poll_events()
        self.app.after_cancel(self.app.poll_job)
        self.app.poll_job = None
        self.app.update()

    def reply(self, index=0, callback=None):
        cb = callback or self.service.begin_positions.call_args.args[0]
        rects = [(500, 400, 200, 60), (760, 400, 200, 60)]
        cb('ok', dict(index=index, count=2, rect=rects[index], title='测试窗口',
                      choices=[dict(index=i, rect=rect) for i, rect in enumerate(rects)]))
        self.pump()

    def snapshot(self):
        a = self.app
        return a.current_text(), a.get_selection(), a.text.index('insert'), a.timeline.items[:], a.geometry()

    def test_navigation_cancel_preserves_editor_and_size_no_fill(self):
        before = self.snapshot()
        self.app.position_selector.invoke()
        self.reply()
        self.assertTrue(self.app.window_controller.protected())
        self.assertTrue(self.app.target_button.instate(['disabled']))
        self.assertEqual(self.app.state(), 'normal')
        self.picker.action('next')
        callback = self.service.position_action.call_args.args[1]
        self.reply(1, callback)
        self.assertEqual(self.picker.candidate['index'], 1)
        self.app.position_selector.invoke()
        self.app.update()
        self.assertEqual(before, self.snapshot())
        self.assertEqual(self.picker.marks, [])
        self.assertFalse(self.picker.active)
        self.service.fill_with_receipt.assert_not_called()

    def test_late_inspect_does_not_undo_clicked_selection(self):
        p = self.picker
        p.open(); self.reply()
        if p.inspect_job:
            self.app.after_cancel(p.inspect_job)
        p.inspect()
        old = self.service.position_action.call_args.args[1]
        p.action('next')
        next_cb = self.service.position_action.call_args.args[1]
        old('inspect', dict(index=0, count=2, rect=(300, 300, 200, 60), title='旧位置'))
        self.pump()
        self.assertTrue(p.pending)
        p.action('choose', 1)
        self.assertEqual(self.service.position_action.call_args.args[0], 'next')
        self.reply(1, next_cb)
        self.assertEqual(p.candidate['index'], 1)
        p.action('choose', 1)
        self.assertEqual(self.service.position_action.call_args.args[:3:2], ('choose', 1))
        callback = self.service.position_action.call_args.args[1]
        callback('confirmed', dict(target=TargetWindow(1, 2, '测试', '会话', 'test'), index=1, count=2))
        self.pump()
        self.assertFalse(p.active)
        old('inspect', dict(index=0, count=2, rect=(300, 300, 200, 60), title='旧位置'))
        self.pump()
        self.assertFalse(p.active)
        self.assertEqual(p.marks, [])

    def test_inspect_reuses_marks_and_negative_coordinates_are_absolute(self):
        p = self.picker
        p.open(); self.reply()
        marks = p.marks[:]
        choices = p.candidate['choices']
        p.show_marks(choices)
        self.assertEqual(marks, p.marks)
        p.show_marks([dict(index=1, rect=(-250, -100, 200, 60))])
        self.app.update()
        # Mac 会把顶级窗口约束到可见桌面，原生多屏由实机验收。
        if sys.platform == 'win32':
            from talkman_position_picker import physical_overlay_coordinates
            from talkman_window import WindowPlatform
            with physical_overlay_coordinates():
                rect = WindowPlatform(p.marks[0]).bounds()
            self.assertEqual((rect.x, rect.y), (-250, -100))

    def test_click_red_label_confirms_and_mac_close_cleans_overlays(self):
        p = self.picker
        p.open(); self.reply()
        label = p.marks[-1].winfo_children()[0]
        label.event_generate('<Button-1>')
        self.app.update()
        self.assertEqual(self.service.position_action.call_args.args[0], 'choose')
        self.assertEqual(self.service.position_action.call_args.args[2], 1)
        callback = self.service.position_action.call_args.args[1]
        callback('confirmed', dict(target=TargetWindow(1, 2, '测试', '会话', 'test'), index=1, count=2))
        self.pump()
        self.assertFalse(p.active)
        self.assertEqual(p.marks, [])
        p.open(); self.reply()
        with patch('talkman_app.sys', SimpleNamespace(platform='darwin')):
            self.app.close_window()
        self.assertFalse(p.active)
        self.assertEqual(p.marks, [])
        self.assertEqual(self.app.state(), 'withdrawn')

    def test_voice_navigation_does_not_call_model_or_edit_text(self):
        a, p = self.app, self.picker
        p.open(); self.reply()
        before = a.current_text()
        a.voice_mode = 'rewrite'
        a.voice_base = before
        a.voice_start, a.voice_end = 2, 5
        a.voice = SimpleNamespace(stop_reason=None)
        with patch.object(a, 'make_text_client') as client:
            a.finish_voice('下一个输入框')
        client.assert_not_called()
        self.assertEqual(a.current_text(), before)
        self.assertEqual(self.service.position_action.call_args.args[0], 'next')

    def test_failed_scan_cancels_backend_transaction(self):
        self.picker.open()
        callback = self.service.begin_positions.call_args.args[0]
        callback('error', '目标输入框已消失')
        self.pump()
        self.assertFalse(self.picker.active)
        self.assertEqual(self.service.position_action.call_args.args[0], 'cancel')
        self.assertIn('已消失', self.app.status_var.get())


if __name__ == '__main__':
    unittest.main()
