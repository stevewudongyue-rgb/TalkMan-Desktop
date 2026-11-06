"""文字悬停放大镜；只读取编辑区，不改写正文、光标或选区。"""
from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from typing import Callable, Mapping


class TextMagnifier:
    def __init__(
        self,
        text: tk.Text,
        enabled: Callable[[], bool],
        allowed: Callable[[], bool],
        font_family: Callable[[], str],
        colors: Callable[[], Mapping[str, str]],
    ):
        self.text = text
        self.enabled = enabled
        self.allowed = allowed
        self.font_family = font_family
        self.colors = colors
        self.window: tk.Toplevel | None = None
        self.canvas: tk.Canvas | None = None
        self._font: tkfont.Font | None = None
        self._job: str | None = None
        self._position: tuple[int, int, int, int] | None = None
        self._suppressed = False
        self._closed = False
        self._bindings = []
        # 先于编辑器自己的快捷键执行；其返回 break 也不会让镜片滞留。
        self._bindtag = f"TalkManMagnifier{id(self)}"
        text.bindtags((self._bindtag,) + text.bindtags())
        for sequence, callback in (
            ("<Motion>", self._motion),
            ("<Leave>", self._leave),
            ("<ButtonPress-1>", self._click),
            ("<KeyPress>", self._activity),
            ("<FocusOut>", self._activity),
            ("<Destroy>", self._text_destroyed),
        ):
            self._bindings.append((sequence, text.bind_class(self._bindtag, sequence, callback)))

    def _usable(self) -> bool:
        if self._closed or self._suppressed:
            return False
        try:
            return bool(self.text.winfo_exists() and self.enabled() and self.allowed())
        except tk.TclError:
            return False

    def _motion(self, event):
        if event.state & 0x100 or not self._usable():
            self.hide()
            return
        self._position = (event.x, event.y, event.x_root, event.y_root)
        if not self._job:
            self._job = self.text.after(60, self._show_pending)

    def _leave(self, _event=None):
        self._suppressed = False
        self.hide()

    def _click(self, _event=None):
        self._suppressed = True
        self.hide()

    def _activity(self, _event=None):
        self.hide()

    def _text_destroyed(self, event):
        if event.widget == self.text:
            self.destroy()

    def _excerpt(self, x: int, y: int) -> tuple[str, str] | None:
        if not (0 <= x < self.text.winfo_width() and 0 <= y < self.text.winfo_height()):
            return None
        index = self.text.index(f"@{x},{y}")
        line_start = self.text.index(f"{index} display linestart")
        line_end = self.text.index(f"{index} display lineend")
        # bbox 只有字形高度，漏掉 spacing1/2/3 与横线，导致逐行出现失效条带。
        # 命中范围使用完整显示行；行尾留白也预览最近的字间位置。
        row = self.text.dlineinfo(line_start)
        if not row or not row[1] <= y < row[1] + row[3]:
            return None
        if self.text.compare(line_start, "==", line_end):
            return None
        box = self.text.bbox(index)
        if not box:
            return None
        left, _top, width, _height = box
        boundary = index
        if x >= left + width / 2 and self.text.compare(index, "<", line_end):
            boundary = self.text.index(f"{index} + 1 chars")
        start = self.text.index(f"{boundary} - 5 chars")
        end = self.text.index(f"{boundary} + 5 chars")
        if self.text.compare(start, "<", line_start):
            start = line_start
        if self.text.compare(end, ">", line_end):
            end = line_end
        # Tk 的字符索引可跨 emoji，避免用 UTF-16 列号截断文字。
        prefix = self.text.get(start, boundary).replace("\t", "    ")
        suffix = self.text.get(boundary, end).replace("\t", "    ")
        return prefix, suffix

    def _show_pending(self):
        self._job = None
        if not self._usable() or not self._position:
            self.hide()
            return
        try:
            x, y, root_x, root_y = self._position
            excerpt = self._excerpt(x, y)
            if not excerpt or not (excerpt[0] + excerpt[1]).strip():
                self.hide()
                return
            prefix, suffix = excerpt
            if not self.window:
                self.window = tk.Toplevel(self.text, takefocus=False)
                self.window.withdraw()
                self.window.overrideredirect(True)
                self.window.transient(self.text.winfo_toplevel())
                self.canvas = tk.Canvas(self.window, takefocus=False, borderwidth=0,
                                        highlightthickness=1)
                self.canvas.pack(fill="both", expand=True)
                self._font = tkfont.Font(root=self.text, size=22)
            self._font.configure(family=self.font_family())
            palette = self.colors()
            background = palette.get("input", "#111111")
            foreground = palette.get("text", "#e8e8e8")
            border = palette.get("line", "#333333")
            self.window.configure(bg=border)
            self.canvas.configure(bg=background, highlightbackground=border)
            width = self._font.measure(prefix + suffix) + 28
            height = self._font.metrics("linespace") + 22
            screen_x = self.text.winfo_vrootx()
            screen_y = self.text.winfo_vrooty()
            screen_width = self.text.winfo_vrootwidth()
            screen_height = self.text.winfo_vrootheight()
            width = max(48, min(width, screen_width - 16))
            height = max(32, min(height, screen_height - 16))
            window_x = min(max(root_x + 18, screen_x + 8), screen_x + screen_width - width - 8)
            window_y = root_y - height - 18
            if window_y < screen_y + 8:
                window_y = root_y + 22
            window_y = min(max(window_y, screen_y + 8), screen_y + screen_height - height - 8)
            self.window.geometry(f"{width}x{height}+{window_x}+{window_y}")
            self.canvas.delete("all")
            self.canvas.create_text(14, 11, text=prefix + suffix, font=self._font,
                                    fill=foreground, anchor="nw")
            boundary_x = 14 + self._font.measure(prefix)
            self.canvas.create_line(boundary_x, 7, boundary_x, height - 7,
                                    fill="#d71921", width=2)
            self.window.deiconify()
            self.window.lift()
        except tk.TclError:
            self.hide()

    def hide(self):
        if self._job:
            try:
                self.text.after_cancel(self._job)
            except tk.TclError:
                pass
            self._job = None
        self._position = None
        if self.window:
            try:
                self.window.withdraw()
            except tk.TclError:
                pass

    def destroy(self):
        if self._closed:
            return
        self._closed = True
        self.hide()
        try:
            self.text.bindtags(tuple(tag for tag in self.text.bindtags() if tag != self._bindtag))
        except tk.TclError:
            pass
        for sequence, binding in self._bindings:
            try:
                self.text.unbind_class(self._bindtag, sequence)
                self.text._root().deletecommand(binding)
            except tk.TclError:
                pass
        self._bindings.clear()
        if self.window:
            try:
                self.window.destroy()
            except tk.TclError:
                pass
        self.window = self.canvas = self._font = None
