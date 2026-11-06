"""验证真实 Tk 文字放大镜不干扰正文、焦点和选区。"""
import tkinter as tk
import unittest
from types import SimpleNamespace

from talkman_magnifier import TextMagnifier
from talkman_editor import RuledText


class MagnifierTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            probe = tk.Tk()
            probe.withdraw()
            probe.destroy()
        except tk.TclError as ex:
            raise unittest.SkipTest("本机 Tcl/Tk 不能初始化：" + str(ex))

    def setUp(self):
        self.root = tk.Tk()
        self.root.geometry("540x280+80+80")
        self.text = RuledText(self.root, font=("Arial", 14), wrap="word", exportselection=False,
                              padx=16, pady=18, spacing1=4, spacing3=6)
        self.text.pack(fill="both", expand=True)
        self.text.insert("1.0", "第一段：😀苹果 和香蕉，第二段。\n第二行文字。")
        self.text.tag_add("sel", "1.0 + 5 chars", "1.0 + 7 chars")
        self.text.mark_set("insert", "1.0 + 7 chars")
        self.enabled = True
        self.allowed = True
        self.lens = TextMagnifier(self.text, lambda: self.enabled, lambda: self.allowed,
                                  lambda: "Arial", lambda: {"input": "#111111", "text": "#eeeeee", "line": "#333333"})
        self.root.update()
        self.text.focus_force()
        self.root.update()

    def tearDown(self):
        self.lens.destroy()
        self.root.destroy()

    def hover(self, index="1.0 + 4 chars", *, root_x=None, root_y=None):
        left, top, width, height = self.text.bbox(index)
        event = SimpleNamespace(x=left + 1, y=top + height // 2,
                                x_root=self.text.winfo_rootx() + left if root_x is None else root_x,
                                y_root=self.text.winfo_rooty() + top if root_y is None else root_y,
                                state=0)
        self.lens._motion(event)
        self.root.after(85, self.root.quit)
        self.root.mainloop()
        self.root.update_idletasks()

    def visible(self):
        return bool(self.lens.window and self.lens.window.winfo_viewable())

    def test_unicode_preview_preserves_document_selection_cursor_and_focus(self):
        before = (self.text.get("1.0", "end-1c"), self.text.index("insert"),
                  tuple(map(str, self.text.tag_ranges("sel"))), self.root.focus_get(),
                  self.text.cget("cursor"))
        self.hover()
        self.assertTrue(self.visible())
        items = self.lens.canvas.find_all()
        content = next(self.lens.canvas.itemcget(item, "text") for item in items
                       if self.lens.canvas.type(item) == "text")
        self.assertIn("😀", content)
        self.assertIn("苹果", content)
        self.assertTrue(any(self.lens.canvas.type(item) == "line" for item in items))
        after = (self.text.get("1.0", "end-1c"), self.text.index("insert"),
                 tuple(map(str, self.text.tag_ranges("sel"))), self.root.focus_get(),
                 self.text.cget("cursor"))
        self.assertEqual(before, after)
        self.assertIsNone(self.root.grab_current())

    def test_click_suppresses_until_leave_and_keeps_existing_bindings(self):
        clicks = []
        self.text.bind("<ButtonPress-1>", lambda event: clicks.append(True), add=True)
        self.hover()
        self.text.event_generate("<ButtonPress-1>", x=10, y=10)
        self.root.update()
        self.assertFalse(self.visible())
        self.assertEqual(clicks, [True])
        self.hover()
        self.assertFalse(self.visible())
        self.text.event_generate("<Leave>")
        self.hover()
        self.assertTrue(self.visible())

    def test_disabled_or_busy_hides_and_cancels_pending_preview(self):
        self.hover()
        self.enabled = False
        self.hover()
        self.assertFalse(self.visible())
        self.enabled = True
        self.allowed = False
        self.hover()
        self.assertFalse(self.visible())
        self.assertIsNone(self.lens._job)

    def test_lens_fits_screen_near_bottom_right_and_top_left(self):
        self.hover(root_x=self.text.winfo_screenwidth() - 1,
                   root_y=self.text.winfo_screenheight() - 1)
        window = self.lens.window
        self.assertGreaterEqual(window.winfo_x(), self.text.winfo_vrootx())
        self.assertGreaterEqual(window.winfo_y(), self.text.winfo_vrooty())
        self.assertLessEqual(window.winfo_x() + window.winfo_width(),
                             self.text.winfo_vrootx() + self.text.winfo_vrootwidth())
        self.assertLessEqual(window.winfo_y() + window.winfo_height(),
                             self.text.winfo_vrooty() + self.text.winfo_vrootheight())
        self.hover(root_x=0, root_y=0)
        self.assertGreaterEqual(window.winfo_y(), self.text.winfo_vrooty())

    def test_mouse_drag_keyboard_and_focus_loss_hide(self):
        self.hover()
        self.lens._motion(SimpleNamespace(state=0x100))
        self.assertFalse(self.visible())
        self.hover()
        self.text.bind("<KeyPress>", lambda event: "break")
        self.text.event_generate("<KeyPress>", keysym="Left")
        self.root.update()
        self.assertFalse(self.visible())
        self.hover()
        self.text.event_generate("<FocusOut>")
        self.root.update()
        self.assertFalse(self.visible())

    def test_empty_display_line_has_no_lens(self):
        self.text.delete("1.0", "end")
        self.text.insert("1.0", "\n")
        self.root.update()
        self.hover("1.0")
        self.assertFalse(self.visible())

    def test_full_display_rows_have_no_dead_stripes_before_and_after_scroll(self):
        self.text.delete("1.0", "end")
        value = ("放大镜应覆盖中文、English、😀及自动换行的行间区域。" * 8 + "\n") * 4
        self.text.insert("1.0", value)
        self.text.tag_add("sel", "1.3", "1.10")
        self.text.mark_set("insert", "1.10")
        for scroll in (0, 7):
            self.text.yview_scroll(scroll, "units")
            self.root.update()
            index = self.text.index("@0,0 display linestart")
            checked = 0
            while (row := self.text.dlineinfo(index)):
                box = self.text.bbox(index)
                if box:
                    for y in range(max(1, row[1]), row[1] + row[3]):
                        self.assertIsNotNone(self.lens._excerpt(box[0] + 2, y),
                                             (scroll, index, y, row, box))
                    checked += 1
                next_index = self.text.index(index + " + 1 display lines")
                if self.text.compare(next_index, "<=", index):
                    break
                index = next_index
            self.assertGreater(checked, 2)
        self.assertEqual(self.text.get("1.0", "end-1c"), value)
        self.assertEqual(self.text.index("insert"), "1.10")
        self.assertEqual(tuple(map(str, self.text.tag_ranges("sel"))), ("1.3", "1.10"))

    def test_line_end_whitespace_previews_nearest_text_but_empty_area_does_not(self):
        self.text.delete("1.0", "end")
        self.text.insert("1.0", "一行文字\n\n下一段")
        self.root.update()
        row = self.text.dlineinfo("1.0")
        self.assertEqual("".join(self.lens._excerpt(self.text.winfo_width() - 20, row[1])), "一行文字")
        blank = self.text.dlineinfo("2.0")
        self.assertIsNone(self.lens._excerpt(40, blank[1] + blank[3] // 2))
        self.assertIsNone(self.lens._excerpt(40, self.text.winfo_height() - 2))

    def test_hover_on_rule_decoration_shows_lens_without_changing_selection(self):
        self.hover()
        self.lens.hide()
        selection = self.text.tag_ranges("sel")
        rule = self.text._rule_widgets[0]
        rule.event_generate("<Motion>", x=40, y=0)
        self.root.after(85, self.root.quit)
        self.root.mainloop()
        self.assertTrue(self.visible())
        self.assertEqual(self.text.tag_ranges("sel"), selection)

    def test_destroy_releases_callbacks_without_removing_other_bindings(self):
        other = self.text.bind("<Motion>", lambda event: None, add=True)
        self.hover()
        self.lens.destroy()
        self.assertIn(other, self.text.bind("<Motion>"))
        self.assertIsNone(self.lens.window)
        self.assertIsNone(self.lens._job)
        self.lens.destroy()

    def test_text_destruction_closes_lens_and_pending_callback(self):
        self.hover()
        self.lens._motion(SimpleNamespace(x=5, y=5, x_root=85, y_root=85, state=0))
        self.assertIsNotNone(self.lens._job)
        self.text.destroy()
        self.root.update()
        self.assertIsNone(self.lens.window)
        self.assertIsNone(self.lens._job)
        self.assertTrue(self.lens._closed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
