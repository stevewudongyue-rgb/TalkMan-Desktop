"""COLOPLAY 回迁回归：实际字号、偏好保留与原生设置窗口层级。"""
import gc
import json
from pathlib import Path
import sys
import tempfile
import tkinter as tk
import unittest
from unittest.mock import patch

from test_talkman_app import FakeClient, FakeVoice
from test_talkman_license_support import licensed_app


class Revision32Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='TalkMan-display-test-')
        self.folder = Path(self.temp.name)
        self.app = self.make_app()

    def make_app(self):
        app = licensed_app(self.folder, session_factory=FakeVoice, client_factory=FakeClient,
                           file_session_factory=FakeVoice, desktop_services=False)
        app.withdraw()
        app.cancel_auto_timer()
        return app

    def tearDown(self):
        self.app.close_app()
        gc.collect()
        self.temp.cleanup()

    def test_larger_text_survives_restart_without_losing_editor_preferences(self):
        app = self.app
        baseline_line = app.text._fit_font.metrics('linespace')
        baseline_dpi = float(app.tk.call('tk', 'scaling'))
        app.save_ui_preferences(translation_language='日语', only_agents=False, auto_delay=13)
        app.set_text('这段正文在更换字号后仍然保留。')
        app.open_settings('window')
        app.window_scale_choice.set('最大 · 150%')
        app.window_drafts[1].set(True)
        with patch.object(app, 'save_ui_preferences', wraps=app.save_ui_preferences) as save:
            app.window_settings_save()
        save.assert_called_once()
        values = json.loads(app.ui_path.read_text(encoding='utf-8'))
        self.assertEqual(values['ui_scale'], 1.5)
        self.assertEqual(values['translation_language'], '日语')
        self.assertEqual(values['auto_delay'], 13)
        self.assertFalse(values['only_agents'])
        self.assertTrue(values['topmost'])
        # 未重启时明确提示；再次打开设置也保留刚刚保存的选择。
        self.assertIn('重新打开', app.status_var.get())
        app.open_settings('window')
        self.assertEqual(app.window_scale_choice.get(), '最大 · 150%')
        app._close_settings(app.settings_window)
        app.close_app()
        self.app = self.make_app()
        self.assertEqual(self.app.current_text(), '这段正文在更换字号后仍然保留。')
        self.assertGreater(self.app.text._fit_font.metrics('linespace'), baseline_line)
        self.assertAlmostEqual(float(self.app.tk.call('tk', 'scaling')), baseline_dpi, delta=.05)
        self.assertTrue(self.app.has_feature('translation'))

    def test_large_editor_keeps_text_and_selection_when_long_content_fits(self):
        self.app.close_app()
        (self.folder / 'interface.json').write_text('{"ui_scale":1.5}', encoding='utf-8')
        self.app = self.make_app()
        app = self.app
        app.geometry('960x720')
        app.deiconify()
        text = '编辑时保留中文、English 和标点。\n' * 60
        app.set_text(text)
        app.text.tag_add('sel', '2.0', '2.5')
        app.text.mark_set('insert', '2.5')
        app.update()
        self.assertEqual(app.current_text(), text)
        self.assertEqual(app.text.get('sel.first', 'sel.last'), '编辑时保留')
        self.assertEqual(app.text.index('insert'), '2.5')
        self.assertGreaterEqual(abs(app.text._fit_font.cget('size')), 21)
        first, last = app.text.yview()
        self.assertLess(last - first, 1)

    def test_invalid_scale_falls_back_to_readable_default(self):
        for value in (True, '1.5', 0, 100):
            with self.subTest(value=value):
                self.app.close_app()
                (self.folder / 'interface.json').write_text(json.dumps({'ui_scale': value}), encoding='utf-8')
                self.app = self.make_app()
                self.assertEqual(self.app.ui_scale, 1.0)
                self.assertEqual(self.app.font_size(16), 16)

    def test_large_text_navigation_reaches_every_settings_page(self):
        self.app.close_app()
        (self.folder / 'interface.json').write_text('{"ui_scale":1.5}', encoding='utf-8')
        self.app = self.make_app()
        app = self.app
        app.deiconify()
        app.open_settings('window')
        win = app.settings_window
        win.geometry('820x700')
        win.update()
        navigation = app.settings_viewport.navigation
        self.assertIsNotNone(navigation)
        self.assertTrue(navigation.winfo_viewable())
        self.assertLessEqual(navigation.winfo_rootx() + navigation.winfo_width(),
                             win.winfo_rootx() + win.winfo_width())
        for index, (name, page) in enumerate(app.settings_tabs.items()):
            with self.subTest(page=name):
                navigation.current(index)
                navigation.event_generate('<<ComboboxSelected>>')
                win.update()
                self.assertEqual(app.settings_notebook.select(), str(page))
        app.open_settings('window')
        win.update()
        self.assertEqual(navigation.get(), '窗口与显示')

    def test_pinned_editor_leaves_settings_accessible_and_restores_state(self):
        app = self.app
        app.deiconify()
        app.topmost_var.set(True)
        app.window_controller.apply_preferences()
        app.update()
        pages = ('window', 'controls', 'about') + (('mac',) if sys.platform == 'darwin' else ())
        for page in pages:
            with self.subTest(page=page):
                app.open_settings(page)
                app.update()
                win = app.settings_window
                self.assertTrue(win.winfo_viewable())
                self.assertTrue(app.tk.getboolean(win.attributes('-topmost')))
                if sys.platform == 'darwin':
                    self.assertFalse(app.tk.getboolean(app.attributes('-topmost')))
                app._close_settings(win)
                app.update()
                self.assertTrue(app.tk.getboolean(app.attributes('-topmost')))
        app.topmost_var.set(False)
        app.window_controller.apply_preferences()
        app.open_settings('window')
        app.update()
        self.assertFalse(app.tk.getboolean(app.settings_window.attributes('-topmost')))

    def test_focus_recalculates_scroll_range_when_switching_from_short_to_long_page(self):
        app = self.app
        app.deiconify()
        app.open_settings('window')
        win = app.settings_window
        win.geometry('700x560')
        win.update()
        view = app.settings_viewport
        app.settings_notebook.select(app.settings_tabs['about'])
        win.update()
        view.canvas.yview_moveto(1)
        app.settings_notebook.select(app.settings_tabs['window'])
        # 焦点先于布局回调到来时，仍使用目标页的实际尺寸定位。
        from types import SimpleNamespace
        view.see_focus(SimpleNamespace(widget=app.window_scale_combo))
        win.update_idletasks()
        top = app.window_scale_combo.winfo_rooty()
        self.assertGreaterEqual(top, view.canvas.winfo_rooty() - 2)
        self.assertLessEqual(top + app.window_scale_combo.winfo_height(),
                             view.canvas.winfo_rooty() + view.canvas.winfo_height() + 2)


if __name__ == '__main__':
    unittest.main()
