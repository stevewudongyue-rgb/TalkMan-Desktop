#!/usr/bin/env python3
"""在 Apple Silicon Mac 上构建 Talk Man；Windows 仅可执行静态检查。"""
from __future__ import annotations

import argparse
import ast
import json
import platform
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
from talkman_version import PRODUCT_NAME, VERSION, BUILD as VERSION_BUILD
BUILD = ROOT / "TalkMan-build-mac"
DIST = ROOT / "dist-mac"
VENV = ROOT / "TalkMan-venv-mac"
PYINSTALLER_VERSION = "6.22.3"
SOURCE_FILES = ("talkman_app.py", "talkman_version.py", "talkman_updates.py", "talkman_update_ui.py", "talkman_license.py", "talkman_license_ui.py", "talkman_free_service.py", "talkman_system_style.py", "talkman_qwen.py", "talkman_voice.py", "talkman_asr.py",
                "talkman_asr_volc_baidu.py", "talkman_asr_tencent_xunfei.py", "talkman_assets.py",
                "talkman_shortcuts.py", "talkman_magnifier.py", "talkman_target.py",
                "talkman_target_windows.py", "talkman_target_macos.py",
                "talkman_settings_ui.py", "talkman_settings_viewport.py", "talkman_ui_widgets.py", "talkman_credentials.py", "talkman_editor.py",
                "talkman_macos_setup.py", "talkman_records.py", "talkman_window.py", "talkman_global_hotkeys.py",
                "talkman_target_picker.py", "talkman_sync.py", "talkman_live_sync.py",
                "talkman_input_positions.py", "talkman_position_picker.py", "talkman_position_windows.py", "talkman_position_macos.py",
                "talkman_commands.py", "talkman_command_ui.py", "talkman_command_intent.py",
                "talkman_onboarding.py", "talkman_hover_help.py", "talkman_ai_edit.py",
                "talkman_mobile_protocol.py", "talkman_mobile_bridge.py",
                "talkman_conventions.py", "talkman_conventions_ui.py", "talkman_convention_learning.py", "talkman_delivery_ui.py", "talkman_translation.py", "talkman_translation_ui.py")
INFO_PLIST = {
    "CFBundleDisplayName": PRODUCT_NAME,
    "CFBundleShortVersionString": VERSION,
    "CFBundleVersion": str(VERSION_BUILD),
    "LSMinimumSystemVersion": "14.0",
    "NSHighResolutionCapable": True,
    "NSMicrophoneUsageDescription": "Talk Man 使用麦克风录制你主动输入的语音，并发送到已配置的模型服务转换为文字。",
}


def run(args: list[str | Path], *, cwd: Path = ROOT) -> None:
    subprocess.run([str(arg) for arg in args], cwd=cwd, check=True)


def spec_text(sign_identity: str | None = None) -> str:
    entitlements = str(ROOT / "TalkMan-Mac.entitlements") if sign_identity else None
    return f'''# 由 TalkMan-构建Mac.py 生成；只在原生 arm64 Python 中使用。
from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules
audio_data, audio_binaries, audio_hidden = collect_all("_sounddevice_data")
mac_hidden = []
for framework in ("Cocoa", "AppKit", "Foundation", "CoreFoundation", "CoreText",
                  "ApplicationServices", "HIServices", "Quartz", "objc", "AVFoundation",
                  "AVFAudio", "CoreAudio", "CoreMedia", "Security"):
    mac_hidden.extend(collect_submodules(framework))
a = Analysis(
    [{str(ROOT / "talkman_app.py")!r}],
    pathex=[{str(ROOT)!r}],
    binaries=audio_binaries,
    datas=audio_data + collect_data_files("certifi") + [({str(ROOT / "TalkMan-assets")!r}, "TalkMan-assets")],
    hiddenimports=audio_hidden + mac_hidden + ["_cffi_backend", "sounddevice", "websocket", "certifi"],
    hookspath=[],
    runtime_hooks=[{str(ROOT / "TalkMan-证书钩子.py")!r}],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True,
    name="TalkMan", debug=False, bootloader_ignore_signals=False,
    strip=False, upx=False, console=False,
    target_arch="arm64", codesign_identity={sign_identity!r}, entitlements_file={entitlements!r},
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="TalkMan")
app = BUNDLE(
    coll, name="TalkMan.app", icon={str(ROOT / 'TalkMan-assets/app/TalkMan.icns')!r},
    bundle_identifier="com.cololight.talkman",
    info_plist={INFO_PLIST!r},
)
'''


