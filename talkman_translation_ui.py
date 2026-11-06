"""选区翻译：语言在设置中选择，沿用同窗对照和过期结果保护。"""
from dataclasses import replace
import tkinter as tk
from tkinter import ttk
from talkman_qwen import QwenError
from talkman_translation import LANGUAGES, TranslationMemory, SelectionTranslationPlan, TranslationResult


class TranslationUIMixin:
    def init_translation(self, preferences):
        language = preferences.get('translation_language', '英语')
        self.translation_language = tk.StringVar(value=language if language in LANGUAGES else '英语')
        self.translation_memory = TranslationMemory()
        self.translation_request = None

    def build_translation(self, parent):
        row = self.translation_tools = ttk.Frame(parent)
        row.pack(side='left')
        self.translate_button = self.button(row, '翻译', self.translate_document,
            icon='languages', style='Outline.TButton',
            tooltip=lambda: '把选中的文字翻译成'+self.translation_language.get()+'，其余文字不动。目标语言在「设置 → 指令改写」选择。原有英文、代码和约定词保留。')
        self.translate_button.pack(side='left')

    def build_translation_settings(self, parent):
        row = ttk.Frame(parent)
        row.pack(fill='x', pady=(8, 12))
        label = ttk.Label(row, text='翻译目标语言')
        label.pack(side='left')
        self.translation_choice = ttk.Combobox(row, textvariable=self.translation_language,
            values=LANGUAGES, state='readonly', width=12)
        self.translation_choice.pack(side='left', padx=12)
        self.translation_choice.bind('<<ComboboxSelected>>', self.translation_language_changed)
        for widget in (label, self.translation_choice):
            self.hover_help.attach(widget, '先选中正文，再点「翻译」。只处理选区，保留原有英文、代码和约定词；这个选择会自动记住。')

    def translation_language_changed(self, _event=None):
        try:
            self.save_ui_preferences(translation_language=self.translation_language.get())
        except OSError:
            self.status_var.set('目标语言本次生效，但未能保存偏好。')
        if self.translation_request is not None:
            self.auto_request += 1
            self.auto_running = False
            self.refresh_cancel_button()
            self.status_var.set('目标语言已切换，请重新点「翻译」。')
        self.refresh_translation_controls()

    def refresh_translation_controls(self):
        if not hasattr(self, 'translate_button'):
            return
        if self.translation_request is not None and self.translation_request != self.auto_request:
            self.translation_request = None
        running = self.translation_request is not None
        self.set_button_label(self.translate_button, '翻译中…' if running else '翻译')
        self.translate_button.state(['disabled'] if self.busy or self.auto_running or self.ai_review is not None
                                    or self.convention_panel is not None or self.target_filling else ['!disabled'])

    def translate_document(self):
        if self.busy or self.auto_running or self.ai_review is not None or self.convention_panel is not None or self.target_filling:
            return
        base = self.current_text()
        selected = self.get_selection()
        if not selected or not selected[2].strip():
            self.status_var.set('请先选中要翻译的文字；要翻译全文，可以先全选。')
            return
        if not self.text_model_ready():
            self.status_var.set('请在「设置 → 文字改写模型」连接可用服务。')
            return
        self.flush_manual_learning()
        self.capture_edit()
        try:
            words = [item['word'] for item in self.convention_snapshot().items]
            protection = self.translation_memory.protection(base, words)
            start = len(self.text.get('1.0', selected[0]))
            end = len(self.text.get('1.0', selected[1]))
            plan = SelectionTranslationPlan(base, self.translation_language.get(), protection, start, end)
        except QwenError as exc:
            self.status_var.set(str(exc))
            return
        self.cancel_auto_timer()
        self.disarm_delivery()
        self.close_history()
        self.auto_request += 1
        self.translation_request = self.auto_request
        self.auto_running = True
        self.refresh_cancel_button()
        self.status_var.set('正在把选中文字翻译为' + plan.language + '…')
        token = (self.auto_request, self.revision, plan)
        settings = replace(self.settings)
        client = self.make_text_client(settings)
        self.run_worker('translation', token, lambda: plan.execute(client))

    def finish_translation(self, token, state, result):
        request, revision, plan = token
        if request != self.auto_request:
            return
        self.translation_request = None
        self.auto_running = False
        self.refresh_cancel_button()
        if revision != self.revision or self.current_text() != plan.base or self.busy:
            self.status_var.set('正文已变化，旧译文已丢弃，请重新翻译。')
            return
        self.auto_block_revision = self.revision
        if state != 'ok' or not isinstance(result, TranslationResult):
            self.status_var.set(str(result) if state != 'ok' else '译文格式无效，原文保留。')
            return
        if result.text == plan.base:
            self.status_var.set('文字无需翻译，原有英文、代码和约定词已保留。')
            return
        def apply():
            self.translation_memory.remember(plan.base, plan.protected)
            self.translation_memory.remember(result.text, result.protected)
            self.set_text(result.text)
            end = plan.end + len(result.text) - len(plan.base)
            self.render_text(result.text, end, selection=(plan.start, end))
            self.auto_block_revision = self.revision
            self.cancel_auto_timer()
            self.status_var.set('已翻译为' + plan.language + '，可用「上一步」恢复；原有英文保持原样。')
            self.button_feedback(self.translate_button, '已翻译')
        self.review_result(plan.base, result.text, '选区翻译 · '+plan.language, apply)
        self.refresh_translation_controls()
