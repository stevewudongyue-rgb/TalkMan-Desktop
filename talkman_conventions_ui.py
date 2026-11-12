"""约定词管理与同窗选词确认。"""
import re
import time
import uuid
import tkinter as tk
from tkinter import ttk

from talkman_conventions import LANGUAGES, SCOPES, CODE_WORDS, convention_key
from talkman_convention_learning import CorrectionMemory, correction_pair


class ConventionsUIMixin:
    def init_convention_learning(self):
        self.correction_memory = CorrectionMemory(self.data_dir)
        self.learning_epoch = uuid.uuid4().hex
        self.learning_baseline = ''
        self.learning_selected = None
        self.learning_pending = None
        self.learning_job = None
        self.learning_hint_time = 0
        self.learning_hint_item = None

    def reset_manual_learning(self, text):
        if self.learning_job:
            self.after_cancel(self.learning_job)
        self.learning_job = None
        self.learning_pending = None
        self.learning_selected = None
        self.learning_baseline = text

    def track_manual_learning(self):
        if not self.has_feature("conventions"):
            return
        if self.learning_pending is None:
            self.learning_pending = (self.learning_baseline, self.learning_selected)
        if self.learning_job:
            self.after_cancel(self.learning_job)
        self.learning_job = self.after(1500, self.flush_manual_learning)

    def flush_manual_learning(self):
        pending, current = self.learning_pending, self.current_text()
        self.reset_manual_learning(current)
        if pending and not self.busy:
            before, selection = pending
            self.learn_correction(before, current, selection)

    def learn_correction(self, before, after, selection=None):
        if not self.has_feature("conventions"):
            return
        pair = correction_pair(before, after, selection)
        if pair:
            try:
                self.correction_memory.observe(*pair, self.learning_epoch + '\n' + before)
            except (OSError, ValueError):
                self.status_var.set('本次文字已保留，但近期纠正记录未能保存。')
            self.refresh_convention_hint(force=True)

    def build_convention_hint(self):
        self.learning_hint = ttk.Frame(self.editor_body, padding=(0, 0, 0, 8))
        self.learning_hint_label = ttk.Label(self.learning_hint, style='Muted.TLabel')
        self.learning_hint_label.pack(side='left', fill='x', expand=True)
        self.button(self.learning_hint, '看看', self.review_learned_word, style='Ghost.TButton').pack(side='left')
        self.button(self.learning_hint, '稍后', lambda: self.dismiss_learned_word(), style='Ghost.TButton').pack(side='left')
        self.button(self.learning_hint, '忽略此词', lambda: self.dismiss_learned_word(True), style='Ghost.TButton').pack(side='left')
        self.learning_hint_label.bind('<Configure>', lambda e: self.learning_hint_label.configure(wraplength=max(100, e.width)))

    def refresh_convention_hint(self, force=False):
        if not hasattr(self, 'learning_hint') or self.closing:
            return
        if not self.has_feature("conventions"):
            self.learning_hint.grid_remove()
            return
        now = time.monotonic()
        if not force and now - self.learning_hint_time < 1:
            return
        self.learning_hint_time = now
        controller = getattr(self, 'window_controller', None)
        settings = getattr(self, 'settings_window', None)
        if (self.busy or self.auto_running or self.ai_review is not None or self.convention_panel is not None
                or self.history_open or self.target_filling or self._editor_selecting
                or (controller and (controller.compact or controller.hidden))
                or (settings and settings.winfo_exists())):
            self.learning_hint.grid_remove()
            return
        choices = self.correction_memory.candidates(self.conventions.items, ready=True)
        self.learning_hint_item = choices[0] if choices else None
        if not choices:
            self.learning_hint.grid_remove()
            return
        item = choices[0]
        if len(item['aliases']) > 1:
            message = f"近期 {item['count']} 次纠正为「{item['new']}」，发现 {len(item['aliases'])} 种误写。一起记住？"
        else:
            message = f"你已 {item['count']} 次把「{item['old']}」改为「{item['new']}」。记住这个写法？"
        self.learning_hint_label.configure(text=message)
        self.learning_hint.grid(row=4, column=0, columnspan=2, sticky='ew')

    def review_learned_word(self):
        item = self.learning_hint_item
        if item:
            self.remember_selection(item['new'])

    def dismiss_learned_word(self, forever=False):
        item = self.learning_hint_item
        if item:
            try:
                self.correction_memory.dismiss(None, item['new'], forever)
            except (OSError, ValueError):
                self.status_var.set('提醒设置未能保存，请稍后重试。')
            self.refresh_convention_hint(force=True)

    def conventions_changed(self):
        self.auto_request += 1
        self.auto_running = False
        self.one_filler_button.state(['!disabled'])
        self.set_button_label(self.one_filler_button, 'AI 整理一次')
        self.refresh_cancel_button()
        self.cancel_auto_timer()
        self.provider_var.set(('代码口述 · ' if self.conventions.language != 'off' else '') + self._provider_label())

    def _provider_label(self):
        from talkman_asr import PROVIDERS
        if self.free_mode_var.get():
            return PROVIDERS['free'].label
        return PROVIDERS[self.asr_settings.provider].label

    def _convention_form(self, parent, *, word='', aliases='', scope='all', enabled=True, priority=True):
        values = dict(word=tk.StringVar(value=word), aliases=tk.StringVar(value=aliases),
                      scope=tk.StringVar(value=SCOPES[scope]), enabled=tk.BooleanVar(value=enabled),
                      priority=tk.BooleanVar(value=priority))
        for key, label in (('word', '正确写法'), ('aliases', '曾经识别错成什么？（逗号分隔，可留空）')):
            ttk.Label(parent, text=label, style='Muted.TLabel').pack(anchor='w', pady=(8, 4))
            entry = ttk.Entry(parent, textvariable=values[key])
            entry.pack(fill='x')
            if key == 'word':
                values['entry'] = entry
            else:
                values['aliases_entry'] = entry
        row = ttk.Frame(parent)
        row.pack(fill='x', pady=(12, 0))
        ttk.Combobox(row, textvariable=values['scope'], values=tuple(SCOPES.values()),
                     state='readonly', width=14).pack(side='left')
        ttk.Checkbutton(row, text='启用', variable=values['enabled']).pack(side='right')
        ttk.Checkbutton(parent, text='录音后优先核对这个词', variable=values['priority']).pack(anchor='w', pady=(12, 4))
        ttk.Label(parent, text='不填误写也可用。连接文字模型后，录音结束时核对新误写；AI 整理时也会参考。',
                  style='Muted.TLabel', wraplength=340).pack(anchor='w')
        return values

    def _save_convention_form(self, values, identifier=None):
        aliases = re.split(r'[,，;；\n]', values['aliases'].get())
        scope = next(key for key, name in SCOPES.items() if name == values['scope'].get())
        identifier = self.conventions.put(values['word'].get(), aliases, scope, values['enabled'].get(), identifier,
                                          priority=values['priority'].get())
        self.conventions_changed()
        return identifier

    def remember_selection(self, word=None):
        if not self.require_feature("conventions"):
            return
        if self.busy or self.ai_review is not None or self.target_filling:
            return
        if self.convention_panel is not None:
            self.term_form['aliases_entry'].focus_set()
            return
        selected = self.get_selection()
        word = word if word is not None else selected[2] if selected else ''
        if not word.strip() or len(word.strip()) > 80 or '\n' in word:
            self.status_var.set('请先选中一个词或短名称（最多 80 个字）。')
            return
        self.flush_manual_learning()
        self.capture_edit()
        self.cancel_auto_timer()
        self.auto_request += 1
        self.auto_running = False
        self.one_filler_button.state(['!disabled'])
        self.set_button_label(self.one_filler_button, 'AI 整理一次')
        self.close_history()
        self.window_controller.reveal(focus=False)
        self.window_controller.keep_expanded()
        self.magnifier.hide()
        win = self.convention_panel = tk.Frame(self, bg=self.palette['line'], padx=1, pady=1)
        actions = ttk.Frame(win, padding=(16, 8, 16, 12))
        actions.pack(side='bottom', fill='x')
        viewport = ttk.Frame(win)
        viewport.pack(fill='both', expand=True)
        canvas = self.term_canvas = tk.Canvas(viewport, bg=self.palette['bg'], highlightthickness=0, width=460, height=350)
        scroll = ttk.Scrollbar(viewport, orient='vertical', command=canvas.yview)
        scroll.pack(side='right', fill='y')
        canvas.pack(side='left', fill='both', expand=True)
        canvas.configure(yscrollcommand=scroll.set)
        body = ttk.Frame(canvas, padding=16)
        body_id = canvas.create_window(0, 0, window=body, anchor='nw')
        canvas.bind('<Configure>', lambda e: canvas.itemconfigure(body_id, width=e.width))
        body.bind('<Configure>', lambda e: canvas.configure(scrollregion=canvas.bbox('all')))
        ttk.Label(body, text='记住这个词', font=(self.fonts.ui, self.font_size(18))).pack(anchor='w')
        ttk.Label(body, text='不同读法可归到同一个词，确认后才生效。', style='Muted.TLabel').pack(anchor='w', pady=(8, 0))
        existing = next((item for item in self.conventions.items if convention_key(item['word']) == convention_key(word)), None)
        observed = self.correction_memory.candidates(self.conventions.items, word=word.strip())
        aliases = list(existing['aliases']) if existing else []
        aliases.extend(item['old'] for item in observed if item['old'] not in aliases)
        values = self.term_form = self._convention_form(body, word=existing['word'] if existing else word.strip(),
            aliases='，'.join(aliases[:12]), scope=existing['scope'] if existing else 'all',
            enabled=existing['enabled'] if existing else True, priority=existing.get('priority', False) if existing else True)
        hint = ('已带入近期纠正过的误写，可删改；保存才生效。' if observed else
                '误写可留空。勾选优先用词后，录音结束时会用文字模型核对。')
        feedback = tk.StringVar(value=self.conventions.error or hint)
        self.term_feedback = feedback
        ttk.Label(body, textvariable=feedback, style='Muted.TLabel', wraplength=390).pack(fill='x', pady=10)
        def save():
            try:
                self._save_convention_form(values, existing['id'] if existing else None)
            except (OSError, ValueError) as exc:
                feedback.set(str(exc) if isinstance(exc, ValueError) else '保存失败，原约定保留。')
                return
            self.close_convention()
            self.status_var.set('已记住“' + values['word'].get().strip() + '”，下次录音生效')
            self.button_feedback(self.remember_button, '已记住')
        self.term_save = save
        self.button(actions, '取消', self.close_convention, style='Ghost.TButton').pack(side='left')
        self.term_save_button = self.button(actions, '保存约定', save, style='Accent.TButton')
        self.term_save_button.pack(side='right')
        def layout(_event=None):
            if self.convention_panel is win and (_event is None or _event.widget is self):
                height = min(body.winfo_reqheight()+actions.winfo_reqheight()+2, self.winfo_height()-48)
                top = min(130, max(24, self.winfo_height()-height-24))
                win.place(relx=1, x=-24, y=top, anchor='ne', width=min(490, self.winfo_width()-48), height=height)
                win.lift()
        def bind_scroll(widget):
            def wheel(event):
                amount = (-1 if event.delta > 0 else 1) if getattr(event, 'delta', 0) else (-1 if event.num == 4 else 1)
                if canvas.yview() != (0.0, 1.0):
                    canvas.yview_scroll(amount*2, 'units')
                return 'break'
            widget.bind('<MouseWheel>', wheel, add=True)
            for child in widget.winfo_children():
                bind_scroll(child)
        bind_scroll(body)
        self.term_layout_binding = self.bind('<Configure>', layout, add=True)
        feedback.trace_add('write', lambda *_: win.after_idle(layout) if self.convention_panel is win else None)
        win.update_idletasks()
        layout()
        values['aliases_entry'].focus_set()
        self.refresh_convention_hint(force=True)
        self.window_controller.sync_hotkeys()

    def close_convention(self):
        if self.convention_panel is None:
            return
        self.convention_panel.destroy()
        self.convention_panel = None
        self.unbind('<Configure>', self.term_layout_binding)
        self.term_layout_binding = None
        self.text.focus_set()
        self.window_controller.sync_hotkeys()
        self.status_var.set('约定面板已关闭，正文保留')
        self.refresh_convention_hint(force=True)

    def _build_conventions_settings(self, box, win):
        if not self.has_feature("conventions"):
            self.license_placeholder(box, "记住品牌名、口述代码，并从近期纠正中学习用词。")
            return
        self._settings_description(box, '让 Talk Man 记住你的用词', '选词后点「记住这个词」，确认误写即可；近期反复纠正也会提醒你。')
        feedback = tk.StringVar(value=self.conventions.error or '已知误写直接纠正；优先词在录音结束时由文字模型核对新误写。')
        learning_row = ttk.Frame(box)
        learning_row.pack(fill='x', pady=(0, 6))
        learning = tk.BooleanVar(value=self.correction_memory.enabled)
        def change_learning():
            if not self.require_feature('conventions'):
                return
            try:
                self.correction_memory.set_enabled(learning.get())
                feedback.set('已保存。7 天内纠正为同一词 3 次后提醒，可包含多种误写。')
            except (OSError, ValueError):
                learning.set(self.correction_memory.enabled)
                feedback.set(self.correction_memory.error or '设置未能保存。')
            self.refresh_convention_hint(force=True)
        self.learning_switch = ttk.Checkbutton(learning_row, text='从我的纠正中发现约定词', variable=learning, command=change_learning)
        self.learning_switch.pack(side='left')
        def clear_learning():
            if not self.require_feature('conventions'):
                return
            try:
                self.correction_memory.clear()
                feedback.set('近期纠正记录已清除，已保存的约定词保留。')
            except (OSError, ValueError):
                feedback.set('清除失败，原记录保留。')
            self.refresh_convention_hint(force=True)
        self.button(learning_row, '清除学习记录', clear_learning, style='Ghost.TButton').pack(side='right')
        language = tk.StringVar(value=LANGUAGES[self.conventions.language])
        combo = self._settings_field(box, '口述场景', language, values=tuple(LANGUAGES.values()), readonly=True)
        samples = tk.StringVar()
        ttk.Label(box, textvariable=samples, style='Muted.TLabel', wraplength=600).pack(fill='x', pady=(4, 8))
        def show_samples():
            key = self.conventions.language
            examples = list(CODE_WORDS[key].items())[:2] if key != 'off' else []
            samples.set('普通口述不转换编程符号。' if not examples else
                        '、'.join(a+' → '+b for a, b in examples) + '；左圆括号 → (。仅转换关键词和符号；此场景暂停自动整理，可手动整理。')
        def change_language(_event=None):
            if not self.require_feature('conventions'):
                return
            key = next(key for key, name in LANGUAGES.items() if name == language.get())
            try:
                self.conventions.set_language(key)
                self.conventions_changed()
                feedback.set('已保存口述场景，下次录音生效。')
            except (ValueError, OSError):
                language.set(LANGUAGES[self.conventions.language])
                feedback.set('场景保存失败，原设置保留。')
            show_samples()
        combo.bind('<<ComboboxSelected>>', change_language)
        show_samples()
        content = ttk.Frame(box)
        content.pack(fill='both', expand=True, pady=(8, 0))
        content.columnconfigure(1, weight=1)
        content.rowconfigure(0, weight=1)
        listing = ttk.Frame(content)
        listing.grid(row=0, column=0, sticky='nsew', padx=(0, 16))
        listing.rowconfigure(0, weight=1)
        listing.columnconfigure(0, weight=1)
        ttk.Style(win).configure('Words.Treeview', rowheight=self.font_size(42))
        tree = self.convention_list = ttk.Treeview(listing, show='tree', height=6, selectmode='browse', style='Words.Treeview')
        tree.column('#0', width=175, minwidth=130)
        tree.grid(row=0, column=0, sticky='nsew')
        scroll = ttk.Scrollbar(listing, command=tree.yview)
        scroll.grid(row=0, column=1, sticky='ns')
        def scrolling(first, last):
            scroll.grid_remove() if float(first) <= 0 and float(last) >= 1 else scroll.grid()
            scroll.set(first, last)
        tree.configure(yscrollcommand=scrolling)
        editor = ttk.Frame(content)
        editor.grid(row=0, column=1, sticky='nsew')
        values = self.convention_settings_form = self._convention_form(editor)
        editing = [None]
        pending_delete = [None]
        def refresh(identifier=None):
            tree.delete(*tree.get_children())
            for item in self.conventions.items:
                tree.insert('', 'end', iid=item['id'], text=item['word'] + (' · 停用' if not item['enabled'] else ''))
            if identifier and tree.exists(identifier):
                tree.selection_set(identifier)
                tree.see(identifier)
        def selected(_event=None):
            selection = tree.selection()
            item = next((item for item in self.conventions.items if selection and item['id'] == selection[0]), None)
            if item:
                editing[0] = item['id']
                values['word'].set(item['word'])
                values['aliases'].set('，'.join(item['aliases']))
                values['scope'].set(SCOPES[item['scope']])
                values['enabled'].set(item['enabled'])
                values['priority'].set(item.get('priority', False))
            pending_delete[0] = None
            delete.configure(text='删除')
        def new():
            editing[0] = None
            pending_delete[0] = None
            delete.configure(text='删除')
            tree.selection_remove(*tree.selection())
            values['word'].set('')
            values['aliases'].set('')
            values['scope'].set(SCOPES['all'])
            values['enabled'].set(True)
            values['priority'].set(True)
            values['entry'].focus_set()
        def save():
            try:
                identifier = self._save_convention_form(values, editing[0])
            except (OSError, ValueError) as exc:
                feedback.set(str(exc) if isinstance(exc, ValueError) else '保存失败，原约定保留。')
                return
            editing[0] = identifier
            refresh(identifier)
            feedback.set('已保存；下次录音生效，当前正文不变。')
        def remove():
            if not self.require_feature('conventions'):
                return
            if not editing[0]:
                feedback.set('请先选择一个约定词。')
                return
            if pending_delete[0] != editing[0]:
                pending_delete[0] = editing[0]
                delete.configure(text='确认删除')
                feedback.set('再点确认删除；当前正文和记录不受影响。')
                return
            try:
                self.conventions.delete(editing[0])
            except (OSError, ValueError):
                feedback.set('删除失败，原约定保留。')
                return
            self.conventions_changed()
            new()
            refresh()
            feedback.set('已删除约定词。')
        self.convention_settings_save = save
        self.convention_settings_new = new
        tree.bind('<<TreeviewSelect>>', selected)
        actions = ttk.Frame(editor)
        actions.pack(fill='x', pady=(16, 0))
        self.button(actions, '新增', new, style='Ghost.TButton').pack(side='left')
        delete = self.button(actions, '删除', remove, style='Ghost.TButton')
        delete.pack(side='left')
        self.button(actions, '保存', save, style='Accent.TButton').pack(side='right')
        self._settings_footer(box, win, feedback)
        refresh()
