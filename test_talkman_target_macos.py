"""macOS 目标窗口的离线身份、权限及投递防误填测试。"""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

import talkman_target_macos as target


class Element:
    def __init__(self, role, *, parent=None, **attributes):
        self.attributes = {"AXRole": role, "AXChildren": [], **attributes}
        self.writable = set()
        self.span = (0, 0)
        if parent is not None:
            self.attributes["AXParent"] = parent
            parent.attributes["AXChildren"].append(self)


class FakeAPI:
    def __init__(self):
        self.allowed = True
        self.apps = {}
        self.roots = {}
        self.front = None
        self.actions = []
        self.after_clipboard = None

    def trusted(self):
        return self.allowed

    def add_app(self, pid=9876, name="Codex", bundle="com.openai.codex", started=100.0):
        app = {
            "pid": pid, "name": name, "bundle": bundle, "identity": bundle,
            "started": started, "regular": True, "terminated": False,
        }
        self.apps[pid] = app
        root = Element("AXApplication", AXWindows=[])
        self.roots[pid] = root
        return app, root

    def add_window(self, root, title="对话"):
        window = Element("AXWindow", parent=root, AXTitle=title)
        root.attributes["AXWindows"].append(window)
        return window

    def add_input(self, window, *, label="随心输入", count=20, writable=True, role="AXTextArea"):
        element = Element(role, parent=window, AXDescription=label, AXNumberOfCharacters=count)
        element.writable.update(("AXFocused", "AXSelectedTextRange"))
        if writable:
            element.writable.add("AXValue")
        return element

    def focus(self, pid, window, element):
        self.front = pid
        self.roots[pid].attributes.update(AXFocusedWindow=window, AXFocusedUIElement=element)

    def applications(self):
        return list(self.apps.values())

    def application(self, pid):
        return self.apps.get(pid)

    def app_info(self, app):
        return app

    def root(self, pid):
        return self.roots[pid]

    def get(self, element, attribute, default=None):
        return element.attributes.get(attribute, default)

    def settable(self, element, attribute):
        return attribute in element.writable

    def equal(self, first, second):
        return first is not None and first is second

    def frontmost_pid(self):
        return self.front

    def activate(self, app, window):
        self.actions.append(("activate", app["pid"], window))
        self.front = app["pid"]
        self.roots[app["pid"]].attributes["AXFocusedWindow"] = window

    def set(self, element, attribute, value):
        self.actions.append(("set", element, attribute, value))
        element.attributes[attribute] = value
        if attribute == "AXFocused":
            for root in self.roots.values():
                window = element.attributes["AXParent"]
                if window in root.attributes["AXWindows"]:
                    root.attributes["AXFocusedUIElement"] = element

    def selection(self, element):
        return element.span

    def set_selection(self, element, location):
        self.actions.append(("selection", location))
        element.span = (location, 0)

    def move_caret(self, *, to_end):
        self.actions.append(("move_caret", to_end))
        element = self.roots[self.front].attributes["AXFocusedUIElement"]
        location = element.attributes["AXNumberOfCharacters"] if to_end else sum(element.span)
        element.span = (location, 0)

    def clipboard(self, text):
        self.actions.append(("clipboard", text))
        if self.after_clipboard:
            self.after_clipboard()

    def paste(self):
        self.actions.append(("paste",))


class MacTargetTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI()
        self.app, self.root = self.api.add_app()
        self.window = self.api.add_window(self.root)
        self.editor = self.api.add_input(self.window)
        self.backend = target.MacTargetBackend(api=self.api)
        self.selected = self.backend.list_windows()[0]

    def test_permissions_rejected_before_enumeration(self):
        self.api.allowed = False
        self.api.applications = Mock()
        with self.assertRaisesRegex(target.TargetError, "隐私与安全性.*辅助功能"):
            self.backend.list_windows()
        self.api.applications.assert_not_called()

    def test_native_launch_date_fallback_reads_exact_process_start(self):
        native = target._NativeAPI.__new__(target._NativeAPI)
        app = Mock()
        app.launchDate.return_value = None
        app.bundleIdentifier.return_value = 'com.bot.pc.doubao'
        app.bundleURL.return_value = None
        app.activationPolicy.return_value = 0
        app.processIdentifier.return_value = 5314
        app.localizedName.return_value = '豆包'
        app.isTerminated.return_value = False
        with patch.object(target.subprocess, 'run', return_value=SimpleNamespace(
                returncode=0, stdout='Mon Nov  2 08:31:12 2026\n')) as run:
            info = native.app_info(app)
        self.assertEqual(info['started'], target.datetime(2026, 11, 2, 8, 31, 12).timestamp())
        self.assertEqual(run.call_args.args[0], ['/bin/ps', '-p', '5314', '-o', 'lstart='])

    def test_missing_process_start_still_blocks_target(self):
        native = target._NativeAPI.__new__(target._NativeAPI)
        with patch.object(target.subprocess, 'run', return_value=SimpleNamespace(returncode=1, stdout='')):
            self.assertIsNone(native._process_started(5314))
        with patch.object(target.subprocess, 'run', side_effect=target.subprocess.TimeoutExpired('ps', 1)):
            self.assertIsNone(native._process_started(5314))
        self.app.update(name='豆包', bundle='com.bot.pc.doubao', identity='com.bot.pc.doubao', started=None)
        self.assertEqual(self.backend.list_windows(), [])

    def test_background_apps_do_not_spawn_process_lookup(self):
        native = target._NativeAPI.__new__(target._NativeAPI)
        app = Mock()
        app.launchDate.return_value = None
        app.bundleIdentifier.return_value = 'com.bot.pc.doubao.browser.helper'
        app.bundleURL.return_value = None
        app.activationPolicy.return_value = 1
        app.processIdentifier.return_value = 5386
        app.localizedName.return_value = 'Doubao Browser Helper'
        app.isTerminated.return_value = False
        with patch.object(native, '_process_started') as lookup:
            self.assertIsNone(native.app_info(app)['started'])
            lookup.assert_not_called()

    def test_permission_revoked_before_fill_does_not_touch_clipboard(self):
        self.api.allowed = False
        with self.assertRaisesRegex(target.TargetError, "辅助功能"):
            self.backend.fill(self.selected, "文字")
        self.assertEqual(self.api.actions, [])

    def test_allowed_apps_only_excluding_self_and_background(self):
        for pid, name, bundle in (
            (111, "Safari", "com.apple.safari"),
            (112, "Terminal", "com.apple.Terminal"),
            (113, "TalkMan", "com.talkman.app"),
            (114, "Unknown", "example.unknown"),
            (115, "Cursor", "com.todesktop.230313mzl4w4u92"),
        ):
            app, root = self.api.add_app(pid, name, bundle)
            self.api.add_window(root, title=name)
        background, root = self.api.add_app(116, "Claude", "com.anthropic.claude")
        background["regular"] = False
        self.api.add_window(root)
        with patch.object(target.os, "getpid", return_value=111):
            windows = self.backend.list_windows()
        self.assertEqual({window.app for window in windows}, {"Codex", "Cursor"})

    def test_zcode_observed_bundle_and_case_insensitive_name_are_enumerated(self):
        for name, bundle in (("ZCode", "dev.zcode.app"),
                             ("本地化应用名", "dev.zcode.app"),
                             (" ZCODE ", "fixture.zcode")):
            with self.subTest(name=name, bundle=bundle):
                self.app.update(name=name, bundle=bundle, identity=bundle)
                windows = self.backend.list_windows()
                self.assertEqual(len(windows), 1)
                self.assertEqual(windows[0].app, name)
                self.assertIsNotNone(self.backend.validate_window(windows[0]))

    def test_zcode_helpers_and_background_processes_are_still_excluded(self):
        for name, bundle, regular in (("ZCode Helper", "dev.zcode.app.helper", True),
                                      ("ZCode Helper (Renderer)", "dev.zcode.app.helper.renderer", True),
                                      ("ZCode", "dev.zcode.app", False)):
            with self.subTest(name=name, bundle=bundle):
                self.app.update(name=name, bundle=bundle, identity=bundle, regular=regular)
                self.assertEqual(self.backend.list_windows(), [])

    def test_zcode_fills_labeled_chat_input_without_sending(self):
        self.app.update(name="ZCode", bundle="dev.zcode.app", identity="dev.zcode.app")
        selected = self.backend.list_windows()[0]
        self.root.attributes["AXFocusedUIElement"] = self.window
        self.backend.fill(selected, "离线测试")
        self.assertEqual(self.api.actions[-2:], [("clipboard", "离线测试"), ("paste",)])

    def test_zcode_does_not_treat_unlabelled_code_editor_as_chat_input(self):
        self.app.update(name="ZCode", bundle="dev.zcode.app", identity="dev.zcode.app")
        selected = self.backend.list_windows()[0]
        self.editor.attributes["AXDescription"] = ""
        self.root.attributes["AXFocusedUIElement"] = self.window
        with self.assertRaisesRegex(target.TargetError, "唯一"):
            self.backend.fill(selected, "不能误填")
        self.assertFalse(any(action[0] in ("set", "clipboard", "paste") for action in self.api.actions))

    def test_doubao_localized_names_are_enumerated_without_assuming_bundle_id(self):
        for name in ("豆包", "Doubao", "DOUBAO", "  豆包  ", " Doubao "):
            with self.subTest(name=name):
                # A fixture ID proves name matching without inventing a real bundle ID.
                self.app.update(name=name, bundle="fixture.doubao", identity="fixture.doubao")
                windows = self.backend.list_windows()
                self.assertEqual(len(windows), 1)
                self.assertEqual(windows[0].app, name)
                self.assertIsNotNone(self.backend.validate_window(windows[0]))

    def test_diagnostic_doubao_and_perplexity_bundle_ids_are_listed(self):
        for name, bundle in (("豆包", "com.bot.pc.doubao"),
                             ("Perplexity", "ai.perplexity.macv3")):
            with self.subTest(name=name):
                self.app.update(name=name, bundle=bundle, identity=bundle)
                windows = self.backend.list_windows()
                self.assertEqual(len(windows), 1)
                self.assertEqual(windows[0].app, name)
                self.assertIsNotNone(self.backend.validate_window(windows[0]))

    def test_perplexity_helper_and_doubao_browser_remain_excluded(self):
        for name, bundle in (("perplexityd", "ai.perplexity.macv3.perplexityd"),
                             ("Perplexity Graphics and Media", "com.apple.WebKit.GPU"),
                             ("豆包浏览器", "com.bot.pc.doubao.browser")):
            with self.subTest(name=name):
                self.app.update(name=name, bundle=bundle, identity=bundle)
                self.assertEqual(self.backend.list_windows(), [])

    def test_doubao_name_does_not_allow_helpers_or_similarly_named_apps(self):
        for name in ("Doubao Helper", "Doubao Helper (Renderer)", "豆包助手", "DoubaoOther"):
            with self.subTest(name=name):
                self.app.update(name=name, bundle="fixture.doubao", identity="fixture.doubao")
                self.assertEqual(self.backend.list_windows(), [])

    def test_doubao_invalid_sessions_and_background_processes_still_excluded(self):
        self.app.update(name="豆包", bundle="fixture.doubao", identity="fixture.doubao")
        for field, invalid in (("regular", False), ("terminated", True), ("started", None), ("identity", "")):
            with self.subTest(field=field):
                previous = self.app[field]
                self.app[field] = invalid
                self.assertEqual(self.backend.list_windows(), [])
                self.app[field] = previous
        with patch.object(target.os, "getpid", return_value=self.app["pid"]):
            self.assertEqual(self.backend.list_windows(), [])

    def test_doubao_explicit_exclusion_normalizes_name(self):
        self.app.update(name=" 豆包 ", bundle="fixture.doubao", identity="fixture.doubao")
        backend = target.MacTargetBackend(api=self.api, excluded_apps=[" 豆包 "])
        self.assertEqual(backend.list_windows(), [])

    def test_doubao_fills_only_chat_input_and_rejects_restarted_session(self):
        self.app.update(name="豆包", bundle="fixture.doubao", identity="fixture.doubao")
        selected = self.backend.list_windows()[0]
        self.editor.attributes["AXDescription"] = "输入消息"
        search = self.api.add_input(self.window, label="搜索对话")
        ordinary = self.api.add_input(self.window, label="editor")
        self.root.attributes["AXFocusedUIElement"] = self.window
        self.backend.fill(selected, "测试文字")
        self.assertIn(("set", self.editor, "AXFocused", True), self.api.actions)
        self.assertNotIn(("set", search, "AXFocused", True), self.api.actions)
        self.assertNotIn(("set", ordinary, "AXFocused", True), self.api.actions)
        self.assertEqual(self.api.actions[-2:], [("clipboard", "测试文字"), ("paste",)])
        self.api.actions.clear()
        self.app["started"] += 1
        with self.assertRaisesRegex(target.TargetError, "会话已变化"):
            self.backend.fill(selected, "后续文字")
        self.assertEqual(self.api.actions, [])

    def test_window_refresh_retains_session_id_even_after_title_change(self):
        self.window.attributes["AXTitle"] = "新标题"
        selected = self.backend.list_windows()[0]
        self.assertEqual(selected.hwnd, self.selected.hwnd)
        self.assertEqual(selected.title, "新标题")
        self.assertIsNotNone(self.backend.validate_window(self.selected))

    def test_untitled_window_uses_app_name_and_keeps_distinct_session_handle(self):
        self.window.attributes["AXTitle"] = ""
        second = self.api.add_window(self.root, title="")
        windows = self.backend.list_windows()
        self.assertEqual([window.title for window in windows], ["Codex", "Codex"])
        self.assertNotEqual(windows[0].hwnd, windows[1].hwnd)
        self.assertNotEqual(self.window, second)

    def test_closed_window_invalidates_old_handle(self):
        self.root.attributes["AXWindows"].clear()
        self.root.attributes["AXChildren"].clear()
        with self.assertRaisesRegex(target.TargetError, "已关闭"):
            self.backend.fill(self.selected, "文字")
        self.assertEqual(self.api.actions, [])

    def test_process_restart_and_identity_change_are_rejected(self):
        for field, value in (("started", 200.0), ("identity", "example.other")):
            with self.subTest(field=field):
                old = self.app[field]
                self.app[field] = value
                self.assertIs(self.backend.capture_focus(self.selected), False)
                with self.assertRaisesRegex(target.TargetError, "会话已变化"):
                    self.backend.fill(self.selected, "文字")
                self.app[field] = old
        self.assertEqual(self.api.actions, [])

    def test_reused_pid_gets_new_handle_when_refreshed(self):
        self.app["started"] = 200.0
        refreshed = self.backend.list_windows()[0]
        self.assertNotEqual(refreshed.hwnd, self.selected.hwnd)
        self.assertIsNone(self.backend.validate_window(self.selected))

    def test_capture_requires_focus_in_selected_window(self):
        other = self.api.add_window(self.root, "另一个对话")
        foreign_input = self.api.add_input(other)
        self.api.focus(self.selected.pid, other, foreign_input)
        self.assertIsNone(self.backend.capture_focus(self.selected))
        # Even an incorrect focused-window attribute cannot authorize a foreign element.
        self.root.attributes["AXFocusedWindow"] = self.window
        self.assertIsNone(self.backend.capture_focus(self.selected))

    def test_readonly_document_search_password_and_terminal_are_excluded(self):
        examples = [
            self.api.add_input(self.window, label="页面正文", writable=False),
            self.api.add_input(self.window, label="搜索消息"),
            self.api.add_input(self.window, label="address bar"),
            self.api.add_input(self.window, label="terminal input"),
            self.api.add_input(self.window, label="password"),
            self.api.add_input(self.window, label="chat", role="AXWebArea"),
        ]
        examples[0].writable.add("AXSelectedTextRange")
        for element in examples:
            with self.subTest(element=element):
                self.assertFalse(self.backend._is_editable(element))

    def test_tied_main_inputs_reject_without_paste(self):
        self.api.add_input(self.window)
        self.root.attributes["AXFocusedUIElement"] = self.window
        with self.assertRaisesRegex(target.TargetError, "唯一"):
            self.backend.fill(self.selected, "文字")
        self.assertNotIn(("paste",), self.api.actions)

    def test_only_obvious_non_chat_editor_is_never_automatically_selected(self):
        for label in ("code editor", "Untitled source", "文件名", "路径", "普通字段"):
            with self.subTest(label=label):
                self.editor.attributes["AXDescription"] = label
                self.root.attributes["AXFocusedUIElement"] = self.window
                self.api.actions.clear()
                with self.assertRaisesRegex(target.TargetError, "唯一"):
                    self.backend.fill(self.selected, "测试")
                self.assertNotIn(("paste",), self.api.actions)

    def test_single_unlabelled_doubao_multiline_input_can_focus_and_append(self):
        self.app.update(name="豆包", bundle="fixture.doubao", identity="fixture.doubao")
        selected = self.backend.list_windows()[0]
        self.editor.attributes["AXDescription"] = ""
        self.editor.span = (4, 3)
        self.root.attributes["AXFocusedUIElement"] = self.window
        self.backend.fill(selected, "测试")
        self.assertIn(("set", self.editor, "AXFocused", True), self.api.actions)
        self.assertEqual(self.editor.span, (20, 0))
        self.assertEqual(self.api.actions[-2:], [("clipboard", "测试"), ("paste",)])

    def test_generic_multiline_label_is_safe_only_for_standalone_chat_agent(self):
        self.editor.attributes["AXDescription"] = "input"
        self.root.attributes["AXFocusedUIElement"] = self.window
        self.backend.fill(self.selected, "测试")
        self.assertIn(("set", self.editor, "AXFocused", True), self.api.actions)
        self.assertEqual(self.api.actions[-1], ("paste",))

    def test_multiple_unlabelled_multiline_editors_reject_even_if_one_count_unavailable(self):
        self.editor.attributes["AXDescription"] = ""
        other = self.api.add_input(self.window, label="", count=None)
        self.root.attributes["AXFocusedUIElement"] = self.window
        for count in (None, 10):
            with self.subTest(other_count=count):
                other.attributes["AXNumberOfCharacters"] = count
                self.api.actions.clear()
                with self.assertRaisesRegex(target.TargetError, "唯一"):
                    self.backend.fill(self.selected, "测试")
                self.assertFalse(any(action[0] in ("set", "clipboard", "paste") for action in self.api.actions))

    def test_unknown_single_line_field_never_qualifies_for_fallback(self):
        self.editor.attributes.update(AXDescription="", AXRole="AXTextField")
        self.root.attributes["AXFocusedUIElement"] = self.window
        with self.assertRaisesRegex(target.TargetError, "唯一"):
            self.backend.fill(self.selected, "测试")
        self.assertFalse(any(action[0] in ("set", "clipboard", "paste") for action in self.api.actions))

    def test_browser_and_code_ide_unlabelled_inputs_never_qualify_for_fallback(self):
        self.editor.attributes["AXDescription"] = ""
        self.root.attributes["AXFocusedUIElement"] = self.window
        examples = (
            ("Safari", "com.apple.safari"), ("Chrome", "com.google.chrome"),
            ("Code", "com.microsoft.vscode"), ("Cursor", "com.todesktop.230313mzl4w4u92"),
            ("Trae", "com.trae.app"),
        )
        for name, bundle in examples:
            with self.subTest(app=name):
                self.app.update(name=name, bundle=bundle, identity=bundle)
                selected = self.backend.list_windows()[0]
                self.api.actions.clear()
                with self.assertRaisesRegex(target.TargetError, "唯一"):
                    self.backend.fill(selected, "测试")
                self.assertFalse(any(action[0] in ("set", "clipboard", "paste") for action in self.api.actions))

    def test_unlabelled_fallback_requires_valid_character_count(self):
        self.editor.attributes["AXDescription"] = ""
        self.root.attributes["AXFocusedUIElement"] = self.window
        for count in (None, True, False, -1, "20", 20.0):
            with self.subTest(count=count):
                self.editor.attributes["AXNumberOfCharacters"] = count
                self.api.actions.clear()
                with self.assertRaisesRegex(target.TargetError, "唯一"):
                    self.backend.fill(self.selected, "测试")
                self.assertFalse(any(action[0] in ("set", "clipboard", "paste") for action in self.api.actions))

    def test_unlabelled_fallback_still_requires_readable_selection_after_focus(self):
        self.editor.attributes["AXDescription"] = ""
        self.root.attributes["AXFocusedUIElement"] = self.window
        self.api.selection = Mock(return_value=None)
        with patch.object(target.time, "monotonic", side_effect=[0.0, 0.0, 0.0, 0.8]):
            with self.assertRaisesRegex(target.TargetError, "无法读取光标"):
                self.backend.fill(self.selected, "测试")
        self.assertIn(("set", self.editor, "AXFocused", True), self.api.actions)
        self.assertFalse(any(action[0] in ("clipboard", "paste") for action in self.api.actions))

    def test_labelled_chat_input_takes_priority_over_unlabelled_fallback(self):
        self.editor.attributes["AXDescription"] = ""
        composer = self.api.add_input(self.window, label="Agent composer", count=12)
        self.root.attributes["AXFocusedUIElement"] = self.window
        self.backend.fill(self.selected, "测试")
        self.assertIn(("set", composer, "AXFocused", True), self.api.actions)
        self.assertNotIn(("set", self.editor, "AXFocused", True), self.api.actions)
        self.assertEqual(self.api.actions[-1], ("paste",))

    def test_composer_is_selected_instead_of_code_editor(self):
        self.editor.attributes["AXDescription"] = "code editor"
        composer = self.api.add_input(self.window, label="Agent composer", count=12)
        self.root.attributes["AXFocusedUIElement"] = self.window
        self.backend.fill(self.selected, "测试")
        self.assertIn(("set", composer, "AXFocused", True), self.api.actions)
        self.assertIn(("selection", 12), self.api.actions)
        self.assertNotIn(("set", self.editor, "AXFocused", True), self.api.actions)

    def test_unfocused_main_input_appends_to_end(self):
        self.root.attributes["AXFocusedUIElement"] = self.window
        self.backend.fill(self.selected, "测试")
        self.assertIn(("selection", 20), self.api.actions)
        self.assertEqual(self.api.actions[-2:], [("clipboard", "测试"), ("paste",)])

    def test_focused_input_preserves_caret_and_collapses_selected_range_to_end(self):
        self.editor.span = (4, 3)
        self.api.focus(self.selected.pid, self.window, self.editor)
        token = self.backend.capture_focus(self.selected)
        self.assertIsInstance(token, target.FocusToken)
        self.backend.fill(self.selected, "测试", token)
        self.assertIn(("selection", 7), self.api.actions)
        self.assertNotIn(("selection", 20), self.api.actions)
        self.assertFalse(any(action[0] == "activate" for action in self.api.actions))

    def test_remembered_input_restored_after_talkman_takes_focus(self):
        self.editor.span = (8, 0)
        self.api.focus(self.selected.pid, self.window, self.editor)
        token = self.backend.capture_focus(self.selected)
        self.api.front = 123
        self.backend.fill(self.selected, "测试", token)
        self.assertEqual(self.editor.span, (8, 0))
        self.assertFalse(any(action[0] in ("selection", "move_caret") for action in self.api.actions))
        self.assertEqual(self.api.actions[-1], ("paste",))

    def test_remembered_unlabelled_input_can_be_refocused_and_appends(self):
        self.editor.attributes["AXDescription"] = ""
        self.editor.span = (4, 3)
        self.api.focus(self.selected.pid, self.window, self.editor)
        token = self.backend.capture_focus(self.selected)
        self.root.attributes["AXFocusedUIElement"] = self.window
        self.api.front = 123
        self.backend._main_input = Mock(side_effect=AssertionError("应恢复已验证的控件"))
        self.backend.fill(self.selected, "测试", token)
        self.backend._main_input.assert_not_called()
        self.assertIn(("set", self.editor, "AXFocused", True), self.api.actions)
        self.assertEqual(self.editor.span, (20, 0))
        self.assertNotIn(("selection", 7), self.api.actions)
        self.assertEqual(self.api.actions[-2:], [("clipboard", "测试"), ("paste",)])

    def test_remembered_input_resolves_tied_chat_fields(self):
        self.api.add_input(self.window)
        self.api.focus(self.selected.pid, self.window, self.editor)
        token = self.backend.capture_focus(self.selected)
        self.root.attributes["AXFocusedUIElement"] = self.window
        self.api.front = 123
        self.backend.fill(self.selected, "测试", token)
        self.assertIn(("set", self.editor, "AXFocused", True), self.api.actions)
        self.assertEqual(self.api.actions[-1], ("paste",))

    def test_remembered_input_refocus_without_selection_setter_appends(self):
        self.editor.attributes["AXDescription"] = ""
        self.editor.writable.remove("AXSelectedTextRange")
        self.editor.span = (4, 3)
        self.api.focus(self.selected.pid, self.window, self.editor)
        token = self.backend.capture_focus(self.selected)
        self.root.attributes["AXFocusedUIElement"] = self.window
        self.api.front = 123
        self.backend.fill(self.selected, "测试", token)
        self.assertIn(("move_caret", True), self.api.actions)
        self.assertNotIn(("move_caret", False), self.api.actions)
        self.assertEqual(self.editor.span, (20, 0))
        self.assertEqual(self.api.actions[-1], ("paste",))

    def test_invalid_remembered_session_does_not_authorize_unlabelled_input(self):
        self.editor.attributes["AXDescription"] = ""
        self.api.add_input(self.window, label="")
        self.api.focus(self.selected.pid, self.window, self.editor)
        token = self.backend.capture_focus(self.selected)
        self.root.attributes["AXFocusedUIElement"] = self.window
        for field, value in (("window_id", self.selected.hwnd + 1), ("pid", 123), ("process_started", 999.0)):
            with self.subTest(field=field):
                self.api.actions.clear()
                with self.assertRaisesRegex(target.TargetError, "唯一"):
                    self.backend.fill(self.selected, "测试", replace(token, **{field: value}))
                self.assertFalse(any(action[0] in ("set", "clipboard", "paste") for action in self.api.actions))

    def test_invalid_remembered_element_never_restores_focus(self):
        self.editor.attributes["AXDescription"] = ""
        self.api.focus(self.selected.pid, self.window, self.editor)
        token = self.backend.capture_focus(self.selected)
        self.root.attributes["AXFocusedUIElement"] = self.window
        second = self.api.add_window(self.root, "另一个窗口")
        attributes = self.editor.attributes.copy()
        writable = self.editor.writable.copy()
        examples = (
            {"AXDescription": "搜索"}, {"AXSubrole": "AXSecureTextField"},
            {"AXSubrole": "AXSearchField"}, {"AXHidden": True}, {"AXEnabled": False},
            {"AXRole": "AXStaticText"}, {"AXParent": second}, {"AXWindow": second},
            {"AXParent": None}, {"readonly": True},
        )
        for change in examples:
            with self.subTest(change=change):
                self.editor.attributes = attributes.copy()
                self.editor.writable = writable.copy()
                if "readonly" in change:
                    self.editor.writable.remove("AXValue")
                else:
                    self.editor.attributes.update(change)
                self.api.actions.clear()
                with self.assertRaisesRegex(target.TargetError, "唯一"):
                    self.backend.fill(self.selected, "测试", token)
                self.assertFalse(any(action[0] in ("set", "clipboard", "paste") for action in self.api.actions))

    def test_missing_focus_setter_reports_found_input_stage_without_paste(self):
        self.editor.attributes["AXDescription"] = ""
        self.api.focus(self.selected.pid, self.window, self.editor)
        token = self.backend.capture_focus(self.selected)
        self.root.attributes["AXFocusedUIElement"] = self.window
        self.editor.writable.remove("AXFocused")
        with self.assertRaisesRegex(target.TargetError, "先前选中的输入框.*不支持 AXFocused"):
            self.backend.fill(self.selected, "测试", token)
        self.assertFalse(any(action[0] in ("set", "clipboard", "paste") for action in self.api.actions))

    def test_failed_focus_setter_reports_stage_without_private_error(self):
        self.root.attributes["AXFocusedUIElement"] = self.window
        self.api.set = Mock(side_effect=RuntimeError("私密异常内容"))
        with self.assertRaisesRegex(target.TargetError, "主输入框的 AXFocused 设置未成功") as raised:
            self.backend.fill(self.selected, "测试")
        self.assertNotIn("私密异常内容", str(raised.exception))
        self.assertFalse(any(action[0] in ("clipboard", "paste") for action in self.api.actions))

    def test_successful_focus_setter_without_focus_proof_never_pastes(self):
        self.root.attributes["AXFocusedUIElement"] = self.window
        self.api.set = Mock()
        with patch.object(target.time, "monotonic", side_effect=[0.0, 0.0, 1.6]):
            with self.assertRaisesRegex(target.TargetError, "目标输入框未获得焦点"):
                self.backend.fill(self.selected, "测试")
        self.api.set.assert_called_once_with(self.editor, "AXFocused", True)
        self.assertFalse(any(action[0] in ("clipboard", "paste") for action in self.api.actions))

    def test_activated_window_without_window_focus_reports_window_stage(self):
        self.api.activate = Mock()
        with patch.object(target.time, "monotonic", side_effect=[0.0, 1.6]):
            with self.assertRaisesRegex(target.TargetError, "目标窗口未获得焦点"):
                self.backend.fill(self.selected, "测试")
        self.assertEqual(self.api.actions, [])

    def test_automatically_restored_old_caret_without_token_appends(self):
        self.editor.span = (8, 0)
        self.api.focus(self.selected.pid, self.window, self.editor)
        self.api.front = 123
        self.backend.fill(self.selected, "测试")
        self.assertIn(("selection", 20), self.api.actions)
        self.assertNotIn(("selection", 8), self.api.actions)

    def test_disabled_numeric_ax_boolean_cannot_be_filled(self):
        self.editor.attributes["AXEnabled"] = 0
        self.assertFalse(self.backend._is_editable(self.editor))

    def test_changed_remembered_input_chooses_main_editor_and_appends(self):
        secondary = self.api.add_input(self.window, label="普通字段")
        self.api.focus(self.selected.pid, self.window, self.editor)
        token = self.backend.capture_focus(self.selected)
        self.api.focus(self.selected.pid, self.window, secondary)
        self.backend.fill(self.selected, "测试", token)
        self.assertIn(("set", self.editor, "AXFocused", True), self.api.actions)
        self.assertIn(("selection", 20), self.api.actions)
        self.assertNotIn(("set", secondary, "AXFocused", True), self.api.actions)

    def test_wrong_token_session_never_preserves_caret(self):
        self.api.focus(self.selected.pid, self.window, self.editor)
        token = self.backend.capture_focus(self.selected)
        self.editor.span = (3, 0)
        self.backend.fill(self.selected, "测试", replace(token, process_started=999.0))
        self.assertIn(("selection", 20), self.api.actions)

    def test_focus_change_after_clipboard_cancels_paste(self):
        self.api.focus(self.selected.pid, self.window, self.editor)
        self.api.after_clipboard = lambda: setattr(self.api, "front", 123)
        with self.assertRaisesRegex(target.TargetError, "失去焦点"):
            self.backend.fill(self.selected, "测试")
        self.assertIn(("clipboard", "测试"), self.api.actions)
        self.assertNotIn(("paste",), self.api.actions)

    def test_restart_after_clipboard_cancels_paste(self):
        self.api.focus(self.selected.pid, self.window, self.editor)
        self.api.after_clipboard = lambda: self.app.update(started=999.0)
        with self.assertRaisesRegex(target.TargetError, "会话已变化"):
            self.backend.fill(self.selected, "测试")
        self.assertNotIn(("paste",), self.api.actions)

    def test_readable_caret_without_selection_setter_can_insert_without_moving(self):
        self.api.focus(self.selected.pid, self.window, self.editor)
        self.editor.writable.remove("AXSelectedTextRange")
        self.editor.span = (6, 0)
        self.backend.fill(self.selected, "测试")
        self.assertEqual(self.api.actions, [("clipboard", "测试"), ("paste",)])
        self.assertEqual(self.editor.span, (6, 0))

    def test_remembered_caret_without_setter_can_insert_after_reactivation(self):
        self.api.focus(self.selected.pid, self.window, self.editor)
        self.editor.writable.remove("AXSelectedTextRange")
        self.editor.span = (6, 0)
        token = self.backend.capture_focus(self.selected)
        self.api.front = 123
        self.backend.fill(self.selected, "测试", token)
        self.assertEqual(self.editor.span, (6, 0))
        self.assertFalse(any(action[0] in ("selection", "move_caret") for action in self.api.actions))
        self.assertEqual(self.api.actions[-2:], [("clipboard", "测试"), ("paste",)])

    def test_readable_selection_without_setter_collapses_to_selection_end(self):
        self.api.focus(self.selected.pid, self.window, self.editor)
        self.editor.writable.remove("AXSelectedTextRange")
        self.editor.span = (4, 3)
        self.backend.fill(self.selected, "测试")
        self.assertEqual(self.editor.span, (7, 0))
        self.assertEqual(self.api.actions, [("move_caret", False), ("clipboard", "测试"), ("paste",)])

    def test_unfocused_input_without_setter_moves_to_document_end(self):
        self.root.attributes["AXFocusedUIElement"] = self.window
        self.editor.writable.remove("AXSelectedTextRange")
        self.editor.span = (4, 3)
        self.backend.fill(self.selected, "测试")
        self.assertEqual(self.editor.span, (20, 0))
        self.assertIn(("move_caret", True), self.api.actions)
        self.assertNotIn(("move_caret", False), self.api.actions)
        self.assertEqual(self.api.actions[-2:], [("clipboard", "测试"), ("paste",)])

    def test_keyboard_movement_waits_for_ax_readback(self):
        self.root.attributes["AXFocusedUIElement"] = self.window
        self.editor.writable.remove("AXSelectedTextRange")
        self.api.move_caret = Mock()
        self.api.selection = Mock(side_effect=[(4, 3), (4, 3), (20, 0), (20, 0), (20, 0)])
        with patch.object(target.time, "sleep") as sleep:
            self.backend.fill(self.selected, "测试")
        self.api.move_caret.assert_called_once_with(to_end=True)
        sleep.assert_called_once_with(0.03)
        self.assertEqual(self.api.actions[-2:], [("clipboard", "测试"), ("paste",)])

    def fill_with_fake_clock(self, on_sleep=None):
        clock = [0.0]

        def advance(seconds):
            clock[0] += seconds
            if on_sleep:
                on_sleep(clock[0])

        with patch.object(target.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(target.time, "sleep", side_effect=advance):
            return self.backend.fill(self.selected, "测试")

    def test_first_fill_retries_ignored_ax_append_then_pastes_once(self):
        self.app.update(name="豆包", bundle="fixture.doubao", identity="fixture.doubao")
        self.selected = self.backend.list_windows()[0]
        self.root.attributes["AXFocusedUIElement"] = self.window
        self.editor.span = (4, 3)
        self.api.set_selection = Mock()
        self.fill_with_fake_clock()
        self.api.set_selection.assert_called_once_with(self.editor, 20)
        self.assertEqual(self.editor.span, (20, 0))
        self.assertEqual(self.api.actions.count(("move_caret", True)), 1)
        self.assertEqual(self.api.actions.count(("clipboard", "测试")), 1)
        self.assertEqual(self.api.actions.count(("paste",)), 1)

    def test_delayed_ax_append_does_not_need_keyboard_retry(self):
        self.root.attributes["AXFocusedUIElement"] = self.window
        self.api.set_selection = Mock()

        def update_selection(now):
            if now >= 0.09:
                self.editor.span = (20, 0)

        self.fill_with_fake_clock(update_selection)
        self.assertFalse(any(action[0] == "move_caret" for action in self.api.actions))
        self.assertEqual(self.api.actions.count(("paste",)), 1)

    def test_first_fill_retries_ignored_keyboard_append_then_pastes_once(self):
        self.root.attributes["AXFocusedUIElement"] = self.window
        self.editor.writable.remove("AXSelectedTextRange")
        move = self.api.move_caret
        attempts = [0]

        def delayed_move(*, to_end):
            attempts[0] += 1
            if attempts[0] > 1:
                move(to_end=to_end)

        self.api.move_caret = Mock(side_effect=delayed_move)
        self.fill_with_fake_clock()
        self.assertEqual(self.api.move_caret.call_args_list, [call(to_end=True), call(to_end=True)])
        self.assertEqual(self.editor.span, (20, 0))
        self.assertEqual(self.api.actions.count(("clipboard", "测试")), 1)
        self.assertEqual(self.api.actions.count(("paste",)), 1)

    def test_failed_append_retry_never_writes_clipboard_or_pastes(self):
        self.root.attributes["AXFocusedUIElement"] = self.window
        self.api.set_selection = Mock()
        self.api.move_caret = Mock()
        with self.assertRaisesRegex(target.TargetError, "无法确认目标光标"):
            self.fill_with_fake_clock()
        self.api.move_caret.assert_called_once_with(to_end=True)
        self.assertFalse(any(action[0] in ("clipboard", "paste") for action in self.api.actions))

    def test_append_wait_does_not_retry_after_user_moves_selection(self):
        self.root.attributes["AXFocusedUIElement"] = self.window
        self.api.set_selection = Mock()
        with self.assertRaisesRegex(target.TargetError, "光标或选区已变化"):
            self.fill_with_fake_clock(lambda now: setattr(self.editor, "span", (6, 0)))
        self.assertFalse(any(action[0] in ("move_caret", "clipboard", "paste") for action in self.api.actions))

    def test_append_wait_does_not_retry_after_document_changes(self):
        self.root.attributes["AXFocusedUIElement"] = self.window
        self.api.set_selection = Mock()
        with self.assertRaisesRegex(target.TargetError, "文字末尾已变化"):
            self.fill_with_fake_clock(lambda now: self.editor.attributes.update(AXNumberOfCharacters=25))
        self.assertFalse(any(action[0] in ("move_caret", "clipboard", "paste") for action in self.api.actions))

    def test_append_wait_does_not_retry_after_focus_is_lost(self):
        self.root.attributes["AXFocusedUIElement"] = self.window
        self.api.set_selection = Mock()
        with self.assertRaisesRegex(target.TargetError, "失去焦点"):
            self.fill_with_fake_clock(lambda now: setattr(self.api, "front", 123))
        self.assertFalse(any(action[0] in ("move_caret", "clipboard", "paste") for action in self.api.actions))

    def test_document_change_at_final_retry_check_cancels_before_movement(self):
        self.root.attributes["AXFocusedUIElement"] = self.window
        self.api.set_selection = Mock()
        retry_reads = [None]

        def advance(now):
            if now >= 0.15 and retry_reads[0] is None:
                retry_reads[0] = 0

        def read_selection(element):
            if retry_reads[0] is not None:
                retry_reads[0] += 1
                if retry_reads[0] == 2:
                    self.editor.attributes["AXNumberOfCharacters"] = 25
            return self.editor.span

        self.api.selection = Mock(side_effect=read_selection)
        with self.assertRaisesRegex(target.TargetError, "文字末尾已变化"):
            self.fill_with_fake_clock(advance)
        self.assertFalse(any(action[0] in ("move_caret", "clipboard", "paste") for action in self.api.actions))

    def test_append_retry_losing_focus_never_writes_clipboard_or_pastes(self):
        self.root.attributes["AXFocusedUIElement"] = self.window
        self.api.set_selection = Mock()
        self.api.move_caret = Mock(side_effect=lambda **kwargs: setattr(self.api, "front", 123))
        with self.assertRaisesRegex(target.TargetError, "失去焦点"):
            self.fill_with_fake_clock()
        self.api.move_caret.assert_called_once_with(to_end=True)
        self.assertFalse(any(action[0] in ("clipboard", "paste") for action in self.api.actions))

    def test_unreadable_selection_refuses_before_keyboard_or_clipboard(self):
        self.api.focus(self.selected.pid, self.window, self.editor)
        self.api.selection = Mock(return_value=None)
        with patch.object(target.time, "monotonic", side_effect=[0.0, 0.8]):
            with self.assertRaisesRegex(target.TargetError, "无法读取光标"):
                self.backend.fill(self.selected, "测试")
        self.assertEqual(self.api.actions, [])

    def test_newly_focused_input_waits_for_readable_selection(self):
        self.api.focus(self.selected.pid, self.window, self.editor)
        self.editor.writable.remove("AXSelectedTextRange")
        self.api.selection = Mock(side_effect=[None, (6, 0), (6, 0), (6, 0)])
        with patch.object(target.time, "sleep") as sleep:
            self.backend.fill(self.selected, "测试")
        sleep.assert_called_once_with(0.03)
        self.assertEqual(self.api.actions, [("clipboard", "测试"), ("paste",)])

    def test_focus_lost_while_waiting_for_initial_selection_cancels(self):
        self.api.focus(self.selected.pid, self.window, self.editor)
        self.api.selection = Mock(return_value=None)
        with patch.object(target.time, "sleep", side_effect=lambda seconds: setattr(self.api, "front", 123)):
            with self.assertRaisesRegex(target.TargetError, "失去焦点"):
                self.backend.fill(self.selected, "测试")
        self.assertEqual(self.api.actions, [])

    def test_invalid_selection_refuses_without_input_events(self):
        self.api.focus(self.selected.pid, self.window, self.editor)
        for span in ((-1, 0), (0, -1), (True, 0), (0, False), ("1", 0), (1.5, 0), (1,), "bad"):
            with self.subTest(span=span):
                self.editor.span = span
                with self.assertRaisesRegex(target.TargetError, "光标位置无效"):
                    self.backend.fill(self.selected, "测试")
                self.assertEqual(self.api.actions, [])

    def test_unconfirmed_keyboard_movement_never_pastes(self):
        self.api.focus(self.selected.pid, self.window, self.editor)
        self.editor.writable.remove("AXSelectedTextRange")
        self.editor.span = (4, 3)
        self.api.move_caret = Mock()
        with patch.object(target.time, "monotonic", side_effect=[0.0, 0.0, 0.8]):
            with self.assertRaisesRegex(target.TargetError, "确认目标光标"):
                self.backend.fill(self.selected, "测试")
        self.api.move_caret.assert_called_once_with(to_end=False)
        self.assertEqual(self.api.actions, [])

    def test_focus_loss_during_keyboard_movement_cancels_before_clipboard(self):
        self.api.focus(self.selected.pid, self.window, self.editor)
        self.editor.writable.remove("AXSelectedTextRange")
        self.editor.span = (4, 3)
        self.api.move_caret = Mock(side_effect=lambda **kwargs: setattr(self.api, "front", 123))
        with self.assertRaisesRegex(target.TargetError, "失去焦点"):
            self.backend.fill(self.selected, "测试")
        self.assertEqual(self.api.actions, [])

    def test_document_change_during_keyboard_movement_cancels_append(self):
        self.root.attributes["AXFocusedUIElement"] = self.window
        self.editor.writable.remove("AXSelectedTextRange")

        def move_then_grow(**kwargs):
            self.editor.span = (20, 0)
            self.editor.attributes["AXNumberOfCharacters"] = 25

        self.api.move_caret = Mock(side_effect=move_then_grow)
        with self.assertRaisesRegex(target.TargetError, "文字末尾已变化"):
            self.backend.fill(self.selected, "测试")
        self.assertFalse(any(action[0] in ("clipboard", "paste") for action in self.api.actions))

    def test_selection_change_during_clipboard_write_cancels_paste(self):
        self.api.focus(self.selected.pid, self.window, self.editor)
        self.editor.writable.remove("AXSelectedTextRange")
        self.editor.span = (6, 0)
        for span in ((6, 2), (8, 0), None):
            with self.subTest(span=span):
                self.editor.span = (6, 0)
                self.api.actions.clear()
                self.api.after_clipboard = lambda: setattr(self.editor, "span", span)
                with self.assertRaisesRegex(target.TargetError, "光标或选区已变化"):
                    self.backend.fill(self.selected, "测试")
                self.assertEqual(self.api.actions, [("clipboard", "测试")])

    def test_document_change_during_clipboard_write_cancels_append(self):
        self.root.attributes["AXFocusedUIElement"] = self.window
        self.api.after_clipboard = lambda: self.editor.attributes.update(AXNumberOfCharacters=25)
        with self.assertRaisesRegex(target.TargetError, "文字末尾已变化"):
            self.backend.fill(self.selected, "测试")
        self.assertNotIn(("paste",), self.api.actions)

    def test_character_count_becoming_boolean_cannot_confirm_append(self):
        self.root.attributes["AXFocusedUIElement"] = self.window
        self.editor.attributes["AXNumberOfCharacters"] = 0
        self.api.after_clipboard = lambda: self.editor.attributes.update(AXNumberOfCharacters=False)
        with self.assertRaisesRegex(target.TargetError, "文字末尾已变化"):
            self.backend.fill(self.selected, "测试")
        self.assertNotIn(("paste",), self.api.actions)

    def test_append_requires_character_count_without_reading_value(self):
        del self.editor.attributes["AXNumberOfCharacters"]
        self.root.attributes["AXFocusedUIElement"] = self.window
        real_get = self.api.get
        self.api.get = Mock(side_effect=real_get)
        with self.assertRaisesRegex(target.TargetError, "文字末尾"):
            self.backend.fill(self.selected, "测试")
        self.assertFalse(any(item.args[1] == "AXValue" for item in self.api.get.call_args_list))
        self.assertNotIn(("paste",), self.api.actions)

    def test_selected_range_must_be_confirmed_before_paste(self):
        self.api.focus(self.selected.pid, self.window, self.editor)
        self.editor.span = (1, 3)
        self.api.set_selection = Mock()
        with patch.object(target.time, "monotonic", side_effect=[0.0, 0.0, 0.8]):
            with self.assertRaisesRegex(target.TargetError, "确认目标光标"):
                self.backend.fill(self.selected, "测试")
        self.assertEqual(self.api.actions, [])


class MacScanTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI()
        self.app, self.root = self.api.add_app(name="豆包", bundle="fixture.doubao")
        self.window = self.api.add_window(self.root, title="私密窗口标题")
        self.api.add_input(self.window).attributes["AXValue"] = "私密输入正文"
        self.backend = target.MacTargetBackend(api=self.api)

    def remove_windows(self):
        self.root.attributes["AXWindows"] = []
        self.root.attributes["AXChildren"] = []
        self.root.attributes.pop("AXFocusedWindow", None)
        self.root.attributes.pop("AXMainWindow", None)

    def test_permission_failure_replaces_previous_successful_scan(self):
        self.backend.list_windows()
        self.api.allowed = False
        with self.assertRaises(target.TargetError):
            self.backend.list_windows()
        self.assertIn("辅助功能授权：未允许", self.backend.last_scan_report)
        self.assertNotIn("可用窗口：1", self.backend.last_scan_report)
        self.assertIn("辅助功能", self.backend.last_scan_note)
        self.assertEqual(self.backend._records, {})

    def test_application_list_failure_replaces_previous_successful_scan(self):
        self.backend.list_windows()
        self.api.applications = Mock(side_effect=RuntimeError("私密错误正文"))
        with self.assertRaisesRegex(target.TargetError, "无法读取 Mac 运行应用列表"):
            self.backend.list_windows()
        self.assertIn("列表读取失败（RuntimeError）", self.backend.last_scan_report)
        self.assertNotIn("可用窗口：1", self.backend.last_scan_report)
        self.assertNotIn("私密错误正文", self.backend.last_scan_report)
        self.assertEqual(self.backend._records, {})

    def test_native_initialization_failure_has_current_diagnostic(self):
        self.backend.list_windows()
        self.backend._api = None
        with patch.object(target, "_NativeAPI", side_effect=RuntimeError("私密错误正文")):
            with self.assertRaisesRegex(target.TargetError, "组件初始化失败"):
                self.backend.list_windows()
        self.assertIn("组件初始化失败（RuntimeError）", self.backend.last_scan_report)
        self.assertNotIn("可用窗口：1", self.backend.last_scan_report)
        self.assertNotIn("私密错误正文", self.backend.last_scan_report)

    def test_diagnostic_contains_metadata_without_title_or_text(self):
        self.api.get = Mock(side_effect=self.api.get)
        self.backend.list_windows()
        report = self.backend.last_scan_report
        for metadata in ("豆包", "fixture.doubao", "9876", "AXWindows：0", "可用窗口：1"):
            self.assertIn(metadata, report)
        self.assertNotIn("私密窗口标题", report)
        self.assertNotIn("私密输入正文", report)
        self.assertFalse(any(item.args[1] == "AXValue" for item in self.api.get.call_args_list))
        self.assertEqual(self.api.actions, [])

    def test_error_code_reported_with_safe_children_fallback(self):
        self.api.read_with_status = Mock(return_value=(-25204, None))
        windows = self.backend.list_windows()
        self.assertEqual(len(windows), 1)
        self.assertIn("AXWindows：-25204", self.backend.last_scan_report)
        self.assertIn("回退：AXChildren", self.backend.last_scan_report)
        self.assertIsNotNone(self.backend.validate_window(windows[0]))

    def test_direct_window_fallbacks_are_deduplicated_and_revalidated(self):
        for source in ("AXChildren", "AXFocusedWindow", "AXMainWindow"):
            with self.subTest(source=source):
                self.remove_windows()
                self.root.attributes[source] = [self.window] if source == "AXChildren" else self.window
                windows = self.backend.list_windows()
                self.assertEqual(len(windows), 1)
                self.assertIn(f"回退：{source}", self.backend.last_scan_report)
                self.assertIsNotNone(self.backend.validate_window(windows[0]))
        self.root.attributes.update(
            AXFocusedWindow=self.window, AXMainWindow=self.window, AXChildren=[self.window, self.window]
        )
        self.assertEqual(len(self.backend.list_windows()), 1)

    def test_fallback_refuses_foreign_unknown_parent_and_non_window_elements(self):
        _, other_root = self.api.add_app(123, "Unknown", "fixture.other")
        foreign = self.api.add_window(other_root)
        unowned = Element("AXWindow")
        group = Element("AXGroup", parent=self.root)
        self.remove_windows()
        self.root.attributes.update(AXFocusedWindow=foreign, AXMainWindow=unowned, AXChildren=[foreign, unowned, group])
        self.assertEqual(self.backend.list_windows(), [])
        self.assertEqual(self.api.actions, [])

    def test_axwindows_also_refuses_explicit_foreign_parent(self):
        _, other_root = self.api.add_app(123, "Unknown", "fixture.other")
        foreign = self.api.add_window(other_root)
        self.remove_windows()
        self.root.attributes["AXWindows"] = [foreign]
        self.assertEqual(self.backend.list_windows(), [])

    def test_one_application_exception_does_not_hide_other_windows(self):
        _, second_root = self.api.add_app(123, "Codex", "com.openai.codex")
        self.api.add_window(second_root)
        original_root = self.api.root
        self.api.root = Mock(side_effect=lambda pid: (_ for _ in ()).throw(RuntimeError()) if pid == 9876 else original_root(pid))
        self.assertEqual([window.app for window in self.backend.list_windows()], ["Codex"])
        self.assertIn("豆包", self.backend.last_scan_report)
        self.assertIn("枚举异常：RuntimeError", self.backend.last_scan_report)

    def test_app_info_exception_does_not_reuse_previous_app_name(self):
        bad_app, _ = self.api.add_app(123, "Unreadable", "fixture.other")
        self.remove_windows()
        self.api.app_info = Mock(side_effect=lambda app: (_ for _ in ()).throw(RuntimeError()) if app is bad_app else app)
        self.assertEqual(self.backend.list_windows(), [])
        self.assertIn("未知应用：枚举异常", self.backend.last_scan_note)
        self.assertNotIn("豆包：枚举异常", self.backend.last_scan_note)

    def test_missing_launch_identity_reports_actual_filter_reason(self):
        self.app["started"] = None
        self.assertEqual(self.backend.list_windows(), [])
        self.assertIn("跳过：缺少可验证的启动身份", self.backend.last_scan_report)
        self.assertIn("缺少可验证的启动身份", self.backend.last_scan_note)
        self.assertEqual(self.api.actions, [])

    def test_manual_accessibility_enables_once_then_reads_windows_once_more(self):
        self.remove_windows()
        self.root.writable.add("AXManualAccessibility")
        original_set = self.api.set

        def enable(element, attribute, value):
            original_set(element, attribute, value)
            if attribute == "AXManualAccessibility":
                self.api.add_window(self.root)

        self.api.set = Mock(side_effect=enable)
        self.api.get = Mock(side_effect=self.api.get)
        self.assertEqual(len(self.backend.list_windows()), 1)
        self.api.set.assert_called_once_with(self.root, "AXManualAccessibility", True)
        window_reads = [item for item in self.api.get.call_args_list if item.args == (self.root, "AXWindows")]
        self.assertEqual(len(window_reads), 2)
        self.assertIn("辅助树：已尝试并启用", self.backend.last_scan_report)
        self.assertIn("重读 AXWindows：0", self.backend.last_scan_report)
        self.backend.list_windows()
        self.api.set.assert_called_once()
        self.assertFalse(any(item.args[1] in ("AXValue", "AXEnhancedUserInterface") for item in self.api.get.call_args_list))
        self.assertEqual(self.api.actions, [("set", self.root, "AXManualAccessibility", True)])

    def test_manual_accessibility_unsupported_does_not_request_permissions(self):
        self.remove_windows()
        self.assertEqual(self.backend.list_windows(), [])
        self.assertIn("辅助树：不支持，未尝试", self.backend.last_scan_report)
        self.assertIn("辅助功能授权：已允许", self.backend.last_scan_report)
        self.assertNotIn("隐私与安全性", self.backend.last_scan_note)
        self.assertEqual(self.api.actions, [])

    def test_manual_accessibility_already_enabled_is_not_set_again(self):
        self.remove_windows()
        self.root.writable.add("AXManualAccessibility")
        self.root.attributes["AXManualAccessibility"] = True
        self.assertEqual(self.backend.list_windows(), [])
        self.assertIn("辅助树：已启用，未重复设置", self.backend.last_scan_report)
        self.assertEqual(self.api.actions, [])

    def test_manual_accessibility_write_failure_does_not_hide_other_app(self):
        self.remove_windows()
        self.root.writable.add("AXManualAccessibility")
        self.api.write_with_status = Mock(return_value=-25205)
        _, second_root = self.api.add_app(123, "Codex", "com.openai.codex")
        self.api.add_window(second_root)
        self.assertEqual([window.app for window in self.backend.list_windows()], ["Codex"])
        self.api.write_with_status.assert_called_once_with(self.root, "AXManualAccessibility", True)
        self.assertIn("尝试失败（AX 错误 -25205）", self.backend.last_scan_report)
        self.assertIn("部分应用读取失败", self.backend.last_scan_note)
        self.assertEqual(self.api.actions, [])

    def test_manual_accessibility_set_exception_reports_failure_without_retry(self):
        self.remove_windows()
        self.root.writable.add("AXManualAccessibility")
        self.api.set = Mock(side_effect=RuntimeError())
        self.assertEqual(self.backend.list_windows(), [])
        self.api.set.assert_called_once()
        self.assertIn("尝试失败（调用异常）", self.backend.last_scan_report)
        self.assertIn("辅助树启用失败", self.backend.last_scan_note)

    def test_manual_accessibility_only_considered_for_allowed_apps_without_windows(self):
        self.root.writable.add("AXManualAccessibility")
        self.api.settable = Mock(side_effect=self.api.settable)
        self.backend.list_windows()
        self.api.settable.assert_not_called()
        self.remove_windows()
        self.app["name"] = "Doubao Helper"
        self.backend.list_windows()
        self.api.settable.assert_not_called()
        self.assertEqual(self.api.actions, [])


class NativeAPITests(unittest.TestCase):
    def make_api(self):
        api = target._NativeAPI.__new__(target._NativeAPI)
        return api

    def test_keyboard_events_are_only_command_v_down_and_up(self):
        api = self.make_api()
        q = Mock()
        q.kCGEventFlagMaskCommand = 0x100000
        q.kCGHIDEventTap = 0
        down, up = object(), object()
        q.CGEventCreateKeyboardEvent.side_effect = [down, up]
        api.quartz = q
        api.paste()
        self.assertEqual(q.CGEventCreateKeyboardEvent.call_args_list, [call(None, 9, True), call(None, 9, False)])
        self.assertEqual(q.CGEventSetFlags.call_args_list, [call(down, 0x100000), call(up, 0x100000)])
        self.assertEqual(q.CGEventPost.call_args_list, [call(0, down), call(0, up)])

    def test_missing_keyboard_event_does_not_post_any_event(self):
        api = self.make_api()
        api.quartz = Mock()
        api.quartz.CGEventCreateKeyboardEvent.side_effect = [object(), None]
        with self.assertRaisesRegex(target.TargetError, "粘贴事件"):
            api.paste()
        api.quartz.CGEventPost.assert_not_called()

    def test_caret_keyboard_events_never_include_enter_or_paste(self):
        for to_end, key, flags in ((True, 125, 0x100000), (False, 124, 0)):
            with self.subTest(to_end=to_end):
                api = self.make_api()
                q = Mock(kCGEventFlagMaskCommand=0x100000, kCGHIDEventTap=0)
                down, up = object(), object()
                q.CGEventCreateKeyboardEvent.side_effect = [down, up]
                api.quartz = q
                api.move_caret(to_end=to_end)
                self.assertEqual(q.CGEventCreateKeyboardEvent.call_args_list, [call(None, key, True), call(None, key, False)])
                self.assertEqual(q.CGEventSetFlags.call_args_list, [call(down, flags), call(up, flags)])
                self.assertEqual(q.CGEventPost.call_args_list, [call(0, down), call(0, up)])

    def test_missing_caret_event_does_not_post_partial_key(self):
        api = self.make_api()
        api.quartz = Mock()
        api.quartz.CGEventCreateKeyboardEvent.side_effect = [object(), None]
        with self.assertRaisesRegex(target.TargetError, "光标定位事件"):
            api.move_caret(to_end=True)
        api.quartz.CGEventPost.assert_not_called()

    def test_ax_output_tuple_shapes_and_range_creation(self):
        api = self.make_api()
        api.ax = Mock()
        api.ax.kAXValueCFRangeType = 4
        api.ax.AXUIElementCopyAttributeValue.return_value = (0, "range")
        api.ax.AXUIElementIsAttributeSettable.return_value = (0, True)
        api.ax.AXValueGetValue.return_value = (True, SimpleNamespace(location=4, length=3))
        api.ax.AXUIElementSetAttributeValue.return_value = 0
        api.ax.AXValueCreate.return_value = "created"
        element = object()
        self.assertEqual(api.selection(element), (4, 3))
        self.assertTrue(api.settable(element, "AXFocused"))
        api.set_selection(element, 7)
        api.ax.AXValueCreate.assert_called_once_with(4, (7, 0))
        api.ax.AXUIElementSetAttributeValue.assert_called_once_with(element, "AXSelectedTextRange", "created")

    def test_range_decoder_accepts_pyobjc_struct_and_tuple_forms(self):
        api = self.make_api()
        api.ax = Mock(kAXValueCFRangeType=4)
        api.get = Mock(return_value=object())
        for span in (SimpleNamespace(location=4, length=3), (4, 3), [4, 3]):
            with self.subTest(span=span):
                api.ax.AXValueGetValue.return_value = (True, span)
                self.assertEqual(api.selection(object()), (4, 3))

    def test_range_decoder_rejects_invalid_or_failed_cf_ranges(self):
        api = self.make_api()
        api.ax = Mock(kAXValueCFRangeType=4)
        api.get = Mock(return_value=object())
        for span in ((-1, 0), (0, -1), (True, 0), (0, False), (4,), (4, 3, 2), ("4", 3), (4.5, 3),
                     SimpleNamespace(location=-1, length=0), None):
            with self.subTest(span=span):
                api.ax.AXValueGetValue.return_value = (True, span)
                self.assertIsNone(api.selection(object()))
        api.ax.AXValueGetValue.return_value = (False, (4, 0))
        self.assertIsNone(api.selection(object()))
        api.ax.AXValueGetValue.side_effect = RuntimeError()
        self.assertIsNone(api.selection(object()))

    def test_single_plural_selection_range_can_supply_unavailable_singular_range(self):
        api = self.make_api()
        api.ax = Mock(kAXValueCFRangeType=4)
        api.get = Mock(side_effect=[None, ["range"]])
        api.ax.AXValueGetValue.return_value = (True, (4, 3))
        element = object()
        self.assertEqual(api.selection(element), (4, 3))
        api.ax.AXValueGetValue.assert_called_once_with("range", 4, None)
        self.assertEqual(api.get.call_args_list, [call(element, "AXSelectedTextRange"), call(element, "AXSelectedTextRanges")])

    def test_missing_or_multiple_plural_selection_ranges_are_not_guessed(self):
        api = self.make_api()
        api.ax = Mock(kAXValueCFRangeType=4)
        for ranges in (None, [], ["first", "second"], 123):
            with self.subTest(ranges=ranges):
                api.get = Mock(side_effect=[None, ranges])
                self.assertIsNone(api.selection(object()))
                api.ax.AXValueGetValue.assert_not_called()

    def test_clipboard_failure_refuses(self):
        api = self.make_api()
        api.appkit = Mock()
        board = api.appkit.NSPasteboard.generalPasteboard.return_value
        board.setString_forType_.return_value = False
        with self.assertRaisesRegex(target.TargetError, "剪贴板"):
            api.clipboard("测试")
        board.clearContents.assert_called_once_with()

    def test_attribute_error_codes_and_exceptions_are_preserved_for_diagnostics(self):
        api = self.make_api()
        api.ax = Mock()
        element = object()
        api.ax.AXUIElementCopyAttributeValue.return_value = (-25204, "unusable")
        self.assertEqual(api.read_with_status(element, "AXWindows"), (-25204, "unusable"))
        self.assertEqual(api.get(element, "AXWindows", []), [])
        api.ax.AXUIElementCopyAttributeValue.side_effect = RuntimeError()
        self.assertEqual(api.read_with_status(element, "AXWindows"), (None, None))
        self.assertEqual(api.get(element, "AXWindows", []), [])

    def test_attribute_write_status_preserved_and_set_remains_strict(self):
        api = self.make_api()
        api.ax = Mock()
        element = object()
        api.ax.AXUIElementSetAttributeValue.return_value = -25205
        self.assertEqual(api.write_with_status(element, "AXManualAccessibility", True), -25205)
        with self.assertRaises(target.TargetError):
            api.set(element, "AXFocused", True)
        api.ax.AXUIElementSetAttributeValue.side_effect = RuntimeError()
        self.assertIsNone(api.write_with_status(element, "AXManualAccessibility", True))

    def test_app_info_includes_activation_policy_and_optional_launch_identity(self):
        api = self.make_api()
        app = Mock()
        app.processIdentifier.return_value = 123
        app.localizedName.return_value = "豆包"
        app.bundleIdentifier.return_value = "fixture.doubao"
        app.launchDate.return_value = None
        app.activationPolicy.return_value = 1
        app.isTerminated.return_value = False
        info = api.app_info(app)
        self.assertEqual(info["activation_policy"], 1)
        self.assertFalse(info["regular"])
        self.assertIsNone(info["started"])

    def test_activation_precedes_raise_and_unsupported_raise_is_not_focus_proof(self):
        api = self.make_api()
        api.ax = Mock()
        api.appkit = SimpleNamespace(NSApplicationActivateIgnoringOtherApps=2)
        app = Mock()
        app.activateWithOptions_.return_value = True
        api.get = Mock(return_value=False)
        api.settable = Mock(return_value=False)
        api.ax.AXUIElementPerformAction.return_value = -25206
        order = Mock()
        order.attach_mock(app.activateWithOptions_, "activate")
        order.attach_mock(api.ax.AXUIElementPerformAction, "raise_window")
        window = object()
        api.activate(app, window)
        self.assertEqual(order.mock_calls, [call.activate(2), call.raise_window(window, "AXRaise")])

    def test_import_is_lazy_and_non_mac_call_has_specific_error(self):
        backend = target.MacTargetBackend()
        self.assertIsNone(backend._api)
        with patch.object(target.sys, "platform", "win32"):
            with self.assertRaisesRegex(target.TargetError, "仅支持 macOS"):
                backend.list_windows()


if __name__ == "__main__":
    unittest.main()
