"""Offline protocol/lifecycle tests. Never opens a device or a network socket."""

import json
import io
import queue
import threading
import time
import types
import unittest
import wave
from contextlib import nullcontext
from unittest.mock import patch

import talkman_voice as voice


class FakeTimeout(Exception):
    pass


def sentence(number, text, final=False, **extra):
    return {"sentence_id": number, "text": text, "sentence_end": final, **extra}


class FakeSocket:
    def __init__(self, results=None, task_finished=True):
        self.incoming = queue.Queue()
        self.sent = []
        self.task_id = None
        self.started_received = False
        self.closed = threading.Event()
        self.results = results if results is not None else [sentence(1, "测试。", True)]
        self.task_finished = task_finished
        self.on_binary = None

    def event(self, name, payload=None, **header):
        return json.dumps({"header": {"task_id": self.task_id, "event": name, **header},
                           "payload": payload or {}})

    def settimeout(self, value):
        self.timeout = value

    def send(self, raw):
        message = json.loads(raw)
        self.sent.append(message)
        action = message["header"]["action"]
        if action == "run-task":
            self.task_id = message["header"]["task_id"]
            self.incoming.put(self.event("task-started"))
        elif action == "finish-task":
            for result in self.results:
                self.incoming.put(self.event("result-generated", {"output": {"sentence": result}}))
            if self.task_finished:
                self.incoming.put(self.event("task-finished"))

    def send_binary(self, data):
        assert self.started_received, "Audio sent before task-started"
        assert not any(isinstance(item, dict) and item["header"]["action"] == "finish-task" for item in self.sent)
        self.sent.append(data)
        if self.on_binary:
            self.on_binary()

    def recv(self):
        try:
            raw = self.incoming.get(timeout=0.005)
        except queue.Empty:
            raise FakeTimeout()
        if raw and json.loads(raw)["header"]["event"] == "task-started":
            self.started_received = True
        return raw

    def shutdown(self):
        self.closed.set()


class FakeAudio:
    def __init__(self, count=1, fail=False):
        self.count = count
        self.fail = fail
        self.opened = threading.Event()
        self.closed = threading.Event()
        self.stream = None

    def query_devices(self, **kwargs):
        return {"index": 4, "max_input_channels": 1, "default_samplerate": 48000.0}

    def CoreAudioSettings(self, **kwargs):
        return kwargs

    def RawInputStream(self, **kwargs):
        self.kwargs = kwargs
        owner = self

        class Stream:
            def start(self):
                owner.opened.set()
                if owner.fail:
                    raise RuntimeError("device failure")
                for _ in range(owner.count):
                    kwargs["callback"](b"\x01\x00" * 1600, 1600, None, False)

            def abort(self):
                owner.closed.set()

            def close(self):
                owner.closed.set()

        self.stream = Stream()
        return self.stream


class FakeMacAudio(FakeAudio):
    def __init__(self, *, query_error=None, start_error=None):
        super().__init__()
        self.query_error = query_error
        self.start_error = start_error
        self.device = {"index": 4, "max_input_channels": 2, "default_samplerate": 48000.0}
        self.conversion = None

    def query_devices(self, **kwargs):
        self.query = kwargs
        if self.query_error:
            raise self.query_error
        return self.device

    def CoreAudioSettings(self, **kwargs):
        self.conversion = kwargs
        return kwargs

    def RawInputStream(self, **kwargs):
        stream = super().RawInputStream(**kwargs)
        if self.start_error:
            def fail():
                self.opened.set()
                raise self.start_error
            stream.start = fail
        return stream


