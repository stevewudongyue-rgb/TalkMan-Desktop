"""多屏红框、录音稳定布局、优先词及瑞士外观的离线验收。"""
import ctypes as c
from ctypes import wintypes as w
import json
import sys
import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import test_talkman_window as window_tests
from talkman_conventions import Conventions
from talkman_command_intent import validate_command
from talkman_commands import parse_command
from talkman_position_picker import physical_overlay_coordinates
from talkman_window import WindowPlatform
from talkman_qwen import QwenSettings


class PriorityWordsTests(unittest.TestCase):
    def test_optional_priority_survives_reload_and_does_not_blindly_replace(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Conventions(folder)
            identifier = store.put('Colo Play', priority=True)
            store.put('普通保存词')
            snapshot = Conventions(folder).snapshot()
            source = '今天讨论科洛普雷的设计'
            self.assertEqual(snapshot.apply(source), source)
            prompt = snapshot.instruction('整理语序', source)
            self.assertIn('Colo Play', prompt)
            self.assertNotIn('普通保存词', prompt)
            self.assertIn('无法判断则保留原词', prompt)
            store.put('Colo Play', ['可乐派'], identifier=identifier)
            self.assertTrue(store.items[0]['priority'])
            with self.assertRaises(ValueError):
                store.put('另一个词', ['COLO PLAY'], priority=True)
            store.put('Colo Play', identifier=identifier, priority=False)
            self.assertEqual(store.snapshot().instruction('整理', source), '整理')

    def test_scope_limit_and_old_file_compatibility(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Conventions(folder)
            store.put('代码专名', scope='code', priority=True)
            self.assertEqual(store.snapshot().instruction('整理', '口述'), '整理')
            store.set_language('python')
            self.assertIn('代码专名', store.snapshot().instruction('整理', '口述'))
            for index in range(39):
                store.put(f'专名{index}', priority=True)
            with self.assertRaises(ValueError):
                store.put('超过上限', priority=True)
            old = json.loads(store.path.read_text(encoding='utf-8'))
            for item in old['items']:
                item.pop('priority')
            store.path.write_text(json.dumps(old, ensure_ascii=False), encoding='utf-8')
            self.assertFalse(Conventions(folder).error)
            self.assertEqual(Conventions(folder).snapshot().instruction('整理', '没有匹配'), '整理')


class Desktop36Tests(unittest.TestCase):
    setUp = window_tests.WindowTests.setUp
    tearDown = window_tests.WindowTests.tearDown

    def test_voice_start_stop_does_not_move_buttons_at_three_widths(self):
        a = self.app
        a.settings = QwenSettings(api_key='sk-offline-test')
        for width, height in ((760, 520), (900, 780), (1180, 780)):
            a.geometry(f'{width}x{height}+40+40')
            a.set_text('保留正文')
            a.status_var.set('就绪')
            a.update()
            def geometry():
                return (a.winfo_width(), a.winfo_height(), a.replace_button.winfo_rooty(),
                        a.rewrite_button.winfo_rooty(), a.target_button.winfo_rooty())
            before = geometry()
            with patch('talkman_app.prepare_microphone_permission', return_value=True):
                self.assertTrue(a.start_voice('insert'))
            a.update()
            self.assertEqual(geometry(), before, f'录音开始 {width}')
            a.status_var.set('正在识别，请稍等。'*7)
            a.update()
            self.assertEqual(geometry(), before, f'录音反馈 {width}')
            a.cancel_operation()
            a.update()
            self.assertEqual(geometry(), before, f'录音结束 {width}')

    def test_priority_is_sent_to_actual_text_client_in_open_source_version(self):
        a = self.app
        a.settings = QwenSettings(api_key='sk-offline-test')
        a.conventions.put('Colo Play', priority=True)
        a.set_text('讨论科洛普雷')
        client = Mock()
        client.transform.return_value = '讨论 Colo Play'
        with patch.object(a, 'make_text_client', return_value=client), patch.object(a, 'run_worker', side_effect=lambda _, __, fn: fn()):
            a.organize_once()
        self.assertIn('Colo Play', client.transform.call_args.args[1])
        self.assertEqual(client.transform.call_args.args[0], '讨论科洛普雷')
        with patch.object(a, 'has_feature', return_value=False):
            self.assertIn('Colo Play', a.ai_instruction(once=True))
        a.auto_running = False

    def test_priority_command_requires_confirmation(self):
        a = self.app
        command = parse_command('记住优先词 Colo Play')
        self.assertEqual(command, validate_command('priority_word', 'Colo Play'))
        a.app_commands.execute(command)
        self.assertFalse(a.conventions.items)
        a.app_commands.confirm()
        self.assertTrue(a.conventions.items[0]['priority'])
        self.assertEqual(a.conventions.items[0]['aliases'], [])

    def test_priority_panel_keeps_save_visible_in_small_window(self):
        a = self.app
        a.geometry('760x520+40+40')
        a.update()
        a.remember_selection('Colo Play')
        a.update()
        button = a.term_save_button
        self.assertLessEqual(button.winfo_rooty()+button.winfo_height(), a.winfo_rooty()+a.winfo_height())
        self.assertTrue(a.term_form['priority'].get())
        self.assertEqual(a.term_form['aliases'].get(), '')
        button.invoke()
        self.assertTrue(a.conventions.items[0]['priority'])

    def test_three_themes_preserve_editor_selection_and_persist(self):
        a = self.app
        a.set_text('保留选区')
        a.text.tag_add('sel', '1.0', '1.2')
        before = a.get_selection()
        for key in ('swiss', 'dark', 'light', 'swiss'):
            a.set_theme(key)
            a.update()
            self.assertEqual(a.theme_key, key)
            self.assertEqual(a.get_selection(), before)
            self.assertEqual(json.loads(a.ui_path.read_text(encoding='utf-8'))['theme'], key)
            self.assertEqual(bool(a.wordmark.find_withtag('wordmark')), key == 'swiss')
            self.assertEqual(a.replace_button.type(a.replace_button.find_withtag('shape')[0]),
                             'rectangle' if key == 'swiss' else 'oval')
        a.open_settings('window')
        a.update()
        self.assertEqual(a.current_text(), '保留选区')
        self.assertEqual(parse_command('切换到瑞士国际风格'), validate_command('theme', 'swiss'))

    def test_settings_pages_fit_width_without_horizontal_scroll(self):
        a=self.app
        a.open_settings('text')
        win=a.settings_window
        for width in (650, 960):
            win.geometry(f'{width}x600')
            for name,page in a.settings_tabs.items():
                a.settings_notebook.select(page);a.update()
                view=a.settings_viewport
                self.assertEqual(view.canvas.xview(),(0.0,1.0),name)
                self.assertFalse(view.horizontal.winfo_ismapped(),name)
                def check(widget):
                    for child in widget.winfo_children():
                        if child.winfo_ismapped() and child.winfo_class() in ('TEntry','TCombobox','TButton','TCheckbutton','TRadiobutton'):
                            self.assertLessEqual(child.winfo_rootx()+child.winfo_width(),view.canvas.winfo_rootx()+view.canvas.winfo_width()+2,(name,child))
                        check(child)
                check(page)

    @unittest.skipUnless(sys.platform == 'win32', '需要 Win32 窗口坐标')
    def test_native_overlay_matches_physical_rect_on_all_monitors(self):
        a = self.app
        user = c.WinDLL('user32', use_last_error=True)
        user.GetThreadDpiAwarenessContext.restype = c.c_void_p
        user.GetAncestor.argtypes = [w.HWND, w.UINT]
        user.GetAncestor.restype = w.HWND
        user.GetWindowRect.argtypes = [w.HWND, c.POINTER(w.RECT)]
        original = user.GetThreadDpiAwarenessContext()
        with physical_overlay_coordinates():
            screens = WindowPlatform(a).screens()
        for rect in [(-450, -120, 320, 120), *[(s.x+50,s.y+80,300,100) for s in screens]]:
            a.position_picker.show_marks([dict(index=0, rect=rect)])
            a.update()
            x, y, width, height = rect
            label_y = y-32 if y >= 32 else y
            expected = [(x,y,width,height), (x,y,width,5), (x,y+height-5,width,5),
                        (x,y,5,height), (x+width-5,y,5,height), (x,label_y,112,32)]
            with physical_overlay_coordinates():
                for mark, box in zip(a.position_picker.marks, expected):
                    r = w.RECT()
                    hwnd = user.GetAncestor(mark.winfo_id(), 2)
                    self.assertTrue(user.GetWindowRect(hwnd, c.byref(r)))
                    self.assertEqual((r.left,r.top,r.right-r.left,r.bottom-r.top), box)
            self.assertEqual(user.GetThreadDpiAwarenessContext(), original)
        a.position_picker.clear_marks()


if __name__ == '__main__':
    unittest.main()
