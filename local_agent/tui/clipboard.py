"""Copy text to the Windows clipboard directly, so copying works in Command Prompt and PowerShell
windows whose terminal ignores the OSC 52 clipboard sequence Textual sends."""
import os
import time

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002


def windows_text(text):
    """Windows programs expect CRLF line breaks in clipboard text."""
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")


def _api():
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.restype = wintypes.HGLOBAL
    return ctypes, user32, kernel32


def _open(user32, attempts=10):
    # Another program may hold the clipboard for a moment; retry briefly.
    for _ in range(attempts):
        if user32.OpenClipboard(None):
            return True
        time.sleep(0.02)
    return False


def copy(text):
    """Put text on the Windows clipboard as Unicode. Returns False where that isn't possible."""
    if os.name != "nt":
        return False
    try:
        ctypes, user32, kernel32 = _api()
    except (OSError, AttributeError, ImportError):
        return False
    data = windows_text(text).encode("utf-16-le") + b"\x00\x00"
    if not _open(user32):
        return False
    try:
        if not user32.EmptyClipboard():
            return False
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not handle:
            return False
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            kernel32.GlobalFree(handle)
            return False
        try:
            ctypes.memmove(pointer, data, len(data))
        finally:
            kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            kernel32.GlobalFree(handle)  # Ownership passes to the system only on success.
            return False
        return True
    finally:
        user32.CloseClipboard()


def paste():
    """Read Unicode text from the Windows clipboard, or None. Used by tests and the doctor check."""
    if os.name != "nt":
        return None
    try:
        ctypes, user32, kernel32 = _api()
    except (OSError, AttributeError, ImportError):
        return None
    if not _open(user32):
        return None
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return None
        try:
            return ctypes.wstring_at(pointer)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()
