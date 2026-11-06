"""Mac 权限自检和设置入口；不录音、不自动批准、不修改 TCC 数据库。"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import platform
import subprocess
import sys

from talkman_voice import _mac_microphone_permission


SETTINGS_URLS = {
    "microphone": "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone",
    "accessibility": "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
}
MICROPHONE_LABELS = {0: "等待首次授权", 1: "受到系统或公司策略限制", 2: "未允许", 3: "已允许"}


def settings_path(kind: str, version: str | None = None) -> str:
    label = {"microphone": "麦克风", "accessibility": "辅助功能"}[kind]
    version = platform.mac_ver()[0] if version is None else version
    try:
        legacy = int(version.split(".")[0]) < 13
    except ValueError:
        legacy = False
    prefix = "系统偏好设置 → 安全性与隐私 → 隐私" if legacy else "系统设置 → 隐私与安全性"
    return f"{prefix} → {label}"


def running_app_path() -> Path | None:
    if getattr(sys, "frozen", False):
        executable = Path(sys.executable).resolve()
        for parent in executable.parents:
            if parent.suffix == ".app":
                return parent
    return None


@dataclass(frozen=True)
class MacSetupStatus:
    version: str
    microphone: int | None
    accessibility: bool | None
    app: Path | None

    @property
    def microphone_label(self) -> str:
        return MICROPHONE_LABELS.get(self.microphone, "无法检测，请检查应用构建")

    @property
    def accessibility_label(self) -> str:
        return {True: "已允许", False: "未允许"}.get(self.accessibility, "无法检测，请检查应用构建")


def check_permissions() -> MacSetupStatus:
    if sys.platform != "darwin":
        return MacSetupStatus("非 macOS", None, None, None)
    trusted = None
    try:
        import ApplicationServices
        import objc
        with objc.autorelease_pool():
            trusted = bool(ApplicationServices.AXIsProcessTrusted())
    except Exception:
        pass
    return MacSetupStatus(platform.mac_ver()[0] or "未知版本", _mac_microphone_permission(),
                          trusted, running_app_path())


def open_permission_settings(kind: str) -> str:
    """优先直达具体页；URL 接受不等于页面已到达，始终给出手动路径。"""
    if kind not in SETTINGS_URLS:
        raise ValueError("未知的权限类型。")
    if sys.platform != "darwin":
        return "此入口仅用于 Mac。"
    commands = (
        ["/usr/bin/open", SETTINGS_URLS[kind]],
        ["/usr/bin/open", "x-apple.systempreferences:com.apple.preference.security"],
        ["/usr/bin/open", "-b", "com.apple.systempreferences"],
    )
    for command in commands:
        try:
            result = subprocess.run(command, capture_output=True, timeout=5, check=False)
            if result.returncode == 0:
                return f"已请求打开设置；若未直达，请进入「{settings_path(kind)}」。授权后回到这里会自动重新检测。"
        except (OSError, subprocess.TimeoutExpired):
            continue
    return f"无法自动打开设置，请进入「{settings_path(kind)}」。"
