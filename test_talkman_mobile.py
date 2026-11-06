"""手机协议回归仅使用假模型和假目标。"""
from dataclasses import dataclass
import hashlib
import http.client
import json
from pathlib import Path
import ssl
import tempfile
import uuid
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from talkman_mobile_protocol import APIError, CompanionHost, CompanionService
from talkman_mobile_bridge import DesktopEngine, private_addresses
from talkman_asr import ASRSettings
from talkman_qwen import QwenSettings, TextEdit, TextMove
from talkman_version import VERSION, BUILD


def request_id():
    return str(uuid.uuid4())


@dataclass
class Translation:
    text: str


class Engine:
    def __init__(self):
        self.calls = []

    def status(self):
        return {"provider_label": "测试语音", "text_provider": "测试文字",
                "recording_seconds": 55,
                "capabilities": ["organize_v2", "selection_move", "selection_translate"]}

    def transform(self, text, instruction):
        self.calls.append(("cleanup", instruction))
        return text

    def edit_selection(self, text, start, end, instruction):
        self.calls.append(("edit", text[start:end]))
        if "移动" in instruction:
            return TextMove(start, end, len(text))
        return TextEdit(start, end, "梨")

    def translate_selection(self, text, start, end, language):
        self.calls.append(("translate", text[start:end], language))
        return Translation(text[:start] + "pear" + text[end:])

    def safe_error(self, error):
        return "测试引擎错误"


class Targets:
    def __init__(self):
        self.calls = []

    def refresh(self):
        return ["target"]

    def label(self, target):
        return target

    def fill(self, target, text):
        self.calls.append((target, text))
        return type("Receipt", (), {"send_token": "verified", "message": "已填入，未发送"})()

    def close(self):
        pass


class MobileServiceTests(unittest.TestCase):
    def setUp(self):
        self.engine = Engine()
        self.targets = Targets()
        self.service = CompanionService(self.engine, self.targets)

    def test_status_reports_real_provider_and_capabilities(self):
        result = self.service.call("GET", "/v1/status")
        self.assertEqual(result["text_provider"], "测试文字")
        self.assertIn("selection_translate", result["capabilities"])
        self.assertEqual(result["app_version"], "")
        self.assertEqual(result["app_build"], 0)

    def test_selection_edit_and_move_preserve_other_text(self):
        source = "甲😀苹果乙"
        payload = {"request_id": request_id(), "text": source,
                   "selection_start": 3, "selection_length": 2,
                   "instruction": "改成梨"}
        result = self.service.call("POST", "/v1/edit", payload)
        self.assertEqual(result["text"], "甲😀梨乙")
        self.assertEqual((result["selection_start"], result["selection_length"]), (3, 1))
        payload.update(request_id=request_id(), instruction="移动到最后")
        moved = self.service.call("POST", "/v1/edit", payload)
        self.assertEqual(moved["text"], "甲😀乙苹果")
        self.assertEqual((moved["selection_start"], moved["selection_length"]), (4, 2))

    def test_translation_only_sends_selection_and_returns_utf16_range(self):
        source = "😀苹果！"
        result = self.service.call("POST", "/v1/translate", {
            "request_id": request_id(), "text": source,
            "selection_start": 2, "selection_length": 2, "language": "英语"})
        self.assertEqual(self.engine.calls, [("translate", "苹果", "英语")])
        self.assertEqual(result["text"], "😀pear！")
        self.assertEqual((result["selection_start"], result["selection_length"]), (2, 4))

    def test_translation_rejects_invalid_ranges_and_cross_selection_result(self):
        payload = {"request_id": request_id(), "text": "😀苹果！",
                   "selection_start": 1, "selection_length": 2, "language": "英语"}
        with self.assertRaises(APIError):
            self.service.call("POST", "/v1/translate", payload)
        payload["selection_start"], payload["selection_length"] = 2, 2
        self.engine.translate_selection = lambda *_: Translation("删除了选区外的内容")
        with self.assertRaises(APIError):
            self.service.call("POST", "/v1/translate", payload)

    def test_cleanup_advanced_flags_and_replay_protection(self):
        result = self.service.call("POST", "/v1/cleanup", {
            "request_id": request_id(), "text": "有几句话。",
            "remove_fillers": True, "punctuate": True, "rewrite": True, "layout": True})
        self.assertEqual(result["text"], "有几句话。")
        instruction = self.engine.calls[-1][1]
        self.assertIn("语序", instruction)
        self.assertIn("另起一行", instruction)
        target_id = self.service.call("GET", "/v1/targets")["targets"][0]["id"]
        payload = {"request_id": request_id(), "target_id": target_id, "text": "待填入"}
        self.assertTrue(self.service.call("POST", "/v1/fill", payload)["verified"])
        with self.assertRaises(APIError):
            self.service.call("POST", "/v1/fill", payload)
        self.assertEqual(self.targets.calls, [("target", "待填入")])

    def test_fill_without_readback_is_not_marked_verified(self):
        self.targets.fill = lambda target, text: type("Receipt", (), {
            "send_token": "", "message": "已填入，但无法核对目标正文；请在目标内手动发送。"})()
        target_id = self.service.call("GET", "/v1/targets")["targets"][0]["id"]
        result = self.service.call("POST", "/v1/fill", {
            "request_id": request_id(), "target_id": target_id, "text": "待填入"})
        self.assertFalse(result["verified"])
        self.assertIn("无法核对", result["message"])

    def test_tls_pairing_and_authentication(self):
        host = CompanionHost(self.service, "127.0.0.1", 0)
        try:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            connection = http.client.HTTPSConnection("127.0.0.1", host.port, context=context, timeout=5)
            connection.connect()
            certificate = connection.sock.getpeercert(binary_form=True)
            self.assertEqual(hashlib.sha256(certificate).hexdigest(), host.fingerprint)
            connection.request("GET", "/v1/status", headers={"Authorization": "Bearer " + self.service.token})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read())["text_provider"], "测试文字")
            connection.close()
            unauthenticated = http.client.HTTPSConnection("127.0.0.1", host.port, context=context, timeout=5)
            unauthenticated.request("GET", "/v1/status")
            self.assertEqual(unauthenticated.getresponse().status, 401)
            unauthenticated.close()
        finally:
            host.close()


