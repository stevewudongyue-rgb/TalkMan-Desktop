"""腾讯云和讯飞的离线协议测试，不开麦、不访问服务、不使用真实密钥。"""

import base64
import hashlib
import hmac
import io
import json
import threading
import unittest
import wave
from urllib import error, parse
from unittest.mock import patch

import talkman_asr_tencent_xunfei as providers
from talkman_qwen import QwenError


def wav_audio(seconds=0.08, channels=1, sample_rate=16000, sample_width=2):
    result = io.BytesIO()
    with wave.open(result, "wb") as audio:
        audio.setnchannels(channels)
        audio.setsampwidth(sample_width)
        audio.setframerate(sample_rate)
        audio.writeframes(b"\x00" * int(sample_rate * seconds) * channels * sample_width)
    return result.getvalue()


TENCENT = {"app_id": "123456789", "secret_id": "AKID-offline-test", "secret_key": "offline-secret"}
XUNFEI = {"app_id": "abcd1234", "api_key": "offline-api-key", "api_secret": "offline-secret"}


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode()
        self.read_limit = None

    def read(self, limit):
        self.read_limit = limit
        return self.payload[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class FakeOpener:
    def __init__(self, payload):
        self.response = FakeResponse(payload)
        self.request = None

    def open(self, request, timeout):
        self.request = request
        self.timeout = timeout
        return self.response


def xunfei_result(sn, text, status=1, pgs="apd", rg=None):
    result = {"sn": sn, "ws": [{"cw": [{"w": text}, {"w": "错误候选"}]}], "pgs": pgs}
    if rg is not None:
        result["rg"] = rg
    return {"code": 0, "data": {"status": status, "result": result}}


class FakeSocket:
    def __init__(self, responses, status=101):
        self.responses = list(responses)
        self.frames = []
        self.status = status
        self.finished = threading.Event()
        self.closed = False

    def getstatus(self):
        return self.status

    def send(self, value):
        frame = json.loads(value)
        self.frames.append(frame)
        if frame["data"]["status"] == 2:
            self.finished.set()

    def settimeout(self, timeout):
        self.timeout = timeout

    def recv(self):
        if not self.finished.wait(2):
            raise TimeoutError("offline test timed out")
        value = self.responses.pop(0)
        return value if isinstance(value, (str, bytes)) else json.dumps(value, ensure_ascii=False)

    def close(self, timeout):
        self.closed = True
        self.finished.set()


class TencentTests(unittest.TestCase):
    def test_official_signing_and_raw_wav_upload(self):
        opener = FakeOpener({"code": 0, "flash_result": [{"channel_id": 0, "text": "中文测试。"}]})
        wav = wav_audio()
        with patch.object(providers.request, "build_opener", return_value=opener) as build, patch.object(providers.time, "time", return_value=1700000000):
            self.assertEqual(providers.transcribe_tencent(wav, TENCENT), "中文测试。")
        req = opener.request
        url = parse.urlsplit(req.full_url)
        self.assertEqual(url.scheme, "https")
        self.assertEqual(url.hostname, "asr.cloud.tencent.com")
        self.assertEqual(url.path, "/asr/flash/v1/123456789")
        params = parse.parse_qs(url.query, keep_blank_values=True)
        self.assertEqual(params["engine_type"], ["16k_zh"])
        self.assertEqual(params["voice_format"], ["wav"])
        self.assertEqual(params["filter_modal"], ["0"])
        self.assertEqual(params["filter_punc"], ["0"])
        query = "&".join(f"{key}={value[0]}" for key, value in sorted(params.items()))
        sign = "POSTasr.cloud.tencent.com/asr/flash/v1/123456789?" + query
        expected = base64.b64encode(hmac.new(b"offline-secret", sign.encode(), hashlib.sha1).digest()).decode()
        self.assertEqual(req.get_header("Authorization"), expected)
        self.assertEqual(req.data, wav)
        self.assertEqual(opener.timeout, 60)
        self.assertEqual(opener.response.read_limit, providers._MAX_RESPONSE_BYTES + 1)
        self.assertIsInstance(build.call_args.args[0], providers._NoRedirect)

    def test_invalid_audio_never_sends_request(self):
        with patch.object(providers.request, "build_opener") as build:
            for wav in (b"broken", wav_audio(channels=2), wav_audio(sample_rate=8000), wav_audio(seconds=0), wav_audio(seconds=121)):
                with self.subTest(size=len(wav)), self.assertRaises(QwenError):
                    providers.transcribe_tencent(wav, TENCENT)
            build.assert_not_called()

    def test_credentials_and_model_validation(self):
        with patch.object(providers.request, "build_opener") as build:
            for changes in ({"app_id": "not-number"}, {"secret_id": ""}, {"secret_key": "a\nsecret"}, {"engine_type": "bad&inject=1"}):
                with self.subTest(changes=changes), self.assertRaises(QwenError):
                    providers.transcribe_tencent(wav_audio(), dict(TENCENT, **changes))
            build.assert_not_called()

    def test_error_message_does_not_expose_server_or_key(self):
        opener = FakeOpener({"code": 1001, "message": "sensitive-server-data offline-secret"})
        with patch.object(providers.request, "build_opener", return_value=opener), self.assertRaises(QwenError) as raised:
            providers.transcribe_tencent(wav_audio(), TENCENT)
        self.assertNotIn("sensitive", str(raised.exception))
        self.assertNotIn("offline-secret", str(raised.exception))

    def test_http_auth_redirect_and_network_are_safe(self):
        for exception in (error.HTTPError("sensitive-url", 401, "secret", {}, None),
                          error.HTTPError("sensitive-url", 302, "secret", {}, None),
                          error.URLError("offline-secret")):
            opener = FakeOpener({})
            with self.subTest(exception=type(exception)), patch.object(opener, "open", side_effect=exception), patch.object(providers.request, "build_opener", return_value=opener), self.assertRaises(QwenError) as raised:
                providers.transcribe_tencent(wav_audio(), TENCENT)
            self.assertNotIn("offline-secret", str(raised.exception))
            self.assertNotIn("sensitive-url", str(raised.exception))
        self.assertIsNone(providers._NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.com"))

    def test_missing_empty_and_oversized_result_rejected(self):
        for payload in ({"code": 0}, {"code": 0, "flash_result": []},
                        {"code": 0, "flash_result": [{"text": " "}]},
                        b"{" + b" " * providers._MAX_RESPONSE_BYTES):
            opener = FakeOpener(payload)
            with self.subTest(type=type(payload)), patch.object(providers.request, "build_opener", return_value=opener), self.assertRaises(QwenError):
                providers.transcribe_tencent(wav_audio(), TENCENT)


class XunfeiTests(unittest.TestCase):
    def test_hmac_url_matches_official_protocol(self):
        date = "Tue, 14 Nov 2023 22:13:20 GMT"
        with patch.object(providers, "formatdate", return_value=date):
            url = providers._xunfei_url("offline-api-key", "offline-secret")
        parsed = parse.urlsplit(url)
        params = parse.parse_qs(parsed.query)
        self.assertEqual((parsed.scheme, parsed.hostname, parsed.path), ("wss", "iat-api.xfyun.cn", "/v2/iat"))
        self.assertEqual(params["date"], [date])
        origin = f"host: iat-api.xfyun.cn\ndate: {date}\nGET /v2/iat HTTP/1.1"
        signature = base64.b64encode(hmac.new(b"offline-secret", origin.encode(), hashlib.sha256).digest()).decode()
        authorization = base64.b64decode(params["authorization"][0]).decode()
        self.assertIn(f'signature="{signature}"', authorization)
        self.assertIn('headers="host date request-line"', authorization)

    def test_pcm_frames_tls_and_dynamic_replacement(self):
        responses = [xunfei_result(0, "帮我"), xunfei_result(1, "苹果"),
                     xunfei_result(2, "梨", pgs="rpl", rg=[1, 1]), xunfei_result(3, "拿过来。", status=2)]
        ws = FakeSocket(responses)
        wav = wav_audio()
        with patch.object(providers.websocket, "create_connection", return_value=ws) as connect:
            self.assertEqual(providers.transcribe_xunfei(wav, XUNFEI), "帮我梨拿过来。")
        self.assertTrue(ws.closed)
        frames = ws.frames
        self.assertEqual([frame["data"]["status"] for frame in frames], [0, 1, 2])
        self.assertEqual(frames[0]["common"], {"app_id": "abcd1234"})
        self.assertEqual(frames[0]["business"]["dwa"], "wpgs")
        self.assertEqual(frames[0]["data"]["format"], "audio/L16;rate=16000")
        self.assertNotIn("common", frames[1])
        pcm = b"".join(base64.b64decode(frame["data"]["audio"]) for frame in frames)
        self.assertEqual(pcm, providers._pcm16(wav, 55, "讯飞"))
        self.assertFalse(pcm.startswith(b"RIFF"))
        self.assertEqual(connect.call_args.kwargs["redirect_limit"], 0)
        self.assertTrue(connect.call_args.kwargs["sslopt"]["check_hostname"])
        self.assertEqual(connect.call_args.kwargs["sslopt"]["cert_reqs"], providers.ssl.CERT_REQUIRED)

    def test_english_omits_chinese_parameters(self):
        ws = FakeSocket([xunfei_result(0, "English speech.", status=2)])
        with patch.object(providers.websocket, "create_connection", return_value=ws):
            self.assertEqual(providers.transcribe_xunfei(wav_audio(seconds=0.01), dict(XUNFEI, language="en_us")), "English speech.")
        self.assertNotIn("dwa", ws.frames[0]["business"])
        self.assertNotIn("accent", ws.frames[0]["business"])

    def test_max_duration_and_credentials_checked_before_connection(self):
        with patch.object(providers.websocket, "create_connection") as connect:
            for audio, credentials in ((wav_audio(seconds=56), XUNFEI), (wav_audio(), dict(XUNFEI, api_secret="")),
                                       (wav_audio(), dict(XUNFEI, language="bad"))):
                with self.subTest(length=len(audio)), self.assertRaises(QwenError):
                    providers.transcribe_xunfei(audio, credentials)
            connect.assert_not_called()

    def test_redirect_does_not_send_credentials_or_audio(self):
        ws = FakeSocket([], status=302)
        with patch.object(providers.websocket, "create_connection", return_value=ws), self.assertRaises(QwenError):
            providers.transcribe_xunfei(wav_audio(), XUNFEI)
        self.assertEqual(ws.frames, [])
        self.assertTrue(ws.closed)

    def test_provider_error_redacted_and_socket_closed(self):
        ws = FakeSocket([{"code": 10114, "message": "offline-secret sensitive response"}])
        with patch.object(providers.websocket, "create_connection", return_value=ws), self.assertRaises(QwenError) as raised:
            providers.transcribe_xunfei(wav_audio(seconds=0.01), XUNFEI)
        self.assertNotIn("offline-secret", str(raised.exception))
        self.assertNotIn("sensitive", str(raised.exception))
        self.assertTrue(ws.closed)

    def test_malformed_result_and_empty_speech_rejected(self):
        bad_range = xunfei_result(0, "你好", status=2, pgs="rpl", rg=[2, 0])
        for payload in (bad_range, {"code": 0, "data": {"status": 2}}, b"not-json",
                        xunfei_result(0, "", status=2)):
            ws = FakeSocket([payload])
            with self.subTest(payload_type=type(payload)), patch.object(providers.websocket, "create_connection", return_value=ws), self.assertRaises(QwenError):
                providers.transcribe_xunfei(wav_audio(seconds=0.01), XUNFEI)
            self.assertTrue(ws.closed)

    def test_oversized_response_rejected(self):
        ws = FakeSocket([b" " * (providers._MAX_RESPONSE_BYTES + 1)])
        with patch.object(providers.websocket, "create_connection", return_value=ws), self.assertRaises(QwenError):
            providers.transcribe_xunfei(wav_audio(seconds=0.01), XUNFEI)

    def test_network_error_redacted(self):
        with patch.object(providers.websocket, "create_connection", side_effect=OSError("offline-secret")), self.assertRaises(QwenError) as raised:
            providers.transcribe_xunfei(wav_audio(), XUNFEI)
        self.assertNotIn("offline-secret", str(raised.exception))

    def test_early_vad_end_does_not_commit_truncated_text(self):
        class EarlyEndSocket(FakeSocket):
            def recv(self):
                return json.dumps(xunfei_result(0, "只识别了开头", status=2), ensure_ascii=False)

        ws = EarlyEndSocket([])
        with patch.object(providers.websocket, "create_connection", return_value=ws), self.assertRaises(QwenError) as raised:
            providers.transcribe_xunfei(wav_audio(seconds=1), XUNFEI)
        self.assertIn("提前结束", str(raised.exception))
        self.assertTrue(ws.closed)


if __name__ == "__main__":
    unittest.main()
