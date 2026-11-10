"""Windows UI Automation 目标输入框后端。

该模块不保存目标正文，也不启动全局键盘钩子。COM/UIA 对象只在调用线程
中惰性创建；上层应在自己的后台 worker 中串行调用本模块。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import time
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from talkman_target import TargetError, SendSnapshot


@dataclass(frozen=True)
class TargetWindow:
    id: int
    pid: int
    app: str
    title: str
    process_path: str = ""
    process_started: str = ""

    @property
    def identity(self) -> str:
        return self.process_path.casefold()


@dataclass(frozen=True)
class FocusToken:
    """只包含控件身份，不包含用户输入内容。"""

    window_id: int
    control_hwnd: int
    automation_id: str = ""
    class_name: str = ""
    captured_at: float = 0.0


def _win_api() -> Any:
    if os.name != "nt":
        raise TargetError("目标输入功能仅支持 Windows。")
    user32 = ctypes.windll.user32
    callback_type = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)(wt.BOOL, wt.HWND, wt.LPARAM)
    # ctypes 默认返回 c_int，64 位 HWND/HANDLE 会被截断。
    signatures = {
        "EnumWindows": ([callback_type, wt.LPARAM], wt.BOOL),
        "IsWindow": ([wt.HWND], wt.BOOL),
        "IsWindowVisible": ([wt.HWND], wt.BOOL),
        "IsIconic": ([wt.HWND], wt.BOOL),
        "GetWindowTextLengthW": ([wt.HWND], ctypes.c_int),
        "GetWindowTextW": ([wt.HWND, wt.LPWSTR, ctypes.c_int], ctypes.c_int),
        "GetWindowThreadProcessId": ([wt.HWND, ctypes.POINTER(wt.DWORD)], wt.DWORD),
        "GetForegroundWindow": ([], wt.HWND),
        "GetAncestor": ([wt.HWND, wt.UINT], wt.HWND),
        "SetForegroundWindow": ([wt.HWND], wt.BOOL),
        "ShowWindow": ([wt.HWND, ctypes.c_int], wt.BOOL),
        "OpenClipboard": ([wt.HWND], wt.BOOL),
        "EmptyClipboard": ([], wt.BOOL),
        "SetClipboardData": ([wt.UINT, wt.HANDLE], wt.HANDLE),
        "CloseClipboard": ([], wt.BOOL),
        "keybd_event": ([wt.BYTE, wt.BYTE, wt.DWORD, ctypes.c_size_t], None),
    }
    for name, (arguments, result) in signatures.items():
        method = getattr(user32, name)
        method.argtypes = arguments
        method.restype = result
    return user32


def _kernel_api() -> Any:
    if os.name != "nt":
        raise TargetError("目标输入功能仅支持 Windows。")
    kernel = ctypes.windll.kernel32
    signatures = {
        "OpenProcess": ([wt.DWORD, wt.BOOL, wt.DWORD], wt.HANDLE),
        "QueryFullProcessImageNameW": ([wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)], wt.BOOL),
        "CloseHandle": ([wt.HANDLE], wt.BOOL),
        "GlobalAlloc": ([wt.UINT, ctypes.c_size_t], wt.HGLOBAL),
        "GlobalLock": ([wt.HGLOBAL], wt.LPVOID),
        "GlobalUnlock": ([wt.HGLOBAL], wt.BOOL),
        "GlobalFree": ([wt.HGLOBAL], wt.HGLOBAL),
    }
    for name, (arguments, result) in signatures.items():
        method = getattr(kernel, name)
        method.argtypes = arguments
        method.restype = result
    return kernel


def _process_path(pid: int) -> str:
    if os.name != "nt":
        return ""
    kernel = _kernel_api()
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = kernel.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(len(buf))
        if kernel.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        kernel.CloseHandle(handle)


def _process_started(pid: int) -> str:
    """创建时间用于识别 PID／窗口句柄复用，不持久化原生控件。"""
    if os.name != 'nt':
        return ''
    kernel = _kernel_api()
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return ''
    try:
        fn = kernel.GetProcessTimes
        fn.argtypes = [wt.HANDLE] + [ctypes.POINTER(wt.FILETIME)] * 4
        fn.restype = wt.BOOL
        times = [wt.FILETIME() for _ in range(4)]
        if fn(handle, *(ctypes.byref(value) for value in times)):
            return str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
        return ''
    finally:
        kernel.CloseHandle(handle)


from talkman_position_windows import WindowsInputPositions


class WindowsTargetBackend(WindowsInputPositions):
    """使用 UI Automation 寻找并填充 Agent 主输入框。"""

    # UIA control type / pattern constants (UIAutomationClient.h)
    _CONTROL_EDIT = 50004
    _CONTROL_GROUP = 50026
    _CONTROL_DOCUMENT = 50030
    _CONTROL_PANE = 50033
    _CONTROL_SEARCH = 50045
    _VALUE_PATTERN = 10002
    _TEXT_PATTERN = 10014
    _TEXT_ATTRIBUTE_IS_READ_ONLY = 40015
    _TREE_DESCENDANTS = 4
    _PROPERTY_CONTROL_TYPE = 30003
    _PROPERTY_IS_ENABLED = 30010
    _PROPERTY_IS_OFFSCREEN = 30022
    _PROPERTY_IS_PASSWORD = 30019
    _PROPERTY_NATIVE_HWND = 30020
    _PROPERTY_AUTOMATION_ID = 30011
    _PROPERTY_CLASS_NAME = 30012

    def __init__(self, *, excluded_apps: Optional[Iterable[str]] = None):
        self._uia = None
        self._uia_api = None
        self._com_initialized = False
        self._excluded_apps = {str(x).casefold() for x in (excluded_apps or ())}

    def _ensure_uia(self) -> Any:
        if self._uia is not None:
            return self._uia
        if os.name != "nt":
            raise TargetError("目标输入功能仅支持 Windows。")
        try:
            import comtypes
            from comtypes.client import CreateObject, GetModule

            comtypes.CoInitialize()
            self._com_initialized = True
            try:
                from comtypes.gen import UIAutomationClient as api
            except ImportError:
                api = GetModule("UIAutomationCore.dll")
            self._uia_api = api
            self._uia = CreateObject(
                "{FF48DBA4-60EF-4201-AA87-54103EEF594E}",
                interface=api.IUIAutomation,
            )
            return self._uia
        except Exception as exc:  # pragma: no cover - 仅在缺依赖/系统组件时触发
            raise TargetError(f"Windows UI Automation 不可用：{exc}") from exc

    def close(self) -> None:
        if self._com_initialized:
            try:
                import comtypes
                comtypes.CoUninitialize()
            except Exception:
                pass
        self._uia = None
        self._uia_api = None
        self._com_initialized = False

    def enumerate_windows(self) -> list[TargetWindow]:
        user32 = _win_api()
        result: list[TargetWindow] = []
        callback_type = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

        @callback_type
        def callback(hwnd: int, _lparam: int) -> bool:
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            title_buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, title_buf, length + 1)
            title = title_buf.value.strip()
            if not title:
                return True
            pid = wt.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            path = _process_path(int(pid.value))
            app = os.path.basename(path) or "未知应用"
            if app.casefold() in self._excluded_apps:
                return True
            result.append(TargetWindow(int(hwnd), int(pid.value), app, title, path, _process_started(int(pid.value))))
            return True

        user32.EnumWindows(callback, 0)
        return result

    # 兼容主线程早期接口命名。
    list_windows = enumerate_windows

    def validate_window(self, window: TargetWindow) -> Optional[TargetWindow]:
        """确认 HWND、PID、进程路径和标题仍指向同一会话。"""
        user32 = _win_api()
        hwnd = int(window.id)
        if not user32.IsWindow(hwnd):
            return None
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if int(pid.value) != int(window.pid):
            return None
        title_len = user32.GetWindowTextLengthW(hwnd)
        title_buf = ctypes.create_unicode_buffer(title_len + 1)
        user32.GetWindowTextW(hwnd, title_buf, title_len + 1)
        title = title_buf.value.strip()
        path = _process_path(int(pid.value))
        if window.process_path and path.casefold() != window.process_path.casefold():
            return None
        started = _process_started(int(pid.value))
        if window.process_started and started != window.process_started:
            return None
        # HWND + PID 已经唯一标识本次窗口会话；Agent/浏览器常会在输入后
        # 动态修改标题，不能因为标题变化就把仍然可用的目标判为失效。
        return TargetWindow(hwnd, int(pid.value), window.app, title, path, started or window.process_started)

    def _window_element(self, window: TargetWindow) -> Any:
        uia = self._ensure_uia()
        try:
            return uia.ElementFromHandle(int(window.id))
        except Exception as exc:
            raise TargetError(f"无法读取目标窗口：{exc}") from exc

    @staticmethod
    def _current(element: Any, name: str, default: Any = None) -> Any:
        try:
            return getattr(element, name)
        except Exception:
            return default

    def _is_editable(self, element: Any) -> bool:
        control_type = self._current(element, "CurrentControlType", 0)
        if control_type not in (self._CONTROL_EDIT, self._CONTROL_DOCUMENT, self._CONTROL_GROUP):
            return False
        if not self._current(element, "CurrentIsEnabled", True):
            return False
        if self._current(element, "CurrentIsOffscreen", False):
            return False
        if self._current(element, "CurrentIsPassword", False):
            return False
        name = str(self._current(element, "CurrentName", "") or "").casefold()
        cls = str(self._current(element, "CurrentClassName", "") or "").casefold()
        # 豆包的正文编辑器暴露为 Group；普通容器及 trailingBreak 均不可写。
        # 仅接纳明确的富文本编辑器，且必须由 TextPattern 证明非只读。
        if control_type == self._CONTROL_GROUP:
            if not {'prosemirror', 'tiptap'}.intersection(cls.split()):
                return False
            try:
                pattern = self._pattern(element, self._TEXT_PATTERN, "IUIAutomationTextPattern")
                readonly = pattern.DocumentRange.GetAttributeValue(self._TEXT_ATTRIBUTE_IS_READ_ONLY)
                if not isinstance(readonly, (bool, int)) or readonly != 0:
                    return False
            except Exception:
                return False
        if control_type == self._CONTROL_SEARCH or any(x in f"{name} {cls}" for x in ("search", "address", "omnibox", "terminal", "console", "搜索", "地址栏")):
            return False
        try:
            value = self._pattern(element, self._VALUE_PATTERN, "IUIAutomationValuePattern")
            readonly = getattr(value, "CurrentIsReadOnly", None)
            if isinstance(readonly, (bool, int)):
                return readonly == 0
        except Exception:
            pass
        if control_type == self._CONTROL_DOCUMENT:
            try:
                pattern = self._pattern(element, self._TEXT_PATTERN, "IUIAutomationTextPattern")
                readonly = pattern.DocumentRange.GetAttributeValue(self._TEXT_ATTRIBUTE_IS_READ_ONLY)
                return isinstance(readonly, (bool, int)) and readonly == 0
            except Exception:
                # 网页正文通常也是 Document，不能默认把它当作输入框。
                return False
        return True

    def _pattern(self, element: Any, pattern_id: int, interface_name: str) -> Any:
        pattern = element.GetCurrentPattern(pattern_id)
        if pattern is None:
            raise TargetError("目标控件不支持所需输入模式。")
        if hasattr(pattern, "QueryInterface"):
            if not pattern:
                raise TargetError("目标控件不支持所需输入模式。")
            self._ensure_uia()
            pattern = pattern.QueryInterface(getattr(self._uia_api, interface_name))
            if not pattern:
                raise TargetError("目标控件不支持所需输入模式。")
        return pattern

    def _find_editable(self, window: TargetWindow, *, focused_only: bool = False) -> Any:
        pinned = self._bound_input(window)
        root = self._window_element(window)
        if focused_only:
            try:
                focused = self._uia.GetFocusedElement()
            except Exception:
                return None
            if focused is not None and self._is_editable(focused):
                if pinned is not None and not self._uia.CompareElements(focused, pinned):
                    return None
                hwnd = self._current(focused, "CurrentNativeWindowHandle", 0)
                if (hwnd and self._root_hwnd(int(hwnd)) == int(window.id)) or self._is_descendant_of(focused, root):
                    return focused
            return None
        if pinned is not None:
            return pinned
        candidates: list[Any] = []
        try:
            for control in (self._CONTROL_EDIT, self._CONTROL_DOCUMENT, self._CONTROL_GROUP):
                condition = self._uia.CreatePropertyCondition(self._PROPERTY_CONTROL_TYPE, control)
                found = root.FindAll(self._TREE_DESCENDANTS, condition)
                for item in self._collection_items(found):
                    if self._is_editable(item):
                        candidates.append(item)
        except Exception:
            return None
        main = [element for element in candidates if self.input_position_role(window, element)]
        if main:
            return main[0] if len(main) == 1 else None
        # 排除搜索栏后按语义提示打分；并列候选拒绝，避免误填。
        def score(element: Any) -> int:
            text = " ".join(str(self._current(element, key, "") or "") for key in ("CurrentName", "CurrentAutomationId", "CurrentClassName")).casefold()
            return sum(weight for token, weight in (("prompt", 8), ("composer", 8), ("message", 6), ("input", 5), ("chat", 4), ("editor", 2), ("输入", 5), ("消息", 6), ("任务", 6)) if token in text)
        ranked = sorted(((score(e), e) for e in candidates), key=lambda pair: pair[0], reverse=True)
        if not ranked:
            return None
        if len(ranked) > 1 and ranked[0][0] == ranked[1][0]:
            return None
        return ranked[0][1]

    def _is_descendant_of(self, element: Any, root: Any) -> bool:
        """确认无原生 HWND 的 UIA 控件确实位于目标窗口树下。"""
        root_hwnd = int(self._current(root, "CurrentNativeWindowHandle", 0) or 0)
        if not root_hwnd:
            return False
        try:
            walker = getattr(self._ensure_uia(), "ControlViewWalker", None)
            current = element
            for _ in range(64):
                if current is None:
                    return False
                if int(self._current(current, "CurrentNativeWindowHandle", 0) or 0) == root_hwnd:
                    return True
                if walker is not None:
                    current = walker.GetParentElement(current)
                elif hasattr(current, "Navigate"):
                    # UIA TreeWalker 的 Parent 方向常量是 0；3 是 FirstChild。
                    current = current.Navigate(0)
                else:
                    return False
        except Exception:
            # 无法证明控件属于目标窗口时必须拒绝，避免误填到其他窗口。
            return False
        return False

    def _matches_focus_token(self, element: Any, token: FocusToken, window_id: int) -> bool:
        if token.window_id != int(window_id):
            return False
        hwnd = int(self._current(element, "CurrentNativeWindowHandle", 0) or 0)
        if token.control_hwnd and hwnd and token.control_hwnd != hwnd:
            return False
        automation_id = str(self._current(element, "CurrentAutomationId", "") or "")
        if token.automation_id and token.automation_id != automation_id:
            return False
        class_name = str(self._current(element, "CurrentClassName", "") or "")
        if token.class_name and token.class_name != class_name:
            return False
        return True

    @staticmethod
    def _collection_items(collection: Any) -> list[Any]:
        """兼容 comtypes SAFEARRAY 和带 Length/索引器的 UIA 集合。"""
        try:
            return [collection.GetElement(i) for i in range(int(collection.Length))]
        except Exception:
            pass
        try:
            return list(collection)
        except Exception:
            pass
        try:
            length = int(collection.Length)
            return [collection[i] for i in range(length)]
        except Exception:
            return []

    @staticmethod
    def _root_hwnd(hwnd: int) -> int:
        if not hwnd or os.name != "nt":
            return int(hwnd or 0)
        try:
            # GA_ROOT = 2
            return int(_win_api().GetAncestor(int(hwnd), 2) or 0)
        except Exception:
            return int(hwnd)

    def capture_focus(self, window: TargetWindow, previous_token: Optional[FocusToken] = None) -> Optional[FocusToken | bool]:
        """返回当前目标控件 token。

        ``None`` 表示目标窗口仍然有效，但当前焦点不在目标窗口，调用方
        可以继续让 ``fill`` 自动聚焦主输入框；只有目标窗口已经失效时才
        返回 ``False``。
        """
        valid = self.validate_window(window)
        if valid is None:
            return False
        user32 = _win_api()
        foreground = int(user32.GetForegroundWindow())
        # 点击 Talk Man 的“填入 Agent”按钮后，前台窗口会暂时变成 Talk Man。
        # 这不是失败：fill() 会在目标窗口内寻找并聚焦主输入框。
        if foreground != int(window.id):
            return None
        element = self._find_editable(valid, focused_only=True)
        if element is None:
            # 目标窗口仍然有效，但当前焦点可能在 Talk Man 的按钮上。
            # 让 fill() 重新寻找主输入框并聚焦，不能把这种情况当成窗口失效。
            return None
        hwnd = int(self._current(element, "CurrentNativeWindowHandle", 0) or window.id)
        return FocusToken(valid.id, hwnd, str(self._current(element, "CurrentAutomationId", "") or ""), str(self._current(element, "CurrentClassName", "") or ""), time.time())

    def _collapse_selection_to_end(self, element: Any) -> None:
        try:
            pattern = self._pattern(element, self._TEXT_PATTERN, "IUIAutomationTextPattern")
            selections = pattern.GetSelection()
            selected = self._collection_items(selections)
            if selected:
                selection = selected[0]
                # TextRange 没有 Collapse；把 Start 移到 End 保留选区末尾的光标。
                selection.MoveEndpointByRange(0, selection, 1)
                selection.Select()
        except Exception:
            pass

    @staticmethod
    def _send_key(vk: int, *, ctrl: bool = False) -> None:
        user32 = _win_api()
        KEYEVENTF_KEYUP = 0x0002
        if ctrl:
            user32.keybd_event(0x11, 0, 0, 0)
        user32.keybd_event(vk, 0, 0, 0)
        user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
        if ctrl:
            user32.keybd_event(0x11, 0, KEYEVENTF_KEYUP, 0)

    @staticmethod
    def _paste_unicode(text: str, expected_window_id: Optional[int] = None) -> None:
        user32 = _win_api()
        owner = int(user32.GetForegroundWindow() or 0)
        if not owner or not user32.IsWindow(owner):
            raise TargetError("目标窗口未获得有效焦点，已取消填入。")
        if expected_window_id is not None and owner != int(expected_window_id):
            raise TargetError("目标窗口已失去焦点，已取消填入。")
        kernel = _kernel_api()
        CF_UNICODETEXT, GMEM_MOVEABLE = 13, 0x0002
        encoded = (str(text) + "\0").encode("utf-16-le")
        handle = kernel.GlobalAlloc(GMEM_MOVEABLE, len(encoded))
        if not handle:
            raise TargetError("无法准备剪贴板内容。")
        pointer = kernel.GlobalLock(handle)
        if not pointer:
            kernel.GlobalFree(handle)
            raise TargetError("无法写入剪贴板内存。")
        try:
            ctypes.memmove(pointer, encoded, len(encoded))
        except Exception:
            kernel.GlobalUnlock(handle)
            kernel.GlobalFree(handle)
            raise TargetError("无法写入剪贴板内存。") from None
        kernel.GlobalUnlock(handle)
        if not user32.OpenClipboard(owner):
            kernel.GlobalFree(handle)
            raise TargetError("无法打开系统剪贴板。")
        try:
            if not user32.EmptyClipboard():
                raise TargetError("无法清空系统剪贴板。")
            if not user32.SetClipboardData(CF_UNICODETEXT, handle):
                raise TargetError("无法写入系统剪贴板。")
            handle = None  # 所有权已交给系统
        finally:
            user32.CloseClipboard()
            if handle:
                kernel.GlobalFree(handle)
        if int(user32.GetForegroundWindow() or 0) != owner or not user32.IsWindow(owner):
            raise TargetError("目标窗口已失去焦点，已取消填入。")
        WindowsTargetBackend._send_key(0x56, ctrl=True)  # Ctrl+V

    def open_sync(self, window, *, require_write=True):
        valid = self.validate_window(window)
        if valid is None:
            raise TargetError('目标窗口不可用，请重新选择。')
        element = self._find_editable(valid)
        if element is None:
            raise TargetError('未找到唯一的主输入框，无法打开同步。')
        if require_write:
            pattern = self._pattern(element, self._VALUE_PATTERN, 'IUIAutomationValuePattern')
            if pattern.CurrentIsReadOnly:
                raise TargetError('此输入框不支持后台同步，请关闭同步后使用「填入」。')
        return element, valid.title

    @staticmethod
    def _known_empty_hint(window, element, value):
        identity = (str(window.app) + ' ' + str(window.identity)).casefold()
        return (('chatgpt' in identity or 'codex' in identity)
                and 'prosemirror' in str(WindowsTargetBackend._current(element, 'CurrentClassName', '')).casefold().split()
                and value.strip().casefold() in ('work with chatgpt', 'work with codex')
                and str(WindowsTargetBackend._current(element, 'CurrentName', '')).strip() == value.strip())

    def read_sync(self, window, handle, *, require_write=True, allow_hint_snapshot=False):
        valid = self.validate_window(window)
        element, title = handle
        if (valid is None or valid.title != title or not self._is_editable(element)
                or not self._is_descendant_of(element, self._window_element(valid))):
            raise TargetError('目标窗口或对话已改变，同步已暂停。')
        main = self._find_editable(valid)
        if main is None or not self._ensure_uia().CompareElements(main, element):
            raise TargetError('目标主输入框已改变，同步已暂停。')
        try:
            pattern = self._pattern(element, self._VALUE_PATTERN, 'IUIAutomationValuePattern')
            value = pattern.CurrentValue
        except Exception:
            if require_write:
                raise
            # 普通填入后的发送只需读回核验，无需 SetValue；兼容仅有 TextPattern 的编辑器。
            pattern = self._pattern(element, self._TEXT_PATTERN, 'IUIAutomationTextPattern')
            readonly = pattern.DocumentRange.GetAttributeValue(self._TEXT_ATTRIBUTE_IS_READ_ONLY)
            if not isinstance(readonly, (bool, int)) or readonly != 0:
                raise TargetError('无法确认目标输入框可编辑，本次未下发。')
            value = pattern.DocumentRange.GetText(-1)
        else:
            if pattern.CurrentIsReadOnly:
                raise TargetError('目标输入框为只读，本次未下发。')
        if not isinstance(value, str):
            raise TargetError('无法读回目标输入框，同步已暂停。')
        # 部分 ChatGPT 版本把编辑器提示同时暴露为 Name 和 Value。
        # 无法证明它是不是实际正文时停止同步，不能将其拼成前缀，更不能删除目标文字。
        if self._known_empty_hint(valid, element, value) and not allow_hint_snapshot:
            raise TargetError('目标返回的文字与 ChatGPT／Codex 输入提示相同，无法确认实际正文；已暂停同步，请关闭同步后使用「填入 Agent」。')
        return value if require_write else value.replace('\r\n', '\n')

    def capture_delivery_snapshot(self, window):
        handle = self.open_delivery(window)
        before = self.read_sync(window, handle, require_write=False, allow_hint_snapshot=True)
        hint = self._known_empty_hint(window, handle[0], before)
        paragraph_end = '\n' if hint and before.endswith('\n') and not before.endswith('\n\n') else ''
        return SendSnapshot(handle, before, empty_hint=hint, paragraph_end=paragraph_end)

    def open_delivery(self, window):
        return self.open_sync(window, require_write=False)

    def read_delivery(self, window, handle):
        return self.read_sync(window, handle, require_write=False)

    def send_delivery(self, window, handle, text, current):
        return self.send_sync(window, handle, text, current, delivery=True)

    def write_sync(self, window, handle, text, expected, current):
        if self.read_sync(window, handle) != expected or not current():
            raise TargetError('目标文字已变化，同步已暂停。')
        pattern = self._pattern(handle[0], self._VALUE_PATTERN, 'IUIAutomationValuePattern')
        pattern.SetValue(text)

    def send_sync(self, window, handle, text, current, *, delivery=False):
        if not self.input_allows_submit():
            raise TargetError('此输入位置的提交方式尚未识别，请在目标内提交。')
        read = self.read_delivery if delivery else self.read_sync
        if read(window, handle) != text or not current():
            raise TargetError('目标文字已变化，本次未下发。')
        user = _win_api()
        if user.IsIconic(int(window.id)):
            user.ShowWindow(int(window.id), 9)
        user.SetForegroundWindow(int(window.id))
        handle[0].SetFocus()
        time.sleep(0.06)
        focused = self._find_editable(window, focused_only=True)
        if (int(user.GetForegroundWindow() or 0) != int(window.id) or focused is None
                or not self._ensure_uia().CompareElements(focused, handle[0])
                or read(window, handle) != text or not current()):
            raise TargetError('目标未获得正确焦点，本次未下发。')
        user.GetAsyncKeyState.argtypes = [ctypes.c_int]
        user.GetAsyncKeyState.restype = ctypes.c_short
        if any(user.GetAsyncKeyState(key) & 0x8000 for key in (0x10, 0x11, 0x12, 0x5B, 0x5C)):
            raise TargetError('请松开修饰键后再下发。')
        self._send_key(0x0D)
        return '已向目标输入框按下回车，请在目标确认发送结果；同步已暂停。'

    def fill(self, window: TargetWindow, text: str, remembered_focus: Optional[FocusToken] = None) -> str:
        if not str(text):
            raise TargetError("没有可填入的文字。")
        valid = self.validate_window(window)
        if valid is None:
            raise TargetError("目标窗口已关闭或会话已变化，请重新选择目标。")
        user32 = _win_api()
        current = self._find_editable(valid, focused_only=True)
        if current is not None and self._bound_input(valid) is None and not self.input_position_role(valid, current):
            preferred = self._find_editable(valid)
            if preferred is not None and self.input_position_role(valid, preferred):
                # 临时填空即使得到过系统焦点，也不自动取代明确的主消息框。
                current = None
                remembered_focus = None
        if current is None:
            # 点击 Talk Man 按钮会暂时夺走焦点；先恢复窗口，再核对原输入控件。
            try:
                if user32.IsIconic(int(valid.id)):
                    user32.ShowWindow(int(valid.id), 9)
                user32.SetForegroundWindow(int(valid.id))
            except Exception as exc:
                raise TargetError(f"无法聚焦 Agent 输入框：{exc}") from exc
            time.sleep(0.06)
            if remembered_focus is not None and int(user32.GetForegroundWindow() or 0) == int(valid.id):
                current = self._find_editable(valid, focused_only=True)
        preserve = bool(
            remembered_focus
            and current is not None
            and self._matches_focus_token(current, remembered_focus, valid.id)
        )
        if remembered_focus is not None and current is not None and not preserve:
            # 记忆的控件身份已变化时，重新寻找主输入框，不能把文字填入当前的其他控件。
            current = None
        if current is None:
            # Electron／浏览器可能在恢复窗口后才挂载 UIA 编辑控件。
            # 仅重试已验证窗口的主输入框；不以坐标或任意可编辑控件代替。
            for attempt in range(5):
                if int(user32.GetForegroundWindow() or 0) != int(valid.id):
                    raise TargetError("目标窗口未获得焦点，已取消填入。")
                current = self._find_editable(valid)
                if current is not None:
                    break
                if attempt < 4:
                    time.sleep(0.08)
            if current is None:
                raise TargetError("未找到可编辑的主输入框，请先在 Agent 输入框中点一下后重试。")
            try:
                current.SetFocus()
            except Exception as exc:
                raise TargetError(f"无法聚焦 Agent 输入框：{exc}") from exc
            time.sleep(0.06)
        if int(user32.GetForegroundWindow() or 0) != int(valid.id):
            raise TargetError("目标窗口未获得焦点，已取消填入。")
        focused = self._find_editable(valid, focused_only=True)
        if focused is None or not self._ensure_uia().CompareElements(focused, current):
            raise TargetError("目标输入框未获得焦点，已取消填入。")
        if preserve:
            self._collapse_selection_to_end(current)
        else:
            self._send_key(0x23, ctrl=True)  # Ctrl+End
        if int(user32.GetForegroundWindow() or 0) != int(valid.id):
            raise TargetError("目标窗口已失去焦点，已取消填入。")
        self._paste_unicode(str(text), expected_window_id=valid.id)
        return "已填入 Agent 输入框（未发送）。"


def create_backend() -> WindowsTargetBackend:
    return WindowsTargetBackend()

