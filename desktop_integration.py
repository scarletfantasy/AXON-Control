"""Windows login startup and notification icon. MIT license; no extra packages.

Native callbacks are pumped by Tk on its UI thread. A separate hidden top-level
window survives Tk withdraw/deiconify and receives Explorer restart broadcasts.
"""
import ctypes as C
from collections import deque
from ctypes import wintypes as W
import json
from pathlib import Path
import subprocess
import sys
import winreg

RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
RUN_NAME = 'AXONControl'
APP_ID = 'AXONControl.Desktop'


def set_taskbar_identity():
    """Call before creating Tk so Windows groups us independently of pythonw."""
    shell = C.WinDLL('shell32')
    shell.SetCurrentProcessExplicitAppUserModelID.argtypes = [W.LPCWSTR]
    shell.SetCurrentProcessExplicitAppUserModelID.restype = C.c_long
    result = shell.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    if result < 0:
        raise OSError(f'Cannot set taskbar identity: {result:#x}')


class WindowIcons:
    """Own native icons for the actual taskbar HWND, including recreated wrappers."""
    def __init__(self, path):
        self.user = C.WinDLL('user32', use_last_error=True)
        self.user.LoadImageW.argtypes = [W.HINSTANCE, W.LPCWSTR, W.UINT, C.c_int, C.c_int, W.UINT]
        self.user.LoadImageW.restype = W.HANDLE
        self.user.SendMessageW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM]
        self.user.SendMessageW.restype = C.c_ssize_t
        self.user.DestroyIcon.argtypes = [W.HICON]
        self.user.DestroyIcon.restype = W.BOOL
        self.handles = []
        try:
            for size in (32, 16):
                handle = self.user.LoadImageW(None, str(path), 1, size, size, 0x10)
                if not handle:
                    raise C.WinError(C.get_last_error())
                self.handles.append(handle)
        except Exception:
            self.close()
            raise

    def apply(self, hwnd):
        for kind, handle in zip((1, 0), self.handles):
            self.user.SendMessageW(hwnd, 0x80, kind, handle)  # WM_SETICON

    def close(self):
        for handle in self.handles:
            self.user.DestroyIcon(handle)
        self.handles.clear()


class Preferences:
    def __init__(self, folder):
        self.path = Path(folder) / 'desktop-settings.json'
        self.minimize_to_tray = True
        self.pause_in_background = True
        self.auto_connect = False
        self.auto_reconnect = True
        self.audio_source = '__default_playback__'
        self.audio_source_label = '默认播放设备'
        self.audio_style = 'flow'
        self.window_geometry = ''
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            if isinstance(data, dict):
                for key in ('auto_connect', 'auto_reconnect', 'audio_source', 'audio_source_label', 'audio_style', 'window_geometry'):
                    if type(data.get(key)) is type(getattr(self, key)):
                        setattr(self, key, data[key])
                if self.audio_style not in ('flow', 'bars'):
                    self.audio_style = 'flow'
            if isinstance(data, dict) and type(data.get('minimize_to_tray')) is bool:
                self.minimize_to_tray = data['minimize_to_tray']
            if isinstance(data, dict) and type(data.get('pause_in_background')) is bool:
                self.pause_in_background = data['pause_in_background']
        except (OSError, ValueError):
            pass

    def set_minimize(self, enabled):
        self.save(bool(enabled), self.pause_in_background)

    def save(self, minimize, pause, **changes):
        temporary = self.path.with_suffix('.tmp')
        data = {key: changes.get(key, getattr(self, key)) for key in
                ('auto_connect', 'auto_reconnect', 'audio_source', 'audio_source_label', 'audio_style', 'window_geometry')}
        if data['audio_style'] not in ('flow', 'bars'):
            raise ValueError('Unknown spectrum style')
        data.update(minimize_to_tray=bool(minimize), pause_in_background=bool(pause))
        temporary.write_text(json.dumps(data, ensure_ascii=False)+'\n', encoding='utf-8')
        temporary.replace(self.path)
        for key, value in data.items():
            setattr(self, key, value)


class Startup:
    def __init__(self, folder, name=RUN_NAME, key=RUN_KEY):
        self.folder, self.name, self.key = Path(folder).resolve(), name, key

    def command(self):
        exe = self.folder / 'AXONControl.exe'
        if not exe.is_file():
            raise OSError('找不到 AXONControl.exe，请将启动器放回程序目录。')
        return subprocess.list2cmdline([str(exe), '--start-in-tray'])

    def relocate_from(self, folders):
        """Preserve an existing opt-in when a known prior install is upgraded."""
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.key) as key:
                value, kind = winreg.QueryValueEx(key, self.name)
            if kind != winreg.REG_SZ:
                return
            for folder in folders:
                try:
                    if value == Startup(folder, self.name, self.key).command():
                        self.set_enabled(True)
                        return
                except OSError:
                    continue
        except FileNotFoundError:
            pass

    def enabled(self):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.key) as key:
                value, kind = winreg.QueryValueEx(key, self.name)
            return kind == winreg.REG_SZ and value == self.command()
        except (OSError, ValueError):
            return False

    def set_enabled(self, enabled):
        if enabled:
            command = self.command()
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, self.key, 0, winreg.KEY_SET_VALUE) as key:
                winreg.SetValueEx(key, self.name, 0, winreg.REG_SZ, command)
        else:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.key, 0, winreg.KEY_SET_VALUE) as key:
                    winreg.DeleteValue(key, self.name)
            except FileNotFoundError:
                pass


