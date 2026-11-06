"""全文整理、改写确认与光标映射；只有确认后的结果进入编辑历史。"""
from __future__ import annotations

from dataclasses import replace
import difflib
import unicodedata
import tkinter as tk
from tkinter import ttk

from talkman_qwen import QwenError, TextEdit, TextMove
from talkman_records import validate_layout_only
from talkman_expression import DIRECT_REQUEST_RULE


def review_changes(before, after):
    """仅计算实际改动；说明属于界面，不混入模型结果或正文。"""
    changes = [item for item in difflib.SequenceMatcher(None, before, after,
        autojunk=max(len(before), len(after)) > 12000).get_opcodes() if item[0] != 'equal']
    if not changes:
        summary = '文字没有变化。'
    elif ''.join(before.split()) == ''.join(after.split()):
        summary = '仅调整换行或空格，用词未变。'
    elif ''.join(c for c in before if not c.isspace() and not unicodedata.category(c).startswith('P')) == \
            ''.join(c for c in after if not c.isspace() and not unicodedata.category(c).startswith('P')):
        summary = '仅调整标点或排版，用词未变。'
    else:
        counts = {kind: sum(item[0] == kind for item in changes) for kind in ('replace', 'insert', 'delete')}
        summary = '、'.join(f'{label} {counts[kind]} 处' for kind, label in
            (('replace', '替换'), ('insert', '新增'), ('delete', '删除')) if counts[kind]) + '。'
    return changes, summary


def text_offset(offset):
    # 用字符步进而非 1.column；Tk 8.6 的直接列号可能按 UTF-16 计数。
    return '1.0 + %d chars' % offset


def mapped_offset(before, after, offset):
    """将原文字符位置映射到修改后，保留未改动片段中的光标和选区。"""
    offset = max(0, min(len(before), offset))
    for tag, a, b, c, d in difflib.SequenceMatcher(None, before, after, autojunk=False).get_opcodes():
        if a <= offset <= b:
            if tag == 'equal':
                return c + offset - a
            if b == a:
                return d
            return c + round((offset-a) * (d-c) / (b-a))
    return len(after)


