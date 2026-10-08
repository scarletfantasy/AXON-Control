"""Single-instance activation and parent-death supervision without native Tk callbacks."""
import ctypes as C
from ctypes import wintypes as W
import multiprocessing as mp
import os
import threading
import time


class SingleInstance:
    def __init__(self, name='Local\\AXONControl.Desktop'):
        self.event = self.mutex = self.owner_mapping = self.owner_view = None
        self.primary = False
        self.foreground_granted = False
        self.kernel = C.WinDLL('kernel32', use_last_error=True)
        for key, args, result in (
            ('CreateMutexW', [W.LPVOID, W.BOOL, W.LPCWSTR], W.HANDLE),
            ('CreateEventW', [W.LPVOID, W.BOOL, W.BOOL, W.LPCWSTR], W.HANDLE),
            ('SetEvent', [W.HANDLE], W.BOOL),
            ('WaitForSingleObject', [W.HANDLE, W.DWORD], W.DWORD),
            ('CreateFileMappingW', [W.HANDLE, W.LPVOID, W.DWORD, W.DWORD, W.DWORD, W.LPCWSTR], W.HANDLE),
            ('MapViewOfFile', [W.HANDLE, W.DWORD, W.DWORD, W.DWORD, C.c_size_t], W.LPVOID),
            ('UnmapViewOfFile', [W.LPCVOID], W.BOOL),
            ('CloseHandle', [W.HANDLE], W.BOOL)):
            fn = getattr(self.kernel, key)
            fn.argtypes, fn.restype = args, result
        try:
            self.event = self.kernel.CreateEventW(None, False, False, name+'.activate')
            if not self.event:
                raise C.WinError(C.get_last_error())
            self.mutex = self.kernel.CreateMutexW(None, False, name)
            self.primary = C.get_last_error() != 183
            if not self.mutex:
                raise C.WinError(C.get_last_error())
            # Share only the owner's PID in session-local kernel memory. The
            # process started by the user's double-click can grant foreground
            # permission to that owner before requesting window activation.
            self.owner_mapping = self.kernel.CreateFileMappingW(
                C.c_void_p(-1), None, 4, 0, C.sizeof(W.DWORD), name+'.owner')
            if not self.owner_mapping:
                raise C.WinError(C.get_last_error())
            self.owner_view = self.kernel.MapViewOfFile(
                self.owner_mapping, 2 if self.primary else 4, 0, 0, C.sizeof(W.DWORD))
            if not self.owner_view:
                raise C.WinError(C.get_last_error())
            if self.primary:
                C.cast(self.owner_view, C.POINTER(W.DWORD)).contents.value = os.getpid()
            else:
                # A second launch may arrive between mutex creation and PID
                # publication. Wait briefly in this short-lived process only.
                deadline = time.monotonic()+.2
                while not self.owner_pid and time.monotonic() < deadline:
                    time.sleep(.01)
                self.request_activation()
        except BaseException:
            self.close()
            raise

    @property
    def owner_pid(self):
        return C.cast(self.owner_view, C.POINTER(W.DWORD)).contents.value if self.owner_view else 0

    def request_activation(self):
        # Do not grant permission to ASFW_ANY. The named mapping identifies
        # exactly the UI process owning this instance's mutex and event.
        # https://learn.microsoft.com/windows/win32/api/winuser/nf-winuser-allowsetforegroundwindow
        if self.owner_pid:
            user = C.WinDLL('user32', use_last_error=True)
            user.AllowSetForegroundWindow.argtypes = [W.DWORD]
            user.AllowSetForegroundWindow.restype = W.BOOL
            self.foreground_granted = bool(user.AllowSetForegroundWindow(self.owner_pid))
        if not self.kernel.SetEvent(self.event):
            raise C.WinError(C.get_last_error())

    def requested(self):
        return self.kernel.WaitForSingleObject(self.event, 0) == 0

    def close(self):
        if getattr(self, 'owner_view', None):
            if self.primary:
                C.cast(self.owner_view, C.POINTER(W.DWORD)).contents.value = 0
            self.kernel.UnmapViewOfFile(self.owner_view)
            self.owner_view = None
        for field in ('owner_mapping', 'mutex', 'event'):
            handle = getattr(self, field, None)
            if handle:
                self.kernel.CloseHandle(handle)
                setattr(self, field, None)


def supervise_parent():
    parent = mp.parent_process()
    if parent is not None:
        def watch():
            parent.join()
            # The UI is gone; bypass possibly blocked native audio cleanup.
            os._exit(0)
        threading.Thread(target=watch, name='AXON parent guard', daemon=True).start()