def static_check() -> None:
    """只检查源码、构建配置；不将此结果表述为 Mac 运行验证。"""
    for name in SOURCE_FILES:
        source = ROOT / name
        if not source.is_file():
            raise RuntimeError(f"源码缺失：{name}；请先完整解压源码包。")
        ast.parse(source.read_text(encoding="utf-8-sig"), filename=name)
    requirement_file = ROOT / "TalkMan-requirements.txt"
    if not requirement_file.is_file():
        raise RuntimeError("缺少 TalkMan-requirements.txt。")
    ast.parse(spec_text(), filename="TalkMan-Mac.spec")
    hook = ROOT / "TalkMan-证书钩子.py"
    if not hook.is_file():
        raise RuntimeError("缺少 TalkMan-证书钩子.py。")
    ast.parse(hook.read_text(encoding="utf-8-sig"), filename=hook.name)
    plistlib.dumps(INFO_PLIST)
    if not INFO_PLIST.get("NSMicrophoneUsageDescription"):
        raise RuntimeError("缺少 Mac 麦克风用途说明。")
    with (ROOT / "TalkMan-Mac.entitlements").open("rb") as stream:
        entitlements = plistlib.load(stream)
    if entitlements != {"com.apple.security.device.audio-input": True}:
        raise RuntimeError("签名权限文件应只包含麦克风录音能力。")
    ast.parse(spec_text("Developer ID Application: 检查用 (XXXXXXXXXX)"))
    launcher = ROOT / "TalkMan-构建Mac.command"
    if not launcher.is_file() or b"\r\n" in launcher.read_bytes():
        raise RuntimeError("Mac 启动脚本缺失或使用了 Windows 换行，请重新解压源码包。")
    print("静态检查通过：源码语法、Mac 构建配置、麦克风用途说明与启动脚本完整。")
    print("这不代表已在 Mac 构建或验证运行。")


def require_native_mac() -> None:
    if sys.platform != "darwin":
        raise RuntimeError("生成 .app 必须在 Mac 上执行；Windows 只能用 --check 检查构建准备情况。")
    translated = subprocess.run(
        ["/usr/sbin/sysctl", "-in", "sysctl.proc_translated"],
        text=True, capture_output=True, check=False,
    )
    if platform.machine() != "arm64" or translated.stdout.strip() == "1":
        raise RuntimeError("请使用 Apple Silicon 的原生 arm64 Python，关闭终端“使用 Rosetta 打开”后重试。")
    if not (3, 12) <= sys.version_info[:2] < (3, 14):
        raise RuntimeError("本构建脚本支持 Python 3.12 或 3.13；请安装 python.org 的 macOS universal2 版本。")
    if not shutil.which("ditto"):
        raise RuntimeError("未找到 macOS 的 ditto 工具。")
    try:
        import tkinter as tk
        probe = tk.Tk()
        probe.withdraw()
        probe.update_idletasks()
        probe.destroy()
    except Exception as exc:
        raise RuntimeError("当前 Python 缺少可用的 Tk 图形环境；请安装 python.org 的 macOS Python 3.12 或 3.13，并在桌面终端构建。") from exc


def validate_signing(sign_identity: str | None, notary_profile: str | None) -> None:
    if notary_profile and not sign_identity:
        raise RuntimeError("公证必须同时指定 --sign-identity，使用 Developer ID Application 证书。")
    if sign_identity and not sign_identity.startswith("Developer ID Application: "):
        raise RuntimeError("分发必须使用 Developer ID Application 证书；免费 Apple Development 证书不适用。")


def signing_preflight(sign_identity: str | None, notary_profile: str | None) -> None:
    validate_signing(sign_identity, notary_profile)
    if sign_identity:
        result = subprocess.run(["/usr/bin/security", "find-identity", "-v", "-p", "codesigning"],
                                text=True, capture_output=True, check=True)
        if f'"{sign_identity}"' not in result.stdout:
            raise RuntimeError("本机钥匙串未找到带私钥的签名身份，请先在此 Mac 创建或安装 Developer ID Application 证书。")
    if notary_profile:
        for tool in ("notarytool", "stapler"):
            run(["/usr/bin/xcrun", "--find", tool])


