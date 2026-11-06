"""离线桌面回归：真实 Tk 按键、取消事务、快捷键和历史删除。"""
from test_talkman_license_support import licensed_app
import gc
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import tkinter as tk
import unittest
from unittest.mock import patch

from talkman_app import TalkManApp
from talkman_qwen import QwenError, QwenSettings
from talkman_asr import ASRSettings
from talkman_shortcuts import DEFAULT_SHORTCUTS, load_shortcuts
from talkman_macos_setup import MacSetupStatus
from test_talkman_app import FakeClient, FakeVoice


class DesktopControlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            probe = tk.Tk()
            probe.withdraw()
            probe.destroy()
        except tk.TclError as error:
            raise unittest.SkipTest("本机 Tcl/Tk 不能初始化：" + str(error))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="TalkMan-controls-")
        permission = patch("talkman_app.prepare_microphone_permission", return_value=True)
        permission.start()
        self.addCleanup(permission.stop)
        self.app = licensed_app(Path(self.temp.name), session_factory=FakeVoice,
                              client_factory=FakeClient, file_session_factory=FakeVoice)
        self.app.settings = QwenSettings(api_key="sk-offline-test")
        self.app.after_cancel(self.app.poll_job)
        self.app.poll_job = None
        self.app.geometry("1000x700+30+30")
        self.app.update()
        self.app.text.focus_force()
        self.app.update()

    def tearDown(self):
        self.app.close_app()
        self.app = None
        FakeVoice.sessions.clear()
        gc.collect()
        self.temp.cleanup()

    def select(self, start="1.0", end="1.2"):
        self.app.text.tag_remove("sel", "1.0", "end")
        self.app.text.tag_add("sel", start, end)
        self.app.update_meta()

    def pump(self):
        self.app.poll_events()
        self.app.after_cancel(self.app.poll_job)
        self.app.poll_job = None

    def key(self, key, release=False, target=None, state=0):
        widget = target or self.app.text
        widget.event_generate("<KeyRelease>" if release else "<KeyPress>", keysym=key, state=state)
        self.app.update()

    def wait_hold(self):
        self._button_time = getattr(self, '_button_time', 1000) + 300
        done = tk.BooleanVar(master=self.app, value=False)
        self.app.after(300, done.set, True)
        self.app.wait_variable(done)

    def save_history_item(self, text):
        self.app.set_text(text)
        self.assertTrue(self.app.save_current())
        return self.app.saved_history[0]["id"]

    def test_escape_from_disabled_editor_restores_original_and_ignores_late_voice(self):
        app = self.app
        app.set_text("苹果\n原文  ")
        self.select()
        self.assertTrue(app.start_voice("replace"))
        voice = FakeVoice.sessions[-1]
        voice.emit("partial", "错误替换")
        self.pump()
        self.assertIn("错误替换", app.current_text())
        self.key("Escape")
        self.assertFalse(app.busy)
        self.assertTrue(voice.cancelled)
        self.assertEqual(app.current_text(), "苹果\n原文  ")
        voice.emit("done", "迟到的内容")
        self.pump()
        self.assertEqual(app.current_text(), "苹果\n原文  ")

    def test_escape_releases_button_grab_and_pressed_state(self):
        app = self.app
        app.set_text("苹果")
        self.select()
        app.replace_button.event_generate("<ButtonPress-1>", x=10, y=10)
        app.update()
        self.assertTrue(app.busy)
        self.assertEqual(app.replace_button.grab_current(), app.replace_button)
        self.key("Escape")
        self.assertFalse(app.busy)
        self.assertIsNone(app.grab_current())
        self.assertFalse(app.replace_button.instate(["pressed"]))
        self.assertEqual(app.current_text(), "苹果")

    def test_cancel_during_instruction_request_ignores_late_model_result(self):
        app = self.app
        app.set_text("请把苹果拿过来。")
        self.select("1.0", "1.8")
        self.assertTrue(app.start_voice("rewrite"))
        with patch.object(app, "run_worker") as worker:
            FakeVoice.sessions[-1].emit("done", "把苹果改成梨")
            self.pump()
        kind, token, _ = worker.call_args.args
        self.assertTrue(app.busy)
        self.key("Escape")
        app.events.put((kind, token, "ok", "请把梨拿过来。"))
        self.pump()
        self.assertFalse(app.busy)
        self.assertEqual(app.current_text(), "请把苹果拿过来。")

    def test_escape_cancels_auto_result_without_discarding_new_draft(self):
        app = self.app
        app.set_text("嗯 苹果")
        app.filler_var.set(True)
        with patch.object(app, "run_worker") as worker:
            app.apply_auto()
        kind, token, _ = worker.call_args.args
        self.assertTrue(app.auto_running)
        self.key("Escape")
        app.events.put((kind, token, "ok", "苹果"))
        self.pump()
        self.assertEqual(app.current_text(), "嗯 苹果")
        self.assertFalse(app.auto_running)
        self.assertIsNone(app.auto_job)

    def test_configured_selection_hold_shortcuts_start_and_stop(self):
        app = self.app
        app.shortcuts = {"mic": "F5", "replace": "F3", "rewrite": "F4"}
        for key, mode in (("F3", "replace"), ("F4", "rewrite")):
            with self.subTest(mode=mode):
                app.set_text("苹果")
                self.select()
                self.key(key)
                self.wait_hold()
                self.assertEqual(app.voice_mode, mode)
                self.assertTrue(app.busy)
                voice = FakeVoice.sessions[-1]
                self.key(key, release=True)
                self.assertTrue(voice.stopped)
                self.assertIsNone(app.hold_key)
                app.cancel_operation()

    def test_mic_toggle_ignores_key_repeat_until_release(self):
        app = self.app
        app.shortcuts = {"mic": "F5", "replace": "F3", "rewrite": "F4"}
        app.set_text("原文")
        app.text.mark_set("insert", "end-1c")
        self.key("F5")
        self.assertEqual(app.voice_mode, "insert")
        voice = FakeVoice.sessions[-1]
        count = len(FakeVoice.sessions)
        self.key("F5")
        self.key("F5")
        self.assertFalse(voice.stopped)
        self.assertEqual(len(FakeVoice.sessions), count)
        self.key("F5", release=True)
        self.key("F5")
        self.assertTrue(voice.stopped)
        self.key("F5", release=True)

    def test_short_space_follows_recording_caret_then_hold_inserts(self):
        app = self.app
        app.set_text("拿苹果过来")
        self.select("1.1", "1.3")
        self.assertTrue(app.start_voice("replace"))
        FakeVoice.sessions[-1].emit("done", "香蕉")
        self.pump()
        self.assertIsNone(app.get_selection())
        self.assertEqual(app.text.index("insert"), "1.3")
        self.key("space")
        self.key("space", release=True)
        self.assertEqual(app.current_text(), "拿香蕉 过来")
        self.assertIsNone(app.get_selection())
        self.key("space")
        self.wait_hold()
        self.assertEqual(app.voice_mode, "insert")
        self.key("space", release=True)
        FakeVoice.sessions[-1].emit("done", "梨")
        self.pump()
        self.assertEqual(app.current_text(), "拿香蕉 梨过来")

    def test_clearing_selection_resumes_whole_text_processing(self):
        app = self.app
        app.set_text("苹果")
        app.punct_var.set(True)
        self.select()
        app.schedule_auto()
        app.cancel_auto_timer()
        with patch.object(app, "run_worker") as worker:
            app.apply_auto()
        self.assertTrue(app.auto_running)
        self.assertEqual(worker.call_args.args[0], "auto")
        app.cancel_operation()
        app.auto_block_revision = -1
        self.key("Escape")
        self.assertIsNone(app.get_selection())
        self.assertIsNotNone(app.auto_job)
        self.assertNotEqual(app.auto_block_revision, app.revision)
        app.cancel_auto_timer()
        with patch.object(app, "run_worker") as worker:
            app.apply_auto()
        self.assertEqual(worker.call_args.args[0], "auto")
        app.cancel_operation()
        self.select()
        app.auto_block_revision = -1
        app.text.event_generate("<ButtonPress-1>", x=20, y=20)
        app.text.event_generate("<ButtonRelease-1>", x=20, y=20)
        app.update()
        self.assertIsNone(app.get_selection())
        self.assertIsNotNone(app.auto_job)

    def test_delete_selection_is_undoable_preserves_surroundings_history_and_rejects_late_processing(self):
        app = self.app
        identifier = self.save_history_item("保存的文本")
        app.set_text("当前😀草稿\n第二行  ")
        self.select("1.2", "2.2")
        app.auto_running = True
        token = (app.auto_request, app.revision, app.current_text())
        app.one_filler_button.state(["disabled"])
        app.backspace_button.invoke()
        self.assertEqual(app.current_text(), "当前行  ")
        self.assertEqual(app.text.index("insert"), "1.2")
        self.assertIsNone(app.get_selection())
        self.assertFalse(app.auto_running)
        self.assertFalse(app.one_filler_button.instate(["disabled"]))
        self.assertEqual(app.saved_history[0]["id"], identifier)
        app.events.put(("auto", token, "ok", "迟到的处理"))
        self.pump()
        self.assertEqual(app.current_text(), "当前行  ")
        app.undo()
        self.assertEqual(app.current_text(), "当前😀草稿\n第二行  ")
        app.redo()
        self.assertEqual(app.current_text(), "当前行  ")

    def test_clear_is_disabled_during_voice_and_filler_has_an_icon(self):
        app = self.app
        app.set_text("原文")
        self.select()
        self.assertTrue(app.one_filler_button.cget("image"))
        self.assertEqual(str(app.one_filler_button.cget("style")), "Outline.TButton")
        self.assertTrue(app.start_voice("insert"))
        self.assertTrue(app.clear_button.instate(["disabled"]))
        app.clear_button.invoke()
        self.assertEqual(app.current_text(), "原文")
        app.cancel_operation()

    def test_settings_capture_existing_shortcuts_and_persist_on_save(self):
        app = self.app
        app.open_controls_settings()
        app.controls_window.update()
        for action, key in (("mic", "F5"), ("replace", "F3"), ("rewrite", "F4"), ("clear", "F11")):
            entry, variable = app.shortcut_fields[action]
            entry.focus_force()
            app.update()
            self.key(key, target=entry)
            self.key(key, release=True, target=entry)
            self.assertEqual(variable.get(), key)
        self.assertEqual(len(FakeVoice.sessions), 0)
        app.controls_save()
        self.assertEqual(app.shortcuts, {**DEFAULT_SHORTCUTS, "mic": "F5", "replace": "F3", "rewrite": "F4", "clear": "F11"})
        self.assertEqual(load_shortcuts(app.data_dir), app.shortcuts)
        self.assertFalse(app.controls_window.winfo_exists())

    def test_delete_selection_shortcut_is_undoable_and_ignored_while_recording(self):
        app = self.app
        app.set_text("要删除的内容")
        self.select("1.1", "1.3")
        self.key("F8")
        self.key("F8", release=True)
        self.assertEqual(app.current_text(), "")
        app.undo()
        self.assertEqual(app.current_text(), "要删除的内容")
        self.select("1.1", "1.3")
        self.assertTrue(app.start_voice("insert"))
        self.key("F8")
        self.key("F8", release=True)
        self.assertEqual(app.current_text(), "要删除的内容")
        self.assertFalse(FakeVoice.sessions[-1].stopped)
        app.cancel_operation()

    def test_delete_button_tracks_native_selection_and_no_selection_never_clears_text(self):
        app = self.app
        app.set_text("全文需要保留")
        app.update()
        self.assertFalse(app.clear_button.instate(["disabled"]))
        self.assertEqual(app.current_text(), "全文需要保留")
        # 原生全选和取消选区也必须刷新按钮，不依赖鼠标抬起回调。
        app.text.tag_add("sel", "1.0", "end-1c")
        app.update()
        self.assertFalse(app.clear_button.instate(["disabled"]))
        app.text.tag_remove("sel", "1.0", "end")
        app.update()
        self.assertFalse(app.clear_button.instate(["disabled"]))
        app.text.tag_add("sel", "1.0", "end-1c")
        app.update()
        app.clear_button.invoke()
        self.assertEqual(app.current_text(), "")
        app.undo()
        self.assertEqual(app.current_text(), "全文需要保留")

    def button_event(self, button, release=False, x=20, y=20):
        self._button_time = getattr(self, '_button_time', 1000) + 10
        button.event_generate("<ButtonRelease-1>" if release else "<ButtonPress-1>", x=x, y=y, time=self._button_time)
        self.app.update()

    def test_both_voice_buttons_support_click_to_start_and_click_to_stop(self):
        app = self.app
        for button, mode in ((app.replace_button, "replace"), (app.rewrite_button, "rewrite")):
            with self.subTest(mode=mode):
                app.set_text("苹果")
                self.select()
                self.button_event(button)
                self.button_event(button, release=True)
                self.assertTrue(app.busy)
                voice = FakeVoice.sessions[-1]
                self.assertFalse(voice.stopped)
                self.assertEqual(app.voice_mode, mode)
                self.assertIsNone(app.grab_current())
                self.assertFalse(button.instate(["disabled"]))
                self.assertFalse(button.instate(["pressed"]))
                self.assertEqual(button.cget("text"), "结束录音")
                self.button_event(button)
                self.button_event(button, release=True)
                self.assertTrue(voice.stopped)
                self.assertTrue(button.instate(["disabled"]))
                self.assertEqual(len(FakeVoice.sessions), 1)
                app.cancel_operation()
                FakeVoice.sessions.clear()

    def test_both_voice_buttons_stop_on_hold_release_and_release_outside(self):
        app = self.app
        for button, mode in ((app.replace_button, "replace"), (app.rewrite_button, "rewrite")):
            with self.subTest(mode=mode):
                app.set_text("苹果")
                self.select()
                self.button_event(button)
                self.wait_hold()
                self.button_event(button, release=True)
                self.assertTrue(FakeVoice.sessions[-1].stopped)
                self.assertIsNone(app.grab_current())
                app.cancel_operation()
                self.button_event(button)
                self.button_event(button, release=True, x=-10)
                self.assertTrue(FakeVoice.sessions[-1].stopped)
                app.cancel_operation()

    def test_recording_button_keeps_editor_focus_and_caret_for_instruction(self):
        app = self.app
        app.set_text("前文后文")
        app.text.mark_set("insert", "1.2")
        self.button_event(app.rewrite_button)
        self.button_event(app.rewrite_button, release=True)
        self.assertEqual(app.focus_displayof(), app.text)
        self.assertEqual((app.voice_start, app.voice_end), (2, 2))
        self.assertFalse(FakeVoice.sessions[-1].stopped)
        self.key("Escape")
        self.assertFalse(app.busy)
        self.assertIsNone(app.voice_button_press)

    def test_button_keyboard_hold_release_follows_focus_to_editor(self):
        app = self.app
        app.set_text("原文")
        app.text.mark_set("insert", "1.1")
        for key in ("Return", "space"):
            with self.subTest(key=key):
                app.rewrite_button.focus_force()
                app.update()
                self.key(key, target=app.rewrite_button)
                self.wait_hold()
                self.assertEqual(app.focus_displayof(), app.text)
                self.key(key, release=True)
                self.assertTrue(FakeVoice.sessions[-1].stopped)
                self.assertIsNone(app.grab_current())
                self.assertIsNone(app.voice_button_press)
                app.cancel_operation()

    def test_empty_hint_is_above_caret_and_target_button_has_icon_in_both_themes(self):
        app = self.app
        app.update()
        self.assertLessEqual(app.empty_hint.winfo_rooty() + app.empty_hint.winfo_height(),
                             app.text.winfo_rooty() + int(app.text.cget("pady")))
        self.assertTrue(app.target_button.cget("image"))
        app.toggle_theme()
        app.update()
        self.assertTrue(app.target_button.cget("image"))
        style = tk.ttk.Style(app)
        self.assertEqual(style.lookup("Outline.TButton", "background"), app.palette["bg"])

    def test_modifier_mic_triggers_on_release_but_not_for_copy_or_undo(self):
        app = self.app
        app.shortcuts = {"mic": "Control", "replace": "F3", "rewrite": "F4"}
        app.set_text("苹果")
        app.set_text("梨")
        self.key("Control_L")
        self.assertEqual(len(FakeVoice.sessions), 0)
        self.key("c", state=4)
        self.key("c", release=True, state=4)
        self.key("Control_L", release=True, state=4)
        self.assertEqual(len(FakeVoice.sessions), 0)
        self.key("Control_L")
        self.key("z", state=4)
        self.key("z", release=True, state=4)
        self.key("Control_L", release=True, state=4)
        self.assertEqual(app.current_text(), "苹果")
        self.assertEqual(len(FakeVoice.sessions), 0)
        # Windows Tk 无法合成右 Ctrl 的 keycode；直接验证同一事件处理路径。
        from types import SimpleNamespace
        app.key_press(SimpleNamespace(keysym="Control_R",state=0))
        self.assertEqual(len(FakeVoice.sessions), 0)
        app.key_release(SimpleNamespace(keysym="Control_R",state=4))
        self.assertEqual(len(FakeVoice.sessions), 1)
        self.assertEqual(app.voice_mode, "insert")

    def test_settings_duplicate_shortcuts_do_not_replace_valid_configuration(self):
        app = self.app
        original = dict(app.shortcuts)
        app.open_controls_settings()
        app.shortcut_draft.update({"mic": "F3", "replace": "F3"})
        app.controls_save()
        self.assertEqual(app.shortcuts, original)
        self.assertTrue(app.controls_window.winfo_exists())
        self.assertEqual(load_shortcuts(app.data_dir), original)

    def test_history_is_embedded_and_close_preserves_document_selection_and_saved_data(self):
        app = self.app
        self.assertFalse(app.history_open)
        self.assertFalse(app.history_panel.winfo_viewable())
        identifier = self.save_history_item("历史内容  \n第二行")
        app.set_text("当前草稿")
        self.select()
        before_selection = app.get_selection()
        before_size = (app.text.winfo_width(), app.text.winfo_height())
        before_windows = {str(child) for child in app.winfo_children() if isinstance(child, tk.Toplevel)}
        app.open_history()
        app.update()
        self.assertTrue(app.history_open)
        self.assertTrue(app.history_panel.winfo_viewable())
        self.assertIs(app.history_panel.master, app)
        self.assertIs(app.history_panel.winfo_toplevel(), app)
        self.assertEqual((app.text.winfo_width(), app.text.winfo_height()), before_size)
        self.assertEqual({str(child) for child in app.winfo_children() if isinstance(child, tk.Toplevel)}, before_windows)
        app.history_list.selection_set(identifier)
        app.preview_history()
        self.assertEqual(app.preview_widget.get("1.0", "end-1c"), "历史内容  \n第二行")
        self.assertEqual(app.current_text(), "当前草稿")
        self.assertEqual(app.get_selection(), before_selection)
        app.close_history()
        app.update()
        self.assertFalse(app.history_open)
        self.assertFalse(app.history_panel.winfo_viewable())
        self.assertEqual(app.saved_history[0]["text"], "历史内容  \n第二行")
        self.assertEqual(app.current_text(), "当前草稿")
        self.assertEqual(app.get_selection(), before_selection)
        app.open_history()
        app.update()
        self.assertTrue(app.history_panel.winfo_viewable())
        self.assertIn(identifier, app.history_list.get_children())

    def test_history_drawer_stays_inside_main_window_when_window_resizes(self):
        app = self.app
        app.open_history()
        for dimensions in ("900x640", "1180x780", "1500x900"):
            with self.subTest(dimensions=dimensions):
                app.geometry(dimensions)
                app.update()
                panel, surface = app.history_panel, app.editor_surface
                self.assertGreater(panel.winfo_x(), 0)
                self.assertGreaterEqual(panel.winfo_y(), surface.master.winfo_y())
                self.assertLessEqual(panel.winfo_x() + panel.winfo_width(), app.winfo_width() - 20)
                self.assertLessEqual(panel.winfo_y() + panel.winfo_height(), app.winfo_height() - 20)
                self.assertGreater(panel.winfo_height(), surface.winfo_height())
                self.assertGreaterEqual(panel.winfo_width(), 580)
                self.assertGreaterEqual(app.text.winfo_width(), surface.winfo_width() - 16)
                for button in (app.history_restore_button, app.history_delete_button):
                    x = button.winfo_rootx() - panel.winfo_rootx()
                    y = button.winfo_rooty() - panel.winfo_rooty()
                    self.assertGreaterEqual(x, 0)
                    self.assertGreaterEqual(y, 0)
                    self.assertLessEqual(x + button.winfo_width(), panel.winfo_width())
                    self.assertLessEqual(y + button.winfo_height(), panel.winfo_height())

    def test_open_history_cancels_pending_voice_shortcut_and_escape_closes_drawer(self):
        app = self.app
        app.set_text("苹果")
        self.select()
        self.key("space")
        self.assertIsNotNone(app.hold_job)
        app.open_history()
        app.update()
        self.wait_hold()
        self.assertIsNone(app.hold_job)
        self.assertIsNone(app.hold_key)
        self.assertEqual(len(FakeVoice.sessions), 0)
        self.key("Escape", target=app.history_list)
        self.assertFalse(app.history_open)
        self.assertEqual(app.current_text(), "苹果")

    def test_history_prevents_magnifier_and_voice_closes_overlay_without_losing_selection(self):
        app = self.app
        app.magnifier_var.set(True)
        app.set_text("苹果香蕉")
        self.select()
        self.assertTrue(app.magnifier.allowed())
        app.open_history()
        app.update()
        self.assertFalse(app.magnifier.allowed())
        self.assertTrue(app.start_voice("replace"))
        self.assertFalse(app.history_open)
        FakeVoice.sessions[-1].emit("done", "梨")
        self.pump()
        self.assertEqual(app.current_text(), "梨香蕉")

    def test_history_scrollbars_appear_only_for_overflow_and_allow_reading_to_end(self):
        app = self.app
        identifier = self.save_history_item("短历史")
        app.open_history()
        app.history_list.selection_set(identifier)
        app.preview_history()
        app.update()

        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)

        widgets = list(descendants(app.history_panel))
        scrollbars = [widget for widget in widgets if isinstance(widget, tk.ttk.Scrollbar)]
        self.assertFalse(any(isinstance(widget, (tk.PanedWindow, tk.ttk.Panedwindow)) for widget in widgets))
        self.assertEqual(len(scrollbars), 2)
        self.assertFalse(any(widget.winfo_viewable() for widget in scrollbars))
        self.assertEqual(str(app.preview_widget.cget("state")), "disabled")

        long_text = "\n".join(f"第 {number} 行历史文字" for number in range(120))
        app.write_history([{"id": str(number), "time": "2026-10-09 08:00:00", "text": long_text}
                           for number in range(30)])
        app.history_list.selection_set("0")
        app.preview_history()
        app.update()
        self.assertTrue(all(widget.winfo_viewable() for widget in scrollbars))
        app.history_list.yview_moveto(1)
        app.preview_widget.yview_moveto(1)
        app.update()
        self.assertAlmostEqual(app.history_list.yview()[1], 1)
        self.assertAlmostEqual(app.preview_widget.yview()[1], 1)
        self.assertEqual(app.current_text(), "短历史")

    def test_mac_setup_only_requests_microphone_when_clicked_and_never_records(self):
        app = self.app
        with patch("talkman_settings_ui.sys.platform", "darwin"), \
                patch("talkman_settings_ui.check_permissions", return_value=MacSetupStatus("26.1", 0, False, None)), \
                patch("talkman_settings_ui.prepare_microphone_permission", return_value=True) as prepare:
            app.open_settings("mac")
            app.update()
            self.assertEqual(app.settings_notebook.select(), str(app.settings_tabs["mac"]))
            self.assertIn("等待首次授权", app.mac_setup_status.get())
            prepare.assert_not_called()
            app.mac_setup_microphone_button.invoke()
            prepare.assert_called_once()
            self.assertFalse(app.busy)
            self.assertEqual(FakeVoice.sessions, [])

    def test_mac_setup_denied_authorization_does_not_change_document(self):
        from talkman_voice import VoicePermissionError
        app = self.app
        app.set_text("保留当前编辑内容")
        self.select("1.1", "1.4")
        with patch("talkman_settings_ui.sys.platform", "darwin"), \
                patch("talkman_settings_ui.check_permissions", return_value=MacSetupStatus("15.7", 2, False, None)), \
                patch("talkman_settings_ui.prepare_microphone_permission", side_effect=VoicePermissionError("未允许麦克风")):
            app.open_settings("mac")
            app.update()
            app.mac_setup_microphone_button.invoke()
            self.assertIn("麦克风设置", app.mac_setup_feedback.get())
            self.assertEqual(app.current_text(), "保留当前编辑内容")
            self.assertFalse(app.busy)
            self.assertEqual(FakeVoice.sessions, [])

    def test_closing_mac_setup_during_authorization_does_not_leave_recording_locked(self):
        app = self.app
        callbacks = []

        def prepare(callback):
            callbacks.append(callback)
            return False

        with patch("talkman_settings_ui.sys.platform", "darwin"), \
                patch("talkman_settings_ui.check_permissions", return_value=MacSetupStatus("27.0", 0, False, None)), \
                patch("talkman_settings_ui.prepare_microphone_permission", side_effect=prepare):
            app.open_settings("mac")
            app.mac_setup_microphone_button.invoke()
            self.assertTrue(app._mic_permission_pending)
            app._close_settings(app.settings_window)
            callbacks[0](True, "已允许麦克风")
            app.poll_events()
            self.assertFalse(app._mic_permission_pending)
            self.assertFalse(app.busy)
            self.assertEqual(FakeVoice.sessions, [])

    def test_settings_sections_reuse_one_window_and_keep_unsaved_fields(self):
        app = self.app
        app.open_controls_settings()
        original_window = app.settings_window
        app.shortcut_draft["mic"] = "F5"
        app.open_asr_settings()
        self.assertIs(app.settings_window, original_window)
        self.assertIs(app.asr_window, original_window)
        self.assertEqual(app.settings_notebook.select(), str(app.settings_tabs["asr"]))
        app.open_text_settings()
        self.assertIs(app.settings_window, original_window)
        self.assertEqual(app.settings_notebook.select(), str(app.settings_tabs["model"]))
        app.open_controls_settings()
        self.assertIs(app.controls_window, original_window)
        self.assertEqual(app.settings_notebook.select(), str(app.settings_tabs["controls"]))
        self.assertEqual(app.shortcut_draft["mic"], "F5")
        windows = [child for child in app.winfo_children() if isinstance(child, tk.Toplevel)]
        self.assertEqual(windows, [original_window])

    def test_mac_target_diagnostics_copy_metadata_without_editor_or_credentials(self):
        app = self.app
        app.set_text("这份正文不应出现在诊断中")
        controller = SimpleNamespace(
            refresh_note="豆包已运行，但未开放窗口信息。",
            refresh_report="辅助功能：已授权\n豆包：AXWindows 错误码 -25204",
        )
        with patch.object(app, "target_controller", controller), patch("talkman_settings_ui.sys.platform", "darwin"):
            app.open_target_settings()
            app.settings_window.geometry("650x660")
            app.update()
            win = app.settings_window
            for button in (app.target_diagnostic_button, app.target_refresh_button,
                           app.target_choose_button, app.target_clear_button):
                button.focus_force()
                app.update()
                self.assertGreaterEqual(button.winfo_rootx(), win.winfo_rootx())
                self.assertLessEqual(button.winfo_rootx() + button.winfo_width(), win.winfo_rootx() + win.winfo_width())
                self.assertLessEqual(button.winfo_rooty() + button.winfo_height(), win.winfo_rooty() + win.winfo_height())
            app.target_diagnostic_button.invoke()
            report = app.clipboard_get()
        self.assertIn("辅助功能：已授权", report)
        self.assertIn("错误码 -25204", report)
        self.assertIn("运行程序：", report)
        self.assertNotIn(app.current_text(), report)
        self.assertNotIn(app.settings.api_key, report)
        self.assertEqual(app.current_text(), "这份正文不应出现在诊断中")

    def test_mac_key_memory_controls_are_clickable_and_read_saved_flags(self):
        app = self.app
        with patch("talkman_settings_ui.sys.platform", "darwin"), \
             patch("talkman_settings_ui.secure_storage_supported", return_value=True), \
             patch("talkman_settings_ui.qwen_key_remembered", return_value=True) as text_saved, \
             patch("talkman_settings_ui.asr_keys_remembered", return_value=True) as asr_saved:
            app.open_text_settings()
            app.asr_provider_choice.set("豆包 / 火山引擎")
            app.asr_provider_combobox.event_generate("<<ComboboxSelected>>")
            app.update()
            self.assertFalse(app.text_remember_box.instate(["disabled"]))
            self.assertFalse(app.asr_remember_box.instate(["disabled"]))
            self.assertIn("系统钥匙串", app.text_remember_box.cget("text"))
            self.assertIn("系统钥匙串", app.asr_remember_box.cget("text"))
            self.assertTrue(app.text_remember_var.get())
            self.assertTrue(app.asr_remember_var.get())
            app.text_remember_box.invoke()
            app.asr_remember_box.invoke()
            self.assertFalse(app.text_remember_var.get())
            self.assertFalse(app.asr_remember_var.get())
            text_saved.assert_called_once_with(app.data_dir)
            asr_saved.assert_called_once_with(app.data_dir)

    def test_mac_text_settings_save_passes_key_memory_choice(self):
        app = self.app
        with patch("talkman_settings_ui.sys.platform", "darwin"), \
             patch("talkman_settings_ui.secure_storage_supported", return_value=True), \
             patch("talkman_settings_ui.qwen_key_remembered", return_value=False), \
             patch("talkman_settings_ui.asr_keys_remembered", return_value=False), \
             patch("talkman_settings_ui.save_settings") as save:
            app.open_text_settings()
            app.text_remember_box.invoke()
            app.text_settings_save()
            self.assertEqual(save.call_args.args[0], app.data_dir)
            self.assertEqual(save.call_args.args[1].api_key, "sk-offline-test")
            self.assertIs(save.call_args.args[2], True)
            self.assertIn("已保存", app.status_var.get())

    def test_mac_asr_settings_save_passes_credential_memory_choice(self):
        app = self.app
        with patch("talkman_settings_ui.sys.platform", "darwin"), \
             patch("talkman_settings_ui.secure_storage_supported", return_value=True), \
             patch("talkman_settings_ui.qwen_key_remembered", return_value=False), \
             patch("talkman_settings_ui.asr_keys_remembered", return_value=False), \
             patch("talkman_settings_ui.save_asr_settings") as save:
            app.open_asr_settings()
            app.asr_provider_choice.set("豆包 / 火山引擎")
            app.asr_provider_combobox.event_generate("<<ComboboxSelected>>")
            app.asr_fields["api_key"].set("offline-asr-key")
            app.asr_remember_box.invoke()
            app.asr_save()
            self.assertEqual(save.call_args.args[0], app.data_dir)
            self.assertEqual(save.call_args.args[1].provider, "volc")
            self.assertIs(save.call_args.args[2], True)

    def test_missing_mac_keychain_component_keeps_controls_disabled_with_reason(self):
        app = self.app
        with patch("talkman_settings_ui.sys.platform", "darwin"), \
             patch("talkman_settings_ui.secure_storage_supported", return_value=False), \
             patch("talkman_settings_ui.qwen_key_remembered") as text_saved, \
             patch("talkman_settings_ui.asr_keys_remembered") as asr_saved:
            app.open_text_settings()
            self.assertTrue(app.text_remember_box.instate(["disabled"]))
            self.assertTrue(app.asr_remember_box.instate(["disabled"]))
            self.assertIn("钥匙串组件缺失", app.text_settings_feedback.get())
            self.assertFalse(app.text_remember_var.get())
            text_saved.assert_not_called()
            asr_saved.assert_not_called()

    def test_unread_saved_asr_credentials_are_not_overwritten_by_public_profiles(self):
        app = self.app
        app.asr_credentials_error = "系统钥匙串未允许读取凭证"
        with patch("talkman_settings_ui.secure_storage_supported", return_value=True), \
             patch("talkman_settings_ui.asr_keys_remembered", return_value=True), \
             patch("talkman_settings_ui.save_asr_settings") as save:
            app.open_asr_settings()
            app.asr_save()
            save.assert_not_called()
            self.assertIn("尚未读取", app.asr_feedback.get())
            self.assertTrue(app.settings_window.winfo_exists())
            app.asr_remember_box.invoke()
            app.asr_save()
            self.assertIs(save.call_args.args[2], False)
            self.assertEqual(app.asr_credentials_error, "")

    def test_explicit_keychain_restore_preserves_other_providers_on_next_save(self):
        app = self.app
        app.asr_credentials_error = "系统钥匙串未允许读取凭证"
        restored = ASRSettings("qwen", {
            "volc": {"api_key": "offline-volc"}, "baidu": {"api_key": "offline-baidu"},
        })
        with patch("talkman_settings_ui.secure_storage_supported", return_value=True), \
             patch("talkman_settings_ui.asr_keys_remembered", return_value=True), \
             patch("talkman_settings_ui.load_asr_settings", return_value=restored) as load, \
             patch("talkman_settings_ui.save_asr_settings") as save:
            app.open_asr_settings()
            app.asr_restore_credentials()
            load.assert_called_once_with(app.data_dir, allow_keychain_prompt=True)
            self.assertEqual(app.asr_credentials_error, "")
            app.asr_save()
            saved = save.call_args.args[1]
            self.assertEqual(saved.credentials("volc")["api_key"], "offline-volc")
            self.assertEqual(saved.credentials("baidu")["api_key"], "offline-baidu")

    def test_explicit_keychain_restore_failure_keeps_existing_session_key(self):
        app = self.app
        app.qwen_credentials_error = "系统钥匙串未允许读取凭证"
        with patch("talkman_settings_ui.load_settings", side_effect=QwenError("系统钥匙串未允许读取凭证")) as load:
            app.open_text_settings()
            app.text_restore_key()
            load.assert_called_once_with(app.data_dir, allow_keychain_prompt=True)
            self.assertEqual(app.settings.api_key, "sk-offline-test")
            self.assertEqual(app.text_settings_fields["key"].get(), "sk-offline-test")
            self.assertTrue(app.qwen_credentials_error)

    def test_startup_credential_errors_keep_public_configuration_and_details(self):
        self.app.close_app()
        public_qwen = QwenSettings(text_model="custom-model", service_origin="https://maas.qianwenaiapi.com")
        public_asr = ASRSettings("tencent", {"tencent": {"app_id": "123", "engine_type": "16k_en"}})
        with patch("talkman_app.load_settings", side_effect=[QwenError("千问钥匙串读取被拒绝"), public_qwen]) as qwen_load, \
             patch("talkman_app.load_asr_settings", side_effect=[QwenError("语音钥匙串读取被拒绝"), public_asr]) as asr_load:
            self.app = licensed_app(Path(self.temp.name), session_factory=FakeVoice,
                                  client_factory=FakeClient, file_session_factory=FakeVoice)
        self.app.after_cancel(self.app.poll_job)
        self.app.poll_job = None
        self.assertEqual(self.app.settings.text_model, "custom-model")
        self.assertEqual(self.app.asr_settings.provider, "tencent")
        self.assertEqual(self.app.asr_settings.credentials()["engine_type"], "16k_en")
        self.assertIn("千问钥匙串读取被拒绝", self.app.status_var.get())
        self.assertIn("语音钥匙串读取被拒绝", self.app.status_var.get())
        self.assertEqual(qwen_load.call_args.kwargs, {"read_credentials": False})
        self.assertEqual(asr_load.call_args.kwargs, {"read_credentials": False})

    def test_mac_keychain_restore_actions_fit_minimum_settings_window(self):
        app = self.app
        app.qwen_credentials_error = app.asr_credentials_error = "钥匙串读取被拒绝"
        with patch("talkman_settings_ui.sys.platform", "darwin"), \
             patch("talkman_settings_ui.secure_storage_supported", return_value=True):
            app.open_text_settings()
            window = app.settings_window
            window.geometry("650x660")
            window.update()
            for page in ("asr", "text"):
                app.settings_notebook.select(app.settings_tabs[page])
                window.update_idletasks()
                for widget in app.settings_tabs[page].winfo_children():
                    for child in widget.winfo_children():
                        if child.winfo_class() == "TButton" and child.winfo_ismapped():
                            self.assertLessEqual(child.winfo_rootx() + child.winfo_width(),
                                                 window.winfo_rootx() + window.winfo_width())
                            self.assertLessEqual(child.winfo_rooty() + child.winfo_height(),
                                                 window.winfo_rooty() + window.winfo_height())

    def test_history_delete_requires_confirmation_and_persists_only_selected_item(self):
        app = self.app
        first = self.save_history_item("第一条历史")
        second = self.save_history_item("第二条历史")
        app.set_text("当前草稿")
        app.open_history()
        app.history_list.selection_set(first)
        app.preview_history()
        app.delete_history()
        self.assertEqual(len(app.saved_history), 2)
        app.confirm_delete_history()
        self.assertEqual([item["id"] for item in app.saved_history], [second])
        stored = json.loads(app.history_path.read_text(encoding="utf-8"))
        self.assertEqual([item["id"] for item in stored], [second])
        self.assertEqual(app.current_text(), "当前草稿")
        self.assertNotIn(first, app.history_list.get_children())

    def test_history_delete_write_failure_keeps_memory_and_disk(self):
        app = self.app
        identifier = self.save_history_item("应当保留的历史")
        before = app.history_path.read_bytes()
        app.open_history()
        app.history_list.selection_set(identifier)
        app.delete_history()
        with patch("pathlib.Path.replace", side_effect=OSError("模拟写入失败")):
            app.confirm_delete_history()
        self.assertEqual([item["id"] for item in app.saved_history], [identifier])
        self.assertEqual(app.history_path.read_bytes(), before)
        self.assertIn(identifier, app.history_list.get_children())
        self.assertIn("失败", app.status_var.get())


if __name__ == "__main__":
    unittest.main(verbosity=2)
