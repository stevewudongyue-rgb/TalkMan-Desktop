"""豆包与百度录音识别；仅向固定官方 HTTPS 地址发送内存中的 WAV。"""

from __future__ import annotations

import base64
import io
import json
import socket
import ssl
import uuid
import wave
from urllib import error, parse, request

from talkman_qwen import QwenError


VOLC_ENDPOINT = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash"
VOLC_RESOURCE_ID = "volc.bigasr.auc_turbo"
BAIDU_OAUTH_ENDPOINT = "https://aip.baidubce.com/oauth/2.0/token"
BAIDU_FAST_ENDPOINT = "https://vop.baidu.com/pro_api"
BAIDU_STANDARD_ENDPOINT = "https://vop.baidu.com/server_api"
REQUEST_TIMEOUT = 40
OAUTH_TIMEOUT = 15
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_AUDIO_BYTES = 16000 * 2 * 120 + 65536
_BAIDU_MODELS = {80001, 1537, 1737, 1637, 1837}
_OFFICIAL_ENDPOINTS = {
    VOLC_ENDPOINT, BAIDU_OAUTH_ENDPOINT, BAIDU_FAST_ENDPOINT, BAIDU_STANDARD_ENDPOINT,
}


class ProviderError(QwenError):
    """可展示的中文错误；不包含密钥、音频、服务端正文或原异常。"""


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _credential(credentials: dict, name: str, label: str, *, required: bool = True) -> str:
    if not isinstance(credentials, dict):
        raise ProviderError("语音服务设置格式无效，请重新保存模型设置。")
    value = credentials.get(name, "")
    if not isinstance(value, str):
        raise ProviderError(f"{label}格式无效，请检查模型设置。")
    value = value.strip()
    if not value and required:
        raise ProviderError(f"请先在模型设置中填写{label}。")
    if len(value) > 4096 or any(not c.isascii() or c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value):
        raise ProviderError(f"{label}格式无效，请检查模型设置。")
    return value


def _audio_frames(wav_bytes: bytes, *, max_seconds: int) -> bytes:
    if not isinstance(wav_bytes, bytes) or not wav_bytes or len(wav_bytes) > _MAX_AUDIO_BYTES:
        raise ProviderError("录音为空或过大，请重新录音。")
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as audio:
            if (audio.getframerate() != 16000 or audio.getnchannels() != 1
                    or audio.getsampwidth() != 2 or audio.getcomptype() != "NONE"):
                raise ProviderError("录音必须为16千赫、单声道、16位的 WAV，请检查录音设置。")
            frame_count = audio.getnframes()
            if frame_count <= 0:
                raise ProviderError("录音没有有效声音数据，请重新录音。")
            if frame_count > 16000 * max_seconds:
                raise ProviderError(f"当前语音服务每次最多识别{max_seconds}秒，请分段录音。")
            frames = audio.readframes(frame_count)
            if len(frames) != frame_count * 2:
                raise ProviderError("录音数据不完整，请重新录音。")
            return frames
    except (wave.Error, EOFError, ValueError, OSError):
        raise ProviderError("录音文件格式无效，请重新录音。") from None


def _http_message(provider: str, code: int) -> str:
    if code in (301, 302, 303, 307, 308):
        return f"{provider}接口发生重定向，为保护密钥已停止请求。"
    if code in (401, 403):
        return f"{provider}鉴权或授权失败，请检查密钥及语音服务开通状态。"
    if code in (402, 429):
        return f"{provider}额度不足或请求过于频繁，请检查服务额度后重试。"
    if code >= 500:
        return f"{provider}服务暂时不可用，请稍后重试。"
    return f"{provider}请求未成功，请检查模型设置和服务开通状态。"


def _post(endpoint: str, body: bytes, headers: dict, provider: str,
          *, timeout: int = REQUEST_TIMEOUT) -> tuple[dict, dict]:
    if endpoint not in _OFFICIAL_ENDPOINTS:
        raise ProviderError("语音识别只能连接预设的官方 HTTPS 地址。")
    opener = request.build_opener(_NoRedirect(), request.HTTPSHandler(context=ssl.create_default_context()))
    req = request.Request(endpoint, data=body, headers=headers, method="POST")
    try:
        with opener.open(req, timeout=timeout) as response:
            if response.geturl() != endpoint:
                raise ProviderError(f"{provider}响应地址不符，为保护密钥已停止请求。")
            if response.status != 200:
                raise ProviderError(_http_message(provider, response.status))
            data = response.read(_MAX_RESPONSE_BYTES + 1)
            if len(data) > _MAX_RESPONSE_BYTES:
                raise ProviderError(f"{provider}响应过大，已停止读取，请分段录音。")
            response_headers = dict(response.headers)
    except error.HTTPError as exc:
        code = exc.code
        exc.close()
        raise ProviderError(_http_message(provider, code)) from None
    except (socket.timeout, TimeoutError):
        raise ProviderError(f"{provider}请求超时，请检查网络后重试。") from None
    except (error.URLError, OSError, ssl.SSLError):
        raise ProviderError(f"无法安全连接{provider}，请检查网络及系统时间后重试。") from None
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ProviderError(f"{provider}返回格式无效，请稍后重试。") from None
    if not isinstance(payload, dict):
        raise ProviderError(f"{provider}返回格式无效，请稍后重试。")
    return payload, {str(k).lower(): str(v) for k, v in response_headers.items()}