class NotifyData(C.Structure):
    _fields_ = [('cbSize', W.DWORD), ('hWnd', W.HWND), ('uID', W.UINT),
                ('uFlags', W.UINT), ('uCallbackMessage', W.UINT), ('hIcon', W.HICON),
                ('szTip', W.WCHAR*128), ('dwState', W.DWORD), ('dwStateMask', W.DWORD),
                ('szInfo', W.WCHAR*256), ('uVersion', W.UINT), ('szInfoTitle', W.WCHAR*64),
                ('dwInfoFlags', W.DWORD), ('guidItem', C.c_byte*16), ('hBalloonIcon', W.HICON)]


WNDPROC = C.WINFUNCTYPE(C.c_ssize_t, W.HWND, W.UINT, W.WPARAM, W.LPARAM)


class WindowClass(C.Structure):
    _fields_ = [('style', W.UINT), ('lpfnWndProc', WNDPROC), ('cbClsExtra', C.c_int),
                ('cbWndExtra', C.c_int), ('hInstance', W.HINSTANCE), ('hIcon', W.HICON),
                ('hCursor', W.HANDLE), ('hbrBackground', W.HBRUSH),
                ('lpszMenuName', W.LPCWSTR), ('lpszClassName', W.LPCWSTR)]


class TrayIcon:
    MESSAGE = 0x8001

    def __init__(self, root, icon_path, restore, quit_app):
        self.root, self.restore, self.quit_app = root, restore, quit_app
        self.on_resume = None
        self.on_devices_changed = None
        self.visible = False
        self.closed = False
        self.events = deque()
        self.poll_id = None
        self.hwnd = self.hicon = None
        self.registered = False
        self.user = C.WinDLL('user32', use_last_error=True)
        self.shell = C.WinDLL('shell32', use_last_error=True)
        signatures = {
            'DefWindowProcW': ([W.HWND, W.UINT, W.WPARAM, W.LPARAM], C.c_ssize_t),
            'RegisterClassW': ([C.POINTER(WindowClass)], W.ATOM),
            'UnregisterClassW': ([W.LPCWSTR, W.HINSTANCE], W.BOOL),
            'CreateWindowExW': ([W.DWORD, W.LPCWSTR, W.LPCWSTR, W.DWORD, C.c_int, C.c_int,
                                 C.c_int, C.c_int, W.HWND, W.HMENU, W.HINSTANCE, W.LPVOID], W.HWND),
            'DestroyWindow': ([W.HWND], W.BOOL),
            'LoadImageW': ([W.HINSTANCE, W.LPCWSTR, W.UINT, C.c_int, C.c_int, W.UINT], W.HANDLE),
            'DestroyIcon': ([W.HICON], W.BOOL),
            'RegisterWindowMessageW': ([W.LPCWSTR], W.UINT),
            'CreatePopupMenu': ([], W.HMENU),
            'AppendMenuW': ([W.HMENU, W.UINT, C.c_size_t, W.LPCWSTR], W.BOOL),
            'SetForegroundWindow': ([W.HWND], W.BOOL),
            'GetCursorPos': ([C.POINTER(W.POINT)], W.BOOL),
            'TrackPopupMenu': ([W.HMENU, W.UINT, C.c_int, C.c_int, C.c_int, W.HWND, W.LPVOID], W.UINT),
            'DestroyMenu': ([W.HMENU], W.BOOL),
            'PostMessageW': ([W.HWND, W.UINT, W.WPARAM, W.LPARAM], W.BOOL),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.user, name)
            function.argtypes, function.restype = args, result
        self.shell.Shell_NotifyIconW.argtypes = [W.DWORD, C.POINTER(NotifyData)]
        self.shell.Shell_NotifyIconW.restype = W.BOOL
        self.callback = WNDPROC(self._procedure)
        self.class_name = f'AXONControlTray_{id(self)}'
        self.window_class = WindowClass(lpfnWndProc=self.callback, lpszClassName=self.class_name)
        self.restart_message = self.user.RegisterWindowMessageW('TaskbarCreated')
        try:
            if not self.user.RegisterClassW(C.byref(self.window_class)):
                raise C.WinError(C.get_last_error())
            self.registered = True
            self.hwnd = self.user.CreateWindowExW(0, self.class_name, '', 0, 0, 0, 0, 0,
                                                  None, None, None, None)
            if not self.hwnd:
                raise C.WinError(C.get_last_error())
            self.hicon = self.user.LoadImageW(None, str(icon_path), 1, 32, 32, 0x10)
            if not self.hicon:
                raise C.WinError(C.get_last_error())
            self.data = NotifyData(cbSize=C.sizeof(NotifyData), hWnd=self.hwnd, uID=1,
                                   uFlags=7, uCallbackMessage=self.MESSAGE, hIcon=self.hicon,
                                   szTip='AXON Control · 点击显示，右键打开菜单')
            self.poll_id = self.root.after(40, self._drain_events)
        except Exception:
            self.close()
            raise

    def show(self):
        if not self.visible:
            # TaskbarCreated is also broadcast after DPI changes, when the icon
            # may still exist. In that case refresh the existing entry.
            if not (self.shell.Shell_NotifyIconW(0, C.byref(self.data)) or
                    self.shell.Shell_NotifyIconW(1, C.byref(self.data))):
                raise OSError('Windows 托盘暂不可用，窗口保持显示。')
            self.visible = True
            # Return to fast delivery immediately when entering the tray.
            if self.poll_id is not None:
                self.root.after_cancel(self.poll_id)
                self.poll_id = self.root.after(40, self._drain_events)

    def hide(self):
        if self.visible:
            self.shell.Shell_NotifyIconW(2, C.byref(self.data))
            self.visible = False

    def _procedure(self, hwnd, message, wparam, lparam):
        # Never call Tk here, including after() or report_callback_exception().
        # Tk dispatches native messages while its Python thread state is detached.
        # Re-entering _tkinter from this ctypes callback corrupts that state.
        try:
            if message == 0x219 and wparam in (7, 0x8000, 0x8004):
                if 'devices' not in self.events:
                    self.events.append('devices')
                return 1
            if message == 0x218 and wparam in (7, 18):
                if 'resume' not in self.events:
                    self.events.append('resume')
                return 1
            if message == self.MESSAGE:
                if not self.closed and self.visible:
                    if lparam in (0x202, 0x203, 0x400, 0x401):
                        if 'restore' not in self.events:
                            self.events.append('restore')
                    elif lparam in (0x205, 0x7B):
                        if 'menu' not in self.events:
                            self.events.append('menu')
                return 0
            if message == self.restart_message and self.visible:
                if 'restart' not in self.events:
                    self.events.append('restart')
                return 0
        except Exception:
            self.events.append(sys.exc_info())
        return self.user.DefWindowProcW(hwnd, message, wparam, lparam)

    def _drain_events(self):
        self.poll_id = None
        try:
            while self.events and not self.closed:
                event = self.events.popleft()
                if isinstance(event, tuple):
                    self.root.report_callback_exception(*event)
                elif event == 'resume' and self.on_resume:
                    self.on_resume()
                elif event == 'devices' and self.on_devices_changed:
                    self.on_devices_changed()
                elif self.visible:
                    if event == 'restore':
                        self.restore()
                    elif event == 'menu':
                        self._menu()
                    elif event == 'restart':
                        self.visible = False
                        try:
                            self.show()
                        except OSError:
                            self.restore()
        finally:
            if not self.closed:
                self.poll_id = self.root.after(40 if self.visible else 250, self._drain_events)

    def _menu(self):
        menu = self.user.CreatePopupMenu()
        if not menu:
            self.restore()
            return
        try:
            self.user.AppendMenuW(menu, 0, 1, '显示 AXON Control')
            self.user.AppendMenuW(menu, 0x800, 0, None)
            self.user.AppendMenuW(menu, 0, 2, '退出')
            point = W.POINT()
            self.user.GetCursorPos(C.byref(point))
            self.user.SetForegroundWindow(self.hwnd)
            choice = self.user.TrackPopupMenu(menu, 0x100 | 0x2, point.x, point.y, 0, self.hwnd, None)
            self.user.PostMessageW(self.hwnd, 0, 0, 0)
        finally:
            self.user.DestroyMenu(menu)
        if choice == 1:
            self.restore()
        elif choice == 2:
            # Show any save/operation errors instead of leaving an invisible app.
            self.restore()
            self.quit_app()

    def close(self):
        self.closed = True
        self.events.clear()
        if self.poll_id is not None:
            self.root.after_cancel(self.poll_id)
            self.poll_id = None
        self.hide()
        if self.hwnd:
            self.user.DestroyWindow(self.hwnd)
            self.hwnd = None
        if self.hicon:
            self.user.DestroyIcon(self.hicon)
            self.hicon = None
        if self.registered:
            self.user.UnregisterClassW(self.class_name, None)
            self.registered = False
