"""原生 Tk 的点阵字标、胶囊按钮和开关；无网络、无额外字体依赖。"""
from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont


_FALLBACK = dict(bg="#000000", text="#e8e8e8", muted="#999999", line="#333333",
                 accent="#ffffff", inverse="#000000", hover="#222222", pressed="#333333")


def _capsule(canvas, box, fill, outline, width=1, tag="shape"):
    """用圆弧描边，避免平滑多边形在胶囊端点留下尖角。"""
    x0, y0, x1, y1 = box
    radius = min((y1-y0)/2, (x1-x0)/2)
    if radius <= 0:
        return
    if fill:
        canvas.create_oval(x0, y0, x0+2*radius, y1, fill=fill, outline="", tags=tag)
        canvas.create_oval(x1-2*radius, y0, x1, y1, fill=fill, outline="", tags=tag)
        canvas.create_rectangle(x0+radius, y0, x1-radius, y1, fill=fill, outline="", tags=tag)
    if outline:
        canvas.create_arc(x0, y0, x0+2*radius, y1, start=90, extent=180,
                          style="arc", outline=outline, width=width, tags=tag)
        canvas.create_arc(x1-2*radius, y0, x1, y1, start=-90, extent=180,
                          style="arc", outline=outline, width=width, tags=tag)
        canvas.create_line(x0+radius, y0, x1-radius, y0, fill=outline, width=width, tags=tag)
        canvas.create_line(x0+radius, y1, x1-radius, y1, fill=outline, width=width, tags=tag)


class DotWordmark(tk.Canvas):
    """品牌字标用真实圆点绘制，不需要安装点阵字体。"""
    _GLYPHS = {
        "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
        "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
        "L": ("10000", "10000", "10000", "10000", "10000", "10000", "11111"),
        "K": ("10001", "10010", "10100", "11000", "10100", "10010", "10001"),
        "M": ("10001", "11011", "10101", "10101", "10001", "10001", "10001"),
        "N": ("10001", "11001", "11001", "10101", "10011", "10011", "10001"),
    }

    def __init__(self, parent, palette=lambda: _FALLBACK, **kw):
        self.palette = palette
        super().__init__(parent, width=kw.pop("width", 270), height=kw.pop("height", 40),
                         highlightthickness=0, borderwidth=0, takefocus=False, **kw)
        self.bind("<Configure>", lambda _event: self.refresh())
        self.refresh()

    def refresh(self):
        colors = {**_FALLBACK, **self.palette()}
        self.configure(bg=colors["bg"])
        self.delete("all")
        height = self.winfo_height() if self.winfo_height() > 1 else int(self.cget("height"))
        width = self.winfo_width() if self.winfo_width() > 1 else int(self.cget("width"))
        if colors.get('rectilinear'):
            family = 'Helvetica Neue' if self.tk.call('tk', 'windowingsystem') == 'aqua' else 'Arial'
            size = max(20, min(48, int(height*.48)))
            if height > 64:
                self.create_text(0, 0, text='TALKMAN', anchor='nw', fill=colors['text'],
                                 font=(family, -size, 'normal'), tags='wordmark')
                self.create_rectangle(0, height-26, 16, height-10, fill=colors['accent'], outline='')
                self.create_text(28, height-8, text='说 · 改 · 填', anchor='sw', fill=colors['muted'],
                                 font=('TkDefaultFont', -12), tags='wordmark')
            else:
                self.create_text(0, height/2, text='TALKMAN', anchor='w', fill=colors['text'],
                                 font=(family, -size, 'normal'), tags='wordmark')
                self.create_rectangle(width-20, height/2-6, width-8, height/2+6, fill=colors['accent'], outline='')
            return
        # TALKMAN 共 41 列；保持字标左对齐。
        step = min(5.4, (height-4)/7, (width-4)/41)
        diameter = max(1.0, step*0.64)
        ystart = (height-(6*step+diameter))/2
        column = 0
        for letter in "TALKMAN":
            for row, bits in enumerate(self._GLYPHS[letter]):
                for col, active in enumerate(bits):
                    if active == "1":
                        x, y = 2+(column+col)*step, ystart+row*step
                        self.create_oval(x, y, x+diameter, y+diameter,
                                         fill=colors["accent"], outline="", tags="dot")
            column += 6


