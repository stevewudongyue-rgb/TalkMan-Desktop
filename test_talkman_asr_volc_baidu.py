"""离线验证官方请求协议、录音边界与异常脱敏，不使用真实凭证。"""

from __future__ import annotations

import base64
import io
import json
import socket
import unittest
import wave
from email.message import Message
from urllib import error, parse
from unittest import mock

import talkman_asr_volc_baidu as asr


def make_wav(seconds=0.1, *, rate=16000, channels=1, width=2):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as audio:
        audio.setnchannels(channels)
        audio.setsampwidth(width)
        audio.setframerate(rate)
        audio.writeframes(b"\x01" * (int(seconds * rate) * channels * width))
    return buf.getvalue()


class Response:
    def __init__(self, url, payload, headers=None, *, raw=None, status=200):
        self.url = url
        self.status = status
        self.data = raw if raw is not None else json.dumps(payload).encode("utf-8")
        self.headers = Message()
        for name, value in (headers or {}).items():
            self.headers[name] = value

    def geturl(self):
        return self.url

    def read(self, size):
        return self.data[:size]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.wav = make_wav()

    def request_to(self, response):
        opener = mock.Mock()
        opener.open.return_value = response
        patcher = mock.patch.object(asr.request, "build_opener", return_value=opener)
        patcher.start()
        self.addCleanup(patcher.stop)
        return opener

    def volc_response(self, *, code="20000000", payload=None):
        return Response(asr.VOLC_ENDPOINT,
                        {"result": {"text": " 今天很好。 "}} if payload is None else payload,
                        {"X-Api-Status-Code": code})

    def test_volc_new_key_request_and_result(self):
        opener = self.request_to(self.volc_response())
        self.assertEqual(asr.transcribe_volc(self.wav, {"api_key": "fake-key"}), "今天很好。")
        req = opener.open.call_args.args[0]
        body = json.loads(req.data)
        self.assertEqual(req.full_url, asr.VOLC_ENDPOINT)
        self.assertEqual(req.get_header("X-api-key"), "fake-key")
        self.assertIsNone(req.get_header("X-api-app-key"))
        self.assertEqual(req.get_header("X-api-resource-id"), asr.VOLC_RESOURCE_ID)
        self.assertEqual(req.get_header("X-api-sequence"), "-1")
        self.assertEqual(base64.b64decode(body["audio"]["data"]), self.wav)
        self.assertEqual(body["request"]["model_name"], "bigmodel")
        self.assertEqual(body["request"]["enable_ddc"], False)
        self.assertNotIn("fake-key", json.dumps(body))
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 40)

    def test_volc_legacy_credentials(self):
        opener = self.request_to(self.volc_response())
        asr.transcribe_volc(self.wav, {"app_id": "fake-app", "access_key": "fake-token"})
        req = opener.open.call_args.args[0]
        self.assertEqual(req.get_header("X-api-app-key"), "fake-app")
        self.assertEqual(req.get_header("X-api-access-key"), "fake-token")
        self.assertIsNone(req.get_header("X-api-key"))

    def test_volc_new_key_has_priority(self):
        opener = self.request_to(self.volc_response())
        asr.transcribe_volc(self.wav, {"api_key": "fake-key", "app_id": "ignored", "access_key": "ignored"})
        self.assertIsNone(opener.open.call_args.args[0].get_header("X-api-app-key"))

    def test_volc_missing_credentials_never_connect(self):
        with mock.patch.object(asr.request, "build_opener") as builder:
            with self.assertRaises(asr.ProviderError):
                asr.transcribe_volc(self.wav, {})
            builder.assert_not_called()

    def test_volc_silent_response(self):
        self.request_to(self.volc_response(code="20000003", payload={}))
        with self.assertRaisesRegex(asr.ProviderError, "有效人声"):
            asr.transcribe_volc(self.wav, {"api_key": "fake"})

    def test_volc_rejects_error_header_despite_text(self):
        self.request_to(self.volc_response(code="55000031"))
        with self.assertRaisesRegex(asr.ProviderError, "繁忙"):
            asr.transcribe_volc(self.wav, {"api_key": "fake"})

    def test_volc_success_header_is_required(self):
        self.request_to(Response(asr.VOLC_ENDPOINT, {"result": {"text": "不应提交"}}))
        with self.assertRaises(asr.ProviderError):
            asr.transcribe_volc(self.wav, {"api_key": "fake"})

    def test_volc_rejects_empty_or_malformed_result(self):
        for payload in ({}, {"result": []}, {"result": {"text": "  "}}, {"result": {"text": 1}}):
            with self.subTest(payload=payload):
                with mock.patch.object(asr, "_json_post", return_value=(payload, {"x-api-status-code": "20000000"})):
                    with self.assertRaises(asr.ProviderError):
                        asr.transcribe_volc(self.wav, {"api_key": "fake"})

    def test_baidu_bearer_request_uses_pcm(self):
        opener = self.request_to(Response(asr.BAIDU_FAST_ENDPOINT, {"err_no": 0, "result": ["你好。", "其他候选"]}))
        self.assertEqual(asr.transcribe_baidu(self.wav, {"api_key": "bce-v3/fake"}), "你好。")
        req = opener.open.call_args.args[0]
        body = json.loads(req.data)
        self.assertEqual(req.get_header("Authorization"), "Bearer bce-v3/fake")
        self.assertNotIn("token", body)
        self.assertEqual(body["format"], "pcm")
        self.assertEqual(body["rate"], 16000)
        self.assertEqual(body["channel"], 1)
        self.assertEqual(body["dev_pid"], 80001)
        pcm = base64.b64decode(body["speech"])
        self.assertEqual(body["len"], len(pcm))
        self.assertEqual(pcm, b"\x01" * 3200)
        self.assertLessEqual(len(body["cuid"]), 60)

    def test_baidu_legacy_oauth_before_audio(self):
        opener = mock.Mock()
        opener.open.side_effect = [
            Response(asr.BAIDU_OAUTH_ENDPOINT, {"access_token": "fake-access-token", "expires_in": 100}),
            Response(asr.BAIDU_FAST_ENDPOINT, {"err_no": 0, "result": ["旧版成功"]}),
        ]
        with mock.patch.object(asr.request, "build_opener", return_value=opener):
            self.assertEqual(asr.transcribe_baidu(self.wav, {
                "api_key": "old-app-key", "secret_key": "old-app-secret", "dev_pid": "80001",
            }), "旧版成功")
        auth_call, audio_call = opener.open.call_args_list
        auth_req = auth_call.args[0]
        self.assertEqual(auth_req.full_url, asr.BAIDU_OAUTH_ENDPOINT)
        self.assertNotIn("old-app-secret", auth_req.full_url)
        self.assertEqual(parse.parse_qs(auth_req.data.decode("ascii")), {
            "grant_type": ["client_credentials"], "client_id": ["old-app-key"],
            "client_secret": ["old-app-secret"],
        })
        self.assertEqual(auth_call.kwargs["timeout"], 15)
        audio_req = audio_call.args[0]
        self.assertIsNone(audio_req.get_header("Authorization"))
        self.assertEqual(json.loads(audio_req.data)["token"], "fake-access-token")

    def test_baidu_oauth_failure_does_not_send_audio(self):
        opener = self.request_to(Response(asr.BAIDU_OAUTH_ENDPOINT, {
            "error": "invalid_client", "error_description": "secret-old-key should never appear",
        }))
        with self.assertRaises(asr.ProviderError) as caught:
            asr.transcribe_baidu(self.wav, {"api_key": "fake", "secret_key": "fake"})
        self.assertNotIn("secret-old-key", str(caught.exception))
        self.assertEqual(opener.open.call_count, 1)

    def test_baidu_standard_language_models(self):
        for dev_pid in (1537, 1737, 1637, 1837):
            with self.subTest(dev_pid=dev_pid):
                with mock.patch.object(asr, "_json_post", return_value=({"err_no": 0, "result": ["文字"]}, {})) as post:
                    asr.transcribe_baidu(self.wav, {"api_key": "fake", "dev_pid": str(dev_pid)})
                    self.assertEqual(post.call_args.args[0], asr.BAIDU_STANDARD_ENDPOINT)

    def test_baidu_unknown_model_never_connects(self):
        for model in (True, 80001.5, "invalid", "80001.0", 99999):
            with self.subTest(model=model), mock.patch.object(asr.request, "build_opener") as builder:
                with self.assertRaises(asr.ProviderError):
                    asr.transcribe_baidu(self.wav, {"api_key": "fake", "dev_pid": model})
                builder.assert_not_called()

    def test_baidu_error_codes_are_sanitized(self):
        for key, code, expected in (("err_no", 3302, "鉴权"), ("error_code", 6, "授权"),
                                    ("err_no", 3305, "额度"), ("err_no", 3308, "60秒")):
            with self.subTest(code=code):
                with mock.patch.object(asr, "_json_post", return_value=({key: code, "err_msg": "private-secret"}, {})):
                    with self.assertRaisesRegex(asr.ProviderError, expected) as caught:
                        asr.transcribe_baidu(self.wav, {"api_key": "fake"})
                    self.assertNotIn("private-secret", str(caught.exception))

    def test_baidu_rejects_missing_or_empty_result(self):
        for payload in ({}, {"err_no": 0}, {"err_no": 0, "result": "文字"}, {"err_no": 0, "result": [""]}):
            with self.subTest(payload=payload), mock.patch.object(asr, "_json_post", return_value=(payload, {})):
                with self.assertRaises(asr.ProviderError):
                    asr.transcribe_baidu(self.wav, {"api_key": "fake"})


