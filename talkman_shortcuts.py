"""桌面快捷键：按键捕获、编辑冲突校验与无凭证设置保存。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
import tempfile


DEFAULT_SHORTCUTS = {
    "mic": "F2", "replace": "space", "rewrite": "Control", "clear": "F8",
    "focus": "F6", "cursor_prev": "F9", "cursor_next": "F10",
    "global_record": "Control+Alt+space", "global_rewrite": "Control+Alt+R",
    "global_show": "Control+Alt+T",
}
ACTION_LABELS = {
    "mic": "麦克风", "replace": "录音和覆盖", "rewrite": "指令修改", "clear": "清空编辑区",
    "focus": "聚焦鼠标位置", "cursor_prev": "光标前移", "cursor_next": "光标后移",
    "global_record": "全局录音和覆盖", "global_rewrite": "全局指令修改",
    "global_show": "唤醒并聚焦 TalkMan",
}
SETTINGS_FILE = "shortcuts.json"
_MODIFIERS = ("Control", "Alt", "Shift", "Command")
_ALIASES = {
    "ctrl": "Control", "control": "Control", "control_l": "Control", "control_r": "Control",
    "alt": "Alt", "alt_l": "Alt", "alt_r": "Alt", "option": "Alt",
    "shift": "Shift", "shift_l": "Shift", "shift_r": "Shift",
    "cmd": "Command", "command": "Command", "meta_l": "Command", "meta_r": "Command",
    "super_l": "Command", "super_r": "Command", "command_l": "Command", "command_r": "Command",
    "space": "space", "空格": "space", "esc": "Escape", "escape": "Escape",
    "enter": "Return", "return": "Return", "回车": "Return",
    "backspace": "BackSpace", "delete": "Delete", "tab": "Tab", "iso_left_tab": "Tab",
    "left": "Left", "right": "Right", "up": "Up", "down": "Down",
    "home": "Home", "end": "End", "prior": "Prior", "next": "Next",
}
_EDIT_KEYS = {"A", "C", "V", "X", "Z", "Y", "BackSpace", "Delete", "Left", "Right", "Up", "Down", "Home", "End"}
_BARE_KEYS = {"space", "Control", "Alt", "Shift", "Command"}
_DISPLAY = {"space": "空格", "Control": "Ctrl", "Command": "Command", "Alt": "Alt", "Shift": "Shift"}


class ShortcutError(ValueError):
    pass


def _key(value: str) -> str:
    value = value.strip()
    if value.lower() in _ALIASES:
        return _ALIASES[value.lower()]
    if re.fullmatch(r"[fF](?:[1-9]|1[0-9]|2[0-4])", value):
        return value.upper()
    if re.fullmatch(r"[a-zA-Z0-9]", value):
        return value.upper()
    raise ShortcutError("请选择功能键、空格、修饰键或修饰键与字母的组合。")


def normalize_shortcut(value: str) -> str:
    if not isinstance(value, str) or len(value) > 80:
        raise ShortcutError("快捷键格式无效。")
    if not value.strip():
        return ""
    parts = value.split("+")
    if any(not part.strip() for part in parts):
        raise ShortcutError("快捷键格式无效。")
    keys = [_key(part) for part in parts]
    if len(set(keys)) != len(keys):
        raise ShortcutError("一个快捷键不能包含重复按键。")
    primary = keys[-1]
    modifiers = keys[:-1]
    if any(key not in _MODIFIERS for key in modifiers) or (modifiers and primary in _MODIFIERS):
        raise ShortcutError("组合键需以字母、数字、空格或功能键结尾。")
    if primary == "Escape":
        raise ShortcutError("Esc 已用于取消操作，请选择其他快捷键。")
    if primary in {"Return", "Tab", "BackSpace", "Delete", "Left", "Right", "Up", "Down", "Home", "End", "Prior", "Next"}:
        raise ShortcutError("此按键用于编辑文字或移动光标，请选择其他快捷键。")
    if not modifiers and primary not in _BARE_KEYS and not re.fullmatch(r"F(?:[1-9]|1[0-9]|2[0-4])", primary):
        raise ShortcutError("字母和数字需搭配 Ctrl、Alt、Shift 或 Command，避免影响打字。")
    if modifiers == ["Shift"] and (len(primary) == 1 or primary == "space"):
        raise ShortcutError("Shift 与字母、数字或空格的组合会影响正常输入。")
    if any(modifier in modifiers for modifier in ("Control", "Command")) and primary in _EDIT_KEYS:
        raise ShortcutError("此组合已用于全选、复制、粘贴或撤销等文字编辑，请选择其他快捷键。")
    if ("Alt" in modifiers and primary == "F4") or ("Command" in modifiers and primary in {"Q", "W", "H", "M"}):
        raise ShortcutError("此组合用于关闭或切换系统窗口，请选择其他快捷键。")
    return "+".join([modifier for modifier in _MODIFIERS if modifier in modifiers] + [primary])


def display_shortcut(value: str) -> str:
    normalized = normalize_shortcut(value)
    return " + ".join(_DISPLAY.get(part, part) for part in normalized.split("+")) if normalized else "未设置"


def _event_modifiers(event, platform: str | None = None) -> list[str]:
    state = int(getattr(event, "state", 0))
    platform = platform or sys.platform
    modifiers = []
    if state & 4:
        modifiers.append("Control")
    if state & (16 if platform == "darwin" else (8 | 0x20000)):
        modifiers.append("Alt")
    if state & 1:
        modifiers.append("Shift")
    if platform == "darwin" and state & 8:
        modifiers.append("Command")
    return modifiers


def shortcut_from_event(event, platform: str | None = None) -> str:
    primary = _key(str(getattr(event, "keysym", "")))
    if primary in _MODIFIERS:
        return normalize_shortcut(primary)
    return normalize_shortcut("+".join(_event_modifiers(event, platform) + [primary]))


def matches_event(value: str, event, platform: str | None = None) -> bool:
    normalized = normalize_shortcut(value)
    if not normalized:
        return False
    try:
        if normalized in _MODIFIERS:
            if any(modifier != normalized for modifier in _event_modifiers(event, platform)):
                return False
        return shortcut_from_event(event, platform) == normalized
    except ShortcutError:
        return False


def released_event(value: str, event) -> bool:
    normalized = normalize_shortcut(value)
    if not normalized:
        return False
    try:
        released = _key(str(getattr(event, "keysym", "")))
    except ShortcutError:
        return False
    return released in normalized.split("+")


def validate_shortcuts(mapping: dict[str, str]) -> dict[str, str]:
    if not isinstance(mapping, dict) or any(action not in DEFAULT_SHORTCUTS for action in mapping):
        raise ShortcutError("快捷键设置格式无效。")
    explicit = {action: normalize_shortcut(binding) for action, binding in mapping.items()}
    assigned_defaults = set(explicit.values()) - {""}
    result = {}
    # 升级时优先保留原绑定，冲突的新默认键留空等待用户指定。
    for action, default in DEFAULT_SHORTCUTS.items():
        if action in explicit:
            result[action] = explicit[action]
        else:
            result[action] = "" if default in assigned_defaults else default
            if result[action]:
                assigned_defaults.add(result[action])
    assigned = {}
    for action, binding in result.items():
        if action.startswith("global_") and binding:
            parts = binding.split("+")
            if not set(parts[:-1]).intersection({"Control", "Alt", "Command"}):
                raise ShortcutError("全局快捷键需搭配 Ctrl、Alt 或 Command，避免在其他窗口打字时误录音。")
            if sys.platform == "darwin" and parts[-1] in {"F21", "F22", "F23", "F24"}:
                raise ShortcutError("Mac 全局快捷键最多支持到 F20。")
        if action == "clear" and binding and binding.split("+")[-1] in _BARE_KEYS:
            raise ShortcutError("删除选区请使用功能键或字母、数字组合键，不能使用空格或单独的修饰键。")
        if action in {"focus", "cursor_prev", "cursor_next"} and binding in _BARE_KEYS:
            raise ShortcutError(f"{ACTION_LABELS[action]}请使用功能键或组合键，不能使用空格或单独的修饰键。")
        if binding and binding in assigned:
            raise ShortcutError(f"「{ACTION_LABELS[action]}」与「{ACTION_LABELS[assigned[binding]]}」不能使用同一快捷键。")
        if binding:
            assigned[binding] = action
    return result


def load_shortcuts(data_dir: Path) -> dict[str, str]:
    path = Path(data_dir) / SETTINGS_FILE
    if not path.exists():
        return DEFAULT_SHORTCUTS.copy()
    try:
        if path.stat().st_size > 16384:
            raise ShortcutError("快捷键设置文件过大，请重新设置。")
        return validate_shortcuts(json.loads(path.read_text(encoding="utf-8")))
    except ShortcutError:
        raise
    except (OSError, ValueError, UnicodeError, TypeError):
        raise ShortcutError("快捷键设置未能读取，请重新设置。") from None


def save_shortcuts(data_dir: Path, mapping: dict[str, str]) -> dict[str, str]:
    result = validate_shortcuts(mapping)
    directory = Path(data_dir)
    temporary = None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=directory, suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(result, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, directory / SETTINGS_FILE)
        temporary = None
        return result
    except OSError:
        raise ShortcutError("快捷键设置保存失败，请检查数据目录。") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