class TranscriptTests(unittest.TestCase):
    def test_corrections_duplicate_final_and_legitimate_repetition(self):
        transcript = voice._Transcript()
        self.assertEqual(transcript.update(sentence(1, "你")), ("partial", "你"))
        self.assertEqual(transcript.update(sentence(1, "你好")), ("partial", "你好"))
        self.assertEqual(transcript.update(sentence(1, "你好。", True)), ("final", "你好。"))
        self.assertIsNone(transcript.update(sentence(1, "你好。", True)))
        self.assertIsNone(transcript.update(sentence(1, "旧的临时结果")))
        self.assertEqual(transcript.update(sentence(2, "你好。", True)), ("final", "你好。你好。"))
        self.assertEqual(transcript.final_text, "你好。你好。")

    def test_heartbeat_and_out_of_order_sentences(self):
        transcript = voice._Transcript()
        self.assertIsNone(transcript.update({"heartbeat": True, "sentence_id": 0}))
        transcript.update(sentence(2, "乙。", True))
        transcript.update(sentence(1, "甲。", True))
        self.assertEqual(transcript.final_text, "甲。乙。")


class _SessionFixture(unittest.TestCase):
    def setUp(self):
        self.sessions = []
        self.patches = []
        # 生命周期测试使用假设备，不读取运行测试的 Mac 的真实麦克风权限。
        self.patch("talkman_voice._mac_microphone_permission", return_value=3)

    def tearDown(self):
        for session in self.sessions:
            session.cancel()
            if session._worker:
                session._worker.join(1)
        for value in reversed(self.patches):
            value.stop()

    def patch(self, *args, **kwargs):
        value = patch(*args, **kwargs)
        self.patches.append(value)
        return value.start()

    def make(self, socket=None, audio=None, connector=None, key="fake-offline-key"):
        socket = socket or FakeSocket()
        audio = audio or FakeAudio()
        calls = []

        def connect(url, **kwargs):
            calls.append((url, kwargs))
            self.assertTrue(audio.opened.is_set())
            return connector() if connector else socket

        fake_ws = types.SimpleNamespace(create_connection=connect, WebSocketTimeoutException=FakeTimeout)
        modules = patch.dict("sys.modules", {"sounddevice": audio, "websocket": fake_ws})
        self.patches.append(modules)
        modules.start()
        events = []
        session = voice.VoiceSession(key, voice.DEFAULT_MODEL, lambda kind, text: events.append((kind, text)))
        self.sessions.append(session)
        return session, socket, audio, events, calls

    def wait(self, predicate, timeout=1.5):
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() >= deadline:
                self.fail("Timed out waiting for the simulated session")
            time.sleep(0.005)

    def done(self, session):
        self.wait(lambda: session.finished)
        session._worker.join(0.5)


