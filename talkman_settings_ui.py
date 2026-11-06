"""统一设置窗口：语音识别、文字处理、快捷键、显示与填入目标。"""
from __future__ import annotations

import json
from pathlib import Path
import plistlib
import queue
import sys
import tkinter as tk
from tkinter import ttk

from talkman_asr import (ASRSettings, PROVIDERS, asr_keys_remembered, load_asr_settings,
                         save_asr_settings, validate)
from talkman_credentials import secure_storage_supported
from talkman_macos_setup import check_permissions, open_permission_settings
from talkman_voice import prepare_microphone_permission, VoicePermissionError
from talkman_qwen import QwenError, QwenSettings, load_settings, qwen_key_remembered, save_settings
from talkman_shortcuts import (
    ACTION_LABELS, DEFAULT_SHORTCUTS, ShortcutError, display_shortcut,
    save_shortcuts, shortcut_from_event, validate_shortcuts,
)
from talkman_global_hotkeys import GLOBAL_ACTIONS
from talkman_hover_help import FIELD_HELP
from talkman_settings_viewport import SettingsViewport
from talkman_window import WindowPlatform, window_screen, fit_window_rect
from talkman_version import PRODUCT_NAME, VERSION, BUILD, RELEASE_DATE, RELEASE_HISTORY, version_label
from talkman_free_service import NOTICE, FreeClient
from talkman_update_ui import UpdateUIMixin
from talkman_system_style import keep_mac_windows_separate


