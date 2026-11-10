"""实际 Tk 验证横线、操作光标与原生编辑行为共存。"""
import tkinter as tk
import tkinter.font as tkfont
import unittest
from unittest.mock import patch

from talkman_editor import RuledText


class RuledEditorTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(str(error))
        self.root.geometry("430x350")
        self.text = RuledText(self.root, font=("Arial", 16), wrap="word", padx=16,
                              pady=18, spacing1=4, spacing3=6, exportselection=False)
        self.text.pack(fill="both", expand=True)
        self.root.update()

    def tearDown(self):
        if hasattr(self, "root"):
            self.root.destroy()

    def visible_rules(self):
        positions = []
        index = self.text.index("@0,0 display linestart")
        while True:
            line = self.text.dlineinfo(index)
            if not line:
                break
            next_index = self.text.index(index + " + 1 display lines")
            height = self.text.count(index, next_index, "ypixels")[0]
            bottom = self.text.winfo_height()-1-int(self.text.cget("pady"))
            if 1 <= line[1] + height - 1 < bottom:
                positions.append(line[1] + height - 1)
            if self.text.compare(next_index, "<=", index):
                break
            index = next_index
        return positions

    def test_empty_area_has_rules_and_palette_keeps_visible_border(self):
        text = self.text
        text.set_editor_palette("#ffffff", "#222222", "#999999", "#dddddd", "#d82828")
        self.root.update()
        self.assertEqual(int(text.cget("highlightthickness")), 1)
        self.assertEqual(text.cget("highlightbackground"), "#999999")
        self.assertGreater(len(text._rule_positions), 4)
        self.assertEqual(text._rule_positions[0], self.visible_rules()[0])
        self.assertTrue(all(widget.cget("background") == "#dddddd" for widget in text._rule_widgets))
        self.assertEqual([widget.winfo_y() for widget in text._rule_widgets
                          if widget.winfo_ismapped()], text._rule_positions)

    def test_partially_visible_bottom_row_does_not_squeeze_extra_rule_against_edge(self):
        text = self.text
        text.insert("1.0", "正文 line\n" * 30)
        self.root.update()
        distances = [b-a for a, b in zip(text._rule_positions, text._rule_positions[1:])]
        self.assertTrue(distances)
        self.assertEqual(len(set(distances)), 1)

    def test_wrapped_and_scrolled_rows_align_without_changing_text_or_selection(self):
        text = self.text
        value = ("中文文字与 English text 可以自动换行。" * 8 + "\n") * 7
        text.insert("1.0", value)
        text.tag_add("sel", "1.3", "2.7")
        selection = text.tag_ranges("sel")
        text.yview_scroll(3, "units")
        self.root.update()
        self.assertEqual(text._rule_positions[:len(self.visible_rules())], self.visible_rules())
        self.assertEqual(text.get("1.0", "end-1c"), value)
        self.assertEqual(text.tag_ranges("sel"), selection)

    def test_font_change_recalculates_rule_spacing(self):
        before = self.text._rule_positions[1] - self.text._rule_positions[0]
        self.text.configure(font=("Arial", 24))
        self.root.update()
        after = self.text._rule_positions[1] - self.text._rule_positions[0]
        self.assertGreater(after, before)

    def test_resize_reflows_rules_and_keeps_clipboard_selection_native(self):
        text = self.text
        value = "abcdefghij 中文段落 " * 30
        text.insert("1.0", value)
        text.tag_add("sel", "1.2", "1.8")
        text.event_generate("<<Copy>>")
        self.root.update()
        self.assertEqual(text.clipboard_get(), value[2:8])
        first_next = text.index("1.0 + 1 display lines")
        self.root.geometry("290x420")
        self.root.update()
        self.assertTrue(text.compare(text.index("1.0 + 1 display lines"), "<", first_next))
        self.assertEqual(text._rule_positions[:len(self.visible_rules())], self.visible_rules())
        self.assertEqual(text.clipboard_get(), value[2:8])

    def test_own_modified_binding_and_native_edit_commands_remain_available(self):
        text = self.text
        modified = []
        text.bind("<<Modified>>", lambda _event: modified.append(True))
        text.insert("1.0", "原生文字")
        self.root.update()
        self.assertTrue(modified)
        text.replace("1.0", "1.2", "替换")
        self.root.update()
        self.assertEqual(text.get("1.0", "end-1c"), "替换文字")
        self.assertTrue(text._rule_positions)

    def key(self, keysym):
        self.text.event_generate("<KeyPress>", keysym=keysym)
        self.text.event_generate("<KeyRelease>", keysym=keysym)
        self.root.update()

    def test_typing_and_backspace_without_selection_use_native_class_bindings(self):
        text = self.text
        text.focus_force()
        self.root.update()
        self.assertFalse(text.tag_ranges("sel"))
        self.key("a")
        self.key("b")
        self.assertEqual(text.get("1.0", "end-1c"), "ab")
        self.key("BackSpace")
        self.assertEqual(text.get("1.0", "end-1c"), "a")
        self.key("BackSpace")
        self.key("BackSpace")
        self.assertEqual(text.get("1.0", "end-1c"), "")

    def test_native_paste_and_delete_work_without_selection(self):
        text = self.text
        text.focus_force()
        text.clipboard_clear()
        text.clipboard_append("粘贴 text\n下一行")
        self.root.update()
        text.event_generate("<<Paste>>")
        self.root.update()
        self.assertEqual(text.get("1.0", "end-1c"), "粘贴 text\n下一行")
        self.assertFalse(text.tag_ranges("sel"))
        text.mark_set("insert", "1.0")
        self.key("Delete")
        self.assertEqual(text.get("1.0", "end-1c"), "贴 text\n下一行")

    def test_caught_native_errors_preserve_tcl_options_and_do_not_escape_mainloop(self):
        text = self.text
        def caught(command):
            self.root.tk.call("catch", command, "ruled_result", "ruled_options")
            return (self.root.tk.getvar("ruled_result"),
                    self.root.tk.call("dict", "get", self.root.tk.getvar("ruled_options"), "-errorcode"))
        actual = caught((str(text), "index", "sel.first"))
        original = caught((text._original_command, "index", "sel.first"))
        self.assertEqual(actual, original)
        self.assertIn("sel", actual[0])
        finished = []
        self.root.after(0, lambda: self.root.tk.call("catch", (str(text), "index", "sel.first")))
        self.root.after(10, lambda: (finished.append(True), self.root.quit()))
        self.root.mainloop()
        self.assertTrue(finished)
        with self.assertRaises(tk.TclError):
            text.index("sel.first")

    def test_operation_caret_survives_disabled_editor_and_focus_loss(self):
        text = self.text
        text.insert("1.0", "正在光标处录入")
        text.set_editor_palette("#111111", "#eeeeee", "#555555", "#282828", "#ed3434")
        text.set_operation_caret("1.3")
        text.configure(state="disabled")
        self.root.focus_force()
        self.root.update()
        self.assertTrue(text._caret_widget.winfo_ismapped())
        self.assertEqual(text._caret_widget.cget("background"), "#ed3434")
        self.assertEqual(text._caret_widget.winfo_x(), text.bbox("1.3")[0])
        text.set_operation_caret(None)
        self.root.update()
        self.assertFalse(text._caret_widget.winfo_ismapped())

    def test_repeated_operation_caret_redraw_settles_without_expose_idle_loop(self):
        text = self.text
        text.insert("1.0", "持续录音中的操作光标")
        text.configure(state="disabled")
        text.set_operation_caret("1.3")
        self.root.update()
        with patch.object(text, "_redraw", wraps=text._redraw) as redraw:
            for _ in range(4):
                text._schedule_redraw()
                self.root.update()
            self.assertLessEqual(redraw.call_count, 8)
        self.assertTrue(text._caret_widget.winfo_ismapped())
        self.assertIsNone(text._redraw_job)

    def test_rule_mouse_events_select_native_text_and_reach_app_binding(self):
        text = self.text
        text.insert("1.0", "abcdefghij\nklmnopqrst\nuvwxyz")
        clicks = []
        text.bind("<ButtonPress-1>", lambda event: clicks.append((event.x, event.y)), add=True)
        self.root.update()
        rule = text._rule_widgets[0]
        rule.event_generate("<ButtonPress-1>", x=0, y=0)
        rule.event_generate("<Motion>", x=95, y=0, state=256)
        rule.event_generate("<ButtonRelease-1>", x=95, y=0)
        self.root.update()
        self.assertEqual(len(clicks), 1)
        self.assertTrue(text.tag_ranges("sel"))
        self.assertEqual(text.get("1.0", "end-1c"), "abcdefghij\nklmnopqrst\nuvwxyz")

    def test_auto_fit_shrinks_before_scrolling_then_restores_size_and_preserves_edit_state(self):
        text = self.text
        self.root.geometry('430x365')
        self.root.update()
        text._auto_fit = True
        text.configure(undo=True, autoseparators=False)
        font_size = lambda: tkfont.Font(text, font=text.cget('font')).actual('size')
        found_fit = False
        for lines in range(2, 22):
            text.delete('1.0', 'end')
            text.insert('1.0', '\n'.join(['中文 English 😀'] * lines))
            text.yview_moveto(0)
            self.root.update()
            if font_size() < 16 and text.yview()[1] >= .999:
                found_fit = True
        self.assertTrue(found_fit, '满屏后应存在缩字仍可完整显示的阶段')
        self.assertEqual(font_size(), 14)
        self.assertLess(text.yview()[1], 1)
        text.tag_add('sel', '1.0 + 1 chars', '2.3')
        text.mark_set('insert', '2.3')
        selection, cursor = text.tag_ranges('sel'), text.index('insert')
        value = text.get('1.0', 'end-1c')
        text.edit_separator()
        self.root.geometry('640x900')
        self.root.update()
        self.assertEqual(font_size(), 16)
        self.assertEqual(text.get('1.0', 'end-1c'), value)
        self.assertEqual(text.tag_ranges('sel'), selection)
        self.assertEqual(text.index('insert'), cursor)
        text.insert('end', '新增')
        text.edit_separator()
        self.root.update()
        text.edit_undo()
        self.root.update()
        self.assertEqual(text.get('1.0', 'end-1c'), value)
        text.delete('1.0', 'end')
        text.insert('1.0', '短文')
        self.root.update()
        self.assertEqual(font_size(), 16)
        with patch.object(text, '_fit_content', wraps=text._fit_content) as fit:
            for _ in range(4):
                self.root.update()
            self.assertLessEqual(fit.call_count, 2)
        self.assertIsNone(text._redraw_job)

    def test_auto_fit_pause_keeps_review_font_stable(self):
        text = self.text
        text._auto_fit = True
        text.insert('1.0', '短文')
        self.root.update()
        text.pause_auto_fit(True)
        text.insert('end', '\n较长内容' * 40)
        self.root.update()
        self.assertEqual(tkfont.Font(text, font=text.cget('font')).actual('size'), 16)
        text.pause_auto_fit(False)
        self.root.update()
        self.assertEqual(tkfont.Font(text, font=text.cget('font')).actual('size'), 14)

    def test_long_document_skips_full_layout_fit_and_scroll_does_not_refit(self):
        text = self.text
        text._auto_fit = True
        text.insert('1.0', ('长文编辑与滚动。' * 24 + '\n') * 75)
        self.root.update()
        self.assertEqual(tkfont.Font(text, font=text.cget('font')).actual('size'), 14)
        with patch.object(text, '_fit_content', wraps=text._fit_content) as fit:
            text.yview_moveto(.5)
            self.root.update()
            self.assertLessEqual(fit.call_count, 1)
        self.assertGreater(text.yview()[0], 0)


if __name__ == "__main__":
    unittest.main()
