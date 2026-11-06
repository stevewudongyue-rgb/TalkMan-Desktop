"""Qwen streaming ASR; microphone data is kept only in a bounded memory queue.

Protocol sources (checked 2026-10-09):
https://platform.qianwenai.com/docs/api-reference/speech-recognition/qwen-audio-asr-streaming/client-events
https://platform.qianwenai.com/docs/api-reference/speech-recognition/qwen-audio-asr-streaming/server-events

Callbacks run on the session worker, never on the Tk thread. A successful task
ends with ``done``; a failed/empty task ends with ``error``. Cancellation emits
neither. Only ``done`` text is safe to commit to the document.
"""

from __future__ import annotations

import json
import io
import queue
import sys
import threading
import time
import uuid
import wave
from typing import Callable


ASR_URL = "wss://maas.qianwenaiapi.com/api-ws/v1/inference"
DEFAULT_MODEL = "qwen-audio-3.0-asr-flash-streaming"
SAMPLE_RATE = 16000
BLOCK_FRAMES = 1600  # 100 ms, mono signed 16-bit PCM.
MAX_RECORD_SECONDS = 120
MAX_QUEUED_BLOCKS = 100  # Ten seconds / 320 KB, including connection pre-roll.
CONNECT_TIMEOUT = 8.0
START_TIMEOUT = 8.0
FINISH_TIMEOUT = 10.0
SOCKET_TIMEOUT = 0.2
CLEANUP_TIMEOUT = 1.0
FILE_TRANSCRIBE_TIMEOUT = 65.0


class _VoiceError(Exception):
    """An already sanitized, user-facing error."""


class _Cancelled(Exception):
    pass


class VoicePermissionError(_VoiceError):
    """A microphone authorization failure safe to show in the UI."""


MAC_MICROPHONE_DENIED = "Mac 未允许麦克风：请在「系统设置 → 隐私与安全性 → 麦克风」开启 Talk Man，然后重新打开程序。"


def _mac_microphone_permission() -> int | None:
    """Read Apple's public authorization status; never change permissions."""
    if sys.platform != "darwin":
        return None
    try:
        import AVFoundation
        import objc

        with objc.autorelease_pool():
            status = AVFoundation.AVCaptureDevice.authorizationStatusForMediaType_(AVFoundation.AVMediaTypeAudio)
        return status if status in (0, 1, 2, 3) else None
    except Exception:
        return None


def prepare_microphone_permission(on_result: Callable[[bool, str], None]) -> bool:
    """Run on the UI thread before recording; first authorization never records.

    Return True if recording may start now. A first-time Mac request returns
    False and reports its result on Apple's callback thread; callers must queue
    that callback before touching UI widgets and require a new recording press.
    """
    if sys.platform != "darwin":
        return True
    status = _mac_microphone_permission()
    if status == 3:
        return True
    if status in (1, 2):
        raise VoicePermissionError(MAC_MICROPHONE_DENIED)
    if status != 0:
        raise VoicePermissionError("Mac 麦克风权限组件不可用，请使用最新构建包重新生成并通过 TalkMan.app 启动。")
    if threading.current_thread() is not threading.main_thread():
        raise VoicePermissionError("Mac 首次麦克风授权需要从程序窗口发起，请重新按录音按钮。")
    try:
        import AVFoundation
        import objc

        def completed(allowed):
            message = "已允许麦克风，请重新按住录音。" if allowed else MAC_MICROPHONE_DENIED
            try:
                on_result(bool(allowed), message)
            except Exception:
                pass

        with objc.autorelease_pool():
            AVFoundation.AVCaptureDevice.requestAccessForMediaType_completionHandler_(
                AVFoundation.AVMediaTypeAudio, completed,
            )
    except Exception:
        raise VoicePermissionError("Mac 麦克风授权请求失败，请通过 TalkMan.app 重新打开程序后重试。") from None
    return False


def _check_mac_microphone_permission() -> int | None:
    status = _mac_microphone_permission()
    if status in (1, 2):
        raise _VoiceError(MAC_MICROPHONE_DENIED)
    return status


