"""Protect local credentials with Windows DPAPI for the current user."""

import ctypes
import sys
from ctypes import wintypes


class CredentialProtectionError(RuntimeError):
    """Report a protection failure without including credential data."""


class _DataBlob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


class WindowsCredentialProtector:
    """Use current-user protection without prompts or machine-wide access."""

    def __init__(self):
        if sys.platform != "win32":
            raise CredentialProtectionError("Secure profile storage requires Windows.")
        try:
            self._crypt = ctypes.WinDLL("crypt32", use_last_error=True)
            self._kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            parameters = [
                ctypes.POINTER(_DataBlob),
                ctypes.c_void_p,
                ctypes.POINTER(_DataBlob),
                ctypes.c_void_p,
                ctypes.c_void_p,
                wintypes.DWORD,
                ctypes.POINTER(_DataBlob),
            ]
            for function in (self._crypt.CryptProtectData, self._crypt.CryptUnprotectData):
                function.argtypes = parameters
                function.restype = wintypes.BOOL
            self._kernel.LocalFree.argtypes = [ctypes.c_void_p]
            self._kernel.LocalFree.restype = ctypes.c_void_p
        except (AttributeError, OSError):
            raise CredentialProtectionError(
                "Windows credential protection is unavailable."
            ) from None

    def protect(self, plaintext: bytes) -> bytes:
        return self._transform(plaintext, self._crypt.CryptProtectData)

    def unprotect(self, ciphertext: bytes) -> bytes:
        return self._transform(ciphertext, self._crypt.CryptUnprotectData)

    def _transform(self, value, function):
        if not isinstance(value, bytes) or not 1 <= len(value) <= 2 * 1024 * 1024:
            raise CredentialProtectionError("The protected data has an invalid size.")
        buffer = ctypes.create_string_buffer(value)
        source = _DataBlob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
        output = _DataBlob()
        try:
            # CRYPTPROTECT_UI_FORBIDDEN. Do not set CRYPTPROTECT_LOCAL_MACHINE.
            if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
                raise CredentialProtectionError(
                    "Windows could not protect or read the profile data."
                )
            return ctypes.string_at(output.data, output.size)
        finally:
            ctypes.memset(buffer, 0, ctypes.sizeof(buffer))
            if output.data:
                ctypes.memset(output.data, 0, output.size)
                self._kernel.LocalFree(output.data)
