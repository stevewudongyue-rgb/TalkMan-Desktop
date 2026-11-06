"""Talk Man 局域网配对协议；桌面应用提供模型和目标适配器。"""
from __future__ import annotations

import base64
import copy
from collections import deque
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import ipaddress
import json
import os
from pathlib import Path
import queue
import secrets
import socket
import ssl
import sys
import tempfile
import threading
import time
from urllib.parse import urlencode
import uuid
import wave


MAX_BODY = 2_700_000
MAX_TEXT = 40_000
MAX_INSTRUCTION = 4_000
PORT = 8765
SOCKET_TIMEOUT = 12
_session_base = None


class APIError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(message)


def checked_text(value, *, limit=MAX_TEXT, empty=True):
    if not isinstance(value, str) or len(value) > limit:
        raise APIError(400, "文字格式无效或超过长度限制。")
    if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise APIError(400, "文字含有不完整的 Unicode 字符。")
    if not empty and not value.strip():
        raise APIError(400, "请先输入文字。")
    return value


def utf16_length(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def utf16_range(text: str, start, length) -> tuple[int, int]:
    """把 UIKit 的 UTF-16 范围严格转换为 Python 字符范围。"""
    if type(start) is not int or type(length) is not int or start < 0 or length < 0:
        raise APIError(400, "光标或选区范围无效。")
    offsets = {0: 0}
    units = 0
    for index, char in enumerate(text):
        units += 2 if ord(char) > 0xFFFF else 1
        offsets[units] = index + 1
    if start not in offsets or start + length not in offsets:
        raise APIError(400, "光标不能落在 Unicode 字符内部，请重新选择。")
    return offsets[start], offsets[start + length]


def request_uuid(value) -> str:
    try:
        parsed = uuid.UUID(value) if isinstance(value, str) else None
    except (ValueError, AttributeError):
        parsed = None
    if parsed is None or str(parsed) != value.lower():
        raise APIError(400, "请求编号需要是 UUID。")
    return str(parsed)


def validate_wav(value, seconds: int) -> bytes:
    if not isinstance(value, str) or len(value) > MAX_BODY - 128:
        raise APIError(400, "录音数据无效或过大。")
    try:
        raw = base64.b64decode(value, validate=True)
        if len(raw) > seconds * 32_000 + 4096:
            raise ValueError
        with wave.open(io.BytesIO(raw), "rb") as audio:
            if (audio.getframerate(), audio.getnchannels(), audio.getsampwidth(), audio.getcomptype()) != (16000, 1, 2, "NONE"):
                raise ValueError
            frames = audio.getnframes()
            if not 0 < frames <= seconds * 16000:
                raise ValueError
            if len(audio.readframes(frames + 1)) != frames * 2:
                raise ValueError
        return raw
    except (ValueError, EOFError, wave.Error, OSError):
        raise APIError(400, f"需要 16kHz、单声道、16 位 PCM WAV，本服务本轮最多 {seconds} 秒。") from None


def model_data_dir() -> Path:
    if os.environ.get("TALKMAN_DATA_DIR"):
        return Path(os.environ["TALKMAN_DATA_DIR"])
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "TalkMan"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "TalkMan"
    return Path.home() / ".local" / "share" / "TalkMan"


def engine_directory() -> Path:
    bundled = Path(__file__).resolve().parent / "引擎"
    if (bundled / "talkman_qwen.py").is_file():
        return bundled
    source = Path(r"F:\LifeSmart\COLOLIGHT\TalkMan-原图桌面版\源码")
    if (source / "talkman_qwen.py").is_file():
        return source
    raise RuntimeError("未找到桌面引擎，请保留伴侣旁的「引擎」目录。")


def private_temporary_directory(prefix):
    """受限便携环境可回退到应用自己的缓存，不改系统目录权限。"""
    global _session_base
    if _session_base is None:
        try:
            directory = tempfile.TemporaryDirectory(prefix=prefix)
            _session_base = Path(directory.name).parent
            return directory
        except OSError:
            candidates = [model_data_dir() / "iPad伴侣" / "临时会话",
                          (Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent) / ".TalkMan-会话缓存"]
            for candidate in candidates:
                try:
                    candidate.mkdir(parents=True, exist_ok=True)
                    directory = tempfile.TemporaryDirectory(prefix=prefix, dir=str(candidate))
                    _session_base = candidate
                    return directory
                except OSError:
                    continue
            raise RuntimeError("系统临时目录和伴侣缓存目录均不可写，请把便携版放在可写文件夹。") from None
    return tempfile.TemporaryDirectory(prefix=prefix, dir=str(_session_base))


class ExistingEngine:
    """凭证只在用户启动伴侣或发起操作后读取，绝不发给 iPad。"""

    def __init__(self, data_dir: Path | None = None):
        engine = str(engine_directory())
        if engine not in sys.path:
            sys.path.insert(0, engine)
        import talkman_asr
        import talkman_qwen
        self.asr = talkman_asr
        self.qwen = talkman_qwen
        self.source_data_dir = data_dir or model_data_dir()
        self.data_dir = Path(os.environ.get("TALKMAN_COMPANION_DATA_DIR", str(model_data_dir() / "iPad伴侣")))
        self.qwen_session = None
        self.asr_session = None

    def _settings_source(self, filename):
        return self.data_dir if (self.data_dir / filename).is_file() else self.source_data_dir

    def snapshot(self):
        """启用动作冻结服务商和凭证；外部桌面改设置不会改变本次配对。"""
        frozen = copy.copy(self)
        frozen.qwen_session = copy.deepcopy(self.qwen_session or self.qwen.load_settings(
            self._settings_source(self.qwen.SETTINGS_FILENAME), allow_keychain_prompt=True))
        frozen.asr_session = copy.deepcopy(self.asr_session or self.asr.load_asr_settings(
            self._settings_source(self.asr.SETTINGS_FILE), allow_keychain_prompt=True))
        return frozen

    def status(self):
        # 状态和窗口枚举不读取 Key、不触发钥匙串确认、不请求模型。
        settings = self.asr_session or self.asr.load_asr_settings(self._settings_source(self.asr.SETTINGS_FILE), read_credentials=False)
        provider = self.asr.PROVIDERS.get(settings.provider)
        return {"provider_label": provider.label if provider else "尚未设置",
                "recording_seconds": min(60, provider.seconds) if provider else 60}

    def _client(self):
        settings = self.qwen_session or self.qwen.load_settings(self._settings_source(self.qwen.SETTINGS_FILENAME))
        if not settings.api_key:
            raise APIError(422, "请先在电脑 Talk Man 的设置中保存千问 Key，并启用本机安全记忆。")
        return self.qwen.QwenClient(settings)

    def transcribe(self, wav):
        settings = self.asr_session or self.asr.load_asr_settings(self._settings_source(self.asr.SETTINGS_FILE))
        if settings.provider == "qwen":
            return self._client().transcribe(wav)
        return self.asr.transcriber(settings)(wav)

    def transform(self, text, instruction):
        return self._client().transform(text, instruction)

    def edit_by_instruction(self, before, after, instruction):
        return self._client().edit_by_instruction(before, after, instruction)

    def safe_error(self, error):
        # 引擎的 QwenError 已过滤服务正文和凭证；其他异常不直接回传。
        return str(error) if isinstance(error, self.qwen.QwenError) else "电脑模型处理失败，原文已保留，请检查电脑设置。"


class ExistingTargets:
    def __init__(self, data_dir: Path):
        engine = str(engine_directory())
        if engine not in sys.path:
            sys.path.insert(0, engine)
        from talkman_target import TargetController
        self.controller = TargetController(data_dir)

    @staticmethod
    def _await(submit, timeout):
        received = queue.Queue(maxsize=1)
        submit(lambda state, value: received.put_nowait((state, value)))
        try:
            state, value = received.get(timeout=timeout)
        except queue.Empty:
            raise APIError(504, "电脑操作结果尚未确认，请检查目标窗口；请勿重复填入同一段文字。") from None
        if state != "ok":
            raise APIError(422, str(value))
        return value

    def refresh(self):
        return self._await(self.controller.refresh, 15)

    @staticmethod
    def label(window):
        return window.display_name()

    def fill(self, target, text):
        # 控制器后端枚举／捕获／填入始终在其唯一 worker 中完成。
        self.controller.select(target)
        return self._await(lambda callback: self.controller.fill(text, callback), 20)

    def close(self):
        self.controller.close()


class CompanionService:
    def __init__(self, engine, targets, *, token=None, computer_name=None, on_status=None):
        self.engine = engine
        self.targets = targets
        self.token = token or secrets.token_urlsafe(32)
        self.computer_name = computer_name or socket.gethostname()
        self.on_status = on_status or (lambda message: None)
        self.active = threading.Event()
        self.active.set()
        self._operation = threading.Lock()
        self._state = threading.Lock()
        self._target_map = {}
        # 会话期间不清除已使用的填入编号；满额后需要重新配对，避免重放。
        self._fill_requests = set()
        self._rate = {}

    def deactivate(self):
        self.active.clear()
        with self._state:
            self.token = ""
            self._target_map.clear()

    def authorize(self, header):
        if not self.active.is_set():
            raise APIError(503, "电脑连接已停用，请重新配对。")
        if not isinstance(header, str) or not header.startswith("Bearer "):
            raise APIError(401, "配对已失效，请重新配对。")
        supplied = header[7:]
        if not supplied.isascii() or len(supplied) > 256 or not hmac.compare_digest(supplied, self.token):
            raise APIError(401, "配对已失效，请重新配对。")

    def rate_limit(self, peer):
        now = time.monotonic()
        with self._state:
            bucket = self._rate.setdefault(peer, deque())
            while bucket and now - bucket[0] > 60:
                bucket.popleft()
            if len(bucket) >= 120:
                raise APIError(429, "操作过于频繁，请稍后再试。")
            bucket.append(now)
            if len(self._rate) > 256:
                self._rate = {peer: bucket}

    def call(self, method, path, payload=None):
        if not self.active.is_set():
            raise APIError(503, "电脑连接已停用。")
        if method == "GET" and path == "/v1/status":
            details = self.engine.status()
            return {"protocol": 1, "computer_name": self.computer_name,
                    "provider_label": checked_text(details["provider_label"], limit=100),
                    "text_provider": checked_text(details.get("text_provider", "千问"), limit=100),
                    "recording_seconds": max(1, min(60, int(details["recording_seconds"]))),
                    "app_version": checked_text(details.get("app_version", ""), limit=32),
                    "app_build": max(0, int(details.get("app_build", 0))),
                    "capabilities": details.get("capabilities", [])}
        if (method, path) not in {
            ("GET", "/v1/targets"), ("POST", "/v1/fill"), ("POST", "/v1/transcribe"),
            ("POST", "/v1/edit"), ("POST", "/v1/cleanup"), ("POST", "/v1/translate")
        }:
            raise APIError(404, "没有这个接口。")
        if method == "POST" and not isinstance(payload, dict):
            raise APIError(400, "请求需要是 JSON 对象。")
        if not self._operation.acquire(blocking=False):
            raise APIError(409, "电脑正在处理上一次操作，请等待完成。")
        try:
            if not self.active.is_set():
                raise APIError(503, "电脑连接已停用。")
            if path == "/v1/targets":
                # 先作废旧映射，即使刷新失败也不会使用旧窗口编号。
                with self._state:
                    self._target_map.clear()
                windows = self.targets.refresh()
                mapping = {str(uuid.uuid4()): window for window in windows[:100]}
                with self._state:
                    self._target_map = mapping
                return {"targets": [{"id": identifier, "label": checked_text(self.targets.label(window), limit=600)}
                                    for identifier, window in mapping.items()]}
            identifier = request_uuid(payload.get("request_id"))
            if path == "/v1/fill":
                return self._fill(identifier, payload)
            if path == "/v1/transcribe":
                seconds = min(60, int(self.engine.status()["recording_seconds"]))
                audio = validate_wav(payload.get("wav_base64"), seconds)
                self.on_status("正在识别 iPad 的录音…")
                text = checked_text(self.engine.transcribe(audio))
                return {"request_id": identifier, "text": text}
            text = checked_text(payload.get("text"))
            if path == "/v1/edit":
                return self._edit(identifier, text, payload)
            if path == "/v1/translate":
                return self._translate(identifier, text, payload)
            return self._cleanup(identifier, text, payload)
        except APIError:
            raise
        except Exception as error:
            raise APIError(502, self.engine.safe_error(error)) from None
        finally:
            self._operation.release()

    def _fill(self, identifier, payload):
        text = checked_text(payload.get("text"), empty=False)
        target_id = payload.get("target_id")
        if not isinstance(target_id, str):
            raise APIError(400, "请选择目标窗口。")
        with self._state:
            if identifier in self._fill_requests:
                raise APIError(409, "此填入请求已经处理或结果未知；请检查目标窗口，不会重复粘贴。")
            target = self._target_map.get(target_id)
            if target is None:
                raise APIError(409, "目标列表已经变化，请刷新并重新选择窗口。")
            if len(self._fill_requests) >= 10_000:
                raise APIError(429, "本次配对操作已达上限，请停用后重新配对。")
            # 副作用发生前登记；失败／超时也保留，服务器绝不自动重试。
            self._fill_requests.add(identifier)
        if not self.active.is_set():
            raise APIError(503, "电脑连接已停用，本次未提交。")
        self.on_status("正在向已选择的目标输入框填入…")
        receipt = self.targets.fill(target, text)
        self.on_status("已填入目标输入框，未发送。")
        return {"request_id": identifier, "state": "filled",
                "message": getattr(receipt, "message", "已填入，未发送"),
                "verified": bool(getattr(receipt, "send_token", ""))}

    def _edit(self, identifier, text, payload):
        start, end = utf16_range(text, payload.get("selection_start"), payload.get("selection_length"))
        instruction = checked_text(payload.get("instruction"), limit=MAX_INSTRUCTION, empty=False)
        self.on_status("正在按 iPad 的指令修改文字…")
        if start != end:
            if hasattr(self.engine, "edit_selection"):
                change = self.engine.edit_selection(text, start, end, instruction)
                if hasattr(change, "destination"):
                    if (change.start, change.end) != (start, end):
                        raise APIError(502, "模型返回的移动范围越过选区，原文已保留。")
                    result, start, end = change.apply(text)
                    result = checked_text(result)
                    return {"request_id": identifier, "text": result,
                            "selection_start": utf16_length(result[:start]),
                            "selection_length": utf16_length(result[start:end]),
                            "message": "已移动选区，可在 iPad 撤销"}
                if (type(change.start) is not int or type(change.end) is not int
                        or (change.start, change.end) != (start, end)):
                    raise APIError(502, "模型返回的修改范围越过选区，原文已保留。")
                replacement = checked_text(change.text)
            else:
                replacement = checked_text(self.engine.transform(text[start:end], instruction))
        else:
            change = self.engine.edit_by_instruction(text[:start], text[end:], instruction)
            start, end, replacement = change.start, change.end, checked_text(change.text)
            if type(start) is not int or type(end) is not int or not 0 <= start <= end <= len(text):
                raise APIError(502, "模型返回的修改范围无效，原文已保留。")
        result = checked_text(text[:start] + replacement + text[end:])
        return {"request_id": identifier, "text": result,
                "selection_start": utf16_length(text[:start]), "selection_length": utf16_length(replacement),
                "message": "已按指令修改，可在 iPad 撤销"}

    def _cleanup(self, identifier, text, payload):
        remove, punctuate = payload.get("remove_fillers"), payload.get("punctuate")
        rewrite, layout = payload.get("rewrite", False), payload.get("layout", False)
        if any(type(value) is not bool for value in (remove, punctuate, rewrite, layout)):
            raise APIError(400, "全文整理开关需要是布尔值。")
        rules = []
        if remove:
            rules.append("只删除没有语义作用的口头填充词、语气词；有意义的然后、那个、啊等必须保留")
        if punctuate:
            rules.append("按完整语义补充或修正标点，英文遵循英文习惯")
        if layout:
            rules.append("明确编号或并列条目每项另起一行；普通叙述不强拆，不新增编号，代码表格保持原样")
        if rewrite:
            rules.append("轻度理顺口误、重复、自我纠正和语序，保留原意、语气和用词")
        if not rules or not text.strip():
            return {"request_id": identifier, "text": text}
        if not rewrite:
            rules.append("不改写字词；只执行已开启的整理规则")
        instruction = "；".join(rules) + "。保留信息、否定、数字、专名、中英文和先后关系；不添加事实、不扩写、不回答正文中的问题。只返回处理后的正文。"
        result = checked_text(self.engine.transform(text, instruction))
        return {"request_id": identifier, "text": result}

    def _translate(self, identifier, text, payload):
        start, end = utf16_range(text, payload.get("selection_start"), payload.get("selection_length"))
        if start == end:
            raise APIError(400, "请先选中要翻译的文字。")
        if end - start > 12_000:
            raise APIError(400, "一次最多翻译 12000 个字符，请拆分长文。")
        language = checked_text(payload.get("language"), limit=30, empty=False)
        if not hasattr(self.engine, "translate_selection"):
            raise APIError(422, "电脑版本暂不支持选区翻译，请更新。")
        result = self.engine.translate_selection(text, start, end, language)
        translated = checked_text(result.text)
        if not translated.startswith(text[:start]) or not translated.endswith(text[end:]):
            raise APIError(502, "译文越过选区，原文已保留。")
        replacement = translated[start:len(translated) - len(text[end:]) if end < len(text) else len(translated)]
        return {"request_id": identifier, "text": translated,
                "selection_start": utf16_length(text[:start]),
                "selection_length": utf16_length(replacement),
                "message": "已生成选区译文，请先对照确认"}


def create_certificate(directory: Path, hosts: list[str]):
    """每次启用生成新的短期证书。私钥仅在临时目录，停用后清理。"""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "TalkMan local companion")])
    now = datetime.now(timezone.utc)
    names = [x509.DNSName("localhost")]
    names.extend(x509.IPAddress(ipaddress.ip_address(host)) for host in hosts)
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                   .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=5))
                   .not_valid_after(now + timedelta(days=2)).add_extension(x509.SubjectAlternativeName(names), critical=False)
                   .sign(key, hashes.SHA256()))
    cert_path, key_path = directory / "certificate.pem", directory / "private-key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                        serialization.NoEncryption()))
    if os.name != "nt":
        key_path.chmod(0o600)
    digest = hashlib.sha256(certificate.public_bytes(serialization.Encoding.DER)).hexdigest()
    return cert_path, key_path, digest


class CompanionHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True

    def __init__(self, address, service, context):
        self.service = service
        self.context = context
        self.capacity = threading.BoundedSemaphore(8)
        super().__init__(address, CompanionHandler)

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(SOCKET_TIMEOUT)
        try:
            secure = self.context.wrap_socket(connection, server_side=True, do_handshake_on_connect=False)
        except Exception:
            connection.close()
            raise
        return secure, address

    def process_request(self, request, client_address):
        if not self.capacity.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.capacity.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.capacity.release()

    def handle_error(self, request, client_address):
        # 默认 traceback 可能带请求信息；有意保持静默。
        pass


class CompanionHandler(BaseHTTPRequestHandler):
    server_version = "TalkMan"
    sys_version = ""
    protocol_version = "HTTP/1.0"

    def setup(self):
        self.request.settimeout(SOCKET_TIMEOUT)
        self.request.do_handshake()
        super().setup()

    def log_message(self, *args):
        pass

    def _json(self, status, payload):
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(encoded)
        self.close_connection = True

    def send_error(self, code, message=None, explain=None):
        self._json(code, {"error": "请求格式或访问方式不受支持。"})

    def _handle(self, method):
        try:
            if self.headers.get("Origin") is not None:
                raise APIError(403, "不接受来自网页的请求，请使用 Talk Man 应用。")
            if len(self.headers.get_all("Authorization", [])) != 1:
                raise APIError(401, "请先配对电脑。")
            self.server.service.authorize(self.headers.get("Authorization"))
            self.server.service.rate_limit(self.client_address[0])
            if self.headers.get("Transfer-Encoding"):
                raise APIError(400, "不支持分块请求。")
            payload = None
            if method == "POST":
                lengths = self.headers.get_all("Content-Length", [])
                if len(lengths) != 1 or not lengths[0].isdigit():
                    raise APIError(411, "请求需要一个有效的数据长度。")
                size = int(lengths[0])
                if size > MAX_BODY:
                    raise APIError(413, "请求过大，录音最多 60 秒、正文最多四万字。")
                if self.headers.get_content_type() != "application/json":
                    raise APIError(415, "请求需要使用 JSON。")
                raw = self.rfile.read(size)
                if len(raw) != size:
                    raise APIError(400, "请求数据不完整。")
                try:
                    payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                                         parse_constant=lambda value: (_ for _ in ()).throw(ValueError()))
                except (ValueError, UnicodeError):
                    raise APIError(400, "JSON 请求格式无效。") from None
            result = self.server.service.call(method, self.path, payload)
            self._json(200, result)
        except APIError as error:
            self._json(error.status, {"error": error.message})
        except (TimeoutError, socket.timeout):
            self._json(408, {"error": "请求超时，请检查连接。"})
        except Exception:
            self._json(500, {"error": "电脑暂时无法处理请求，原文已保留。"})

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_OPTIONS(self):
        self._json(403, {"error": "不接受网页跨域请求。"})