class SessionTests(_SessionFixture):
    def test_prebuffer_quick_release_and_late_finals(self):
        gate = threading.Event()
        socket = FakeSocket([sentence(1, "首"), sentence(1, "首句。", True),
                             sentence(1, "首句。", True), sentence(2, "末句。", True)])
        session, socket, audio, events, calls = self.make(socket, connector=lambda: (gate.wait(1), socket)[1])
        try:
            session.start()
            self.assertTrue(audio.opened.wait(1))
            session.stop()
            self.assertTrue(audio.closed.wait(0.3), "Release must stop the microphone while connecting")
            gate.set()
            self.done(session)
        finally:
            gate.set()
        self.assertEqual(events[-1], ("done", "首句。末句。"))
        self.assertEqual(sum(kind == "final" for kind, _ in events), 2)
        self.assertEqual(sum(isinstance(item, bytes) for item in socket.sent), 1)
        run, finish = [item for item in socket.sent if isinstance(item, dict)]
        self.assertEqual(run["payload"], {"task_group": "audio", "task": "asr", "function": "recognition",
                                         "model": voice.DEFAULT_MODEL, "parameters": {"format": "pcm", "sample_rate": 16000}, "input": {}})
        self.assertEqual(finish["header"]["action"], "finish-task")
        self.assertEqual(finish["header"]["task_id"], run["header"]["task_id"])
        self.assertEqual(finish["payload"], {"input": {}})
        self.assertEqual(calls[0][0], voice.ASR_URL)
        self.assertEqual(calls[0][1]["header"]["Authorization"], "Bearer fake-offline-key")
        self.assertTrue(socket.closed.is_set())
        self.assertEqual(session._api_key, "")

    def test_cancel_during_connect_releases_microphone_and_late_socket(self):
        gate = threading.Event()
        socket = FakeSocket()
        session, _, audio, events, _ = self.make(socket, connector=lambda: (gate.wait(1), socket)[1])
        try:
            session.start()
            self.assertTrue(audio.opened.wait(1))
            session.cancel()
            self.done(session)
            self.assertTrue(audio.closed.is_set())
            self.assertEqual(events, [])
            gate.set()
            self.assertTrue(socket.closed.wait(0.5))
        finally:
            gate.set()

    def test_finish_timeout_does_not_commit_partial_or_final_early(self):
        self.patch("talkman_voice.FINISH_TIMEOUT", 0.05)
        session, socket, _, events, _ = self.make(FakeSocket(task_finished=False))
        session.start()
        self.wait(lambda: any(kind == "ready" for kind, _ in events))
        session.stop()
        self.done(session)
        self.assertEqual(events[-1][0], "error")
        self.assertIn("最终", events[-1][1])
        self.assertFalse(any(kind == "done" for kind, _ in events))
        self.assertTrue(socket.closed.is_set())

    def test_empty_speech_is_clear_error(self):
        session, _, _, events, _ = self.make(FakeSocket(results=[]))
        session.start()
        self.wait(lambda: any(kind == "ready" for kind, _ in events))
        session.stop()
        self.done(session)
        self.assertEqual(events[-1][0], "error")
        self.assertIn("没有识别到语音", events[-1][1])

    def test_unconfirmed_tail_is_not_committed(self):
        session, _, _, events, _ = self.make(FakeSocket([sentence(1, "确认。", True), sentence(2, "未确认")]))
        session.start()
        self.wait(lambda: any(kind == "ready" for kind, _ in events))
        session.stop()
        self.done(session)
        self.assertEqual(events[-1][0], "error")
        self.assertFalse(any(kind == "done" for kind, _ in events))

    def test_disconnect_is_error(self):
        session, socket, audio, events, _ = self.make()
        socket.on_binary = lambda: socket.incoming.put("")
        session.start()
        self.done(session)
        self.assertEqual(events[-1][0], "error")
        self.assertIn("断开", events[-1][1])
        self.assertTrue(audio.closed.is_set())

    def test_limit_stops_and_finishes_with_bounded_last_chunk(self):
        self.patch("talkman_voice.MAX_RECORD_SECONDS", 0.05)
        session, socket, audio, events, _ = self.make()
        session.start()
        self.done(session)
        self.assertEqual(session.stop_reason, "limit")
        self.assertEqual(events[-1], ("done", "测试。"))
        self.assertEqual(sum(len(item) for item in socket.sent if isinstance(item, bytes)), 1600)
        self.assertTrue(audio.closed.is_set())

    def test_queue_overflow_fails_explicitly(self):
        self.patch("talkman_voice.MAX_QUEUED_BLOCKS", 1)
        session, _, audio, events, _ = self.make(audio=FakeAudio(count=2))
        session.start()
        self.done(session)
        self.assertEqual(events[-1][0], "error")
        self.assertIn("缓冲已满", events[-1][1])
        self.assertTrue(audio.closed.is_set())

    def test_remote_error_and_credentials_are_not_forwarded(self):
        session, socket, _, events, _ = self.make()
        socket.on_binary = lambda: socket.incoming.put(socket.event("task-failed", error_code="InvalidApiKey", error_message="fake-offline-key private audio"))
        session.start()
        self.done(session)
        self.assertEqual(events[-1][0], "error")
        self.assertIn("API Key", events[-1][1])
        self.assertNotIn("fake-offline-key", repr(events))
        self.assertNotIn("private audio", repr(events))

    def test_explicit_workspace_endpoint(self):
        session, _, _, events, calls = self.make()
        endpoint = "wss://ws-offline-example.cn-beijing.maas.aliyuncs.com/api-ws/v1/inference"
        session.endpoint = endpoint
        session.start()
        self.wait(lambda: any(kind == "ready" for kind, _ in events))
        session.stop()
        self.done(session)
        self.assertEqual(calls[0][0], endpoint)

    def test_release_before_start_does_not_open_device_or_network(self):
        session, _, audio, events, calls = self.make()
        session.stop()
        session.start()
        self.done(session)
        self.assertEqual(events[-1][0], "error")
        self.assertIn("没有录到音频", events[-1][1])
        self.assertFalse(audio.opened.is_set())
        self.assertEqual(calls, [])

    def test_start_timeout_releases_microphone(self):
        self.patch("talkman_voice.START_TIMEOUT", 0.03)
        session, socket, audio, events, _ = self.make()
        # Simulate timeout events, not a disconnected socket.
        def timeout_recv():
            time.sleep(0.005)
            raise FakeTimeout()
        socket.recv = timeout_recv
        session.start()
        self.done(session)
        self.assertIn("启动超时", events[-1][1])
        self.assertTrue(audio.closed.is_set())
        self.assertFalse(any(isinstance(item, bytes) for item in socket.sent))

    def test_microphone_failure_and_empty_key(self):
        for fail, key, expected in ((True, "fake-offline-key", "麦克风"), (False, "", "API Key")):
            with self.subTest(fail=fail, key=key):
                session, _, audio, events, calls = self.make(audio=FakeAudio(fail=fail), key=key)
                session.start()
                self.done(session)
                self.assertEqual(events[-1][0], "error")
                self.assertIn(expected, events[-1][1])
                self.assertEqual(calls, [])
                if fail:
                    self.assertTrue(audio.closed.is_set())


