"""保留原生 Text 编辑行为的横线编辑器与操作光标。"""
from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont


class RuledText(tk.Text):
    """横线按实际显示行绘制；装饰不进入文本、选区或剪贴板。"""

    def __init__(self, master=None, **kwargs):
        self._auto_fit = kwargs.pop('auto_fit', False)
        text_scale = kwargs.pop('text_scale', 1.0)
        self._fit_paused = False
        self._delivery_fit_hold = False
        self._fit_resume_job = None
        self._fit_key = None
        self._fit_revision = 0
        kwargs.update(borderwidth=0, relief="flat", highlightthickness=1)
        # 自动换行之间留出细线的空间，不让横线压到字母下伸部。
        kwargs.setdefault("spacing2", 4)
        super().__init__(master, **kwargs)
        self._fit_font = tkfont.Font(self, font=self.cget('font'))
        self._fit_sizes = tuple(round(size * text_scale) for size in (16, 15, 14))
        self._rule_color = "#242424"
        self._caret_color = "#ef5350"
        self._rule_widgets = []
        self._rule_positions = []
        self._redraw_job = None
        self._editor_destroying = False
        self._operation_caret = None
        self._caret_widget = self._decoration(self._caret_color)
        # Tcl 类绑定中的输入、滚动与选区移动也经过这里；无需占用应用的事件绑定。
        self._original_command = self._w + "_ruled_original"
        self.tk.call("rename", self._w, self._original_command)
        self._notify_command = self._register(self._schedule_redraw)
        self._content_command = self._register(self._content_changed)
        # 原生类绑定会 catch sel.first 等不存在选区的错误；不能让它穿过 Python 回调。
        # Tcl 保留完整 return options，Python 只接收成功操作后的无参数刷新通知。
        dispatch = self.tk.call("format", """
            set code [catch [linsert $args 0 %s] result options]
            if {$code != 0} {return -options $options $result}
            if {[lindex $args 0] in {insert delete replace} ||
                ([lindex $args 0] eq "edit" && [lindex $args 1] in {undo redo})} {%s}
            if {[lindex $args 0] in {insert delete replace configure tag mark yview xview see scan edit}} {
                %s
            }
            return $result
        """, self.tk.call("list", self._original_command), self.tk.call("list", self._content_command),
            self.tk.call("list", self._notify_command))
        self.tk.call("proc", self._w, "args", dispatch)
        # 子装饰的隐藏/显示会产生 Expose；监听它会造成光标重绘的无限 idle 循环。
        for event in ("<Configure>", "<<ThemeChanged>>"):
            self.bind(event, self._schedule_redraw, add=True)
        self.bind("<Destroy>", self._on_destroy, add=True)
        self.set_editor_palette(self.cget("background"), self.cget("foreground"),
                                "#555555", self._rule_color, self._caret_color)

    def set_editor_palette(self, bg, foreground, border, rule, accent):
        self._rule_color, self._caret_color = rule, accent
        self.configure(background=bg, foreground=foreground, highlightthickness=1,
                       highlightbackground=border, highlightcolor=border,
                       insertbackground=accent)
        for widget in self._rule_widgets:
            widget.configure(background=rule)
        self._caret_widget.configure(background=accent)
        self._schedule_redraw()

    def set_operation_caret(self, index=None):
        """设为 None 关闭；否则在指定索引显示不闪烁、不会抢焦点的操作光标。"""
        self._operation_caret = None if index is None else self.index(index)
        self._schedule_redraw()

    def _decoration(self, color):
        widget = tk.Frame(self, background=color, borderwidth=0,
                          highlightthickness=0, takefocus=False, cursor="xterm")
        for sequence in ("<ButtonPress>", "<ButtonRelease>", "<Motion>", "<MouseWheel>",
                         "<Button-4>", "<Button-5>"):
            widget.bind(sequence, lambda event, sequence=sequence: self._forward_pointer(event, sequence))
        return widget

    def _forward_pointer(self, event, sequence):
        """细线与光标只提供外观；点击、拖选和滚轮交回同一 Text。"""
        source = event.widget
        options = dict(x=source.winfo_x() + event.x,
                       y=source.winfo_y() + event.y,
                       state=event.state, time=event.time)
        if sequence in ("<ButtonPress>", "<ButtonRelease>"):
            sequence = sequence[:-1] + "-" + str(event.num) + ">"
        if sequence == "<MouseWheel>":
            options["delta"] = event.delta
        self.event_generate(sequence, **options)
        return "break"

    def _schedule_redraw(self, _event=None):
        if not self._editor_destroying and self._redraw_job is None:
            self._redraw_job = self.after_idle(self._redraw)

    def _content_changed(self):
        self._fit_revision += 1

    def _pixels(self, option):
        return self.winfo_pixels(self.cget(option))

    def pause_auto_fit(self, paused):
        """对照预览期间固定字号，让左右文字保持一致。"""
        self._fit_paused = paused
        self._fit_key = None
        self._schedule_redraw()

    def _fit_content(self):
        if not self._auto_fit or self._fit_paused or self._delivery_fit_hold:
            return
        key = (self.winfo_width(), self.winfo_height(), self._fit_revision)
        if key == self._fit_key:
            return
        self._fit_key = key
        height = self.winfo_height() - 2 * (self._pixels('pady') +
            self._pixels('highlightthickness') + self._pixels('borderwidth'))
        if height <= 0:
            return
        # 超过小窗可容纳的文本量后直接用最小字号；完整排版测量会阻塞主线程。
        count = self.count('1.0', 'end-1c', 'chars')
        if count and count[0] > 12000:
            self._fit_font.configure(size=self._fit_sizes[-1])
            self.tk.call(self._original_command, 'configure', '-font', str(self._fit_font))
            return
        # 在原生控件内精确测量自动折行。临时关闭滚动回调，避免试算字号时闪动滚动条。
        callback = self.cget('yscrollcommand')
        self.tk.call(self._original_command, 'configure', '-yscrollcommand', '')
        try:
            for size in self._fit_sizes:
                self._fit_font.configure(size=size)
                self.tk.call(self._original_command, 'configure', '-font', str(self._fit_font))
                pixels = int(self.tk.call(self._original_command, 'count', '-update', '-ypixels', '1.0', 'end'))
                if pixels <= height:
                    break
        finally:
            self.tk.call(self._original_command, 'configure', '-yscrollcommand', callback)

    def defer_fit_until_interaction(self):
        self._delivery_fit_hold = True
        if self._fit_resume_job is not None:
            self.after_cancel(self._fit_resume_job)
            self._fit_resume_job = None

    def resume_fit_after_interaction(self):
        if not self._delivery_fit_hold:
            return
        if self._fit_resume_job is not None:
            self.after_cancel(self._fit_resume_job)
        def resume():
            self._fit_resume_job = None
            if self.focus_displayof() != self:
                return
            self._delivery_fit_hold = False
            self._fit_key = None
            self._schedule_redraw()
        self._fit_resume_job = self.after(350, resume)

    def _redraw(self):
        self._redraw_job = None
        if self._editor_destroying or not self.winfo_exists():
            return
        width, height = self.winfo_width(), self.winfo_height()
        if width <= 2 or height <= 2:
            return
        self._fit_content()
        inset = self._pixels("highlightthickness") + self._pixels("borderwidth")
        left = inset + self._pixels("padx")
        right = width - inset - self._pixels("padx")
        bottom = height - inset - self._pixels("pady")
        positions = []
        index = self.index("@0,0 display linestart")
        last_bottom = None
        # 遍历显示行，而不是逻辑段落：自动换行和部分滚出的行也各有一条底线。
        while True:
            line = self.dlineinfo(index)
            if line is None:
                break
            _x, y, _w, line_height, _baseline = line
            next_index = self.index(index + " + 1 display lines")
            # dlineinfo 的高度在视口底端会被裁短；使用完整显示行高避免出现挤在底部的假横线。
            line_pixels = self.count(index, next_index, "ypixels")
            if line_pixels:
                line_height = line_pixels[0]
            y_rule = y + line_height - 1
            if inset <= y_rule < bottom:
                positions.append(y_rule)
            last_bottom = y + line_height
            if self.compare(next_index, "<=", index):
                break
            index = next_index
        # 空白区域延续正文的标准行高，避免只有已有文字下面才出现横线。
        font = tkfont.Font(self, font=self.cget("font"))
        step = max(1, font.metrics("linespace") + self._pixels("spacing1") + self._pixels("spacing3"))
        if last_bottom is not None:
            y_rule = last_bottom + step - 1
            while y_rule < bottom:
                if y_rule >= inset:
                    positions.append(y_rule)
                y_rule += step
        self._rule_positions = positions
        while len(self._rule_widgets) < len(positions):
            self._rule_widgets.append(self._decoration(self._rule_color))
        for number, widget in enumerate(self._rule_widgets):
            if number < len(positions) and right > left:
                widget.place(x=left, y=positions[number], width=right-left, height=1,
                             bordermode="ignore")
            else:
                widget.place_forget()
        caret_visible = False
        if self._operation_caret is not None:
            box = self.bbox(self._operation_caret)
            if box:
                x, y, _w, caret_height = box
                top, end = max(inset, y), min(bottom, y + caret_height)
                if inset <= x < width-inset and end > top:
                    self._caret_widget.place(x=x, y=top, width=2, height=end-top,
                                             bordermode="ignore")
                    self._caret_widget.lift()
                    caret_visible = True
        if not caret_visible:
            self._caret_widget.place_forget()

    def _on_destroy(self, event):
        if event.widget != self:
            return
        self._editor_destroying = True
        if self._fit_resume_job is not None:
            self.after_cancel(self._fit_resume_job)
            self._fit_resume_job = None
        if self._redraw_job is not None:
            self.after_cancel(self._redraw_job)
            self._redraw_job = None
        # destroy 会移除原生命令；代理 proc 须单独释放。
        try:
            self.tk.call("rename", self._w, "")
        except tk.TclError:
            pass