def _unique_object(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("重复字段")
        result[key] = value
    return result


class CompanionHost:
    def __init__(self, service, host, port=PORT):
        ipaddress.IPv4Address(host)
        self.service = service
        self.directory = private_temporary_directory(prefix="TalkMan-iPad-")
        try:
            cert, key, self.fingerprint = create_certificate(Path(self.directory.name), [host, "127.0.0.1"])
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(cert, key)
            # SSLContext 已载入证书；磁盘私钥无需保留到连接结束。
            key.unlink(missing_ok=True)
            cert.unlink(missing_ok=True)
            self.server = CompanionHTTPServer((host, port), service, context)
        except Exception:
            self.directory.cleanup()
            raise
        self.port = self.server.server_address[1]
        self.pairing_string = "talkman://pair?" + urlencode({"host": host, "port": self.port,
                                                          "token": service.token, "sha256": self.fingerprint})
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)
        self.thread.start()

    def close(self):
        self.service.deactivate()
        self.server.shutdown()
        self.server.server_close()
        self.service.targets.close()
        self.directory.cleanup()


def local_addresses():
    addresses = set()
    # UDP connect 只确定路由，不发应用数据。
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))
        addresses.add(probe.getsockname()[0])
    except OSError:
        pass
    finally:
        probe.close()
    try:
        for result in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addresses.add(result[4][0])
    except OSError:
        pass
    return sorted(address for address in addresses if not address.startswith("127.") and address != "0.0.0.0") or ["127.0.0.1"]