class MacMicrophoneTests(_SessionFixture):
    def mac(self, status=3):
        self.patch("talkman_voice.sys.platform", "darwin")
        return self.patch("talkman_voice._mac_microphone_permission", return_value=status)

    def test_native_48k_device_uses_coreaudio_conversion_keeps_16k_protocol(self):
        self.mac()
        session, socket, audio, events, _ = self.make(audio=FakeMacAudio())
        session.start()
        self.wait(lambda: any(kind == "ready" for kind, _ in events))
        session.stop()
        self.done(session)
        self.assertEqual(events[-1], ("done", "测试。"))
        self.assertEqual(audio.query, {"kind": "input"})
        self.assertEqual(audio.kwargs["device"], 4)
        self.assertEqual(audio.kwargs["samplerate"], 16000)
        self.assertEqual(audio.conversion, {"change_device_parameters": False,
                                          "fail_if_conversion_required": False,
                                          "conversion_quality": "max"})
        run = next(item for item in socket.sent if isinstance(item, dict))
        self.assertEqual(run["payload"]["parameters"]["sample_rate"], 16000)

    def test_denied_and_restricted_permission_never_open_device_or_connect(self):
        for status in (1, 2):
            with self.subTest(status=status):
                self.mac(status)
                session, _, audio, events, calls = self.make(audio=FakeMacAudio())
                session.start()
                self.done(session)
                self.assertIn("隐私与安全性", events[-1][1])
                self.assertIn("Talk Man", events[-1][1])
                self.assertFalse(audio.opened.is_set())
                self.assertEqual(calls, [])

    def test_first_time_denial_after_start_closes_device_and_never_uploads(self):
        permission = self.mac()
        permission.side_effect = (0, 2)
        session, _, audio, events, calls = self.make(audio=FakeMacAudio())
        session.start()
        self.done(session)
        self.assertIn("未允许麦克风", events[-1][1])
        self.assertTrue(audio.closed.is_set())
        self.assertEqual(calls, [])

    def test_no_default_input_is_a_clear_error(self):
        self.mac()
        session, _, audio, events, calls = self.make(audio=FakeMacAudio(query_error=RuntimeError("device -1")))
        session.start()
        self.done(session)
        self.assertIn("没有可用的默认麦克风", events[-1][1])
        self.assertIn("声音 → 输入", events[-1][1])
        self.assertFalse(audio.opened.is_set())
        self.assertEqual(calls, [])

    def test_output_only_device_does_not_start_recording(self):
        self.mac()
        audio = FakeMacAudio()
        audio.device["max_input_channels"] = 0
        session, _, _, events, calls = self.make(audio=audio)
        session.start()
        self.done(session)
        self.assertIn("没有录音通道", events[-1][1])
        self.assertFalse(audio.opened.is_set())
        self.assertEqual(calls, [])

    def test_audio_error_is_actionable_and_does_not_forward_raw_message(self):
        self.mac()
        error = RuntimeError("private arbitrary exception text", -9997)
        session, _, audio, events, calls = self.make(audio=FakeMacAudio(start_error=error))
        session.start()
        self.done(session)
        self.assertIn("16 kHz", events[-1][1])
        self.assertIn("内置麦克风", events[-1][1])
        self.assertNotIn("private", events[-1][1])
        self.assertTrue(audio.closed.is_set())
        self.assertEqual(calls, [])

    def test_not_determined_permission_guides_first_authorization(self):
        self.mac(0)
        session, _, audio, events, calls = self.make(audio=FakeMacAudio(start_error=RuntimeError("start")))
        session.start()
        self.done(session)
        self.assertIn("尚未完成麦克风授权", events[-1][1])
        self.assertIn("TalkMan.app", events[-1][1])
        self.assertTrue(audio.closed.is_set())
        self.assertEqual(calls, [])

    def test_unavailable_authorization_reader_does_not_block_valid_device(self):
        self.mac(None)
        session, _, _, events, _ = self.make(audio=FakeMacAudio())
        session.start()
        self.wait(lambda: any(kind == "ready" for kind, _ in events))
        session.stop()
        self.done(session)
        self.assertEqual(events[-1], ("done", "测试。"))

    def test_release_or_cancel_while_mac_opens_microphone_never_connects(self):
        self.mac()
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                gate = threading.Event()
                audio = FakeMacAudio()
                open_stream = audio.RawInputStream

                def delayed_stream(**kwargs):
                    stream = open_stream(**kwargs)
                    start = stream.start

                    def wait_then_start():
                        audio.opened.set()
                        gate.wait(1)
                        start()

                    stream.start = wait_then_start
                    return stream

                audio.RawInputStream = delayed_stream
                session, _, _, events, calls = self.make(audio=audio)
                try:
                    session.start()
                    self.assertTrue(audio.opened.wait(1))
                    (session.cancel if cancel else session.stop)()
                    gate.set()
                    self.done(session)
                    self.assertTrue(audio.closed.is_set())
                    self.assertEqual(calls, [])
                    if cancel:
                        self.assertEqual(events, [])
                    else:
                        self.assertIn("没有录到音频", events[-1][1])
                finally:
                    gate.set()