def _microphone_error(error: Exception) -> str:
    if sys.platform != "darwin":
        return "无法打开麦克风，请检查默认输入设备、麦克风权限及设备占用。"
    status = _mac_microphone_permission()
    if status in (1, 2):
        return MAC_MICROPHONE_DENIED
    if status == 0:
        return "Mac 尚未完成麦克风授权：请允许系统的麦克风请求，随后重新按住录音；请通过 TalkMan.app 启动。"
    code = error.args[1] if len(error.args) > 1 and isinstance(error.args[1], int) else None
    if code in (-9996, -9998):
        return "Mac 默认输入设备不可用：请在「系统设置 → 声音 → 输入」选择可用麦克风后重试。"
    if code == -9997:
        return "Mac 当前麦克风无法转换为 16 kHz 语音：请在「系统设置 → 声音 → 输入」切换内置麦克风后重试。"
    suffix = f"（音频错误码 {code}）" if code is not None else ""
    return "Mac 无法打开麦克风：请检查「声音 → 输入」及「隐私与安全性 → 麦克风」，关闭占用设备的程序后重试。" + suffix


class _Transcript:
    """Replace each sentence hypothesis; never concatenate repeated hypotheses."""

    def __init__(self) -> None:
        self.sentences: dict[int, tuple[str, bool]] = {}

    @property
    def text(self) -> str:
        return "".join(self.sentences[key][0] for key in sorted(self.sentences))

    @property
    def final_text(self) -> str:
        return "".join(text for _, (text, final) in sorted(self.sentences.items()) if final)

    @property
    def has_unfinished_text(self) -> bool:
        return any(text.strip() and not final for text, final in self.sentences.values())

    def update(self, sentence: dict) -> tuple[str, str] | None:
        if sentence.get("heartbeat") is True:
            return None
        number = sentence.get("sentence_id")
        text = sentence.get("text")
        if not isinstance(number, int) or isinstance(number, bool) or number < 1 or not isinstance(text, str):
            raise _VoiceError("语音服务返回了无法识别的句子数据，请重试。")
        final = sentence.get("sentence_end") is True
        previous = self.sentences.get(number)
        if previous == (text, final) or (previous and previous[1] and not final):
            return None
        self.sentences[number] = (text, final)
        if not text and not final:
            return None
        return ("final" if final else "partial", self.text)


def _service_error(code: str, message: str = "") -> str:
    # Never pass remote error text through: it can include credentials or input.
    hint = (str(code) + " " + str(message)).lower()
    if any(word in hint for word in ("apikey", "api_key", "api key", "unauthoriz", "authentication", "invalidkey")):
        return "千问 API Key 无效或已失效，请在语音设置中检查。"
    if any(word in hint for word in ("quota", "balance", "arrears", "payment", "insufficient")):
        return "千问账户额度不足或服务未开通，请检查账户额度和语音模型权限。"
    if any(word in hint for word in ("forbidden", "accessdenied", "permission", "not authorized")):
        return "千问拒绝访问此语音模型，请检查 API Key 的模型权限。"
    if any(word in hint for word in ("rate", "throttl", "too many")):
        return "千问语音请求过于频繁，请稍后重试。"
    if any(word in hint for word in ("model", "unsupported")):
        return "语音模型不可用，请检查模型名称和账户权限。"
    if any(word in hint for word in ("timeout", "timed out")):
        return "语音识别等待超时，请检查网络后重试。"
    return "千问语音识别失败，请检查网络、账户额度和模型权限后重试。"


