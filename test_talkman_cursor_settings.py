"""离线验证光标快捷键设置、显示偏好保存与设置窗口尺寸。"""
from test_talkman_license_support import licensed_app
import gc
import json
from pathlib import Path
import tempfile
import tkinter as tk
import unittest
from unittest.mock import patch

from talkman_app import TalkManApp
from talkman_global_hotkeys import GLOBAL_ACTIONS
from talkman_shortcuts import DEFAULT_SHORTCUTS, load_shortcuts
from test_talkman_app import FakeClient, FakeVoice


class CursorSettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            probe = tk.Tk()
            probe.withdraw()
            probe.destroy()
        except tk.TclError as error:
            raise unittest.SkipTest("本机 Tcl/Tk 不能初始化：" + str(error))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="TalkMan-cursor-settings-")
        self.root = Path(self.temp.name)
        (self.root / "interface.json").write_text(json.dumps({"magnifier": False, "future_option": 17}), encoding="utf-8")
        permission = patch("talkman_app.prepare_microphone_permission", return_value=True)
        permission.start()
        self.addCleanup(permission.stop)
        self.app = licensed_app(self.root, session_factory=FakeVoice, client_factory=FakeClient,
                              file_session_factory=FakeVoice)
        self.app.after_cancel(self.app.poll_job)
        self.app.poll_job = None
        self.app.update()

    def tearDown(self):
        self.app.close_app()
        self.app = None
        FakeVoice.sessions.clear()
        gc.collect()
        self.temp.cleanup()

    def test_save_pointer_shortcuts_and_keep_other_ui_preferences(self):
        app = self.app
        self.assertTrue(app.wheel_caret_var.get())
        app.open_controls_settings()
        app.update()
        for action, binding in (("focus", "F11"), ("cursor_prev", "F12"), ("cursor_next", "Ctrl+Alt+N")):
            app.shortcut_draft[action] = binding
        app.wheel_caret_draft.set(False)
        app.magnifier_draft.set(True)
        app.controls_save()
        expected = {**DEFAULT_SHORTCUTS, "focus": "F11", "cursor_prev": "F12", "cursor_next": "Control+Alt+N"}
        self.assertEqual(app.shortcuts, expected)
        self.assertEqual(load_shortcuts(self.root), expected)
        self.assertEqual(json.loads(app.ui_path.read_text(encoding="utf-8")),
                         {"magnifier": True, "wheel_caret": False, "future_option": 17})
        self.assertEqual(app.ui_preferences["future_option"], 17)
        self.assertFalse(app.wheel_caret_var.get())
        self.assertTrue(app.magnifier_var.get())

    def test_pointer_capture_rejects_bare_modifier_without_starting_recording(self):
        app = self.app
        app.open_controls_settings()
        app.update()
        entry, label = app.shortcut_fields["focus"]
        entry.focus_force()
        app.update()
        entry.event_generate("<KeyPress>", keysym="Control_L")
        entry.event_generate("<KeyRelease>", keysym="Control_L", state=4)
        app.update()
        self.assertEqual(app.shortcut_draft["focus"], "F6")
        self.assertEqual(label.get(), "F6")
        self.assertIn("单独的修饰键", app.controls_feedback.get())
        self.assertEqual(FakeVoice.sessions, [])

    def test_controls_are_visible_and_window_does_not_grow_on_page_switches(self):
        app = self.app
        app.open_controls_settings()
        window = app.settings_window
        window.update()
        initial_size = (window.winfo_width(), window.winfo_height())
        initial_minimum = window.minsize()
        self.assertEqual(set(app.shortcut_fields), set(DEFAULT_SHORTCUTS)-set(GLOBAL_ACTIONS))
        self.assertEqual(set(app.window_shortcut_draft), set(GLOBAL_ACTIONS))
        for entry, _variable in app.shortcut_fields.values():
            self.assertTrue(entry.winfo_ismapped())
            self.assertGreaterEqual(entry.winfo_rooty(), window.winfo_rooty())
            self.assertLessEqual(entry.winfo_rooty() + entry.winfo_height(),
                                 window.winfo_rooty() + window.winfo_height())
        for _iteration in range(3):
            for page in app.settings_tabs.values():
                app.settings_notebook.select(page)
                window.update()
                self.assertEqual(window.minsize(), initial_minimum)
                self.assertEqual((window.winfo_width(), window.winfo_height()), initial_size)


if __name__ == "__main__":
    unittest.main()
