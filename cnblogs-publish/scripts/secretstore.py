"""Cross-platform secure credential storage.

Backends
--------
* macOS: login Keychain (Security.framework, ctypes).
* Windows: Credential Manager generic credential (advapi32, ctypes; DPAPI protected).
* Linux: Secret Service via ``secret-tool`` (libsecret) or the Python ``keyring``
  package (GNOME Keyring / KWallet).

The credential value is a small JSON blob containing username, blog name and
access token.  Callers must never print it.
"""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys

__all__ = [
    "SecretStoreError",
    "secret_store_backend_name",
    "secret_store_delete",
    "secret_store_get",
    "secret_store_set",
]


class SecretStoreError(Exception):
    """Raised when the platform secret store is unavailable or fails."""

    def __init__(self, message: str, code: int = 5):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# macOS Keychain
# ---------------------------------------------------------------------------

_MAC_FRAMEWORK = "/System/Library/Frameworks/Security.framework/Security"
_MAC_ERR_NOT_FOUND = -25300
_MAC_ERR_DUPLICATE = -25299
_MAC_ERR_INTERACTION_NOT_ALLOWED = -25308
_MAC_ERR_INTERNAL = 100001


class _MacKeychain:
    name = "macos-keychain"

    def __init__(self):
        self._sec = ctypes.CDLL(_MAC_FRAMEWORK)
        sec = self._sec
        sec.SecKeychainAddGenericPassword.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p,
            ctypes.c_uint32, ctypes.c_char_p,
            ctypes.c_uint32, ctypes.c_char_p, ctypes.c_void_p,
        ]
        sec.SecKeychainAddGenericPassword.restype = ctypes.c_int32
        sec.SecKeychainFindGenericPassword.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p,
            ctypes.c_uint32, ctypes.c_char_p,
            ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
        ]
        sec.SecKeychainFindGenericPassword.restype = ctypes.c_int32
        sec.SecKeychainItemModifyAttributesAndData.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p,
        ]
        sec.SecKeychainItemModifyAttributesAndData.restype = ctypes.c_int32
        sec.SecKeychainItemDelete.argtypes = [ctypes.c_void_p]
        sec.SecKeychainItemDelete.restype = ctypes.c_int32
        sec.SecKeychainItemFreeContent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        sec.SecKeychainItemFreeContent.restype = ctypes.c_int32

    def _find(self, service: str, account: str | None):
        sec = self._sec
        service_b = service.encode("utf-8")
        account_b = account.encode("utf-8") if account else None
        pw_len = ctypes.c_uint32(0)
        pw_data = ctypes.c_void_p()
        item = ctypes.c_void_p()
        status = sec.SecKeychainFindGenericPassword(
            None,
            ctypes.c_uint32(len(service_b)), service_b,
            ctypes.c_uint32(len(account_b)) if account_b else ctypes.c_uint32(0), account_b,
            ctypes.byref(pw_len), ctypes.byref(pw_data), ctypes.byref(item),
        )
        if status == 0:
            try:
                data = ctypes.string_at(pw_data, pw_len.value)
            finally:
                sec.SecKeychainItemFreeContent(None, pw_data)
            return data, item
        if status == _MAC_ERR_NOT_FOUND:
            return None, None
        raise _mac_error(status)

    def get(self, service: str, account: str) -> str | None:
        data, _item = self._find(service, account)
        if data is None:
            return None
        return data.decode("utf-8", "strict")

    def set(self, service: str, account: str, value: str) -> None:
        sec = self._sec
        service_b = service.encode("utf-8")
        account_b = account.encode("utf-8")
        value_b = value.encode("utf-8")
        status = sec.SecKeychainAddGenericPassword(
            None,
            ctypes.c_uint32(len(service_b)), service_b,
            ctypes.c_uint32(len(account_b)), account_b,
            ctypes.c_uint32(len(value_b)), value_b,
            None,
        )
        if status == 0:
            return
        if status == _MAC_ERR_DUPLICATE:
            _data, item = self._find(service, account)
            if item is None:
                raise _mac_error(status)
            status = sec.SecKeychainItemModifyAttributesAndData(
                item, None, ctypes.c_uint32(len(value_b)), value_b
            )
            if status == 0:
                return
        raise _mac_error(status)

    def delete(self, service: str, account: str) -> bool:
        data, item = self._find(service, account)
        if data is None or item is None:
            return False
        status = self._sec.SecKeychainItemDelete(item)
        if status != 0:
            raise _mac_error(status)
        return True


