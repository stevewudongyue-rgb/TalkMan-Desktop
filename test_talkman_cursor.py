"""离线验证滚轮光标与指针聚焦；不移动系统鼠标、不录音、不联网。"""
from test_talkman_license_support import licensed_app
import gc
from pathlib import Path
from types import SimpleNamespace
import tempfile
import tkinter as tk
from tkinter import ttk
import unittest
from unittest.mock import patch

from talkman_app import TalkManApp
from talkman_qwen import QwenSettings
from test_talkman_app import FakeClient, FakeVoice


class CursorNavigationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            probe = tk.Tk()
            probe.withdraw()
            probe.destroy()
        except tk.TclError as error:
            raise unittest.SkipTest("本机 Tcl/Tk 不能初始化：" + str(error))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="TalkMan-cursor-")
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

    def select(self, start, end):
        self.app.text.tag_remove("sel", "1.0", "end")
        self.app.text.tag_add("sel", start, end)
        self.app.update_meta()

    def document_state(self):
        app = self.app
        return (app.current_text(), app.revision, list(app.timeline.items), app.timeline.index)

    def prefix(self):
        return self.app.text.get("1.0", "insert")

    def wheel(self, delta=120, state=0, num=None):
        return self.app.editor_wheel(SimpleNamespace(delta=delta, state=state, num=num))

    def key(self, keysym, *, target=None, state=0):
        (target or self.app.text).event_generate("<KeyPress>", keysym=keysym, state=state)
        self.app.update()

    def pointer(self, x, y):
        text = self.app.text
        return patch.object(text, "winfo_pointerxy", return_value=(
            text.winfo_rootx() + x, text.winfo_rooty() + y))

    def test_default_and_custom_cursor_keys_preserve_document_and_undo(self):
        app = self.app
        app.set_text("中ab文")
        app.text.mark_set("insert", "1.2")
        before = self.document_state()
        self.key("F9")
        self.assertEqual(self.prefix(), "中")
        self.key("F10")
        self.assertEqual(self.prefix(), "中a")
        app.shortcuts["cursor_prev"] = "Control+F11"
        app.shortcuts["cursor_next"] = "F12"
        self.key("F11", state=4)
        self.assertEqual(self.prefix(), "中")
        self.key("F12")
        self.assertEqual(self.prefix(), "中a")
        self.assertEqual(self.document_state(), before)

    def test_movement_clamps_boundaries_and_keeps_whole_emoji_and_newlines(self):
        app = self.app
        app.set_text("中😀a\n文🛰b")
        app.text.mark_set("insert", "1.0")
        before = self.document_state()
        expected = ["中", "中😀", "中😀a", "中😀a\n", "中😀a\n文", "中😀a\n文🛰", "中😀a\n文🛰b"]
        for prefix in expected:
            self.assertTrue(app.move_editor_cursor(1))
            self.assertEqual(self.prefix(), prefix)
        app.move_editor_cursor(1000)
        self.assertEqual(app.text.index("insert"), app.text.index("end-1c"))
        for prefix in reversed([""] + expected[:-1]):
            app.move_editor_cursor(-1)
            self.assertEqual(self.prefix(), prefix)
        app.move_editor_cursor(-1000)
        self.assertEqual(app.text.index("insert"), "1.0")
        self.assertEqual(self.document_state(), before)

    def test_first_selected_step_collapses_to_directional_boundary(self):
        app = self.app
        app.set_text("甲abc乙")
        before = self.document_state()
        for delta, expected in ((-1, "甲"), (1, "甲abc"), (-2, ""), (2, "甲abc乙")):
            with self.subTest(delta=delta):
                self.select("1.1", "1.4")
                app.move_editor_cursor(delta)
                self.assertEqual(self.prefix(), expected)
                self.assertIsNone(app.get_selection())
        self.assertEqual(self.document_state(), before)

    def test_windows_wheel_accumulates_partial_ticks_and_reverses_direction(self):
        app = self.app
        app.set_text("abcdef")
        app.text.mark_set("insert", "1.3")
        before = self.document_state()
        with patch("talkman_app.sys.platform", "win32"):
            self.assertEqual(self.wheel(60), "break")
            self.assertEqual(self.prefix(), "abc")
            self.wheel(60)
            self.assertEqual(self.prefix(), "ab")
            self.wheel(-240)
            self.assertEqual(self.prefix(), "abcd")
            self.wheel(60)
            self.wheel(-60)
            self.assertEqual(app._wheel_remainder, 0)
            self.assertEqual(self.prefix(), "abcd")
        self.assertEqual(self.document_state(), before)

    def test_mac_small_deltas_and_linux_wheel_buttons_move_by_character(self):
        app = self.app
        app.set_text("甲乙丙丁戊")
        app.text.mark_set("insert", "1.3")
        before = self.document_state()
        with patch("talkman_app.sys.platform", "darwin"):
            self.wheel(1)
            self.assertEqual(self.prefix(), "甲乙")
            self.wheel(-2)
            self.assertEqual(self.prefix(), "甲乙丙丁")
        with patch("talkman_app.sys.platform", "linux"):
            self.wheel(0, num=4)
            self.assertEqual(self.prefix(), "甲乙丙")
            self.wheel(0, num=5)
            self.assertEqual(self.prefix(), "甲乙丙丁")
        self.assertEqual(self.document_state(), before)

    def test_wheel_needs_editor_focus_and_shift_scrolls_without_moving_caret(self):
        app = self.app
        app.set_text("\n".join(f"第 {number} 行" for number in range(100)))
        app.text.mark_set("insert", "1.0")
        app.text.yview_moveto(0)
        before = self.document_state()
        old_view = app.text.yview()
        self.assertEqual(self.wheel(-120, state=1), "break")
        self.assertEqual(app.text.index("insert"), "1.0")
        self.assertGreater(app.text.yview()[0], old_view[0])
        app.wheel_caret_var.set(False)
        self.assertIsNone(self.wheel(-120))
        self.assertEqual(app.text.index("insert"), "1.0")
        app.wheel_caret_var.set(True)
        with patch.object(app, "focus_get", return_value=app.settings_button):
            self.assertIsNone(self.wheel(-120))
        self.assertIsNone(self.wheel(-120, state=4))
        self.assertEqual(app.text.index("insert"), "1.0")
        self.assertEqual(self.document_state(), before)

    def test_decorative_lines_forward_mousewheel_and_linux_buttons(self):
        app = self.app
        app.set_text("甲乙丙丁戊")
        app.update()
        app.text.mark_set("insert", "1.3")
        before = self.document_state()
        line = next(widget for widget in app.text._rule_widgets if widget.winfo_ismapped())
        line.event_generate("<MouseWheel>", delta=120, x=2, y=0)
        app.update()
        self.assertEqual(self.prefix(), "甲乙")
        line.event_generate("<Button-5>", x=2, y=0)
        app.update()
        self.assertEqual(self.prefix(), "甲乙丙")
        line.event_generate("<Button-4>", x=2, y=0)
        app.update()
        self.assertEqual(self.prefix(), "甲乙")
        self.assertEqual(self.document_state(), before)

    def test_pointer_focus_uses_nearest_gap_and_clears_selection(self):
        app = self.app
        app.set_text("W苹果ABC")
        app.update()
        box = app.text.bbox("1.2")
        self.assertIsNotNone(box)
        x, y, width, height = box
        before = self.document_state()
        for fraction in (0.1, 0.9):
            with self.subTest(fraction=fraction):
                px, py = x + int(width * fraction), y + height // 2
                expected = "1.2" if fraction < 0.5 else "1.3"
                self.select("1.0", "1.1")
                with self.pointer(px, py), patch.object(app, "winfo_containing", return_value=app.text):
                    self.assertEqual(app.focus_at_pointer(), "break")
                self.assertEqual(app.text.index("insert"), app.text.index(expected))
                self.assertIsNone(app.get_selection())
        self.assertEqual(self.document_state(), before)

    def test_pointer_over_rule_is_editor_but_outside_preserves_selection_and_caret(self):
        app = self.app
        app.set_text("甲乙丙丁")
        app.update()
        line = next(widget for widget in app.text._rule_widgets if widget.winfo_ismapped())
        x, y = line.winfo_x() + 15, line.winfo_y()
        expected = str(app.text.tk.call("tk::TextClosestGap", app.text._w, x, y))
        self.select("1.0", "1.2")
        with self.pointer(x, y), patch.object(app, "winfo_containing", return_value=line):
            app.focus_at_pointer()
        self.assertEqual(app.text.index("insert"), app.text.index(expected))
        self.assertIsNone(app.get_selection())
        self.select("1.0", "1.2")
        app.text.mark_set("insert", "1.3")
        original = app.get_selection()
        with self.pointer(-10, -10):
            app.focus_at_pointer()
        self.assertEqual(app.text.index("insert"), "1.3")
        self.assertEqual(app.get_selection(), original)

    def test_focus_key_works_from_main_window_control_but_not_settings_window(self):
        app = self.app
        app.set_text("甲乙丙丁")
        app.text.mark_set("insert", "1.2")
        app.settings_button.focus_force()
        app.update()
        with self.pointer(-10, -10):
            self.key("F6", target=app.settings_button)
        self.assertEqual(app.focus_get(), app.text)
        self.assertEqual(app.text.index("insert"), "1.2")
        app.shortcuts["focus"] = "F7"
        app.settings_button.focus_force()
        app.update()
        with self.pointer(-10, -10):
            self.key("F7", target=app.settings_button)
        self.assertEqual(app.focus_get(), app.text)
        window = tk.Toplevel(app)
        entry = ttk.Entry(window)
        entry.pack()
        window.update()
        entry.focus_force()
        app.update()
        with patch.object(app, "focus_at_pointer") as focus:
            self.key("F7", target=entry)
            self.assertIsNone(app.focus_shortcut(SimpleNamespace(widget=entry, keysym="F7", state=0)))
            focus.assert_not_called()
        self.assertEqual(app.focus_get(), entry)
        window.destroy()

    def test_voice_history_and_pending_permission_block_navigation(self):
        app = self.app
        app.set_text("甲乙丙丁")
        app.text.mark_set("insert", "1.2")
        self.assertTrue(app.start_voice("insert"))
        voice = FakeVoice.sessions[-1]
        operation = app.operation_id
        index = app.text.index("insert")
        before = self.document_state()
        self.assertFalse(app.move_editor_cursor(1))
        self.assertEqual(self.wheel(-120), "break")
        with self.pointer(20, 20):
            app.focus_at_pointer()
        self.assertEqual(app.text.index("insert"), index)
        self.assertEqual(app.operation_id, operation)
        self.assertFalse(voice.stopped)
        self.assertFalse(voice.cancelled)
        self.assertEqual(self.document_state(), before)
        app.cancel_operation()
        for flag in ("history_open", "_mic_permission_pending"):
            with self.subTest(flag=flag):
                setattr(app, flag, True)
                index = app.text.index("insert")
                before = self.document_state()
                self.assertFalse(app.move_editor_cursor(-1))
                with self.pointer(20, 20):
                    app.focus_at_pointer()
                self.assertEqual(app.text.index("insert"), index)
                self.assertEqual(self.document_state(), before)
                setattr(app, flag, False)

    def test_navigation_closes_magnifier(self):
        app = self.app
        app.set_text("甲乙丙丁")
        app.text.mark_set("insert", "1.2")
        with patch.object(app.magnifier, "hide") as hide:
            app.move_editor_cursor(1)
            hide.assert_called()
        with patch.object(app.magnifier, "hide") as hide, self.pointer(-10, -10):
            app.focus_at_pointer()
            hide.assert_called()


if __name__ == "__main__":
    unittest.main()
