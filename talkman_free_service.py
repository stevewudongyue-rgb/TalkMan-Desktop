"""封闭测试的免费在线通道；只持有设备令牌，不接触供应商密钥。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from urllib import request, error

from talkman_credentials import load_keychain_secret, save_keychain_secret, CredentialError
from talkman_qwen import QwenClient, QwenSettings, QwenError, _dpapi, _atomic_write, _NoRedirect

NOTICE = '免费服务受服务方政策和额度影响，可能调整、暂停或不可用。'
PROFILE = 'free-service.dpapi'


def save_profile(directory, profile):
    """仅供内测开通工具调用；配置不进入安装包。"""
    data = json.dumps(profile).encode('utf-8')
    if sys.platform == 'darwin':
        save_keychain_secret(Path(directory), 'free', data)
        _atomic_write(Path(directory) / 'free-service.enabled', b'1')
    elif os.name == 'nt':
        _atomic_write(Path(directory) / PROFILE, _dpapi(data, decrypt=False))
    else:
        raise QwenError('本轮免费内测支持 Windows 和 Mac。')


class FreeService:
    def __init__(self, directory):
        self.directory = Path(directory)
        self._lock = threading.Lock()
        self._process = None
        self._port = None
        self._profile = None
        self._closed = False
        self._text_retry_at = 0

    @property
    def configured(self):
        name = 'free-service.enabled' if sys.platform == 'darwin' else PROFILE
        return (self.directory / name).is_file()

    def _load_profile(self):
        if self._profile is not None:
            return self._profile
        try:
            if sys.platform != 'darwin' and (self.directory / PROFILE).stat().st_size > 65536:
                raise ValueError
            data = (load_keychain_secret(self.directory, 'free') if sys.platform == 'darwin'
                    else _dpapi((self.directory / PROFILE).read_bytes(), decrypt=True))
            value = json.loads(data)
            if (not isinstance(value, dict) or not re.fullmatch(r'[a-zA-Z0-9.-]+', value['host'])
                    or not re.fullmatch(r'[a-zA-Z0-9_-]+', value['user'])
                    or not re.fullmatch(r'[a-zA-Z0-9_-]{32,256}', value['token'])
                    or any(not Path(value[k]).is_file() for k in ('identity', 'known_hosts'))):
                raise ValueError
            self._profile = value
            return value
        except (OSError, ValueError, TypeError, KeyError, CredentialError):
            raise QwenError('这台电脑的免费内测连接未开通或已失效，请联系维护者；也可切回自己的模型。') from None

    def _connect(self):
        with self._lock:
            if self._closed:
                raise QwenError('免费连接已关闭，请重新打开 Talk Man。')
            profile = self._load_profile()
            if self._process is not None and self._process.poll() is None:
                return self._port, profile['token']
            executable = shutil.which('ssh')
            if not executable:
                raise QwenError('系统缺少 OpenSSH 客户端，请启用后再使用免费内测连接。')
            with socket.socket() as reservation:
                reservation.bind(('127.0.0.1', 0))
                port = reservation.getsockname()[1]
            args = [executable, '-F', os.devnull, '-N', '-T', '-i', profile['identity'],
                    '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes', '-o', 'IdentityAgent=none',
                    '-o', 'StrictHostKeyChecking=yes', '-o', 'UserKnownHostsFile="'+Path(profile['known_hosts']).as_posix()+'"',
                    '-o', 'GlobalKnownHostsFile=none', '-o', 'ExitOnForwardFailure=yes',
                    '-o', 'ConnectTimeout=8', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=2',
                    '-L', f'127.0.0.1:{port}:127.0.0.1:8787', profile['user']+'@'+profile['host']]
            try:
                process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                self._process = process
                deadline = time.monotonic()+10
                while not self._closed and process.poll() is None and time.monotonic() < deadline:
                    try:
                        with socket.create_connection(('127.0.0.1', port), timeout=.15):
                            if self._closed:
                                break
                            self._process, self._port = process, port
                            return port, profile['token']
                    except OSError:
                        time.sleep(.05)
                self._stop_process(process)
                self._process, self._port = None, None
            except OSError:
                pass
            raise QwenError('免费内测连接暂不可用，请检查网络或联系维护者；原文已保留。')

    def call(self, path, data=None, *, audio=False):
        if path not in ('/v1/status', '/v1/chat/completions', '/v1/audio/transcriptions'):
            raise QwenError('免费服务请求不受支持。')
        if path == '/v1/chat/completions' and time.monotonic() < self._text_retry_at:
            raise QwenError('免费文字服务正在繁忙，请稍后重试；已录入的文字保留。')
        port, token = self._connect()
        body = data if audio or data is None else json.dumps(data, ensure_ascii=False).encode('utf-8')
        req = request.Request(f'http://127.0.0.1:{port}'+path, data=body,
            headers={'Authorization': 'Bearer '+token, 'Content-Type': 'audio/wav' if audio else 'application/json'})
        try:
            opener = request.build_opener(request.ProxyHandler({}), _NoRedirect())
            with opener.open(req, timeout=55) as response:
                raw = response.read(2*1024*1024+1)
                if len(raw) > 2*1024*1024:
                    raise QwenError('免费服务回复过大，原文已保留。')
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise ValueError
                return result
        except error.HTTPError as exc:
            status = exc.code
            exc.close()
            if status == 503 and path == '/v1/chat/completions':
                self._text_retry_at = time.monotonic()+30
                raise QwenError('免费文字服务暂不可用或繁忙，请稍后重试；已录入的文字保留。') from None
            messages = {401: '免费内测凭证无效，请联系维护者。', 403: '本轮免费内测已到期，请联系维护者续期。',
                        429: '免费测试额度已用完或请求过于频繁，请稍后再试。',
                        413: '本次内容超过免费测试限制，请缩短录音或文字。',
                        504: '免费模型响应超时，请稍后再试。'}
            raise QwenError(messages.get(status, '免费模型暂不可用，原文已保留。')) from None
        except QwenError:
            raise
        except (OSError, ValueError, error.URLError):
            raise QwenError('免费服务连接或回复异常，原文已保留。') from None

    def close(self):
        # 连接等待发生在 worker 中；退出界面不等待网络锁。
        self._closed = True
        process = self._process
        self._profile = None
        if process is not None:
            threading.Thread(target=self._stop_process, args=(process,), daemon=True).start()

    @staticmethod
    def _stop_process(process):
        try:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)
        except OSError:
            pass


class FreeClient(QwenClient):
    """沿用严格的选区编辑、移动、翻译结果校验，只更换请求通道。"""
    def __init__(self, service):
        super().__init__(QwenSettings())
        self.service = service

    def _request_json(self, path, payload, **kwargs):
        return self.service.call('/v1/chat/completions',
            {'messages': payload['messages'], 'max_tokens': payload.get('max_tokens', 4096)})

    def transcribe(self, wav_bytes):
        result = self.service.call('/v1/audio/transcriptions', wav_bytes, audio=True)
        text = result.get('text')
        if not isinstance(text, str) or not text.strip():
            raise QwenError('未识别到可用文字，原文已保留。')
        return text