def _mac_error(status: int) -> SecretStoreError:
    if status == _MAC_ERR_INTERNAL:
        return SecretStoreError(
            "钥匙串写入被系统拒绝（OSStatus 100001）。这通常是命令运行在受限沙箱里："
            "请用完整访问权限重试，或在“终端”里运行同一条 setup 命令。"
        )
    if status == _MAC_ERR_INTERACTION_NOT_ALLOWED:
        return SecretStoreError("钥匙串当前不可交互（可能已锁定）。请解锁登录钥匙串后重试。")
    if status == _MAC_ERR_DUPLICATE:
        return SecretStoreError("钥匙串条目已存在，更新失败。")
    return SecretStoreError("钥匙串操作失败（OSStatus %d）。" % status)


# ---------------------------------------------------------------------------
# Windows Credential Manager
# ---------------------------------------------------------------------------

_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2
_ERROR_NOT_FOUND = 1168


def windows_target(service: str, account: str) -> str:
    return "%s:%s" % (service, account)


class _WindowsCredentialManager:
    name = "windows-credential-manager"

    def __init__(self):
        from ctypes import wintypes

        class FILETIME(ctypes.Structure):
            _fields_ = [
                ("dwLowDateTime", wintypes.DWORD),
                ("dwHighDateTime", wintypes.DWORD),
            ]

        class CREDENTIALW(ctypes.Structure):
            _fields_ = [
                ("Flags", wintypes.DWORD),
                ("Type", wintypes.DWORD),
                ("TargetName", wintypes.LPWSTR),
                ("Comment", wintypes.LPWSTR),
                ("LastWritten", FILETIME),
                ("CredentialBlobSize", wintypes.DWORD),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                ("Persist", wintypes.DWORD),
                ("AttributeCount", wintypes.DWORD),
                ("Attributes", ctypes.c_void_p),
                ("TargetAlias", wintypes.LPWSTR),
                ("UserName", wintypes.LPWSTR),
            ]

        self._wintypes = wintypes
        self._CREDENTIALW = CREDENTIALW
        self._advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        advapi = self._advapi
        advapi.CredReadW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
            ctypes.POINTER(ctypes.POINTER(CREDENTIALW)),
        ]
        advapi.CredReadW.restype = wintypes.BOOL
        advapi.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIALW), wintypes.DWORD]
        advapi.CredWriteW.restype = wintypes.BOOL
        advapi.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        advapi.CredDeleteW.restype = wintypes.BOOL
        advapi.CredFree.argtypes = [ctypes.c_void_p]
        advapi.CredFree.restype = None

    def _last_error(self) -> int:
        return ctypes.get_last_error()

    def get(self, service: str, account: str) -> str | None:
        target = windows_target(service, account)
        cred_ptr = ctypes.POINTER(self._CREDENTIALW)()
        ok = self._advapi.CredReadW(
            target, _CRED_TYPE_GENERIC, 0, ctypes.byref(cred_ptr)
        )
        if not ok:
            error = self._last_error()
            if error == _ERROR_NOT_FOUND:
                return None
            raise SecretStoreError("Windows 凭据管理器读取失败（错误 %d）。" % error)
        try:
            cred = cred_ptr.contents
            size = int(cred.CredentialBlobSize)
            if size <= 0 or not cred.CredentialBlob:
                return ""
            return ctypes.string_at(cred.CredentialBlob, size).decode("utf-8", "strict")
        finally:
            self._advapi.CredFree(cred_ptr)

    def set(self, service: str, account: str, value: str) -> None:
        blob = value.encode("utf-8")
        buffer = ctypes.create_string_buffer(blob, len(blob))
        cred = self._CREDENTIALW()
        cred.Type = _CRED_TYPE_GENERIC
        cred.TargetName = windows_target(service, account)
        cred.Comment = "cnblogs-publish credentials"
        cred.CredentialBlobSize = len(blob)
        cred.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
        cred.Persist = _CRED_PERSIST_LOCAL_MACHINE
        cred.UserName = account
        ok = self._advapi.CredWriteW(ctypes.byref(cred), 0)
        if not ok:
            raise SecretStoreError(
                "Windows 凭据管理器写入失败（错误 %d）。" % self._last_error()
            )

    def delete(self, service: str, account: str) -> bool:
        ok = self._advapi.CredDeleteW(
            windows_target(service, account), _CRED_TYPE_GENERIC, 0
        )
        if not ok:
            error = self._last_error()
            if error == _ERROR_NOT_FOUND:
                return False
            raise SecretStoreError("Windows 凭据管理器删除失败（错误 %d）。" % error)
        return True


# ---------------------------------------------------------------------------
# Linux Secret Service
# ---------------------------------------------------------------------------

