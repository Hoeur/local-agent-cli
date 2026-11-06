"""Copy text to the Windows clipboard directly, so copying works in Command Prompt and PowerShell
windows whose terminal ignores the OSC 52 clipboard sequence Textual sends."""
import os
import time

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
HWND_MESSAGE = -3


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
    user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                      ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                      wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.DestroyWindow.restype = wintypes.BOOL
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


def _open(user32, owner=None, attempts=10):
    # Another program may hold the clipboard for a moment; retry briefly.
    for _ in range(attempts):
        if user32.OpenClipboard(owner):
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
    # A clipboard write needs an owner even in a pseudoconsole with no visible application window.
    owner = user32.CreateWindowExW(0, "STATIC", None, 0, 0, 0, 0, 0, HWND_MESSAGE, None, None, None)
    if not owner:
        return False
    handle = None
    try:
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not handle:
            return False
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return False
        try:
            ctypes.memmove(pointer, data, len(data))
        finally:
            kernel32.GlobalUnlock(handle)
        if not _open(user32, owner):
            return False
        try:
            if not user32.EmptyClipboard() or not user32.SetClipboardData(CF_UNICODETEXT, handle):
                return False
            handle = None  # Ownership passes to the system only on success.
            return True
        finally:
            user32.CloseClipboard()
    finally:
        if handle:
            kernel32.GlobalFree(handle)
        user32.DestroyWindow(owner)


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
