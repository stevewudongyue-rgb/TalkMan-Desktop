"""离线图标与字体资源；字体缺失时保持系统中文字体可用。"""
from __future__ import annotations

import ctypes
import sys
import tkinter as tk
import tkinter.font as tkfont
from dataclasses import dataclass
from pathlib import Path


ICON_NAMES = ("mic", "square", "undo-2", "redo-2", "copy", "save", "settings", "sun", "moon", "trash-2", "eraser", "brain", "clipboard-paste", "languages", "bookmark-plus", "check", "delete")
_registered_fonts: set[Path] = set()


@dataclass(frozen=True)
class FontSet:
    ui: str
    heading: str
    mono: str


def asset_dir() -> Path:
    return Path(__file__).resolve().parent / "TalkMan-assets"


def load_icons(master: tk.Misc, tone: str = "white") -> dict[str, tk.PhotoImage]:
    """调用方保留返回字典；文件缺失时返回可用图标，不阻止编辑器启动。"""
    if tone not in ("black", "white", "red"):
        raise ValueError("图标颜色仅支持 black、white 或 red。")
    icons: dict[str, tk.PhotoImage] = {}
    for name in ICON_NAMES:
        path = asset_dir() / "icons" / f"{name}-{tone}.png"
        if path.is_file():
            try:
                icons[name] = tk.PhotoImage(master=master, file=str(path))
            except tk.TclError:
                continue
    # 瑞士外观使用不等宽网格标记，完全离线绘制。
    swiss = tk.PhotoImage(master=master, width=24, height=24)
    ink = {'black': '#1c1917', 'white': '#fafaf9', 'red': '#c8102e'}[tone]
    for rect in ((3, 3, 10, 10), (12, 3, 21, 10), (3, 12, 10, 21), (12, 12, 21, 15), (12, 17, 21, 21)):
        swiss.put(ink, to=rect)
    icons['swiss'] = swiss
    return icons


def _register_windows_fonts() -> None:
    if sys.platform != "win32":
        return
    try:
        add_font = ctypes.WinDLL("gdi32", use_last_error=True).AddFontResourceExW
    except (AttributeError, OSError):
        return
    add_font.argtypes = [ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_void_p]
    add_font.restype = ctypes.c_int
    for path in (asset_dir() / "fonts").glob("*.ttf"):
        if path not in _registered_fonts and add_font(str(path), 0x10, None) > 0:
            _registered_fonts.add(path)


def load_fonts(master: tk.Misc) -> FontSet:
    """Space 字体仅用于英文品牌/数字；中文正文采用原生字体。"""
    _register_windows_fonts()
    available = set(tkfont.families(master))
    default = tkfont.nametofont("TkDefaultFont", root=master).actual("family")
    def choose(*candidates: str) -> str:
        return next((name for name in candidates if name in available), default)
    if sys.platform == "darwin":
        ui = choose("PingFang SC", "Heiti SC", "Arial Unicode MS")
        heading = choose("Space Grotesk", "Helvetica Neue", "Arial")
        mono = choose("Space Mono", "SF Mono", "Menlo", "Monaco")
    else:
        ui = choose("Microsoft YaHei UI", "Microsoft YaHei", "Noto Sans CJK SC", "DejaVu Sans")
        heading = choose("Space Grotesk", "Segoe UI", "Arial")
        mono = choose("Space Mono", "Cascadia Mono", "Consolas", "DejaVu Sans Mono")
    return FontSet(ui=ui, heading=heading, mono=mono)