def notarize(path: Path, profile: str) -> None:
    # profile 只是本机钥匙串条目名称；脚本不读取、打印或保存 Apple 密码。
    result = subprocess.run(["/usr/bin/xcrun", "notarytool", "submit", str(path),
                             "--keychain-profile", profile, "--wait", "--output-format", "json"],
                            text=True, capture_output=True, check=False)
    try:
        reply = json.loads(result.stdout)
        if not isinstance(reply, dict):
            raise ValueError("公证结果必须是对象")
    except (ValueError, TypeError):
        raise RuntimeError("未取得公证结果。请检查 Xcode 工具、网络及本机公证凭证，再重试。") from None
    if result.returncode or reply.get("status") != "Accepted":
        raise RuntimeError(f"Apple 公证未通过：{reply.get('status', '未知')}；任务号 {reply.get('id', '未知')}。请用 notarytool log 查看原因，勿分发此构建。")


def make_disk_image(app: Path, *, test_build: bool = False) -> Path:
    stage = BUILD / ("安装镜像-测试" if test_build else "安装镜像")
    stage.mkdir(exist_ok=True)
    staged_app = stage / "TalkMan.app"
    if staged_app.exists():
        shutil.rmtree(staged_app)
    run(["/usr/bin/ditto", app, staged_app])
    shortcut = stage / "Applications"
    if not shortcut.is_symlink() and not shortcut.exists():
        shortcut.symlink_to("/Applications", target_is_directory=True)
    instructions = (
        "把 TalkMan.app 拖到 Applications，再从“应用程序”打开。\n"
        "无须安装 Python。首次录音允许麦克风；需要填入 Agent 时再开启辅助功能。\n"
        "在设置的“Mac 授权”页可检测权限并直接打开系统设置。\n")
    if test_build:
        instructions += (
            "\n这是测试版，未使用 Developer ID 签名、未完成 Apple 公证。\n"
            "若被系统阻止，请先尝试打开，再到“系统设置 → 隐私与安全性”查看“仍要打开”。\n"
            "仅对你信任的来源使用此功能；无需关闭系统安全保护。\n")
    (stage / "安装说明.txt").write_text(instructions, encoding="utf-8")
    image = DIST / ("TalkMan-Mac-AppleSilicon-测试版.dmg" if test_build else "TalkMan-Mac-AppleSilicon.dmg")
    run(["/usr/bin/hdiutil", "create", "-volname", "Talk Man", "-srcfolder", stage,
         "-ov", "-format", "UDZO", image])
    run(["/usr/bin/hdiutil", "verify", image])
    return image


def make_notarized_distribution(app: Path, archive: Path, sign_identity: str, profile: str) -> Path:
    upload = DIST / "TalkMan-Mac-待公证.zip"
    run(["/usr/bin/ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", app, upload])
    notarize(upload, profile)
    run(["/usr/bin/xcrun", "stapler", "staple", app])
    run(["/usr/bin/xcrun", "stapler", "validate", app])
    run(["/usr/sbin/spctl", "--assess", "--type", "execute", "--verbose=2", app])
    image = make_disk_image(app)
    run(["/usr/bin/codesign", "--force", "--timestamp", "--sign", sign_identity, image])
    notarize(image, profile)
    run(["/usr/bin/xcrun", "stapler", "staple", image])
    run(["/usr/bin/xcrun", "stapler", "validate", image])
    run(["/usr/sbin/spctl", "--assess", "--type", "open", "--context", "context:primary-signature",
         "--verbose=2", image])
    # 公证票据装订后重新压缩，避免把公证前的 ZIP 当成最终分发包。
    run(["/usr/bin/ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", app, archive])
    upload.unlink()
    return image


