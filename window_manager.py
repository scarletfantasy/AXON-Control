"""Native window actions for our custom Tk titlebar; no Tk calls in callbacks."""
import ctypes as C
from ctypes import wintypes as W

WM_GETMINMAXINFO = 0x24
GWL_STYLE = -16
SW_MAXIMIZE, SW_RESTORE = 3, 9


class MonitorInfo(C.Structure):
    _fields_ = [('size', W.DWORD), ('monitor', W.RECT), ('work', W.RECT), ('flags', W.DWORD)]


class MinMaxInfo(C.Structure):
    _fields_ = [('reserved', W.POINT), ('maximum_size', W.POINT),
                ('maximum_position', W.POINT), ('minimum_track', W.POINT),
                ('maximum_track', W.POINT)]


SUBCLASS = C.WINFUNCTYPE(C.c_ssize_t, W.HWND, W.UINT, W.WPARAM, W.LPARAM,
                       C.c_size_t, C.c_size_t)


def maximize_bounds(monitor, work):
    """Offsets are monitor-relative, including monitors left/above the primary."""
    return (work.right - work.left, work.bottom - work.top,
            work.left - monitor.left, work.top - monitor.top)


class NativeWindow:
    """Retain Windows placement/state while respecting the current monitor's taskbar."""
    def __init__(self, hwnd, *, minimum=(980, 740)):
        self.hwnd, self.minimum = hwnd, minimum
        self.closed = False
        self.callback_error = None
        self.user = C.WinDLL('user32', use_last_error=True)
        self.common = C.WinDLL('comctl32', use_last_error=True)
        signatures = {
            'GetWindowLongW': ([W.HWND, C.c_int], C.c_long),
            'SetWindowLongW': ([W.HWND, C.c_int, C.c_long], C.c_long),
            'ShowWindow': ([W.HWND, C.c_int], W.BOOL),
            'IsZoomed': ([W.HWND], W.BOOL),
            'IsIconic': ([W.HWND], W.BOOL),
            'IsWindow': ([W.HWND], W.BOOL),
            'GetWindowRect': ([W.HWND, C.POINTER(W.RECT)], W.BOOL),
            'SetWindowPos': ([W.HWND, W.HWND, C.c_int, C.c_int, C.c_int, C.c_int, W.UINT], W.BOOL),
            'MonitorFromWindow': ([W.HWND, W.DWORD], W.HANDLE),
            'GetMonitorInfoW': ([W.HANDLE, C.POINTER(MonitorInfo)], W.BOOL),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.user, name)
            function.argtypes, function.restype = args, result
        self.common.SetWindowSubclass.argtypes = [W.HWND, SUBCLASS, C.c_size_t, C.c_size_t]
        self.common.SetWindowSubclass.restype = W.BOOL
        self.common.RemoveWindowSubclass.argtypes = [W.HWND, SUBCLASS, C.c_size_t]
        self.common.RemoveWindowSubclass.restype = W.BOOL
        self.common.DefSubclassProc.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM]
        self.common.DefSubclassProc.restype = C.c_ssize_t
        self.callback = SUBCLASS(self._procedure)
        self.identity = id(self)
        if not self.common.SetWindowSubclass(hwnd, self.callback, self.identity, 0):
            raise C.WinError(C.get_last_error())
        try:
            self.prepare()
        except BaseException:
            self.close()
            raise

    def prepare(self):
        # Keep the borderless custom titlebar while enabling standard window actions.
        style = self.user.GetWindowLongW(self.hwnd, GWL_STYLE)
        self.user.SetWindowLongW(self.hwnd, GWL_STYLE, style | 0x10000 | 0x20000 | 0x80000)

    def _procedure(self, hwnd, message, wparam, lparam, identity, reference):
        # Let Tk handle the message first: its override-redirect defaults otherwise
        # replace the work area with the entire monitor and cover the taskbar.
        result = self.common.DefSubclassProc(hwnd, message, wparam, lparam)
        if message == WM_GETMINMAXINFO and lparam:
            try:
                info = MonitorInfo()
                info.size = C.sizeof(info)
                monitor = self.user.MonitorFromWindow(hwnd, 2)
                if self.user.GetMonitorInfoW(monitor, C.byref(info)):
                    maximum = C.cast(lparam, C.POINTER(MinMaxInfo)).contents
                    width, height, x, y = maximize_bounds(info.monitor, info.work)
                    maximum.maximum_size = W.POINT(width, height)
                    maximum.maximum_position = W.POINT(x, y)
                    maximum.minimum_track = W.POINT(min(self.minimum[0], width), min(self.minimum[1], height))
            except BaseException as error:
                # Exceptions must never escape a native callback; Tk reads state
                # later on its owner thread, outside this call stack.
                self.callback_error = type(error).__name__
        return result

    @property
    def maximized(self):
        return bool(self.user.IsZoomed(self.hwnd))

    @property
    def minimized(self):
        return bool(self.user.IsIconic(self.hwnd))

    def show(self, maximized=False):
        self.user.ShowWindow(self.hwnd, SW_MAXIMIZE if maximized else SW_RESTORE)

    @property
    def bounds(self):
        rect = W.RECT()
        if not self.user.GetWindowRect(self.hwnd, C.byref(rect)):
            raise C.WinError(C.get_last_error())
        return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top

    @property
    def geometry(self):
        x, y, width, height = self.bounds
        return f'{width}x{height}{x:+d}{y:+d}'

    def move(self, x, y):
        self.user.SetWindowPos(self.hwnd, None, x, y, 0, 0, 0x15)

    def resize(self, width, height):
        self.user.SetWindowPos(self.hwnd, None, 0, 0, width, height, 0x16)

    def close(self):
        if not self.closed:
            if self.user.IsWindow(self.hwnd):
                self.common.RemoveWindowSubclass(self.hwnd, self.callback, self.identity)
            self.closed = True
