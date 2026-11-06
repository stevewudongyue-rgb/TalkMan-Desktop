"""Nothing 原生控件的离线交互验收，不访问用户数据。"""
import tkinter as tk
import unittest

from talkman_ui_widgets import DotWordmark, PillButton, ToggleSwitch


class NothingWidgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tk.Tk()
            cls.root.geometry("500x300")
        except tk.TclError as error:
            raise unittest.SkipTest("无法初始化本机 Tk："+str(error))

    @classmethod
    def tearDownClass(cls):
        cls.root.destroy()

    def setUp(self):
        self.calls = []
        self.colors = dict(bg="#000000", accent="#ffffff", inverse="#000000", text="#e8e8e8")
        self.button = PillButton(self.root, "开始输入", command=lambda: self.calls.append("执行"), palette=lambda: self.colors)
        self.button.pack(fill="x")
        self.root.update()

    def tearDown(self):
        for child in self.root.winfo_children():
            child.destroy()
        self.root.update()

    def event(self, sequence, **kwargs):
        self.button.event_generate(sequence, **kwargs)
        self.root.update()

    def test_mouse_activation_requires_release_inside(self):
        self.event("<ButtonPress-1>", x=15, y=15)
        self.event("<ButtonRelease-1>", x=-1, y=15)
        self.assertEqual(self.calls, [])
        self.event("<ButtonPress-1>", x=15, y=15)
        self.event("<ButtonRelease-1>", x=15, y=15)
        self.assertEqual(self.calls, ["执行"])

    def test_disable_during_press_cancels_activation(self):
        self.event("<ButtonPress-1>", x=15, y=15)
        self.button.state(["disabled"])
        self.event("<ButtonRelease-1>", x=15, y=15)
        self.button.invoke()
        self.assertEqual(self.calls, [])
        self.assertTrue(self.button.instate(["disabled", "!pressed"]))
        self.button.configure(state="normal")
        self.button.invoke()
        self.assertEqual(self.calls, ["执行"])

    def test_keyboard_repeats_do_not_repeat_command(self):
        self.button.focus_force()
        self.root.update()
        for _ in range(3):
            self.event("<KeyPress-space>")
        self.assertEqual(self.calls, [])
        self.event("<KeyRelease-space>")
        self.event("<KeyRelease-space>")
        self.assertEqual(self.calls, ["执行"])

    def test_external_voice_bindings_replace_default_command(self):
        self.button.bind("<ButtonPress-1>", lambda _e: self.calls.append("录音"))
        self.button.bind("<ButtonRelease-1>", lambda _e: self.calls.append("停止"))
        self.event("<ButtonPress-1>", x=15, y=15)
        self.event("<ButtonRelease-1>", x=15, y=15)
        self.assertEqual(self.calls, ["录音", "停止"])

    def test_reconfigure_preserves_image_and_resizes_label(self):
        image = tk.PhotoImage(master=self.root, width=20, height=20)
        self.button.configure(text="按住直接覆盖 · 空格", style="Accent.TButton", image=image)
        self.root.update()
        self.assertIs(self.button.cget("image"), image)
        self.assertEqual(self.button.cget("style"), "Accent.TButton")
        self.assertEqual(self.button.itemcget("label", "text"), "按住直接覆盖 · 空格")
        self.assertGreaterEqual(self.button.winfo_height(), 48)
        self.assertTrue(self.button.find_withtag("icon"))
        self.colors.update(bg="#f5f5f5", accent="#000000", inverse="#ffffff")
        self.button.refresh()
        self.assertEqual(self.button.itemcget("label", "fill"), "#ffffff")

    def test_dot_wordmark_recolors_without_text_font(self):
        mark = DotWordmark(self.root, palette=lambda: self.colors)
        mark.pack()
        self.root.update()
        self.assertGreater(len(mark.find_withtag("dot")), 100)
        self.assertEqual(set(mark.type(item) for item in mark.find_all()), {"oval"})
        self.colors["accent"] = "#000000"
        mark.refresh()
        self.assertEqual(mark.itemcget(mark.find_all()[0], "fill"), "#000000")

    def test_toggle_tracks_variable_and_removes_trace_on_destroy(self):
        variable = tk.BooleanVar(master=self.root, value=False)
        switch = ToggleSwitch(self.root, "自动标点", variable, command=lambda: self.calls.append("切换"))
        switch.pack()
        self.root.update()
        off_x = switch.coords("knob")[0]
        switch.invoke()
        self.assertTrue(variable.get())
        self.assertEqual(self.calls, ["切换"])
        self.assertGreater(switch.coords("knob")[0], off_x)
        variable.set(False)
        self.assertEqual(switch.coords("knob")[0], off_x)
        switch.state(["disabled"])
        switch.invoke()
        self.assertFalse(variable.get())
        self.assertEqual(len(variable.trace_info()), 1)
        switch.destroy()
        self.assertEqual(variable.trace_info(), [])
        variable.set(True)


if __name__ == "__main__":
    unittest.main()