def build(sign_identity: str | None = None, notary_profile: str | None = None, *, test_dmg: bool = False) -> None:
    if test_dmg and notary_profile:
        raise RuntimeError("--test-dmg 用于未公证测试版；正式公证构建请去掉此参数。")
    require_native_mac()
    static_check()
    signing_preflight(sign_identity, notary_profile)
    BUILD.mkdir(exist_ok=True)
    DIST.mkdir(exist_ok=True)
    print("创建项目专用 Python 环境…", flush=True)
    if not (VENV / "bin" / "python").is_file():
        run([sys.executable, "-m", "venv", VENV])
    python = VENV / "bin" / "python"
    # 每次核验已有环境，避免复用从 Intel/Rosetta 环境复制来的 venv。
    run([python, "-c", "import platform, sys; assert sys.platform == 'darwin' and platform.machine() == 'arm64', '需要原生 arm64 环境'"])
    print("安装构建及语音依赖（首次需要联网）…", flush=True)
    run([python, "-m", "pip", "install", "--disable-pip-version-check",
         "-r", ROOT / "TalkMan-requirements.txt",
         f"pyinstaller=={PYINSTALLER_VERSION}", "certifi>=2025.1.31"])
    run([python, "-c", "import tkinter, sounddevice, websocket, cffi, certifi, AppKit, ApplicationServices, Quartz, AVFoundation, Security; print('图形、语音、目标窗口、钥匙串与证书依赖可导入')"])
    run([python, ROOT / "TalkMan-收集第三方许可.py"])
    spec = BUILD / "TalkMan-Mac.spec"
    spec.write_text(spec_text(sign_identity), encoding="utf-8", newline="\n")
    print("构建 Apple Silicon 应用…", flush=True)
    run([python, "-m", "PyInstaller", "--noconfirm", "--clean",
         "--distpath", DIST, "--workpath", BUILD / "work", spec])
    app = DIST / "TalkMan.app"
    executable = app / "Contents" / "MacOS" / "TalkMan"
    if not executable.is_file():
        raise RuntimeError("构建未生成完整的 TalkMan.app。")
    with (app / "Contents" / "Info.plist").open("rb") as stream:
        actual_info = plistlib.load(stream)
    if actual_info.get("NSMicrophoneUsageDescription") != INFO_PLIST["NSMicrophoneUsageDescription"]:
        raise RuntimeError("应用缺少正确的麦克风权限说明。")
    if sign_identity:
        run(["/usr/bin/codesign", "--verify", "--deep", "--strict", "--verbose=2", app])
    archive = DIST / "TalkMan-Mac-AppleSilicon.zip"
    run(["/usr/bin/ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", app, archive])
    print(f"已生成应用：{app}")
    print(f"已生成分发包：{archive}")
    if notary_profile:
        image = make_notarized_distribution(app, archive, sign_identity, notary_profile)
        print(f"已签名、公证并校验的安装镜像：{image}")
    elif test_dmg:
        image = make_disk_image(app, test_build=True)
        print(f"已生成未公证测试镜像：{image}")
    print("仍需在这台 Mac 实测：启动、麦克风授权、录音识别、选区修改和撤销。")
    if not notary_profile:
        print("此版本未完成 Apple 公证；仅供测试，首次下载打开可能被系统阻止。")
    print("首次使用麦克风需要系统授权；若拒绝，可在系统设置→隐私与安全性→麦克风中调整。")
    print("首次允许后请松开并重新按住录音；授权本身不会开始录音。")
    print("填入 Agent 需要系统设置→隐私与安全性→辅助功能授权；列表没有 TalkMan 时用 + 添加此 TalkMan.app，然后重新打开。")


def main() -> int:
    parser = argparse.ArgumentParser(description="构建 Apple Silicon Mac 版 Talk Man")
    parser.add_argument("--check", action="store_true", help="仅进行可跨平台运行的静态检查")
    parser.add_argument("--sign-identity", help="本机钥匙串中完整的 Developer ID Application 证书名称")
    parser.add_argument("--notary-profile", help="由 notarytool store-credentials 存在本机钥匙串的条目名称")
    parser.add_argument("--test-dmg", action="store_true", help="另生成未公证测试 DMG，无需开发者证书")
    args = parser.parse_args()
    try:
        validate_signing(args.sign_identity, args.notary_profile)
        if args.check:
            static_check()
        else:
            build(args.sign_identity, args.notary_profile, test_dmg=args.test_dmg)
    except (RuntimeError, OSError, SyntaxError, subprocess.CalledProcessError) as exc:
        print(f"构建未完成：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
