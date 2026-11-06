"""验证快捷键不会占用文本编辑，并可独立保存和恢复。"""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from talkman_shortcuts import (
    DEFAULT_SHORTCUTS, SETTINGS_FILE, ShortcutError, display_shortcut, load_shortcuts,
    matches_event, normalize_shortcut, released_event, save_shortcuts,
    shortcut_from_event, validate_shortcuts,
)


def event(key, state=0):
    return SimpleNamespace(keysym=key, state=state)


class ShortcutTests(unittest.TestCase):
    def test_defaults_and_chinese_display(self):
        self.assertEqual(validate_shortcuts({}), DEFAULT_SHORTCUTS)
        self.assertEqual(display_shortcut("space"), "空格")
        self.assertEqual(display_shortcut("Control_R"), "Ctrl")
        self.assertEqual(display_shortcut(""), "未设置")

    def test_normalizes_modifier_order_and_aliases(self):
        self.assertEqual(normalize_shortcut("shift+ctrl+f5"), "Control+Shift+F5")
        self.assertEqual(normalize_shortcut("Alt+Ctrl+r"), "Control+Alt+R")

    def test_rejects_editing_and_cancel_shortcuts(self):
        for binding in ("Escape", "Ctrl+C", "Ctrl+V", "Ctrl+X", "Ctrl+A", "Ctrl+Z", "Ctrl+Shift+Z", "Command+C", "Tab", "Return", "Left", "Alt+F4"):
            with self.subTest(binding=binding), self.assertRaises(ShortcutError):
                normalize_shortcut(binding)

    def test_rejects_printing_keys_and_invalid_combinations(self):
        for binding in ("r", "F", "3", "Shift+R", "F0", "F25", "Ctrl+Ctrl+R", "R+Ctrl", "Ctrl+", "Ctrl+Shift"):
            with self.subTest(binding=binding), self.assertRaises(ShortcutError):
                normalize_shortcut(binding)

    def test_rejects_duplicates_but_allows_unassigned(self):
        with self.assertRaises(ShortcutError):
            validate_shortcuts({"mic": "F2", "replace": "F2"})
        self.assertEqual(validate_shortcuts({"mic": "", "replace": ""})["mic"], "")

    def test_clear_requires_deliberate_non_printing_shortcut(self):
        for binding in ("space", "Ctrl", "Alt", "Shift", "Command", "Ctrl+space", "Alt+space"):
            with self.subTest(binding=binding), self.assertRaises(ShortcutError):
                validate_shortcuts({"clear": binding})
        for binding in ("F9", "Ctrl+Shift+K", "Command+K", "Alt+K", ""):
            with self.subTest(binding=binding):
                self.assertEqual(validate_shortcuts({"clear": binding})["clear"], normalize_shortcut(binding))

    def test_clear_shortcut_participates_in_conflict_check(self):
        with self.assertRaisesRegex(ShortcutError, "清空编辑区"):
            validate_shortcuts({"mic": "F8", "clear": "F8"})

    def test_legacy_three_action_config_adds_clear_default(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = {"mic": "F7", "replace": "Ctrl+Alt+R", "rewrite": "F6"}
            (root / SETTINGS_FILE).write_text(json.dumps(legacy), encoding="utf-8")
            loaded = load_shortcuts(root)
            self.assertEqual(loaded, {
                **DEFAULT_SHORTCUTS,
                **{action: normalize_shortcut(binding) for action, binding in legacy.items()},
                "focus": "",
                "global_rewrite": "",
            })
            self.assertEqual(json.loads((root / SETTINGS_FILE).read_text(encoding="utf-8")), legacy)

    def test_legacy_config_keeps_existing_f8_binding(self):
        loaded = validate_shortcuts({"mic": "F2", "replace": "space", "rewrite": "F8"})
        self.assertEqual(loaded["rewrite"], "F8")
        self.assertEqual(loaded["clear"], "")

    def test_legacy_config_keeps_bindings_on_new_defaults(self):
        legacy = {"mic": "F6", "replace": "F9", "rewrite": "F10", "clear": "F8"}
        loaded = validate_shortcuts(legacy)
        self.assertEqual({action: loaded[action] for action in legacy}, legacy)
        for action in ("focus", "cursor_prev", "cursor_next"):
            self.assertEqual(loaded[action], "")

    def test_pointer_actions_reject_bare_recording_keys(self):
        for action in ("focus", "cursor_prev", "cursor_next"):
            for binding in ("space", "Control", "Alt", "Shift", "Command"):
                with self.subTest(action=action, binding=binding), self.assertRaises(ShortcutError):
                    validate_shortcuts({action: binding})
            for binding in ("F11", "Ctrl+Shift+K", "Ctrl+space", ""):
                with self.subTest(action=action, binding=binding):
                    self.assertEqual(validate_shortcuts({action: binding})[action], normalize_shortcut(binding))

    def test_explicit_pointer_shortcuts_still_reject_duplicates(self):
        with self.assertRaises(ShortcutError):
            validate_shortcuts({"focus": "F11", "cursor_next": "F11"})

    def test_both_control_keys_match_default(self):
        for key in ("Control_L", "Control_R"):
            self.assertTrue(matches_event("Control", event(key, 4), "win32"))
            self.assertTrue(released_event("Control", event(key)))
        self.assertFalse(matches_event("Control", event("Control_L", 1 | 4), "win32"))

    def test_space_does_not_hijack_modified_space(self):
        self.assertTrue(matches_event("space", event("space"), "win32"))
        for state in (4, 8, 0x20000, 1):
            self.assertFalse(matches_event("space", event("space", state), "win32"))

    def test_combination_capture_and_exact_matching(self):
        self.assertEqual(shortcut_from_event(event("r", 4 | 0x20000), "win32"), "Control+Alt+R")
        self.assertTrue(matches_event("Ctrl+Alt+R", event("r", 4 | 0x20000), "win32"))
        self.assertFalse(matches_event("Ctrl+Alt+R", event("r", 4), "win32"))
        self.assertEqual(shortcut_from_event(event("F6", 1 | 4), "win32"), "Control+Shift+F6")

    def test_mac_command_and_option_capture(self):
        self.assertEqual(shortcut_from_event(event("r", 8 | 16), "darwin"), "Alt+Command+R")
        self.assertTrue(matches_event("Command+R", event("r", 8), "darwin"))
        self.assertFalse(matches_event("Alt+R", event("r", 8), "darwin"))

    def test_release_primary_or_required_modifier_ends_hold(self):
        for key in ("r", "R", "Control_L", "Control_R", "Alt_L", "Alt_R"):
            self.assertTrue(released_event("Ctrl+Alt+R", event(key)))
        self.assertFalse(released_event("Ctrl+Alt+R", event("Shift_L")))
        self.assertFalse(released_event("Ctrl+Alt+R", event("F5")))

    def test_unsupported_event_and_disabled_shortcut_never_match(self):
        self.assertFalse(matches_event("F2", event("aacute")))
        self.assertFalse(matches_event("", event("F2")))
        self.assertFalse(released_event("", event("F2")))

    def test_file_roundtrip_and_default_when_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(load_shortcuts(root), DEFAULT_SHORTCUTS)
            expected = save_shortcuts(root, {"mic": "F7", "replace": "Ctrl+Alt+R", "rewrite": "F6", "clear": "Ctrl+Shift+K"})
            self.assertEqual(load_shortcuts(root), expected)
            saved = json.loads((root / SETTINGS_FILE).read_text(encoding="utf-8"))
            self.assertEqual(set(saved), set(DEFAULT_SHORTCUTS))
            self.assertEqual(list(root.glob("*.tmp")), [])

    def test_invalid_config_does_not_replace_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            save_shortcuts(root, DEFAULT_SHORTCUTS)
            original = (root / SETTINGS_FILE).read_bytes()
            with self.assertRaises(ShortcutError):
                save_shortcuts(root, {"mic": "Ctrl+V"})
            self.assertEqual((root / SETTINGS_FILE).read_bytes(), original)

    def test_corrupt_or_oversized_config_has_chinese_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for data in ("not json", '[]', '{"unknown":"F2"}', " " * 16385):
                (root / SETTINGS_FILE).write_text(data, encoding="utf-8")
                with self.subTest(data=data[:20]), self.assertRaises(ShortcutError):
                    load_shortcuts(root)


if __name__ == "__main__":
    unittest.main()