class MicrophonePermissionTests(unittest.TestCase):
    def foundation(self, status):
        state = types.SimpleNamespace(status=status, callbacks=[], queries=[])

        class CaptureDevice:
            @staticmethod
            def authorizationStatusForMediaType_(media):
                state.queries.append(media)
                return state.status

            @staticmethod
            def requestAccessForMediaType_completionHandler_(media, callback):
                self.assertEqual(media, "audio")
                state.callbacks.append(callback)

        foundation = types.SimpleNamespace(AVMediaTypeAudio="audio", AVCaptureDevice=CaptureDevice)
        bridge = types.SimpleNamespace(autorelease_pool=nullcontext)
        return state, {"AVFoundation": foundation, "objc": bridge}

    def test_windows_never_imports_or_requests_mac_permission(self):
        with patch("talkman_voice.sys.platform", "win32"), patch.dict("sys.modules", {"AVFoundation": None}):
            self.assertTrue(voice.prepare_microphone_permission(lambda *_: self.fail("Unexpected callback")))

    def test_first_request_returns_false_even_when_approved_and_requires_new_press(self):
        state, modules = self.foundation(0)
        results = []
        with patch("talkman_voice.sys.platform", "darwin"), patch.dict("sys.modules", modules):
            self.assertFalse(voice.prepare_microphone_permission(lambda *result: results.append(result)))
            self.assertEqual(results, [])
            self.assertEqual(len(state.callbacks), 1)
            state.status = 3
            state.callbacks[0](True)
            self.assertEqual(results, [(True, "已允许麦克风，请重新按住录音。")])
            self.assertTrue(voice.prepare_microphone_permission(lambda *_: self.fail("Repeated authorization")))
            self.assertEqual(len(state.callbacks), 1)

    def test_first_request_denial_reports_settings_and_never_authorizes_recording(self):
        state, modules = self.foundation(0)
        results = []
        with patch("talkman_voice.sys.platform", "darwin"), patch.dict("sys.modules", modules):
            self.assertFalse(voice.prepare_microphone_permission(lambda *result: results.append(result)))
            state.status = 2
            state.callbacks[0](False)
            self.assertFalse(results[0][0])
            self.assertIn("隐私与安全性", results[0][1])
            with self.assertRaises(voice.VoicePermissionError):
                voice.prepare_microphone_permission(lambda *_: self.fail("Repeated authorization"))
            self.assertEqual(len(state.callbacks), 1)

    def test_previously_authorized_does_not_request_again(self):
        state, modules = self.foundation(3)
        with patch("talkman_voice.sys.platform", "darwin"), patch.dict("sys.modules", modules):
            self.assertTrue(voice.prepare_microphone_permission(lambda *_: self.fail("Unexpected callback")))
            self.assertEqual(state.callbacks, [])

    def test_previously_denied_or_restricted_raise_displayable_error(self):
        for status in (1, 2):
            with self.subTest(status=status):
                state, modules = self.foundation(status)
                with patch("talkman_voice.sys.platform", "darwin"), patch.dict("sys.modules", modules):
                    with self.assertRaisesRegex(voice.VoicePermissionError, "Mac 未允许麦克风"):
                        voice.prepare_microphone_permission(lambda *_: self.fail("Unexpected callback"))
                    self.assertEqual(state.callbacks, [])

    def test_missing_framework_prevents_recording_with_build_hint(self):
        with patch("talkman_voice.sys.platform", "darwin"), patch.dict("sys.modules", {"AVFoundation": None}):
            with self.assertRaisesRegex(voice.VoicePermissionError, "重新生成"):
                voice.prepare_microphone_permission(lambda *_: self.fail("Unexpected callback"))

    def test_first_request_requires_main_thread(self):
        state, modules = self.foundation(0)
        with patch("talkman_voice.sys.platform", "darwin"), patch.dict("sys.modules", modules), \
                patch("talkman_voice.threading.current_thread", return_value=object()):
            with self.assertRaisesRegex(voice.VoicePermissionError, "程序窗口"):
                voice.prepare_microphone_permission(lambda *_: self.fail("Unexpected callback"))
            self.assertEqual(state.callbacks, [])