class PillButton(tk.Canvas):
    """保留应用使用的 ttk.Button 接口，并让按住录音可覆盖事件绑定。"""
    def __init__(self, parent, text="", command=None, image=None, style="TButton",
                 palette=lambda: _FALLBACK, font=("TkDefaultFont", 11), **kw):
        self.palette = palette
        self._text, self._command, self._image = text, command, image
        self._style, self._font = style, font
        self._states = set()
        self._hover = self._focused = self._mouse_armed = False
        self._key_armed = None
        self._width_units = kw.pop("width", None)
        self._minimum_height = max(48, int(kw.pop("height", 48)))
        self._padding = kw.pop("padding", 16)
        kw.pop("compound", None)
        initial_state = kw.pop("state", "normal")
        super().__init__(parent, height=self._minimum_height, highlightthickness=0,
                         borderwidth=0, takefocus=kw.pop("takefocus", True), **kw)
        self.bind("<Configure>", lambda _event: self.refresh())
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)
        self.bind("<FocusIn>", self._focus_in)
        self.bind("<FocusOut>", self._focus_out)
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<ButtonRelease-1>", self._release)
        for key in ("space", "Return", "KP_Enter"):
            self.bind(f"<KeyPress-{key}>", self._key_press)
            self.bind(f"<KeyRelease-{key}>", self._key_release)
        self._resize_request()
        self.configure(state=initial_state)

    def _font_object(self):
        return tkfont.Font(root=self, font=self._font)

    def _image_size(self):
        if not self._image:
            return 0, 0
        try:
            return int(self.tk.call("image", "width", self._image)), int(self.tk.call("image", "height", self._image))
        except tk.TclError:
            return 0, 0

    def _resize_request(self):
        font = self._font_object()
        iw, _ = self._image_size()
        content = font.measure(self._text) + iw + (8 if iw and self._text else 0)
        if self._width_units is not None:
            content = max(content, abs(int(self._width_units))*font.measure("0"))
        super().configure(width=max(48, content+36), height=self._minimum_height)

    def configure(self, cnf=None, **kw):
        if isinstance(cnf, str) and not kw:
            if cnf in ("text", "style", "image", "command", "font", "state"):
                return (cnf, cnf, cnf.title(), "", self.cget(cnf))
            return super().configure(cnf)
        if cnf:
            kw = {**cnf, **kw}
        if not kw:
            return super().configure()
        resized = False
        for option, attr in (("text", "_text"), ("style", "_style"), ("image", "_image"),
                             ("command", "_command"), ("font", "_font"), ("width", "_width_units")):
            if option in kw:
                setattr(self, attr, kw.pop(option))
                resized = resized or option in ("text", "image", "font", "width")
        if "height" in kw:
            self._minimum_height = max(48, int(kw.pop("height")))
            resized = True
        if "state" in kw:
            value = kw.pop("state")
            self.state(["disabled"] if value == "disabled" else ["!disabled"])
        self._padding = kw.pop("padding", self._padding)
        kw.pop("compound", None)
        if kw:
            super().configure(**kw)
        if resized:
            self._resize_request()
        self.refresh()

    config = configure

    def cget(self, key):
        options = {"text": self._text, "style": self._style, "image": self._image,
                   "command": self._command, "font": self._font,
                   "state": "disabled" if "disabled" in self._states else "normal"}
        return options[key] if key in options else super().cget(key)

    def state(self, statespec=None):
        if statespec is None:
            return tuple(sorted(self._states))
        before = self._states.copy()
        for name in self.tk.splitlist(statespec) if isinstance(statespec, str) else statespec:
            if name.startswith("!"):
                self._states.discard(name[1:])
            else:
                self._states.add(name)
        if "disabled" in self._states:
            self._mouse_armed, self._key_armed = False, None
            self._states.discard("pressed")
        self.refresh()
        return tuple(name if name in before else "!"+name for name in sorted(before ^ self._states))

    def instate(self, statespec, callback=None, *args):
        if isinstance(statespec, str):
            statespec = self.tk.splitlist(statespec)
        result = all((name[1:] not in self._states) if name.startswith("!") else (name in self._states)
                     for name in statespec)
        if result and callback:
            return callback(*args)
        return result

    def invoke(self):
        if not self.instate(["disabled"]) and self._command:
            return self._command()

    def _enter(self, _event):
        self._hover = True
        self.refresh()

    def _leave(self, _event):
        self._hover = False
        self.refresh()

    def _focus_in(self, _event):
        self._focused = True
        self.refresh()

    def _focus_out(self, _event):
        self._focused = False
        self._key_armed = None
        self._states.discard("pressed")
        self.refresh()

    def _press(self, _event):
        if not self.instate(["disabled"]):
            self.focus_set()
            self._mouse_armed = True
            self.state(["pressed"])
        return "break"

    def _release(self, event):
        activate = self._mouse_armed and 0 <= event.x < self.winfo_width() and 0 <= event.y < self.winfo_height()
        self._mouse_armed = False
        self.state(["!pressed"])
        if activate:
            self.invoke()
        return "break"

    def _key_press(self, event):
        if not self.instate(["disabled"]) and self._key_armed is None:
            self._key_armed = event.keysym
            self.state(["pressed"])
        return "break"

    def _key_release(self, event):
        activate = self._key_armed == event.keysym
        self._key_armed = None
        self.state(["!pressed"])
        if activate:
            self.invoke()
        return "break"

    def refresh(self):
        if not self.winfo_exists():
            return
        colors = {**_FALLBACK, **self.palette()}
        super().configure(bg=colors["bg"], cursor="" if self.instate(["disabled"]) else "hand2")
        self.delete("all")
        width = self.winfo_width() if self.winfo_width() > 1 else int(super().cget("width"))
        height = self.winfo_height() if self.winfo_height() > 1 else self._minimum_height
        disabled = self.instate(["disabled"])
        recording = self._style == "Recording.TButton"
        primary = self._style == "Accent.TButton"
        fill = "#d71921" if recording else colors["accent"] if primary else colors["bg"]
        foreground = "#ffffff" if recording else colors["inverse"] if primary else colors["text"]
        outline = fill if primary or recording else colors["line"]
        if not disabled and recording and (self._hover or self.instate(["pressed"])):
            fill = "#b31219" if self.instate(["pressed"]) else "#c6161e"
            outline = fill
        elif not disabled and primary and (self._hover or self.instate(["pressed"])):
            fill = colors["muted"] if self.instate(["pressed"]) else colors["text"]
            outline = fill
        if not disabled and not (primary or recording):
            if self.instate(["pressed"]):
                fill = colors["pressed"]
            elif self._hover:
                fill = colors["hover"]
                outline = colors["muted"]
        if disabled:
            fill, foreground, outline = colors["bg"], colors["muted"], colors["line"]
        def shape(box, color, border, tag='shape'):
            if colors.get('rectilinear'):
                self.create_rectangle(*box, fill=color, outline=border, tags=tag)
            else:
                _capsule(self, box, color, border, tag=tag)
        shape((3, 3, width-3, height-3), fill, outline)
        if self._focused and not disabled:
            shape((1, 1, width-1, height-1), "", colors["accent"], tag="focus")
        if colors.get('rectilinear') and getattr(self, 'section_number', None):
            number = self.section_number
            self.create_text(17, height/2, text=number, anchor='w', fill=foreground,
                             font=('Arial', -min(30, int(height*.48))), tags='section')
            self.create_line(62, 13, 62, height-13, fill=foreground, tags='section')
            self.create_text(78, height/2, text=self._text, anchor='w', fill=foreground,
                             font=self._font, tags='label')
            return
        font = self._font_object()
        iw, _ = self._image_size()
        text_width = font.measure(self._text)
        gap = 8 if iw and self._text else 0
        x = (width-(iw+gap+text_width))/2
        if iw:
            self.create_image(x+iw/2, height/2, image=self._image, tags="icon")
            x += iw+gap
        if self._text:
            self.create_text(x, height/2, text=self._text, anchor="w", fill=foreground,
                             font=self._font, tags="label")