def main():
    import argparse
    import tkinter as tk
    from tkinter import ttk, messagebox
    arguments = argparse.ArgumentParser(description="Talk Man · iPad 桌面伴侣")
    arguments.add_argument("--engine-data-dir", type=Path, help="只读复用此桌面版设置目录；伴侣设置另行保存")
    arguments.add_argument("--smoke-test-report", type=Path, help="隐藏窗口安全冒烟验证，写入报告后退出；不读取 Key、不监听、不填入")
    options = arguments.parse_args()
    try:
        engine = ExistingEngine(options.engine_data_dir)
    except Exception:
        messagebox.showerror("缺少桌面引擎", "未找到引擎，请保留桌面伴侣旁的「引擎」目录。")
        return
    root = tk.Tk()
    root.title("Talk Man · iPad 桌面伴侣")
    root.geometry("660x600")
    root.minsize(600, 540)
    root.configure(bg="#080808")
    if options.smoke_test_report:
        root.withdraw()
        def no_credentials(*_args, **_kwargs):
            raise AssertionError("安全冒烟验证不允许读取凭证")
        engine.qwen.load_settings = no_credentials
        engine.asr.load_asr_settings = no_credentials
    frame = tk.Frame(root, bg="#080808", padx=26, pady=24)
    frame.pack(fill="both", expand=True)
    def label(text, **options):
        widget = tk.Label(frame, text=text, bg="#080808", fg="#eeeeee", anchor="w", justify="left", **options)
        widget.pack(fill="x", pady=(0, 12))
        return widget
    label("TALK MAN / iPad 桌面伴侣", font=("Microsoft YaHei UI", 18, "bold"))
    label("同一 Wi-Fi 配对。仅填入目标输入框，不代替你发送。")
    label("电脑局域网地址（选择 iPad 能访问的网卡）")
    ip = tk.StringVar(value=local_addresses()[0])
    box = ttk.Combobox(frame, textvariable=ip, values=local_addresses(), state="readonly")
    box.pack(fill="x", pady=(0, 14))
    pairing = tk.Text(frame, height=5, wrap="char", bg="#151515", fg="#dddddd", insertbackground="white",
                      relief="solid", borderwidth=1, padx=10, pady=8)
    pairing.pack(fill="x", pady=(0, 14))
    pairing.insert("1.0", "启用连接后，这里显示含证书指纹的一次性会话配对串。")
    pairing.configure(state="disabled")
    status = tk.StringVar(value="尚未启用。请先在桌面 Talk Man 保存模型设置和 Key。")
    tk.Label(frame, textvariable=status, bg="#080808", fg="#bbbbbb", wraplength=580, anchor="w",
             justify="left").pack(fill="x", pady=(0, 14))
    updates = queue.Queue()
    holder = {"host": None, "target_directory": None}
    def cleanup_target():
        directory = holder.get("target_directory")
        if directory:
            directory.cleanup()
            holder["target_directory"] = None
    def set_pair(value):
        pairing.configure(state="normal")
        pairing.delete("1.0", "end")
        pairing.insert("1.0", value)
        pairing.configure(state="disabled")
    def toggle():
        if holder["host"] is not None:
            host, holder["host"] = holder["host"], None
            host.close()
            cleanup_target()
            set_pair("已停用，旧配对串和令牌已经失效。")
            status.set("已停用；已提交到目标窗口的填入操作无法远程撤回。")
            start.configure(text="启用 iPad 连接")
            box.configure(state="readonly")
            return
        # 控制器配置独立于既有桌面数据；关闭时清除临时目标配置。
        try:
            holder["target_directory"] = private_temporary_directory(prefix="TalkMan-target-session-")
            target_data = Path(holder["target_directory"].name)
            targets = ExistingTargets(target_data)
            service = CompanionService(engine.snapshot(), targets, on_status=updates.put)
            holder["host"] = CompanionHost(service, ip.get())
            set_pair(holder["host"].pairing_string)
            details = service.engine.status()
            status.set(f"已启用 · {details['provider_label']} · 单轮最多 {details['recording_seconds']} 秒。请在 iPad 粘贴配对串。")
            start.configure(text="停用连接（旧配对立即失效）")
            box.configure(state="disabled")
        except Exception as error:
            if holder["host"] is not None:
                holder["host"].close()
                holder["host"] = None
            elif "targets" in locals():
                targets.close()
            cleanup_target()
            if isinstance(error, engine.qwen.QwenError):
                status.set(str(error))
            else:
                status.set("启用失败：检查依赖是否安装、8765 端口是否占用、电脑网卡是否可用。")
    start = tk.Button(frame, text="启用 iPad 连接", command=toggle, bg="#eeeeee", fg="#000000", relief="flat", pady=10)
    start.pack(fill="x", pady=(0, 10))
    def copy():
        if holder["host"] is None:
            status.set("请先启用连接。")
            return
        root.clipboard_clear()
        root.clipboard_append(holder["host"].pairing_string)
        status.set("配对串已复制，仅交给你自己的 iPad；请勿公开发送。")
    tk.Button(frame, text="复制配对串", command=copy, bg="#222222", fg="white", relief="flat", pady=8).pack(fill="x", pady=(0, 10))
    def settings_help():
        if holder["host"] is not None:
            messagebox.showinfo("先停用连接", "请先停用 iPad 连接，再修改模型设置，避免当前请求混用不同设置。")
            return
        settings_dialog(root, engine, status)
    tk.Button(frame, text="电脑模型设置 / 输入 Key", command=settings_help, bg="#222222", fg="white", relief="flat", pady=8).pack(fill="x")
    def poll():
        try:
            while True:
                status.set(updates.get_nowait())
        except queue.Empty:
            pass
        root.after(100, poll)
    def close():
        if holder["host"] is not None:
            holder["host"].close()
        cleanup_target()
        root.destroy()
    root.protocol("WM_DELETE_WINDOW", close)
    poll()
    if options.smoke_test_report:
        def smoke_report():
            try:
                import certifi
                import websocket
                if sys.platform == "win32":
                    import comtypes
                    import comtypes.gen.UIAutomationClient
                with private_temporary_directory(prefix="TalkMan-certificate-test-") as certificate_dir:
                    _cert, _key, fingerprint = create_certificate(Path(certificate_dir), ["127.0.0.1"])
                report = {"result": "通过", "frozen": bool(getattr(sys, "frozen", False)),
                          "engine_source_present": (engine_directory() / "talkman_qwen.py").is_file(),
                          "tk_window_created": True, "certificate_generated": len(fingerprint) == 64,
                          "service_started": holder["host"] is not None,
                          "credentials_read": False, "model_requested": False, "target_pasted": False}
            except Exception as error:
                # 本模式没有载入任何凭证；错误仅用于定位冻结依赖问题。
                report = {"result": "失败", "error_type": type(error).__name__, "error": str(error)}
                probes = []
                for directory in [Path(os.environ.get("TEMP", ".")), Path.cwd(), options.smoke_test_report.parent]:
                    probe_path = directory / ("TalkMan-test-" + uuid.uuid4().hex + ".tmp")
                    try:
                        descriptor = os.open(str(probe_path), os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
                        try:
                            os.write(descriptor, b"test")
                        finally:
                            os.close(descriptor)
                        probe_path.unlink()
                        probes.append({"directory": str(directory), "writable": True})
                    except OSError as probe_error:
                        probes.append({"directory": str(directory), "error": str(probe_error)})
                report["temporary_directory_probes"] = probes
            try:
                options.smoke_test_report.parent.mkdir(parents=True, exist_ok=True)
                options.smoke_test_report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            finally:
                close()
        root.after_idle(smoke_report)
    root.mainloop()


def settings_dialog(root, engine, status):
    """凭证可仅当前会话使用，也可由既有 DPAPI／钥匙串保存到伴侣独立目录。"""
    import tkinter as tk
    from tkinter import ttk, messagebox
    try:
        qwen = engine.qwen_session or engine.qwen.load_settings(engine._settings_source(engine.qwen.SETTINGS_FILENAME), allow_keychain_prompt=True)
        asr = engine.asr_session or engine.asr.load_asr_settings(engine._settings_source(engine.asr.SETTINGS_FILE), allow_keychain_prompt=True)
    except Exception:
        qwen, asr = engine.qwen.QwenSettings(), engine.asr.ASRSettings()
    dialog = tk.Toplevel(root)
    dialog.title("Talk Man · 电脑模型设置")
    dialog.geometry("640x650")
    dialog.transient(root)
    dialog.grab_set()
    body = ttk.Frame(dialog, padding=20)
    body.pack(fill="both", expand=True)
    ttk.Label(body, text="Key 仅在电脑使用，不发送到 iPad。\n此处保存到伴侣独立目录，不修改原桌面版配置。", justify="left").pack(anchor="w", pady=(0, 12))
    notebook = ttk.Notebook(body)
    notebook.pack(fill="both", expand=True)
    text_tab, voice_tab = ttk.Frame(notebook, padding=12), ttk.Frame(notebook, padding=12)
    notebook.add(text_tab, text="千问文字与语音")
    notebook.add(voice_tab, text="其他语音服务")
    values = {}
    fields = [("api_key", "千问 API Key", qwen.api_key, True),
              ("text_model", "文字模型", qwen.text_model, False),
              ("file_model", "录后识别模型", qwen.file_model, False),
              ("service_origin", "千问专属服务地址（HTTPS）", qwen.service_origin, False)]
    for name, title, initial, secret in fields:
        ttk.Label(text_tab, text=title).pack(anchor="w", pady=(8, 3))
        variable = tk.StringVar(value=initial)
        ttk.Entry(text_tab, textvariable=variable, show="•" if secret else "").pack(fill="x")
        values[name] = variable
    labels = {key: spec.label for key, spec in engine.asr.PROVIDERS.items()}
    ttk.Label(voice_tab, text="语音服务（文字修改始终使用千问）").pack(anchor="w", pady=(0, 4))
    provider = tk.StringVar(value=labels.get(asr.provider, "千问"))
    provider_box = ttk.Combobox(voice_tab, values=list(labels.values()), textvariable=provider, state="readonly")
    provider_box.pack(fill="x")
    provider_fields = ttk.Frame(voice_tab)
    provider_fields.pack(fill="both", expand=True)
    profiles = {key: dict(value) for key, value in asr.profiles.items()}
    visible = {}
    previous = {"provider": asr.provider}
    def render(_event=None):
        if visible:
            profiles[previous["provider"]] = {name: variable.get() for name, variable in visible.items()}
        visible.clear()
        for child in provider_fields.winfo_children():
            child.destroy()
        selected = next(key for key, value in labels.items() if value == provider.get())
        previous["provider"] = selected
        spec = engine.asr.PROVIDERS[selected]
        ttk.Label(provider_fields, text=spec.note, wraplength=525, justify="left").pack(anchor="w", pady=10)
        for item in spec.fields:
            ttk.Label(provider_fields, text=item.label).pack(anchor="w", pady=(7, 2))
            variable = tk.StringVar(value=profiles.get(selected, {}).get(item.name, item.default))
            ttk.Entry(provider_fields, textvariable=variable, show="•" if item.secret else "").pack(fill="x")
            visible[item.name] = variable
    provider_box.bind("<<ComboboxSelected>>", render)
    render()
    remember = tk.BooleanVar(value=False)
    ttk.Checkbutton(body, text="本机安全记忆 Key（Windows DPAPI / Mac 钥匙串）", variable=remember).pack(anchor="w", pady=12)
    ttk.Label(body, text="不开启安全记忆：Key 只在本次伴侣会话可用，退出后需重填。", wraplength=570).pack(anchor="w")
    def save():
        from dataclasses import replace
        try:
            profiles[previous["provider"]] = {name: variable.get() for name, variable in visible.items()}
            selected = next(key for key, value in labels.items() if value == provider.get())
            next_qwen = replace(qwen, **{name: variable.get().strip() for name, variable in values.items()}, voice_mode="file")
            next_asr = engine.asr.validate(engine.asr.ASRSettings(selected, profiles))
            engine.qwen.QwenClient(next_qwen)  # 仅本地格式验证，不发送模型请求。
            if not next_qwen.api_key:
                raise ValueError("请填写千问 Key；文字指令与全文整理需要它。")
            engine.qwen.save_settings(engine.data_dir, next_qwen, remember.get())
            engine.asr.save_asr_settings(engine.data_dir, next_asr, remember.get())
            engine.qwen_session, engine.asr_session = next_qwen, next_asr
            status.set("电脑模型设置已保存，可启用 iPad 连接。" if remember.get() else "模型 Key 仅当前会话可用，可启用 iPad 连接。")
            dialog.destroy()
        except Exception as error:
            safe = str(error) if isinstance(error, (ValueError, engine.qwen.QwenError)) else "设置保存失败，请检查系统安全存储和目录权限。"
            messagebox.showerror("无法保存设置", safe, parent=dialog)
    ttk.Button(body, text="保存设置", command=save).pack(fill="x", pady=(12, 0))


if __name__ == "__main__":
    main()