class VoiceSession:
    """One press-to-talk session. start/stop/cancel never wait for network I/O.

    ``on_event(kind, payload)`` receives strings only. ``partial`` and ``final``
    contain the current complete transcript, and ``done`` contains confirmed
    final text. ``stop_reason`` is ``user``, ``limit``, or ``cancel`` after stop.
    """

    def __init__(self, api_key: str, model: str, on_event: Callable[[str, str], None], endpoint: str = ASR_URL):
        self._api_key = api_key.strip()
        self.model = model.strip() or DEFAULT_MODEL
        self.endpoint = endpoint
        self.max_record_seconds = MAX_RECORD_SECONDS
        self._on_event = on_event
        self._stop = threading.Event()
        self._cancel = threading.Event()
        self._finished = threading.Event()
        self._started = False
        self._state_lock = threading.Lock()
        self._audio: queue.Queue[bytes] = queue.Queue(MAX_QUEUED_BLOCKS)
        self._audio_problem: str | None = None
        self._frames = 0
        self._stop_reason = ""
        self._worker: threading.Thread | None = None
        self._transcript = _Transcript()

    @property
    def stop_reason(self) -> str:
        return self._stop_reason

    @property
    def finished(self) -> bool:
        return self._finished.is_set()

    def start(self) -> None:
        with self._state_lock:
            if self._started:
                return
            self._started = True
            self._worker = threading.Thread(target=self._run, name="TalkMan-ASR", daemon=True)
            self._worker.start()

    def stop(self) -> None:
        with self._state_lock:
            if not self._stop_reason:
                self._stop_reason = "user"
            self._stop.set()

    def cancel(self) -> None:
        self._cancel.set()
        self._stop_reason = "cancel"
        self._stop.set()

    def _emit(self, kind: str, text: str) -> None:
        if not self._cancel.is_set():
            try:
                self._on_event(kind, text)
            except Exception:
                # A closing UI must not escape into a thread traceback/log.
                self._cancel.set()
                self._stop.set()

    def _check_cancel(self) -> None:
        if self._cancel.is_set():
            raise _Cancelled()

    def _capture(self, indata, frames: int, _timing, status) -> None:
        if self._stop.is_set() or self._cancel.is_set():
            return
        if status:
            self._audio_problem = "麦克风录音中断或音频缓冲溢出，请关闭占用麦克风的程序后重试。"
            self._stop.set()
            return
        remaining = max(0, int(self.max_record_seconds * SAMPLE_RATE) - self._frames)
        count = min(frames, remaining)
        if count:
            try:
                self._audio.put_nowait(bytes(indata)[:count * 2])
            except queue.Full:
                self._audio_problem = "网络发送速度跟不上录音，音频缓冲已满。本轮已停止，请检查网络后重试。"
                self._stop.set()
                return
            self._frames += count
        if count < frames or self._frames >= int(self.max_record_seconds * SAMPLE_RATE):
            self._stop_reason = "limit"
            self._stop.set()

    def _open_microphone(self, sounddevice):
        stream = None
        try:
            settings = {}
            if sys.platform == "darwin":
                _check_mac_microphone_permission()
                try:
                    device = sounddevice.query_devices(kind="input")
                except Exception:
                    raise _VoiceError("Mac 没有可用的默认麦克风：请在「系统设置 → 声音 → 输入」选择设备后重试。") from None
                if device.get("max_input_channels", 0) < 1:
                    raise _VoiceError("Mac 默认设备没有录音通道：请在「系统设置 → 声音 → 输入」选择麦克风。")
                # Core Audio performs high-quality conversion from the device's
                # native rate, keeping the ASR wire format at 16 kHz.
                settings["device"] = device["index"]
                settings["extra_settings"] = sounddevice.CoreAudioSettings(
                    change_device_parameters=False, fail_if_conversion_required=False,
                    conversion_quality="max",
                )
            stream = sounddevice.RawInputStream(
                samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                blocksize=BLOCK_FRAMES, callback=self._capture, **settings,
            )
            stream.start()
            _check_mac_microphone_permission()
            self._check_cancel()
            if self._stop.is_set() and not self._frames:
                raise _VoiceError("没有录到音频，请在允许麦克风后重新按住录音，说话后再松开。" if sys.platform == "darwin"
                                  else "没有录到音频，请按住语音按钮说话后再松开。")
            return stream
        except (_VoiceError, _Cancelled):
            self._dispose_stream(stream)
            raise
        except Exception as error:
            self._dispose_stream(stream)
            raise _VoiceError(_microphone_error(error)) from None

    @staticmethod
    def _dispose_socket(connection) -> None:
        if connection is not None:
            try:
                # shutdown closes immediately, unlike close's peer-handshake wait.
                connection.shutdown()
            except Exception:
                pass

    @staticmethod
    def _dispose_stream(stream) -> None:
        if stream is None:
            return

        def release() -> None:
            try:
                stream.abort()
            except Exception:
                pass
            finally:
                try:
                    stream.close()
                except Exception:
                    pass

        closer = threading.Thread(target=release, name="TalkMan-MicClose", daemon=True)
        closer.start()
        closer.join(CLEANUP_TIMEOUT)

    def _connect(self, websocket, on_wait: Callable[[], None]):
        # DNS resolution can outlive a socket timeout. Keep it off the recording
        # worker so cancellation can release the microphone promptly regardless.
        completed = threading.Event()
        abandoned = threading.Event()
        lock = threading.Lock()
        result: list = []
        api_key = self._api_key

        def connect() -> None:
            try:
                value = websocket.create_connection(
                    self.endpoint, header={"Authorization": "Bearer " + api_key},
                    timeout=CONNECT_TIMEOUT, enable_multithread=True, redirect_limit=0,
                )
                if hasattr(value, "getstatus") and value.getstatus() != 101:
                    self._dispose_socket(value)
                    value = _VoiceError("语音服务连接地址返回异常，请检查设置。")
            except Exception as error:
                value = error
            with lock:
                if not abandoned.is_set():
                    result.append(value)
                    completed.set()
                    return
            if not isinstance(value, Exception):
                self._dispose_socket(value)

        threading.Thread(target=connect, name="TalkMan-ASRConnect", daemon=True).start()
        deadline = time.monotonic() + CONNECT_TIMEOUT
        try:
            while not completed.wait(0.02):
                self._check_cancel()
                on_wait()
                if self._audio_problem:
                    raise _VoiceError(self._audio_problem)
                if time.monotonic() >= deadline:
                    raise _VoiceError("连接千问语音服务超时，请检查网络后重试。")
            self._check_cancel()
            value = result.pop()
            if isinstance(value, Exception):
                code = getattr(value, "status_code", None)
                if code == 401:
                    raise _VoiceError("千问 API Key 无效或已失效，请在语音设置中检查。")
                if code == 403:
                    raise _VoiceError("千问拒绝访问语音服务，请检查 API Key 和模型权限。")
                if code == 429:
                    raise _VoiceError("千问语音请求过于频繁或额度不足，请稍后重试。")
                raise _VoiceError("无法连接千问语音服务，请检查网络、代理和 API Key。")
            return value
        finally:
            with lock:
                abandoned.set()
                orphan = result.pop() if result else None
            if orphan is not None and not isinstance(orphan, Exception):
                self._dispose_socket(orphan)

    def _receive(self, connection, websocket, task_id: str) -> str | None:
        try:
            raw = connection.recv()
        except websocket.WebSocketTimeoutException:
            return None
        if raw is None or raw == "" or raw == b"":
            raise _VoiceError("语音服务连接已断开，本轮未完成，请检查网络后重试。")
        if not isinstance(raw, str):
            raise _VoiceError("语音服务返回了无法识别的数据，请重试。")
        try:
            event = json.loads(raw)
            header = event["header"]
            if header.get("task_id") != task_id:
                return None
            name = header.get("event")
            if name == "task-failed":
                raise _VoiceError(_service_error(header.get("error_code", ""), header.get("error_message", "")))
            if name == "result-generated":
                sentence = event["payload"]["output"]["sentence"]
                update = self._transcript.update(sentence)
                if update:
                    self._emit(*update)
            return name
        except (KeyError, TypeError, ValueError, AttributeError):
            raise _VoiceError("语音服务响应格式不正确，请重试。") from None

    def _run(self) -> None:
        connection = None
        stream = None
        terminal: tuple[str, str] | None = None
        try:
            self._check_cancel()
            if not self._api_key:
                raise _VoiceError("请先在语音设置中填写千问 API Key。")
            if self._stop.is_set():
                raise _VoiceError("没有录到音频，请按住语音按钮说话后再松开。")
            try:
                import sounddevice
                import websocket
            except (ImportError, OSError):
                raise _VoiceError("语音组件未安装完整，请重新安装 Talk Man。") from None
            if not self._stop.is_set():
                stream = self._open_microphone(sounddevice)
            recording_started = time.monotonic()

            def stop_microphone() -> None:
                nonlocal stream
                if self._stop.is_set() and stream is not None:
                    self._dispose_stream(stream)
                    stream = None
                    if not self._frames:
                        raise _VoiceError("没有录到音频，请按住语音按钮说话后再松开。")

            connection = self._connect(websocket, stop_microphone)
            self._check_cancel()
            connection.settimeout(SOCKET_TIMEOUT)
            task_id = str(uuid.uuid4())
            connection.send(json.dumps({
                "header": {"action": "run-task", "task_id": task_id, "streaming": "duplex"},
                "payload": {"task_group": "audio", "task": "asr", "function": "recognition",
                            "model": self.model, "parameters": {"format": "pcm", "sample_rate": SAMPLE_RATE},
                            "input": {}},
            }))
            started_deadline = time.monotonic() + START_TIMEOUT
            task_started = False
            finish_deadline: float | None = None
            while True:
                self._check_cancel()
                if self._audio_problem:
                    raise _VoiceError(self._audio_problem)
                now = time.monotonic()
                if not self._stop.is_set() and now - recording_started >= self.max_record_seconds:
                    self._stop_reason = "limit"
                    self._stop.set()
                stop_microphone()
                if not task_started and now >= started_deadline:
                    raise _VoiceError("千问语音任务启动超时，请检查网络和模型权限后重试。")
                if finish_deadline is not None and now >= finish_deadline:
                    raise _VoiceError("等待最终识别结果超时，本轮未提交，请检查网络后重试。")
                if task_started and finish_deadline is None:
                    # Bound each pass, allowing incoming results and cancellation
                    # even while the recording queue contains many pre-roll blocks.
                    for _ in range(10):
                        self._check_cancel()
                        try:
                            audio = self._audio.get_nowait()
                        except queue.Empty:
                            break
                        connection.send_binary(audio)
                    if self._stop.is_set() and self._audio.empty():
                        connection.send(json.dumps({
                            "header": {"action": "finish-task", "task_id": task_id, "streaming": "duplex"},
                            "payload": {"input": {}},
                        }))
                        finish_deadline = time.monotonic() + FINISH_TIMEOUT
                event = self._receive(connection, websocket, task_id)
                if event == "task-started" and not task_started:
                    task_started = True
                    self._emit("ready", "正在聆听" if not self._stop.is_set() else "正在完成识别")
                elif event == "task-finished":
                    if finish_deadline is None:
                        raise _VoiceError("语音任务提前结束，本轮未完成，请重试。")
                    if self._transcript.has_unfinished_text:
                        raise _VoiceError("语音服务未确认完整的最终结果，本轮未提交，请重试。")
                    text = self._transcript.final_text.strip()
                    if not text:
                        raise _VoiceError("没有识别到语音，请检查麦克风并靠近麦克风说话后重试。")
                    terminal = ("done", text)
                    break
        except _Cancelled:
            pass
        except _VoiceError as error:
            terminal = ("error", str(error))
        except Exception:
            terminal = ("error", "语音连接或录音发生异常，本轮未完成，请检查网络和麦克风后重试。")
        finally:
            self._stop.set()
            self._dispose_stream(stream)
            self._dispose_socket(connection)
            self._api_key = ""
            while not self._audio.empty():
                try:
                    self._audio.get_nowait()
                except queue.Empty:
                    break
            self._finished.set()
            if terminal:
                self._emit(*terminal)


