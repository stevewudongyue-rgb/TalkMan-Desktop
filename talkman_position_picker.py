"""在目标窗口上标出可编辑区域；点击红框后按原生控件身份确认。"""
import sys
import tkinter as tk

from talkman_input_positions import navigation_instruction
from talkman_system_style import keep_mac_windows_separate
from talkman_windows_coordinates import physical_screen_coordinates as physical_overlay_coordinates


RED = '#d71921'


def place_physical_mark(mark, x, y, width, height):
    import ctypes as c
    from ctypes import wintypes as w
    user = c.WinDLL('user32', use_last_error=True)
    user.GetAncestor.argtypes = [w.HWND, w.UINT]
    user.GetAncestor.restype = w.HWND
    user.SetWindowPos.argtypes = [w.HWND, w.HWND, c.c_int, c.c_int, c.c_int, c.c_int, w.UINT]
    user.SetWindowPos.restype = w.BOOL
    mark.update_idletasks()
    hwnd = user.GetAncestor(mark.winfo_id(), 2)
    user.GetWindowLongW.argtypes = [w.HWND, c.c_int]
    user.GetWindowLongW.restype = c.c_long
    user.SetWindowLongW.argtypes = [w.HWND, c.c_int, c.c_long]
    # 点击标记不激活 TalkMan，目标窗口继续留在前台。
    user.SetWindowLongW(hwnd, -20, user.GetWindowLongW(hwnd, -20) | 0x08000080)
    if not user.SetWindowPos(hwnd, w.HWND(-1), x, y, max(1, width), max(1, height), 0x0010):
        raise c.WinError(c.get_last_error())


