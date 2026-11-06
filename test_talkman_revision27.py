"""置顶悬停、选区翻译、工具栏与小屏工作区离线回归。"""
import ctypes as C
from ctypes import wintypes as W
import sys
import time
import unittest
from unittest.mock import Mock, patch
from talkman_window import Rect, fit_window_rect, window_screen
import test_talkman_revision26 as revision26


class Revision27Tests(unittest.TestCase):
    setUp = revision26.Revision26Tests.setUp
    tearDown = revision26.Revision26Tests.tearDown

    def test_no_selection_does_not_translate_whole_document(self):
        a = self.app
        a.set_text('这是正文。')
        a.translate_document()
        a.run_worker.assert_not_called()
        self.assertIn('先选中', a.status_var.get())
        self.assertEqual(a.current_text(), '这是正文。')

    def test_only_selected_text_is_sent_and_provenance_outside_selection_survives(self):
        a = self.app
        base = '前文 Keep 保留。\n请检查 Colo Play 和 API。\n后文 Tail 保留。'
        start, end = base.index('请'), base.index('\n后')
        a.set_text(base)
        a.text.tag_add('sel', f'1.0+{start}c', f'1.0+{end}c')
        a.preview_ai_var.set(True)
        a.translate_document()
        _, token, _ = a.run_worker.call_args.args
        plan = token[2]
        self.assertEqual(plan.fragment.base, base[start:end])
        tokens = list(plan.fragment.tokens)
        fragment = plan.fragment.restore('Please check '+tokens[0]+' and '+tokens[1]+'.')
        result = plan.combine(fragment)
        a.finish_translation(token, 'ok', result)
        self.assertEqual(a.current_text(), base)
        a.ai_review_finish(True)
        self.assertEqual(a.current_text(), base[:start]+fragment.text+base[end:])
        self.assertEqual(a.get_selection()[2], fragment.text)
        spans = a.translation_memory.protection(a.current_text())
        self.assertEqual([a.current_text()[x:y] for x,y in spans], ['Keep', 'Colo Play', 'API', 'Tail'])
        a.undo()
        self.assertEqual(a.current_text(), base)

    def test_translating_pure_english_selection_to_chinese_is_allowed(self):
        a = self.app
        a.set_text('Before. Open the window. After.')
        a.text.tag_add('sel', '1.8', '1.24')
        a.translation_language.set('简体中文')
        a.translate_document()
        plan = a.run_worker.call_args.args[1][2]
        self.assertEqual(plan.fragment.masked, 'Open the window.')
        self.assertEqual(plan.combine(plan.fragment.restore('打开窗户。')).text, 'Before. 打开窗户。 After.')

    def test_toolbar_icons_do_not_grow_during_feedback_and_language_lives_in_settings(self):
        a = self.app
        a.update()
        for button in (a.save_button, a.copy_button, a.clear_button, a.backspace_button, a.remember_button):
            self.assertEqual(button.cget('text'), '')
            self.assertEqual(button.cget('style'), 'Tool.TButton')
            self.assertTrue(button.cget('image'))
        width = a.save_button.winfo_width()
        a.button_feedback(a.save_button, '已保存')
        a.update()
        self.assertEqual(a.save_button.cget('text'), '')
        self.assertEqual(a.save_button.winfo_width(), width)
        self.assertEqual(a.save_button.feedback_icon, 'check')
        self.assertEqual(a.translate_button.cget('text'), '翻译')
        self.assertLess(a.translate_button.winfo_rootx()-(a.one_filler_button.winfo_rootx()+a.one_filler_button.winfo_width()), 20)
        self.assertFalse(hasattr(a, 'translation_choice'))
        a.open_settings('text')
        a.translation_language.set('日语')
        a.translation_choice.event_generate('<<ComboboxSelected>>')
        self.assertEqual(a.ui_preferences['translation_language'], '日语')

    def test_hover_timer_above_pinned_main_window_preserves_selection_and_focus(self):
        a = self.app
        a.set_text('保留选区')
        a.text.tag_add('sel', '1.0', '1.2')
        a.text.focus_force()
        a.attributes('-topmost', True)
        a.update()
        selected = a.get_selection()
        for button in (a.copy_button, a.replace_button):
            button.event_generate('<Enter>')
            until = time.monotonic()+.8
            while time.monotonic() < until:
                a.update()
                time.sleep(.01)
            tip = a.hover_help.window
            self.assertIsNotNone(tip)
            self.assertTrue(tip.winfo_viewable())
            self.assertEqual(tip.winfo_toplevel(), a)
            at = a.winfo_containing(tip.winfo_rootx()+10, tip.winfo_rooty()+10)
            self.assertTrue(str(at).startswith(str(tip)))
            if sys.platform == 'win32':
                user = C.WinDLL('user32')
                user.WindowFromPoint.argtypes = [W.POINT]
                user.WindowFromPoint.restype = W.HWND
                user.GetAncestor.argtypes = [W.HWND, W.UINT]
                user.GetAncestor.restype = W.HWND
                visible = user.WindowFromPoint(W.POINT(tip.winfo_rootx()+10, tip.winfo_rooty()+10))
                self.assertEqual(user.GetAncestor(visible, 2), user.GetAncestor(tip.winfo_id(), 2))
            self.assertEqual(a.focus_get(), a.text)
            self.assertEqual(a.get_selection(), selected)
            button.event_generate('<Leave>')
            self.assertIsNone(a.hover_help.window)

    def test_small_main_window_keeps_editor_and_actions_visible(self):
        a = self.app
        a.minsize(1, 1)
        for theme in ('dark', 'light'):
            if a.theme_key != theme: a.toggle_theme()
            a.geometry('720x480')
            a.update()
            for widget in (a.text, a.translate_button, a.one_filler_button, a.save_button,
                           a.copy_button, a.target_button, a.replace_button, a.rewrite_button,
                           a.records_button, a.settings_button, a.pin_button):
                self.assertTrue(widget.winfo_ismapped())
                self.assertGreaterEqual(widget.winfo_rootx(), a.winfo_rootx())
                self.assertGreaterEqual(widget.winfo_rooty(), a.winfo_rooty())
                self.assertLessEqual(widget.winfo_rootx()+widget.winfo_width(), a.winfo_rootx()+a.winfo_width())
                self.assertLessEqual(widget.winfo_rooty()+widget.winfo_height(), a.winfo_rooty()+a.winfo_height())
            self.assertGreater(a.text.winfo_height(), 100)

    def test_settings_small_screen_clamps_window_and_scrolls_to_language(self):
        a = self.app
        class SmallDisplay:
            def __init__(self, window): self.window = window
            def bounds(self):
                w = self.window
                return Rect(w.winfo_x(), w.winfo_y(), w.winfo_width()+16, w.winfo_height()+40)
            def screens(self): return [Rect(0, 0, 1024, 600)]
            def move_to(self, x, y): self.window.geometry(f'+{x}+{y}')
        with patch('talkman_settings_ui.WindowPlatform', SmallDisplay):
            a.open_settings('text')
            a.update()
            win = a.settings_window
            self.assertLessEqual(win.winfo_height()+40, 600)
            canvas = a.settings_viewport.canvas
            self.assertLess(canvas.yview()[1]-canvas.yview()[0], 1)
            a.translation_choice.focus_force()
            a.update()
            self.assertGreaterEqual(a.translation_choice.winfo_rooty(), canvas.winfo_rooty())
            self.assertLessEqual(a.translation_choice.winfo_rooty()+a.translation_choice.winfo_height(), canvas.winfo_rooty()+canvas.winfo_height())

    def test_monitor_change_waits_for_drag_release_and_preserves_editor_state(self):
        a = self.app
        a.geometry('1400x900')
        a.set_text('跨屏时保留文字与选区')
        a.text.tag_add('sel', '1.0', '1.3')
        a.update()
        selected = a.get_selection()
        initial_width = a.winfo_width()
        position = [-1240, 40]
        platform = Mock()
        platform.bounds.side_effect = lambda: Rect(*position, a.winfo_width()+16, a.winfo_height()+40)
        platform.screens.return_value = [Rect(-1280, 24, 1280, 680)]
        platform.move_to.side_effect = lambda x,y: position.__setitem__(slice(None), [x,y])
        platform.maximized.return_value = False
        platform.pointer_down.return_value = True
        c = a.window_controller
        c.platform = platform
        c._layout_tick()
        self.assertEqual(a.winfo_width(), initial_width)
        platform.pointer_down.return_value = False
        c._layout_tick()
        fitted = platform.bounds()
        self.assertEqual(fitted.overlap(platform.screens()[0]), fitted.width*fitted.height)
        a.update()
        self.assertEqual(a.current_text(), '跨屏时保留文字与选区')
        self.assertEqual(a.get_selection(), selected)
        platform.screens.return_value = [Rect(-1280, 24, 3840, 2100)]
        c.screens = platform.screens()
        c.fit_to_screen(force=True)
        self.assertEqual((platform.bounds().width, platform.bounds().height), (fitted.width, fitted.height))


class DisplayFitTests(unittest.TestCase):
    def test_cross_monitor_negative_coordinates_and_return_to_large_screen(self):
        small = Rect(-1280, 24, 1280, 680)
        large = Rect(0, 0, 3840, 2100)
        oversized = Rect(-1250, 40, 1800, 1200)
        self.assertEqual(window_screen(oversized, [large, small]), small)
        fitted = fit_window_rect(oversized, small)
        self.assertEqual(fitted.overlap(small), fitted.width*fitted.height)
        self.assertLess(fitted.height, small.height)
        returned = fit_window_rect(Rect(10, 30, fitted.width, fitted.height), large)
        self.assertEqual((returned.width, returned.height), (fitted.width, fitted.height))

    def test_unplugged_monitor_moves_window_fully_into_remaining_workarea(self):
        screen = Rect(0, 28, 1024, 700)
        result = fit_window_rect(Rect(2300, 100, 1700, 1100), screen)
        self.assertEqual(result.overlap(screen), result.width*result.height)


if __name__ == '__main__': unittest.main()