class FileVoiceSession(VoiceSession):
    """Record to an in-memory WAV and pass it to the supplied HTTP adapter.

    The callable is run off the recording worker with a finite wait. Cancellation
    releases the microphone and suppresses any result still in flight; it cannot
    recall audio that the adapter has already sent to the service.
    """

    def __init__(self, api_key: str, model: str, on_event: Callable[[str, str], None],
                 transcribe: Callable[[bytes], str], *, max_seconds: float | None = None,
                 request_timeout: float | None = None):
        super().__init__(api_key, model, on_event)
        self._transcribe = transcribe
        self.max_record_seconds = MAX_RECORD_SECONDS if max_seconds is None else max_seconds
        self.request_timeout = FILE_TRANSCRIBE_TIMEOUT if request_timeout is None else request_timeout
        if not 0 < self.max_record_seconds <= 120 or not 0 < self.request_timeout <= 120:
            raise ValueError("录音时长或识别等待时长无效。")

    def _transcribe_wav(self, wav_bytes: bytes) -> str:
        self._check_cancel()
        result: queue.Queue = queue.Queue(1)
        transcribe = self._transcribe

        def request() -> None:
            try:
                self._check_cancel()
                result.put((True, transcribe(wav_bytes)))
            except _Cancelled:
                return
            except Exception as error:
                # Our HTTP adapter's QwenError is explicitly safe to display;
                # arbitrary exceptions and remote text still stay private.
                from talkman_qwen import QwenError

                message = str(error) if isinstance(error, QwenError) else _service_error(type(error).__name__, str(error))
                result.put((False, message))

        threading.Thread(target=request, name="TalkMan-ASRFile", daemon=True).start()
        deadline = time.monotonic() + self.request_timeout
        while True:
            self._check_cancel()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _VoiceError("等待语音识别结果超时，本轮未提交，请检查网络后重试。")
            try:
                success, text = result.get(timeout=min(0.02, remaining))
            except queue.Empty:
                continue
            self._check_cancel()
            if not success:
                raise _VoiceError(text)
            if not isinstance(text, str):
                raise _VoiceError("语音服务返回了无法识别的文本，请重试。")
            return text.strip()

    def _run(self) -> None:
        stream = None
        terminal: tuple[str, str] | None = None
        recording = bytearray()
        try:
            self._check_cancel()
            if not self._api_key:
                raise _VoiceError("请先在语音设置中填写千问 API Key。")
            try:
                import sounddevice
            except (ImportError, OSError):
                raise _VoiceError("语音组件未安装完整，请重新安装 Talk Man。") from None
            if not self._stop.is_set():
                stream = self._open_microphone(sounddevice)
            self._emit("ready", "正在聆听" if not self._stop.is_set() else "正在完成识别")
            recording_started = time.monotonic()
            while True:
                self._check_cancel()
                if self._audio_problem:
                    raise _VoiceError(self._audio_problem)
                if not self._stop.is_set() and time.monotonic() - recording_started >= self.max_record_seconds:
                    self._stop_reason = "limit"
                    self._stop.set()
                if self._stop.is_set():
                    self._dispose_stream(stream)
                    stream = None
                try:
                    recording.extend(self._audio.get(timeout=0.02))
                except queue.Empty:
                    if self._stop.is_set():
                        break
                if len(recording) > int(self.max_record_seconds * SAMPLE_RATE * 2):
                    raise _VoiceError("录音超过本轮时长上限，请重新录音。")
            self._dispose_stream(stream)
            stream = None
            self._check_cancel()
            if not recording:
                raise _VoiceError("没有录到音频，请按住语音按钮说话后再松开。")
            buffer = io.BytesIO()
            with wave.open(buffer, "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(SAMPLE_RATE)
                output.writeframes(recording)
            recording.clear()
            text = self._transcribe_wav(buffer.getvalue())
            buffer.close()
            if not text:
                raise _VoiceError("没有识别到语音，请检查麦克风并靠近麦克风说话后重试。")
            terminal = ("done", text)
        except _Cancelled:
            pass
        except _VoiceError as error:
            terminal = ("error", str(error))
        except Exception:
            terminal = ("error", "语音录制或识别发生异常，本轮未完成，请检查网络和麦克风后重试。")
        finally:
            self._stop.set()
            self._dispose_stream(stream)
            self._api_key = ""
            recording.clear()
            while not self._audio.empty():
                try:
                    self._audio.get_nowait()
                except queue.Empty:
                    break
            self._finished.set()
            if terminal:
                self._emit(*terminal)
