"""目标输入框选择与投递核心。

本模块不读取目标窗口的正文，也不在 Tk 线程中调用平台 UI 自动化 API。
平台后端只需要实现 ``list_windows``、``capture_focus`` 和 ``fill``，具体的
窗口枚举与输入由一个专用 worker 串行完成。
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime
import json
import os
from pathlib import Path
import queue
import re
import sys
import tempfile
import threading
import time
import uuid
from typing import Any, Callable, Iterable
from talkman_input_positions import InputPositionsMixin


TARGET_FILE = "target.json"
UNSUPPORTED_MESSAGE = "当前平台尚未支持自动填入，请复制全部后粘贴。"


@dataclass(frozen=True)
class FillReceipt:
    message: str
    text: str
    app: str
    title: str
    time: str
    status: str = '已填入'
    send_token: str = ''


class TargetError(RuntimeError):
    """目标选择、重连或投递失败。消息面向中文界面。"""


class PositionRelocationRequired(TargetError):
    """填入前确认原输入位置已失效，需要用户重新确认；尚未写入目标。"""


def is_agent_window(window) -> bool:
    """仅用窗口身份作可关闭的列表筛选，不读取窗口正文或影响填入校验。"""
    get = window.get if isinstance(window, dict) else lambda key, default='': getattr(window, key, default)
    app = str(get('app', '')).casefold()
    identity = str(get('identity', '')).replace('\\', '/').rsplit('/', 1)[-1].casefold()
    title = str(get('title', '')).casefold()
    pattern = r'(?<![a-z0-9])(?:chatgpt|claude|codex|zcode|doubao|qwen|tongyi|kimi|deepseek|yuanbao|perplexity|gemini|cursor|trae|cherry[ _-]?studio|chatbox|lm[ _-]?studio|open[ _-]?webui)(?![a-z0-9])|豆包|千问|通义|元宝|文小言|文心一言'
    if re.search(pattern, app + ' ' + identity):
        return True
    # 浏览器和通用编辑器只有活动窗口标题明确标识 AI 工具时才纳入。
    generic = r'chrome|msedge|edge|firefox|safari|brave|arc|browser|code|vscodium|浏览器'
    administrative = (r'api[ _-]?keys?|api密钥|密钥管理|控制台|开发者|开发平台|开放平台|开发文档|接入文档|'
                      r'\bdocumentation\b|\bdocs\b|\bconsole\b|\bdashboard\b|\bdeveloper\b|'
                      r'\bpricing\b|\bsettings\b|账户设置|账号设置|模型设置|计费|教程')
    if re.search(generic, app + ' ' + identity) and re.search(administrative, title):
        return False
    return bool(re.search(generic, app + ' ' + identity) and re.search(pattern, title))


@dataclass(frozen=True)
class TargetWindow:
    """可展示的窗口身份。

    ``hwnd``、``pid`` 只代表本次运行的会话句柄，不能写入配置并跨重启复用。
    ``identity`` 通常为进程可执行路径，是跨会话重连的主要身份。
    """

    hwnd: Any = None
    pid: int | None = None
    app: str = ""
    title: str = ""
    identity: str = ""
    process_started: str | float | int | None = None

    @classmethod
    def from_value(cls, value: Any) -> "TargetWindow":
        if isinstance(value, cls):
            return value
        # 平台后端可保留自己的 dataclass；这里只取
        # 两边约定的身份字段，避免 core 依赖平台模块形成循环导入。
        if isinstance(value, dict):
            get = value.get
            hwnd = get("hwnd", get("id"))
            pid = get("pid")
            app = get("app", "")
            title = get("title", "")
            identity = get("identity", get("process_path", get("process", "")))
            process_started = get("process_started")
        else:
            if not all(hasattr(value, name) for name in ("pid", "app", "title")):
                raise TargetError("目标窗口信息格式无效。")
            hwnd = getattr(value, "hwnd", getattr(value, "id", None))
            pid = getattr(value, "pid", None)
            app = getattr(value, "app", "")
            title = getattr(value, "title", "")
            identity = getattr(value, "identity", "") or getattr(value, "process_path", "")
            process_started = getattr(value, "process_started", None)
        try:
            pid = int(pid) if pid is not None else None
        except (TypeError, ValueError):
            pid = None
        return cls(
            hwnd=hwnd,
            pid=pid,
            app=str(app or ""),
            title=str(title or ""),
            identity=str(identity or ""),
            process_started=process_started,
        )

    @property
    def id(self) -> Any:
        """与平台后端的 ``id`` 命名兼容。"""
        return self.hwnd

    @property
    def process_path(self) -> str:
        """Windows 后端使用的进程身份别名。"""
        return self.identity

    def session_key(self) -> tuple[Any, int | None]:
        return self.hwnd, self.pid

    def metadata(self) -> dict[str, str]:
        """返回可跨进程保存的字段，不包含 hwnd、pid 或进程启动时间。"""
        return {"app": self.app, "title": self.title, "identity": self.identity}

    def display_name(self) -> str:
        return self.title or self.app or self.identity or "未命名窗口"


class TargetSelection:
    """目标选择的持久化容器，只保存身份元数据。"""

    def __init__(self, data_dir: Path, metadata: dict[str, Any] | None = None):
        self.data_dir = Path(data_dir)
        self.metadata = self._clean(metadata or {})
        self.positions = []

    @staticmethod
    def _clean(value: dict[str, Any]) -> dict[str, Any]:
        allowed = ("app", "title", "identity", "input_mode")
        result = {key: str(value.get(key, "") or "") for key in allowed if value.get(key)}
        role = value.get('input_role')
        if isinstance(role, (list, tuple)) and len(role) == 2:
            from talkman_input_positions import main_input_role
            known = main_input_role(result, role[1] if role[0] == 'id' else '',
                                    [role[1]] if role[0] == 'label' else [])
            if known == tuple(role):
                result['input_role'] = list(known)
        return result

    @staticmethod
    def position_key(value):
        # 不把坐标、位置序号或正文当作身份。运行中的原生控件另按窗口会话隔离。
        return (str(value.get('identity', '')).casefold(),
                str(value.get('app', '')).casefold(), str(value.get('title', '')))

    def position_for(self, target):
        key = self.position_key(target.metadata())
        return next((dict(item) for item in reversed(self.positions)
                     if self.position_key(item) == key), None)

    def remember_position(self, metadata):
        item = self._clean(metadata)
        key = self.position_key(item)
        self.positions = [old for old in self.positions if self.position_key(old) != key]
        self.positions = (self.positions + [item])[-24:]

    @property
    def path(self) -> Path:
        return self.data_dir / TARGET_FILE

    @property
    def selected(self) -> bool:
        return bool(self.metadata.get("identity") or self.metadata.get("title") or self.metadata.get("app"))

    @classmethod
    def load(cls, data_dir: Path) -> "TargetSelection":
        selection = cls(data_dir)
        path = selection.path
        if not path.exists():
            return selection
        try:
            if path.stat().st_size > 131072:
                raise TargetError("目标设置文件过大，请重新选择目标输入框。")
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise TargetError("目标设置未能读取，请重新选择目标输入框。")
            payload = raw.get("target", raw)
            if not isinstance(payload, dict):
                raise TargetError("目标设置未能读取，请重新选择目标输入框。")
            selection.metadata = cls._clean(payload)
            positions = raw.get('positions', [])
            if isinstance(positions, list):
                for item in positions[-24:]:
                    if isinstance(item, dict) and item.get('input_mode') == 'explicit':
                        selection.remember_position(item)
            # 兼容只存最后一个输入框的旧配置。
            if selection.metadata.get('input_mode') == 'explicit':
                selection.remember_position(selection.metadata)
            return selection
        except TargetError:
            raise
        except (OSError, ValueError, UnicodeError, TypeError):
            raise TargetError("目标设置未能读取，请重新选择目标输入框。") from None

    def set(self, target: TargetWindow | dict[str, Any]) -> dict[str, str]:
        target = TargetWindow.from_value(target)
        self.metadata = self.position_for(target) or self._clean(target.metadata())
        return dict(self.metadata)

    def clear(self) -> None:
        self.metadata = {}

    def save(self) -> None:
        directory = self.data_dir
        temporary: Path | None = None
        try:
            directory.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=directory, suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump({"version": 2, "target": self.metadata, "positions": self.positions},
                          stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            temporary = None
        except OSError:
            raise TargetError("目标设置保存失败，请检查数据目录。") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def _as_windows(values: Iterable[Any]) -> list[TargetWindow]:
    result: list[TargetWindow] = []
    for value in values or ():
        try:
            target = TargetWindow.from_value(value)
        except TargetError:
            continue
        if target.hwnd is not None or target.identity or target.title:
            result.append(target)
    return result


class _UnsupportedBackend:
    def list_windows(self) -> list[TargetWindow]:
        raise TargetError(UNSUPPORTED_MESSAGE)


Callback = Callable[[str, Any], None]


class TargetController(InputPositionsMixin):
    """目标窗口 worker。

    所有后端调用都在同一线程发生，以满足 Windows UIA/COM apartment 要求。
    回调也在 worker 线程触发；Tk 应用应只把事件放回自己的队列。
    """

    def __init__(self, data_dir: Path, backend: Any | None = None, platform: str | None = None):
        self.data_dir = Path(data_dir)
        self.selection = TargetSelection.load(self.data_dir)
        self.backend = backend if backend is not None else self._default_backend(platform)
        self._queue: queue.Queue[tuple[str, Any, Any]] = queue.Queue()
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._windows: list[TargetWindow] = []
        self._selected: TargetWindow | None = None
        self._focus_token: Any = None
        self._last_focus_check = 0.0
        self._refresh_note = ""
        self._sync_epoch = 0
        self._sync_lease = None
        self._pending_send = None
        self._refresh_report = "尚未刷新目标窗口。"
        self._position_relocation_needed = False
        self._init_input_positions()
        self._thread = threading.Thread(target=self._run, name="TalkManTarget", daemon=True)
        self._thread.start()

    @staticmethod
    def _default_backend(platform: str | None) -> Any:
        platform = platform or sys.platform
        if platform == "win32":
            try:
                from talkman_target_windows import WindowsTargetBackend
                return WindowsTargetBackend()
            except Exception:
                return _UnsupportedBackend()
        if platform == "darwin":
            from talkman_target_macos import MacTargetBackend
            return MacTargetBackend()
        return _UnsupportedBackend()

    @property
    def selected(self) -> TargetWindow | None:
        with self._lock:
            return self._selected

    @property
    def label(self) -> str:
        with self._lock:
            if self._selected:
                return self._selected.display_name()
            metadata = self.selection.metadata
            return str(metadata.get("title") or metadata.get("app") or "未选择目标输入框")

    @property
    def windows(self) -> list[TargetWindow]:
        with self._lock:
            return list(self._windows)

    @property
    def refresh_note(self) -> str:
        with self._lock:
            return self._refresh_note

    @property
    def refresh_report(self) -> str:
        with self._lock:
            return self._refresh_report

    def _emit(self, callback: Callback | None, state: str, value: Any) -> None:
        if callback:
            try:
                callback(state, value)
            except Exception:
                # UI 回调异常不能终止窗口 worker。
                pass

    def refresh(self, callback: Callback | None = None) -> None:
        self._queue.put(("refresh", callback, None))

    def select(self, window: TargetWindow | dict[str, Any]) -> TargetWindow:
        target = TargetWindow.from_value(window)
        if target.hwnd is None and not target.identity and not target.title:
            raise TargetError("请选择一个有效的目标窗口。")
        with self._lock:
            self._position_reset()
            self._position_relocation_needed = False
            self._selected = target
            self._sync_epoch += 1
            self._focus_token = None
            self.selection.set(target)
            self._position_restore(target)
            self.selection.save()
        return target

    def clear(self) -> None:
        with self._lock:
            self._position_reset()
            self._position_relocation_needed = False
            self._selected = None
            self._sync_epoch += 1
            self._focus_token = None
            self.selection.clear()
            self.selection.save()

    def save(self) -> None:
        self.selection.save()

    @property
    def needs_position_relocation(self) -> bool:
        with self._lock:
            return self._position_relocation_needed

    def fill(self, text: str, callback: Callback | None = None) -> None:
        if not isinstance(text, str) or not text:
            self._emit(callback, "error", "没有可填入的文字。")
            return
        self._queue.put(("fill", callback, text))

    def fill_with_receipt(self, text: str, callback: Callback | None = None) -> None:
        if not isinstance(text, str) or not text:
            self._emit(callback, "error", "没有可填入的文字。")
            return
        with self._lock:
            self._queue.put(("fill_receipt", callback, (text, self._sync_epoch)))

    def close(self) -> None:
        self.stop_sync()
        self._stop.set()
        self._queue.put(("close", None, None))
        if self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)

    def stop_sync(self):
        with self._lock:
            self._sync_epoch += 1
            self._pending_send = None

    def send_filled(self, token, callback=None):
        """只发送已核实的填入快照，绝不重新粘贴或自动重试。"""
        self._queue.put(('send_filled', callback, token))

    def _send_filled(self, callback, token):
        with self._lock:
            pending, self._pending_send = self._pending_send, None
        try:
            if pending is None or not token or pending[0] != token:
                raise TargetError('这次下发确认已失效，请检查目标输入框。')
            _, epoch, target, handle, expected, expires = pending
            def current():
                return (not self._stop.is_set() and epoch == self._sync_epoch
                        and time.monotonic() < expires and self._selected == target)
            _open, read, send = self._delivery_methods()
            if not current() or read(target, handle) != expected:
                raise TargetError('目标、文字或确认时间已变化，本次未下发；请检查目标输入框。')
            send(target, handle, expected, current)
            result = FillReceipt('已向目标按下回车，请在目标确认发送结果。', expected, target.app, target.title,
                datetime.now().astimezone().isoformat(timespec='seconds'), '已触发下发')
            self._emit(callback, 'ok', result)
        except TargetError as exc:
            self._emit(callback, 'error', str(exc))
        except Exception:
            self._emit(callback, 'error', '无法确认下发结果，不会自动重试；请检查目标输入框。')

    def _delivery_methods(self):
        names = ('open_delivery', 'read_delivery', 'send_delivery')
        if not all(callable(getattr(self.backend, name, None)) for name in names):
            names = ('open_sync', 'read_sync', 'send_sync')
        return tuple(getattr(self.backend, name, None) for name in names)

    def _prepare_send_snapshot(self, target):
        open_target, read, _send = self._delivery_methods()
        if not all(callable(method) for method in self._delivery_methods()):
            return None
        # Chromium 编辑器在焦点或布局切换时可能短暂重建 UIA 节点。
        # 只在粘贴前重新取得快照；尚未写入目标，可以等待其稳定。
        for attempt in range(3):
            try:
                handle = open_target(target)
                before = read(target, handle)
                if isinstance(before, str):
                    return handle, before
            except Exception:
                pass
            if attempt < 2:
                time.sleep(.08)
        return None

    def _arm_filled_send(self, target, text, snapshot, epoch):
        if not self._input_allows_submit():
            return ''
        if snapshot is None:
            return ''
        handle, before = snapshot
        _open, read, _send = self._delivery_methods()
        text = text.replace('\r\n', '\n')
        try:
            for attempt in range(5):
                try:
                    after = read(target, handle)
                except Exception:
                    # 读回短暂失败也参与有限重试；不重新粘贴、不更换原控件快照。
                    after = None
                if isinstance(after, str) and len(after) == len(before) + len(text):
                    # 允许填在光标处；移除刚插入的完整文本后必须严格等于旧正文。
                    prefix = 0
                    while prefix < len(before) and before[prefix] == after[prefix]:
                        prefix += 1
                    suffix = 0
                    while suffix < len(before) and before[-suffix-1] == after[-suffix-1]:
                        suffix += 1
                    # 重复词可能使最长公共前缀越过插入点；在前后文均匹配的范围内找完整插入。
                    lower = len(before) - suffix
                    start = after.find(text, lower, prefix+len(text)) if lower <= prefix else -1
                    if start >= 0:
                        token = uuid.uuid4().hex
                        with self._lock:
                            if epoch != self._sync_epoch or self._selected != target:
                                return ''
                            self._pending_send = (token, epoch, target, handle, after, time.monotonic()+60)
                        return token
                if attempt < 4:
                    time.sleep(.08)
        except Exception:
            pass
        return ''

    def _input_allows_submit(self):
        method = getattr(self.backend, 'input_allows_submit', None)
        try:
            return bool(method()) if method else True
        except Exception:
            # 粘贴后界面可能立即重绘。发送能力未知不等于刚才没有填入，
            # 不能让调用方把它当作可安全重复粘贴的失败。
            return False

    def sync(self, text, callback, send=False):
        with self._lock:
            request = (self._sync_epoch, self._selected, str(text), send)
        self._queue.put(('sync', callback, request))

    def _sync(self, callback, request):
        from talkman_sync import SyncLease
        epoch, target, text, send = request
        def current():
            return not self._stop.is_set() and epoch == self._sync_epoch
        try:
            if not current():
                raise TargetError('同步已停止。')
            if target is None:
                raise TargetError('请先刷新并选择目标，再打开同步。')
            self._position_sync_backend(target)
            if send and not self._input_allows_submit():
                raise TargetError('此输入位置的提交方式尚未识别，请在目标内提交。')
            if not all(callable(getattr(self.backend, name, None)) for name in
                       ('open_sync', 'read_sync', 'write_sync', 'send_sync')):
                raise TargetError('当前目标不支持实时同步，可关闭同步后使用「填入」。')
            if self._sync_lease is None or self._sync_lease[0] != epoch:
                self._sync_lease = (epoch, SyncLease(self.backend, target, text))
            lease = self._sync_lease[1]
            if send:
                message = lease.send(text, current)
                result = FillReceipt(message, lease.expected, target.app, target.title,
                                     datetime.now().astimezone().isoformat(timespec='seconds'), '已触发下发')
            else:
                lease.update(text, current)
                result = text
            self._emit(callback, 'ok', result)
        except TargetError as error:
            self._emit(callback, 'error', str(error))
        except Exception:
            self._emit(callback, 'error', '此输入框暂不支持可靠同步；可关闭同步后使用「填入」。')

    def _resolve(self, windows: list[TargetWindow]) -> TargetWindow | None:
        with self._lock:
            current = self._selected
            metadata = self.selection.metadata
        # 本次运行中优先使用 hwnd+pid，避免同标题窗口误投。
        if current and current.hwnd is not None and current.pid is not None:
            for window in windows:
                if window.hwnd == current.hwnd and window.pid == current.pid:
                    return window
        identity = metadata.get("identity", "")
        title = metadata.get("title", "")
        app = metadata.get("app", "")
        # 优先精确匹配进程路径和标题；部分 Windows 进程没有权限读取路径，
        # 因此保存的目标可能只有 app/title，仍应允许唯一窗口自动重连。
        predicates = []
        if identity and title:
            predicates.append(lambda w: w.identity.casefold() == identity.casefold() and w.title == title)
        if identity:
            predicates.append(lambda w: w.identity.casefold() == identity.casefold())
        if title and app:
            predicates.append(lambda w: w.title == title and w.app.casefold() == app.casefold())
        if title:
            predicates.append(lambda w: w.title == title)
        for predicate in predicates:
            matches = [w for w in windows if predicate(w)]
            if len(matches) == 1:
                return matches[0]
        return None

    def _record_refresh(self, *, count: int = 0, error: str = "") -> None:
        note = str(getattr(self.backend, "last_scan_note", "") or "")
        report = str(getattr(self.backend, "last_scan_report", "") or "")
        if error:
            note = error
            report = (report + "\n" if report else "") + f"刷新结果：{error}"
        elif not report:
            report = f"刷新结果：找到 {count} 个目标窗口。"
        with self._lock:
            self._refresh_note = note
            self._refresh_report = report

    def _refresh(self, callback: Callback | None) -> None:
        try:
            windows = _as_windows(self.backend.list_windows())
            selected = self._resolve(windows)
            with self._lock:
                self._windows = windows
                self._selected = selected
                if selected is None:
                    self._focus_token = None
            self._record_refresh(count=len(windows))
            self._emit(callback, "ok", windows)
        except TargetError as exc:
            self._record_refresh(error=str(exc))
            self._emit(callback, "error", str(exc))
        except Exception:
            message = "目标窗口枚举失败，请稍后重试。"
            self._record_refresh(error=message)
            self._emit(callback, "error", message)

    def _capture_focus(self, target: TargetWindow) -> Any:
        method = getattr(self.backend, "capture_focus", None)
        if method is None:
            raise TargetError(UNSUPPORTED_MESSAGE)
        try:
            return method(target)
        except TypeError:
            # 兼容早期后端只接受 id/句柄的接口。
            return method(target.hwnd)

    def _monitor(self) -> None:
        now = time.monotonic()
        if now - self._last_focus_check < 0.3:
            return
        self._last_focus_check = now
        with self._lock:
            target = self._selected
        if target is None:
            return
        try:
            focus = self._capture_focus(target)
            with self._lock:
                if focus is False:
                    self._focus_token = None
                elif focus is not None:
                    self._focus_token = focus
        except Exception:
            # 监测失败不打断编辑；下一次 fill 会再严格验证。
            return

    def _fill(self, callback: Callback | None, text: str, receipt=False, requested_epoch=None) -> None:
        with self._lock:
            self._position_relocation_needed = False
            if requested_epoch is not None and requested_epoch != self._sync_epoch:
                self._emit(callback, 'error', '目标已切换，本次未填入。')
                return
            self._pending_send = None
            epoch = self._sync_epoch
            target = self._selected
            token = self._focus_token
            persisted = self.selection.selected
        # 应用重启后 UI 只加载了持久化标签，第一次点“填入 Agent”时不应
        # 强迫用户再打开设置页刷新。后台直接枚举并恢复唯一匹配窗口。
        if target is None and persisted:
            try:
                windows = _as_windows(self.backend.list_windows())
                target = self._resolve(windows)
                with self._lock:
                    self._windows = windows
                    self._selected = target
                    if target is None:
                        self._focus_token = None
            except TargetError as exc:
                self._emit(callback, "error", str(exc))
                return
            except Exception:
                target = None
        if target is None:
            self._emit(callback, "error", "目标输入框不可用，请点「填入」旁的目标下拉，刷新并重新选择。")
            return
        try:
            validate = getattr(self.backend, "validate_window", None)
            if validate is not None:
                current = validate(target)
                if current is None:
                    raise TargetError("目标窗口已失效，请重新刷新并选择。")
                target = TargetWindow.from_value(current)
                with self._lock:
                    if epoch != self._sync_epoch:
                        raise TargetError('目标已切换，本次未填入。')
                    # 验证后的标题可能比列表旧标题新；保存本次真实窗口快照。
                    self._selected = target
            self._position_sync_backend(target, recover=True, epoch=epoch)
            if self._position_token and self._position_roles.get(self._position_token):
                token = None
            focus = self._capture_focus(target)
            if focus is False:
                raise TargetError("目标输入框已失去焦点，请重新点击目标输入框后再试。")
            if focus is not None:
                token = focus
            # 点击 Talk Man 的填入按钮后，目标窗口必然暂时失去前台焦点。
            # token 为空时交给平台后端重新寻找并聚焦主输入框；只有窗口会话
            # 已失效（focus is False）才应直接返回不可用提示。
            snapshot = self._prepare_send_snapshot(target) if receipt else None
            with self._lock:
                if epoch != self._sync_epoch or self._selected != target or self._stop.is_set():
                    raise TargetError('目标已切换，本次未填入。')
            result = self.backend.fill(target, text, token)
            message = result if result is not None else "已填入目标输入框。"
            if receipt:
                send_token = self._arm_filled_send(target, text, snapshot, epoch)
                if not send_token:
                    message = str(message) + (' 无法核对目标正文，请在目标内手动发送。' if self._input_allows_submit()
                                             else ' 此位置仅填入，请在目标内提交回答。')
                message = FillReceipt(str(message), text, target.app, target.title,
                                      datetime.now().astimezone().isoformat(timespec="seconds"), send_token=send_token)
            self._emit(callback, "ok", message)
        except PositionRelocationRequired as exc:
            with self._lock:
                self._position_relocation_needed = True
            self._emit(callback, "error", str(exc))
        except TargetError as exc:
            self._emit(callback, "error", str(exc))
        except Exception:
            self._emit(callback, "error", "目标输入框不可用，请重新选择后再试。")

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    command, callback, value = self._queue.get(timeout=0.1)
                except queue.Empty:
                    self._monitor()
                    continue
                if command == "close":
                    break
                if command == "refresh":
                    self._refresh(callback)
                elif command == "fill":
                    self._fill(callback, value)
                elif command == "fill_receipt":
                    self._fill(callback, value[0], receipt=True, requested_epoch=value[1])
                elif command == 'sync':
                    self._sync(callback, value)
                elif command == 'send_filled':
                    self._send_filled(callback, value)
                elif command == 'positions':
                    self._positions(callback, value)
        finally:
            self._position_preview = None
            self._position_bindings.clear()
            if hasattr(self.backend, '_position_binding'):
                self.backend._position_binding = None
            close_backend = getattr(self.backend, "close", None)
            if close_backend is not None:
                try:
                    close_backend()
                except Exception:
                    pass


__all__ = [
    "TARGET_FILE", "UNSUPPORTED_MESSAGE", "TargetError", "TargetWindow",
    "TargetSelection", "TargetController", "FillReceipt", "PositionRelocationRequired",
]
