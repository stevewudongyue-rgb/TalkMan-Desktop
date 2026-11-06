"""只注册明确的全局组合键；不安装键盘钩子、不采集普通按键。"""
from __future__ import annotations

import ctypes as C
import queue
import sys
import threading
import time

from talkman_shortcuts import normalize_shortcut, ShortcutError

GLOBAL_ACTIONS = ("global_record", "global_rewrite", "global_show")
VOICE_ALIASES = {"replace": "global_record", "rewrite": "global_rewrite"}


def voice_bindings(shortcuts, platform=None):
    """把用户设置的安全组合键同时用于后台唤起，保留单键的窗口内行为。"""
    bindings = {key: shortcuts[key] for key in GLOBAL_ACTIONS if shortcuts.get(key)}
    for action in VOICE_ALIASES:
        binding = shortcuts.get(action)
        if not binding or binding in bindings.values():
            continue
        try:
            native_binding(binding, platform or sys.platform)
        except (ShortcutError, ValueError):
            continue
        bindings[action] = binding
    return bindings


def native_binding(value, platform):
    parts = normalize_shortcut(value).split("+")
    key, modifiers = parts[-1], set(parts[:-1])
    if not modifiers.intersection({"Control", "Alt", "Command"}):
        raise ShortcutError("全局快捷键需搭配 Ctrl、Alt 或 Command，不能使用单独的空格或 Ctrl。")
    if platform == "win32":
        flags = sum({"Control": 2, "Alt": 1, "Shift": 4, "Command": 8}[m] for m in modifiers)
        code = 32 if key == "space" else 111 + int(key[1:]) if key.startswith("F") and len(key)>1 else ord(key)
        return flags | 0x4000, code  # MOD_NOREPEAT
    if platform == "darwin":
        codes = dict(A=0, S=1, D=2, F=3, H=4, G=5, Z=6, X=7, C=8, V=9,
                     B=11, Q=12, W=13, E=14, R=15, Y=16, T=17, O=31, U=32,
                     I=34, P=35, L=37, J=38, K=40, N=45, M=46)
        codes.update(dict(zip('1234659780', (18, 19, 20, 21, 22, 23, 25, 26, 28, 29))))
        codes.update(space=49, F1=122, F2=120, F3=99, F4=118, F5=96, F6=97,
                     F7=98, F8=100, F9=101, F10=109, F11=103, F12=111,
                     F13=105, F14=107, F15=113, F16=106, F17=64, F18=79, F19=80, F20=90)
        if key not in codes:
            raise ShortcutError("Mac 全局快捷键支持字母、数字、空格及 F1–F20。")
        flags = sum({"Control": 4096, "Alt": 2048, "Shift": 512, "Command": 256}[m] for m in modifiers)
        return flags, codes[key]
    raise ShortcutError("此系统暂不支持全局快捷键。")


class GlobalHotkeys:
    def __init__(self, enabled=True):
        self.events = queue.Queue()
        self.backend = None
        self.generation = 0
        self.enabled = enabled

    def configure(self, bindings):
        self.generation += 1
        if not self.enabled:
            return
        try:
            if self.backend is None and bindings:
                self.backend = (WindowsHotkeys(self.events) if sys.platform == "win32"
                                else MacHotkeys(self.events) if sys.platform == "darwin" else None)
                if self.backend is None:
                    raise RuntimeError("此系统暂不支持全局快捷键")
            if self.backend:
                self.backend.configure(bindings, self.generation)
        except Exception as error:
            self.events.put((self.generation, "error", "全局快捷键不可用：" + str(error)))

    def poll(self):
        result = []
        while True:
            try:
                generation, kind, value = self.events.get_nowait()
            except queue.Empty:
                return result
            if generation == self.generation:
                result.append((kind, value))

    def close(self):
        self.generation += 1
        if self.backend:
            self.backend.close()
            self.backend = None


class WindowsHotkeys:
    def __init__(self, events):
        self.events = events
        self.commands = queue.Queue()
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self._run, name="TalkMan-hotkeys", daemon=True)
        self.thread.start()

    def configure(self, bindings, generation):
        self.commands.put((dict(bindings), generation))

    def close(self):
        self.stopping.set()
        self.thread.join(timeout=2)

    def _run(self):
        from ctypes import wintypes as W
        user = C.WinDLL("user32", use_last_error=True)
        kernel = C.WinDLL('kernel32')
        kernel.GetCurrentThreadId.restype = W.DWORD
        self.thread_id = kernel.GetCurrentThreadId()
        user.RegisterHotKey.argtypes = [W.HWND, C.c_int, W.UINT, W.UINT]
        user.RegisterHotKey.restype = W.BOOL
        user.UnregisterHotKey.argtypes = [W.HWND, C.c_int]
        user.PeekMessageW.argtypes = [C.POINTER(W.MSG), W.HWND, W.UINT, W.UINT, W.UINT]
        user.GetAsyncKeyState.argtypes = [C.c_int]
        user.GetAsyncKeyState.restype = C.c_short
        registered = {}
        pressed = {}
        generation = 0
        try:
            while not self.stopping.is_set():
                latest = None
                while not self.commands.empty():
                    latest = self.commands.get_nowait()
                if latest is not None:
                    bindings, generation = latest
                    for identifier in registered:
                        user.UnregisterHotKey(None, identifier)
                    registered.clear()
                    pressed.clear()
                    stale = W.MSG()
                    while user.PeekMessageW(C.byref(stale), None, 0x0312, 0x0312, 1):
                        pass
                    for number, (action, binding) in enumerate(bindings.items(), 0x5440):
                        try:
                            modifiers, key = native_binding(binding, "win32")
                            if not user.RegisterHotKey(None, number, modifiers, key):
                                raise RuntimeError("组合键已被其他程序占用")
                            registered[number] = (action, modifiers, key)
                        except Exception as error:
                            self.events.put((generation, "error", f"{binding}：{error}；请在窗口设置中更换"))
                message = W.MSG()
                while user.PeekMessageW(C.byref(message), None, 0, 0, 1):
                    if message.message == 0x0312 and message.wParam in registered and message.wParam not in pressed:
                        pressed[message.wParam] = time.monotonic()
                        self.events.put((generation, "action", registered[message.wParam][0]))
                # 仅检查已触发组合键的松开状态，不监听普通输入。
                for identifier, started in list(pressed.items()):
                    action, modifiers, key = registered[identifier]
                    down = bool(user.GetAsyncKeyState(key) & 0x8000)
                    for flag, codes in ((1, (0x12,)), (2, (0x11,)), (4, (0x10,)), (8, (0x5B, 0x5C))):
                        if modifiers & flag:
                            down = down and any(user.GetAsyncKeyState(code) & 0x8000 for code in codes)
                    if not down:
                        pressed.pop(identifier)
                        self.events.put((generation, "release", (action, time.monotonic()-started)))
                self.stopping.wait(0.025)
        finally:
            for identifier in registered:
                user.UnregisterHotKey(None, identifier)


