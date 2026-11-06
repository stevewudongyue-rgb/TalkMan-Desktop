"""Mac 系统钥匙串；只通过 Security API 传递凭证，不写明文文件。"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import sys
from pathlib import Path


_SERVICE = "com.cololight.talkman.credentials"
_MAX_SECRET_BYTES = 32 * 1024


class CredentialError(Exception):
    """可展示的中文错误；不包含系统异常、凭证或账户标识。"""


def secure_storage_supported() -> bool:
    """只检测支持状态；设置窗口调用时不读取或解锁钥匙串。"""
    if sys.platform == "darwin":
        try:
            return importlib.util.find_spec("Security") is not None
        except (ImportError, AttributeError, ValueError):
            return False
    return os.name == "nt"


def _security():
    if sys.platform != "darwin":
        raise CredentialError("当前平台不支持系统钥匙串，请仅在本次会话使用凭证。")
    try:
        import Security
        return Security
    except Exception:
        raise CredentialError("系统钥匙串组件不可用，请使用新版 Mac 应用或关闭本机记忆。") from None


def _query(api, data_dir: Path, group: str) -> dict:
    if group not in ("qwen", "asr", "free", "license"):
        raise CredentialError("凭证存储类型无效。")
    directory = os.path.normcase(os.path.realpath(os.fspath(data_dir)))
    account = group + ":" + hashlib.sha256(directory.encode("utf-8")).hexdigest()
    return {api.kSecClass: api.kSecClassGenericPassword,
            api.kSecAttrService: _SERVICE, api.kSecAttrAccount: account,
            api.kSecAttrSynchronizable: False}


def _status(result) -> int:
    value = result[0] if isinstance(result, tuple) and result else result
    if not isinstance(value, int) or isinstance(value, bool):
        raise CredentialError("系统钥匙串返回异常，请重试或关闭本机记忆。")
    return value


def _failed(action: str, status: int) -> CredentialError:
    if status in (-128, -25293, -25308):
        return CredentialError(f"系统钥匙串未允许{action}凭证，请解锁钥匙串并允许 TalkMan 访问后重试。")
    return CredentialError(f"系统钥匙串无法{action}凭证，请重试或关闭本机记忆。")


def load_keychain_secret(data_dir: Path, group: str, *, allow_interaction: bool = False) -> bytes | None:
    api = _security()
    try:
        query = _query(api, data_dir, group)
        query.update({api.kSecReturnData: True, api.kSecMatchLimit: api.kSecMatchLimitOne,
                      api.kSecUseAuthenticationUI: (api.kSecUseAuthenticationUIAllow if allow_interaction
                                                   else api.kSecUseAuthenticationUIFail)})
        result = api.SecItemCopyMatching(query, None)
        status = _status(result)
        if status == api.errSecItemNotFound:
            return None
        if status != api.errSecSuccess:
            raise _failed("读取", status)
        if not isinstance(result, tuple) or len(result) != 2 or result[1] is None:
            raise CredentialError("系统钥匙串返回异常，请重新填写凭证。")
        if not 0 < len(result[1]) <= _MAX_SECRET_BYTES:
            raise CredentialError("已保存的凭证格式异常，请重新填写。")
        secret = bytes(result[1])
        if not secret or len(secret) > _MAX_SECRET_BYTES:
            raise CredentialError("已保存的凭证格式异常，请重新填写。")
        return secret
    except CredentialError:
        raise
    except Exception:
        raise CredentialError("系统钥匙串未能读取凭证，请重新填写或稍后重试。") from None


def save_keychain_secret(data_dir: Path, group: str, secret: bytes) -> None:
    if not isinstance(secret, bytes) or not secret or len(secret) > _MAX_SECRET_BYTES:
        raise CredentialError("凭证格式异常，无法保存。")
    api = _security()
    try:
        query = _query(api, data_dir, group)
        changes = {api.kSecValueData: secret}
        status = _status(api.SecItemUpdate(query, changes))
        if status == api.errSecItemNotFound:
            attributes = dict(query)
            attributes.update(changes)
            attributes[api.kSecAttrLabel] = {"qwen": "TalkMan 千问密钥", "asr": "TalkMan 语音服务凭证", "free": "TalkMan 免费内测凭证", "license": "TalkMan 旧版兼容凭证"}[group]
            status = _status(api.SecItemAdd(attributes, None))
            if status == api.errSecDuplicateItem:
                status = _status(api.SecItemUpdate(query, changes))
        if status != api.errSecSuccess:
            raise _failed("保存", status)
    except CredentialError:
        raise
    except Exception:
        raise CredentialError("系统钥匙串未能保存凭证，请重试；密钥未写入本地文件。") from None


def delete_keychain_secret(data_dir: Path, group: str) -> None:
    api = _security()
    try:
        status = _status(api.SecItemDelete(_query(api, data_dir, group)))
        if status not in (api.errSecSuccess, api.errSecItemNotFound):
            raise _failed("删除", status)
    except CredentialError:
        raise
    except Exception:
        raise CredentialError("系统钥匙串未能删除凭证，请稍后重试。") from None
