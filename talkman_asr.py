"""国内语音服务的配置与分发；凭证仅会话使用或由系统安全保存。"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from talkman_qwen import QwenError, _atomic_write, _dpapi, _read_limited
from talkman_credentials import (CredentialError, delete_keychain_secret, load_keychain_secret,
                                 save_keychain_secret, secure_storage_supported)


@dataclass(frozen=True)
class CredentialField:
    name: str
    label: str
    secret: bool = False
    default: str = ""


@dataclass(frozen=True)
class Provider:
    label: str
    seconds: int
    timeout: int
    fields: tuple[CredentialField, ...]
    note: str


PROVIDERS = {
    "free": Provider("免费在线内测", 120, 70, (), "停止录音后识别；免费服务可能因额度或供应商策略而不可用。"),
    "qwen": Provider("千问", 120, 65, (), "沿用千问设置；可选录后识别或实时转写。"),
    "volc": Provider("豆包 / 火山引擎", 120, 65, (
        CredentialField("api_key", "语音服务 API Key（新版）", True),
        CredentialField("app_id", "应用标识（旧版，可选）"),
        CredentialField("access_key", "访问令牌（旧版，可选）", True),
    ), "开通大模型录音识别极速版；新版填 API Key，旧版填应用标识和访问令牌。"),
    "xunfei": Provider("讯飞", 55, 75, (
        CredentialField("app_id", "应用标识"),
        CredentialField("api_key", "API Key", True),
        CredentialField("api_secret", "API Secret", True),
        CredentialField("language", "语言", default="zh_cn"),
        CredentialField("accent", "口音", default="mandarin"),
    ), "开通语音听写；普通话默认 zh_cn / mandarin，英语用 en_us。单轮最多 55 秒。"),
    "tencent": Provider("腾讯云", 120, 65, (
        CredentialField("app_id", "应用标识"),
        CredentialField("secret_id", "SecretId", True),
        CredentialField("secret_key", "SecretKey", True),
        CredentialField("engine_type", "识别引擎", default="16k_zh"),
    ), "开通录音文件识别极速版；普通话用 16k_zh，英文用 16k_en，以账户支持为准。"),
    "baidu": Provider("百度智能云", 60, 65, (
        CredentialField("api_key", "API Key（新版，或旧应用 AK）", True),
        CredentialField("secret_key", "Secret Key（仅旧应用填写）", True),
        CredentialField("dev_pid", "识别模型编号", default="80001"),
    ), "开通短语音识别；80001 为极速版，1537 为普通话标准版，1737 为英语。单轮最多 60 秒。"),
}
SETTINGS_FILE = "asr-settings.json"
KEYS_FILE = "asr-keys.dpapi"


@dataclass
class ASRSettings:
    provider: str = "qwen"
    profiles: dict[str, dict[str, str]] = field(default_factory=dict, repr=False)

    def credentials(self, provider: str | None = None) -> dict[str, str]:
        selected = provider or self.provider
        spec = PROVIDERS[selected]
        saved = self.profiles.get(selected, {})
        return {item.name: saved.get(item.name, item.default) for item in spec.fields}


def validate(settings: ASRSettings, *, require_credentials: bool = True) -> ASRSettings:
    if settings.provider not in PROVIDERS:
        raise QwenError("请选择支持的语音服务。")
    profiles = {}
    for provider, spec in PROVIDERS.items():
        values = settings.profiles.get(provider, {})
        if not isinstance(values, dict):
            raise QwenError("语音设置格式异常，请重新填写。")
        profiles[provider] = {}
        for item in spec.fields:
            value = values.get(item.name, item.default)
            if not isinstance(value, str) or len(value) > 4096:
                raise QwenError("语音凭证格式异常，请检查设置。")
            value = value.strip()
            if value and any(c.isspace() or not c.isascii() or ord(c)<32 or ord(c)==127 for c in value):
                raise QwenError(f"{spec.label}的{item.label}不能包含空格或中文。")
            profiles[provider][item.name] = value
    result = ASRSettings(settings.provider, profiles)
    if require_credentials and result.provider not in ("qwen", "free"):
        values = result.credentials()
        required = {
            "volc": ("api_key",) if values.get("api_key") else ("app_id", "access_key"),
            "xunfei": ("app_id", "api_key", "api_secret"),
            "tencent": ("app_id", "secret_id", "secret_key"),
            "baidu": ("api_key",),
        }[result.provider]
        if any(not values.get(name) for name in required):
            raise QwenError(f"请先在「语音服务」填写{PROVIDERS[result.provider].label}的完整凭证。")
    return result


def load_asr_settings(data_dir: Path, *, read_credentials: bool = True,
                      allow_keychain_prompt: bool = False) -> ASRSettings:
    config_path, key_path = Path(data_dir) / SETTINGS_FILE, Path(data_dir) / KEYS_FILE
    try:
        if not config_path.exists():
            return ASRSettings()
        config = json.loads(_read_limited(config_path))
        if not isinstance(config, dict) or not isinstance(config.get("profiles", {}), dict):
            raise ValueError
        profiles = {}
        for provider, spec in PROVIDERS.items():
            saved = config.get("profiles", {}).get(provider, {})
            if not isinstance(saved, dict):
                raise ValueError
            profiles[provider] = {item.name: saved.get(item.name, item.default)
                                  for item in spec.fields if not item.secret}
        secret_bytes = None
        if read_credentials and sys.platform == "darwin" and config.get("remember_credentials") is True:
            secret_bytes = (load_keychain_secret(data_dir, "asr", allow_interaction=True) if allow_keychain_prompt
                            else load_keychain_secret(data_dir, "asr"))
        elif read_credentials and sys.platform != "darwin" and os.name == "nt" and key_path.exists():
            secret_bytes = _dpapi(_read_limited(key_path), decrypt=True)
        if secret_bytes is not None:
            secrets = json.loads(secret_bytes)
            if not isinstance(secrets, dict):
                raise ValueError
            for provider, spec in PROVIDERS.items():
                saved = secrets.get(provider, {})
                if not isinstance(saved, dict):
                    raise ValueError
                profiles[provider].update({item.name: saved.get(item.name, "")
                                          for item in spec.fields if item.secret})
        return validate(ASRSettings(config.get("provider", "qwen"), profiles), require_credentials=False)
    except CredentialError as exc:
        raise QwenError(str(exc)) from None
    except QwenError:
        raise
    except (OSError, ValueError, UnicodeError, TypeError):
        raise QwenError("语音设置未能读取，请重新填写凭证。") from None


def asr_keys_remembered(data_dir: Path) -> bool:
    """只读取非敏感记忆标记，不访问 Mac 钥匙串。"""
    if sys.platform == "darwin":
        try:
            content = json.loads(_read_limited(Path(data_dir) / SETTINGS_FILE))
            return isinstance(content, dict) and content.get("remember_credentials") is True
        except (QwenError, OSError, ValueError, UnicodeError):
            return False
    return os.name == "nt" and (Path(data_dir) / KEYS_FILE).exists()


def save_asr_settings(data_dir: Path, settings: ASRSettings, remember: bool) -> None:
    validated = validate(settings)
    public, secrets = {}, {}
    for provider, spec in PROVIDERS.items():
        values = validated.credentials(provider)
        public[provider] = {item.name: values[item.name] for item in spec.fields if not item.secret}
        secrets[provider] = {item.name: values[item.name] for item in spec.fields if item.secret}
    on_mac = sys.platform == "darwin"
    secret_bytes = json.dumps(secrets).encode("utf-8")
    encrypted = _dpapi(secret_bytes, decrypt=False) if remember and not on_mac else None
    config = json.dumps({"version": 2, "provider": validated.provider, "profiles": public,
                         "remember_credentials": bool(remember)},
                        ensure_ascii=False, indent=2).encode("utf-8")
    directory = Path(data_dir)
    try:
        if on_mac:
            if remember:
                save_keychain_secret(directory, "asr", secret_bytes)
            elif secure_storage_supported() or asr_keys_remembered(directory):
                delete_keychain_secret(directory, "asr")
        directory.mkdir(parents=True, exist_ok=True)
        if encrypted is not None:
            _atomic_write(directory / KEYS_FILE, encrypted)
        else:
            (directory / KEYS_FILE).unlink(missing_ok=True)
        _atomic_write(directory / SETTINGS_FILE, config)
    except CredentialError as exc:
        raise QwenError(str(exc)) from None
    except OSError:
        if on_mac:
            raise QwenError("语音配置文件未能保存；钥匙串可能已更新，请检查目录权限后重新保存。") from None
        raise QwenError("语音设置保存失败，请检查本地目录权限。") from None


def transcriber(settings: ASRSettings):
    validated = validate(settings)
    credentials = validated.credentials()
    if validated.provider == "volc":
        from talkman_asr_volc_baidu import transcribe_volc
        return lambda wav: transcribe_volc(wav, credentials)
    if validated.provider == "baidu":
        from talkman_asr_volc_baidu import transcribe_baidu
        return lambda wav: transcribe_baidu(wav, credentials)
    if validated.provider == "tencent":
        from talkman_asr_tencent_xunfei import transcribe_tencent
        return lambda wav: transcribe_tencent(wav, credentials)
    if validated.provider == "xunfei":
        from talkman_asr_tencent_xunfei import transcribe_xunfei
        return lambda wav: transcribe_xunfei(wav, credentials)
    raise QwenError("千问请使用千问客户端进行识别。")