class ToggleSwitch(PillButton):
    """整个标签均可点击，选中采用单色开关，不使用额外强调色。"""
    def __init__(self, parent, text, variable, command=None, palette=lambda: _FALLBACK,
                 font=("TkDefaultFont", 11), **kw):
        self.variable = variable
        self._toggle_command = command
        self._variable_trace = None
        super().__init__(parent, text=text, command=self._toggle, palette=palette, font=font, **kw)
        self._variable_trace = variable.trace_add("write", lambda *_args: self.refresh())

    def _resize_request(self):
        width = self._font_object().measure(self._text)+72
        tk.Canvas.configure(self, width=max(88, width), height=self._minimum_height)

    def _toggle(self):
        self.variable.set(not self.variable.get())
        if self._toggle_command:
            self._toggle_command()

    def refresh(self):
        if not self.winfo_exists():
            return
        colors = {**_FALLBACK, **self.palette()}
        tk.Canvas.configure(self, bg=colors["bg"], cursor="" if self.instate(["disabled"]) else "hand2")
        self.delete("all")
        height = self.winfo_height() if self.winfo_height() > 1 else self._minimum_height
        disabled = self.instate(["disabled"])
        on = bool(self.variable.get())
        fill = colors["line"] if disabled else colors["accent"] if on else colors["hover"]
        border = colors["muted"] if self._hover and not disabled else colors["line"]
        _capsule(self, (3, height/2-12, 47, height/2+12), fill, fill if on else border)
        knob = colors["muted"] if disabled else colors["inverse"] if on else colors["text"]
        center = 35 if on else 15
        self.create_oval(center-8, height/2-8, center+8, height/2+8,
                         fill=knob, outline="", tags="knob")
        self.create_text(60, height/2, text=self._text, anchor="w", font=self._font,
                         fill=colors["muted"] if disabled else colors["text"], tags="label")
        if self._focused and not disabled:
            _capsule(self, (1, height/2-14, 49, height/2+14), "", colors["accent"], tag="focus")

    def destroy(self):
        if self._variable_trace:
            self.variable.trace_remove("write", self._variable_trace)
            self._variable_trace = None
        super().destroy()