def _json_post(endpoint: str, payload: dict, headers: dict, provider: str) -> tuple[dict, dict]:
    return _post(endpoint, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                 {"Content-Type": "application/json", "Accept": "application/json", **headers}, provider)


def transcribe_volc(wav_bytes: bytes, credentials: dict) -> str:
    """新版 api_key；旧版 app_id + access_key。须开通极速版资源。"""
    api_key = _credential(credentials, "api_key", "豆包语音 API Key", required=False)
    if api_key:
        headers = {"X-Api-Key": api_key}
    else:
        app_id = _credential(credentials, "app_id", "豆包语音应用编号")
        access_key = _credential(credentials, "access_key", "豆包语音访问令牌")
        headers = {"X-Api-App-Key": app_id, "X-Api-Access-Key": access_key}
    _audio_frames(wav_bytes, max_seconds=120)
    headers.update({
        "X-Api-Resource-Id": VOLC_RESOURCE_ID,
        "X-Api-Request-Id": str(uuid.uuid4()),
        "X-Api-Sequence": "-1",
    })
    payload, response_headers = _json_post(VOLC_ENDPOINT, {
        "user": {"uid": "talkman-desktop"},
        "audio": {"data": base64.b64encode(wav_bytes).decode("ascii")},
        "request": {"model_name": "bigmodel", "enable_punc": True, "enable_ddc": False},
    }, headers, "豆包语音")
    code = response_headers.get("x-api-status-code", "")
    if code == "20000003":
        raise ProviderError("豆包语音未检测到有效人声，请重新录音。")
    if code != "20000000":
        if code in {"45000001", "45000002", "45000151"}:
            raise ProviderError("豆包语音录音参数无效，请重新录音并检查设置。")
        if code.startswith("550"):
            raise ProviderError("豆包语音服务暂时繁忙，请稍后重试。")
        raise ProviderError("豆包语音识别未成功，请检查密钥、极速版授权及额度。")
    result = payload.get("result")
    text = result.get("text") if isinstance(result, dict) else None
    if not isinstance(text, str) or not text.strip():
        raise ProviderError("豆包语音没有返回有效文字，请重新录音。")
    return text.strip()


def _baidu_token(api_key: str, secret_key: str) -> str:
    body = parse.urlencode({
        "grant_type": "client_credentials", "client_id": api_key, "client_secret": secret_key,
    }).encode("ascii")
    payload, _ = _post(BAIDU_OAUTH_ENDPOINT, body, {
        "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json",
    }, "百度语音", timeout=OAUTH_TIMEOUT)
    token = payload.get("access_token")
    if payload.get("error") or not isinstance(token, str) or not token or len(token) > 4096:
        raise ProviderError("百度应用鉴权失败，请核对应用 API Key 和 Secret Key。")
    if any(not c.isascii() or c.isspace() or ord(c) < 32 or ord(c) == 127 for c in token):
        raise ProviderError("百度应用鉴权返回无效，请稍后重试。")
    return token


def transcribe_baidu(wav_bytes: bytes, credentials: dict) -> str:
    """api_key 为新 Bearer 密钥；同时有 secret_key 则使用旧应用 OAuth。"""
    api_key = _credential(credentials, "api_key", "百度语音 API Key")
    secret_key = _credential(credentials, "secret_key", "百度应用 Secret Key", required=False)
    value = credentials.get("dev_pid", 80001)
    try:
        if isinstance(value, bool):
            raise ValueError
        dev_pid = int(value)
        if str(value) != str(dev_pid) or dev_pid not in _BAIDU_MODELS:
            raise ValueError
    except (ValueError, TypeError):
        raise ProviderError("百度语音模型编号无效，请选择80001、1537、1737、1637或1837。") from None
    pcm = _audio_frames(wav_bytes, max_seconds=60)
    endpoint = BAIDU_FAST_ENDPOINT if dev_pid == 80001 else BAIDU_STANDARD_ENDPOINT
    payload = {
        "format": "pcm", "rate": 16000, "channel": 1, "dev_pid": dev_pid,
        "cuid": "talkman-" + uuid.uuid4().hex, "len": len(pcm),
        "speech": base64.b64encode(pcm).decode("ascii"),
    }
    if secret_key:
        payload["token"] = _baidu_token(api_key, secret_key)
        headers = {}
    else:
        headers = {"Authorization": "Bearer " + api_key}
    result, _ = _json_post(endpoint, payload, headers, "百度语音")
    code = result.get("err_no", result.get("error_code"))
    if code != 0:
        if code in {6}:
            raise ProviderError("百度语音服务尚未授权，请在百度控制台开通对应语音识别能力。")
        if code in {14, 110, 111, 217, 3302}:
            raise ProviderError("百度语音鉴权失败，请检查密钥、服务授权及额度。")
        if code in {4, 17, 18, 19, 3304, 3305}:
            raise ProviderError("百度语音额度不足或请求过于频繁，请检查额度后重试。")
        if code in {3301, 3314}:
            raise ProviderError("百度语音录音不清晰或过短，请重新录音。")
        if code in {3308, 3310}:
            raise ProviderError("百度语音每次最多识别60秒，请分段录音。")
        raise ProviderError("百度语音识别未成功，请检查模型设置和服务开通状态。")
    candidates = result.get("result")
    text = candidates[0] if isinstance(candidates, list) and candidates else None
    if not isinstance(text, str) or not text.strip():
        raise ProviderError("百度语音没有返回有效文字，请重新录音。")
    return text.strip()
