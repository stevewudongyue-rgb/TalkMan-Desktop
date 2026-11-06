"""填入按钮旁的目标切换：在窗口内部展开，不改变正文或触发填入。"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk


class TargetPicker:
    def __init__(self, app):
        self.app = app
        self.buttons = []
        self.panel = None
        self.generation = 0
        self.refreshing = False
        app.target_label_var.trace_add('write', self.update_labels)
        app.bind('<ButtonPress-1>', self.outside_click, add=True)
        app.bind('<Configure>', self.reposition, add=True)
        app.bind('<Configure>', lambda e: self.update_labels() if e.widget == app else None, add=True)

    def button(self, parent, compact=False):
        button = self.app.button(parent, '', style='Outline.TButton',
            tooltip=lambda: self.app.target_label_var.get() + '\n点击切换目标；只选择，不填入')
        button.configure(command=lambda: self.toggle(button, compact))
        self.buttons.append((button, compact))
        self.update_labels()
        return button

    def update_labels(self, *_args):
        label = ' '.join(self.app.target_label_var.get().split())
        target = getattr(self.app.target_controller, 'selected', None)
        if target is not None and target.app and target.app not in label:
            label = target.app + ' · ' + label
        if label == '未选择目标输入框':
            label = '选择目标'
        for button, compact in self.buttons:
            limit = 12 if compact else max(8, min(22, (self.app.winfo_width()-650)//12))
            short = label if len(label) <= limit else label[:limit - 1] + '…'
            button.configure(text=short + ' ▾')

    def update_buttons(self):
        disabled = self.app.busy or self.app.target_filling
        for button, _compact in self.buttons:
            button.state(['disabled'] if disabled else ['!disabled'])

    def toggle(self, anchor, compact=False):
        if self.panel is not None:
            self.close(restore_focus=True)
            return
        app = self.app
        if app.busy or app.target_filling or app.closing:
            return
        self.app.position_picker.cancel()
        self.anchor, self.compact = anchor, compact
        self.rows = {}
        self.panel = ttk.Frame(app, padding=12, style='Panel.TFrame',
                               borderwidth=1, relief='solid')
        self.panel.columnconfigure(0, weight=1)
        self.panel.rowconfigure(1, weight=1)
        heading = ttk.Frame(self.panel, style='Panel.TFrame')
        heading.grid(row=0, column=0, columnspan=2, sticky='ew', pady=(0, 6))
        ttk.Label(heading, text='切换目标', style='Panel.TLabel').pack(side='left')
        self.refresh_button = app.button(heading, '刷新', self.refresh, style='PanelGhost.TButton')
        self.refresh_button.pack(side='right')
        style = ttk.Style(app)
        style.configure('Targets.Treeview', rowheight=app.font_size(36))
        self.listing = ttk.Treeview(self.panel, show='tree', selectmode='browse',
                                   height=3 if compact else 5, style='Targets.Treeview')
        self.listing.column('#0', width=260, minwidth=100, stretch=True)
        self.listing.grid(row=1, column=0, sticky='nsew')
        self.scrollbar = ttk.Scrollbar(self.panel, command=self.listing.yview)
        self.scrollbar.grid(row=1, column=1, sticky='ns', padx=(6, 0))
        self.listing.configure(yscrollcommand=self.scroll)
        self.feedback = tk.StringVar(value='正在查找已打开的窗口…')
        self.note = ttk.Label(self.panel, textvariable=self.feedback, style='PanelMuted.TLabel')
        self.note.grid(row=2, column=0, columnspan=2, sticky='ew', pady=(8, 0))
        self.listing.bind('<ButtonRelease-1>', self.choose_click)
        self.listing.bind('<Return>', self.choose_focused)
        self.listing.bind('<space>', self.choose_focused)
        # 独立控件绑定优先于窗口快捷键；Esc 只收起列表，不取消正文操作。
        for widget in (self.panel, self.listing, self.refresh_button):
            widget.bind('<Escape>', lambda _event: self.dismiss())
        self.reposition()
        self.panel.lift()
        self.listing.focus_set()
        self.refresh()

    def scroll(self, first, last):
        if float(first) <= 0 and float(last) >= 1:
            self.scrollbar.grid_remove()
        else:
            self.scrollbar.grid()
        self.scrollbar.set(first, last)

    def reposition(self, event=None):
        if self.panel is None or (event is not None and event.widget != self.app):
            return
        app = self.app
        margin = 14 if self.compact else 32
        width = min(480, max(260, app.winfo_width() - margin * 2))
        right = self.anchor.master.winfo_rootx() - app.winfo_rootx() + self.anchor.master.winfo_width()
        x = max(margin, min(right - width, app.winfo_width() - margin - width))
        bottom = self.anchor.winfo_rooty() - app.winfo_rooty() - 6
        height = min(260 if not self.compact else 220, max(100, bottom - 8))
        self.panel.place(x=x, y=max(8, bottom - height), width=width, height=height)
        self.note.configure(wraplength=width - 30)

    def refresh(self):
        if self.panel is None or self.refreshing:
            return
        self.generation += 1
        generation = self.generation
        self.refreshing = True
        self.rows.clear()
        for row in self.listing.get_children():
            self.listing.delete(row)
        self.refresh_button.state(['disabled'])
        self.feedback.set('正在查找已打开的窗口…')

        def done(state, value):
            if self.panel is None or generation != self.generation or self.app.closing:
                return
            self.refreshing = False
            self.refresh_button.state(['!disabled'])
            if state != 'ok':
                self.feedback.set(str(value) or '刷新失败，请重试。')
                return
            current = getattr(self.app.target_controller, 'selected', None)
            selected = None
            for index, item in enumerate(value or []):
                if not isinstance(item, dict) or item.get('id') is None:
                    continue
                row = str(index)
                self.rows[row] = item
                label = item.get('label') or ' · '.join(filter(None, (item.get('app'), item.get('title'))))
                self.listing.insert('', 'end', iid=row, text=label)
                if current is not None and item['id'] == current.id:
                    selected = row
            rows = self.listing.get_children()
            if rows:
                selected = selected or rows[0]
                self.listing.selection_set(selected)
                self.listing.focus(selected)
                self.listing.see(selected)
            note = getattr(self.app.target_controller, 'refresh_note', '')
            self.feedback.set(note or ('点击窗口即可切换 · 不会填入或发送' if rows else
                    '未发现可识别的 Agent；可在「设置 → 填入目标」关闭筛选。'
                                       if self.app.only_agents_var.get() else '未发现可用窗口，请打开目标后刷新。'))
        try:
            self.app.refresh_target_windows(done)
        except Exception:
            done('error', '暂时无法刷新窗口，请稍后重试。')

    def choose_click(self, event):
        row = self.listing.identify_row(event.y)
        if row:
            return self.choose(row)

    def choose_focused(self, _event=None):
        selection = self.listing.selection()
        if selection:
            return self.choose(selection[0])
        return 'break'

    def choose(self, row):
        if self.refreshing or row not in self.rows or self.app.busy or self.app.target_filling:
            return 'break'
        try:
            self.app.choose_target(self.rows[row]['id'])
        except Exception as error:
            self.feedback.set(str(error) or '目标选择失败，请刷新后重试。')
            return 'break'
        self.close(restore_focus=True)
        return 'break'

    def outside_click(self, event):
        if self.panel is None:
            return
        widget = event.widget
        while widget is not None:
            if widget == self.panel or widget == self.anchor:
                return
            widget = getattr(widget, 'master', None)
        self.close()

    def dismiss(self):
        self.close(restore_focus=True)
        return 'break'

    def close(self, restore_focus=False):
        self.generation += 1
        self.refreshing = False
        if self.panel is not None:
            self.panel.destroy()
            self.panel = None
            if restore_focus and not self.app.closing:
                self.app.text.focus_set()