class BoundaryTests(unittest.TestCase):
    def test_baidu_60_seconds_accepted_just_over_rejected(self):
        asr._audio_frames(make_wav(60), max_seconds=60)
        with self.assertRaisesRegex(asr.ProviderError, "60秒"):
            asr._audio_frames(make_wav(60.1), max_seconds=60)

    def test_volc_120_seconds_accepted_just_over_rejected(self):
        asr._audio_frames(make_wav(120), max_seconds=120)
        with self.assertRaisesRegex(asr.ProviderError, "120秒"):
            asr._audio_frames(make_wav(120.1), max_seconds=120)

    def test_audio_format_and_truncated_data_rejected(self):
        for audio in (b"bad-file", make_wav(rate=8000), make_wav(channels=2), make_wav(width=1), make_wav()[:-2], make_wav(0)):
            with self.subTest(length=len(audio)), self.assertRaises(asr.ProviderError):
                asr._audio_frames(audio, max_seconds=60)

    def test_invalid_credentials_cannot_inject_headers(self):
        for key in ("fake\r\nAuthorization: stolen", "不应发送", 123, "\x7f"):
            with self.subTest(key=key), mock.patch.object(asr.request, "build_opener") as builder:
                with self.assertRaises(asr.ProviderError):
                    asr.transcribe_volc(make_wav(), {"api_key": key})
                builder.assert_not_called()

    def test_requests_reject_non_official_address(self):
        for endpoint in ("http://vop.baidu.com/pro_api", "https://example.com", asr.VOLC_ENDPOINT + "?token=secret"):
            with self.subTest(endpoint=endpoint), mock.patch.object(asr.request, "build_opener") as builder:
                with self.assertRaises(asr.ProviderError):
                    asr._post(endpoint, b"{}", {}, "语音服务")
                builder.assert_not_called()

    def test_redirect_handler_does_not_forward_credentials(self):
        self.assertIsNone(asr._NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.com"))

    def test_http_error_suppresses_server_body_and_url(self):
        http_error = error.HTTPError(asr.VOLC_ENDPOINT + "?secret=fake", 403, "secret body", {}, io.BytesIO(b"private-key"))
        opener = mock.Mock()
        opener.open.side_effect = http_error
        with mock.patch.object(asr.request, "build_opener", return_value=opener):
            with self.assertRaisesRegex(asr.ProviderError, "授权") as caught:
                asr.transcribe_volc(make_wav(), {"api_key": "fake"})
        self.assertNotIn("secret", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)

    def test_redirect_http_error_is_explained(self):
        opener = mock.Mock()
        opener.open.side_effect = error.HTTPError(asr.VOLC_ENDPOINT, 302, "Moved", {}, None)
        with mock.patch.object(asr.request, "build_opener", return_value=opener):
            with self.assertRaisesRegex(asr.ProviderError, "重定向"):
                asr.transcribe_volc(make_wav(), {"api_key": "fake"})

    def test_timeout_and_tls_failure_sanitized(self):
        for exc, message in ((socket.timeout("private-key"), "超时"), (error.URLError("private-key"), "安全连接")):
            with self.subTest(error=type(exc).__name__):
                opener = mock.Mock()
                opener.open.side_effect = exc
                with mock.patch.object(asr.request, "build_opener", return_value=opener):
                    with self.assertRaisesRegex(asr.ProviderError, message) as caught:
                        asr.transcribe_volc(make_wav(), {"api_key": "fake"})
                self.assertNotIn("private-key", str(caught.exception))

    def test_oversized_or_non_json_response_rejected(self):
        for raw, message in ((b"x" * (asr._MAX_RESPONSE_BYTES + 1), "过大"), (b"not-json", "格式"), (b"[]", "格式")):
            with self.subTest(length=len(raw)):
                opener = mock.Mock()
                opener.open.return_value = Response(asr.VOLC_ENDPOINT, {}, raw=raw)
                with mock.patch.object(asr.request, "build_opener", return_value=opener):
                    with self.assertRaisesRegex(asr.ProviderError, message):
                        asr.transcribe_volc(make_wav(), {"api_key": "fake"})

    def test_changed_response_address_is_rejected(self):
        opener = mock.Mock()
        opener.open.return_value = Response("https://example.com", {"result": {"text": "错误"}})
        with mock.patch.object(asr.request, "build_opener", return_value=opener):
            with self.assertRaisesRegex(asr.ProviderError, "地址不符"):
                asr.transcribe_volc(make_wav(), {"api_key": "fake"})


if __name__ == "__main__":
    unittest.main()