class _LinuxSecretTool:
    name = "linux-secret-service"

    def __init__(self, executable: str):
        self._exe = executable

    def _run(self, args, input_text=None):
        try:
            return subprocess.run(
                [self._exe] + args,
                input=input_text,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SecretStoreError("Linux Secret Service 调用失败：%s" % exc)

    def get(self, service: str, account: str) -> str | None:
        proc = self._run(["lookup", "service", service, "account", account])
        if proc.returncode == 0:
            return proc.stdout.rstrip("\n")
        stderr = (proc.stderr or "").strip()
        if proc.returncode == 1 and not stderr:
            return None
        raise SecretStoreError(
            "Linux Secret Service 读取失败：%s" % (stderr[:200] or "secret-tool lookup 返回 %d" % proc.returncode)
        )

    def set(self, service: str, account: str, value: str) -> None:
        proc = self._run(
            ["store", "--label", "%s credentials" % service, "service", service, "account", account],
            input_text=value,
        )
        if proc.returncode != 0:
            raise SecretStoreError(
                "Linux Secret Service 写入失败：%s" % ((proc.stderr or "").strip()[:200] or "secret-tool store 返回 %d" % proc.returncode)
            )

    def delete(self, service: str, account: str) -> bool:
        proc = self._run(["clear", "service", service, "account", account])
        if proc.returncode == 0:
            return True
        stderr = (proc.stderr or "").strip()
        if proc.returncode == 1 and not stderr:
            return False
        raise SecretStoreError(
            "Linux Secret Service 删除失败：%s" % (stderr[:200] or "secret-tool clear 返回 %d" % proc.returncode)
        )


class _LinuxKeyring:
    name = "linux-keyring"

    def __init__(self):
        import keyring  # noqa: F401  (lazy: optional dependency)
        import keyring.errors

        backend = keyring.get_keyring()
        chain = getattr(backend, "backends", None) or [backend]
        for candidate in chain:
            name = "%s.%s" % (type(candidate).__module__, type(candidate).__name__)
            if "plaintext" in name.lower():
                raise SecretStoreError(
                    "检测到明文 keyring 后端（%s），拒绝使用。请改用 Secret Service / KWallet，"
                    "或安装 `secret-tool`。" % name
                )
        self._keyring = keyring
        self._errors = keyring.errors
        self.name = "linux-keyring"

    def get(self, service: str, account: str) -> str | None:
        try:
            return self._keyring.get_password(service, account)
        except Exception as exc:  # backend-specific errors
            raise SecretStoreError("Linux keyring 读取失败：%s" % exc)

    def set(self, service: str, account: str, value: str) -> None:
        try:
            self._keyring.set_password(service, account, value)
        except Exception as exc:
            raise SecretStoreError("Linux keyring 写入失败：%s" % exc)

    def delete(self, service: str, account: str) -> bool:
        try:
            self._keyring.delete_password(service, account)
            return True
        except self._errors.PasswordDeleteError:
            return False
        except Exception as exc:
            raise SecretStoreError("Linux keyring 删除失败：%s" % exc)


# ---------------------------------------------------------------------------
# selection
# ---------------------------------------------------------------------------

_BACKEND = None


def _linux_backend():
    executable = shutil.which("secret-tool")
    if executable:
        return _LinuxSecretTool(executable)
    try:
        return _LinuxKeyring()
    except Exception as exc:
        raise SecretStoreError(
            "Linux 上未找到可用的安全凭据存储。请安装 libsecret-tools（提供 `secret-tool`）"
            "并确保 GNOME Keyring / KWallet 正在运行；或安装 Python `keyring` 包。"
            "（%s）" % exc
        )


def _get_backend():
    global _BACKEND
    if _BACKEND is not None:
        return _BACKEND
    try:
        if sys.platform == "darwin":
            _BACKEND = _MacKeychain()
        elif os.name == "nt" or sys.platform.startswith("win"):
            _BACKEND = _WindowsCredentialManager()
        else:
            _BACKEND = _linux_backend()
    except SecretStoreError:
        raise
    except Exception as exc:
        raise SecretStoreError("系统凭据库初始化失败：%s" % exc)
    return _BACKEND


def secret_store_backend_name() -> str:
    try:
        return _get_backend().name
    except Exception:
        return "unavailable"


def secret_store_get(service: str, account: str) -> str | None:
    return _get_backend().get(service, account)


def secret_store_set(service: str, account: str, value: str) -> None:
    _get_backend().set(service, account, value)


def secret_store_delete(service: str, account: str) -> bool:
    return _get_backend().delete(service, account)