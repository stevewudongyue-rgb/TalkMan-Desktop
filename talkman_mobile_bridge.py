"""桌面版复用模型和目标输入框的 iPhone／iPad 局域网连接。"""
from __future__ import annotations

from copy import deepcopy
import ipaddress
from pathlib import Path
import tkinter as tk
from tkinter import ttk

from talkman_asr import PROVIDERS, transcriber, validate
from talkman_conventions import Conventions
from talkman_free_service import FreeClient
from talkman_mobile_protocol import APIError, CompanionHost, CompanionService, local_addresses
from talkman_qwen import QwenClient, QwenError
from talkman_target import TargetController
from talkman_translation import SelectionTranslationPlan, TranslationMemory
from talkman_version import VERSION, BUILD


CAPABILITIES = ["organize_v2", "selection_move", "selection_translate"]


class DesktopEngine:
    def __init__(self, app):
        self.free_mode = app.free_mode_var.get()
        self.free_service = app.free_service
        self.asr_settings = deepcopy(app.asr_settings)
        self.qwen_settings = deepcopy(app.settings)
        self.conventions = Conventions(app.data_dir).snapshot()
        self.translation_memory = TranslationMemory()
        if self.free_mode:
            if not self.free_service.configured:
                raise QwenError("这台电脑尚未开通免费内测，请在桌面版设置中检查连接。")
            self.text_client = FreeClient(self.free_service)
        else:
            validate(self.asr_settings)
            if not self.qwen_settings.api_key:
                raise QwenError("请先在桌面版设置中填写千问文字模型 Key。")
            self.text_client = QwenClient(self.qwen_settings)

    def status(self):
        provider = "free" if self.free_mode else self.asr_settings.provider
        return {"provider_label": PROVIDERS[provider].label,
                "text_provider": "免费内测·星辰" if self.free_mode else "千问",
                "recording_seconds": min(60, PROVIDERS[provider].seconds),
                "app_version": VERSION, "app_build": BUILD,
                "capabilities": CAPABILITIES}

    def transcribe(self, wav):
        if self.free_mode or self.asr_settings.provider == "qwen":
            result = self.text_client.transcribe(wav)
        else:
            result = transcriber(self.asr_settings)(wav)
        return self.conventions.apply(result)

    def transform(self, text, instruction):
        return self.text_client.transform(text, self.conventions.instruction(instruction, text))

    def edit_selection(self, text, start, end, instruction):
        return self.text_client.edit_selection(text, start, end,
            self.conventions.instruction(instruction, text))

    def edit_by_instruction(self, before, after, instruction):
        context = before + after
        return self.text_client.edit_by_instruction(before, after,
            self.conventions.instruction(instruction, context))

    def translate_selection(self, text, start, end, language):
        words = [item["word"] for item in self.conventions.items]
        protected = self.translation_memory.protection(text, words)
        plan = SelectionTranslationPlan(text, language, protected, start, end)
        result = plan.execute(self.text_client)
        self.translation_memory.remember(result.text, result.protected)
        return result

    @staticmethod
    def safe_error(error):
        return str(error) if isinstance(error, (QwenError, APIError)) else "电脑模型处理失败，原文已保留，请检查设置。"


class DesktopTargets:
    def __init__(self, directory):
        # 手机目标使用独立控制器，避免改变桌面编辑器当前选择。
        self.controller = TargetController(Path(directory) / "mobile")

    @staticmethod
    def _await(submit, timeout):
        from queue import Queue, Empty
        result = Queue(maxsize=1)
        submit(lambda state, value: result.put_nowait((state, value)))
        try:
            state, value = result.get(timeout=timeout)
        except Empty:
            raise APIError(504, "电脑操作结果未确认，请检查目标窗口；不要重复填入。") from None
        if state != "ok":
            raise APIError(422, str(value))
        return value

    def refresh(self):
        return self._await(self.controller.refresh, 15)

    @staticmethod
    def label(window):
        return window.display_name()

    def fill(self, target, text):
        self.controller.select(target)
        return self._await(lambda callback: self.controller.fill_with_receipt(text, callback), 25)

    def close(self):
        self.controller.close()


def private_addresses():
    allowed = []
    for address in local_addresses():
        try:
            value = ipaddress.IPv4Address(address)
        except ipaddress.AddressValueError:
            continue
        parts = value.packed
        if (parts[0] == 10 or parts[0] == 172 and 16 <= parts[1] <= 31
                or parts[0] == 192 and parts[1] == 168):
            allowed.append(address)
    return allowed


class MobileConnection:
    def __init__(self, app):
        self.app = app
        self.host = None
        self.status = tk.StringVar(value="手机连接未启用")

    def start(self, address):
        if self.host is not None:
            return self.host.pairing_string
        if address not in private_addresses():
            raise ValueError("请选择当前电脑的私有局域网地址。")
        engine = DesktopEngine(self.app)
        targets = DesktopTargets(self.app.data_dir)
        try:
            service = CompanionService(engine, targets)
            host = CompanionHost(service, address)
        except Exception:
            targets.close()
            raise
        self.host = host
        self.status.set("已启用 · " + engine.status()["provider_label"] + " · 同 Wi-Fi 配对")
        return host.pairing_string

    def stop(self):
        host, self.host = self.host, None
        if host is not None:
            host.close()
        self.status.set("手机连接未启用；重新启用后需重新配对")

    def build_settings(self, parent):
        ttk.Label(parent, text="iPhone／iPad 连接", font=(self.app.fonts.ui, 18)).pack(anchor="w", pady=(0, 10))
        ttk.Label(parent, text="手机与电脑连接同一 Wi-Fi。连接仅在此电脑主动启用时开放；模型 Key 不会传到手机。",
                  style="Muted.TLabel", wraplength=580).pack(anchor="w", pady=(0, 18))
        addresses = private_addresses()
        selected = tk.StringVar(value=addresses[0] if addresses else "")
        ttk.Combobox(parent, textvariable=selected, values=addresses, state="readonly", width=24).pack(anchor="w", pady=(0, 12))
        ttk.Label(parent, textvariable=self.status, style="Muted.TLabel", wraplength=580).pack(anchor="w", pady=(0, 14))

        def enable():
            try:
                pairing = self.start(selected.get())
                self.app.clipboard_clear()
                self.app.clipboard_append(pairing)
                self.status.set("已启用，配对码已复制。请在手机设置中粘贴并连接。")
            except (OSError, ValueError, RuntimeError, QwenError, APIError):
                self.status.set("启用失败。请检查模型设置、局域网地址及 8765 端口。")

        self.app.button(parent, "启用并复制配对码", enable, style="Accent.TButton").pack(anchor="w", pady=(0, 10))
        self.app.button(parent, "停止手机连接", self.stop, style="Ghost.TButton").pack(anchor="w")