class SettingsPanelMixin(UpdateUIMixin):
    """依赖主窗口的 palette、fonts、button 及原有设置/语音状态接口。"""

    def open_asr_settings(self):
        self.open_settings("asr")

    def open_controls_settings(self):
        self.open_settings("controls")

    def open_text_settings(self):
        self.open_settings("model")

    def open_target_settings(self):
        self.open_settings("target")

    def open_settings(self, page="asr"):
        if page == 'license':
            page = 'about'
        if self.busy or self.ai_review is not None or self.convention_panel is not None:
            return
        pages = [("asr", "语音识别模型"), ("model", "文字改写模型"), ("text", "指令改写"), ("conventions", "约定"),
                 ("controls", "快捷键"), ("window", "窗口与显示"), ("target", "填入目标"), ("about", "版本")]
        if sys.platform == "darwin":
            pages.append(("mac", "Mac 授权"))
        if page not in dict(pages):
            page = "asr"
        existing = getattr(self, "settings_window", None)
        if existing and existing.winfo_exists():
            self.settings_notebook.select(self.settings_tabs[page])
            self._raise_settings_window(existing)
            return

        keep_mac_windows_separate()
        win = tk.Toplevel(self)
        # 布局完成后再显示，避免先闪出默认尺寸再扩张。
        win.withdraw()
        self.settings_window = self.asr_window = self.controls_window = win
        if hasattr(self, 'window_controller'):
            self.window_controller.sync_hotkeys()
        win.title("设置 · Talk Man")
        win.configure(bg=self.palette["bg"])
        win.geometry("700x700")
        win.minsize(650, 520)
        if sys.platform != 'darwin':
            win.transient(self)

        shell = ttk.Frame(win, padding=(24, 20, 24, 20))
        shell.pack(fill="both", expand=True)
        heading = ttk.Frame(shell)
        heading.pack(fill="x", pady=(0, 18))
        ttk.Label(heading, text="设置", font=(self.fonts.ui, self.font_size(22))).pack(side="left")
        def command_from_settings():
            self._close_settings(win)
            self.toggle_command()
        self.button(heading, '返回说指令', command_from_settings, style='Outline.TButton',
            tooltip='回到主窗口开始说指令；需要保留的设置请先保存。可以开关选项、保存草稿或修改快捷键。').pack(side='left', padx=16)
        self.button(heading, '新手引导', self.show_quick_guide, style='Ghost.TButton').pack(side='right', padx=(12, 0))
        ttk.Label(heading, text=f'{VERSION} · 构建 {BUILD}', style="Muted.TLabel").pack(side="right", anchor="s", pady=(0, 4))

        # Mac 额外提供权限页；紧凑页签适应最小窗口。
        style = ttk.Style(win)
        style.configure("Settings.TNotebook.Tab", padding=(10, 12))
        style.map("Settings.TNotebook.Tab", padding=[("selected", (10, 12))])
        self.settings_viewport = SettingsViewport(shell, win, self.palette, compact_navigation=self.ui_scale > 1.0)
        self.settings_viewport.pack(fill='both', expand=True)
        self.settings_notebook = self.settings_viewport.notebook
        self.settings_tabs = {}
        for name, label in pages:
            tab = ttk.Frame(self.settings_notebook, padding=(0, 18, 0, 0))
            self.settings_tabs[name] = tab
            self.settings_notebook.add(tab, text=label)

        self._build_asr_settings(self.settings_tabs["asr"], win)
        self._build_text_settings(self.settings_tabs["text"], win)
        self._build_conventions_settings(self.settings_tabs["conventions"], win)
        self._build_model_settings(self.settings_tabs["model"], win)
        self._build_controls_settings(self.settings_tabs["controls"], win)
        self._build_window_settings(self.settings_tabs["window"], win)
        self.mobile_connection.build_settings(self.settings_tabs["target"])
        self._build_target_settings(self.settings_tabs["target"], win)
        self._build_about_settings(self.settings_tabs['about'], win)
        if "mac" in self.settings_tabs:
            self._build_mac_setup(self.settings_tabs["mac"], win)
        self.settings_notebook.select(self.settings_tabs[page])
        win._talkman_resize_job = None
        win._talkman_resize_running = False
        win._talkman_resize_initial = True
        win.bind("<Destroy>", lambda event: self._cancel_settings_resize(win) if event.widget == win else None, add=True)
        win.bind('<Configure>', lambda event: self._queue_settings_resize(win) if event.widget == win else None, add=True)
        self._fit_settings_window(win)
        keep_mac_windows_separate()
        win.deiconify()
        self._raise_settings_window(win)
        win.protocol("WM_DELETE_WINDOW", lambda: self._close_settings(win))

    def _raise_settings_window(self, win):
        if not win.winfo_exists():
            return
        pinned = self.topmost_var.get() or self.window_controller.compact
        if sys.platform == 'darwin':
            # Mac 的置顶主窗口可能压住 transient 子窗口；设置打开时临时让出置顶层。
            self.attributes('-topmost', False)
            win.attributes('-topmost', pinned)
            win.lift()
            win.focus_set()
        else:
            win.attributes('-topmost', pinned)
            win.lift(self)

    def _cancel_settings_resize(self, win):
        job = getattr(win, "_talkman_resize_job", None)
        if job is not None:
            try:
                win.after_cancel(job)
            except tk.TclError:
                pass
            win._talkman_resize_job = None

    def _queue_settings_resize(self, win):
        if (not win.winfo_ismapped() or getattr(win, "_talkman_resize_running", False)
                or getattr(win, "_talkman_resize_job", None) is not None):
            return
        win._talkman_resize_job = win.after_idle(lambda: self._fit_settings_window(win))

    def _fit_settings_window(self, win):
        self._cancel_settings_resize(win)
        if not win.winfo_exists():
            return
        win._talkman_resize_running = True
        try:
            win.update_idletasks()
            native = WindowPlatform(win)
            bounds = native.bounds()
            screens = native.screens()
            initial = win._talkman_resize_initial
            editor_bounds = WindowPlatform(self).bounds()
            screen = window_screen(editor_bounds if initial else bounds, screens)
            if initial:
                # Mac 尚未映射的 Toplevel 可能仍报告 1×1；不能把差值当作巨大标题栏。
                # 使用已显示编辑器的窗框厚度，直接配置首次显示的最终矩形。
                frame_w = max(0, editor_bounds.width-self.winfo_width())
                frame_h = max(0, editor_bounds.height-self.winfo_height())
                limit_w = max(1, screen.width-48-frame_w)
                limit_h = max(1, screen.height-48-frame_h)
                preferred = getattr(self, '_settings_size', (round(960*self.ui_scale), 720))
                width, height = min(limit_w, preferred[0]), min(limit_h, preferred[1])
                win.minsize(min(650, width), min(520, height))
                x = screen.x+(screen.width-width-frame_w)//2
                y = screen.y+(screen.height-height-frame_h)//2
                win.geometry(f'{width}x{height}+{x}+{y}')
                win._talkman_resize_initial = False
                self.settings_viewport.layout()
                return
            frame_w = max(0, bounds.width-win.winfo_width())
            frame_h = max(0, bounds.height-win.winfo_height())
            limit_w, limit_h = max(1, screen.width-48-frame_w), max(1, screen.height-48-frame_h)
            minimum = (min(650, limit_w), min(520, limit_h))
            if win.minsize() != minimum:
                win.minsize(*minimum)
            # 设置外框不随模型字段、版本记录或不同页面的内容长度变化。
            # 记住用户本次运行手动调整的大小；换到小屏时才限制到工作区。
            width = min(limit_w, win.winfo_width())
            height = min(limit_h, win.winfo_height())
            if (width, height) != (win.winfo_width(), win.winfo_height()):
                win.geometry(f"{width}x{height}")
            win.update_idletasks()
            bounds = native.bounds()
            destination = fit_window_rect(bounds, screen)
            if (destination.x, destination.y) != (bounds.x, bounds.y):
                native.move_to(destination.x, destination.y)
            self.settings_viewport.layout()
        finally:
            win._talkman_resize_running = False

    def _close_settings(self, win):
        self._cancel_settings_resize(win)
        self._settings_size = (win.winfo_width(), win.winfo_height())
        win.destroy()
        if sys.platform == 'darwin' and not self.closing:
            self.attributes('-topmost', self.topmost_var.get() or self.window_controller.compact)
        if hasattr(self, 'window_controller'):
            self.window_controller.sync_hotkeys()
        if not self.closing:
            self.text.focus_set()
            guide = getattr(self, 'guide', None)
            if guide and guide.opened:
                guide.settings_closed()

    def _settings_description(self, parent, title, description):
        ttk.Label(parent, text=title, style='SettingsTitle.TLabel').pack(anchor="w", pady=(0, 6))
        ttk.Label(parent, text=description, style="SettingsHelp.TLabel", wraplength=560).pack(anchor="w", pady=(0, 18))

    def _settings_heading(self, parent, title):
        ttk.Separator(parent, style='Settings.TSeparator').pack(fill='x', pady=(18, 14))
        ttk.Label(parent, text=title, style='SettingsTitle.TLabel').pack(anchor='w', pady=(0, 12))

    def _settings_group(self, parent, title):
        group = ttk.Frame(parent, style='SettingsGroup.TFrame', padding=16)
        group.pack(fill='x', pady=(0, 20))
        ttk.Label(group, text=title, style='SettingsTitle.TLabel').pack(anchor='w', pady=(0, 12))
        return group

    def _build_about_settings(self, box, win):
        self._settings_description(box, PRODUCT_NAME, version_label())
        self._build_update_settings(box)
        self._build_license_settings(box, win)
        self._settings_heading(box, '版本记录')
        ttk.Label(box, text='更新记录 · 从新到旧，包含已归档的 0.1.9 起所有版本。',
                  style='Muted.TLabel', wraplength=560).pack(anchor='w', pady=(0, 16))
        for version, date, updates in RELEASE_HISTORY:
            group = self._settings_group(box, version + (' · 当前版本' if version == VERSION else ''))
            ttk.Label(group, text=date, style='Muted.TLabel').pack(anchor='w', pady=(0, 10))
            for item in updates:
                ttk.Label(group, text='· '+item, wraplength=540).pack(anchor='w', pady=(0, 8))
        self._settings_footer(box, win, tk.StringVar())

    def _build_free_service_settings(self, box, win):
        box = self._settings_group(box, '免费在线内测')
        row = ttk.Frame(box)
        row.pack(fill='x')
        feedback = tk.StringVar(value='本机已开通，开启后录音和文字处理均走免费服务。'
            if self.free_service.configured else '本机尚未开通；需要维护者发放独立测试连接。')
        self.free_settings_feedback = feedback
        def changed():
            if self.busy or self.auto_running or self.ai_review is not None:
                self.free_mode_var.set(not self.free_mode_var.get())
                feedback.set('请先结束当前录音或改写，再切换服务。')
                return
            if self.free_mode_var.get() and not self.free_service.configured:
                self.free_mode_var.set(False)
                feedback.set('本机尚未开通免费测试连接。')
                return
            try:
                self.save_ui_preferences(free_mode=self.free_mode_var.get())
            except OSError:
                feedback.set('本次切换已生效，但未能保存。')
                return
            self.cancel_auto_timer()
            self.auto_request += 1
            self.provider_var.set(('代码口述 · ' if self.convention_language() != 'off' else '') + self._provider_label())
            feedback.set('已启用免费在线服务；停止录音后返回文字。' if self.free_mode_var.get() else '已切回自己配置的语音和文字模型。')
        self.free_settings_toggle = ttk.Checkbutton(row, text='使用免费服务', variable=self.free_mode_var, command=changed)
        self.free_settings_toggle.pack(side='left')
        self.free_settings_changed = changed
        def check():
            test.state(['disabled'])
            feedback.set('正在检查免费连接和文字模型…')
            def work():
                state = self.free_service.call('/v1/status')
                if not state.get('asr_configured'):
                    raise QwenError('免费语音尚未配置，请联系维护者。')
                try:
                    FreeClient(self.free_service).check_connection()
                    return '连接正常，文字模型可用；语音效果在录音后确认。'
                except QwenError as exc:
                    return '连接正常，语音已配置。'+str(exc)
            def done(state, value):
                if win.winfo_exists():
                    test.state(['!disabled'])
                    feedback.set(value)
            self.run_worker('check', done, work)
        test = self.button(row, '检查免费服务', check, style='Ghost.TButton')
        test.pack(side='right')
        ttk.Label(box, textvariable=feedback, style='Muted.TLabel', wraplength=580).pack(anchor='w', pady=(6, 4))
        ttk.Label(box, text=NOTICE+' 录音与待处理文字会发送到在线服务。', style='Muted.TLabel', wraplength=580).pack(anchor='w', pady=(0, 18))

    def _build_mac_setup(self, box, win):
        self._settings_description(box, "一次准备，按需授权",
            "建议先把 TalkMan.app 放到“应用程序”并从那里启动。麦克风用于录音；辅助功能只用于填入其他应用，可稍后设置。")
        feedback = tk.StringVar(value="检测不会录音或访问其他应用正文。系统授权需由你亲自确认。")
        self.mac_setup_feedback = feedback
        self._settings_footer(box, win, feedback)
        info = tk.StringVar()
        ttk.Label(box, textvariable=info, style="Muted.TLabel", wraplength=585).pack(anchor="w", pady=(0, 18))
        self.mac_setup_status = info
        callbacks = queue.SimpleQueue()
        pending = [False]

        def open_page(kind):
            feedback.set(open_permission_settings(kind))

        def allow_microphone():
            if pending[0] or self.busy or self._mic_permission_pending:
                feedback.set("请先结束当前操作，或完成已打开的麦克风授权。")
                return
            pending[0] = True
            microphone_button.state(["disabled"])
            self._mic_permission_request += 1
            request = self._mic_permission_request
            self._mic_permission_pending = True

            def completed(allowed, message):
                # 用主窗口事件队列解除等待；关闭设置窗口也不会遗留锁定状态。
                self.events.put(("mic_permission", request, allowed, message))
                callbacks.put((allowed, message))

            try:
                ready = prepare_microphone_permission(completed)
                if ready:
                    self._mic_permission_pending = False
                    callbacks.put((True, "麦克风已允许；回到正文后再开始录音。"))
                else:
                    feedback.set("请确认系统的麦克风授权；本次不会开始录音。")
            except VoicePermissionError as exc:
                pending[0] = False
                self._mic_permission_pending = False
                microphone_button.state(["!disabled"])
                feedback.set(str(exc) + " 可点击下方“麦克风设置”。")

        for title, description, kind in (
            ("麦克风", "只在你开始录音时使用，不需要屏幕录制或语音识别权限。", "microphone"),
            ("辅助功能（可选）", "让 Talk Man 聚焦 Agent 输入框并粘贴；不会按回车发送。", "accessibility"),
        ):
            self._settings_heading(box, title)
            ttk.Label(box, text=description, style="Muted.TLabel", wraplength=585).pack(anchor="w", pady=(0, 8))
            row = ttk.Frame(box)
            row.pack(fill="x", pady=(0, 20))
            if kind == "microphone":
                microphone_button = self.button(row, "授权麦克风", allow_microphone)
                microphone_button.pack(side="left", padx=(0, 8))
                self.mac_setup_microphone_button = microphone_button
            self.button(row, "麦克风设置" if kind == "microphone" else "辅助功能设置",
                        lambda kind=kind: open_page(kind)).pack(side="left")

        ttk.Label(box, text="辅助功能列表中没有 TalkMan？点“＋”添加正在运行的应用。若仍未生效，退出后从“应用程序”重新打开。",
                  style="Muted.TLabel", wraplength=585).pack(anchor="w")

        job = [None]

        def cancel_poll(event):
            if event.widget == win and job[0] is not None:
                try:
                    win.after_cancel(job[0])
                except tk.TclError:
                    pass
                job[0] = None

        win.bind("<Destroy>", cancel_poll, add=True)

        def poll():
            job[0] = None
            try:
                if not win.winfo_exists():
                    return
                while not callbacks.empty():
                    allowed, message = callbacks.get_nowait()
                    pending[0] = False
                    microphone_button.state(["!disabled"])
                    feedback.set("麦克风已允许；回到正文后再开始录音。" if allowed else message)
                status = check_permissions()
                location = str(status.app) if status.app else "源码启动：权限可能归属终端或 Python；建议使用 TalkMan.app。"
                info.set(f"macOS {status.version}\n麦克风：{status.microphone_label}    辅助功能：{status.accessibility_label}\n运行位置：{location}")
                job[0] = win.after(1000, poll)
            except tk.TclError:
                return
        poll()

    def _settings_footer(self, parent, win, feedback):
        """先分配底部空间，内容区域无需拖动侧边滑块。"""
        footer = ttk.Frame(parent)
        footer.pack(side="bottom", fill="x", pady=(16, 0))
        ttk.Separator(footer, style='Settings.TSeparator').pack(fill='x', pady=(0, 12))
        ttk.Label(footer, textvariable=feedback, style="Muted.TLabel", wraplength=585).pack(anchor="w", pady=(0, 12))
        actions = ttk.Frame(footer)
        actions.pack(fill="x")
        self.button(actions, "关闭", lambda: self._close_settings(win)).pack(side="right")
        return actions

    def _settings_field(self, parent, label, variable, *, secret=False, values=None, readonly=False):
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=(0, 10))
        name = ttk.Label(row, text=label, width=21, style='SettingsField.TLabel')
        name.pack(side="left", padx=(0, 8))
        if values is None:
            entry = ttk.Entry(row, textvariable=variable, show="●" if secret else "")
        else:
            entry = ttk.Combobox(row, textvariable=variable, values=values, state="readonly" if readonly else "normal")
        entry.pack(side="left", fill="x", expand=True)
        if label in FIELD_HELP:
            self.hover_help.attach(name, FIELD_HELP[label])
            self.hover_help.attach(entry, FIELD_HELP[label])
        return entry

    def _target_diagnostic_text(self):
        controller = self.target_controller
        report = controller.refresh_report if controller else self.target_platform_note
        executable = Path(sys.executable)
        version = version_label() + ('' if getattr(sys, 'frozen', False) else '（源码运行）')
        if sys.platform == "darwin" and getattr(sys, "frozen", False):
            try:
                with (executable.parent.parent / "Info.plist").open("rb") as stream:
                    info = plistlib.load(stream)
                version = f"{info.get('CFBundleShortVersionString', '未知')}（构建号 {info.get('CFBundleVersion', '未知')}）"
            except (OSError, ValueError, plistlib.InvalidFileException):
                version = "无法读取应用版本"
        return f"Talk Man 目标窗口诊断\n运行版本：{version}\n运行程序：{executable}\n{report}"

    def _build_target_settings(self, box, win):
        self._settings_description(box, "把文字送到目标", "普通模式只填入；同步模式持续更新输入框，点击「下发」才按回车发送。")
        def clear_changed():
            try:
                self.save_ui_preferences(clear_after_fill=self.clear_after_fill_var.get())
                self.status_var.set('已记住填入后清空设置')
            except OSError:
                self.clear_after_fill_var.set(self.ui_preferences.get('clear_after_fill', False) is True)
                self.status_var.set('设置保存失败，已恢复原开关')
        self.clear_after_fill_switch = ttk.Checkbutton(box, text='成功填入／下发后，自动清空本次正文',
            variable=self.clear_after_fill_var, command=clear_changed)
        self.clear_after_fill_switch.pack(anchor='w', pady=(0, 6))
        ttk.Label(box, text='成功并存入历史后清空，可撤销；失败或期间新增的文字保留。',
            style='Muted.TLabel', wraplength=600).pack(anchor='w', pady=(0, 12))
        available = self.target_available
        feedback = tk.StringVar(value=self.target_platform_note)
        self.target_settings_feedback = feedback
        actions = self._settings_footer(box, win, feedback)

        sync_row = ttk.Frame(box)
        sync_row.pack(fill='x', pady=(0, 8))
        sync_draft = tk.BooleanVar(value=self.sync_mode_var.get())
        def sync_changed():
            if not self.set_sync_mode(sync_draft.get()):
                sync_draft.set(self.sync_mode_var.get())
            feedback.set(self.status_var.get())
        self.sync_mode_toggle = ttk.Checkbutton(sync_row, text='实时同步到目标输入框', variable=sync_draft, command=sync_changed)
        self.sync_mode_toggle.pack(side='left')
        self.button(sync_row, '重新连接同步', lambda: self.set_sync_mode(True), style='Ghost.TButton').pack(side='right')
        ttk.Label(box, text='保留目标已有文字，在末尾接续同步。目标被手动修改或下发后会暂停同步。',
                  style='Muted.TLabel', wraplength=580).pack(anchor='w', pady=(0, 12))

        self._settings_heading(box, '目标输入框')
        ttk.Label(box, text="当前目标", style="SettingsField.TLabel").pack(anchor="w", pady=(0, 6))
        ttk.Label(box, textvariable=self.target_label_var, wraplength=575).pack(anchor="w", pady=(0, 18))
        heading = ttk.Frame(box)
        heading.pack(fill="x", pady=(0, 10))
        ttk.Label(heading, text="已打开的 Agent", style='SettingsField.TLabel').pack(side="left")
        only_agents = tk.BooleanVar(value=self.only_agents_var.get())
        self.target_only_agents_draft = only_agents

        def filter_changed():
            try:
                self.save_ui_preferences(only_agents=only_agents.get())
            except OSError:
                only_agents.set(self.only_agents_var.get())
                feedback.set("筛选设置保存失败，请检查数据目录")
                return
            self.only_agents_var.set(only_agents.get())
            self.target_picker.close()
            if not refreshing[0]:
                refreshed("ok", self.target_window_rows())

        self.target_filter_button = ttk.Checkbutton(heading, text="只显示 Agent", variable=only_agents,
                                                    command=filter_changed)
        self.target_filter_button.pack(side="right")
        ttk.Label(box, text="按应用名称和窗口标题识别；找不到目标时可关闭筛选。",
                  style="Muted.TLabel").pack(anchor="w", pady=(0, 6))

        listing = ttk.Frame(box)
        listing.pack(fill="both", expand=True)
        listing.rowconfigure(0, weight=1)
        listing.columnconfigure(0, weight=1)
        # 目标名只有一行，不使用投递记录的多行高度；小屏仍可见四个目标。
        ttk.Style(win).configure('Targets.Treeview', rowheight=40)
        candidates = ttk.Treeview(listing, show="tree", height=4, selectmode="browse", takefocus=True, style='Targets.Treeview')
        candidates.column("#0", width=350, minwidth=100, stretch=True)
        candidates.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(listing, orient="vertical", command=candidates.yview)
        scrollbar.grid(row=0, column=1, sticky="ns", padx=(8, 0))

        def scroll(first, last):
            if float(first) <= 0 and float(last) >= 1:
                scrollbar.grid_remove()
            else:
                scrollbar.grid()
            scrollbar.set(first, last)

        candidates.configure(yscrollcommand=scroll)
        self.target_candidates = candidates
        rows = {}
        refreshing = [False]

        def alive():
            try:
                return bool(win.winfo_exists() and candidates.winfo_exists())
            except tk.TclError:
                return False

        def selected(_event=None):
            selection = candidates.selection()
            valid = bool(available and not refreshing[0] and selection and selection[0] in rows)
            choose_button.state(["!disabled"] if valid else ["disabled"])
            if valid:
                item = rows[selection[0]]
                feedback.set(item.get("label") or " · ".join(filter(None, (item.get("app"), item.get("title")))))

        def refreshed(state, value):
            if not alive():
                return
            refreshing[0] = False
            refresh_button.state(["!disabled"])
            rows.clear()
            for row in candidates.get_children():
                candidates.delete(row)
            if state != "ok":
                feedback.set(str(value))
            else:
                for index, item in enumerate(value or []):
                    if not isinstance(item, dict):
                        continue
                    row = str(index)
                    rows[row] = item
                    label = item.get("label") or " · ".join(filter(None, (item.get("app"), item.get("title"))))
                    candidates.insert("", "end", iid=row, text=label)
                note = self.target_controller.refresh_note if self.target_controller else ""
                feedback.set(note or (f"找到 {len(rows)} 个窗口，选中后设为目标。" if rows else "未发现 Agent 窗口。请打开后再刷新。"))
            selected()

        def refresh():
            if refreshing[0] or not available:
                return
            refreshing[0] = True
            refresh_button.state(["disabled"])
            choose_button.state(["disabled"])
            feedback.set("正在查找已打开的 Agent 窗口…")
            try:
                self.refresh_target_windows(refreshed)
            except Exception:
                refreshed("error", "暂时无法刷新窗口，请稍后重试。")

        def choose():
            selection = candidates.selection()
            if refreshing[0] or not selection or selection[0] not in rows:
                return
            try:
                message = self.choose_target(rows[selection[0]]["id"])
                feedback.set(message or "目标已保存，可回到编辑器填入文字。")
            except Exception as ex:
                feedback.set(str(ex) or "目标保存失败，请重新刷新后选择。")

        def clear():
            try:
                self.clear_target()
                candidates.selection_remove(*candidates.selection())
                selected()
                feedback.set("已清除目标，文字会继续保留在预输入框。")
            except Exception as ex:
                feedback.set(str(ex) or "清除目标失败，请稍后重试。")

        def copy_diagnostics():
            self.clipboard_clear()
            self.clipboard_append(self._target_diagnostic_text())
            feedback.set("已复制窗口诊断。")

        refresh_button = self.button(heading, "刷新窗口", refresh)
        refresh_button.pack(side="right")
        if sys.platform == "darwin":
            diagnostic_button = self.button(heading, "复制诊断", copy_diagnostics, icon="copy")
            diagnostic_button.pack(side="right", padx=(0, 8))
            self.target_diagnostic_button = diagnostic_button
        choose_button = self.button(actions, "设为目标", choose, style="Accent.TButton")
        choose_button.pack(side="left", padx=(0, 8))
        choose_button.state(["disabled"])
        clear_button = self.button(actions, "清除目标", clear)
        clear_button.pack(side="left")
        self.target_refresh_button = refresh_button
        self.target_choose_button = choose_button
        self.target_clear_button = clear_button
        candidates.bind("<<TreeviewSelect>>", selected)
        if not available:
            refresh_button.state(["disabled"])

    def _build_asr_settings(self, box, win):
        self._settings_description(box, "把声音变成文字", "录音和覆盖、麦克风输入共用此服务；指令修改会在识别后继续调用文字模型。")
        self.asr_provider_choice = tk.StringVar(value=PROVIDERS[self.asr_settings.provider].label)
        self.asr_provider_combobox = self._settings_field(box, "识别服务", self.asr_provider_choice,
            values=tuple(spec.label for name, spec in PROVIDERS.items() if name != 'free'), readonly=True)
        note = tk.StringVar()
        ttk.Label(box, textvariable=note, wraplength=585, style="Muted.TLabel").pack(anchor="w", pady=(4, 18))
        feedback = tk.StringVar()
        actions = self._settings_footer(box, win, feedback)
        fields = ttk.Frame(box)
        fields.pack(fill="x")
        profiles = {name: self.asr_settings.credentials(name) for name in PROVIDERS}
        selected = [self.asr_settings.provider]
        storage_supported = secure_storage_supported()
        remember = tk.BooleanVar(value=storage_supported and asr_keys_remembered(self.data_dir))
        remember_label = "在这台电脑记住凭证（系统钥匙串）" if sys.platform == "darwin" else "在这台电脑加密记住凭证"
        remember_box = ttk.Checkbutton(box, text=remember_label, variable=remember)
        self.asr_remember_var = remember
        self.asr_remember_box = remember_box
        self.asr_feedback = feedback
        if not storage_supported:
            remember_box.state(["disabled"])

        def collect():
            profiles[selected[0]] = {name: variable.get() for name, variable in self.asr_fields.items()}

        def show_fields(_event=None):
            collect()
            provider = next((name for name, spec in PROVIDERS.items() if spec.label == self.asr_provider_choice.get()), selected[0])
            selected[0] = provider
            for child in fields.winfo_children():
                child.destroy()
            self.asr_fields = {}
            spec = PROVIDERS[provider]
            note.set(spec.note if provider != "qwen" else "千问可选实时转写或录后识别，与文字处理共用一份连接设置。")
            storage_note = "未记住的凭证仅本次运行使用" if storage_supported else "本次运行使用凭证"
            if not storage_supported and sys.platform == "darwin":
                storage_note = "钥匙串组件缺失，请用新版包重新构建"
            feedback.set(self.asr_credentials_error or f"每轮最多 {spec.seconds} 秒 · {storage_note}")
            if provider == "qwen":
                remember_box.pack_forget()
                ttk.Label(fields, text="无需重复填写 Key", style='SettingsTitle.TLabel').pack(anchor="w", pady=(8, 6))
                ttk.Label(fields, text="在相邻的「文字改写模型」页统一设置千问 Key、服务地址和识别方式。",
                    style="Muted.TLabel", wraplength=585).pack(anchor="w", pady=(0, 16))
                self.button(fields, "设置千问连接", self.open_text_settings, icon="settings").pack(anchor="w")
            else:
                for item in spec.fields:
                    variable = tk.StringVar(value=profiles[provider].get(item.name, item.default))
                    self.asr_fields[item.name] = variable
                    self._settings_field(fields, item.label, variable, secret=item.secret)
                remember_box.pack(anchor="w", pady=(12, 0))

        def save():
            if self.busy:
                feedback.set("请在本轮录音和处理结束后保存。")
                return
            collect()
            try:
                if self.asr_credentials_error and asr_keys_remembered(self.data_dir) and remember.get():
                    raise QwenError("已记住的语音凭证尚未读取，保存已停止。请点击「读取已记住凭证」并允许钥匙串访问；取消记忆会删除原凭证。")
                new = validate(ASRSettings(selected[0], profiles))
                save_asr_settings(self.data_dir, new, remember.get())
                self.asr_settings = new
                self.asr_credentials_error = ""
                self.provider_var.set(('代码口述 · ' if self.convention_language() != 'off' else '') + PROVIDERS[new.provider].label)
                self.status_var.set(f"语音已切换为{PROVIDERS[new.provider].label}；首次录音时验证连接")
                self._close_settings(win)
            except QwenError as ex:
                feedback.set(str(ex))
            except Exception:
                feedback.set("语音设置保存失败，请检查数据目录权限。")

        self.asr_save = save
        self.button(actions, "保存语音设置", save, style="Accent.TButton").pack(side="left")
        self.asr_provider_combobox.bind("<<ComboboxSelected>>", show_fields)
        # 先保存原配置，避免首次绘制时把凭证清空。
        self.asr_fields = {name: tk.StringVar(value=value) for name, value in profiles[selected[0]].items()}
        show_fields()

        def restore_credentials():
            if self.busy:
                feedback.set("请在本轮录音和处理结束后读取。")
                return
            try:
                restored = load_asr_settings(self.data_dir, allow_keychain_prompt=True)
                if not any(restored.credentials(name).get(item.name) for name, spec in PROVIDERS.items()
                           for item in spec.fields if item.secret):
                    raise QwenError("没有找到已记住的语音凭证，请重新填写。")
                for name, spec in PROVIDERS.items():
                    for item in spec.fields:
                        if item.secret:
                            value = restored.credentials(name).get(item.name, "")
                            profiles[name][item.name] = value
                            if name == selected[0] and item.name in self.asr_fields:
                                self.asr_fields[item.name].set(value)
                self.asr_settings = restored
                self.asr_credentials_error = ""
                feedback.set("已从系统钥匙串读取凭证。")
            except QwenError as ex:
                feedback.set(str(ex))

        self.asr_restore_credentials = restore_credentials
        if sys.platform == "darwin" and storage_supported and self.asr_credentials_error:
            self.button(actions, "读取已记住凭证", restore_credentials).pack(side="left", padx=(8, 0))

    def _build_text_settings(self, box, win):
        self._settings_description(box, '用指令操作 Talk Man', '同一个按钮，既能改文字，也能操作应用；无需先聚焦正文。')
        ttk.Label(box, text='可以说“停下以后再晚几秒整理”“把上一条历史放回来”。\n没听明白会在原窗口问你，可继续说、点选或取消。说“指令帮助”查看例句。',
            style='SettingsHelp.TLabel', wraplength=560).pack(anchor='w', pady=(0, 10))
        self.command_guide_button = self.button(box, '看看指令怎么用', lambda:self.show_quick_guide(2),
            style='Outline.TButton', tooltip='查看改文字、操作应用和继续对话的例子。')
        self.command_guide_button.pack(anchor='w',pady=(0,20))
        review = self._settings_group(box, '改写结果怎么应用')
        ttk.Style(win).configure('ReviewChoice.TRadiobutton', padding=(0, 4), font=(self.fonts.ui, self.font_size(10), 'bold'))
        self.preview_ai_switch = ttk.Radiobutton(review, text='先看对照，确认后替换',
            variable=self.preview_ai_var, value=True, command=self.preview_preference_changed,
            style='ReviewChoice.TRadiobutton')
        self.preview_ai_switch.pack(fill='x')
        ttk.Label(review, text='原文在左，结果在右；只标出改动。',
            style='SettingsHelp.TLabel', wraplength=530).pack(anchor='w', padx=20, pady=(2, 14))
        self.preview_ai_direct = ttk.Radiobutton(review, text='直接替换，用「上一步」撤销',
            variable=self.preview_ai_var, value=False, command=self.preview_preference_changed,
            style='ReviewChoice.TRadiobutton')
        self.preview_ai_direct.pack(fill='x')
        ttk.Label(review, text='适用于指令修改、翻译、AI 整理；录音覆盖直接写入。',
            style='SettingsHelp.TLabel', wraplength=530).pack(anchor='w', padx=20, pady=(2, 0))
        automatic = self._settings_group(box, '停止输入后，自动整理全文')
        delay_row = ttk.Frame(automatic)
        delay_row.pack(fill='x', pady=(0, 12))
        ttk.Label(delay_row, text='停下输入').pack(side='left')
        delay = ttk.Combobox(delay_row, textvariable=self.auto_delay_var, values=(1, 3, 5, 10, 20, 30, 60), state='readonly', width=5)
        delay.pack(side='left', padx=8)
        ttk.Label(delay_row, text='秒再整理 · 继续输入会重新计时', style='Muted.TLabel').pack(side='left')
        def delay_changed(_event=None):
            try:
                self.save_ui_preferences(auto_delay=self.auto_delay_var.get())
            except OSError:
                self.auto_delay_var.set(self.ui_preferences.get('auto_delay', 5))
                self.status_var.set('延迟保存失败，已恢复原设置')
            self.schedule_auto()
        delay.bind('<<ComboboxSelected>>', delay_changed)
        self.auto_delay_choice = delay
        processing = ttk.Frame(automatic)
        processing.pack(fill='x')
        for i, (name, label, variable, explanation) in enumerate((
            ('filler_switch', '去语气词', self.filler_var, '自动去掉多余的“嗯、呃”，保留有意义的表达。'),
            ('punct_switch', '自动标点', self.punct_var, '自动按语义补充或修正标点。'),
            ('layout_switch', 'AI 分行排版', self.layout_var, '把编号和同类条目另起一行，不把普通叙述强行拆开。'),
            ('rewrite_switch', 'AI 整理语序', self.rewrite_var, '轻度理顺口误、重复和语序，尽量保留原意、结构和说话方式。'),
        )):
            switch = ttk.Checkbutton(processing, text=label, variable=variable, command=self.auto_changed)
            switch.grid(row=i//2, column=i%2, sticky='w', padx=(0, 32), pady=(0, 12))
            setattr(self, name, switch)
            self.hover_help.attach(switch, explanation + ' 停止输入后处理全文，开关会立即记住。')
        self.direct_request_switch = ttk.Checkbutton(automatic, text='去客气表达 · 明确想做的事，直接说',
            variable=self.direct_request_var, command=self.auto_changed)
        self.direct_request_switch.pack(anchor='w', pady=(4, 4))
        ttk.Label(automatic, text='配合去语气词使用：去掉明确要求里的“我觉得、能不能”。真正询问能否做到、尚未决定的事，保留原意。',
            style='SettingsHelp.TLabel', wraplength=530).pack(fill='x')
        self.hover_help.attach(self.direct_request_switch, '例如“我觉得能不能改成黑色，就按这个做”变为“改成黑色，就按这个做”。不会把真正的提问变成命令。')
        speaker = self._settings_group(box, '说话人角色')
        self.speaker_enabled_input = tk.BooleanVar(value=self.speaker_enabled_var.get())
        self.speaker_name_input = tk.StringVar(value=self.speaker_name_var.get())
        self.speaker_feedback = tk.StringVar(value='只处理之后的新录入；引用、代码和“我们”保持原样。')
        self.speaker_switch = ttk.Checkbutton(speaker, text='用名字代替本人“我”', variable=self.speaker_enabled_input)
        self.speaker_switch.pack(anchor='w', pady=(0, 8))
        ttk.Label(speaker, text='说话人名字 · 例如 Steve', style='SettingsHelp.TLabel').pack(anchor='w')
        self.speaker_name_entry = ttk.Entry(speaker, textvariable=self.speaker_name_input, width=20)
        self.speaker_name_entry.pack(fill='x', pady=(4, 8))
        self.speaker_save_button = self.button(speaker, '保存角色', self.save_speaker_settings,
            tooltip='保存后，新的录音和覆盖会用这个名字表达；已有文字与语音指令不变。')
        self.speaker_save_button.pack(anchor='w', pady=(0, 8))
        ttk.Label(speaker, text='“我去公司” → “Steve去公司”。\n录音结束后转换，需文字模型；失败保留原听写。',
            style='SettingsHelp.TLabel', wraplength=530).pack(fill='x')
        ttk.Label(speaker, textvariable=self.speaker_feedback,
            style='SettingsHelp.TLabel', wraplength=530).pack(fill='x', pady=(6, 0))
        translation = self._settings_group(box, '翻译')
        self.build_translation_settings(translation)
        footer = self._settings_footer(box, win, tk.StringVar(value='整理与预览立即记住；角色请点「保存角色」。'))
        self.button(footer, '模型与 API Key', lambda: self.settings_notebook.select(self.settings_tabs['model'])).pack(side='left')

    def _build_model_settings(self, box, win):
        self._build_free_service_settings(box, win)
        self._settings_description(box, "文字模型与千问连接", "指令改写与全文整理共用文字模型；千问语音也使用下方连接。")
        feedback = tk.StringVar(value=self.qwen_credentials_error or "Key 未记住时仅本次运行使用；语音连接在首次录音时验证。")
        actions = self._settings_footer(box, win, feedback)
        fields = ttk.Frame(box)
        fields.pack(fill="x")
        key = tk.StringVar(value=self.settings.api_key)
        origin = tk.StringVar(value=self.settings.service_origin)
        model = tk.StringVar(value=self.settings.text_model)
        mode = tk.StringVar(value="录后识别" if self.settings.voice_mode == "file" else "实时转写")
        file_model = tk.StringVar(value=self.settings.file_model)
        asr = tk.StringVar(value=self.settings.asr_model)
        storage_supported = secure_storage_supported()
        remember = tk.BooleanVar(value=storage_supported and qwen_key_remembered(self.data_dir))
        self.text_settings_fields = {"key": key, "origin": origin, "text_model": model,
            "voice_mode": mode, "file_model": file_model, "asr_model": asr}
        self.text_settings_feedback = feedback
        self.text_remember_var = remember
        self._settings_field(fields, "千问通用 API Key", key, secret=True)
        remember_label = "在这台电脑记住 Key（系统钥匙串）" if sys.platform == "darwin" else "在这台电脑加密记住 Key"
        remember_box = ttk.Checkbutton(fields, text=remember_label, variable=remember)
        remember_box.pack(anchor="w", pady=(0, 12))
        self.text_remember_box = remember_box
        if not storage_supported:
            remember_box.state(["disabled"])
            if sys.platform == "darwin":
                feedback.set("钥匙串组件缺失，请用新版 Mac 包重新构建；当前 Key 仅本次运行使用。")
        self._settings_field(fields, "服务地址", origin, values=(
            "https://ws-pmganpahtj36kvv0.cn-beijing.maas.aliyuncs.com", "https://maas.qianwenaiapi.com"))
        self._settings_field(fields, "文字模型", model,
            values=("qwen-plus", "qwen3.8-flash", "qwen3.7-plus", "qwen3.8-max"))
        self._settings_heading(fields, '千问语音')
        self._settings_field(fields, "识别方式", mode, values=("录后识别", "实时转写"), readonly=True)
        self._settings_field(fields, "录后识别模型", file_model)
        self._settings_field(fields, "实时转写模型", asr, readonly=True, values=(
            "qwen-audio-3.0-asr-flash-streaming", "fun-asr-realtime", "paraformer-realtime-v2"))

        def values():
            api_key = key.get().strip()
            if not api_key.startswith("sk-") or any(char.isspace() for char in api_key):
                raise ValueError("请填写有效的千问通用 API Key。")
            if api_key.startswith("sk-sp-"):
                raise ValueError("请使用通用 API Key；套餐专属 Key 暂不支持。")
            if not model.get().strip():
                raise ValueError("请填写文字模型名称。")
            return QwenSettings(api_key=api_key, asr_model=asr.get().strip(), text_model=model.get().strip(),
                file_model=file_model.get().strip(), voice_mode="file" if mode.get() == "录后识别" else "stream",
                service_origin=origin.get().strip().rstrip("/"))

        def save():
            if self.busy:
                feedback.set("请在本轮录音和处理结束后保存。")
                return
            try:
                new = values()
                save_settings(self.data_dir, new, remember.get())
                self.settings = new
                self.qwen_credentials_error = ""
                self.auto_request += 1
                self.auto_running = False
                self.refresh_cancel_button()
                self.status_var.set("千问连接与文字设置已保存")
                self._close_settings(win)
            except (QwenError, ValueError) as ex:
                feedback.set(str(ex))
            except Exception:
                feedback.set("设置保存失败，请检查数据目录权限。")

        def check():
            if self.busy:
                feedback.set("请在本轮录音和处理结束后测试。")
                return
            try:
                new = values()
                client = self.client_factory(new)
            except (ValueError, QwenError) as ex:
                feedback.set(str(ex))
                return
            test_button.state(["disabled"])
            feedback.set("正在测试文字连接，会产生少量调用费用…")

            def finished(state, value):
                if win.winfo_exists():
                    test_button.state(["!disabled"])
                    feedback.set("文字连接成功；语音将在首次录音时验证。" if state == "ok" else value)

            self.run_worker("check", finished, client.check_connection)

        self.text_settings_save = save
        self.button(actions, "保存千问设置", save, style="Accent.TButton").pack(side="left", padx=(0, 8))
        test_button = self.button(actions, "测试文字连接", check)
        test_button.pack(side="left")

        def restore_key():
            if self.busy:
                feedback.set("请在本轮录音和处理结束后读取。")
                return
            try:
                restored = load_settings(self.data_dir, allow_keychain_prompt=True)
                if not restored.api_key:
                    raise QwenError("没有找到已记住的千问 Key，请重新填写。")
                key.set(restored.api_key)
                self.settings.api_key = restored.api_key
                self.qwen_credentials_error = ""
                feedback.set("已从系统钥匙串读取 Key。")
            except QwenError as ex:
                feedback.set(str(ex))

        self.text_restore_key = restore_key
        if sys.platform == "darwin" and storage_supported and self.qwen_credentials_error:
            self.button(actions, "读取已记住凭证", restore_key).pack(side="left", padx=(8, 0))

    def _build_controls_settings(self, box, win):
        self._settings_description(box, "当前窗口按键", "在 TalkMan 内操作，无需先点正文；单独空格／Ctrl 不会占用其他软件的键。")
        feedback = tk.StringVar(value="录音支持点按或按住。清空编辑区默认 F8，可用上一步恢复。")
        actions = self._settings_footer(box, win, feedback)
        draft = {**DEFAULT_SHORTCUTS, **self.shortcuts}
        self.shortcut_fields = {}
        self.shortcut_draft = draft
        self.controls_feedback = feedback
        ttk.Style(win).configure("SettingsClear.TButton", padding=(8, 3))
        for action in DEFAULT_SHORTCUTS:
            if action in GLOBAL_ACTIONS:
                continue
            row = ttk.Frame(box)
            row.pack(fill="x", pady=(0, 6))
            ttk.Label(row, text=ACTION_LABELS[action], width=13, style='SettingsField.TLabel').pack(side="left")
            value = tk.StringVar(value=display_shortcut(draft[action]))
            entry = ttk.Entry(row, textvariable=value, state="readonly", width=24)
            entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
            self.shortcut_fields[action] = (entry, value)

            def capture(event, action=action, value=value):
                if event.keysym in ("Tab", "ISO_Left_Tab"):
                    return None
                try:
                    binding = shortcut_from_event(event)
                    if action in {"clear", "focus", "cursor_prev", "cursor_next"}:
                        validate_shortcuts({**draft, action: binding})
                    draft[action] = binding
                    value.set(display_shortcut(draft[action]))
                    feedback.set("保存后生效；重复绑定和编辑快捷键冲突会提示。")
                except ShortcutError as ex:
                    feedback.set(str(ex))
                return "break"

            entry.bind("<KeyPress>", capture)

            def clear(action=action, value=value):
                draft[action] = ""
                value.set("未设置")

            ttk.Button(row, text="清除", command=clear, style="SettingsClear.TButton").pack(side="left")

        self._settings_heading(box, '阅读与光标')
        wheel_enabled = tk.BooleanVar(value=self.wheel_caret_var.get())
        self.wheel_caret_draft = wheel_enabled
        ttk.Checkbutton(box, text="鼠标滚轮移动光标", variable=wheel_enabled).pack(anchor="w", pady=(0, 4))
        enabled = tk.BooleanVar(value=self.magnifier_var.get())
        self.magnifier_draft = enabled
        ttk.Checkbutton(box, text="悬停文字时局部放大", variable=enabled).pack(anchor="w")
        ttk.Label(box, text="点击后收起；移出文字区再进入即可重新显示。", style="Muted.TLabel").pack(anchor="w", pady=(4, 0))

        def save():
            if self.busy:
                feedback.set("请在本轮录音和处理结束后保存。")
                return
            try:
                self.shortcuts = save_shortcuts(self.data_dir, draft)
                self.cancel_hold()
                self.mic_key_down = None
                self.mic_key_pending = False
                self.refresh_shortcut_labels()
                temp = self.ui_path.with_suffix(".tmp")
                preferences = {**self.ui_preferences, "magnifier": enabled.get(), "wheel_caret": wheel_enabled.get()}
                temp.write_text(json.dumps(preferences, ensure_ascii=False), encoding="utf-8")
                temp.replace(self.ui_path)
            except (ShortcutError, OSError) as ex:
                feedback.set(str(ex) if isinstance(ex, ShortcutError) else "快捷键已保存；显示设置保存失败，请检查数据目录。")
                return
            self.ui_preferences = preferences
            self.magnifier_var.set(enabled.get())
            self.wheel_caret_var.set(wheel_enabled.get())
            self.magnifier.hide()
            self.status_var.set("快捷键与显示设置已保存")
            self._close_settings(win)

        def defaults():
            draft.update(DEFAULT_SHORTCUTS)
            for action, (_, value) in self.shortcut_fields.items():
                value.set(display_shortcut(draft[action]))
            feedback.set("已恢复默认快捷键，保存后生效。")

        self.controls_save = save
        self.button(actions, "保存快捷键与显示", save, style="Accent.TButton").pack(side="left", padx=(0, 8))
        self.button(actions, "恢复默认", defaults).pack(side="left")
        ttk.Label(box, text='想在其他软件里直接录音？到「窗口」设置后台唤起组合键。\n这里的录音／指令若也设为 Ctrl、Alt 或 Command 组合键，启用后台键后可两处使用。',
                  style='Muted.TLabel', wraplength=585).pack(anchor='w', pady=(16, 0))
        self.button(box, '设置后台唤起组合键 →', lambda: self.settings_notebook.select(self.settings_tabs['window']),
                    style='Ghost.TButton').pack(anchor='w', pady=(8, 0))

    def _build_window_settings(self, box, win):
        self._settings_description(box, '窗口与显示', '调整文字大小、窗口位置和后台唤起方式。')
        self._settings_heading(box, '设计外观')
        from talkman_app import THEME_LABELS
        row = ttk.Frame(box)
        row.pack(fill='x', pady=(0, 16))
        for key, label in THEME_LABELS.items():
            self.button(row, label, lambda k=key: self.set_theme(k), style='Outline.TButton',
                        tooltip='立即切换并记住外观。瑞士国际使用暖白、朱红和直线网格。').pack(side='left', padx=(0, 8))
        feedback = tk.StringVar(value='唤醒键只打开窗口；录音键可点按，也可按住后松手结束。')
        actions = self._settings_footer(box, win, feedback)
        self._settings_heading(box, '文字大小')
        scale_labels = {'标准 · 100%': 1.0, '较大 · 125%': 1.25, '最大 · 150%': 1.5}
        saved_scale = self.ui_preferences.get('ui_scale', self.ui_scale)
        scale_choice = tk.StringVar(value=next((label for label, factor in scale_labels.items()
                                                if factor == saved_scale), '标准 · 100%'))
        self.window_scale_choice = scale_choice
        scale_row = ttk.Frame(box)
        scale_row.pack(fill='x', pady=(0, 8))
        ttk.Label(scale_row, text='界面与正文', style='SettingsField.TLabel').pack(side='left')
        self.window_scale_combo = ttk.Combobox(scale_row, textvariable=scale_choice, values=list(scale_labels),
                                               state='readonly', width=20)
        self.window_scale_combo.pack(side='right')
        ttk.Label(box, text='保存后完全退出，再打开 Talk Man 即可生效。长文会在所选字号范围内略微缩小。',
                  style='SettingsHelp.TLabel', wraplength=560).pack(anchor='w', pady=(0, 12))
        self._settings_heading(box, '窗口位置')
        edge = tk.BooleanVar(value=self.edge_hide_var.get())
        pinned = tk.BooleanVar(value=self.topmost_var.get())
        voice = tk.BooleanVar(value=self.global_voice_var.get())
        self.window_drafts = (edge, pinned, voice)
        gesture = tk.StringVar(value=self.voice_gesture_var.get())
        self.window_gesture_draft = gesture
        edge_label = '启用手动收起' if sys.platform == 'darwin' else '启用贴边收起'
        for label, variable in ((edge_label, edge), ('窗口置顶', pinned), ('在其他软件中也能唤起并录音（全局快捷键）', voice)):
            ttk.Checkbutton(box, text=label, variable=variable).pack(anchor='w', pady=(0, 12))
        gestures = ('Mac 使用手动收起，点击文字标签展开；鼠标移开不会自动隐藏。' if sys.platform == 'darwin' else
                    '收起后悬停或点击文字标签展开；拖离停靠位置或在菜单中取消收起。')
        ttk.Label(box, text='拖动收起标签，可沿边移动或换边。\n'+gestures,
                  style='Muted.TLabel', wraplength=580).pack(anchor='w', pady=(0, 18))
        self.window_shortcut_draft = {key: self.shortcuts[key] for key in GLOBAL_ACTIONS}
        self._settings_heading(box, '后台唤起组合键')
        for action in GLOBAL_ACTIONS:
            row = ttk.Frame(box)
            row.pack(fill='x', pady=(0, 10))
            ttk.Label(row, text=ACTION_LABELS[action], width=15, style='SettingsField.TLabel').pack(side='left')
            value = tk.StringVar(value=display_shortcut(self.window_shortcut_draft[action]))
            entry = ttk.Entry(row, textvariable=value, state='readonly', width=24)
            entry.pack(side='left', fill='x', expand=True, padx=(0, 8))
            def capture(event, action=action, value=value):
                if event.keysym in ('Tab', 'ISO_Left_Tab'):
                    return None
                try:
                    binding = shortcut_from_event(event)
                    validate_shortcuts({**self.shortcuts, **self.window_shortcut_draft, action: binding})
                    self.window_shortcut_draft[action] = binding
                    value.set(display_shortcut(binding))
                    feedback.set('保存后生效。Mac 的 Alt 对应 Option。')
                except ShortcutError as error:
                    feedback.set(str(error))
                return 'break'
            entry.bind('<KeyPress>', capture)
            def clear(action=action, value=value):
                self.window_shortcut_draft[action] = ''
                value.set('未设置')
            self.button(row, '清除', clear, style='Ghost.TButton').pack(side='left')
        self._settings_heading(box, '录音按键的操作方式')
        modes = ttk.Frame(box)
        modes.pack(fill='x', pady=(4, 8))
        for label, value in (('点按＋按住', 'both'), ('仅点按启停', 'toggle'), ('仅按住录音', 'hold')):
            ttk.Radiobutton(modes, text=label, variable=gesture, value=value).pack(side='left', padx=(0, 12))
        ttk.Label(box, text='模式适用于全局录音／指令组合键。单独空格／Ctrl 仅在主窗口内按住使用。\n小窗本轮语音超过 120 字自动展开；处理完成 2 秒后收起，手动操作则保持打开。',
                  style='Muted.TLabel', wraplength=580).pack(anchor='w', pady=(4, 0))
        def save():
            if self.busy:
                feedback.set('请在本轮录音和处理结束后保存。')
                return
            previous = (self.edge_hide_var.get(), self.topmost_var.get(), self.global_voice_var.get(), self.voice_gesture_var.get())
            try:
                self.shortcuts = save_shortcuts(self.data_dir, {**self.shortcuts, **self.window_shortcut_draft})
                self.edge_hide_var.set(edge.get())
                self.topmost_var.set(pinned.get())
                self.global_voice_var.set(voice.get())
                self.voice_gesture_var.set(gesture.get())
                self.save_window_preferences(ui_scale=scale_labels[scale_choice.get()])
            except (ShortcutError, OSError) as error:
                for var, value in zip((self.edge_hide_var, self.topmost_var, self.global_voice_var, self.voice_gesture_var), previous):
                    var.set(value)
                feedback.set(str(error))
                return
            self.refresh_shortcut_labels()
            self.status_var.set('窗口设置已保存；重新打开 Talk Man 后应用新字号'
                                if scale_labels[scale_choice.get()] != self.ui_scale else '窗口与全局快捷键设置已保存')
            self._close_settings(win)
        self.window_settings_save = save
        self.window_settings_feedback = feedback
        self.button(actions, '保存窗口设置', save, style='Accent.TButton').pack(side='left')