class PositionPicker:
    def __init__(self, app):
        self.app = app
        self._active = False
        self.marks = []
        self.buttons = []
        self.generation = 0
        self.pending = False
        self.candidate = None
        self.target = None
        self.inspect_job = None
        self.inspect_pending = False
        self.mark_location = None
        self.main_state = None

    @property
    def active(self):
        return self._active

    def button(self, parent, compact=False):
        button = self.app.button(parent, '位置', self.toggle, style='Outline.TButton',
            tooltip='在 Agent 窗口上显示可点击的红框。点一个框就会记住；再点本按钮可取消。')
        self.buttons.append((button, compact))
        return button

    def toggle(self):
        if self.active:
            self.cancel()
        else:
            self.open()

    def refresh(self):
        app = self.app
        controller = app.target_controller
        label = getattr(controller, 'position_label', '输入位置') if controller else '输入位置'
        if not isinstance(label, str):
            label = '输入位置'
        for button, compact in self.buttons:
            button.configure(text='取消选位' if self.active else
                             ('位置' if label == '输入位置' else label) if compact else label)
            button.state(['disabled'] if not self.active and (app.busy or app.target_filling) else ['!disabled'])

    def open(self, target=None):
        app = self.app
        if app.busy or app.target_filling or app.closing or app.ai_review is not None or app.convention_panel is not None:
            return
        if not app.target_controller:
            app.status_var.set(app.target_platform_note)
            return
        if self.active:
            self.scan()
            return
        self.target = target
        app.cancel_auto_timer()
        app.target_picker.close()
        app.disarm_delivery()
        app.live_sync.cancel_timer()
        app.live_sync.paused = True
        keep_mac_windows_separate()
        self.main_state = (app.state(), bool(app.attributes('-topmost')))
        app.hover_help.hide()
        app.attributes('-topmost', False)
        self._active = True
        app.status_var.set('请在 Agent 窗口点击要填入的红框；再次点「取消选位」可退出。')
        self.refresh()
        self.scan()
        self.inspect_job = app.after(600, self.inspect)
        app.live_sync.refresh()

    def focus(self):
        if self.active:
            self.clear_marks()
            self.app.deiconify()
            self.app.lift()
            self.app.focus_force()

    def target_foreground(self):
        if sys.platform != 'win32' or not self.app.window_controller.native_enabled or self.target is None:
            return True
        import ctypes
        return int(ctypes.windll.user32.GetForegroundWindow() or 0) == int(self.target.id)

    def toggle_voice(self):
        # 保留全局指令导航，但不在选位界面另设录音按钮。
        if self.app.voice and not self.app.voice_stopping:
            self.app.stop_voice()
        elif not self.app.busy and not self.pending:
            self.app.start_voice('rewrite')

    def callback(self, generation, inspection=False):
        return lambda state, value: self.app.events.put(('input_position', (generation, self),
            'inspect_error' if inspection and state == 'error' else state, value))

    def scan(self):
        if not self.active or self.pending:
            return
        self.generation += 1
        self.inspect_pending = False
        self.candidate = None
        self.clear_marks()
        self.pending = True
        self.app.target_controller.begin_positions(self.callback(self.generation), self.target)

    def action(self, action, index=None):
        if not self.active or self.pending or self.app.busy:
            return
        if action in ('confirm', 'choose') and self.candidate is None:
            return
        self.generation += 1
        self.inspect_pending = False
        self.pending = True
        self.app.target_controller.position_action(action, self.callback(self.generation), index)

    def finish(self, generation, state, value):
        if generation != self.generation or not self.active or self.app.closing:
            return
        if state in ('inspect', 'inspect_error'):
            self.inspect_pending = False
        else:
            self.pending = False
        if state in ('ok', 'inspect'):
            self.candidate = value
            self.target = value.get('target', self.target)
            choices = value.get('choices') or [dict(index=value['index'], rect=value['rect'])]
            try:
                if self.target_foreground():
                    self.show_marks(choices)
                else:
                    self.clear_marks()
            except (OSError, tk.TclError):
                self.app.target_controller.position_action('cancel')
                self.close()
                self.app.status_var.set('红框暂时无法显示；位置未改变，请稍后重试。')
                return
            if state == 'ok':
                self.app.status_var.set('点击 Agent 窗口中的红框即可记住输入位置；不会立即填字或发送。')
        elif state == 'confirmed':
            self.app.target_label_var.set(value['target'].display_name())
            self.close()
            self.app.live_sync.configure(False)
            if self.app.sync_mode_var.get():
                self.app.live_sync.paused = True
            self.app.live_sync.refresh()
            self.app.status_var.set(f"已记住输入位置 {value['index']+1}；以后直接填入，需要更换时再点「输入位置」。"
                                    + ('同步模式需重新连接。' if self.app.sync_mode_var.get() else ''))
        elif state in ('error', 'inspect_error'):
            self.app.target_controller.position_action('cancel')
            self.close()
            self.app.status_var.set(str(value))

    def _mark(self, x, y, width, height, index, *, label='', alpha=None):
        mark = tk.Toplevel(self.app)
        self.marks.append(mark)
        mark.withdraw()
        mark.overrideredirect(True)
        mark.configure(bg=RED, cursor='hand2')
        mark.attributes('-topmost', True)
        if alpha is not None:
            mark.attributes('-alpha', alpha)
        mark.bind('<Button-1>', lambda _event, selected=index: self.action('choose', selected))
        if label:
            font_size = self.app.font_size(11) if hasattr(self.app, 'font_size') else 11
            text = tk.Label(mark, text=label, bg=RED, fg='white', cursor='hand2',
                            font=(self.app.fonts.ui, font_size, 'bold'))
            text.pack(fill='both', expand=True)
            text.bind('<Button-1>', lambda _event, selected=index: self.action('choose', selected))
        tx = x if x >= 0 else x + width - mark.winfo_screenwidth()
        ty = y if y >= 0 else y + height - mark.winfo_screenheight()
        if sys.platform == 'win32':
            place_physical_mark(mark, x, y, width, height)
        else:
            mark.geometry(f'{max(1,width)}x{max(1,height)}{tx:+d}{ty:+d}')
        mark.deiconify()
        if sys.platform == 'win32':
            place_physical_mark(mark, x, y, width, height)

    def show_marks(self, choices):
        locations = tuple((item['index'], *(round(v) for v in item['rect'])) for item in choices)
        if self.mark_location == locations:
            return
        self.clear_marks()
        self.mark_location = locations
        with physical_overlay_coordinates():
            # 透明点击区先创建；红色轮廓和「选这里」标签始终位于最上层。
            for index, x, y, width, height in locations:
                self._mark(x, y, width, height, index, alpha=.04)
            for index, x, y, width, height in locations:
                edge = 5
                for px, py, w, h in ((x, y, width, edge), (x, y+height-edge, width, edge),
                                     (x, y, edge, height), (x+width-edge, y, edge, height)):
                    self._mark(px, py, w, h, index)
                label_width = 112
                label_y = y - 32 if y >= 32 else y
                self._mark(x, label_y, label_width, 32, index, label=f'选这里 {index+1}')

    def clear_marks(self):
        for mark in self.marks:
            mark.destroy()
        self.marks.clear()
        self.mark_location = None

    def cancel(self):
        if self.active:
            if self.app.busy and self.app.voice_mode == 'rewrite':
                self.app.cancel_operation()
            self.app.target_controller.position_action('cancel')
            self.close()
            self.app.status_var.set('已取消位置选择，之前记住的位置不变。')

    def inspect(self):
        self.inspect_job = None
        if self.active:
            if not self.pending and not self.inspect_pending and not self.app.busy and self.candidate is not None:
                self.inspect_pending = True
                self.app.target_controller.position_action('inspect', self.callback(self.generation, inspection=True))
            self.inspect_job = self.app.after(600, self.inspect)

    def close(self):
        self.generation += 1
        if self.inspect_job:
            self.app.after_cancel(self.inspect_job)
            self.inspect_job = None
        self.clear_marks()
        self._active = False
        self.pending = False
        self.inspect_pending = False
        self.candidate = None
        if self.main_state is not None:
            state, topmost = self.main_state
            self.main_state = None
            if not self.app.closing:
                self.app.attributes('-topmost', topmost)
                if state not in ('withdrawn', 'iconic'):
                    self.app.deiconify()
                    self.app.lift()
                elif state == 'iconic':
                    self.app.iconify()
        self.refresh()
        self.app.live_sync.refresh()

    def instruction(self, text):
        request = navigation_instruction(text)
        if request is None:
            return False
        action, index = request
        if action == 'begin':
            self.open()
        elif action == 'cancel':
            self.cancel()
        elif self.active:
            self.action(action, index)
        else:
            self.app.status_var.set('请先点「输入位置」，再点目标窗口里的红框。')
        return True