class MacHotkeys:
    """Carbon 系统热键事件，无需键盘监听或录屏权限。回调只写入队列。"""
    def __init__(self, events):
        self.events = events
        self.library = C.CDLL('/System/Library/Frameworks/Carbon.framework/Carbon')
        self.refs = []
        self.actions = {}
        self.pressed = {}
        self.generation = 0
        self.handler = C.c_void_p()
        self.callback_type = C.CFUNCTYPE(C.c_int32, C.c_void_p, C.c_void_p, C.c_void_p)
        class EventType(C.Structure):
            _fields_ = [("eventClass", C.c_uint32), ("eventKind", C.c_uint32)]
        class HotkeyID(C.Structure):
            _fields_ = [("signature", C.c_uint32), ("id", C.c_uint32)]
        self.HotkeyID = HotkeyID
        lib = self.library
        lib.GetEventDispatcherTarget.restype = C.c_void_p
        lib.InstallEventHandler.argtypes = [C.c_void_p, self.callback_type, C.c_uint32,
                                          C.POINTER(EventType), C.c_void_p, C.POINTER(C.c_void_p)]
        lib.RegisterEventHotKey.argtypes = [C.c_uint32, C.c_uint32, HotkeyID, C.c_void_p,
                                           C.c_uint32, C.POINTER(C.c_void_p)]
        lib.GetEventParameter.argtypes = [C.c_void_p, C.c_uint32, C.c_uint32, C.c_void_p,
                                         C.c_uint32, C.c_void_p, C.c_void_p]
        lib.GetEventKind.argtypes = [C.c_void_p]
        lib.GetEventKind.restype = C.c_uint32
        lib.UnregisterEventHotKey.argtypes = [C.c_void_p]
        lib.RemoveEventHandler.argtypes = [C.c_void_p]
        self.callback = self.callback_type(self._event)
        events_spec = (EventType * 2)(EventType(self._four('keyb'), 5), EventType(self._four('keyb'), 6))
        result = lib.InstallEventHandler(lib.GetEventDispatcherTarget(), self.callback, 2,
                                        events_spec, None, C.byref(self.handler))
        if result:
            raise RuntimeError(f"Mac 热键事件注册失败（{result}）")

    @staticmethod
    def _four(value):
        return int.from_bytes(value.encode('ascii'), 'big')

    def _event(self, _next, event, _data):
        identifier = self.HotkeyID()
        result = self.library.GetEventParameter(event, self._four('----'), self._four('hkid'),
                                               None, C.sizeof(identifier), None, C.byref(identifier))
        if result or identifier.signature != self._four('Tlkm') or identifier.id not in self.actions:
            return -9874  # eventNotHandledErr
        if self.library.GetEventKind(event) == 6:
            started = self.pressed.pop(identifier.id, None)
            if started is not None:
                self.events.put((self.generation, 'release',
                                 (self.actions[identifier.id], time.monotonic()-started)))
        elif identifier.id not in self.pressed:
            self.pressed[identifier.id] = time.monotonic()
            self.events.put((self.generation, 'action', self.actions[identifier.id]))
        return 0

    def configure(self, bindings, generation):
        self._clear()
        self.generation = generation
        for number, (action, binding) in enumerate(bindings.items(), 1):
            try:
                modifiers, code = native_binding(binding, 'darwin')
                ref = C.c_void_p()
                result = self.library.RegisterEventHotKey(code, modifiers,
                    self.HotkeyID(self._four('Tlkm'), number), self.library.GetEventDispatcherTarget(),
                    1, C.byref(ref))  # kEventHotKeyExclusive
                if result:
                    raise RuntimeError(f"组合键被占用或无法注册（{result}）")
                self.refs.append(ref)
                self.actions[number] = action
            except Exception as error:
                self.events.put((generation, 'error', f'{binding}：{error}；请在窗口设置中更换'))

    def _clear(self):
        for ref in self.refs:
            self.library.UnregisterEventHotKey(ref)
        self.refs.clear()
        self.actions.clear()
        self.pressed.clear()

    def close(self):
        self._clear()
        if self.handler:
            self.library.RemoveEventHandler(self.handler)
            self.handler = C.c_void_p()
