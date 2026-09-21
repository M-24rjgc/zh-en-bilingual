"""Capture one Windows monitor into memory for translation context."""

from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes as wt
import struct
import sys
import zlib
from contextlib import contextmanager
from dataclasses import dataclass, field


class ScreenCaptureError(RuntimeError):
    pass


@dataclass(frozen=True)
class ScreenCapture:
    data_url: str = field(repr=False)
    width: int
    height: int
    byte_count: int


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT),
                ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
        ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
        ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD),
    ]


def _winapi():
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    signatures = (
        (user32, "GetForegroundWindow", wt.HWND, []),
        (user32, "IsWindow", wt.BOOL, [wt.HWND]),
        (user32, "MonitorFromWindow", wt.HANDLE, [wt.HWND, wt.DWORD]),
        (user32, "MonitorFromPoint", wt.HANDLE, [wt.POINT, wt.DWORD]),
        (user32, "GetCursorPos", wt.BOOL, [ctypes.POINTER(wt.POINT)]),
        (user32, "GetMonitorInfoW", wt.BOOL, [wt.HANDLE, ctypes.POINTER(MONITORINFO)]),
        (user32, "GetWindowDisplayAffinity", wt.BOOL, [wt.HWND, ctypes.POINTER(wt.DWORD)]),
        (user32, "SetWindowDisplayAffinity", wt.BOOL, [wt.HWND, wt.DWORD]),
        (user32, "GetDC", wt.HDC, [wt.HWND]),
        (user32, "ReleaseDC", ctypes.c_int, [wt.HWND, wt.HDC]),
        (gdi32, "CreateCompatibleDC", wt.HDC, [wt.HDC]),
        (gdi32, "CreateDIBSection", wt.HBITMAP,
         [wt.HDC, ctypes.POINTER(BITMAPINFOHEADER), wt.UINT,
          ctypes.POINTER(ctypes.c_void_p), wt.HANDLE, wt.DWORD]),
        (gdi32, "SelectObject", wt.HANDLE, [wt.HDC, wt.HANDLE]),
        (gdi32, "DeleteObject", wt.BOOL, [wt.HANDLE]),
        (gdi32, "DeleteDC", wt.BOOL, [wt.HDC]),
        (gdi32, "SetStretchBltMode", ctypes.c_int, [wt.HDC, ctypes.c_int]),
        (gdi32, "SetBrushOrgEx", wt.BOOL,
         [wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.POINTER(wt.POINT)]),
        (gdi32, "StretchBlt", wt.BOOL,
         [wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
          wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.DWORD]),
        (gdi32, "GdiFlush", wt.BOOL, []),
    )
    for library, name, result, args in signatures:
        function = getattr(library, name)
        function.restype = result
        function.argtypes = args
    return user32, gdi32


def _require(value, operation: str):
    if not value:
        raise ScreenCaptureError(f"{operation} 失败（Windows {ctypes.get_last_error()}）")
    return value


def foreground_window() -> int:
    user32, _ = _winapi()
    return user32.GetForegroundWindow() or 0


def _monitor_bounds(user32, source_hwnd: int) -> tuple[int, int, int, int]:
    if source_hwnd and user32.IsWindow(source_hwnd):
        monitor = user32.MonitorFromWindow(source_hwnd, 2)
    else:
        point = wt.POINT()
        _require(user32.GetCursorPos(ctypes.byref(point)), "GetCursorPos")
        monitor = user32.MonitorFromPoint(point, 2)
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(info)
    _require(user32.GetMonitorInfoW(monitor, ctypes.byref(info)), "GetMonitorInfoW")
    rect = info.rcMonitor
    return rect.left, rect.top, rect.right, rect.bottom


