"""设置窗口独立性、稳定尺寸与 Mac 关闭／退出语义。"""
import gc
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_talkman_app import FakeClient, FakeVoice
from test_talkman_license_support import licensed_app


class WindowLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='TalkMan-window34-')
        self.app = licensed_app(self.temp.name, session_factory=FakeVoice,
                                client_factory=FakeClient, desktop_services=False)
        self.app.geometry('900x640+60+60')
        self.app.update()
        # CI 的 Mac 工作区较小；先放进工作区，再检查重开不会无故改变尺寸。
        self.app.window_controller.fit_to_screen(force=True)
        self.app.update()
        self.app.cancel_auto_timer()

    def tearDown(self):
        self.app.busy = False
        self.app.ai_review = None
        self.app.close_app()
        gc.collect()
        self.temp.cleanup()

    def test_settings_open_switch_and_close_leave_editor_geometry_and_selection_intact(self):
        a = self.app
        a.set_text('主窗口保持原来的形状和正在编辑的文字。')
        a.text.tag_add('sel', '1.3', '1.7')
        a.update()
        editor = a.geometry()
        mapped = []
        a.bind_all('<Map>', lambda e: mapped.append((e.widget, e.widget.winfo_width(), e.widget.winfo_height())), add=True)
        a.open_settings('about')
        a.update()
        win = a.settings_window
        initial = win.geometry()
        maps = [(w,h) for obj,w,h in mapped if obj is win]
        self.assertTrue(maps)
        self.assertEqual(maps[0], (win.winfo_width(), win.winfo_height()))
        for page in ('window', 'asr', 'model', 'controls', 'about'):
            a.settings_notebook.select(a.settings_tabs[page])
            win.update()
            self.assertEqual(win.geometry(), initial)
            self.assertEqual(a.geometry(), editor)
        self.assertEqual(a.text.get('sel.first', 'sel.last'), '保持原来')
        a._close_settings(win)
        a.update()
        self.assertEqual(a.geometry(), editor)
        self.assertFalse(a.closing)

    def test_reopening_different_settings_page_remembers_user_size(self):
        a = self.app
        a.open_settings('asr')
        win = a.settings_window
        win.geometry('780x560')
        win.update()
        size = win.winfo_width(), win.winfo_height()
        a._close_settings(win)
        a.open_settings('about')
        win = a.settings_window
        win.update()
        self.assertEqual((win.winfo_width(), win.winfo_height()), size)
        self.assertLess(a.settings_viewport.canvas.yview()[1], 1)

    def test_mac_close_saves_hides_then_reopens_same_editor_without_exit(self):
        a = self.app
        a.set_text('关闭窗口后继续保存的正文。')
        a.text.tag_add('sel', '1.2', '1.5')
        a.update()
        timeline = a.timeline
        geometry = a.geometry()
        with patch('talkman_app.sys', SimpleNamespace(platform='darwin')):
            a.close_window()
        a.window_controller.tick(reschedule=False)
        self.assertFalse(a.closing)
        self.assertFalse(a.window_controller.closed)
        self.assertFalse(a.system_style.closed)
        self.assertEqual(a.state(), 'withdrawn')
        self.assertEqual((Path(self.temp.name)/'current-text.txt').read_text(encoding='utf-8'), a.current_text())
        marker = []
        a.after_idle(lambda: marker.append(True))
        a.update()
        self.assertEqual(marker, [True])
        with patch.object(a.window_controller.platform, 'activate'):
            a.reopen_window()
        a.update()
        self.assertEqual(a.state(), 'normal')
        self.assertEqual(a.geometry(), geometry)
        self.assertIs(a.timeline, timeline)
        self.assertEqual(a.text.get('sel.first', 'sel.last'), '窗口后')
        a.close_app()
        self.assertTrue(a.closing)

    def test_mac_close_does_not_hide_on_failed_save_or_pending_work(self):
        a = self.app
        with patch('talkman_app.sys', SimpleNamespace(platform='darwin')):
            with patch.object(a, 'persist_current_text', return_value=False):
                a.close_window()
            self.assertEqual(a.state(), 'normal')
            for attribute, value in (('busy', True), ('ai_review', {}), ('target_filling', True)):
                old = getattr(a, attribute)
                setattr(a, attribute, value)
                with patch.object(a, 'persist_current_text') as save:
                    a.close_window()
                    save.assert_not_called()
                setattr(a, attribute, old)
                self.assertEqual(a.state(), 'normal')
        self.assertFalse(a.closing)

    def test_windows_close_still_exits_and_update_uses_explicit_quit(self):
        a = self.app
        with patch('talkman_app.sys', SimpleNamespace(platform='win32')), patch.object(a, 'close_app') as quit_app:
            a.close_window()
        quit_app.assert_called_once_with()

    @unittest.skipUnless(sys.platform == 'darwin', '需要 Mac 原生窗口')
    def test_native_mac_window_tabbing_and_dock_reopen_and_quit(self):
        import AppKit
        a = self.app
        # 模拟系统偏好倾向标签页的环境；设置打开入口必须重新禁止自动合并。
        AppKit.NSWindow.setAllowsAutomaticWindowTabbing_(True)
        editor = a.geometry()
        a.open_settings('about')
        a.update()
        self.assertFalse(AppKit.NSWindow.allowsAutomaticWindowTabbing())
        windows = {str(w.title()): w for w in AppKit.NSApplication.sharedApplication().windows()}
        main, settings = windows[a.title()], windows[a.settings_window.title()]
        self.assertNotEqual(main.windowNumber(), settings.windowNumber())
        for window in (main, settings):
            self.assertEqual(window.tabbingMode(), AppKit.NSWindowTabbingModeDisallowed)
            self.assertLessEqual(len(window.tabbedWindows() or []), 1)
        self.assertEqual(a.geometry(), editor)
        a._close_settings(a.settings_window)
        a.tk.call(a.protocol('WM_DELETE_WINDOW'))
        a.update()
        self.assertFalse(a.closing)
        self.assertEqual(a.state(), 'withdrawn')
        a.tk.call('::tk::mac::ReopenApplication')
        a.update()
        self.assertEqual(a.state(), 'normal')
        self.assertEqual(a.geometry(), editor)
        a.tk.call('::tk::mac::Quit')
        self.assertTrue(a.closing)


if __name__ == '__main__':
    unittest.main()
