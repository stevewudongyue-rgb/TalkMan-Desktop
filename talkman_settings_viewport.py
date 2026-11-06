"""小屏幕设置视口：超出工作区才滚动，页签及控件仍使用原生 Tk。"""
import sys
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk


class SettingsViewport(ttk.Frame):
    def __init__(self, parent, window, palette, compact_navigation=False):
        super().__init__(parent)
        self.compact_navigation = compact_navigation
        self.canvas_row = 1
        self.navigation_row = ttk.Frame(self)
        self.navigation_row.grid(row=0, column=0, columnspan=2, sticky='ew', pady=(0, 14))
        ttk.Label(self.navigation_row, text='设置项目', style='Muted.TLabel').pack(side='left', padx=(0, 16))
        self.navigation = ttk.Combobox(self.navigation_row, state='readonly', width=24)
        self.navigation.pack(side='left', fill='x', expand=True)
        self.navigation.bind('<<ComboboxSelected>>', self.choose_page)
        ttk.Style(self).layout('Compact.Settings.TNotebook.Tab', [])
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0,
                                bg=palette['bg'], width=600, height=540)
        self.canvas.grid(row=self.canvas_row, column=0, sticky='nsew')
        self.columnconfigure(0, weight=1)
        self.rowconfigure(self.canvas_row, weight=1)
        self.vertical = ttk.Scrollbar(self, command=self.canvas.yview)
        self.horizontal = ttk.Scrollbar(self, orient='horizontal', command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=lambda a,b: self.scrollbar(self.vertical, a,b,1,self.canvas_row,'ns'))
        self.notebook = ttk.Notebook(self.canvas, style=('Compact.Settings.TNotebook' if compact_navigation else 'Settings.TNotebook'))
        self.item = self.canvas.create_window(0, 0, window=self.notebook, anchor='nw')
        self._extent = None
        self.canvas.bind('<Configure>', self.layout)
        self.notebook.bind('<Configure>', self.layout, add=True)
        self.notebook.bind('<<NotebookTabChanged>>', self.tab_changed, add=True)
        window.bind('<MouseWheel>', self.wheel, add=True)
        window.bind('<Button-4>', self.wheel, add=True)
        window.bind('<Button-5>', self.wheel, add=True)
        window.bind('<FocusIn>', self.see_focus, add=True)

    def scrollbar(self, bar, first, last, column, row, sticky):
        bar.set(first, last)
        needed = not (float(first) <= 0 and float(last) >= 1)
        shown = bar.winfo_manager() == 'grid'
        if not needed and shown:
            bar.grid_remove()
        elif needed and not shown:
            bar.grid(row=row, column=column, sticky=sticky)

    def layout(self, _event=None):
        selected = self.notebook.select()
        available = max(1, self.canvas.winfo_width())
        style = ttk.Style(self)
        font = tkfont.Font(self, font=style.lookup('TNotebook.Tab', 'font') or 'TkDefaultFont')
        tab_width = sum(font.measure(self.notebook.tab(tab, 'text'))+24 for tab in self.notebook.tabs())
        compact = self.compact_navigation or tab_width > available
        desired_style = 'Compact.Settings.TNotebook' if compact else 'Settings.TNotebook'
        if self.notebook.cget('style') != desired_style:
            self.notebook.configure(style=desired_style)
        if compact:
            self.navigation_row.grid()
        else:
            self.navigation_row.grid_remove()
        if self.navigation is not None:
            tabs = self.notebook.tabs()
            labels = tuple(self.notebook.tab(tab, 'text') for tab in tabs)
            if tuple(self.navigation.cget('values')) != labels:
                self.navigation.configure(values=labels)
            if selected:
                self.navigation.current(tabs.index(selected))
        if selected:
            page = self.notebook.nametowidget(selected)
            self.reflow(page)
            # Notebook 默认按最高的所有页定高，会给短页面留下大块空白，
            # 并把关闭/保存推到不必要的滚动区。只使用当前页的内容高度。
            needed = page.winfo_reqheight()
            if int(self.notebook.cget('height') or 0) != needed:
                self.notebook.configure(height=needed)
            if int(self.notebook.cget('width') or 0) != available:
                self.notebook.configure(width=available)
        width = available
        height = max(self.canvas.winfo_height(), self.notebook.winfo_reqheight())
        # Mac 对重复几何配置也会派发事件；只在范围变化时刷新，避免回调循环。
        if self._extent != (width, height):
            self._extent = (width, height)
            self.canvas.itemconfigure(self.item, width=width, height=height)
            self.canvas.configure(scrollregion=(0, 0, width, height))
        self.canvas.xview_moveto(0)

    def reflow(self, widget):
        """按可用宽度换行；窄窗口内将并排表单行改为上下排列。"""
        width = widget.winfo_width()
        if width < 30:
            return
        children = widget.winfo_children()
        if isinstance(widget, ttk.Frame):
            packed = list(widget.pack_slaves())
            original = getattr(widget, '_responsive_pack', None)
            if original is None and len(packed) > 1 and all(c.pack_info().get('side') in ('left','right') for c in packed):
                original = [(child, child.pack_info()) for child in packed]
                widget._responsive_pack = original
                widget._responsive_width = sum(child.winfo_reqwidth()+16 for child in packed)
            if original:
                narrow = width < widget._responsive_width
                if narrow != getattr(widget, '_responsive_stacked', None):
                    widget._responsive_stacked = narrow
                    for child, options in original:
                        child.pack_forget()
                    for child, options in original:
                        child.pack(side='top', fill='x', expand=False, anchor='w', pady=4) if narrow else child.pack(**options)
            gridded = list(widget.grid_slaves())
            grid_original = getattr(widget, '_responsive_grid', None)
            if grid_original is None and len(gridded)>1 and all(isinstance(child, (ttk.Frame, ttk.Checkbutton)) for child in gridded):
                columns = {int(child.grid_info()['column']) for child in gridded}
                if columns == {0,1}:
                    grid_original = sorted([(child, child.grid_info()) for child in gridded], key=lambda pair:(int(pair[1]['row']),int(pair[1]['column'])))
                    widget._responsive_grid = grid_original
                    widget._responsive_grid_width = sum(max(child.winfo_reqwidth() for child in gridded if int(child.grid_info()['column'])==col)+24 for col in columns)
            if grid_original:
                narrow = width < widget._responsive_grid_width
                if narrow != getattr(widget, '_responsive_grid_stacked', None):
                    widget._responsive_grid_stacked = narrow
                    for child, options in grid_original:
                        child.grid_forget()
                    for index, (child, options) in enumerate(grid_original):
                        child.grid(row=index,column=0,columnspan=2,sticky='ew',pady=(0,12)) if narrow else child.grid(**options)
        for child in children:
            if isinstance(child, ttk.Label) and float(child.cget('wraplength') or 0):
                limit = getattr(child, '_wrap_limit', None)
                if limit is None:
                    child._wrap_limit = limit = int(float(child.cget('wraplength')))
                maximum = max(80, min(limit, width-32))
                if int(float(child.cget('wraplength'))) != maximum:
                    child.configure(wraplength=maximum)
            if isinstance(child, (ttk.Checkbutton, ttk.Radiobutton)) and not child.cget('textvariable'):
                original = getattr(child, '_original_text', str(child.cget('text')))
                child._original_text = original
                font = tkfont.Font(child, font=ttk.Style(child).lookup(child.cget('style') or child.winfo_class(), 'font') or 'TkDefaultFont')
                maximum = max(100, width-48)
                lines, line = [], ''
                for char in original:
                    if char=='\n' or (line and font.measure(line+char)>maximum):
                        lines.append(line);line=''
                    if char!='\n':line+=char
                lines.append(line)
                text = '\n'.join(lines)
                if str(child.cget('text')) != text:
                    child.configure(text=text)
            self.reflow(child)

    def choose_page(self, _event=None):
        index = self.navigation.current()
        if index >= 0:
            self.notebook.select(index)

    def tab_changed(self, _event=None):
        self.canvas.yview_moveto(0)
        self.layout()

    def wheel(self, event):
        if event.widget.winfo_class() in ('Text', 'Treeview', 'TCombobox'):
            return
        if self.canvas.yview() == (0.0, 1.0):
            return
        number = getattr(event, 'num', None)
        raw = 120 if number == 4 else -120 if number == 5 else int(getattr(event, 'delta', 0))
        if raw:
            step = max(-3, min(3, -raw)) if sys.platform == 'darwin' else -max(1, abs(raw)//120)*(1 if raw>0 else -1)
            self.canvas.yview_scroll(step, 'units')
            return 'break'

    def see_focus(self, event):
        widget = event.widget
        if not str(widget).startswith(str(self.notebook)+'.'):
            return
        # 聚焦可能早于页签切换事件；先按当前页重算，不能沿用上一短页的滚动范围。
        self.layout()
        self.canvas.update_idletasks()
        for axis, start, size, dimension in (('y', widget.winfo_rooty()-self.canvas.winfo_rooty(), widget.winfo_height(), self.canvas.winfo_height()),):
            if start < 0 or start+size > dimension:
                total = self.notebook.winfo_height() if axis == 'y' else self.notebook.winfo_width()
                offset = self.canvas.canvasy(0) if axis == 'y' else self.canvas.canvasx(0)
                getattr(self.canvas, axis+'view_moveto')(max(0, offset+start-16)/max(1,total))
