"""腾讯云极速录音识别与讯飞语音听写；凭据仅用于官方 TLS 接口。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import re
import socket
import ssl
import threading
import time
import wave
from email.utils import formatdate
from urllib import error, parse, request

import websocket

from talkman_qwen import QwenError


TENCENT_SOURCE = "https://github.com/TencentCloud/tencentcloud-speech-sdk-python/blob/master/asr/flash_recognizer.py"
XUNFEI_SOURCE = "https://www.xfyun.cn/doc/asr/voicedictation/API.html"
TENCENT_MAX_SECONDS = 120
XUNFEI_MAX_SECONDS = 55
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_TEXT_CHARS = 200_000
_TENCENT_HOST = "asr.cloud.tencent.com"
_XUNFEI_HOST = "iat-api.xfyun.cn"
_XUNFEI_PATH = "/v2/iat"
_XUNFEI_FRAME_BYTES = 1280


def _credential(credentials: dict[str, str], name: str, provider: str) -> str:
    value = credentials.get(name, "") if isinstance(credentials, dict) else ""
    if not isinstance(value, str) or not value.strip():
        raise QwenError(f"请在语音设置中填写{provider}的 {name}。")
    value = value.strip()
    if len(value) > 4096 or not re.fullmatch(r"[A-Za-z0-9_./+=-]+", value):
        raise QwenError(f"{provider}的 {name} 格式不正确，请重新填写。")
    return value


def _pcm16(wav_bytes: bytes, max_seconds: int, provider: str) -> bytes:
    if not isinstance(wav_bytes, bytes) or len(wav_bytes) > max_seconds * 32_000 + 4096:
        raise QwenError(f"{provider}本轮录音不能超过 {max_seconds} 秒。")
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as audio:
            if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getcomptype()) != (1, 2, 16000, "NONE"):
                raise QwenError("录音格式需要是 16kHz、单声道、16 位 PCM WAV。")
            frames = audio.getnframes()
            if frames == 0:
                raise QwenError("录音为空，请重新录入。")
            if frames > max_seconds * 16000:
                raise QwenError(f"{provider}本轮录音不能超过 {max_seconds} 秒。")
            pcm = audio.readframes(frames)
            if len(pcm) != frames * 2:
                raise QwenError("录音文件不完整，请重新录入。")
            return pcm
    except QwenError:
        raise
    except (wave.Error, EOFError, OSError, ValueError):
        raise QwenError("录音文件格式无效，请重新录入。") from None


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _decode_response(content: bytes | str, provider: str) -> dict:
    size = len(content.encode("utf-8")) if isinstance(content, str) else len(content)
    if size > _MAX_RESPONSE_BYTES:
        raise QwenError(f"{provider}返回内容过大，本轮未修改正文。")
    try:
        result = json.loads(content)
    except (ValueError, UnicodeError, TypeError):
        raise QwenError(f"{provider}返回格式异常，请稍后重试。") from None
    if not isinstance(result, dict):
        raise QwenError(f"{provider}返回格式异常，请稍后重试。")
    return result


def _checked_text(text: object, provider: str) -> str:
    if not isinstance(text, str) or len(text) > _MAX_TEXT_CHARS:
        raise QwenError(f"{provider}识别结果格式异常，本轮未修改正文。")
    if not text.strip():
        raise QwenError("没有识别到语音，请靠近麦克风后重试。")
    return text


def transcribe_tencent(wav_bytes: bytes, credentials: dict[str, str]) -> str:
    """使用腾讯云录音识别极速版；最长 120 秒为本软件录音上限。"""
    _pcm16(wav_bytes, TENCENT_MAX_SECONDS, "腾讯云")
    app_id = _credential(credentials, "app_id", "腾讯云")
    secret_id = _credential(credentials, "secret_id", "腾讯云")
    secret_key = _credential(credentials, "secret_key", "腾讯云")
    if not app_id.isdecimal():
        raise QwenError("腾讯云 AppId 应为数字，请在腾讯云账号信息中确认。")
    engine_type = credentials.get("engine_type", "16k_zh") or "16k_zh"
    if not isinstance(engine_type, str) or not re.fullmatch(r"16k_[A-Za-z0-9_]{1,50}", engine_type):
        raise QwenError("腾讯云识别引擎名称无效，请选择 16kHz 语音引擎。")
    params = {
        "secretid": secret_id,
        "timestamp": str(int(time.time())),
        "engine_type": engine_type,
        "voice_format": "wav",
        "speaker_diarization": 0,
        "customization_id": "",
        "filter_dirty": 0,
        "filter_modal": 0,
        "filter_punc": 0,
        "convert_num_mode": 1,
        "word_info": 0,
        "first_channel_only": 1,
        "reinforce_hotword": 0,
        "sentence_max_length": 0,
    }
    path = f"/asr/flash/v1/{app_id}"
    # 腾讯官方 SDK 对排序后的参数原文签名，网络 URL 才做百分号编码。
    query = "&".join(f"{key}={value}" for key, value in sorted(params.items()))
    sign_input = f"POST{_TENCENT_HOST}{path}?{query}"
    signature = base64.b64encode(hmac.new(secret_key.encode(), sign_input.encode(), hashlib.sha1).digest()).decode()
    url = f"https://{_TENCENT_HOST}{path}?{parse.urlencode(sorted(params.items()))}"
    req = request.Request(url, data=wav_bytes, method="POST", headers={
        "Authorization": signature, "Content-Type": "application/octet-stream", "Host": _TENCENT_HOST,
    })
    opener = request.build_opener(_NoRedirect(), request.HTTPSHandler(context=ssl.create_default_context()))
    try:
        with opener.open(req, timeout=60) as response:
            payload = _decode_response(response.read(_MAX_RESPONSE_BYTES + 1), "腾讯云")
    except QwenError:
        raise
    except error.HTTPError as exc:
        if exc.code in (301, 302, 303, 307, 308):
            raise QwenError("腾讯云接口要求跳转，已停止请求，请检查网络代理。") from None
        if exc.code in (401, 403):
            raise QwenError("腾讯云鉴权失败，请检查 AppId、SecretId、SecretKey 和语音服务权限。") from None
        if exc.code == 429:
            raise QwenError("腾讯云请求过于频繁，请稍后再试。") from None
        raise QwenError("腾讯云暂时无法完成识别，请检查服务额度并稍后重试。") from None
    except (error.URLError, socket.timeout, TimeoutError, ssl.SSLError, OSError):
        raise QwenError("无法连接腾讯云语音服务，请检查网络后重试。") from None
    code = payload.get("code", payload.get("status"))
    if code != 0:
        raise QwenError("腾讯云未完成识别，请检查凭据、语音服务开通状态和剩余额度。")
    results = payload.get("flash_result")
    if not isinstance(results, list) or not results or not isinstance(results[0], dict):
        raise QwenError("腾讯云没有返回识别文本，请重新录入。")
    # 本软件仅上传单声道，避免误把多声道结果重复拼接。
    return _checked_text(results[0].get("text"), "腾讯云")


def _xunfei_url(api_key: str, api_secret: str) -> str:
    date = formatdate(usegmt=True)
    origin = f"host: {_XUNFEI_HOST}\ndate: {date}\nGET {_XUNFEI_PATH} HTTP/1.1"
    signature = base64.b64encode(hmac.new(api_secret.encode(), origin.encode(), hashlib.sha256).digest()).decode()
    authorization = (f'api_key="{api_key}", algorithm="hmac-sha256", '
                     f'headers="host date request-line", signature="{signature}"')
    query = parse.urlencode({"authorization": base64.b64encode(authorization.encode()).decode(),
                             "date": date, "host": _XUNFEI_HOST})
    return f"wss://{_XUNFEI_HOST}{_XUNFEI_PATH}?{query}"


def _merge_xunfei(result: dict, segments: dict[int, str]) -> None:
    sn = result.get("sn")
    words = result.get("ws")
    if not isinstance(sn, int) or not 0 <= sn < 10000 or not isinstance(words, list):
        raise QwenError("讯飞识别结果格式异常，请重新录入。")
    if len(words) > 10000:
        raise QwenError("讯飞识别结果过大，本轮未修改正文。")
    tokens = []
    for word in words:
        candidates = word.get("cw") if isinstance(word, dict) else None
        if not isinstance(candidates, list) or not candidates or not isinstance(candidates[0], dict):
            raise QwenError("讯飞识别结果格式异常，请重新录入。")
        text = candidates[0].get("w")
        if not isinstance(text, str):
            raise QwenError("讯飞识别结果格式异常，请重新录入。")
        tokens.append(text)
    if result.get("pgs") == "rpl":
        replacement = result.get("rg")
        if (not isinstance(replacement, list) or len(replacement) != 2
                or not all(isinstance(n, int) for n in replacement)
                or not 0 <= replacement[0] <= replacement[1] < 10000):
            raise QwenError("讯飞动态修正范围异常，请重新录入。")
        for old in list(segments):
            if replacement[0] <= old <= replacement[1]:
                del segments[old]
    segments[sn] = "".join(tokens)
    if sum(map(len, segments.values())) > _MAX_TEXT_CHARS:
        raise QwenError("讯飞识别结果过大，本轮未修改正文。")


def transcribe_xunfei(wav_bytes: bytes, credentials: dict[str, str]) -> str:
    """录音后按讯飞官方流式帧协议上传，单轮限制 55 秒。"""
    pcm = _pcm16(wav_bytes, XUNFEI_MAX_SECONDS, "讯飞")
    app_id = _credential(credentials, "app_id", "讯飞")
    api_key = _credential(credentials, "api_key", "讯飞")
    api_secret = _credential(credentials, "api_secret", "讯飞")
    language = credentials.get("language", "zh_cn") or "zh_cn"
    accent = credentials.get("accent", "mandarin") or "mandarin"
    if language not in ("zh_cn", "en_us") or accent not in ("mandarin", "cantonese"):
        raise QwenError("讯飞语言设置无效，请选择中文普通话、粤语或英语并开通相应服务。")
    business = {"language": language, "domain": "iat", "vad_eos": 10000, "ptt": 1}
    if language == "zh_cn":
        business.update({"accent": accent})
        if accent == "mandarin":
            business["dwa"] = "wpgs"
    ws = None
    sender = None
    stop = threading.Event()
    audio_uploaded = threading.Event()
    send_errors: list[Exception] = []
    deadline = time.monotonic() + 70
    try:
        ws = websocket.create_connection(_xunfei_url(api_key, api_secret), timeout=15,
                                         sslopt={"cert_reqs": ssl.CERT_REQUIRED, "check_hostname": True},
                                         redirect_limit=0, enable_multithread=True)
        if ws.getstatus() != 101:
            raise QwenError("讯飞连接未通过验证，已停止请求，请检查网络代理。")

        def send_audio():
            try:
                for position in range(0, len(pcm), _XUNFEI_FRAME_BYTES):
                    if stop.is_set():
                        return
                    frame = {"data": {"status": 0 if position == 0 else 1,
                                      "format": "audio/L16;rate=16000", "encoding": "raw",
                                      "audio": base64.b64encode(pcm[position:position + _XUNFEI_FRAME_BYTES]).decode()}}
                    if position == 0:
                        frame.update({"common": {"app_id": app_id}, "business": business})
                    ws.send(json.dumps(frame, ensure_ascii=False))
                    if stop.wait(0.04):
                        return
                audio_uploaded.set()
                ws.send(json.dumps({"data": {"status": 2, "format": "audio/L16;rate=16000",
                                             "encoding": "raw", "audio": ""}}))
            except Exception as exc:
                send_errors.append(exc)
                stop.set()

        sender = threading.Thread(target=send_audio, daemon=True, name="TalkMan-Xunfei-upload")
        sender.start()
        segments: dict[int, str] = {}
        total_bytes = 0
        for _ in range(4096):
            if send_errors:
                raise QwenError("讯飞音频上传失败，请检查网络后重试。")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise QwenError("讯飞识别超时，请缩短录音后重试。")
            ws.settimeout(min(15, remaining))
            raw = ws.recv()
            if not isinstance(raw, (bytes, str)) or not raw:
                raise QwenError("讯飞连接在识别完成前关闭，请重新录入。")
            total_bytes += len(raw.encode("utf-8")) if isinstance(raw, str) else len(raw)
            if total_bytes > _MAX_RESPONSE_BYTES:
                raise QwenError("讯飞返回内容过大，本轮未修改正文。")
            payload = _decode_response(raw, "讯飞")
            if payload.get("code") != 0:
                raise QwenError("讯飞未完成识别，请检查凭据、语音听写服务权限和剩余额度。")
            data = payload.get("data")
            if not isinstance(data, dict) or data.get("status") not in (0, 1, 2):
                raise QwenError("讯飞返回格式异常，请重新录入。")
            result = data.get("result")
            if result is not None:
                if not isinstance(result, dict):
                    raise QwenError("讯飞返回格式异常，请重新录入。")
                _merge_xunfei(result, segments)
            if data["status"] == 2:
                if not audio_uploaded.is_set():
                    raise QwenError("讯飞因静音提前结束识别，后半段可能未被识别，请缩短停顿后重试。")
                return _checked_text("".join(segments[sn] for sn in sorted(segments)), "讯飞")
        raise QwenError("讯飞返回次数异常，本轮未修改正文。")
    except QwenError:
        raise
    except websocket.WebSocketBadStatusException as exc:
        if exc.status_code in (401, 403):
            raise QwenError("讯飞鉴权失败，请检查 AppId、APIKey、APISecret 和电脑时间。") from None
        raise QwenError("讯飞连接失败，请检查网络和语音听写服务开通状态。") from None
    except (websocket.WebSocketTimeoutException, socket.timeout, TimeoutError):
        raise QwenError("讯飞识别超时，请检查网络或缩短录音后重试。") from None
    except (websocket.WebSocketException, ssl.SSLError, OSError, ValueError):
        raise QwenError("无法连接讯飞语音服务，请检查网络后重试。") from None
    finally:
        stop.set()
        if ws is not None:
            try:
                ws.close(timeout=1)
            except Exception:
                pass
        if sender is not None:
            sender.join(timeout=1)
