"""在线更新状态留在主应用内，关闭设置页不会中断下载。"""
import sys
import hashlib
import json
import threading
import tkinter as tk
from tkinter import ttk
import webbrowser

import talkman_updates as updates
from talkman_version import VERSION
from talkman_license import atomic_write


class UpdateUIMixin:
    def init_app_updates(self):
        self.update_info = None
        self.update_package = self.update_stage = self.update_installed = None
        self.update_phase = 'idle'
        self.update_cancel = threading.Event()
        self.update_job = None
        self.update_startup_job = None
        self.update_installing = False
        self.update_message = tk.StringVar(value='检查公开发布的 Desktop 版本，包括测试版。')
        self.update_auto = tk.BooleanVar(value=self.ui_preferences.get('check_updates', True) is not False)
        self.update_result_path = self.data_dir / 'update-result.txt'
        self.update_resume_job = self.after_idle(self.restore_update_workspace)
        if self.window_controller.native_enabled:
            self.update_startup_job = self.after(1200, self._update_startup_result)
        if (self.window_controller.native_enabled and getattr(sys, 'frozen', False) and self.update_auto.get()
                and self.ui_preferences.get('agreement_version') == 1):
            self.update_job = self.after(8000, self.check_app_update)

    def _update_startup_result(self):
        self.update_startup_job = None
        if self.closing:
            return
        updates.acknowledge_startup()
        def read_result():
            self.update_startup_job = None
            if self.closing:
                return
            try:
                message = self.update_result_path.read_text(encoding='utf-8-sig')[:500]
                if self.update_phase == 'idle':
                    self.update_message.set(message)
            except OSError:
                pass
        self.update_startup_job = self.after(2000, read_result)

    def _build_update_settings(self, box):
        group = self._settings_group(box, '在线升级')
        ttk.Label(group, textvariable=self.update_message, style='SettingsHelp.TLabel', wraplength=530).pack(anchor='w', pady=(0, 12))
        row = ttk.Frame(group)
        row.pack(fill='x')
        self.update_check_button = self.button(row, '检查更新', self.check_app_update, style='Ghost.TButton')
        self.update_check_button.pack(side='left')
        self.update_action_button = self.button(row, '下载更新', self._app_update_action)
        self.update_action_button.pack(side='left', padx=10)
        self.button(row, '下载页', lambda: webbrowser.open(updates.RELEASES_PAGE), style='Ghost.TButton').pack(side='right')
        def changed():
            try:
                self.save_ui_preferences(check_updates=self.update_auto.get())
            except OSError:
                self.update_message.set('选项暂未保存，请检查数据目录权限。')
            if not self.update_auto.get() and self.update_job:
                self.after_cancel(self.update_job)
                self.update_job = None
        ttk.Checkbutton(group, text='启动时检查更新（只提醒，不自动安装）', variable=self.update_auto,
                        command=changed).pack(anchor='w', pady=(14, 0))
        ttk.Label(group, text='升级保留正文、草稿、已记住的 Key、授权、快捷键和外观；重开后回到原来的文字位置。',
                  style='SettingsHelp.TLabel', wraplength=530).pack(anchor='w', pady=(8, 0))
        if sys.platform == 'darwin':
            ttk.Label(group, text='沿用当前安装位置，不主动重置系统权限。测试签名版本升级后，macOS 仍可能要求重新允许。',
                      style='SettingsHelp.TLabel', wraplength=530).pack(anchor='w', pady=(8, 0))
        self._refresh_update_buttons()

    def _refresh_update_buttons(self):
        check = getattr(self, 'update_check_button', None)
        action = getattr(self, 'update_action_button', None)
        if check and check.winfo_exists():
            check.state(['disabled'] if self.update_phase in ('checking', 'downloading', 'ready') else ['!disabled'])
        if action and action.winfo_exists():
            label = {'downloading': '取消下载', 'ready': '保存并重启升级'}.get(self.update_phase, '下载更新')
            self.set_button_label(action, label)
            action.state(['!disabled'] if self.update_phase in ('available', 'downloading', 'ready') else ['disabled'])

    def check_app_update(self):
        if self.update_job:
            self.after_cancel(self.update_job)
            self.update_job = None
        if self.closing or self.update_phase in ('checking', 'downloading', 'ready'):
            return
        self.update_phase = 'checking'
        self.update_message.set('正在检查更新…')
        self._refresh_update_buttons()
        def done(state, value):
            if self.closing:
                return
            self.update_info = value if state == 'ok' else None
            self.update_phase = 'available' if self.update_info else 'idle'
            self.update_message.set(f'发现 {value.version}，可下载并升级。' if self.update_info
                                    else '已是当前发布的最新版本。' if state == 'ok' else str(value))
            if self.update_info:
                self.status_var.set(f'有新版 {value.version} · 打开「设置 → 版本」升级')
            self._refresh_update_buttons()
        self.run_worker('check', done, lambda: updates.check_update(VERSION))

    def _app_update_action(self):
        if self.update_phase == 'downloading':
            self.update_cancel.set()
            self.update_message.set('正在取消下载…')
        elif self.update_phase == 'ready':
            self.install_app_update()
        elif self.update_phase == 'available':
            self.download_app_update()

    def download_app_update(self):
        if self.closing or self.update_phase != 'available':
            return
        try:
            installed = updates.installed_app_path()
        except updates.UpdateError as exc:
            self.update_message.set(str(exc))
            return
        self.update_phase = 'downloading'
        self.update_cancel.clear()
        self.update_message.set('正在下载安装包…')
        self._refresh_update_buttons()
        info, last_percent = self.update_info, [-1]
        def progress(count, total):
            percent = int(count * 100 / total)
            if percent != last_percent[0]:
                last_percent[0] = percent
                self.events.put(('check', show_progress, 'ok', percent))
        def show_progress(state, value):
            if not self.closing and self.update_phase == 'downloading' and not self.update_cancel.is_set():
                self.update_message.set(f'下载 {info.version} · {value}%' if value < 100 else '下载完成，正在校验并准备更新…')
        def work():
            package = stage = None
            try:
                package = updates.download_update(info, progress=progress, cancelled=self.update_cancel)
                if self.update_cancel.is_set():
                    raise updates.UpdateError('下载已取消。')
                stage = (updates.prepare_windows_update(package, installed) if info.platform == 'windows-x64'
                         else updates.prepare_mac_update(package, info.version, installed))
                if self.update_cancel.is_set():
                    raise updates.UpdateError('下载已取消。')
                return package, stage, installed
            except Exception:
                updates.discard_prepared_update(package, stage)
                raise
        def done(state, value):
            if self.closing or self.update_cancel.is_set():
                if state == 'ok':
                    updates.discard_prepared_update(value[0], value[1])
                if self.closing:
                    return
                state, value = 'error', '下载已取消，当前版本未改变。'
            if state == 'ok':
                self.update_package, self.update_stage, self.update_installed = value
                self.update_phase = 'ready'
                self.update_message.set(f'{info.version} 已准备好。点击“保存并重启升级”后才会安装。')
            else:
                self.update_phase = 'available'
                self.update_message.set(str(value))
            self._refresh_update_buttons()
        self.run_worker('check', done, work)

    def _update_busy(self):
        return (self.busy or self.auto_running or self.target_filling or self.ai_review is not None
                or self._mic_permission_pending or self.live_sync.inflight or self.live_sync.sending
                or self.convention_panel is not None or self.app_commands.active or self.position_picker.active)

    def save_update_workspace(self):
        from talkman_window import WindowPlatform
        self.update_idletasks()
        selected = self.get_selection()
        bounds = WindowPlatform(self).bounds()
        resume = dict(version=self.update_info.version if self.update_info else VERSION,
            digest=hashlib.sha256(self.current_text().encode('utf-8')).hexdigest(),
            caret=len(self.text.get('1.0', 'insert')),
            selection=[len(self.text.get('1.0', selected[0])), len(self.text.get('1.0', selected[1]))] if selected else None,
            scroll=max(0.0, min(1.0, self.text.yview()[0])),
            size=[max(200, self.winfo_width()), max(200, self.winfo_height())], position=[bounds.x, bounds.y])
        atomic_write(self.data_dir/'update-workspace.json', json.dumps(resume).encode('utf-8'))

    def restore_update_workspace(self):
        if self.update_resume_job:
            self.after_cancel(self.update_resume_job)
        self.update_resume_job = None
        path = self.data_dir/'update-workspace.json'
        if self.closing or not path.is_file():
            return
        try:
            if path.stat().st_size > 4096:
                raise ValueError
            resume = json.loads(path.read_text(encoding='utf-8'))
            if resume.get('version') != VERSION:
                return
            text = self.current_text()
            if resume.get('digest') != hashlib.sha256(text.encode('utf-8')).hexdigest():
                raise ValueError
            caret, selected = resume['caret'], resume['selection']
            if type(caret) is not int or not 0<=caret<=len(text):
                raise ValueError
            if selected is not None and not (isinstance(selected,list) and len(selected)==2 and all(type(x) is int for x in selected) and 0<=selected[0]<selected[1]<=len(text)):
                raise ValueError
            size, position = resume['size'], resume['position']
            if not (isinstance(size,list) and len(size)==2 and all(type(x) is int and 200<=x<=20000 for x in size)
                    and isinstance(position,list) and len(position)==2 and all(type(x) is int and abs(x)<=100000 for x in position)):
                raise ValueError
            scroll = resume['scroll']
            if type(scroll) not in (int,float) or not 0<=scroll<=1:
                raise ValueError
            self.geometry(f'{size[0]}x{size[1]}')
            self.update_idletasks()
            self.window_controller.platform.move_to(*position)
            self.window_controller.fit_to_screen(force=True)
            self.text.tag_remove('sel','1.0','end')
            if selected:
                self.text.tag_add('sel',f'1.0+{selected[0]}c',f'1.0+{selected[1]}c')
            self.text.mark_set('insert',f'1.0+{caret}c')
            self.text.yview_moveto(scroll)
            self.cancel_auto_timer()
            self.auto_block_revision = self.revision
            self.update_meta()
        except (OSError, ValueError, KeyError, TypeError, tk.TclError):
            pass  # 正文已由常规存储恢复，损坏的会话信息不能阻止启动。
        finally:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def install_app_update(self):
        if self.closing or self.update_phase != 'ready':
            return
        if self._update_busy():
            self.update_message.set('请先结束录音、文字处理、填入或待确认的修改，再安装更新。')
            return
        process = None
        try:
            self.save_update_workspace()
            self.update_result_path.unlink(missing_ok=True)
            ready, process = updates.launch_installer(self.update_package, self.update_stage,
                                                      self.update_installed, self.update_result_path)
            self.update_installing = True
            self.close_app()
            if not self.closing:
                process.terminate()
                self.update_installing = False
                self.update_message.set('正文尚未成功保存，未退出或安装。请处理保存问题后重试。')
                return
            # 只有 close_app 保存正文、草稿并真正关闭后才放行 helper。
            ready.touch()
        except (OSError, updates.UpdateError) as exc:
            if process:
                process.terminate()
            self.update_installing = False
            if not self.closing:
                self.update_message.set(str(exc))
            else:
                self.update_result_path.write_text('更新未安装，当前版本未改变。', encoding='utf-8')

    def close_app_updates(self):
        if self.update_resume_job:
            self.after_cancel(self.update_resume_job)
            self.update_resume_job = None
        self.update_cancel.set()
        if self.update_startup_job:
            self.after_cancel(self.update_startup_job)
            self.update_startup_job = None
        if self.update_job:
            self.after_cancel(self.update_job)
            self.update_job = None
        if not self.update_installing:
            updates.discard_prepared_update(self.update_package, self.update_stage)
