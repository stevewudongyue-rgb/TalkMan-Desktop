"""系统窗口装饰与应用身份。保留原生标题栏、窗口拖动和系统按钮。"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import sys
import tkinter as tk
from talkman_assets import asset_dir


def keep_mac_windows_separate(*, existing=True):
    """在创建／显示窗口前调用：设置不是另一个文档，不参与 macOS 窗口标签。"""
    if sys.platform != 'darwin':
        return
    try:
        import AppKit
        AppKit.NSWindow.setAllowsAutomaticWindowTabbing_(False)
        # Tk 必须先创建自己的 NSApplication 子类；启动前只设置类级策略。
        if existing:
            for window in AppKit.NSApplication.sharedApplication().windows():
                window.setTabbingMode_(AppKit.NSWindowTabbingModeDisallowed)
    except (ImportError, AttributeError, RuntimeError):
        pass


def set_app_identity():
    if sys.platform == 'win32':
        try:
            method = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID
            method.argtypes = [ctypes.c_wchar_p]
            method.restype = ctypes.c_long
            method('Cololight.TalkMan.Desktop')
        except (OSError, AttributeError):
            pass


def windows_titlebar(window_id, dark, *, user=None, dwm=None):
    user = user or ctypes.windll.user32
    dwm = dwm or ctypes.windll.dwmapi
    user.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user.GetAncestor.restype = wintypes.HWND
    hwnd = user.GetAncestor(window_id, 2) or window_id
    setter = dwm.DwmSetWindowAttribute
    setter.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
    setter.restype = ctypes.c_long
    enabled = ctypes.c_int(bool(dark))
    if setter(hwnd, 20, ctypes.byref(enabled), ctypes.sizeof(enabled)) != 0:
        setter(hwnd, 19, ctypes.byref(enabled), ctypes.sizeof(enabled))
    # Windows 11 的标题/文字颜色。旧系统不支持时保留上面的深色模式。
    for attribute, number in ((35, 0x000000 if dark else 0xF5F5F5),
                              (36, 0xE8E8E8 if dark else 0x1A1A1A)):
        value = wintypes.DWORD(number)
        setter(hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value))
    return hwnd


_menu_target_class = None


class SystemStyle:
    def __init__(self, app, native=True):
        self.app, self.native = app, native
        self.dark = True
        self.images = []
        self.menu_item = self.menu_target = None
        self.closed = False
        self.jobs = set()
        folder = asset_dir() / 'app'
        for size in (16, 32, 48, 128, 256):
            try:
                self.images.append(tk.PhotoImage(master=app, file=str(folder / f'TalkMan-{size}.png')))
            except tk.TclError:
                pass
        if self.images:
            app.iconphoto(True, *self.images)
        app.bind_all('<Map>', self._mapped, add=True)
        self._later(self._start)

    def _later(self, callback):
        if self.closed:
            return
        job = None
        def run():
            self.jobs.discard(job)
            if not self.closed:
                callback()
        job = self.app.after_idle(run)
        self.jobs.add(job)

    def _mapped(self, event):
        window = event.widget
        if isinstance(window, (tk.Tk, tk.Toplevel)):
            self._later(lambda: self._apply(window))

    def _start(self):
        self.set_theme(self.app.theme_key)
        if sys.platform == 'darwin' and self.native:
            try:
                self._mac_menu()
            except (ImportError, AttributeError, RuntimeError):
                pass  # 缺少原生组件仍保留 Dock 和正常编辑。

    def set_theme(self, theme):
        self.dark = theme == 'dark'
        for window in [self.app, *self.app.winfo_children()]:
            if isinstance(window, (tk.Tk, tk.Toplevel)):
                self._apply(window)

    def _apply(self, window):
        if not self.native or self.closed:
            return
        try:
            if not window.winfo_exists() or window.overrideredirect():
                return
            if sys.platform == 'win32':
                windows_titlebar(window.winfo_id(), self.dark)
            elif sys.platform == 'darwin':
                keep_mac_windows_separate()
                # Tk 的入口避免绕过其窗口生命周期直接改 NSWindow。
                window.tk.call('::tk::unsupported::MacWindowStyle', 'appearance',
                               window._w, 'darkaqua' if self.dark else 'aqua')
        except (tk.TclError, OSError, AttributeError):
            if sys.platform == 'darwin':
                try:
                    import AppKit
                    appearance = AppKit.NSAppearance.appearanceNamed_(
                        AppKit.NSAppearanceNameDarkAqua if self.dark else AppKit.NSAppearanceNameAqua)
                    AppKit.NSApplication.sharedApplication().setAppearance_(appearance)
                except (ImportError, AttributeError, RuntimeError):
                    pass

    def _mac_menu(self):
        global _menu_target_class
        import AppKit
        if _menu_target_class is None:
            class TalkManStatusActions(AppKit.NSObject):
                def showTalkMan_(self, _sender):
                    self.owner._later(lambda: self.owner.app.window_controller.reveal(focus=True))

                def quitTalkMan_(self, _sender):
                    self.owner._later(self.owner.app.close_app)
            _menu_target_class = TalkManStatusActions
        target = _menu_target_class.alloc().init()
        target.owner = self
        folder = asset_dir() / 'app'
        icon = AppKit.NSImage.alloc().initWithContentsOfFile_(str(folder / 'TalkMan-menu-36.png'))
        if icon is None:
            return
        icon.setSize_((18, 18))
        icon.setTemplate_(True)
        item = AppKit.NSStatusBar.systemStatusBar().statusItemWithLength_(AppKit.NSVariableStatusItemLength)
        item.button().setImage_(icon)
        item.button().setToolTip_('TALKMAN · 录音、修改，再填入')
        menu = AppKit.NSMenu.alloc().init()
        for label, action in (('打开 TALKMAN', 'showTalkMan:'), ('退出 TALKMAN', 'quitTalkMan:')):
            entry = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(label, action, '')
            entry.setTarget_(target)
            menu.addItem_(entry)
        item.setMenu_(menu)
        self.menu_item, self.menu_target = item, target
        app_icon = AppKit.NSImage.alloc().initWithContentsOfFile_(str(folder / 'TalkMan.icns'))
        if app_icon:
            AppKit.NSApplication.sharedApplication().setApplicationIconImage_(app_icon)

    def close(self):
        self.closed = True
        for job in self.jobs:
            self.app.after_cancel(job)
        self.jobs.clear()
        if self.menu_item is not None:
            import AppKit
            AppKit.NSStatusBar.systemStatusBar().removeStatusItem_(self.menu_item)
            self.menu_target.owner = None
            self.menu_item = self.menu_target = None
