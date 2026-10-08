"""Desktop integration checks; no speaker/audio and no production startup entry."""
import ctypes
import os
from pathlib import Path
import tempfile
import subprocess
import sys
import tkinter as tk
import unittest
from unittest.mock import Mock, patch
import uuid
import winreg

from desktop_integration import Preferences, Startup, TrayIcon, NotifyData
from axon_control import App


class DesktopTests(unittest.TestCase):
    def test_settings_roundtrip_and_corrupt_file(self):
        with tempfile.TemporaryDirectory() as folder:
            prefs = Preferences(folder)
            self.assertTrue(prefs.minimize_to_tray)
            prefs.set_minimize(False)
            self.assertFalse(Preferences(folder).minimize_to_tray)
            prefs.path.write_text('[]')
            self.assertTrue(Preferences(folder).minimize_to_tray)
            prefs.path.write_text('{broken')
            self.assertTrue(Preferences(folder).minimize_to_tray)

    def test_failed_setting_write_keeps_previous_preference(self):
        with tempfile.TemporaryDirectory() as folder:
            prefs = Preferences(folder)
            with patch.object(Path, 'replace', side_effect=PermissionError):
                with self.assertRaises(OSError):
                    prefs.set_minimize(False)
            self.assertTrue(prefs.minimize_to_tray)

    def test_startup_registry_roundtrip_isolated_from_run_key(self):
        key = 'Software\\AXONControlTest_' + uuid.uuid4().hex
        with tempfile.TemporaryDirectory() as folder:
            # Only its existence is inspected; never execute this placeholder.
            (Path(folder) / 'AXONControl.exe').write_bytes(b'test-placeholder')
            startup = Startup(folder, key=key)
            try:
                self.assertFalse(startup.enabled())
                startup.set_enabled(True)
                self.assertTrue(startup.enabled())
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as handle:
                    value, _ = winreg.QueryValueEx(handle, startup.name)
                self.assertIn('AXONControl.exe', value)
                self.assertTrue(value.endswith(' --start-in-tray'))
                startup.set_enabled(False)
                self.assertFalse(startup.enabled())
                startup.set_enabled(False)
            finally:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)

    def test_missing_launcher_does_not_create_startup(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(OSError):
                Startup(folder).set_enabled(True)

    @unittest.skipIf(os.environ.get('AXON_CONTROL_SKIP_NATIVE_UI') == '1',
                     'Requires an interactive Windows desktop with Explorer.')
    def test_real_tray_lifecycle_and_explorer_restart(self):
        root = tk.Tk()
        root.withdraw()
        restore, quit_app = Mock(), Mock()
        tray = None
        try:
            tray = TrayIcon(root, Path(__file__).with_name('axon-icon.ico'), restore, quit_app)
            tray.show()
            self.assertTrue(tray.visible)
            tray._procedure(tray.hwnd, tray.restart_message, 0, 0)
            root.after(100, root.quit)
            root.mainloop()
            self.assertTrue(tray.visible)
            for event in (0x202, 0x203, 0x202):
                tray.user.PostMessageW(tray.hwnd, tray.MESSAGE, 1, event)
            root.after(100, root.quit)
            root.mainloop()
            restore.assert_called_once()
            tray.hide()
            self.assertFalse(tray.visible)
            tray.show()
            tray.close()
            tray.close()
            self.assertFalse(tray.visible)
            self.assertIsNone(tray.hwnd)
            self.assertIsNone(tray.hicon)
        finally:
            if tray:
                tray.close()
            root.destroy()

    @unittest.skipIf(os.environ.get('AXON_CONTROL_SKIP_NATIVE_UI') == '1',
                     'Requires an interactive Windows desktop with Explorer.')
    def test_native_messages_in_real_mainloop_do_not_abort_python(self):
        result = subprocess.run([sys.executable, '-X', 'faulthandler', __file__, '--native-probe'],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('20 native double-click cycles; clean exit', result.stdout)

    def test_native_callback_never_enters_tk(self):
        tray = TrayIcon.__new__(TrayIcon)
        from collections import deque
        tray.events = deque()
        tray.root = Mock()
        tray.visible, tray.closed = True, False
        tray.restart_message = 0xC123
        for event in (0x202, 0x203, 0x202, 0x205):
            tray._procedure(1, tray.MESSAGE, 1, event)
        tray._procedure(1, tray.restart_message, 0, 0)
        self.assertEqual(list(tray.events), ['restore', 'menu', 'restart'])
        self.assertEqual(tray.root.mock_calls, [])

    def test_failed_tray_creation_keeps_window_accessible(self):
        app = App.__new__(App)
        app.root = Mock()
        app.root.grab_current.return_value = None
        app.tray = Mock()
        app.tray.show.side_effect = OSError('Explorer unavailable')
        app.status = Mock()
        app._hide_to_tray()
        app.root.withdraw.assert_not_called()
        self.assertIn('Explorer unavailable', app.status.set.call_args.args[0])

    def test_minimize_respects_preference(self):
        app = App.__new__(App)
        app.desktop = Mock(minimize_to_tray=False)
        app._hide_to_tray = Mock()
        app._minimize_taskbar = Mock()
        app._minimize()
        app._minimize_taskbar.assert_called_once()
        app._hide_to_tray.assert_not_called()
        app.desktop.minimize_to_tray = True
        app._minimize()
        app._hide_to_tray.assert_called_once()


def native_probe():
    """Runs in its own process so a native runtime abort fails the parent test."""
    import shutil
    import axon_control
    from desktop_integration import set_taskbar_identity, APP_ID
    set_taskbar_identity()
    shell = ctypes.WinDLL('shell32')
    shell.GetCurrentProcessExplicitAppUserModelID.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
    value = ctypes.c_void_p()
    assert shell.GetCurrentProcessExplicitAppUserModelID(ctypes.byref(value)) == 0
    try:
        assert ctypes.wstring_at(value) == APP_ID
    finally:
        ole = ctypes.WinDLL('ole32')
        ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        ole.CoTaskMemFree(value)
    with tempfile.TemporaryDirectory() as folder:
        folder = Path(folder)
        for name in ('axon-icon.png', 'axon-icon.ico'):
            shutil.copy2(Path(__file__).with_name(name), folder / name)
        with patch.object(axon_control, 'HERE', folder), patch('audio_view.AudioPanel.start'):
            root = tk.Tk()
            errors = []
            root.report_callback_exception = lambda *args: (errors.append(str(args)), root.quit())
            app = App(root)
            app.settings()
            root.update_idletasks()
            self_settings = app.settings_window
            assert self_settings.winfo_exists()
            assert self_settings.winfo_reqheight() < root.winfo_screenheight()
            self_settings.destroy()
            from tests_audition import audition_app
            sample = audition_app()
            app.library.save('测试预设', '音乐', dict(sample.snapshot, active_slot=sample.slot,
                                                   current=sample.current.as_dict()))
            app.preset_library()
            root.update_idletasks()
            def widgets(parent):
                for child in parent.winfo_children():
                    yield child
                    yield from widgets(child)
            listing = next(w for w in widgets(app.library_window) if isinstance(w, tk.Listbox))
            listing.selection_set(0)
            listing.event_generate('<<ListboxSelect>>')
            root.update()
            assert not errors, errors
            app.library_window.destroy()
            cycles = []
            def hide():
                app._hide_to_tray()
                assert root.state() == 'withdrawn'
                assert app.tray.visible
                for event in (0x202, 0x203, 0x202):
                    assert app.tray.user.PostMessageW(app.tray.hwnd, app.tray.MESSAGE, 1, event)
                root.after(100, check)
            def check():
                assert root.state() == 'normal'
                assert not app.tray.visible
                # Read back icons from the actual Windows taskbar wrapper.
                for kind, handle in zip((1, 0), app.window_icons.handles):
                    assert app.window_icons.user.SendMessageW(app.native_hwnd, 0x7F, kind, 0) == handle
                cycles.append(True)
                if len(cycles) < 20:
                    root.after(10, hide)
                else:
                    app._hide_to_tray()
                    # Dispatch the native right-click event too; select Exit in
                    # the menu stub so CI does not wait for human menu input.
                    app.tray.user.TrackPopupMenu = Mock(return_value=2)
                    app.tray.user.PostMessageW(app.tray.hwnd, app.tray.MESSAGE, 1, 0x205)
            root.after(100, hide)
            root.after(10000, lambda: (errors.append('timeout'), root.quit()))
            root.mainloop()
            assert not errors, errors
            assert len(cycles) == 20
            assert app.tray.closed and app.tray.poll_id is None and app.tray.hwnd is None
            print('20 native double-click cycles; clean exit')


if __name__ == '__main__':
    if '--native-probe' in sys.argv:
        native_probe()
    else:
        unittest.main()