class DesktopEngineTests(unittest.TestCase):
    def test_address_list_matches_ios_pairing_rules(self):
        addresses = ["127.0.0.1", "169.254.3.4", "172.32.0.1", "172.31.0.1", "10.0.0.2", "192.168.1.2"]
        with patch("talkman_mobile_bridge.local_addresses", return_value=addresses):
            self.assertEqual(private_addresses(), ["172.31.0.1", "10.0.0.2", "192.168.1.2"])

    def test_uses_desktop_settings_and_translation_plan(self):
        class Client:
            def __init__(self, settings):
                self.settings = settings
            def transcribe(self, wav):
                return "明天开会"
            def transform(self, text, instruction):
                return text
            def edit_selection(self, text, start, end, instruction):
                return TextEdit(start, end, "梨")
        with tempfile.TemporaryDirectory() as directory:
            app = SimpleNamespace(data_dir=Path(directory),
                free_mode_var=SimpleNamespace(get=lambda: False), free_service=None,
                asr_settings=ASRSettings(provider="qwen"),
                settings=QwenSettings(api_key="sk-test-no-network"))
            with patch("talkman_mobile_bridge.QwenClient", Client):
                engine = DesktopEngine(app)
            self.assertEqual(engine.status()["text_provider"], "千问")
            self.assertEqual(engine.status()["app_version"], VERSION)
            self.assertEqual(engine.status()["app_build"], BUILD)
            self.assertEqual(engine.transcribe(b"fake-audio"), "明天开会")
            self.assertEqual(engine.edit_selection("苹果", 0, 2, "改成梨").text, "梨")
            self.assertEqual(engine.translate_selection("苹果", 0, 2, "英语").text, "苹果")


if __name__ == "__main__":
    unittest.main()
