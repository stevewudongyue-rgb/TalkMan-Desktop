"""公开版功能状态与软件说明。

保留这个模块是为了兼容旧版本的数据目录和调用点。公开版不再区分功能档位，
也不会读取、校验或展示兑换码；模型服务仍由用户在设置中自行配置。
"""
import json
from pathlib import Path
import tkinter as tk
from tkinter import ttk

from talkman_license import ASSETS, LicenseClient, atomic_write
from talkman_conventions import ConventionSnapshot


class LicenseUIMixin:
    def has_feature(self, feature):
        # 公开开源版完整开放功能。旧的 LicenseClient 仍保留，避免老数据目录
        # 或第三方测试桩导入时报错，但授权状态不再参与功能门控。
        return True

    def require_feature(self, feature):
        return True

    def convention_snapshot(self):
        return self.conventions.snapshot()

    def convention_language(self):
        return self.conventions.language

    def persist_current_text(self):
        try:
            atomic_write(self.data_dir / 'current-text.txt', self.current_text().encode('utf-8'))
            return True
        except OSError:
            self.status_var.set('当前正文保存失败，请先复制文字再退出。')
            return False

    def restore_current_text(self):
        path = self.data_dir / 'current-text.txt'
        if not path.exists():
            return
        try:
            value = path.read_text(encoding='utf-8')
            self.set_text(value)
            self.cancel_auto_timer()
            self.auto_block_revision = self.revision
            self.status_var.set(self.settings_error or '已恢复上次退出前的正文')
        except (OSError, UnicodeError):
            self.status_var.set('上次正文未能读取，原文件保留。')

    def init_license_ui(self):
        self.license_running = False
        self.license_tick_job = None
        self.license_widgets = []
        for widget, feature in ((self.records_button, 'records'), (self.save_button, 'records'),
                                (self.remember_button, 'conventions'), (self.translation_tools, 'translation')):
            siblings = list(widget.master.pack_slaves())
            after = siblings[siblings.index(widget)+1:]
            self.license_widgets.append((widget, feature, widget.pack_info(), after))
        self._feature_state = None
        self.refresh_license_ui()
        self.restore_current_text()

    def refresh_license_ui(self):
        state = (True, True, True)
        if state == self._feature_state:
            return
        for widget, feature, packing, later in self.license_widgets:
            if self.has_feature(feature):
                options = dict(packing)
                following = next((item for item in later if item.winfo_manager() == 'pack'
                                  and item.pack_info().get('in') == options.get('in')), None)
                if following is not None:
                    options['before'] = following
                widget.pack(**options)
            else:
                widget.pack_forget()
        self.saved_history = self.load_history()
        self._feature_state = state
        self.refresh_history()
        self.refresh_convention_hint(force=True)
        self.provider_var.set(('代码口述 · ' if self.convention_language() != 'off' else '') + self._provider_label())

    def license_tick(self):
        # 旧版本兼容入口；公开版不再进行后台授权续验或网络请求。
        self.license_tick_job = None

    def license_operation(self, action, code='', silent=False):
        if self.license_running:
            return
        self.license_running = True
        feedback = getattr(self, 'license_feedback', None)
        if not silent and feedback is not None:
            feedback.set('正在验证…')
        self.run_worker('license', silent,
                        lambda: self.license_client.redeem(code) if action == 'redeem' else self.license_client.refresh())

    def finish_license_operation(self, silent, state, value):
        self.license_running = False
        self.refresh_license_ui()
        win = getattr(self, 'settings_window', None)
        if not silent:
            self.status_var.set(value)
            if state == 'ok' and win is not None and win.winfo_exists():
                self._close_settings(win)
                self.open_settings('license')
        feedback = getattr(self, 'license_feedback', None)
        if feedback is not None and win is not None and win.winfo_exists():
            feedback.set(value)

    def license_placeholder(self, parent, description):
        self._settings_description(parent, '完整功能已开放', description)

    def _build_license_settings(self, box, win):
        self._settings_description(box, '公开开源版',
            '录音、编辑、AI 整理、草稿簿、投递历史、约定词、翻译和 Agent 填入等完整功能均可直接使用。')
        ttk.Label(box, text='TalkMan Desktop 采用 Apache License 2.0 发布，不需要兑换码、账号或授权解锁。\n模型 API Key 由你在“语音识别模型”和“文字改写模型”中自行配置；TalkMan 不内置供应商密钥。\n免费在线服务受服务商额度、网络和策略影响，随时可能调整或不可用。',
                  style='SettingsHelp.TLabel', wraplength=560).pack(anchor='w', pady=(0, 20))
        links = ttk.Frame(box)
        links.pack(fill='x')
        for label, name in (('软件许可', 'TalkMan-软件使用协议.md'), ('隐私说明', 'TalkMan-隐私说明.md'),
                            ('第三方组件', 'TalkMan-第三方组件说明.md')):
            self.button(links, label, lambda n=name: self.show_legal_text(n), style='Ghost.TButton').pack(side='left', padx=(0, 12))

    def show_legal_text(self, name):
        previous_grab = self.grab_current()
        win = tk.Toplevel(self)
        win.title(name.removeprefix('TalkMan-').removesuffix('.md'))
        win.geometry('640x500')
        win.transient(previous_grab if previous_grab is not None else self)
        content = tk.Text(win, wrap='word', padx=20, pady=20, bg=self.palette['bg'],
                          fg=self.palette['text'], relief='flat', font=(self.fonts.ui, self.font_size(11)))
        scrollbar = ttk.Scrollbar(win, command=content.yview)
        scrollbar.pack(side='right', fill='y')
        content.configure(yscrollcommand=scrollbar.set)
        content.pack(fill='both', expand=True)
        try:
            value = (ASSETS / 'legal' / name).read_text(encoding='utf-8')
        except OSError:
            value = '说明文件未能读取，请在官方发布页查看。'
        content.insert('1.0', value)
        content.configure(state='disabled')
        if previous_grab is not None:
            def close():
                win.grab_release()
                win.destroy()
                if previous_grab.winfo_exists():
                    previous_grab.grab_set()
            win.protocol('WM_DELETE_WINDOW', close)
            win.grab_set()

    def show_license_terms(self):
        if self.ui_preferences.get('agreement_version') == 1:
            return
        if self.guide_job:
            self.after_cancel(self.guide_job)
            self.guide_job = None
        self.busy = True
        win = tk.Toplevel(self)
        win.withdraw()
        win.title('开始使用 · Talk Man')
        win.transient(self)
        win.resizable(False, False)
        box = ttk.Frame(win, padding=28)
        box.pack(fill='both', expand=True)
        ttk.Label(box, text='把话说出来，改好再发。', font=(self.fonts.ui, self.font_size(18))).pack(anchor='w', pady=(0, 16))
        ttk.Label(box, text='TalkMan Desktop 是完整功能的公开开源桌面预输入工具。\n录音、修改、草稿、历史、约定词、翻译和 Agent 填入均可直接使用；模型 Key 由你自行配置。',
                  wraplength=460).pack(anchor='w')
        links = ttk.Frame(box)
        links.pack(fill='x', pady=16)
        for label, name in (('阅读使用协议', 'TalkMan-软件使用协议.md'), ('阅读隐私说明', 'TalkMan-隐私说明.md')):
            self.button(links, label, lambda n=name: self.show_legal_text(n), style='Ghost.TButton').pack(side='left', padx=(0, 16))
        feedback = tk.StringVar()
        ttk.Label(box, textvariable=feedback, style='Muted.TLabel', wraplength=460).pack(anchor='w')
        def decline():
            win.grab_release()
            win.destroy()
            self.busy = False
            self.close_app()
        def accept():
            try:
                self.save_ui_preferences(agreement_version=1)
            except OSError:
                feedback.set('同意记录未能保存，请检查本机数据目录权限。')
                return
            win.grab_release()
            win.destroy()
            self.busy = False
            self.show_quick_guide()
        actions = ttk.Frame(box)
        actions.pack(fill='x', pady=(16, 0))
        self.button(actions, '不同意并退出', decline, style='Ghost.TButton').pack(side='left')
        self.button(actions, '同意并开始', accept, style='Accent.TButton').pack(side='right')
        win.protocol('WM_DELETE_WINDOW', decline)
        win.update_idletasks()
        win.geometry(f'+{max(0,self.winfo_rootx()+(self.winfo_width()-win.winfo_reqwidth())//2)}'
                     f'+{max(0,self.winfo_rooty()+100)}')
        win.deiconify()
        win.grab_set()
