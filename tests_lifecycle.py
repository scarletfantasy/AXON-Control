import ctypes
import json
import multiprocessing as mp
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock
import uuid

from app_lifecycle import SingleInstance, supervise_parent
from axon_control import App
from editor_session import SessionStore
from tests_audition import audition_app


def guarded_worker(marker):
    supervise_parent()
    Path(marker).write_text(str(os.getpid()))
    time.sleep(30)


class LifecycleTests(unittest.TestCase):
    def test_single_instance_activation_and_reacquisition(self):
        name = 'Local\\AXONTest.' + uuid.uuid4().hex
        first = SingleInstance(name)
        try:
            self.assertTrue(first.primary)
            remote = subprocess.run([sys.executable, '-c',
                'from app_lifecycle import SingleInstance; import sys; '
                's=SingleInstance(sys.argv[1]); print(s.primary); s.close()', name],
                cwd=Path(__file__).parent, capture_output=True, text=True, timeout=5)
            self.assertEqual(remote.returncode, 0, remote.stderr)
            self.assertEqual(remote.stdout.strip(), 'False')
            self.assertTrue(first.requested())
            second = SingleInstance(name)
            try:
                self.assertFalse(second.primary)
                self.assertTrue(first.requested())
                self.assertFalse(first.requested())
            finally:
                second.close()
        finally:
            first.close()
        third = SingleInstance(name)
        self.assertTrue(third.primary)
        third.close()

    def test_autosave_retains_incomplete_input_and_skips_busy_writes(self):
        app = audition_app()
        app.root = Mock()
        app.autosave_signature = None
        with tempfile.TemporaryDirectory() as folder:
            app.recovery_store = SessionStore(Path(folder) / 'recovery.json')
            app.values[2]['frequency'].set('-')
            app._autosave()
            self.assertEqual(app.recovery_store.load()['form'][2][1], '-')
            before = app.recovery_store.path.read_bytes()
            app.busy = True
            app.values[2]['frequency'].set('1200')
            app._autosave()
            self.assertEqual(app.recovery_store.path.read_bytes(), before)
            app.busy = False
            app._autosave()
            self.assertEqual(app.recovery_store.load()['form'][2][1], '1200')

    def test_recovery_is_loaded_offline_without_applying_to_device(self):
        app = audition_app()
        with tempfile.TemporaryDirectory() as folder:
            app.recovery_store = SessionStore(Path(folder) / 'recovery.json')
            app.session_store = SessionStore(Path(folder) / 'session.json')
            app.values[2]['frequency'].set('-')
            app._write_session(app.recovery_store)
            app.device_label = Mock()
            app._snapshot = Mock()
            app.choose_band = Mock()
            app.set_plot_range = Mock()
            app.set_drag_mode = Mock()
            app.set_reference = Mock()
            app._changed = Mock()
            self.assertTrue(app.restore_session())
            self.assertTrue(app.offline)
            self.assertEqual(app.values[2]['frequency'].get(), '-')
            self.assertIn('自动保存', app.status.get())

    def test_background_resume_preserves_manual_pause(self):
        app = App.__new__(App)
        app.root = Mock()
        app.root.state.return_value = 'withdrawn'
        app.closing, app.native_hwnd = False, None
        app.desktop = Mock(pause_in_background=True)
        app.background_audio_resume = False
        panel = app.audio_panel = Mock(running=False, start_timer=None, visible=True)
        app._background_tick()
        app.root.state.return_value = 'normal'
        app._background_tick()
        panel.start.assert_not_called()
        app.root.state.return_value = 'withdrawn'
        panel.running = True
        app._background_tick()
        panel.toggle.assert_called_once()
        panel.running = False
        app.root.state.return_value = 'normal'
        app._background_tick()
        app._background_tick()
        panel.start.assert_called_once()

    def test_worker_exits_after_abrupt_parent_termination(self):
        with tempfile.TemporaryDirectory() as folder:
            marker = Path(folder) / 'pid.txt'
            parent = subprocess.Popen([sys.executable, __file__, '--guard-parent', str(marker)],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            kernel = ctypes.WinDLL('kernel32')
            kernel.OpenProcess.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_uint]
            kernel.OpenProcess.restype = ctypes.c_void_p
            kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            handle = None
            try:
                deadline = time.monotonic()+8
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(.05)
                self.assertTrue(marker.exists())
                handle = kernel.OpenProcess(0x100000, False, int(marker.read_text()))
                self.assertTrue(handle)
                parent.kill()
                parent.wait(timeout=3)
                self.assertEqual(kernel.WaitForSingleObject(handle, 5000), 0)
            finally:
                if parent.poll() is None:
                    parent.kill()
                parent.wait(timeout=3)
                if handle:
                    kernel.CloseHandle(handle)


if __name__ == '__main__':
    if '--guard-parent' in sys.argv:
        child = mp.get_context('spawn').Process(target=guarded_worker, args=(sys.argv[-1],))
        child.start()
        time.sleep(30)
    else:
        unittest.main()
