"""macOS 辅助功能目标窗口后端；只填入文字，不提交消息。

PyObjC 在 worker 首次使用时加载。窗口和输入框只保存 AX 身份，不保存正文。
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from contextlib import nullcontext
from datetime import datetime
from functools import wraps
import os
import subprocess
import sys
import time
from typing import Any, Iterable

from talkman_target import TargetError, TargetWindow
from talkman_position_macos import MacInputPositions


PERMISSION_MESSAGE = (
    "请在 Mac“系统设置 → 隐私与安全性 → 辅助功能”中允许 TalkMan，"
    "然后退出并重新打开 TalkMan，再刷新目标窗口。"
    "如果从终端运行，请允许启动它的终端或 Python。"
)
DEPENDENCY_MESSAGE = "Mac 目标窗口组件未安装，请用新版 Mac 构建包重新构建应用。"

_BUNDLE_IDS = {
    "com.openai.codex", "com.openai.chat", "com.openai.chatgpt",
    "com.anthropic.claudefordesktop", "com.anthropic.claude",
    "com.todesktop.230313mzl4w4u92", "com.microsoft.vscode",
    "com.microsoft.vscodeinsiders", "com.apple.safari", "com.google.chrome",
    "com.google.chrome.beta", "com.google.chrome.canary", "com.microsoft.edgemac",
    "com.brave.browser", "company.thebrowser.browser", "org.mozilla.firefox",
    "com.trae.app", "com.trae.ide", "com.trae.ide.cn",
    "dev.zcode.app",
    "ai.perplexity.macv3", "com.bot.pc.doubao",
}
_APP_NAMES = {"codex", "zcode", "chatgpt", "claude", "cursor", "trae", "trae cn", "豆包", "doubao", "perplexity"}
_EXCLUDED_INPUT = (
    "search", "address", "omnibox", "terminal", "console", "password",
    "搜索", "地址栏", "终端", "密码", "查找",
)
_MAIN_INPUT = (
    ("prompt", 8), ("composer", 8), ("message", 6), ("chat", 4),
    ("input", 5), ("editor", 2), ("输入", 5), ("消息", 6),
    ("任务", 6), ("随心", 6),
)
_AGENT_INPUT = ("prompt", "composer", "message", "chat", "输入", "消息", "任务", "随心")
_UNLABELLED_AGENT_BUNDLES = {
    "com.openai.codex", "com.openai.chat", "com.openai.chatgpt",
    "com.anthropic.claudefordesktop", "com.anthropic.claude", "ai.perplexity.macv3",
}
_UNLABELLED_AGENT_NAMES = {"codex", "chatgpt", "claude", "豆包", "doubao", "perplexity"}
_NON_CHAT_INPUT = (
    "code", "source", "filename", "file name", "filter", "command", "url",
    "代码", "源码", "文件名", "路径", "筛选", "命令", "普通字段",
)


class _NativeAPI:
    """只使用公开的 AppKit、AX 和 CoreGraphics API。"""

    def __init__(self) -> None:
        if sys.platform != "darwin":
            raise TargetError("目标窗口功能仅支持 macOS。")
        try:
            import AppKit
            import ApplicationServices
            import CoreFoundation
            import Quartz
            import objc
        except ImportError as exc:
            raise TargetError(DEPENDENCY_MESSAGE) from exc
        self.appkit = AppKit
        self.ax = ApplicationServices
        self.cf = CoreFoundation
        self.quartz = Quartz
        self.objc = objc

    def pool(self) -> Any:
        return self.objc.autorelease_pool()

    def trusted(self) -> bool:
        return bool(self.ax.AXIsProcessTrusted())

    def applications(self) -> list[Any]:
        return list(self.appkit.NSWorkspace.sharedWorkspace().runningApplications())

    def application(self, pid: int) -> Any:
        return self.appkit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)

    @staticmethod
    def _process_started(pid: int) -> float | None:
        # Some Mac apps omit launchDate; ps still provides a start time to detect PID reuse.
        try:
            result = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "lstart="],
                                    capture_output=True, text=True, timeout=1,
                                    env={**os.environ, "LC_ALL": "C"}, check=False)
            if result.returncode != 0:
                return None
            return datetime.strptime(result.stdout.strip(), "%a %b %d %H:%M:%S %Y").timestamp()
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return None

    def app_info(self, app: Any) -> dict[str, Any]:
        date = app.launchDate()
        bundle = str(app.bundleIdentifier() or "")
        name = str(app.localizedName() or "")
        url = app.bundleURL()
        policy = int(app.activationPolicy())
        started = float(date.timeIntervalSince1970()) if date else None
        if started is None and policy == 0 and (bundle.casefold() in _BUNDLE_IDS or name.strip().casefold() in _APP_NAMES):
            started = self._process_started(int(app.processIdentifier()))
        return {
            "pid": int(app.processIdentifier()),
            "name": name,
            "identity": bundle or (str(url.path()) if url else ""),
            "bundle": bundle,
            "started": started,
            "regular": policy == 0,
            "activation_policy": policy,
            "terminated": bool(app.isTerminated()),
        }

    def root(self, pid: int) -> Any:
        return self.ax.AXUIElementCreateApplication(pid)

    def read_with_status(self, element: Any, attribute: str) -> tuple[int | None, Any]:
        try:
            error, value = self.ax.AXUIElementCopyAttributeValue(element, attribute, None)
            return int(error), value
        except Exception:
            return None, None

    def get(self, element: Any, attribute: str, default: Any = None) -> Any:
        error, value = self.read_with_status(element, attribute)
        return value if error == 0 else default

    def settable(self, element: Any, attribute: str) -> bool:
        try:
            error, value = self.ax.AXUIElementIsAttributeSettable(element, attribute, None)
            return error == 0 and bool(value)
        except Exception:
            return False

    def rect_values(self, position, size):
        try:
            ok1, point = self.ax.AXValueGetValue(position, self.ax.kAXValueCGPointType, None)
            ok2, extent = self.ax.AXValueGetValue(size, self.ax.kAXValueCGSizeType, None)
            if ok1 and ok2:
                return point.x, point.y, extent.width, extent.height
        except Exception:
            pass
        return ()

    def set(self, element: Any, attribute: str, value: Any) -> None:
        if self.write_with_status(element, attribute, value) != 0:
            raise TargetError("目标输入框不支持聚焦或光标定位，请先点击输入框再试。")

    def write_with_status(self, element: Any, attribute: str, value: Any) -> int | None:
        try:
            return int(self.ax.AXUIElementSetAttributeValue(element, attribute, value))
        except Exception:
            return None

    def equal(self, first: Any, second: Any) -> bool:
        return first is not None and second is not None and bool(self.cf.CFEqual(first, second))

    def activate(self, app: Any, window: Any) -> None:
        if not app.activateWithOptions_(self.appkit.NSApplicationActivateIgnoringOtherApps):
            raise TargetError("目标应用未能获得焦点，请手动打开后重试。")
        if self.get(window, "AXMinimized", False) and self.settable(window, "AXMinimized"):
            self.set(window, "AXMinimized", False)
        if self.settable(window, "AXMain"):
            self.set(window, "AXMain", True)
        # 某些原生窗口不支持 Raise；后续必须证明目标窗口实际获得焦点。
        self.ax.AXUIElementPerformAction(window, "AXRaise")

    def frontmost_pid(self) -> int | None:
        app = self.appkit.NSWorkspace.sharedWorkspace().frontmostApplication()
        return int(app.processIdentifier()) if app else None

    def selection(self, element: Any) -> tuple[int, int] | None:
        value = self.get(element, "AXSelectedTextRange")
        if value is None:
            try:
                ranges = self.get(element, "AXSelectedTextRanges")
                if ranges is None or len(ranges) != 1:
                    return None
                value = ranges[0]
            except (TypeError, IndexError, KeyError):
                return None
        try:
            ok, span = self.ax.AXValueGetValue(value, self.ax.kAXValueCFRangeType, None)
            if ok:
                if hasattr(span, "location") and hasattr(span, "length"):
                    pair = (span.location, span.length)
                elif isinstance(span, (tuple, list)) and len(span) == 2:
                    pair = tuple(span)
                else:
                    return None
                if all(isinstance(part, int) and not isinstance(part, bool) and part >= 0 for part in pair):
                    return pair
        except Exception:
            pass
        return None

    def set_selection(self, element: Any, location: int) -> None:
        value = self.ax.AXValueCreate(self.ax.kAXValueCFRangeType, (location, 0))
        if value is None:
            raise TargetError("无法定位目标输入框光标，已取消填入。")
        self.set(element, "AXSelectedTextRange", value)

    def clipboard(self, text: str) -> None:
        board = self.appkit.NSPasteboard.generalPasteboard()
        board.clearContents()
        if not board.setString_forType_(text, self.appkit.NSPasteboardTypeString):
            raise TargetError("无法写入系统剪贴板，已取消填入。")

    def _post_key(self, key: int, flags: int, action: str) -> None:
        q = self.quartz
        events = [q.CGEventCreateKeyboardEvent(None, key, down) for down in (True, False)]
        if any(event is None for event in events):
            raise TargetError(f"无法创建{action}事件，已取消填入。")
        for event in events:
            q.CGEventSetFlags(event, flags)
            q.CGEventPost(q.kCGHIDEventTap, event)

    def move_caret(self, *, to_end: bool) -> None:
        # 文末用 Command+Down；Right 仅用于折叠已确认存在的选区。
        self._post_key(125 if to_end else 124,
                       self.quartz.kCGEventFlagMaskCommand if to_end else 0, "光标定位")

    def paste(self) -> None:
        # 没有 Return、Enter 或提交按钮事件。
        self._post_key(9, self.quartz.kCGEventFlagMaskCommand, "粘贴")


@dataclass(frozen=True)
class FocusToken:
    window_id: int
    pid: int
    process_started: float
    element: Any


@dataclass
class _WindowRecord:
    target: TargetWindow
    app: Any
    root: Any
    element: Any


def _with_native_pool(method: Any) -> Any:
    @wraps(method)
    def wrapped(self: Any, *args: Any, **kwargs: Any) -> Any:
        if method.__name__ == "list_windows":
            self.last_scan_note = "正在刷新 Mac 目标窗口。"
            self.last_scan_report = "Mac 目标窗口诊断\n辅助功能授权：尚未判断\n状态：正在刷新"
        try:
            api = self._ensure_api()
        except TargetError as exc:
            if method.__name__ == "list_windows":
                self._records.clear()
                state = "未允许" if str(exc) == PERMISSION_MESSAGE else "无法判断"
                self.last_scan_note = str(exc)
                self.last_scan_report = f"Mac 目标窗口诊断\n辅助功能授权：{state}\n状态：{exc}"
            raise
        except Exception as exc:
            if method.__name__ != "list_windows":
                raise
            self._records.clear()
            self.last_scan_note = "Mac 目标窗口组件初始化失败，请复制诊断查看。"
            self.last_scan_report = (
                "Mac 目标窗口诊断\n辅助功能授权：无法判断\n"
                f"状态：组件初始化失败（{type(exc).__name__}）"
            )
            raise TargetError(self.last_scan_note) from exc
        pool = getattr(api, "pool", None)
        with pool() if pool else nullcontext():
            return method(self, *args, **kwargs)
    return wrapped


class MacTargetBackend(MacInputPositions):
    """在上层单个 worker 线程中串行使用，不请求系统权限变更。"""

    list_input_positions = _with_native_pool(MacInputPositions.list_input_positions)
    validate_input_position = _with_native_pool(MacInputPositions.validate_input_position)
    preview_input_position = _with_native_pool(MacInputPositions.preview_input_position)
    set_input_binding = _with_native_pool(MacInputPositions.set_input_binding)

    def __init__(self, *, excluded_apps: Iterable[str] | None = None, api: Any = None):
        self._api = api
        self._excluded_apps = {str(name).strip().casefold() for name in (excluded_apps or ())}
        self._records: dict[int, _WindowRecord] = {}
        self._next_id = 1
        self.last_scan_report = "Mac 目标窗口诊断\n尚未刷新目标窗口。"
        self.last_scan_note = "尚未刷新目标窗口。"

    def _ensure_api(self) -> Any:
        if self._api is None:
            self._api = _NativeAPI()
        if not self._api.trusted():
            raise TargetError(PERMISSION_MESSAGE)
        return self._api

    def close(self) -> None:
        self._records.clear()

    def _allowed(self, info: dict[str, Any]) -> bool:
        return not self._skip_reason(info)

    def _skip_reason(self, info: dict[str, Any]) -> str:
        name = str(info.get("name", "")).strip().casefold()
        bundle = str(info.get("bundle", "")).casefold()
        checks = (
            (info.get("pid") == os.getpid(), "当前进程"),
            (name in self._excluded_apps, "已排除的应用"),
            ("talkman" in name or "talkman" in bundle, "TalkMan 自身"),
            (bool(info.get("terminated")), "应用已退出"),
            (info.get("regular") is not True, "后台或无常规窗口应用"),
            (bundle not in _BUNDLE_IDS and name not in _APP_NAMES, "应用名称和标识未匹配"),
            (not info.get("identity"), "缺少应用身份"),
            (info.get("started") is None, "缺少可验证的启动身份"),
        )
        return next((reason for rejected, reason in checks if rejected), "")

    @staticmethod
    def _read_attribute(api: Any, element: Any, attribute: str) -> tuple[int | None, Any]:
        try:
            method = getattr(api, "read_with_status", None)
            if method:
                return method(element, attribute)
            return 0, api.get(element, attribute)
        except Exception:
            return None, None

    def _read_windows(self, api: Any, root: Any) -> tuple[list[Any], int | None, str]:
        status, value = self._read_attribute(api, root, "AXWindows")
        windows: list[Any] = []

        def add(element: Any, *, fallback: bool) -> bool:
            if element is None or api.get(element, "AXRole") != "AXWindow":
                return False
            parent = api.get(element, "AXParent")
            if fallback and not api.equal(parent, root):
                return False
            if parent is not None and not api.equal(parent, root):
                return False
            if any(api.equal(element, previous) for previous in windows):
                return False
            windows.append(element)
            return True

        if status == 0 and value:
            try:
                for element in value:
                    add(element, fallback=False)
            except TypeError:
                pass
        sources: list[str] = []
        if not windows:
            for attribute in ("AXFocusedWindow", "AXMainWindow"):
                if add(api.get(root, attribute), fallback=True):
                    sources.append(attribute)
            children = api.get(root, "AXChildren", []) or []
            for element in children:
                if add(element, fallback=True) and "AXChildren" not in sources:
                    sources.append("AXChildren")
        return windows, status, "、".join(sources) or "无"

    @staticmethod
    def _status_label(status: int | None) -> str:
        return str(status) if status is not None else "读取异常"

    def _enable_manual_accessibility(self, api: Any, root: Any) -> str:
        # Electron 为第三方辅助技术提供此属性，用于开启可访问树。
        try:
            if not api.settable(root, "AXManualAccessibility"):
                return "不支持，未尝试"
            if api.get(root, "AXManualAccessibility", False):
                return "已启用，未重复设置"
            method = getattr(api, "write_with_status", None)
            if method:
                status = method(root, "AXManualAccessibility", True)
                return "已尝试并启用" if status == 0 else f"尝试失败（AX 错误 {self._status_label(status)}）"
            api.set(root, "AXManualAccessibility", True)
            return "已尝试并启用"
        except Exception:
            return "尝试失败（调用异常）"

    @_with_native_pool
    def list_windows(self) -> list[TargetWindow]:
        api = self._ensure_api()
        records: dict[int, _WindowRecord] = {}
        lines = ["Mac 目标窗口诊断", "辅助功能授权：已允许"]
        try:
            apps = api.applications()
        except Exception as exc:
            self._records.clear()
            self.last_scan_note = "无法读取 Mac 运行应用列表，请退出并重新打开 TalkMan 后再试。"
            lines.append(f"状态：运行应用列表读取失败（{type(exc).__name__}）")
            self.last_scan_report = "\n".join(lines)
            raise TargetError(self.last_scan_note) from exc
        candidate_count = 0
        blocked: list[str] = []
        errors: list[str] = []
        for app in apps:
            prefix = "应用信息读取失败"
            info: dict[str, Any] = {}
            try:
                info = api.app_info(app)
                clean = lambda value: str(value or "未知").replace("\r", " ").replace("\n", " ")
                policy = info.get("activation_policy", "常规" if info.get("regular") else "后台")
                prefix = (
                    f"应用：{clean(info.get('name'))} | 应用标识：{clean(info.get('bundle'))}"
                    f" | 进程号：{info.get('pid')} | 策略：{policy}"
                )
                recognized = (
                    str(info.get("name", "")).strip().casefold() in _APP_NAMES
                    or str(info.get("bundle", "")).casefold() in _BUNDLE_IDS
                )
                reason = self._skip_reason(info)
                if reason:
                    lines.append(f"{prefix} | 跳过：{reason}")
                    if recognized:
                        blocked.append(f"{clean(info.get('name'))}：{reason}")
                    continue
                candidate_count += 1
                root = api.root(info["pid"])
                windows, status, fallback = self._read_windows(api, root)
                initial_status = status
                manual = "未需要"
                if not windows:
                    manual = self._enable_manual_accessibility(api, root)
                    if manual == "已尝试并启用":
                        windows, status, fallback = self._read_windows(api, root)
                if initial_status != 0:
                    errors.append(f"{clean(info.get('name'))}：{self._status_label(initial_status)}")
                elif manual.startswith("尝试失败"):
                    errors.append(f"{clean(info.get('name'))}：辅助树启用失败")
                lines.append(
                    f"{prefix} | AXWindows：{self._status_label(initial_status)}"
                    f" | 可用窗口：{len(windows)} | 回退：{fallback} | 辅助树：{manual}"
                    + (f" | 重读 AXWindows：{self._status_label(status)}" if manual == "已尝试并启用" else "")
                )
                for element in windows:
                    title = str(api.get(element, "AXTitle", "") or "").strip() or info["name"]
                    previous = next((record for record in self._records.values() if (
                        record.target.pid == info["pid"]
                        and record.target.process_started == info["started"]
                        and record.target.identity == info["identity"]
                        and api.equal(record.element, element)
                    )), None)
                    window_id = previous.target.hwnd if previous else self._next_id
                    if previous is None:
                        self._next_id += 1
                    target = TargetWindow(
                        hwnd=window_id, pid=info["pid"], app=info["name"], title=title,
                        identity=info["identity"], process_started=info["started"],
                    )
                    records[window_id] = _WindowRecord(target, app, root, element)
            except Exception as exc:
                lines.append(f"{prefix} | 枚举异常：{type(exc).__name__}")
                errors.append(f"{info.get('name', '未知应用')}：枚举异常")
                continue
        self._records = records
        lines.insert(2, f"运行应用：{len(apps)} | 允许候选：{candidate_count} | 可用窗口：{len(records)}")
        self.last_scan_report = "\n".join(lines)
        if records:
            self.last_scan_note = f"已发现 {len(records)} 个目标窗口。"
            if errors:
                self.last_scan_note += "部分应用读取失败，可复制诊断查看。"
        elif errors:
            self.last_scan_note = f"辅助功能已允许，但窗口读取失败（{errors[0]}）。请复制诊断查看。"
        elif blocked:
            self.last_scan_note = f"未获得目标窗口（{blocked[0]}）。请复制诊断查看。"
        elif candidate_count:
            self.last_scan_note = "已识别目标应用，但没有可用窗口。请打开聊天窗口后刷新；仍为空可复制诊断。"
        else:
            self.last_scan_note = "未识别到支持的目标应用。请打开豆包等聊天窗口；仍为空可复制诊断。"
        return [record.target for record in records.values()]

    enumerate_windows = list_windows

    def _validate(self, target: TargetWindow) -> _WindowRecord | None:
        api = self._ensure_api()
        record = self._records.get(target.hwnd)
        if record is None or record.target.pid != target.pid:
            return None
        app = api.application(target.pid)
        if app is None:
            return None
        info = api.app_info(app)
        if (
            not self._allowed(info)
            or info["identity"] != target.identity
            or info["started"] != target.process_started
            or record.target.process_started != target.process_started
        ):
            return None
        root = api.root(target.pid)
        windows, _status, _fallback = self._read_windows(api, root)
        if not any(api.equal(element, record.element) for element in windows):
            return None
        return _WindowRecord(record.target, app, root, record.element)

    @_with_native_pool
    def validate_window(self, target: TargetWindow) -> TargetWindow | None:
        record = self._validate(target)
        if not record:
            return None
        return replace(record.target, title=str(self._api.get(record.element, "AXTitle", "") or ""))

    def _same_window(self, record: _WindowRecord) -> bool:
        api = self._api
        return (
            api.frontmost_pid() == record.target.pid
            and api.equal(api.get(record.root, "AXFocusedWindow"), record.element)
        )

    def _belongs(self, element: Any, record: _WindowRecord) -> bool:
        api = self._api
        seen: list[Any] = []
        current = element
        for _ in range(80):
            if current is None or any(api.equal(current, item) for item in seen):
                return False
            if api.equal(current, record.element):
                return True
            seen.append(current)
            window = api.get(current, "AXWindow")
            if window is not None:
                return api.equal(window, record.element)
            current = api.get(current, "AXParent")
        return False

    def _input_label(self, element: Any) -> str:
        api = self._api
        return " ".join(str(api.get(element, attr, "") or "") for attr in (
            "AXTitle", "AXDescription", "AXIdentifier", "AXPlaceholderValue", "AXSubrole",
        )).casefold()

    def _is_editable(self, element: Any) -> bool:
        api = self._api
        if api.get(element, "AXRole") not in ("AXTextArea", "AXTextField"):
            return False
        if not api.get(element, "AXEnabled", True) or api.get(element, "AXHidden", False):
            return False
        if any(word in self._input_label(element) for word in _EXCLUDED_INPUT):
            return False
        if api.get(element, "AXSubrole") in ("AXSecureTextField", "AXSearchField"):
            return False
        # 仅有可选中正文不代表可写，必须有明确的编辑证据。
        return api.settable(element, "AXValue") or bool(api.get(element, "AXEditable", False))

    def _focused_input(self, record: _WindowRecord) -> Any:
        if not self._same_window(record):
            return None
        element = self._api.get(record.root, "AXFocusedUIElement")
        pinned = self._bound_input(record.target)
        if pinned is not None and not self._api.equal(pinned, element):
            return None
        if element is not None and self._belongs(element, record) and self._is_editable(element):
            return element
        return None

    def _remembered_input(self, target: TargetWindow, record: _WindowRecord, token: Any) -> Any:
        if (
            not isinstance(token, FocusToken)
            or token.window_id != target.hwnd
            or token.pid != target.pid
            or token.process_started != target.process_started
            or token.element is None
            or not self._belongs(token.element, record)
            or not self._is_editable(token.element)
        ):
            return None
        return token.element

    def _main_input(self, record: _WindowRecord) -> Any:
        pinned = self._bound_input(record.target)
        if pinned is not None:
            return pinned
        api = self._api
        candidates: list[Any] = []
        unlabelled: list[Any] = []
        fallback_allowed = (
            record.target.app.strip().casefold() in _UNLABELLED_AGENT_NAMES
            or record.target.identity.casefold() in _UNLABELLED_AGENT_BUNDLES
        )
        seen: list[Any] = []
        pending = [record.element]
        while pending:
            element = pending.pop()
            if any(api.equal(element, other) for other in seen):
                continue
            seen.append(element)
            if len(seen) > 3000:
                raise TargetError("目标界面内容较多，请先点击 Agent 的主输入框再试。")
            if self._is_editable(element) and self._belongs(element, record):
                label = self._input_label(element)
                if any(word in label for word in _AGENT_INPUT):
                    candidates.append(element)
                elif (
                    fallback_allowed and api.get(element, "AXRole") == "AXTextArea"
                    and not any(word in label for word in _NON_CHAT_INPUT)
                ):
                    unlabelled.append(element)
            pending.extend(api.get(element, "AXChildren", []) or [])
        main = [element for element in candidates if self.input_position_role(record.target, element)]
        if main:
            return main[0] if len(main) == 1 else None
        ranked = sorted((
            (sum(weight for word, weight in _MAIN_INPUT if word in self._input_label(element)), element)
            for element in candidates
        ), key=lambda pair: pair[0], reverse=True)
        if ranked:
            return None if len(ranked) > 1 and ranked[0][0] == ranked[1][0] else ranked[0][1]
        if len(unlabelled) != 1:
            return None
        # 网页未聚焦时可能不暴露选区；先要求可靠字符数，聚焦后仍须读回光标。
        count = api.get(unlabelled[0], "AXNumberOfCharacters")
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            return None
        return unlabelled[0]

    @_with_native_pool
    def capture_focus(self, target: TargetWindow) -> FocusToken | bool | None:
        record = self._validate(target)
        if record is None:
            return False
        element = self._focused_input(record)
        if element is None:
            return None
        return FocusToken(target.hwnd, target.pid, target.process_started, element)

    def _assert_focus(self, target: TargetWindow, element: Any) -> _WindowRecord:
        record = self._validate(target)
        if record is None:
            raise TargetError("目标窗口已关闭或会话已变化，请刷新并重新选择。")
        current = self._focused_input(record)
        if current is None or not self._api.equal(current, element):
            raise TargetError("目标输入框已失去焦点，已取消填入。")
        return record

    def _wait_focus(self, target: TargetWindow, element: Any = None) -> _WindowRecord:
        deadline = time.monotonic() + 1.5
        while True:
            record = self._validate(target)
            if record is None:
                raise TargetError("目标窗口已关闭或会话已变化，请刷新并重新选择。")
            if self._same_window(record):
                focused = self._focused_input(record) if element is not None else None
                if element is None or (focused is not None and self._api.equal(focused, element)):
                    return record
            if time.monotonic() >= deadline:
                stage = "目标输入框" if self._same_window(record) else "目标窗口"
                raise TargetError(f"{stage}未获得焦点，已取消填入。")
            time.sleep(0.03)

    def _focus_input(self, target: TargetWindow, element: Any, *, remembered: bool) -> _WindowRecord:
        record = self._validate(target)
        if record is None:
            raise TargetError("目标窗口已关闭或会话已变化，请刷新并重新选择。")
        if not self._belongs(element, record) or not self._is_editable(element):
            raise TargetError("目标输入框已变化或不可编辑，已取消自动聚焦。")
        source = "先前选中的输入框" if remembered else "主输入框"
        if not self._api.settable(element, "AXFocused"):
            raise TargetError(
                f"自动聚焦失败：已找到{source}，但它不支持 AXFocused 设置。"
                "请先点击 Agent 输入框再试。"
            )
        try:
            self._api.set(element, "AXFocused", True)
        except Exception as exc:
            raise TargetError(
                f"自动聚焦失败：{source}的 AXFocused 设置未成功。"
                "请先点击 Agent 输入框再试。"
            ) from exc
        return self._wait_focus(target, element)

    def _wait_selection(
        self, target: TargetWindow, element: Any, location: int | None = None,
        *, retry_append_from: tuple[int, int] | None = None,
    ) -> Any:
        # 部分网页输入框在聚焦或按键之后，辅助功能选区会稍后才更新。
        timeout = 1.5 if retry_append_from is not None else 0.75
        deadline = time.monotonic() + timeout
        retry_at = deadline - timeout + 0.15
        retried = False
        while True:
            self._assert_focus(target, element)
            selected = self._api.selection(element)
            if retry_append_from is not None:
                count = self._api.get(element, "AXNumberOfCharacters")
                if not isinstance(count, int) or isinstance(count, bool) or count != location:
                    raise TargetError("目标文字末尾已变化，已取消填入。")
                if selected is not None and selected not in (retry_append_from, (location, 0)):
                    raise TargetError("目标光标或选区已变化，已取消填入。")
            if selected is not None and (location is None or selected == (location, 0)):
                return selected
            now = time.monotonic()
            if now >= deadline:
                if location is None:
                    raise TargetError("目标输入框无法读取光标位置，请先点击 Agent 输入框再试；仍失败时复制后手动粘贴。")
                raise TargetError("无法确认目标光标位置，已取消填入。请先点击 Agent 输入框再试。")
            if (retry_append_from is not None and not retried and now >= retry_at
                    and selected == retry_append_from):
                # 刚聚焦时 AX 设置或定位按键可能未生效；只重试定位，不重试粘贴。
                self._assert_focus(target, element)
                if self._api.selection(element) != retry_append_from:
                    raise TargetError("目标光标或选区已变化，已取消填入。")
                count = self._api.get(element, "AXNumberOfCharacters")
                if not isinstance(count, int) or isinstance(count, bool) or count != location:
                    raise TargetError("目标文字末尾已变化，已取消填入。")
                self._api.move_caret(to_end=True)
                retried = True
            time.sleep(0.03)

    def _assert_caret(self, target: TargetWindow, element: Any, location: int, *, append: bool) -> None:
        self._assert_focus(target, element)
        if self._api.selection(element) != (location, 0):
            raise TargetError("目标光标或选区已变化，已取消填入。")
        if append:
            count = self._api.get(element, "AXNumberOfCharacters")
            if not isinstance(count, int) or isinstance(count, bool) or count < 0 or count != location:
                raise TargetError("目标文字末尾已变化，已取消填入。")
        self._assert_focus(target, element)

    @_with_native_pool
    def open_sync(self, target, *, require_write=True):
        record = self._validate(target)
        if record is None:
            raise TargetError('目标窗口不可用，请重新选择。')
        element = self._main_input(record)
        if element is None or (require_write and not self._api.settable(element, 'AXValue')):
            raise TargetError('此输入框不支持后台同步，请关闭同步后使用「填入」。')
        return element, str(self._api.get(record.element, 'AXTitle', '') or '')

    @_with_native_pool
    def read_sync(self, target, handle, *, require_write=True):
        record = self._validate(target)
        element, title = handle
        if (record is None or str(self._api.get(record.element, 'AXTitle', '') or '') != title
                or not self._belongs(element, record) or not self._is_editable(element)):
            raise TargetError('目标窗口或对话已改变，同步已暂停。')
        main = self._main_input(record)
        if main is None or not self._api.equal(main, element):
            raise TargetError('目标主输入框已改变，同步已暂停。')
        value = self._api.get(element, 'AXValue')
        if not isinstance(value, str) or (require_write and not self._api.settable(element, 'AXValue')):
            raise TargetError('无法读回目标输入框，同步已暂停。')
        return value

    def open_delivery(self, target):
        return self.open_sync(target, require_write=False)

    def read_delivery(self, target, handle):
        return self.read_sync(target, handle, require_write=False)

    def send_delivery(self, target, handle, text, current):
        return self.send_sync(target, handle, text, current, delivery=True)

    @_with_native_pool
    def write_sync(self, target, handle, text, expected, current):
        if self.read_sync(target, handle) != expected or not current():
            raise TargetError('目标文字已变化，同步已暂停。')
        self._api.set(handle[0], 'AXValue', text)

    @_with_native_pool
    def send_sync(self, target, handle, text, current, *, delivery=False):
        if not self.input_allows_submit():
            raise TargetError('此输入位置的提交方式尚未识别，请在目标内提交。')
        read = self.read_delivery if delivery else self.read_sync
        if read(target, handle) != text or not current():
            raise TargetError('目标文字已变化，本次未下发。')
        self._focus_input(target, handle[0], remembered=True)
        self._assert_focus(target, handle[0])
        if read(target, handle) != text or not current():
            raise TargetError('目标文字已变化，本次未下发。')
        self._api._post_key(36, 0, '下发')
        return '已向目标输入框按下回车，请在目标确认发送结果；同步已暂停。'

    @_with_native_pool
    def fill(self, target: TargetWindow, text: str, remembered_focus: FocusToken | None = None) -> str:
        if not isinstance(text, str) or not text:
            raise TargetError("没有可填入的文字。")
        record = self._validate(target)
        if record is None:
            raise TargetError("目标窗口已关闭或会话已变化，请刷新并重新选择。")
        api = self._api
        pinned = self._bound_input(target)
        if pinned is not None:
            remembered_focus = FocusToken(target.hwnd, target.pid, target.process_started, pinned)
        current = self._focused_input(record)
        if pinned is None and current is not None and not self.input_position_role(target, current):
            preferred = self._main_input(record)
            if preferred is not None and self.input_position_role(target, preferred):
                if current is not None and not api.equal(current, preferred):
                    current = None
                if remembered_focus is not None and not api.equal(remembered_focus.element, preferred):
                    remembered_focus = None
        originally_focused = current is not None
        if current is None:
            api.activate(record.app, record.element)
            record = self._wait_focus(target)
            current = self._focused_input(record)
        preserve = originally_focused and remembered_focus is None
        if not preserve and remembered_focus is None:
            current = None
        remembered = self._remembered_input(target, record, remembered_focus)
        if remembered_focus is not None:
            preserve = bool(
                remembered is not None
                and current is not None
                and api.equal(current, remembered)
            )
            if not preserve:
                current = None
        if current is None:
            # 用户实际聚焦过的控件可以恢复，即使网页没有给它聊天输入标签。
            current = remembered if remembered is not None else self._main_input(record)
            if current is None:
                raise TargetError("未找到唯一可编辑的主输入框，请先点击 Agent 输入框再试。")
            record = self._focus_input(target, current, remembered=remembered is not None)
        self._assert_focus(target, current)
        selected = self._wait_selection(target, current)
        if (
            not isinstance(selected, (tuple, list)) or len(selected) != 2
            or any(not isinstance(part, int) or isinstance(part, bool) or part < 0 for part in selected)
        ):
            raise TargetError("目标光标位置无效，已取消填入。")
        selected = tuple(selected)
        if preserve:
            location = selected[0] + selected[1]
        else:
            count = api.get(current, "AXNumberOfCharacters")
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                raise TargetError("无法确认目标文字末尾，请先点击 Agent 输入框再试。")
            location = count
        if selected != (location, 0):
            self._assert_focus(target, current)
            if api.settable(current, "AXSelectedTextRange"):
                api.set_selection(current, location)
            else:
                api.move_caret(to_end=not preserve)
            self._wait_selection(
                target, current, location,
                retry_append_from=selected if not preserve else None,
            )
        self._assert_caret(target, current, location, append=not preserve)
        api.clipboard(text)
        self._assert_caret(target, current, location, append=not preserve)
        api.paste()
        return "已填入 Agent 输入框（未发送）。"


def create_backend() -> MacTargetBackend:
    return MacTargetBackend()


__all__ = ["MacTargetBackend", "FocusToken", "PERMISSION_MESSAGE", "DEPENDENCY_MESSAGE", "create_backend"]