class FileSessionTests(_SessionFixture):
    def make_file(self, transcribe=None, audio=None):
        audio = audio or FakeAudio()
        modules = patch.dict("sys.modules", {"sounddevice": audio})
        self.patches.append(modules)
        modules.start()
        requests = []

        def capture(data):
            self.assertTrue(audio.closed.is_set(), "HTTP starts only after microphone release")
            requests.append(data)
            return transcribe(data) if transcribe else "录后识别。"

        events = []
        session = voice.FileVoiceSession("fake-offline-key", "qwen3-asr-flash",
                                         lambda kind, text: events.append((kind, text)), capture)
        self.sessions.append(session)
        return session, audio, events, requests

    def test_file_wav_format_and_stop(self):
        session, audio, events, requests = self.make_file()
        session.start()
        self.wait(lambda: any(kind == "ready" for kind, _ in events))
        session.stop()
        self.done(session)
        self.assertEqual(events[-1], ("done", "录后识别。"))
        with wave.open(io.BytesIO(requests[0]), "rb") as wav:
            self.assertEqual((wav.getframerate(), wav.getnchannels(), wav.getsampwidth()), (16000, 1, 2))
            self.assertEqual(wav.getnframes(), 1600)
            self.assertEqual(wav.readframes(1600), b"\x01\x00" * 1600)

    def test_file_mac_uses_same_device_check_and_16k_wav(self):
        self.patch("talkman_voice.sys.platform", "darwin")
        self.patch("talkman_voice._mac_microphone_permission", return_value=3)
        session, audio, events, requests = self.make_file(audio=FakeMacAudio())
        session.start()
        self.wait(lambda: any(kind == "ready" for kind, _ in events))
        session.stop()
        self.done(session)
        self.assertEqual(events[-1][0], "done")
        self.assertEqual(audio.kwargs["device"], 4)
        with wave.open(io.BytesIO(requests[0]), "rb") as wav:
            self.assertEqual((wav.getframerate(), wav.getnchannels(), wav.getsampwidth()), (16000, 1, 2))

    def test_file_mac_denied_permission_never_uploads(self):
        self.patch("talkman_voice.sys.platform", "darwin")
        self.patch("talkman_voice._mac_microphone_permission", return_value=2)
        session, audio, events, requests = self.make_file(audio=FakeMacAudio())
        session.start()
        self.done(session)
        self.assertIn("未允许麦克风", events[-1][1])
        self.assertFalse(audio.opened.is_set())
        self.assertEqual(requests, [])

    def test_file_cancel_ignores_late_http_response(self):
        gate = threading.Event()
        session, _, events, requests = self.make_file(lambda _: (gate.wait(1), "迟到的文本")[1])
        try:
            session.start()
            self.wait(lambda: any(kind == "ready" for kind, _ in events))
            session.stop()
            self.wait(lambda: bool(requests))
            session.cancel()
            self.done(session)
            gate.set()
            self.assertFalse(any(kind in ("done", "error") for kind, _ in events))
        finally:
            gate.set()

    def test_cancel_during_wav_close_does_not_start_upload(self):
        session, _, events, requests = self.make_file()
        original_open = wave.open

        class CancelOnClose:
            def __init__(self, *args, **kwargs):
                self.output = original_open(*args, **kwargs)

            def __enter__(self):
                return self.output.__enter__()

            def __exit__(self, *args):
                result = self.output.__exit__(*args)
                session.cancel()
                return result

        self.patch("talkman_voice.wave.open", side_effect=CancelOnClose)
        session.start()
        self.wait(lambda: any(kind == "ready" for kind, _ in events))
        session.stop()
        self.done(session)
        self.assertEqual(requests, [])
        self.assertFalse(any(kind in ("done", "error") for kind, _ in events))

    def test_file_timeout_and_empty_result(self):
        self.patch("talkman_voice.FILE_TRANSCRIBE_TIMEOUT", 0.03)
        gate = threading.Event()
        try:
            for transcribe, expected in ((lambda _: gate.wait(1), "超时"), (lambda _: "", "没有识别到语音")):
                session, _, events, _ = self.make_file(transcribe)
                session.start()
                self.wait(lambda: any(kind == "ready" for kind, _ in events))
                session.stop()
                self.done(session)
                self.assertEqual(events[-1][0], "error")
                self.assertIn(expected, events[-1][1])
        finally:
            gate.set()

    def test_file_limit_and_cancel_before_upload(self):
        self.patch("talkman_voice.MAX_RECORD_SECONDS", 0.05)
        session, audio, events, requests = self.make_file()
        session.start()
        self.done(session)
        self.assertEqual(session.stop_reason, "limit")
        self.assertEqual(events[-1][0], "done")
        with wave.open(io.BytesIO(requests[0]), "rb") as wav:
            self.assertEqual(wav.getnframes(), 800)
        session, _, events, requests = self.make_file()
        session.cancel()
        session.start()
        self.done(session)
        self.assertEqual(events, [])
        self.assertEqual(requests, [])

    def test_file_adapter_errors_keep_safe_explanation_only(self):
        from talkman_qwen import QwenError

        for error, expected in ((QwenError("账户额度不足，请充值后重试。"), "账户额度不足，请充值后重试。"),
                                (RuntimeError("fake-offline-key private audio"), "千问语音识别失败")):
            def fail(_):
                raise error

            session, _, events, _ = self.make_file(fail)
            session.start()
            self.wait(lambda: any(kind == "ready" for kind, _ in events))
            session.stop()
            self.done(session)
            self.assertEqual(events[-1][0], "error")
            self.assertIn(expected, events[-1][1])
            self.assertNotIn("fake-offline-key", repr(events))
            self.assertNotIn("private audio", repr(events))


if __name__ == "__main__":
    unittest.main()