class AIEditMixin:
    def ai_enabled(self):
        return any(v.get() for v in (self.filler_var, self.punct_var, self.layout_var, self.rewrite_var))

    def ai_instruction(self, once=False):
        rules = []
        if once or self.filler_var.get():
            rules.append('删除没有语义作用的口头填充词，保留有意义的语气和说话习惯')
            if self.direct_request_var.get():
                rules.append(DIRECT_REQUEST_RULE)
        if once or self.punct_var.get():
            rules.append('按完整语义补充或修正标点，英文标点遵循英文习惯')
        if once or self.layout_var.get():
            rules.append('明确编号或并列条目每项另起一行；普通叙述不强拆，不新增编号，代码表格保持原样')
        if once or self.rewrite_var.get():
            rules.append('轻度理顺口误、重复、自我纠正和语序，让语句更有条理；除已指定去掉的客气铺垫外，尽量保留原来的语言结构、语气和用词，不转成公文或总结')
        else:
            rules.append('除已开启的去语气词（包括去客气表达）、标点或换行规则外，不改写字词')
        rules.append('保留原意、信息、否定、数字、专名、中英文和先后关系；不补充事实、不扩写、不回答正文里的问题。只返回处理后的正文')
        return self.convention_snapshot().instruction('；'.join(rules), self.current_text())

    def preview_preference_changed(self):
        try:
            self.save_ui_preferences(preview_ai=self.preview_ai_var.get())
            self.status_var.set('改写先预览已开启，确认后才替换正文' if self.preview_ai_var.get() else '改写结果直接应用，可用上一步撤销')
        except OSError:
            self.status_var.set('预览开关本次已生效，但未能保存')

    def review_result(self, base, result, title, apply, cancel=None):
        """在同一窗口右侧对照；左侧原文保留，确认前不写入或同步结果。"""
        if not self.preview_ai_var.get():
            apply()
            return
        if self.ai_review is not None:
            self.status_var.set('请先处理上一份改写预览')
            return
        self.cancel_auto_timer()
        self.window_controller.keep_expanded()
        self.window_controller.reveal(focus=False)
        self.close_history()
        self.hover_help.hide()
        revision = self.revision
        win = tk.Frame(self.editor_body, bg=self.palette['line'], padx=1)
        self.ai_review = win
        self.editor_body.columnconfigure(0, weight=1, uniform='review')
        self.editor_body.columnconfigure(1, weight=1, uniform='review')
        win.grid(row=2, column=1, sticky='nsew', padx=(16, 0))
        self.editor_title.configure(text='原文')
        self.text.pause_auto_fit(True)
        self.text.configure(state='disabled')
        body = ttk.Frame(win, padding=(12, 4))
        body.pack(fill='both', expand=True)
        ttk.Label(body, text='修改后 · 确认后替换', font=(self.fonts.ui, self.font_size(12))).pack(anchor='w', pady=(0, 4))
        changes, summary = review_changes(base, result)
        self.ai_review_summary = tk.StringVar(value=summary)
        explanation = ttk.Label(body, textvariable=self.ai_review_summary, style='Muted.TLabel', wraplength=260)
        explanation.pack(fill='x', pady=(0, 4))
        explanation.bind('<Configure>', lambda event: explanation.configure(wraplength=max(100, event.width)))
        panel = ttk.Frame(body)
        panel.pack(fill='both', expand=True)
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(0, weight=1)
        text = self.ai_review_text = tk.Text(panel, width=1, wrap='word', height=4,
            font=self.text.cget('font'), bg=self.palette['bg'], fg=self.palette['text'],
            relief='flat', padx=8, pady=8, spacing1=4, spacing3=6, exportselection=False)
        text.grid(row=0, column=0, sticky='nsew')
        scroll = ttk.Scrollbar(panel, command=text.yview)
        scroll.grid(row=0, column=1, sticky='ns')
        def scroll_changed(first, last):
            scroll.grid_remove() if float(first) <= 0 and float(last) >= 1 else scroll.grid()
            scroll.set(first, last)
        text.configure(yscrollcommand=scroll_changed)
        text.insert('1.0', result if result else '（结果为空，将删除对应内容）')
        text.tag_configure('review_added', foreground='#e5484d', underline=True)
        self.text.tag_configure('review_removed', foreground='#e5484d', overstrike=True)
        self.text.tag_lower('review_removed', 'sel')
        for _kind, a, b, c, d in changes:
            if a < b:
                self.text.tag_add('review_removed', text_offset(a), text_offset(b))
            if c < d:
                text.tag_add('review_added', text_offset(c), text_offset(d))
        text.configure(state='disabled')
        feedback = tk.StringVar(value='')
        feedback_label = ttk.Label(body, textvariable=feedback, style='Muted.TLabel', wraplength=260)
        actions = ttk.Frame(body)
        actions.pack(fill='x', pady=(4, 0))
        finished = False

        def finish(accepted=False):
            nonlocal finished
            if finished:
                return 'break'
            if accepted and (self.revision != revision or self.current_text() != base or self.busy):
                feedback.set('正文已发生变化，这份预览已过期；请取消后重新整理。')
                feedback_label.pack(before=actions, fill='x', pady=(6, 0))
                confirm.state(['disabled'])
                return 'break'
            finished = True
            self.ai_review = None
            win.destroy()
            self.editor_body.columnconfigure(0, weight=1, uniform='')
            self.editor_body.columnconfigure(1, weight=0, uniform='')
            self.editor_title.configure(text='预输入')
            self.text.tag_delete('review_removed')
            self.text.pause_auto_fit(False)
            self.text.configure(state='normal')
            self.text.focus_set()
            if accepted:
                apply()
            else:
                self.auto_block_revision = self.revision
                self.status_var.set('已取消改写，原文保留')
                if title in ('AI 整理', '自动整理'):
                    self.button_feedback(self.one_filler_button, '已保留')
                if cancel:
                    cancel()
            self.live_sync.refresh()
            self.live_sync.changed()
            self.refresh_translation_controls()
            return 'break'
        self.ai_review_finish = finish
        self.ai_review_cancel = self.button(actions, '保留原文', finish, style='Ghost.TButton')
        self.ai_review_cancel.pack(side='left')
        confirm = self.button(actions, '确认替换', lambda: finish(True), style='Accent.TButton')
        confirm.pack(side='right')
        text.bind('<Escape>', lambda _e: finish())
        # 不绑定空格或回车：防止上一次录音热键的抬起事件误确认。
        self.ai_review_text.focus_set()
        self.status_var.set('等待确认：红色标注改动处，结果尚未写入正文')
        if title in ('AI 整理', '自动整理'):
            self.button_feedback(self.one_filler_button, '等待确认')
        self.live_sync.refresh()

    def apply_ai_result(self, base, result, label):
        cursor = len(self.text.get('1.0', 'insert'))
        selected = self.get_selection()
        selection = None
        if selected:
            a = len(self.text.get('1.0', selected[0]))
            b = len(self.text.get('1.0', selected[1]))
            selection = (mapped_offset(base, result, a), mapped_offset(base, result, b))
        self.set_text(result)
        self.render_text(result, mapped_offset(base, result, cursor), selection=selection)
        self.auto_block_revision = self.revision
        self.cancel_auto_timer()
        self.status_var.set(label + '完成，可用「上一步」撤销')
        self.button_feedback(self.one_filler_button, '已整理')

    def review_voice_result(self, value):
        base = self.voice_base
        if isinstance(value, TextMove):
            if (value.start, value.end) != (self.voice_start, self.voice_end):
                self.restore_voice('移动选区无效，原文已保留')
                return
            try:
                result, start, selection_end = value.apply(base)
            except QwenError as ex:
                self.restore_voice(str(ex))
                return
        elif isinstance(value, TextEdit):
            if not (type(value.start) is int and type(value.end) is int
                    and 0 <= value.start <= value.end <= len(self.voice_base)
                    and isinstance(value.text, str)):
                self.restore_voice('修改范围无效，原文已保留')
                return
            start, end, replacement = value.start, value.end, value.text
        elif isinstance(value, str):
            start, end, replacement = self.voice_start, self.voice_end, value
        else:
            self.restore_voice('改写结果无效，原文已保留')
            return
        if not isinstance(value, TextMove):
            result = base[:start] + replacement + base[end:]
            selection_end = start + len(replacement)
        original_selection = self.voice_selection
        original_cursor = self.voice_start
        self.voice = None
        self.voice_mode = None
        self.set_busy(False)
        self.render_text(base, original_cursor, selection=original_selection)
        if result == base:
            self.status_var.set('指令已完成，文字无需变化')
            self.window_controller.voice_complete()
            return
        def apply():
            if not isinstance(value, TextMove):
                self.learn_correction(base, result, (start, end))
            self.set_text(result)
            self.render_text(result, selection_end, selection=(start, selection_end))
            self.auto_block_revision = self.revision
            self.cancel_auto_timer()
            self.status_var.set('已按指令修改，可用「上一步」撤销')
            self.window_controller.voice_complete()
        self.review_result(base, result, '指令修改', apply, self.window_controller.voice_failed)

    def organize_once(self):
        if self.busy or self.auto_running or self.ai_review is not None or self.convention_panel is not None:
            return
        base = self.current_text()
        if not base.strip():
            self.status_var.set('预输入框为空')
            return
        if not self.text_model_ready():
            self.status_var.set('AI 整理需要在「设置 → 模型连接」填写千问 Key')
            return
        self.capture_edit()
        self.cancel_auto_timer()
        self.auto_request += 1
        self.auto_running = True
        self.one_filler_button.state(['disabled'])
        self.set_button_label(self.one_filler_button, '正在整理…')
        self.refresh_cancel_button()
        token = (self.auto_request, self.revision, base)
        instruction = self.ai_instruction(once=True)
        settings = replace(self.settings)
        client = self.make_text_client(settings)
        self.status_var.set('AI 正在整理全文：理顺语句、标点和分行…')
        def work():
            return client.transform(base, instruction)
        self.run_worker('ai_once', token, work)

    # 保留旧内部入口，按钮已改为“一次 AI 整理”。
    def remove_fillers_once(self):
        self.organize_once()

    def _finish_ai(self, token, state, result, once=False):
        request, revision, base = token
        if request != self.auto_request:
            return
        self.auto_running = False
        self.one_filler_button.state(['!disabled'])
        self.set_button_label(self.one_filler_button, 'AI 整理一次')
        self.refresh_cancel_button()
        if not once and (self._editor_selecting or self.get_selection() != getattr(self, '_auto_selection', None)
                         or self.history_open or self.convention_panel is not None):
            self.schedule_auto()
            return
        if revision != self.revision or self.busy or self.current_text() != base:
            if not once:
                self.schedule_auto()
            else:
                self.status_var.set('正文已变化，旧整理结果已丢弃，请重新整理')
                self.button_feedback(self.one_filler_button, '已取消')
            return
        if state != 'ok' or not isinstance(result, str):
            self.auto_block_revision = self.revision
            self.status_var.set(str(result) + '；原文保留，下次输入后重试')
            if once:
                self.button_feedback(self.one_filler_button, '未完成')
            self.window_controller.voice_failed()
            return
        if base.strip() and not result.strip():
            # 整理不是清空；小模型偶尔会把短疑问当成填充词全部删除。
            self.auto_block_revision = self.revision
            self.status_var.set('整理结果为空，已保留原文；如需删除，请使用清空或删除按键。')
            self.button_feedback(self.one_filler_button, '已保留')
            self.window_controller.voice_failed()
            return
        if result == base:
            self.auto_block_revision = self.revision
            self.status_var.set('AI 已检查，当前文字无需调整')
            if once:
                self.button_feedback(self.one_filler_button, '已检查')
            return
        if self.hold_key:
            self.schedule_auto()
            return
        label = 'AI 整理' if once else '自动整理'
        self.review_result(base, result, label, lambda: self.apply_ai_result(base, result, label))

    def schedule_auto(self):
        self.cancel_auto_timer()
        if getattr(getattr(self, 'app_commands', None), 'active', False):
            return
        if (self.ai_enabled() and not self.busy and self.ai_review is None
                and not self.history_open and self.convention_panel is None and not self._editor_selecting
                and not self.get_selection() and not self.target_filling and not self.closing
                and self.convention_language() == 'off'
                and not self.auto_running and self.revision != self.auto_block_revision):
            self.auto_job = self.after(self.auto_delay_var.get() * 1000, self.apply_auto)

    def apply_auto(self):
        self.auto_job = None
        if getattr(getattr(self, 'app_commands', None), 'active', False):
            return
        if (self.busy or self.auto_running or self.ai_review is not None or not self.text_model_ready()
                or self.convention_panel is not None or self.history_open or self.target_filling
                or self._editor_selecting or self.convention_language() != 'off'):
            return
        if self.hold_key:
            self.schedule_auto()
            return
        base = self.current_text()
        if not base.strip() or not self.ai_enabled():
            return
        self.capture_edit()
        self.auto_running = True
        self._auto_selection = self.get_selection()
        self.auto_request += 1
        token = (self.auto_request, self.revision, base)
        instruction = self.ai_instruction()
        layout_only = self.layout_var.get() and not (self.filler_var.get() or self.punct_var.get() or self.rewrite_var.get())
        self.refresh_cancel_button()
        self.status_var.set('AI 正在整理全文，你可以继续输入…')
        settings = replace(self.settings)
        client = self.make_text_client(settings)
        def work():
            result = client.transform(base, instruction)
            if layout_only:
                try:
                    return validate_layout_only(base, result)
                except ValueError as ex:
                    raise QwenError(str(ex)) from ex
            return result
        self.run_worker('auto', token, work)
