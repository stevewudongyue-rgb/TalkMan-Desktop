"""桌面窗口：贴边收起、悬停展开、置顶以及全局语音唤起。"""
from __future__ import annotations

import ctypes as C
from dataclasses import dataclass
import sys
import time
import tkinter as tk
import tkinter.font as tkfont

from talkman_global_hotkeys import GLOBAL_ACTIONS, VOICE_ALIASES, GlobalHotkeys, voice_bindings


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    width: int
    height: int

    @property
    def right(self): return self.x + self.width

    @property
    def bottom(self): return self.y + self.height

    def contains(self, point):
        return self.x <= point[0] < self.right and self.y <= point[1] < self.bottom

    def overlap(self, other):
        return max(0, min(self.right, other.right)-max(self.x, other.x)) * max(0, min(self.bottom, other.bottom)-max(self.y, other.y))


def edge_for(window, screen, tolerance=16):
    candidates = [(abs(window.x-screen.x), 'left'), (abs(window.right-screen.right), 'right'),
                  (abs(window.y-screen.y), 'top'), (abs(window.bottom-screen.bottom), 'bottom')]
    distance, edge = min(candidates)
    return edge if distance <= tolerance and window.overlap(screen) else None


def window_screen(window, screens):
    # 看标题栏进入了哪块屏，避免大窗口的面积让它一直被算在旧的大屏上。
    anchor = (window.x+min(window.width//2, 240), window.y+16)
    return next((s for s in screens if s.contains(anchor)), max(screens, key=window.overlap))


def fit_window_rect(window, screen):
    """只缩小过大的窗口，不在返回大屏时放大；工作区已排除任务栏／Dock。"""
    width = min(window.width, max(1, screen.width-32))
    height = min(window.height, max(1, screen.height-32))
    if width != window.width:
        width = min(width, max(1, round(screen.width*.92)))
    if height != window.height:
        height = min(height, max(1, round(screen.height*.90)))
    return Rect(max(screen.x, min(window.x, screen.right-width)),
                max(screen.y, min(window.y, screen.bottom-height)), width, height)


EDGE_LABELS = {'top': '顶部', 'left': '左侧', 'right': '右侧', 'bottom': '底部'}


def tab_rect(window, screen, edge, minimum_size=(0, 0)):
    # 可读的收起标签；以工作区为界，避开任务栏和 Mac 菜单栏。
    width, height = (184, 36) if edge in ('top', 'bottom') else (44, 168)
    width, height = max(width, minimum_size[0]), max(height, minimum_size[1])
    width, height = min(width, screen.width), min(height, screen.height)
    if edge in ('top', 'bottom'):
        return Rect(max(screen.x, min(screen.right-width, window.x+(window.width-width)//2)),
                    screen.y if edge == 'top' else screen.bottom-height, width, height)
    return Rect(screen.x if edge == 'left' else screen.right-width,
                max(screen.y, min(screen.bottom-height, window.y+(window.height-height)//2)), width, height)


def dock_rect(window, screen, edge):
    if edge not in EDGE_LABELS:
        raise ValueError('未知收起方向')
    x = max(screen.x, min(window.x, max(screen.x, screen.right-window.width)))
    y = max(screen.y, min(window.y, max(screen.y, screen.bottom-window.height)))
    if edge in ('top', 'bottom'):
        y = screen.y if edge == 'top' else screen.bottom-window.height
    else:
        x = screen.x if edge == 'left' else screen.right-window.width
    return Rect(x, y, window.width, window.height)


def compact_rect(tab, screen, edge, width, height):
    x = tab.x+(tab.width-width)//2 if edge in ('top', 'bottom') else screen.x if edge == 'left' else screen.right-width
    y = (screen.y if edge == 'top' else screen.bottom-height) if edge in ('top', 'bottom') else tab.y+(tab.height-height)//2
    return Rect(max(screen.x, min(x, max(screen.x, screen.right-width))),
                max(screen.y, min(y, max(screen.y, screen.bottom-height))), width, height)


def nearest_screen_edge(point, screens):
    """按鼠标所在工作区选择最近边沿，支持负坐标和屏幕之间的空隙。"""
    x, y = point
    def distance(screen):
        return max(screen.x-x, 0, x-screen.right)**2 + max(screen.y-y, 0, y-screen.bottom)**2
    screen = min(screens, key=distance)
    edge = min(((abs(x-screen.x), 'left'), (abs(x-screen.right), 'right'),
                (abs(y-screen.y), 'top'), (abs(y-screen.bottom), 'bottom')))[1]
    return screen, edge


def position_tab(tab, screen, edge, fraction):
    fraction = max(0.0, min(1.0, fraction))
    if edge in ('top', 'bottom'):
        return Rect(screen.x+round((screen.width-tab.width)*fraction),
                    screen.y if edge == 'top' else screen.bottom-tab.height, tab.width, tab.height)
    return Rect(screen.x if edge == 'left' else screen.right-tab.width,
                screen.y+round((screen.height-tab.height)*fraction), tab.width, tab.height)


class WindowPlatform:
    def __init__(self, app):
        self.app = app
        if sys.platform == 'win32':
            from ctypes import wintypes as W
            self.W = W
            self.user = C.WinDLL('user32', use_last_error=True)
            self.user.GetAncestor.argtypes = [W.HWND, W.UINT]
            self.user.GetAncestor.restype = W.HWND
            self.user.GetWindowRect.argtypes = [W.HWND, C.POINTER(W.RECT)]
            self.user.IsZoomed.argtypes = [W.HWND]
            self.user.SetForegroundWindow.argtypes = [W.HWND]
            self.user.SetWindowPos.argtypes = [W.HWND, W.HWND, C.c_int, C.c_int, C.c_int, C.c_int, W.UINT]
            self.user.SetWindowPos.restype = W.BOOL

    def hwnd(self):
        return self.user.GetAncestor(self.app.winfo_id(), 2)

    def bounds(self):
        a = self.app
        if sys.platform == 'win32':
            r = self.W.RECT()
            if self.user.GetWindowRect(self.hwnd(), C.byref(r)):
                return Rect(r.left, r.top, r.right-r.left, r.bottom-r.top)
        if sys.platform == 'darwin':
            try:
                import AppKit
            except ImportError:
                pass  # 原生组件缺失时仍能打开设置修复，使用 Tk 提供的窗口尺寸。
            else:
                top = AppKit.NSScreen.screens()[0].frame().size.height
                for win in AppKit.NSApplication.sharedApplication().windows():
                    if win.title() == a.title():
                        r = win.frame()
                        return Rect(round(r.origin.x), round(top-r.origin.y-r.size.height), round(r.size.width), round(r.size.height))
        return Rect(a.winfo_x(), a.winfo_y(), a.winfo_width(), a.winfo_height())

    def screens(self):
        if sys.platform == 'win32':
            W = self.W
            class MonitorInfo(C.Structure):
                _fields_ = [('size', W.DWORD), ('monitor', W.RECT), ('work', W.RECT), ('flags', W.DWORD)]
            callback_type = C.WINFUNCTYPE(W.BOOL, W.HANDLE, W.HDC, C.POINTER(W.RECT), W.LPARAM)
            self.user.GetMonitorInfoW.argtypes = [W.HANDLE, C.POINTER(MonitorInfo)]
            self.user.EnumDisplayMonitors.argtypes = [W.HDC, C.POINTER(W.RECT), callback_type, W.LPARAM]
            screens = []
            def read(handle, _dc, _rect, _data):
                info = MonitorInfo()
                info.size = C.sizeof(info)
                if self.user.GetMonitorInfoW(handle, C.byref(info)):
                    r = info.work
                    screens.append(Rect(r.left, r.top, r.right-r.left, r.bottom-r.top))
                return True
            self.user.EnumDisplayMonitors(None, None, callback_type(read), 0)
            if screens:
                return screens
        if sys.platform == 'darwin':
            try:
                import AppKit
            except ImportError:
                pass
            else:
                native = AppKit.NSScreen.screens()
                top = native[0].frame().size.height
                result = []
                for screen in native:
                    r = screen.visibleFrame()
                    result.append(Rect(round(r.origin.x), round(top-r.origin.y-r.size.height), round(r.size.width), round(r.size.height)))
                return result
        return [Rect(0, 0, self.app.winfo_screenwidth(), self.app.winfo_screenheight())]

    def maximized(self):
        if self.app.state() == 'zoomed':
            return True
        if sys.platform == 'win32':
            return bool(self.user.IsZoomed(self.hwnd()))
        try:
            return bool(self.app.attributes('-fullscreen'))
        except tk.TclError:
            return False

    def pointer_down(self):
        if sys.platform == 'win32':
            return bool(self.user.GetAsyncKeyState(1) & 0x8000)
        if sys.platform == 'darwin':
            import AppKit
            return bool(AppKit.NSEvent.pressedMouseButtons())
        return False

    def activate(self):
        if sys.platform == 'win32':
            self.user.SetForegroundWindow(self.hwnd())
        elif sys.platform == 'darwin':
            import AppKit
            AppKit.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        self.app.lift()
        self.app.text.focus_force()

    def move_to(self, x, y):
        """直接定位，不模拟标题栏拖动，不进入系统 Snap 拖拽流程。"""
        if sys.platform == 'win32':
            if not self.user.SetWindowPos(self.hwnd(), None, x, y, 0, 0, 0x0015):
                raise C.WinError(C.get_last_error())
        else:
            # Mac 必须由 Tk 的 wm 通道移动；直接调用 Cocoa setter 会在
            # Tcl 未进入事件分发时重入 Tk 的原生回调，导致解释器崩溃。
            self.app.geometry(f'+{x}+{y}')
            self.app.update_idletasks()


class DesktopWindowController:
    def __init__(self, app, native_enabled=True, platform=None, hotkeys=None, clock=time.monotonic,
                 automatic_hide=None):
        self.app = app
        self.platform = platform or WindowPlatform(app)
        self.hotkeys = hotkeys or GlobalHotkeys(native_enabled)
        self.clock = clock
        self.native_enabled = native_enabled
        self.key_gestures = {}
        self.expanded_voice = None
        self.expanded_done = False
        self.expanded_ready_at = None
        self.expanded_hold_open = False
        # Mac 采用显式收起／点击展开；鼠标经过时不改变窗口状态。
        self.automatic_hide = sys.platform != 'darwin' if automatic_hide is None else automatic_hide
        self.edge = None
        self.screen = None
        self.hidden = False
        self.temporary = False
        self.last_rect = None
        self.was_working = False
        self.last_active = clock()
        self.suppress_until = clock()+1
        self.next_screen_check = 0
        self.display_screen = None
        self.screens = []
        self.bindings = None
        self.job = None
        self.closed = False
        self.tab = None
        self.tab_area = None
        self.hover_since = None
        self.undocked_rect = None
        self.tab_label = None
        self.compact = False
        self.full_view = None
        self.tab_fraction = None
        self.tab_drag = None
        self.tab_moved = False
        self.hover_blocked = False
        app.bind('<KeyPress>', self.touch, add=True)
        app.bind('<ButtonPress>', self.touch, add=True)
        self.apply_preferences()
        if native_enabled:
            self.job = app.after(250, self.tick)

    def touch(self, _event=None):
        self.last_active = self.clock()
        if _event is not None and self.expanded_voice is not None:
            a = self.app
            stop_buttons = (a.replace_button, a.rewrite_button, a.mic_button,
                            a.compact_record, a.compact_rewrite)
            if getattr(_event, 'widget', None) not in stop_buttons:
                self.keep_expanded()

    def keep_expanded(self):
        if self.expanded_voice is not None:
            self.expanded_voice = None
            self.expanded_ready_at = None
            self.expanded_hold_open = True

    def voice_progress(self, value):
        a = self.app
        if self.compact and len(value.strip()) > 120 and not self.expanded_hold_open:
            self.expanded_voice = a.operation_id
            self.expanded_done = False
            self.expanded_ready_at = None
            self.reveal(focus=True, auto=True)

    def voice_complete(self):
        if self.expanded_voice == self.app.operation_id:
            self.expanded_done = True

    def voice_failed(self):
        self.keep_expanded()

    def settings_open(self):
        win = getattr(self.app, 'settings_window', None)
        guide = getattr(self.app, 'guide', None)
        review = getattr(self.app, 'ai_review', None)
        return bool((win and win.winfo_exists()) or (guide and guide.opened) or review
                    or getattr(self.app, 'convention_panel', None))

    def command_overlay_open(self):
        a = self.app
        win = getattr(a, 'settings_window', None)
        guide = getattr(a, 'guide', None)
        return bool((a.ai_review is not None or a.convention_panel is not None)
                    and not (win and win.winfo_exists()) and not (guide and guide.opened))

    def apply_preferences(self):
        settings = getattr(self.app, 'settings_window', None)
        settings_open = bool(settings and settings.winfo_exists())
        self.app.attributes('-topmost', (self.app.topmost_var.get() or self.compact)
                            and not self.app.position_picker.active
                            and not (sys.platform == 'darwin' and settings_open))
        if settings_open:
            self.app._raise_settings_window(settings)
        self.app.pin_button.configure(text='已置顶 ▾' if self.app.topmost_var.get() else '置顶 ▾')
        if not self.app.edge_hide_var.get():
            self.undock(announce=False)
        self.sync_hotkeys()

    def dock(self, edge):
        if edge not in EDGE_LABELS:
            raise ValueError('未知收起方向')
        if self.protected():
            self.app.status_var.set('请先结束录音／处理或关闭当前面板，再收起窗口')
            return False
        a = self.app
        previous_enabled = a.edge_hide_var.get()
        try:
            if self.compact:
                self.leave_compact()
            # 在改变窗口位置之前确认设置可保存，避免只完成一半。
            if not previous_enabled:
                a.edge_hide_var.set(True)
                a.save_window_preferences()
            if self.hidden or a.state() != 'normal' or self.platform.maximized():
                self.reveal()
                a.state('normal')
                a.update_idletasks()
            bounds = self.platform.bounds()
            screens = self.platform.screens()
            screen = max(screens, key=bounds.overlap)
            destination = dock_rect(bounds, screen, edge)
            self.platform.move_to(destination.x, destination.y)
            a.update_idletasks()
            self.undocked_rect = self.undocked_rect or bounds
            self.screen, self.edge, self.screens = screen, edge, screens
            self.tab_fraction = None
            self.tab_moved = False
            self.next_screen_check = self.clock()+2
            self.last_rect = self.platform.bounds()
            self.suppress_until = self.clock()+0.5
            gesture = '悬停或点击' if self.automatic_hide else '点击'
            a.status_var.set(f'已收起到{EDGE_LABELS[edge]} · {gesture}标签展开；在「置顶 ▾」中可取消收起')
            return self.hide()
        except (tk.TclError, OSError, RuntimeError, ValueError) as error:
            if not previous_enabled and not a.ui_preferences.get('edge_hide', False):
                a.edge_hide_var.set(False)
            if self.hidden:
                self.reveal()
            self.edge = self.screen = None
            a.status_var.set('未能收起窗口：' + str(error))
            return False

    def undock(self, announce=True):
        if self.compact:
            self.leave_compact()
        if self.hidden:
            self.reveal(focus=announce)
        previous = self.undocked_rect
        self.edge = self.screen = self.undocked_rect = None
        self.tab_fraction = None
        self.tab_moved = False
        if previous:
            try:
                screens = self.platform.screens()
                screen = max(screens, key=previous.overlap)
                # 已可见的原位置按原样恢复，不能因 Dock／任务栏使工作区
                # 较短就把原窗口向上挪；仅原屏幕离线时迁回可见工作区。
                if previous.overlap(screen):
                    x, y = previous.x, previous.y
                else:
                    x = max(screen.x, min(previous.x, max(screen.x, screen.right-previous.width)))
                    y = max(screen.y, min(previous.y, max(screen.y, screen.bottom-previous.height)))
                self.platform.move_to(x, y)
            except (tk.TclError, OSError, RuntimeError, ValueError) as error:
                self.app.status_var.set('已取消收起，原位置恢复失败，请拖动窗口：' + str(error))
                return False
            self.last_rect = None
        self.suppress_until = self.clock()+0.5
        self.temporary = False
        if announce:
            self.app.status_var.set('已取消收起 · 恢复普通窗口位置')
        return True

    def sync_hotkeys(self):
        bindings = voice_bindings(self.app.shortcuts)
        if not self.app.global_voice_var.get():
            bindings = {key: value for key, value in bindings.items() if key == 'global_show'}
        if self.settings_open():
            bindings = {key:value for key,value in bindings.items()
                        if self.command_overlay_open() and key in ('rewrite', 'global_rewrite')}
        if bindings != self.bindings:
            self.key_gestures.clear()
            self.bindings = bindings
            self.hotkeys.configure(bindings)

    def protected(self):
        a = self.app
        menu = getattr(a, 'window_menu', None)
        return bool(a.busy or a.voice or a.auto_running or a.target_filling or a._mic_permission_pending
                    or a.history_open or self.settings_open() or a.hold_job or a.voice_button_press
                    or getattr(getattr(a, 'position_picker', None), 'active', False)
                    or getattr(getattr(a, 'app_commands', None), 'active', False)
                    or (menu and menu.winfo_exists() and menu.winfo_ismapped())
                    or a.grab_current())

    def tick(self, reschedule=True):
        self.job = None
        if self.closed or self.app.closing:
            return
        try:
            self.sync_hotkeys()
            for kind, value in self.hotkeys.poll():
                if kind == 'action':
                    self.hotkey_press(value)
                elif kind == 'release':
                    self.hotkey_release(*value)
                elif kind == 'error':
                    self.app.status_var.set(value)
            self._layout_tick()
        except (tk.TclError, OSError, RuntimeError) as error:
            # 可发现地恢复窗口，不能因贴边错误让应用消失。
            if self.hidden:
                self.reveal()
            self.edge = None
            self.app.status_var.set('贴边收起暂停：' + str(error))
        finally:
            if reschedule and not self.closed and not self.app.closing:
                self.job = self.app.after(100, self.tick)

    def _layout_tick(self):
        a, now = self.app, self.clock()
        if self.compact:
            a.refresh_compact_controls()
        if hasattr(a, 'live_sync'):
            a.live_sync.refresh()
        if now >= self.next_screen_check:
            self.screens = self.platform.screens()
            self.next_screen_check = now+2
            if self.hidden and self.screen not in self.screens:
                self.reveal()
                self.edge = self.screen = self.undocked_rect = None
                screen = self.screens[0]
                self.fit_to_screen(force=True)
                self.last_rect = None
                self.suppress_until = now+1
                return
        working = self.protected()
        if self.expanded_voice is not None:
            if self.expanded_voice != a.operation_id:
                self.keep_expanded()
            elif self.expanded_done and not working and not a.auto_job:
                if self.expanded_ready_at is None:
                    self.expanded_ready_at = now + 2
                elif now >= self.expanded_ready_at:
                    if self.hide():
                        self.expanded_voice = None
                        self.hover_blocked = True
                        return
            else:
                self.expanded_ready_at = None
        if working or (self.was_working and not working):
            self.last_active = now
        self.was_working = working
        if self.hidden:
            if a.state() != 'withdrawn':  # 任务栏／Dock 点击也能恢复正常窗口。
                self.reveal()
                return
            if self.tab_drag or not self.automatic_hide:
                return
            point = a.winfo_pointerxy()
            if self.tab_area and self.tab_area.contains(point):
                if self.hover_blocked or self.platform.pointer_down():
                    self.hover_since = None
                    return
                if self.hover_since is None:
                    self.hover_since = now
                if now-self.hover_since >= 0.65:
                    self.reveal()
            else:
                self.hover_since = None
                self.hover_blocked = False
            return
        if a.state() != 'normal' or self.platform.maximized():
            self.edge = self.screen = self.undocked_rect = None
            self.last_rect = None
            return
        if not self.platform.pointer_down():
            self.fit_to_screen()
        bounds = self.platform.bounds()
        if bounds != self.last_rect:
            if self.last_rect is not None and now >= self.suppress_until:
                self.keep_expanded()
            moved = self.last_rect is not None and (bounds.x, bounds.y) != (self.last_rect.x, self.last_rect.y)
            self.last_rect = bounds
            self.last_active = now
            # 只允许菜单主动进入收起模式，拖到屏幕边缘不再自动触发。
            if moved and not self.compact and now >= self.suppress_until and self.edge and self.screen:
                distance = {'left': abs(bounds.x-self.screen.x),
                            'right': abs(bounds.right-self.screen.right),
                            'top': abs(bounds.y-self.screen.y),
                            'bottom': abs(bounds.bottom-self.screen.bottom)}[self.edge]
                if distance > 20 or not bounds.overlap(self.screen):
                    self.edge = self.screen = self.undocked_rect = None
                    a.status_var.set('已离开停靠位置 · 取消自动收起')
        if (self.expanded_voice is not None or self.expanded_hold_open or not self.automatic_hide
                or not a.edge_hide_var.get() or not self.edge or working or a.auto_job or now < self.suppress_until):
            return
        if bounds.contains(a.winfo_pointerxy()) or self.platform.pointer_down():
            self.last_active = now
        elif now-self.last_active >= (4 if self.temporary else 1):
            self.hide()

    def fit_to_screen(self, force=False):
        a = self.app
        if a.state() != 'normal' or self.platform.maximized():
            return
        screens = self.screens or self.platform.screens()
        if not screens:
            return
        bounds = self.platform.bounds()
        screen = window_screen(bounds, screens)
        changed = screen != self.display_screen
        self.display_screen = screen
        oversized = bounds.width > screen.width-32 or bounds.height > screen.height-32
        invisible = not any(bounds.overlap(s) for s in screens)
        if not (force or changed or oversized or invisible):
            return
        destination = fit_window_rect(bounds, screen)
        frame_w = max(0, bounds.width-a.winfo_width())
        frame_h = max(0, bounds.height-a.winfo_height())
        minimum = (400, 280) if self.compact else (760, 520)
        a.minsize(max(1, min(minimum[0], screen.width-32-frame_w, destination.width-frame_w)),
                  max(1, min(minimum[1], screen.height-32-frame_h, destination.height-frame_h)))
        if (destination.width, destination.height) != (bounds.width, bounds.height):
            a.geometry(f'{max(1,destination.width-frame_w)}x{max(1,destination.height-frame_h)}')
            a.update_idletasks()
        if destination != bounds:
            self.platform.move_to(destination.x, destination.y)
            self.last_rect = self.platform.bounds()

    def hide(self):
        if self.hidden or not self.edge or self.protected():
            return False
        a = self.app
        a.capture_edit()
        a.magnifier.hide()
        if self.tab is None:
            self.tab = tk.Toplevel(a, takefocus=False)
            self.tab.title('Talk Man · 收起标签')
            self.tab.withdraw()
            self.tab.overrideredirect(True)
            self.tab.attributes('-topmost', True)
            self.tab.bind('<ButtonPress-1>', self.tab_press)
            self.tab.bind('<B1-Motion>', self.tab_motion)
            self.tab.bind('<ButtonRelease-1>', self.tab_release)
            self.tab_label = tk.Label(self.tab, cursor='fleur', takefocus=False, bd=0)
            self.tab_label.pack(fill='both', expand=True, padx=1, pady=1)
        self.configure_tab()
        self.hidden = True
        self.expanded_hold_open = False
        self.temporary = False
        self.hover_since = None
        self.hover_blocked = False
        a.withdraw()
        if self.compact:
            self.leave_compact()
        self.tab.deiconify()
        self.tab.lift()
        return True

    def configure_tab(self):
        a = self.app
        self.tab.configure(bg=a.palette['text'])
        label = ('TALKMAN  ·  展开 ' + ('▾' if self.edge == 'top' else '▴')) if self.edge in ('top', 'bottom') else 'T\nA\nL\nK\n\n展开'
        self.tab_label.configure(text=label, bg=a.palette['input'], fg=a.palette['text'],
                                 font=(a.fonts.ui, 10), padx=4, pady=2)
        font = tkfont.Font(root=a, font=self.tab_label.cget('font'))
        lines = label.split('\n')
        minimum_size = (max(font.measure(line) for line in lines)+24,
                        font.metrics('linespace')*len(lines)+12)
        anchor = self.full_view['bounds'] if self.compact else self.last_rect or self.platform.bounds()
        self.tab_area = tab_rect(anchor, self.screen, self.edge, minimum_size)
        if self.tab_fraction is not None:
            self.tab_area = position_tab(self.tab_area, self.screen, self.edge, self.tab_fraction)
        r = self.tab_area
        self.tab.geometry(f'{r.width}x{r.height}+{r.x}+{r.y}')

    def tab_press(self, event):
        if not self.hidden or not self.tab_area:
            return 'break'
        r = self.tab_area
        self.tab_drag = {'start': (event.x_root, event.y_root), 'moved': False,
                         'grip': ((event.x_root-r.x)/r.width, (event.y_root-r.y)/r.height)}
        self.hover_since = None
        self.tab.grab_set()
        return 'break'

    def tab_motion(self, event):
        drag = self.tab_drag
        if not drag or not self.hidden:
            return 'break'
        point = (event.x_root, event.y_root)
        if not drag['moved'] and max(abs(point[i]-drag['start'][i]) for i in (0, 1)) < 6:
            return 'break'
        drag['moved'] = True
        screen, edge = nearest_screen_edge(point, self.platform.screens())
        self.screen, self.edge = screen, edge
        self.configure_tab()
        r = self.tab_area
        horizontal = edge in ('top', 'bottom')
        span = screen.width-r.width if horizontal else screen.height-r.height
        offset = point[0]-screen.x-drag['grip'][0]*r.width if horizontal else point[1]-screen.y-drag['grip'][1]*r.height
        self.tab_fraction = max(0.0, min(1.0, offset/max(1, span)))
        self.configure_tab()
        self.tab_moved = True
        self.hover_since = None
        self.app.status_var.set(f'标签已移到{EDGE_LABELS[edge]} · 点击展开，按住拖动可换位置')
        return 'break'

    def tab_release(self, event):
        drag = self.tab_drag
        if drag is None:
            return 'break'
        # 松手位置可能是最后一个鼠标移动事件之后的位置。
        self.tab_motion(event)
        self.tab_drag = None
        if self.tab.grab_current() == self.tab:
            self.tab.grab_release()
        self.hover_since = None
        self.hover_blocked = bool(drag['moved'])
        if not drag['moved']:
            self.reveal(focus=True)
        return 'break'

    def align_to_tab(self):
        if not self.tab_moved or not self.tab_area or not self.screen:
            return
        bounds = self.platform.bounds()
        destination = compact_rect(self.tab_area, self.screen, self.edge, bounds.width, bounds.height)
        self.platform.move_to(destination.x, destination.y)
        self.last_rect = self.platform.bounds()
        self.tab_moved = False

    def leave_compact(self):
        if not self.compact:
            return
        a, view = self.app, self.full_view
        self.compact = False
        self.full_view = None
        a.set_compact_layout(False)
        a.minsize(*view['minimum'])
        a.geometry(view['geometry'])
        a.update_idletasks()
        self.platform.move_to(view['bounds'].x, view['bounds'].y)
        self.fit_to_screen(force=True)
        self.last_rect = self.platform.bounds()

    def reveal_compact(self):
        a = self.app
        if not self.compact:
            self.align_to_tab()
            bounds = self.last_rect or self.platform.bounds()
            screen = self.screen or max(self.platform.screens(), key=bounds.overlap)
            tab = self.tab_area or tab_rect(bounds, screen, self.edge or 'top')
            self.full_view = {'geometry': a.geometry(), 'minimum': a.minsize(), 'bounds': bounds}
            self.compact = True
            a.set_compact_layout(True)
            # 随系统字体缩放，但远小于完整窗口。与当前屏幕工作区保持相交。
            scale = max(1.0, min(1.5, a.winfo_fpixels('1i')/96))
            width = min(round(480*scale), max(280, screen.width-32))
            height = min(round(330*scale), max(240, screen.height-64))
            a.minsize(min(400, width), min(280, height))
            a.geometry(f'{width}x{height}')
            a.update_idletasks()
            actual = self.platform.bounds()
            destination = compact_rect(tab, screen, self.edge or 'top', actual.width, actual.height)
            self.platform.move_to(destination.x, destination.y)
            self.last_rect = self.platform.bounds()
        self.reveal(focus=True, full=False)

    def dismiss_window(self):
        """Mac 关闭主窗口后留驻后台，不留下贴边标签，不触发自动展开。"""
        self.app.capture_edit()
        self.app.magnifier.hide()
        self.keep_expanded()
        self.expanded_voice = self.expanded_ready_at = None
        self.hidden = self.temporary = False
        self.edge = self.screen = self.undocked_rect = None
        self.tab_drag = self.tab_area = None
        self.tab_moved = False
        self.hover_since = None
        if self.tab:
            if self.tab.grab_current() == self.tab:
                self.tab.grab_release()
            self.tab.withdraw()
        self.app.withdraw()
        self.leave_compact()
        self.last_rect = None

    def reveal(self, focus=False, full=True, auto=False):
        if self.app.position_picker.active:
            self.app.position_picker.focus()
            return False
        if full and not auto:
            self.keep_expanded()
        if self.tab_drag:
            self.tab_drag = None
            if self.tab.grab_current() == self.tab:
                self.tab.grab_release()
        if full and self.compact:
            self.leave_compact()
        if full:
            self.align_to_tab()
        was_hidden = self.hidden
        self.hidden = False
        if self.tab:
            self.tab.withdraw()
        self.app.deiconify()
        self.app.update_idletasks()
        self.fit_to_screen(force=True)
        self.app.attributes('-topmost', self.app.topmost_var.get() or self.compact)
        self.app.lift()
        self.last_active = self.clock()
        self.suppress_until = self.clock()+0.5
        self.hover_since = None
        if focus:
            self.platform.activate()
        return was_hidden

    def hotkey_press(self, action):
        if action == 'global_show':
            self.trigger(action)
            return
        if action in self.key_gestures:
            return
        gesture = {'started': self.clock(), 'released': False, 'token': None,
                   'mode': self.app.voice_gesture_var.get()}
        self.key_gestures[action] = gesture
        self.trigger(action, gesture)

    def hotkey_release(self, action, duration=None):
        gesture = self.key_gestures.pop(action, None)
        if not gesture:
            return
        duration = self.clock()-gesture['started'] if duration is None else duration
        gesture['released'] = True
        gesture['stop_on_release'] = (gesture['mode'] == 'hold' or
                                      gesture['mode'] == 'both' and duration >= 0.25)
        if (gesture['stop_on_release'] and gesture['token'] == self.app.operation_id
                and self.app.voice and not self.app.voice_stopping):
            self.app.stop_voice()

    def trigger(self, action, gesture=None):
        action = VOICE_ALIASES.get(action, action)
        if (self.app.closing or action not in GLOBAL_ACTIONS
                or (self.settings_open() and not (action == 'global_rewrite' and self.command_overlay_open()))):
            return
        if action == 'global_show':
            self.reveal(focus=True)
            return
        if self.app.position_picker.active:
            if action == 'global_rewrite':
                self.app.position_picker.toggle_voice()
                if gesture is not None and self.app.voice:
                    gesture['token'] = self.app.operation_id
            else:
                self.app.position_picker.focus()
                self.app.status_var.set('正在选择位置，请用指令切换或确认输入框。')
            return
        self.temporary = self.hidden or self.temporary
        if self.hidden or self.compact:
            self.reveal_compact()
        else:
            self.reveal(focus=True, auto=True)
        a = self.app
        if a.voice and not a.voice_stopping:
            a.stop_voice()
            return
        if a.busy or a._mic_permission_pending or a.target_filling:
            return
        # 主线程焦点完成后再调用现有事务，选区／光标及撤销逻辑完全复用。
        mode = 'rewrite' if action == 'global_rewrite' else 'replace' if a.get_selection() else 'insert'
        def begin():
            if self.closed or a.closing or a.busy or a._mic_permission_pending:
                return
            if gesture and gesture['released'] and gesture.get('stop_on_release'):
                return
            a.text.focus_force()
            if a.start_voice(mode):
                if gesture is not None:
                    gesture['token'] = a.operation_id
            else:
                self.last_active = self.clock()+4  # 错误提示留够阅读时间。
        a.after_idle(begin)

    def close(self):
        self.closed = True
        self.hotkeys.close()
        if self.job:
            self.app.after_cancel(self.job)
            self.job = None
        if self.tab:
            self.tab.destroy()
            self.tab = None
