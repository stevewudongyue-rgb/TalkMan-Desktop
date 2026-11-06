"""目标窗口核心的离线测试，不访问真实桌面或输入框内容。"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from talkman_target_windows import (
    FocusToken,
    TargetWindow as WindowsTargetWindow,
    WindowsTargetBackend,
)
from talkman_target import (
    TARGET_FILE, UNSUPPORTED_MESSAGE, TargetController, TargetError,
    TargetSelection, TargetWindow, is_agent_window,
)


class FakeBackend:
    def __init__(self):
        self.windows = [TargetWindow(11, 42, "Agent", "Agent", "agent.exe")]
        self.focus = "token-1"
        self.calls = []

    def close(self):
        self.calls.append("close")

    def list_windows(self):
        self.calls.append("list")
        return list(self.windows)

    def capture_focus(self, target):
        self.calls.append(("focus", target.hwnd))
        return self.focus

    def fill(self, target, text, snapshot):
        self.calls.append(("fill", target.hwnd, text, snapshot))
        return "已填入"


def wait_result(results, timeout=2):
    end = time.time() + timeout
    while time.time() < end and not results:
        time.sleep(0.01)
    if not results:
        raise AssertionError("等待目标 worker 超时")
    return results.pop(0)


class TargetTests(unittest.TestCase):
    def test_agent_filter_excludes_admin_pages_and_keeps_native_clients(self):
        for title in ('OpenAI API keys - Chrome', '千问 APIKEY 管理',
                      'Claude Console', 'Gemini Developer Documentation', 'ChatGPT settings',
                      'DeepSeek 开放平台', '获取豆包 API Key 教程'):
            with self.subTest(title=title):
                self.assertFalse(is_agent_window(TargetWindow(1, 2, 'Chrome', title, 'chrome.exe')))
        for app, title, identity in (('Chrome', 'ChatGPT - 今天的需求', 'chrome.exe'),
                                      ('Safari', '豆包 - 新对话', 'Safari'),
                                      ('豆包', '设置', 'Doubao.app'),
                                      ('Codex', 'Talkman', 'Codex.exe'),
                                      ('ZCode', 'Talkman', 'dev.zcode.app'),
                                      ('ZCODE', 'Talkman', '/Applications/ZCode.app'),
                                      ('本地化应用名', 'Talkman', 'dev.zcode.app')):
            self.assertTrue(is_agent_window(TargetWindow(1, 2, app, title, identity)))

    def test_zcode_filter_keeps_word_boundaries_and_excludes_browser_admin_pages(self):
        for app, title, identity in (('NotZcode', '工程', 'notzcode.exe'),
                                      ('Chrome', 'ZCode API keys', 'chrome.exe'),
                                      ('Safari', 'ZCode Developer Documentation', 'Safari')):
            with self.subTest(app=app, title=title):
                self.assertFalse(is_agent_window(TargetWindow(1, 2, app, title, identity)))

    def test_windows_fill_retries_late_uia_input_without_sending(self):
        backend = WindowsTargetBackend()
        window = WindowsTargetWindow(100, 42, 'Agent', 'Agent')
        main = SimpleNamespace(SetFocus=Mock())
        backend.validate_window = Mock(return_value=window)
        backend._find_editable = Mock(side_effect=[None, None, None, main, main])
        backend._uia = SimpleNamespace(CompareElements=lambda a, b: a is b)
        backend._send_key, backend._paste_unicode = Mock(), Mock()
        user = SimpleNamespace(IsIconic=lambda hwnd: False, SetForegroundWindow=Mock(),
                               GetForegroundWindow=lambda: 100)
        with patch('talkman_target_windows._win_api', return_value=user), patch('talkman_target_windows.time.sleep'):
            backend.fill(window, '离线验证')
        user.SetForegroundWindow.assert_called_once_with(100)
        main.SetFocus.assert_called_once()
        backend._send_key.assert_called_once_with(0x23, ctrl=True)
        backend._paste_unicode.assert_called_once_with('离线验证', expected_window_id=100)

    def test_windows_fill_stops_retry_after_foreground_changes(self):
        backend = WindowsTargetBackend()
        window = WindowsTargetWindow(100, 42, 'Agent', 'Agent')
        backend.validate_window = Mock(return_value=window)
        backend._find_editable = Mock(return_value=None)
        backend._paste_unicode = Mock()
        user = SimpleNamespace(IsIconic=lambda hwnd: False, SetForegroundWindow=Mock(),
                               GetForegroundWindow=Mock(side_effect=[100, 200]))
        with patch('talkman_target_windows._win_api', return_value=user), patch('talkman_target_windows.time.sleep'):
            with self.assertRaisesRegex(Exception, '焦点'):
                backend.fill(window, '不能粘贴')
        backend._paste_unicode.assert_not_called()

    def test_refresh_diagnostics_are_cached_before_callback(self):
        backend = FakeBackend()
        backend.last_scan_report = "辅助功能：已授权\n豆包：窗口 1 个"
        backend.last_scan_note = "找到 1 个窗口，选中后设为目标。"
        with tempfile.TemporaryDirectory() as directory:
            controller = TargetController(Path(directory), backend=backend)
            self.addCleanup(controller.close)
            results = []
            controller.refresh(lambda state, value: results.append(
                (state, (controller.refresh_note, controller.refresh_report, value))))
            state, (note, report, windows) = wait_result(results)
            self.assertEqual(state, "ok")
            self.assertEqual(report, backend.last_scan_report)
            self.assertEqual(note, backend.last_scan_note)
            self.assertEqual(windows, backend.windows)
            controller.close()

    def test_refresh_failure_keeps_specific_diagnostics(self):
        backend = FakeBackend()
        with tempfile.TemporaryDirectory() as directory:
            controller = TargetController(Path(directory), backend=backend)
            self.addCleanup(controller.close)
            results = []
            controller.refresh(lambda state, value: results.append((state, value)))
            wait_result(results)
            backend.last_scan_report = "辅助功能：未授权\n运行程序：TalkMan.app"
            backend.list_windows = Mock(side_effect=TargetError("当前程序尚未获得辅助功能授权。"))
            controller.refresh(lambda state, value: results.append((state, value)))
            state, message = wait_result(results)
            self.assertEqual(state, "error")
            self.assertEqual(controller.refresh_note, message)
            self.assertIn("辅助功能：未授权", controller.refresh_report)
            self.assertNotIn("找到 1 个", controller.refresh_report)
            controller.close()

    def test_readonly_web_document_is_not_an_input(self):
        backend = WindowsTargetBackend()
        document = SimpleNamespace(CurrentControlType=backend._CONTROL_DOCUMENT)
        backend._pattern = Mock(side_effect=RuntimeError("没有可编辑模式"))
        self.assertFalse(backend._is_editable(document))

    def test_document_requires_explicit_writable_text_attribute(self):
        backend = WindowsTargetBackend()
        document = SimpleNamespace(CurrentControlType=backend._CONTROL_DOCUMENT)
        text = SimpleNamespace(DocumentRange=SimpleNamespace(GetAttributeValue=Mock(return_value=False)))
        backend._pattern = Mock(side_effect=[RuntimeError("没有 ValuePattern"), text])
        self.assertTrue(backend._is_editable(document))

    def test_document_does_not_default_missing_value_readonly_to_writable(self):
        document = SimpleNamespace(CurrentControlType=WindowsTargetBackend._CONTROL_DOCUMENT)
        for value in (None, SimpleNamespace(), SimpleNamespace(CurrentIsReadOnly=object())):
            with self.subTest(value=value):
                backend = WindowsTargetBackend()
                readonly_text = SimpleNamespace(DocumentRange=SimpleNamespace(GetAttributeValue=lambda _: True))
                backend._pattern = Mock(side_effect=[value, readonly_text])
                self.assertFalse(backend._is_editable(document))

    def test_document_unsupported_text_readonly_attribute_is_not_writable(self):
        document = SimpleNamespace(CurrentControlType=WindowsTargetBackend._CONTROL_DOCUMENT)
        for readonly in (None, object(), "not-supported"):
            with self.subTest(readonly=readonly):
                backend = WindowsTargetBackend()
                text = SimpleNamespace(DocumentRange=SimpleNamespace(GetAttributeValue=lambda _: readonly))
                backend._pattern = Mock(side_effect=[RuntimeError("没有 ValuePattern"), text])
                self.assertFalse(backend._is_editable(document))

    def test_uia_initializes_with_registered_clsid_and_interface(self):
        api = SimpleNamespace(IUIAutomation=object())
        create = Mock(return_value=object())
        comtypes = SimpleNamespace(CoInitialize=Mock(), CoUninitialize=Mock())
        client = SimpleNamespace(CreateObject=create, GetModule=Mock())
        gen = SimpleNamespace(UIAutomationClient=api)
        backend = WindowsTargetBackend()
        with patch("talkman_target_windows.os.name", "nt"), patch.dict("sys.modules", {
            "comtypes": comtypes, "comtypes.client": client, "comtypes.gen": gen,
        }):
            backend._ensure_uia()
            create.assert_called_once_with(
                "{FF48DBA4-60EF-4201-AA87-54103EEF594E}", interface=api.IUIAutomation
            )
            backend.close()

    def test_uia_element_array_uses_get_element(self):
        elements = [object(), object()]
        collection = SimpleNamespace(Length=2, GetElement=lambda i: elements[i])
        self.assertEqual(WindowsTargetBackend._collection_items(collection), elements)

    def test_pattern_queries_typed_com_interface(self):
        backend = WindowsTargetBackend()
        interface = object()
        backend._uia = object()
        backend._uia_api = SimpleNamespace(IUIAutomationValuePattern=interface)
        value = object()
        raw = SimpleNamespace(QueryInterface=Mock(return_value=value))
        element = SimpleNamespace(GetCurrentPattern=Mock(return_value=raw))
        self.assertIs(backend._pattern(element, backend._VALUE_PATTERN, "IUIAutomationValuePattern"), value)
        raw.QueryInterface.assert_called_once_with(interface)

    def test_pattern_refuses_null_and_unavailable_interface(self):
        backend = WindowsTargetBackend()
        element = SimpleNamespace(GetCurrentPattern=lambda _: None)
        with self.assertRaisesRegex(RuntimeError, "不支持"):
            backend._pattern(element, backend._VALUE_PATTERN, "IUIAutomationValuePattern")

        backend._uia = object()
        backend._uia_api = SimpleNamespace(IUIAutomationValuePattern=object())
        element = SimpleNamespace(GetCurrentPattern=lambda _: SimpleNamespace(QueryInterface=lambda _: None))
        with self.assertRaisesRegex(RuntimeError, "不支持"):
            backend._pattern(element, backend._VALUE_PATTERN, "IUIAutomationValuePattern")

    def test_hwndless_focused_control_must_belong_to_target(self):
        backend = WindowsTargetBackend()
        other = SimpleNamespace(CurrentNativeWindowHandle=0)
        backend._uia = SimpleNamespace(GetFocusedElement=lambda: other)
        backend._window_element = Mock(return_value=object())
        backend._is_editable = Mock(return_value=True)
        backend._is_descendant_of = Mock(return_value=False)
        self.assertIsNone(backend._find_editable(WindowsTargetWindow(100, 42, "Agent", "Agent"), focused_only=True))
        backend._is_descendant_of.assert_called_once()

    def test_fill_reactivates_target_before_pasting_without_sending(self):
        backend = WindowsTargetBackend()
        window = WindowsTargetWindow(100, 42, "Agent", "Agent")
        element = SimpleNamespace(SetFocus=Mock())
        backend.validate_window = Mock(return_value=window)
        backend._find_editable = Mock(side_effect=[None, element, element])
        backend._uia = SimpleNamespace(CompareElements=lambda a, b: a is b)
        backend._send_key = Mock()
        backend._paste_unicode = Mock()
        user32 = SimpleNamespace(IsIconic=lambda hwnd: False, SetForegroundWindow=Mock(), GetForegroundWindow=lambda: 100)
        with patch("talkman_target_windows._win_api", return_value=user32), patch("talkman_target_windows.time.sleep"):
            result = backend.fill(window, "文字")
        user32.SetForegroundWindow.assert_called_once_with(100)
        element.SetFocus.assert_called_once()
        backend._paste_unicode.assert_called_once_with("文字", expected_window_id=100)
        backend._send_key.assert_called_once_with(0x23, ctrl=True)
        self.assertIn("未发送", result)

    def test_fill_refuses_when_input_focus_was_not_acquired(self):
        backend = WindowsTargetBackend()
        window = WindowsTargetWindow(100, 42, "Agent", "Agent")
        element = SimpleNamespace(SetFocus=Mock())
        backend.validate_window = Mock(return_value=window)
        backend._find_editable = Mock(side_effect=[None, element, None])
        backend._paste_unicode = Mock()
        user32 = SimpleNamespace(IsIconic=lambda hwnd: False, SetForegroundWindow=Mock(), GetForegroundWindow=lambda: 100)
        with patch("talkman_target_windows._win_api", return_value=user32), patch("talkman_target_windows.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "输入框未获得焦点"):
                backend.fill(window, "文字")
        backend._paste_unicode.assert_not_called()

    def test_fill_restores_remembered_input_cursor_after_button_takes_focus(self):
        backend = WindowsTargetBackend()
        window = WindowsTargetWindow(100, 42, "Agent", "Agent")
        element = SimpleNamespace(
            CurrentNativeWindowHandle=12, CurrentAutomationId="main-input",
            CurrentClassName="Editor", SetFocus=Mock(),
        )
        backend.validate_window = Mock(return_value=window)
        backend._find_editable = Mock(side_effect=[None, element, element])
        backend._uia = SimpleNamespace(CompareElements=lambda a, b: a is b)
        backend._collapse_selection_to_end = Mock()
        backend._send_key = Mock()
        backend._paste_unicode = Mock()
        user32 = SimpleNamespace(IsIconic=lambda hwnd: False, SetForegroundWindow=Mock(), GetForegroundWindow=lambda: 100)
        token = FocusToken(100, 12, "main-input", "Editor")
        with patch("talkman_target_windows._win_api", return_value=user32), patch("talkman_target_windows.time.sleep"):
            backend.fill(window, "文字", token)
        user32.SetForegroundWindow.assert_called_once_with(100)
        element.SetFocus.assert_not_called()
        backend._collapse_selection_to_end.assert_called_once_with(element)
        backend._send_key.assert_not_called()
        backend._paste_unicode.assert_called_once_with("文字", expected_window_id=100)

    def test_fill_uses_main_input_when_restored_control_does_not_match_token(self):
        backend = WindowsTargetBackend()
        window = WindowsTargetWindow(100, 42, "Agent", "Agent")
        other = SimpleNamespace(CurrentNativeWindowHandle=13, CurrentAutomationId="other-input", CurrentClassName="Editor")
        main = SimpleNamespace(SetFocus=Mock())
        backend.validate_window = Mock(return_value=window)
        backend._find_editable = Mock(side_effect=[None, other, main, main])
        backend._uia = SimpleNamespace(CompareElements=lambda a, b: a is b)
        backend._collapse_selection_to_end = Mock()
        backend._send_key = Mock()
        backend._paste_unicode = Mock()
        user32 = SimpleNamespace(IsIconic=lambda hwnd: False, SetForegroundWindow=Mock(), GetForegroundWindow=lambda: 100)
        token = FocusToken(100, 12, "main-input", "Editor")
        with patch("talkman_target_windows._win_api", return_value=user32), patch("talkman_target_windows.time.sleep"):
            backend.fill(window, "文字", token)
        main.SetFocus.assert_called_once()
        backend._collapse_selection_to_end.assert_not_called()
        backend._send_key.assert_called_once_with(0x23, ctrl=True)
        backend._paste_unicode.assert_called_once_with("文字", expected_window_id=100)

    def test_uia_parent_walk_rejects_cross_window_element(self):
        class Element:
            def __init__(self, hwnd, parent=None):
                self.CurrentNativeWindowHandle = hwnd
                self.parent = parent

        class Walker:
            def GetParentElement(self, element):
                return element.parent

        root = Element(100)
        child = Element(0, root)
        other = Element(0, Element(200))
        backend = WindowsTargetBackend()
        backend._uia = type("UIA", (), {"ControlViewWalker": Walker()})()
        self.assertTrue(backend._is_descendant_of(child, root))
        self.assertFalse(backend._is_descendant_of(other, root))

    def test_uia_parent_walk_accepts_fresh_wrappers_in_deep_ancestry(self):
        class Element:
            __slots__ = ("depth", "CurrentNativeWindowHandle")

            def __init__(self, depth):
                self.depth = depth
                self.CurrentNativeWindowHandle = 100 if depth == 0 else 0

        class Walker:
            def GetParentElement(self, element):
                return Element(element.depth - 1) if element.depth else None

        backend = WindowsTargetBackend()
        backend._uia = SimpleNamespace(ControlViewWalker=Walker())
        self.assertTrue(backend._is_descendant_of(Element(12), Element(0)))

    def test_uia_parent_walk_stops_after_bounded_cycle(self):
        element = SimpleNamespace(CurrentNativeWindowHandle=0)
        walker = SimpleNamespace(GetParentElement=Mock(return_value=element))
        backend = WindowsTargetBackend()
        backend._uia = SimpleNamespace(ControlViewWalker=walker)
        self.assertFalse(backend._is_descendant_of(element, SimpleNamespace(CurrentNativeWindowHandle=100)))
        self.assertEqual(walker.GetParentElement.call_count, 64)

    def test_focus_token_mismatch_is_not_preserved(self):
        class Element:
            CurrentNativeWindowHandle = 12
            CurrentAutomationId = "main-input"
            CurrentClassName = "Editor"

        backend = WindowsTargetBackend()
        current = Element()
        self.assertTrue(backend._matches_focus_token(
            current, FocusToken(100, 12, "main-input", "Editor"), 100
        ))
        self.assertFalse(backend._matches_focus_token(
            current, FocusToken(100, 12, "search-input", "Editor"), 100
        ))

    def test_metadata_never_persists_handle_or_process_start(self):
        with tempfile.TemporaryDirectory() as directory:
            selection = TargetSelection(Path(directory))
            selection.set(TargetWindow(99, 100, "Agent", "窗口", "C:/agent.exe", 123.4))
            selection.save()
            raw = (Path(directory) / TARGET_FILE).read_text(encoding="utf-8")
            self.assertNotIn("hwnd", raw)
            self.assertNotIn("pid", raw)
            self.assertNotIn("process_started", raw)
            self.assertEqual(TargetSelection.load(Path(directory)).metadata, {
                "app": "Agent", "title": "窗口", "identity": "C:/agent.exe",
            })

    def test_accepts_windows_backend_window_object(self):
        window = WindowsTargetWindow(11, 42, "Agent", "Agent", "C:/agent.exe", "started")
        target = TargetWindow.from_value(window)
        self.assertEqual(target.hwnd, 11)
        self.assertEqual(target.pid, 42)
        self.assertEqual(target.identity, "c:/agent.exe")
        self.assertEqual(target.process_path, "c:/agent.exe")

    def test_corrupt_selection_has_chinese_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / TARGET_FILE
            path.write_text("[]", encoding="utf-8")
            with self.assertRaisesRegex(TargetError, "目标设置"):
                TargetSelection.load(Path(directory))

    def test_reconnect_requires_unique_identity_and_title(self):
        backend = FakeBackend()
        with tempfile.TemporaryDirectory() as directory:
            selection = TargetSelection(Path(directory))
            selection.set(backend.windows[0])
            selection.save()
            controller = TargetController(Path(directory), backend=backend)
            results = []
            controller.refresh(lambda state, value: results.append((state, value)))
            state, windows = wait_result(results)
            self.assertEqual(state, "ok")
            self.assertEqual(controller.selected.hwnd, 11)
            controller.close()

    def test_duplicate_reconnect_is_refused(self):
        backend = FakeBackend()
        backend.windows.append(TargetWindow(12, 43, "Agent", "Agent", "agent.exe"))
        with tempfile.TemporaryDirectory() as directory:
            TargetSelection(Path(directory)).set(backend.windows[0])
            selection = TargetSelection(Path(directory))
            selection.set(backend.windows[0]); selection.save()
            controller = TargetController(Path(directory), backend=backend)
            results = []
            controller.refresh(lambda state, value: results.append((state, value)))
            wait_result(results)
            self.assertIsNone(controller.selected)
            controller.close()

    def test_fill_revalidates_focus_and_does_not_send(self):
        backend = FakeBackend()
        with tempfile.TemporaryDirectory() as directory:
            controller = TargetController(Path(directory), backend=backend)
            controller.select(backend.windows[0])
            results = []
            controller.fill("新文字", lambda state, value: results.append((state, value)))
            self.assertEqual(wait_result(results)[0], "ok")
            self.assertEqual(backend.calls[-1], ("fill", 11, "新文字", "token-1"))
            self.assertNotIn("send", backend.calls)
            controller.close()

    def test_fill_preserves_backend_target_error_details(self):
        backend = FakeBackend()
        backend.fill = Mock(side_effect=TargetError("目标输入框未获得焦点，已取消填入。"))
        with tempfile.TemporaryDirectory() as directory:
            controller = TargetController(Path(directory), backend=backend)
            controller.select(backend.windows[0])
            results = []
            controller.fill("文字", lambda state, value: results.append((state, value)))
            self.assertEqual(wait_result(results), ("error", "目标输入框未获得焦点，已取消填入。"))
            controller.close()

    def test_fill_reacquires_input_when_target_is_not_focused(self):
        backend = FakeBackend(); backend.focus = None
        with tempfile.TemporaryDirectory() as directory:
            controller = TargetController(Path(directory), backend=backend)
            controller.select(backend.windows[0])
            results = []
            controller.fill("自动聚焦", lambda state, value: results.append((state, value)))
            self.assertEqual(wait_result(results)[0], "ok")
            self.assertEqual(backend.calls[-1], ("fill", 11, "自动聚焦", None))
            controller.close()

    def test_fill_reconnects_saved_target_without_refresh(self):
        backend = FakeBackend()
        with tempfile.TemporaryDirectory() as directory:
            # 模拟上一次运行保存的目标：本次 controller 尚未执行 refresh，
            # 第一次点击“填入 Agent”也应自动恢复唯一窗口。
            selection = TargetSelection(Path(directory))
            selection.set(backend.windows[0])
            selection.save()
            controller = TargetController(Path(directory), backend=backend)
            results = []
            controller.fill("重启后自动恢复", lambda state, value: results.append((state, value)))
            self.assertEqual(wait_result(results)[0], "ok")
            self.assertEqual(backend.calls[-1], ("fill", 11, "重启后自动恢复", "token-1"))
            controller.close()

    def test_fill_rejects_lost_focus_in_chinese(self):
        backend = FakeBackend(); backend.focus = False
        with tempfile.TemporaryDirectory() as directory:
            controller = TargetController(Path(directory), backend=backend)
            controller.select(backend.windows[0])
            results = []
            controller.fill("文字", lambda state, value: results.append((state, value)))
            state, message = wait_result(results)
            self.assertEqual(state, "error")
            self.assertIn("失去焦点", message)
            controller.close()

    def test_unsupported_backend_gives_clear_message(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = TargetController(Path(directory), platform="linux")
            results = []
            controller.refresh(lambda state, value: results.append((state, value)))
            state, message = wait_result(results)
            self.assertEqual(state, "error")
            self.assertEqual(message, UNSUPPORTED_MESSAGE)
            controller.close()

    def test_mac_platform_routes_to_mac_backend(self):
        backend = FakeBackend()
        with patch("talkman_target_macos.MacTargetBackend", return_value=backend):
            self.assertIs(TargetController._default_backend("darwin"), backend)

    def test_saved_target_permission_error_is_preserved_on_first_fill(self):
        backend = FakeBackend()
        backend.list_windows = Mock(side_effect=TargetError("请在 Mac 辅助功能中允许 TalkMan。"))
        with tempfile.TemporaryDirectory() as directory:
            selection = TargetSelection(Path(directory))
            selection.set(backend.windows[0])
            selection.save()
            controller = TargetController(Path(directory), backend=backend)
            try:
                results = []
                controller.fill("文字", lambda state, value: results.append((state, value)))
                self.assertEqual(wait_result(results), ("error", "请在 Mac 辅助功能中允许 TalkMan。"))
                self.assertFalse(any(isinstance(call, tuple) and call[0] == "fill" for call in backend.calls))
            finally:
                controller.close()

    def test_clear_removes_persisted_selection(self):
        backend = FakeBackend()
        with tempfile.TemporaryDirectory() as directory:
            controller = TargetController(Path(directory), backend=backend)
            controller.select(backend.windows[0]); controller.clear()
            self.assertFalse(TargetSelection.load(Path(directory)).selected)
            controller.close()
            self.assertIn("close", backend.calls)


if __name__ == "__main__":
    unittest.main()
