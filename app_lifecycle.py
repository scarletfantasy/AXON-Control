"""Single-instance activation and parent-death supervision without native Tk callbacks."""
import ctypes as C
from ctypes import wintypes as W
import multiprocessing as mp
import os
import threading


class SingleInstance:
    def __init__(self, name='Local\\AXONControl.Desktop'):
        self.kernel = C.WinDLL('kernel32', use_last_error=True)
        for key, args, result in (
            ('CreateMutexW', [W.LPVOID, W.BOOL, W.LPCWSTR], W.HANDLE),
            ('CreateEventW', [W.LPVOID, W.BOOL, W.BOOL, W.LPCWSTR], W.HANDLE),
            ('SetEvent', [W.HANDLE], W.BOOL),
            ('WaitForSingleObject', [W.HANDLE, W.DWORD], W.DWORD),
            ('CloseHandle', [W.HANDLE], W.BOOL)):
            fn = getattr(self.kernel, key)
            fn.argtypes, fn.restype = args, result
        self.event = self.kernel.CreateEventW(None, False, False, name+'.activate')
        self.mutex = self.kernel.CreateMutexW(None, False, name)
        self.primary = C.get_last_error() != 183
        if not self.event or not self.mutex:
            self.close()
            raise C.WinError(C.get_last_error())
        if not self.primary:
            self.kernel.SetEvent(self.event)

    def requested(self):
        return self.kernel.WaitForSingleObject(self.event, 0) == 0

    def close(self):
        for field in ('mutex', 'event'):
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
