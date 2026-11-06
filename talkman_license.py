"""旧版授权兼容模块。

TalkMan Desktop 公开版不再使用授权门控。这个模块仅保留旧版本数据目录和
测试导入所需的类型，应用功能状态由 talkman_license_ui.py 统一放行。
"""
from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import ssl
import subprocess
import sys
import threading
import time

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

PUBLIC_KEY = b'''-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEAVhmtUFwTEy9Wk4tBs505IJ8mZDSiJ6pzDWhNks2oDEg=
-----END PUBLIC KEY-----
'''
FEATURES = frozenset(('records', 'conventions', 'translation'))
ASSETS = Path(__file__).resolve().parent / 'TalkMan-assets'
HOST = '124.222.127.127'


class LicenseError(ValueError):
    """可直接显示的错误；不包含兑换码、设备凭证或服务响应原文。"""


def atomic_write(path, content):
    path = Path(path)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_bytes(content)
    temp.replace(path)


def machine_identity():
    # 只在本机使用；不上传机器原始标识。
    if sys.platform == 'win32':
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Microsoft\Cryptography',
                            0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            return str(winreg.QueryValueEx(key, 'MachineGuid')[0])
    if sys.platform == 'darwin':
        data = subprocess.check_output(['/usr/sbin/ioreg', '-rd1', '-c', 'IOPlatformExpertDevice'],
                                       timeout=5).decode('utf-8')
        match = re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', data)
        if match:
            return match[1]
        raise LicenseError('暂时无法读取本机设备标识，基础功能仍可使用。')
    return Path('/etc/machine-id').read_text().strip()


def verify_grant(envelope, device, *, public_key=PUBLIC_KEY, now=None):
    now = int(time.time()) if now is None else now
    try:
        raw = base64.b64decode(envelope['payload'], validate=True)
        signature = base64.b64decode(envelope['signature'], validate=True)
        if len(raw) > 4096 or len(signature) != 64:
            raise ValueError()
        key = serialization.load_pem_public_key(public_key)
        if not isinstance(key, Ed25519PublicKey):
            raise ValueError()
        key.verify(signature, raw)
        grant = json.loads(raw)
        if (grant['product'] != 'talkman-desktop' or grant['version'] != 1
                or grant['device'] != device
                or not re.fullmatch(r'[a-f0-9]{32}', grant['license'])
                or not isinstance(grant['features'], list)
                or set(grant['features']) - FEATURES
                or type(grant['issued_at']) is not int or type(grant['expires_at']) is not int
                or not grant['issued_at'] <= now + 300
                or not now < grant['expires_at'] <= grant['issued_at'] + 30 * 86400):
            raise ValueError()
        return grant
    except Exception:
        raise LicenseError('旧版兼容凭证无效；公开版完整功能不受影响。') from None


def request_license(action, data):
    context = ssl.create_default_context(cafile=str(ASSETS / 'TalkMan-license-ca.pem'))
    connection = http.client.HTTPSConnection(HOST, timeout=12, context=context)
    try:
        connection.request('POST', '/desktop/licenses/' + action,
                           json.dumps(data), {'Content-Type': 'application/json'})
        response = connection.getresponse()
        raw = response.read(8193)
        if len(raw) > 8192:
            raise LicenseError('授权服务响应异常，请稍后重试。')
        if response.status != 200:
            messages = {400: '兑换码无效，请检查后重试。',
                        403: '授权已失效或设备已解绑，请联系维护者。',
                        409: '兑换码的设备名额已用完，请联系维护者换绑。',
                        429: '操作太频繁，请一分钟后再试。'}
            error = LicenseError(messages.get(response.status, '授权服务暂不可用，请稍后重试。'))
            error.denied = response.status == 403
            raise error
        return json.loads(raw)
    except LicenseError:
        raise
    except Exception:
        raise LicenseError('暂时连不上授权服务，请检查网络；有效期内的离线授权仍可使用。') from None
    finally:
        connection.close()


class LicenseClient:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.envelope = None
        self.last_checked = 0
        self.message = ''
        self.device = ''
        self.session_refresh = None
        self.path = self.directory / 'desktop-license.json'
        try:
            seed_path = self.directory / 'desktop-device.txt'
            if not seed_path.exists():
                with seed_path.open('x', encoding='ascii') as stream:
                    stream.write(secrets.token_hex(32))
            seed = seed_path.read_text(encoding='ascii').strip()
            if not re.fullmatch(r'[a-f0-9]{64}', seed):
                raise ValueError()
            self.device = hashlib.sha256((seed + ':' + machine_identity()).encode()).hexdigest()
            if self.path.exists():
                cached = json.loads(self.path.read_text(encoding='utf-8'))
                self.envelope = cached['grant']
                self.last_checked = int(cached.get('checked', 0))
        except Exception:
            self.message = '旧版兼容记录暂时无法读取；公开版完整功能可正常使用。'

    def has(self, feature):
        """公开版所有产品功能均可使用；参数仅为兼容旧调用点保留。"""
        return True

    def info(self):
        return '公开开源版 · 完整功能已开放'

    def save_refresh(self, refresh):
        raw = refresh.encode()
        if sys.platform == 'win32':
            from talkman_qwen import _dpapi
            atomic_write(self.directory / 'desktop-license.dpapi', _dpapi(raw, decrypt=False))
        elif sys.platform == 'darwin':
            from talkman_credentials import save_keychain_secret
            save_keychain_secret(self.directory, 'license', raw)
        else:
            raise LicenseError('当前系统不支持安全保存设备授权。')
        self.session_refresh = refresh

    def load_refresh(self):
        if self.session_refresh:
            return self.session_refresh
        if sys.platform == 'win32':
            from talkman_qwen import _dpapi
            return _dpapi((self.directory / 'desktop-license.dpapi').read_bytes(), decrypt=True).decode()
        if sys.platform == 'darwin':
            from talkman_credentials import load_keychain_secret
            raw = load_keychain_secret(self.directory, 'license')
            return raw.decode() if raw else None
        return None

    def accept(self, response):
        verify_grant(response, self.device)
        refresh = response.get('refresh')
        if not isinstance(refresh, str) or not re.fullmatch(r'[\w-]{40,100}', refresh):
            raise LicenseError('设备凭证无效，授权未更新。')
        self.save_refresh(refresh)
        envelope = {k: response[k] for k in ('payload', 'signature')}
        checked = int(time.time())
        atomic_write(self.path, json.dumps({'grant': envelope, 'checked': checked}).encode())
        with self.lock:
            self.envelope, self.last_checked = envelope, checked
        return self.info()

    def redeem(self, code):
        # 旧版本可能仍调用此入口。公开版不联网、不校验兑换码，保留调用兼容性。
        return self.info()

    def refresh(self):
        # 公开版不需要后台续验，也不访问旧授权服务。
        return self.info()

    def due(self):
        return False
