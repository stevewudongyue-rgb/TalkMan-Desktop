"""离线窗口回归：不录音、不使用用户凭证、不向其他应用输入。"""
from test_talkman_license_support import licensed_app
import gc
import json
from pathlib import Path
import tempfile
import sys
import time
import tkinter as tk
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from talkman_app import TalkManApp
from talkman_global_hotkeys import GlobalHotkeys, native_binding, voice_bindings
from talkman_shortcuts import DEFAULT_SHORTCUTS, ShortcutError, validate_shortcuts
from talkman_window import (DesktopWindowController, Rect, WindowPlatform, compact_rect, dock_rect,
                            edge_for, tab_rect, nearest_screen_edge, position_tab)
from talkman_qwen import QwenSettings
from test_talkman_app import FakeClient, FakeVoice


class GeometryTests(unittest.TestCase):
    def test_four_edges_and_no_middle(self):
        screen = Rect(0, 0, 1920, 1040)
        for rect, edge in ((Rect(-8, 150, 900, 640), 'left'),
                           (Rect(1028, 150, 900, 640), 'right'),
                           (Rect(400, 0, 900, 640), 'top'),
                           (Rect(400, 400, 900, 640), 'bottom'),
                           (Rect(400, 150, 900, 640), None)):
            self.assertEqual(edge_for(rect, screen), edge)

    def test_negative_monitor_coordinates_and_tab_clamping(self):
        screen = Rect(-1920, -180, 1920, 1000)
        for edge in ('left', 'right', 'top'):
            tab = tab_rect(Rect(-1920, -160, 1200, 780), screen, edge)
            self.assertGreaterEqual(tab.x, screen.x)
            self.assertGreaterEqual(tab.y, screen.y)
            self.assertLessEqual(tab.right, screen.right)
            self.assertLessEqual(tab.bottom, screen.bottom)
            self.assertGreaterEqual(min(tab.width, tab.height), 36)

    def test_dock_uses_chosen_work_area_without_resizing(self):
        screen = Rect(-1920, 32, 1920, 1000)
        window = Rect(-1500, 140, 900, 640)
        for edge in ('top', 'left', 'right'):
            result = dock_rect(window, screen, edge)
            self.assertEqual((result.width, result.height), (900, 640))
            self.assertEqual(result.overlap(screen), 900*640)
            self.assertEqual({'top': result.y, 'left': result.x, 'right': result.right}[edge],
                             {'top': screen.y, 'left': screen.x, 'right': screen.right}[edge])
        with self.assertRaises(ValueError):
            dock_rect(window, screen, 'middle')

    def test_dragged_tab_clamps_to_all_edges_and_negative_monitors(self):
        screens = [Rect(0, 24, 1920, 1016), Rect(-1600, -200, 1600, 900)]
        for point, edge, monitor in (((8, 500), 'left', 0), ((1900, 500), 'right', 0),
                                      ((800, 28), 'top', 0), ((800, 1030), 'bottom', 0),
                                      ((-1000, -190), 'top', 1), ((-1595, 100), 'left', 1)):
            screen, result = nearest_screen_edge(point, screens)
            self.assertEqual((screen, result), (screens[monitor], edge))
            initial = tab_rect(screen, screen, edge)
            for fraction in (-1, 0.25, 0.8, 2):
                tab = position_tab(initial, screen, edge, fraction)
                self.assertEqual(tab.overlap(screen), tab.width*tab.height)
                self.assertEqual({'top': tab.y, 'bottom': tab.bottom, 'left': tab.x, 'right': tab.right}[edge],
                                 {'top': screen.y, 'bottom': screen.bottom, 'left': screen.x, 'right': screen.right}[edge])
                popup = compact_rect(tab, screen, edge, 480, 330)
                self.assertEqual(popup.overlap(screen), 480*330)

    def test_custom_voice_combinations_are_global_but_single_keys_are_not(self):
        keys = {**DEFAULT_SHORTCUTS, 'replace': 'Alt+1', 'rewrite': 'Alt+2'}
        for platform in ('win32', 'darwin'):
            bindings = voice_bindings(keys, platform)
            self.assertEqual(bindings['replace'], 'Alt+1')
            self.assertEqual(bindings['rewrite'], 'Alt+2')
            self.assertNotIn('replace', voice_bindings(DEFAULT_SHORTCUTS, platform))
            self.assertNotIn('rewrite', voice_bindings(DEFAULT_SHORTCUTS, platform))
            bindings = voice_bindings({**keys, 'global_record': 'Alt+1'}, platform)
            self.assertNotIn('replace', bindings)

    def test_label_size_can_grow_for_display_scaling(self):
        screen = Rect(0, 0, 1920, 1040)
        for edge in ('top', 'left', 'right'):
            label = tab_rect(Rect(200, 100, 900, 640), screen, edge, (280, 240))
            self.assertGreaterEqual(label.width, 280)
            self.assertGreaterEqual(label.height, 240)
            self.assertEqual(label.overlap(screen), label.width*label.height)

    def test_compact_popup_stays_at_label_on_negative_monitor(self):
        screen = Rect(-1920, -180, 1920, 1000)
        for edge in ('top', 'left', 'right'):
            tab = tab_rect(Rect(-1500, -160, 1000, 700), screen, edge)
            popup = compact_rect(tab, screen, edge, 480, 330)
            self.assertEqual(popup.overlap(screen), 480*330)
            self.assertEqual({'top': popup.y, 'left': popup.x, 'right': popup.right}[edge],
                             {'top': screen.y, 'left': screen.x, 'right': screen.right}[edge])

    def test_native_shortcut_codes_and_unsafe_bindings(self):
        self.assertEqual(native_binding('Control+Alt+space', 'win32'), (0x4003, 32))
        self.assertEqual(native_binding('Control+Alt+F', 'win32'), (0x4003, 70))
        self.assertEqual(native_binding('Control+Alt+R', 'darwin'), (6144, 15))
        self.assertEqual(native_binding('Control+Alt+space', 'darwin'), (6144, 49))
        for binding in ('space', 'Control', 'F3'):
            with self.assertRaises(ShortcutError):
                validate_shortcuts({**DEFAULT_SHORTCUTS, 'global_record': binding})

    def test_old_shortcut_settings_migrate_without_losing_custom_binding(self):
        old = {k: v for k, v in DEFAULT_SHORTCUTS.items() if not k.startswith('global_')}
        old['replace'] = 'Control+Alt+space'
        upgraded = validate_shortcuts(old)
        self.assertEqual(upgraded['replace'], old['replace'])
        self.assertEqual(upgraded['global_record'], '')

    def test_stale_native_events_are_discarded(self):
        hotkeys = GlobalHotkeys(enabled=False)
        hotkeys.configure({})
        hotkeys.events.put((0, 'action', 'global_record'))
        hotkeys.events.put((1, 'error', '冲突'))
        self.assertEqual(hotkeys.poll(), [('error', '冲突')])


class WindowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='TalkMan-window-')
        self.app = licensed_app(Path(self.temp.name), session_factory=FakeVoice, client_factory=FakeClient,
                              file_session_factory=FakeVoice, desktop_services=False)
        self.app.geometry('900x640+60+60')
        for variable in (self.app.filler_var, self.app.punct_var, self.app.layout_var, self.app.rewrite_var):
            variable.set(False)
        self.app.update()
        self.app.after_cancel(self.app.poll_job)
        self.app.poll_job = None
        self.app.window_controller.close()
        self.now = 100.0
        self.rect = Rect(300, 100, 1000, 700)
        self.screens = [Rect(0, 0, 1920, 1040)]
        self.pointer = (1800, 500)
        self.platform = Mock()
        self.platform.bounds.side_effect = lambda: self.rect
        self.platform.screens.side_effect = lambda: self.screens
        self.platform.maximized.return_value = False
        self.platform.pointer_down.return_value = False
        self.platform.activate.side_effect = lambda: self.app.text.focus_force()
        def move_to(x, y):
            self.rect = Rect(x, y, self.rect.width, self.rect.height)
        self.platform.move_to.side_effect = move_to
        self.hotkeys = Mock()
        self.hotkeys.poll.return_value = []
        self.controller = DesktopWindowController(self.app, native_enabled=False, platform=self.platform,
                                                  hotkeys=self.hotkeys, clock=lambda: self.now, automatic_hide=True)
        self.app.window_controller = self.controller
        self.pointer_patch = patch.object(self.app, 'winfo_pointerxy', side_effect=lambda: self.pointer)
        self.pointer_patch.start()
        self.controller.tick(False)

    def tearDown(self):
        self.pointer_patch.stop()
        self.app.close_app()
        self.app = None
        FakeVoice.sessions.clear()
        gc.collect()
        self.temp.cleanup()

    def tick(self, seconds=0.2):
        self.now += seconds
        self.controller.tick(False)
        self.app.update()

    def dock(self):
        self.now += 2
        self.assertTrue(self.controller.dock('left'))
        self.app.update()
        self.assertTrue(self.controller.hidden, {
            'state': self.app.state(), 'status': self.app.status_var.get(),
            'edge': self.controller.edge, 'protected': self.controller.protected(),
            'rect': self.controller.last_rect, 'last_active': self.controller.last_active,
            'now': self.now})

    def test_hide_hover_reveal_preserves_text_selection_and_size(self):
        a, c = self.app, self.controller
        a.set_text('苹果与梨，保留选区')
        a.text.tag_add('sel', '1.0', '1.2')
        before = a.get_selection()
        size = (a.winfo_width(), a.winfo_height())
        self.dock()
        self.assertTrue(c.hidden)
        self.assertEqual(a.state(), 'withdrawn')
        self.assertTrue(c.tab.winfo_viewable())
        self.pointer = (c.tab_area.x+2, c.tab_area.y+2)
        self.tick()
        self.assertTrue(c.hidden)
        self.tick(0.8)
        self.assertFalse(c.hidden)
        self.assertEqual(a.current_text(), '苹果与梨，保留选区')
        self.assertEqual(a.get_selection(), before)
        self.assertEqual((a.winfo_width(), a.winfo_height()), size)
        self.assertFalse(c.tab.winfo_viewable())

    def test_global_wake_without_selection_uses_insertion_and_same_key_stops(self):
        self.app.set_text('保留正文')
        self.dock()
        self.app.start_voice = Mock(return_value=True)
        self.controller.trigger('global_record')
        self.app.update()
        self.assertFalse(self.controller.hidden)
        self.assertTrue(self.controller.temporary)
        self.assertTrue(self.controller.compact)
        self.assertFalse(self.app.full_header.winfo_ismapped())
        self.assertTrue(self.app.compact_header.winfo_ismapped())
        self.assertLess(self.app.winfo_width(), 900)
        self.assertLess(self.app.winfo_height(), 640)
        self.app.start_voice.assert_called_once_with('insert')
        self.app.voice = Mock()
        self.app.stop_voice = Mock()
        self.controller.trigger('global_record')
        self.app.stop_voice.assert_called_once()
        self.assertTrue(self.controller.compact)
        self.app.voice = None

    def test_manual_policy_never_hides_on_mouse_leave_or_opens_on_hover(self):
        a, c = self.app, self.controller
        c.automatic_hide = False
        self.dock()
        self.pointer = (c.tab_area.x+3, c.tab_area.y+3)
        self.tick(5)
        self.tick(5)
        self.assertTrue(c.hidden)
        a.start_voice = Mock(return_value=True)
        c.trigger('global_record')
        a.update()
        self.assertTrue(c.compact)
        self.pointer = (1800, 1000)
        self.tick(30)
        self.assertFalse(c.hidden)
        self.assertTrue(c.hide())
        a.update()
        c.tab_label.event_generate('<ButtonPress-1>', x=10, y=10)
        c.tab_label.event_generate('<ButtonRelease-1>', x=10, y=10)
        a.update()
        self.assertFalse(c.hidden)
        self.assertFalse(c.compact)

    def test_compact_then_full_uses_same_editor_selection_and_undo(self):
        a, c = self.app, self.controller
        a.set_text('需要编辑的苹果')
        a.text.tag_add('sel', '1.5', '1.7')
        selection = a.get_selection()
        editor = a.text
        full_size, minimum = (a.winfo_width(), a.winfo_height()), a.minsize()
        self.dock()
        a.start_voice = Mock(return_value=True)
        c.trigger('global_rewrite')
        a.update()
        self.assertIs(a.text, editor)
        self.assertEqual(a.get_selection(), selection)
        a.start_voice.assert_called_once_with('rewrite')
        self.assertTrue(c.compact)
        a.busy, a.voice = True, Mock()
        c.reveal(focus=True)
        a.update()
        self.assertFalse(c.compact)
        self.assertTrue(a.busy)
        self.assertIs(a.text, editor)
        self.assertEqual(a.get_selection(), selection)
        self.assertEqual((a.winfo_width(), a.winfo_height()), full_size)
        self.assertEqual(a.minsize(), minimum)
        self.assertFalse(a.compact_header.winfo_ismapped())
        self.assertTrue(a.full_header.winfo_ismapped())
        a.busy, a.voice = False, None

    def test_compact_rehide_keeps_original_label_and_hover_opens_full(self):
        self.dock()
        a, c = self.app, self.controller
        label = c.tab_area
        size = (a.winfo_width(), a.winfo_height())
        a.start_voice = Mock(return_value=True)
        c.trigger('global_record')
        a.update()
        self.assertTrue(c.compact)
        self.assertTrue(c.hide())
        a.update()
        self.assertFalse(c.compact)
        self.assertEqual(c.tab_area, label)
        self.pointer = (label.x+4, label.y+4)
        self.tick()
        self.tick(0.8)
        self.assertFalse(c.hidden)
        self.assertFalse(c.compact)
        self.assertEqual((a.winfo_width(), a.winfo_height()), size)

    def test_compact_layout_actions_fit_both_themes_and_do_not_change_text(self):
        self.dock()
        a, c = self.app, self.controller
        a.set_text('快速输入的文本')
        a.start_voice = Mock(return_value=True)
        c.trigger('global_record')
        a.update()
        for _ in range(2):
            for widget in (a.compact_header, a.compact_record, a.compact_rewrite, a.compact_cancel,
                           a.compact_fill, a.text, a.compact_status):
                self.assertTrue(widget.winfo_ismapped())
                self.assertGreaterEqual(widget.winfo_rootx(), a.winfo_rootx())
                self.assertLessEqual(widget.winfo_rootx()+widget.winfo_width(), a.winfo_rootx()+a.winfo_width())
                self.assertLessEqual(widget.winfo_rooty()+widget.winfo_height(), a.winfo_rooty()+a.winfo_height())
            a.toggle_theme()
            a.update()
        self.assertEqual(a.current_text(), '快速输入的文本')

    def test_native_compact_window_anchors_and_restores_full_geometry(self):
        a, c = self.app, self.controller
        c.platform = platform = WindowPlatform(a)
        a.update()
        c.screens = platform.screens()
        c.fit_to_screen(force=True)
        a.update()
        original = platform.bounds()
        full_size = (a.winfo_width(), a.winfo_height())
        for edge in ('top', 'left', 'right', 'bottom'):
            c.dock(edge)
            a.update()
            c.reveal_compact()
            a.update()
            bounds, screen = platform.bounds(), c.screen
            self.assertLess(bounds.width, original.width)
            self.assertLess(bounds.height, original.height)
            self.assertLessEqual(abs({'top': bounds.y-screen.y, 'bottom': bounds.bottom-screen.bottom, 'left': bounds.x-screen.x,
                                      'right': bounds.right-screen.right}[edge]), 2)
            self.assertEqual(bounds.overlap(screen), bounds.width*bounds.height)
            c.reveal()
            a.update()
            self.assertEqual((a.winfo_width(), a.winfo_height()), full_size)
        c.undock()
        a.update()
        restored = platform.bounds()
        self.assertLessEqual(abs(restored.x-original.x), 2)
        self.assertLessEqual(abs(restored.y-original.y), 2)

    def test_unfocused_global_wake_preserves_selection_for_replace_or_instruction(self):
        a = self.app
        a.set_text('帮我把苹果拿过来')
        a.text.tag_add('sel', '1.0', '1.9')
        selection = a.get_selection()
        a.start_voice = Mock(return_value=True)
        for action, mode in (('global_record', 'replace'), ('global_rewrite', 'rewrite')):
            self.controller.trigger(action)
            a.update()
            self.assertEqual(a.get_selection(), selection)
            self.assertEqual(a.start_voice.call_args.args, (mode,))

    def test_real_voice_transaction_retains_range_and_undo_when_woken(self):
        a = self.app
        a.settings = QwenSettings(api_key='offline')
        a.set_text('苹果香蕉')
        a.text.tag_add('sel', '1.0', '1.2')
        self.dock()
        with patch('talkman_app.prepare_microphone_permission', return_value=True):
            self.controller.trigger('global_record')
            a.update()
        self.assertTrue(a.busy)
        self.tick(10)
        self.assertFalse(self.controller.hidden)
        FakeVoice.sessions[-1].emit('done', '梨')
        a.poll_events()
        a.after_cancel(a.poll_job)
        a.poll_job = None
        self.tick()
        self.assertEqual(a.current_text(), '梨香蕉')
        self.tick(3)
        self.assertFalse(self.controller.hidden)
        self.tick(1.2)
        self.assertTrue(self.controller.hidden)
        self.controller.reveal()
        a.undo()
        self.assertEqual(a.current_text(), '苹果香蕉')

    def test_work_settings_and_history_prevent_hiding(self):
        self.dock()
        self.controller.reveal()
        for field in ('busy', 'auto_running', 'target_filling', '_mic_permission_pending', 'history_open'):
            setattr(self.app, field, True)
            self.tick(8)
            self.assertFalse(self.controller.hidden, field)
            setattr(self.app, field, False)
        self.app.open_settings('window')
        self.tick(8)
        self.assertFalse(self.controller.hidden)
        self.assertEqual(self.controller.bindings, {})
        self.app._close_settings(self.app.settings_window)
        self.assertIn('global_record', self.controller.bindings)

    def test_dragging_away_disarms_and_maximize_never_hides(self):
        self.dock()
        self.controller.reveal()
        self.pointer = (500, 300)
        self.tick(1)
        self.rect = Rect(250, 150, 1000, 700)
        self.tick()
        self.tick(10)
        self.assertIsNone(self.controller.edge)
        self.assertFalse(self.controller.hidden)
        self.platform.maximized.return_value = True
        self.rect = self.screens[0]
        self.tick(10)
        self.assertFalse(self.controller.hidden)

    def test_removing_docked_monitor_recovers_window(self):
        self.screens = [Rect(0, 0, 1920, 1040), Rect(-1920, 0, 1920, 1040)]
        self.now += 3
        self.rect = Rect(-1920, 100, 1000, 700)
        self.assertTrue(self.controller.dock('left'))
        self.app.update()
        self.assertTrue(self.controller.hidden)
        self.screens = self.screens[:1]
        self.tick(3)
        self.assertFalse(self.controller.hidden)
        self.assertIsNone(self.controller.edge)
        self.assertEqual(self.app.state(), 'normal')

    def test_pin_and_window_preferences_persist_without_losing_other_preferences(self):
        a = self.app
        a.ui_preferences['magnifier'] = True
        a.toggle_topmost()
        a.update()
        self.assertTrue(a.topmost_var.get())
        self.assertEqual(a.pin_button.cget('text'), '已置顶 ▾')
        values = json.loads(a.ui_path.read_text(encoding='utf-8'))
        self.assertTrue(values['topmost'])
        self.assertTrue(values['magnifier'])
        a.toggle_topmost()
        self.assertFalse(a.topmost_var.get())

    def test_drag_to_screen_edges_never_arms_automatic_hiding(self):
        for rect in (Rect(300, 0, 1000, 700), Rect(0, 100, 1000, 700), Rect(920, 100, 1000, 700)):
            self.rect = rect
            self.tick(2)
            self.tick(10)
            self.assertIsNone(self.controller.edge)
            self.assertFalse(self.controller.hidden)

    def test_dock_each_direction_then_cancel_restores_position_and_selection(self):
        a, c = self.app, self.controller
        a.set_text('保留原始选区与文字')
        a.text.tag_add('sel', '1.0', '1.2')
        selection, original = a.get_selection(), self.rect
        for edge in ('top', 'right', 'left'):
            self.assertTrue(c.dock(edge))
            a.update()
            self.assertEqual(c.edge, edge)
            self.assertTrue(c.tab_label.cget('text'))
            self.assertIn('展开', c.tab_label.cget('text'))
            self.assertGreaterEqual(c.tab_area.height, 36)
            self.assertLessEqual(c.tab_label.winfo_reqwidth()+2, c.tab_area.width)
            self.assertLessEqual(c.tab_label.winfo_reqheight()+2, c.tab_area.height)
            self.assertEqual(a.get_selection(), selection)
        c.undock()
        a.update()
        self.assertIsNone(c.edge)
        self.assertFalse(c.hidden)
        self.assertEqual(self.rect, original)
        self.assertEqual(a.get_selection(), selection)

    def test_failed_position_restore_leaves_visible_normal_window(self):
        self.dock()
        self.platform.move_to.side_effect = OSError('屏幕改变')
        self.assertFalse(self.controller.undock())
        self.assertFalse(self.controller.hidden)
        self.assertIsNone(self.controller.edge)
        self.assertEqual(self.app.state(), 'normal')
        self.assertIn('原位置恢复失败', self.app.status_var.get())

    def test_restore_keeps_original_position_when_bottom_overlaps_taskbar(self):
        original = self.rect = Rect(300, 100, 1000, 970)
        self.dock()
        self.controller.undock()
        self.assertEqual(self.rect, original)

    def test_menu_provides_pin_and_all_directions_and_keeps_selection(self):
        a = self.app
        a.set_text('苹果与梨')
        a.text.tag_add('sel', '1.0', '1.2')
        selection = a.get_selection()
        with patch('tkinter.Menu.tk_popup'):
            a.show_window_menu()
        menu = a.window_menu
        labels = {menu.entrycget(i, 'label'): i for i in range(menu.index('end')+1)
                  if menu.type(i) != 'separator'}
        self.assertEqual(set(labels), {'始终置顶', '收起到顶部', '收起到左侧', '收起到右侧', '收起到底部', '取消收起，恢复普通窗口'})
        menu.invoke(labels['始终置顶'])
        a.update()
        self.assertTrue(a.topmost_var.get())
        menu.invoke(labels['收起到顶部'])
        a.update()
        self.assertTrue(self.controller.hidden)
        self.assertEqual(self.controller.edge, 'top')
        self.assertEqual(a.get_selection(), selection)
        self.controller.reveal()
        with patch('tkinter.Menu.tk_popup'):
            a.show_window_menu()
        a.window_menu.invoke('取消收起，恢复普通窗口')
        a.update()
        self.assertIsNone(self.controller.edge)
        self.assertTrue(a.topmost_var.get())

    def test_request_dock_during_work_does_not_move_or_hide(self):
        self.app.busy = True
        try:
            self.assertFalse(self.controller.dock('top'))
            self.platform.move_to.assert_not_called()
            self.assertFalse(self.controller.hidden)
        finally:
            self.app.busy = False

    def test_dock_can_enable_disabled_preference_and_failure_is_visible(self):
        a, c = self.app, self.controller
        a.edge_hide_var.set(False)
        a.save_window_preferences()
        with patch.object(a, 'save_window_preferences', side_effect=OSError('只读目录')):
            self.assertFalse(c.dock('top'))
        self.assertFalse(a.edge_hide_var.get())
        self.platform.move_to.assert_not_called()
        self.assertIn('只读目录', a.status_var.get())
        self.assertTrue(c.dock('top'))
        self.assertTrue(json.loads(a.ui_path.read_text(encoding='utf-8'))['edge_hide'])
        c.reveal()
        self.platform.move_to.side_effect = OSError('窗口移动失败')
        self.assertFalse(c.dock('right'))
        self.assertFalse(c.hidden)
        self.assertIsNone(c.edge)

    def test_label_click_reveals_with_focus(self):
        self.dock()
        self.controller.tab_label.event_generate('<ButtonPress-1>', x=10, y=10)
        self.assertTrue(self.controller.hidden)
        self.controller.tab_label.event_generate('<ButtonRelease-1>', x=10, y=10)
        self.app.update()
        self.assertFalse(self.controller.hidden)
        self.platform.activate.assert_called_once()

    def drag_label(self, point):
        c = self.controller
        tab = c.tab_area
        c.tab_press(SimpleNamespace(x_root=tab.x+tab.width//2, y_root=tab.y+tab.height//2))
        event = SimpleNamespace(x_root=point[0], y_root=point[1])
        c.tab_motion(event)
        c.tab_release(event)
        self.app.update()

    def test_label_drag_stays_hidden_keeps_position_and_preserves_selection(self):
        a, c = self.app, self.controller
        a.set_text('苹果和香蕉，保留选区')
        a.text.tag_add('sel', '1.0', '1.2')
        selected = a.get_selection()
        original = self.rect
        self.dock()
        first = c.tab_area
        self.drag_label((4, 850))
        moved = c.tab_area
        self.assertEqual(c.edge, 'left')
        self.assertGreater(moved.y, first.y)
        self.assertTrue(c.hidden)
        self.assertEqual(a.get_selection(), selected)
        self.pointer = (moved.x+2, moved.y+2)
        self.tick(3)
        self.assertTrue(c.hidden, '松手后不能立即悬停展开')
        a.start_voice = Mock(return_value=True)
        c.trigger('global_record')
        a.update()
        self.assertTrue(c.compact)
        self.assertEqual(a.start_voice.call_args.args, ('replace',))
        self.assertTrue(c.hide())
        self.assertEqual(c.tab_area, moved)
        c.undock()
        self.assertEqual(self.rect, original)
        self.assertEqual(a.get_selection(), selected)

    def test_label_drag_crosses_edges_and_monitor_then_popup_uses_new_anchor(self):
        a, c = self.app, self.controller
        self.screens.append(Rect(-1920, -180, 1920, 1040))
        self.dock()
        for point, edge in (((1500, 3), 'top'), ((1918, 800), 'right'),
                            ((1000, 1038), 'bottom'), ((-900, -177), 'top')):
            self.drag_label(point)
            self.assertTrue(c.hidden)
            self.assertEqual(c.edge, edge)
            tab = c.tab_area
            self.assertEqual(tab.overlap(c.screen), tab.width*tab.height)
        self.assertEqual(c.screen, self.screens[1])
        a.start_voice = Mock(return_value=True)
        c.trigger('global_rewrite')
        a.update()
        self.assertTrue(c.compact)
        self.assertEqual(self.rect.y, self.screens[1].y)
        self.assertLess(self.rect.x, 0)

    def test_pressing_label_prevents_hover_until_release(self):
        self.dock()
        c = self.controller
        r = c.tab_area
        self.pointer = (r.x+10, r.y+10)
        event = SimpleNamespace(x_root=r.x+10, y_root=r.y+10)
        c.tab_press(event)
        self.tick(2)
        self.assertTrue(c.hidden)
        c.tab_release(event)
        self.assertFalse(c.hidden)

    def test_window_shortcuts_work_with_toolbar_focus_without_changing_selection(self):
        a = self.app
        a.set_text('苹果和梨')
        a.text.tag_add('sel', '1.0', '1.2')
        selection = a.get_selection()
        a.pin_button.focus_force()
        a.update()
        a.toggle_mic = Mock()
        a.pin_button.event_generate('<KeyPress-F2>')
        a.pin_button.event_generate('<KeyRelease-F2>')
        a.update()
        a.toggle_mic.assert_called_once()
        self.assertEqual(a.get_selection(), selection)
        self.assertEqual(a.focus_get(), a.text)

    def test_short_space_outside_editor_never_inserts_a_space(self):
        a = self.app
        a.set_text('保持正文')
        a.pin_button.focus_force()
        a.update()
        a.pin_button.event_generate('<KeyPress-space>')
        a.text.event_generate('<KeyRelease-space>')
        a.update()
        self.assertEqual(a.current_text(), '保持正文')
        self.assertIsNone(a.hold_key)

    def test_custom_global_key_wakes_hidden_window_while_another_editor_has_focus(self):
        a, c = self.app, self.controller
        a.shortcuts.update(replace='Alt+1', rewrite='Alt+2')
        c.sync_hotkeys()
        self.assertEqual(c.bindings['replace'], 'Alt+1')
        self.assertEqual(c.bindings['rewrite'], 'Alt+2')
        a.set_text('TalkMan 里的苹果')
        a.text.tag_add('sel', '1.10', '1.12')
        selection = a.get_selection()
        other = tk.Toplevel(a)
        other.title('离线测试的其他输入框')
        field = tk.Entry(other)
        field.insert(0, '其他输入框的文字不能改变')
        field.pack()
        try:
            for action, mode in (('replace', 'replace'), ('rewrite', 'rewrite')):
                self.dock()
                field.focus_force()
                a.update()
                a.start_voice = Mock(return_value=True)
                self.hotkeys.poll.return_value = [('action', action)]
                self.tick()
                self.hotkeys.poll.return_value = []
                self.assertFalse(c.hidden)
                self.assertTrue(c.compact)
                a.start_voice.assert_called_once_with(mode)
                self.assertEqual(a.get_selection(), selection)
                self.assertEqual(field.get(), '其他输入框的文字不能改变')
                c.hotkey_release(action, 0.05)
                a.voice = Mock()
                a.stop_voice = Mock()
                self.hotkeys.poll.return_value = [('action', action)]
                self.tick()
                self.hotkeys.poll.return_value = []
                a.stop_voice.assert_called_once()
                a.voice = None
                c.reveal()
        finally:
            other.destroy()

    def test_alias_keys_pause_for_settings_and_respect_global_disable(self):
        a, c = self.app, self.controller
        a.shortcuts.update(replace='Alt+1', rewrite='Alt+2')
        c.sync_hotkeys()
        a.open_settings('controls')
        self.assertEqual(c.bindings, {})
        a._close_settings(a.settings_window)
        self.assertIn('replace', c.bindings)
        a.global_voice_var.set(False)
        c.apply_preferences()
        self.assertEqual(c.bindings, {'global_show': a.shortcuts['global_show']})

    def test_global_combo_in_main_window_is_not_repeated_or_inserted_as_text(self):
        a, c = self.app, self.controller
        a.shortcuts.update(replace='Alt+1', rewrite='Alt+2')
        c.sync_hotkeys()
        a.set_text('原光标和文字')
        a.text.mark_set('insert', '1.2')
        a.start_voice = Mock(return_value=True)
        event = SimpleNamespace(widget=a.pin_button, keysym='1', state=8 if sys.platform != 'darwin' else 16)
        a.window_shortcut(event)
        a.window_shortcut(event)
        a.update()
        a.start_voice.assert_called_once_with('insert')
        self.assertEqual(a.text.index('insert'), '1.2')
        self.assertEqual(a.current_text(), '原光标和文字')
        a.key_release(event)
        self.assertIsNone(a._global_key_down)

    def available_windows_test_bindings(self, count):
        # 测试电脑可能已被其他工具占用 F18/F19；只挑空闲测试键，不抢用户的快捷键。
        import ctypes as C
        from ctypes import wintypes as W
        user = C.WinDLL('user32', use_last_error=True)
        user.RegisterHotKey.argtypes = [W.HWND, C.c_int, W.UINT, W.UINT]
        user.RegisterHotKey.restype = W.BOOL
        user.UnregisterHotKey.argtypes = [W.HWND, C.c_int]
        available = []
        for number in range(20, 5, -1):
            binding = f'Control+Alt+Shift+F{number}'
            flags, key = native_binding(binding, 'win32')
            if user.RegisterHotKey(None, 0x540E, flags, key):
                user.UnregisterHotKey(None, 0x540E)
                available.append(binding)
                if len(available) == count:
                    return available
        self.skipTest('本机没有足够空闲的原生测试热键')

    @unittest.skipUnless(sys.platform == 'win32', 'Windows 原生后台唤起')
    def test_windows_native_alias_dispatch_wakes_hidden_app_from_other_input(self):
        import ctypes as C
        from ctypes import wintypes as W
        a, c = self.app, self.controller
        c.platform = WindowPlatform(a)
        native = GlobalHotkeys()
        c.hotkeys = native
        c.bindings = None
        record_binding, rewrite_binding = self.available_windows_test_bindings(2)
        a.shortcuts.update(global_record='', global_rewrite='', global_show='',
                           replace=record_binding, rewrite=rewrite_binding)
        c.sync_hotkeys()
        a.set_text('保留 TalkMan 的原光标')
        a.text.mark_set('insert', '1.3')
        self.dock()
        other = tk.Toplevel(a)
        field = tk.Entry(other)
        field.insert(0, '其他软件输入区')
        field.pack()
        field.focus_force()
        a.update()
        a.start_voice = Mock(return_value=True)
        user = C.WinDLL('user32', use_last_error=True)
        user.RegisterHotKey.argtypes = [W.HWND, C.c_int, W.UINT, W.UINT]
        user.UnregisterHotKey.argtypes = [W.HWND, C.c_int]
        user.PostThreadMessageW.argtypes = [W.DWORD, W.UINT, W.WPARAM, W.LPARAM]
        flags, code = native_binding(a.shortcuts['replace'], 'win32')
        try:
            deadline = time.monotonic()+3
            while time.monotonic() < deadline:
                if not user.RegisterHotKey(None, 0x540F, flags, code):
                    break
                user.UnregisterHotKey(None, 0x540F)
                time.sleep(0.025)
            else:
                self.fail('测试全局键没有注册成功')
            self.assertEqual(native.poll(), [])
            identifier = 0x5440 + list(c.bindings).index('replace')
            self.assertTrue(user.PostThreadMessageW(native.backend.thread_id, 0x0312, identifier, 0))
            deadline = time.monotonic()+3
            while time.monotonic() < deadline and not a.start_voice.called:
                self.tick()
                time.sleep(0.025)
            a.start_voice.assert_called_once_with('insert')
            self.assertTrue(c.compact)
            self.assertFalse(c.hidden)
            self.assertEqual(a.focus_get(), a.text)
            self.assertEqual(a.text.index('insert'), '1.3')
            self.assertEqual(field.get(), '其他软件输入区')
        finally:
            other.destroy()
            native.close()
            c.hotkeys = self.hotkeys
            c.bindings = None
            user.UnregisterHotKey(None, 0x540F)

    def test_actual_platform_dock_and_cancel_preserve_client_size(self):
        # 仅操控本测试的临时窗口，不依赖真实鼠标，不访问用户数据。
        self.controller.platform = platform = WindowPlatform(self.app)
        self.app.update()
        self.controller.screens = platform.screens()
        self.controller.fit_to_screen(force=True)
        self.app.update()
        original = platform.bounds()
        client_size = (self.app.winfo_width(), self.app.winfo_height())
        for edge in ('top', 'right', 'left', 'bottom'):
            self.assertTrue(self.controller.dock(edge))
            self.app.update()
            self.controller.reveal()
            self.app.update()
            actual, screen = platform.bounds(), self.controller.screen
            self.assertLessEqual(abs({'top': actual.y-screen.y,
                                      'bottom': actual.bottom-screen.bottom,
                                      'left': actual.x-screen.x,
                                      'right': actual.right-screen.right}[edge]), 2)
            self.assertEqual((self.app.winfo_width(), self.app.winfo_height()), client_size)
        self.controller.undock()
        self.app.update()
        restored = platform.bounds()
        self.assertLessEqual(abs(restored.x-original.x), 2)
        self.assertLessEqual(abs(restored.y-original.y), 2)

    def test_disabled_edge_hide_restores_and_failed_voice_stays_readable(self):
        self.dock()
        self.app.edge_hide_var.set(False)
        self.controller.apply_preferences()
        self.assertFalse(self.controller.hidden)
        self.assertIsNone(self.controller.edge)
        self.app.edge_hide_var.set(True)
        self.rect = Rect(300, 100, 1000, 700)
        self.tick(2)
        self.dock()
        self.app.start_voice = Mock(return_value=False)
        self.controller.trigger('global_record')
        self.app.update()
        self.tick(5)
        self.assertFalse(self.controller.hidden)

    def test_settings_save_global_bindings_and_drafts_label(self):
        a = self.app
        self.assertIn('草稿簿', a.history_button.cget('text'))
        a.open_settings('window')
        a.window_drafts[0].set(False)
        a.window_drafts[1].set(True)
        a.window_shortcut_draft['global_record'] = 'Control+Alt+F3'
        a.window_settings_save()
        self.assertFalse(a.edge_hide_var.get())
        self.assertTrue(a.topmost_var.get())
        self.assertEqual(a.shortcuts['global_record'], 'Control+Alt+F3')
        self.assertEqual(self.controller.bindings['global_record'], 'Control+Alt+F3')

    def test_header_and_window_settings_fit_minimum_window(self):
        a = self.app
        a.geometry('900x640')
        a.update()
        for button in (a.pin_button, a.records_button, a.settings_button):
            self.assertGreaterEqual(button.winfo_rootx(), a.winfo_rootx())
            self.assertLessEqual(button.winfo_rootx()+button.winfo_width(), a.winfo_rootx()+a.winfo_width())
        a.open_settings('window')
        a.update()
        win = a.settings_window
        self.assertLessEqual(win.winfo_height(), a.winfo_screenheight(),
            {name: tab.winfo_reqheight() for name, tab in a.settings_tabs.items()})

    @unittest.skipUnless(sys.platform == 'win32', 'Windows 原生热键')
    def test_windows_native_registration_dispatch_conflict_and_release(self):
        import ctypes as C
        from ctypes import wintypes as W
        hotkeys = GlobalHotkeys()
        user = C.WinDLL('user32', use_last_error=True)
        user.RegisterHotKey.argtypes = [W.HWND, C.c_int, W.UINT, W.UINT]
        user.UnregisterHotKey.argtypes = [W.HWND, C.c_int]
        user.PostThreadMessageW.argtypes = [W.DWORD, W.UINT, W.WPARAM, W.LPARAM]
        binding = self.available_windows_test_bindings(1)[0]
        flags, key = native_binding(binding, 'win32')
        try:
            hotkeys.configure({'global_record': binding})
            deadline = time.monotonic()+3
            registered = False
            while time.monotonic() < deadline:
                if not user.RegisterHotKey(None, 0x540F, flags, key):
                    registered = True
                    break
                user.UnregisterHotKey(None, 0x540F)
                time.sleep(0.025)
            self.assertTrue(registered)
            self.assertEqual(hotkeys.poll(), [])
            # 向本测试的注册线程投递系统消息，不模拟用户键盘、不操作其他窗口。
            self.assertTrue(user.PostThreadMessageW(hotkeys.backend.thread_id, 0x0312, 0x5440, 0))
            deadline = time.monotonic()+3
            events = []
            while time.monotonic() < deadline and not any(e[0] == 'release' for e in events):
                time.sleep(0.025)
                events.extend(hotkeys.poll())
            self.assertEqual([e for e in events if e[0] == 'action'], [('action', 'global_record')])
            self.assertTrue(any(e[0] == 'release' and e[1][0] == 'global_record' for e in events))
            hotkeys.configure({})
            time.sleep(0.1)
            self.assertTrue(user.RegisterHotKey(None, 0x540F, flags, key))
            hotkeys.configure({'global_record': binding})
            time.sleep(0.1)
            errors = hotkeys.poll()
            self.assertEqual(errors[0][0], 'error')
            self.assertIn('占用', errors[0][1])
        finally:
            user.UnregisterHotKey(None, 0x540F)
            hotkeys.close()

    @unittest.skipUnless(sys.platform == 'darwin', 'Mac 原生热键')
    def test_mac_native_registration_and_event_dispatch(self):
        import ctypes as C
        hotkeys = GlobalHotkeys()
        try:
            hotkeys.configure({'global_record': 'Control+Alt+Shift+F19'})
            self.assertEqual(hotkeys.poll(), [])
            backend = hotkeys.backend
            self.assertEqual(len(backend.refs), 1)
            lib = backend.library
            lib.CreateEvent.argtypes = [C.c_void_p, C.c_uint32, C.c_uint32, C.c_double,
                                       C.c_uint32, C.POINTER(C.c_void_p)]
            lib.SetEventParameter.argtypes = [C.c_void_p, C.c_uint32, C.c_uint32, C.c_uint32, C.c_void_p]
            lib.GetMainEventQueue.restype = C.c_void_p
            lib.PostEventToQueue.argtypes = [C.c_void_p, C.c_void_p, C.c_int16]
            lib.ReleaseEvent.argtypes = [C.c_void_p]
            event = C.c_void_p()
            self.assertEqual(lib.CreateEvent(None, backend._four('keyb'), 5, 0, 0, C.byref(event)), 0)
            identifier = backend.HotkeyID(backend._four('Tlkm'), 1)
            try:
                self.assertEqual(lib.SetEventParameter(event, backend._four('----'), backend._four('hkid'),
                                                      C.sizeof(identifier), C.byref(identifier)), 0)
                self.assertEqual(lib.PostEventToQueue(lib.GetMainEventQueue(), event, 1), 0)
                deadline = time.monotonic()+3
                events = []
                while time.monotonic() < deadline and not events:
                    self.app.update()
                    events = hotkeys.poll()
                    time.sleep(0.01)
                self.assertEqual(events, [('action', 'global_record')])
            finally:
                lib.ReleaseEvent(event)
            hotkeys.configure({})
            self.assertEqual(backend.refs, [])
        finally:
            hotkeys.close()


if __name__ == '__main__':
    unittest.main()