@contextmanager
def _exclude_window(user32, hwnd: int):
    if not hwnd:
        yield
        return
    if sys.getwindowsversion().build < 19041:
        raise ScreenCaptureError("当前 Windows 版本不支持排除翻译气泡")
    previous = wt.DWORD()
    _require(user32.GetWindowDisplayAffinity(hwnd, ctypes.byref(previous)),
             "GetWindowDisplayAffinity")
    _require(user32.SetWindowDisplayAffinity(hwnd, 0x11), "SetWindowDisplayAffinity")
    try:
        # Wait for the compositor so the popup is removed from the captured frame.
        dwm = ctypes.WinDLL("dwmapi", use_last_error=True)
        dwm.DwmFlush.argtypes = []
        dwm.DwmFlush.restype = wt.LONG
        if dwm.DwmFlush() < 0:
            raise ScreenCaptureError("桌面合成器尚未准备好")
        yield
    finally:
        if user32.IsWindow(hwnd):
            _require(user32.SetWindowDisplayAffinity(hwnd, previous.value),
                     "恢复窗口捕获设置")


def _encode_png(raw: bytes, width: int, height: int, stride: int) -> bytes:
    rows = bytearray()
    for row in range(height):
        line = bytearray(raw[row * stride:row * stride + width * 3])
        line[0::3], line[2::3] = line[2::3], line[0::3]
        rows.append(0)
        rows.extend(line)

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 3)) + chunk(b"IEND", b""))


def _capture_png(user32, gdi32, bounds, max_edge: int) -> tuple[bytes, int, int]:
    left, top, right, bottom = bounds
    source_w, source_h = right - left, bottom - top
    if source_w <= 0 or source_h <= 0:
        raise ScreenCaptureError("屏幕尺寸无效")
    scale = min(1.0, max_edge / max(source_w, source_h))
    width, height = max(1, round(source_w * scale)), max(1, round(source_h * scale))
    screen = _require(user32.GetDC(None), "GetDC")
    memory = bitmap = previous = None
    try:
        memory = _require(gdi32.CreateCompatibleDC(screen), "CreateCompatibleDC")
        header = BITMAPINFOHEADER()
        header.biSize = ctypes.sizeof(header)
        header.biWidth, header.biHeight = width, -height  # top-down rows
        header.biPlanes, header.biBitCount = 1, 24
        bits = ctypes.c_void_p()
        bitmap = _require(gdi32.CreateDIBSection(memory, ctypes.byref(header), 0,
                                                ctypes.byref(bits), None, 0), "CreateDIBSection")
        _require(bits.value, "DIB 像素缓冲区")
        previous = _require(gdi32.SelectObject(memory, bitmap), "SelectObject")
        if previous == ctypes.c_void_p(-1).value:
            previous = None
            raise ScreenCaptureError("SelectObject 失败")
        _require(gdi32.SetStretchBltMode(memory, 4), "SetStretchBltMode")  # HALFTONE
        _require(gdi32.SetBrushOrgEx(memory, 0, 0, None), "SetBrushOrgEx")
        _require(gdi32.StretchBlt(memory, 0, 0, width, height, screen,
                                left, top, source_w, source_h, 0x40CC0020), "StretchBlt")
        _require(gdi32.GdiFlush(), "GdiFlush")
        stride = ((width * 3 + 3) // 4) * 4
        raw = ctypes.string_at(bits, stride * height)
    finally:
        if previous:
            gdi32.SelectObject(memory, previous)
        if bitmap:
            gdi32.DeleteObject(bitmap)
        if memory:
            gdi32.DeleteDC(memory)
        user32.ReleaseDC(None, screen)
    return _encode_png(raw, width, height, stride), width, height


def capture_screen_context(source_hwnd: int = 0, exclude_hwnd: int = 0,
                           max_edge: int = 1920) -> ScreenCapture:
    """Capture the source application's current monitor; never write image files."""
    try:
        user32, gdi32 = _winapi()
        bounds = _monitor_bounds(user32, source_hwnd)
        max_edge = max(640, min(2560, int(max_edge)))
        with _exclude_window(user32, exclude_hwnd):
            png, width, height = _capture_png(user32, gdi32, bounds, max_edge)
    except (OSError, ValueError, TypeError) as exc:
        raise ScreenCaptureError(f"屏幕捕获不可用：{type(exc).__name__}") from exc
    data_url = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
    return ScreenCapture(data_url, width, height, len(png))
