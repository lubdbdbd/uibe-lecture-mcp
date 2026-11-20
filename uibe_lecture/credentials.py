from __future__ import annotations

"""Opt-in, local Windows Credential Manager storage. Never log credential values."""
import ctypes
import os
from ctypes import wintypes

TARGET = "UIBE-Lecture/cas.uibe.edu.cn"

class CredentialError(RuntimeError):
    pass

class CREDENTIALW(ctypes.Structure):
    _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
                ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
                ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD),
                ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR),
                ("UserName", wintypes.LPWSTR)]

def _api():
    if os.name != "nt":
        raise CredentialError("Windows Credential Manager is required")
    api = ctypes.WinDLL("advapi32", use_last_error=True)
    api.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIALW), wintypes.DWORD]
    api.CredWriteW.restype = wintypes.BOOL
    api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                             ctypes.POINTER(ctypes.POINTER(CREDENTIALW))]
    api.CredReadW.restype = wintypes.BOOL
    api.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    api.CredDeleteW.restype = wintypes.BOOL
    api.CredFree.argtypes = [ctypes.c_void_p]
    return api

def save_credentials(username: str, password: str):
    if not username or not password or len(password.encode("utf-16-le")) > 2560:
        raise CredentialError("Invalid credential input")
    api = _api()
    raw = password.encode("utf-16-le")
    buffer = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
    cred = CREDENTIALW(Type=1, TargetName=TARGET, CredentialBlobSize=len(raw),
                       CredentialBlob=buffer, Persist=2, UserName=username)
    try:
        if not api.CredWriteW(ctypes.byref(cred), 0):
            raise CredentialError("Credential Manager write failed")
    finally:
        ctypes.memset(buffer, 0, len(raw))

def load_credentials():
    api = _api()
    ptr = ctypes.POINTER(CREDENTIALW)()
    if not api.CredReadW(TARGET, 1, 0, ctypes.byref(ptr)):
        if ctypes.get_last_error() == 1168:
            return None
        raise CredentialError("Credential Manager read failed")
    try:
        c = ptr.contents
        return c.UserName, ctypes.string_at(c.CredentialBlob, c.CredentialBlobSize).decode("utf-16-le")
    finally:
        api.CredFree(ptr)

def delete_credentials():
    api = _api()
    if not api.CredDeleteW(TARGET, 1, 0) and ctypes.get_last_error() != 1168:
        raise CredentialError("Credential Manager deletion failed")

def configure_interactive():
    # getpass refuses to silently fall back to echoed stdin.
    import getpass
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        username = getpass.getpass("School account (hidden): ")
        password = getpass.getpass("School password (hidden): ")
    save_credentials(username, password)
    from .storage import Store
    Store().put("system", "autologin", {"state": "configured", "attempt_at": 0})
    return {"saved": True, "storage": "Windows Credential Manager"}


def configure_stdin():
    """Explicit secure pipe input. Never accept a file path, argv, or echo input."""
    import json
    import sys
    if sys.stdin.isatty():
        raise CredentialError("Use auth configure for an interactive terminal")
    try:
        raw=sys.stdin.readline(8193)
        if len(raw)>8192:
            raise ValueError()
        value=json.loads(raw)
        username=value["username"]
        password=value["password"]
        if not isinstance(username,str) or not isinstance(password,str):
            raise ValueError()
    except Exception:
        raise CredentialError("Invalid credential input") from None
    try:
        save_credentials(username,password)
        from .storage import Store
        Store().put("system","autologin",{"state":"configured","attempt_at":0})
        return {"saved":True,"storage":"Windows Credential Manager"}
    finally:
        raw=value=username=password=None
