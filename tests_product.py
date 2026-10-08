import json
from pathlib import Path
import tempfile
import queue
import time
import tkinter as tk
import unittest
from unittest.mock import Mock
from preset_library import PresetLibrary
from user_data import prepare_data
from desktop_integration import Preferences, TrayIcon
from tests_audition import audition_app


class ProductTests(unittest.TestCase):
    def test_migration_keeps_originals_and_existing_user_data(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            old, new = base/'old', base/'new'
            old.mkdir()
            (old/'last-session.json').write_text('original')
            (old/'backups').mkdir()
            (old/'backups/a.json').write_text('backup')
            prepare_data(old, new)
            self.assertEqual((new/'last-session.json').read_text(), 'original')
            (new/'last-session.json').write_text('newer')
            prepare_data(old, new)
            self.assertEqual((new/'last-session.json').read_text(), 'newer')
            self.assertEqual((old/'last-session.json').read_text(), 'original')
            self.assertEqual((new/'backups/a.json').read_text(), 'backup')

    def test_preferences_remember_source_window_and_connection_options(self):
        with tempfile.TemporaryDirectory() as folder:
            prefs = Preferences(folder)
            prefs.save(True, True, auto_connect=True, auto_reconnect=False,
                       audio_source='Headphones', audio_source_label='耳机', window_geometry='1000x800+20+40')
            prefs.set_minimize(False)
            saved = Preferences(folder)
            self.assertTrue(saved.auto_connect)
            self.assertFalse(saved.auto_reconnect)
            self.assertEqual(saved.audio_source, 'Headphones')
            self.assertEqual(saved.window_geometry, '1000x800+20+40')

    def test_library_search_category_favorite_and_edit(self):
        app = audition_app()
        document = dict(app.snapshot, current=app.current.as_dict(), active_slot=app.slot)
        with tempfile.TemporaryDirectory() as folder:
            library = PresetLibrary(Path(folder)/'library.json')
            item = library.save('夜间听歌', '音乐', document)
            library.save('Movie', '电影', document)
            library.update(item['id'], favorite=True, name='夜间')
            result = library.search('夜', '音乐', True)
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]['document']['current']['raw_hex'], app.current.raw.hex())
            library.delete(item['id'])
            self.assertEqual(len(library.load()), 1)
            before = library.path.read_bytes()
            with self.assertRaises(ValueError):
                library.save('broken', '', {})
            self.assertEqual(library.path.read_bytes(), before)

    def test_disconnect_saves_draft_and_only_closes_port(self):
        app = audition_app()
        app.desktop = Mock(auto_reconnect=True)
        app.recovery_store = Mock()
        app._write_session = Mock()
        app.client = Mock()
        app._changed = Mock()
        app.device_label = Mock()
        app._run = lambda message, work, done, **kw: done(work())
        app._lost_device('断开')
        app.client.close.assert_called_once()
        self.assertEqual(app.client.mock_calls, [unittest.mock.call.close()])
        app._write_session.assert_called_once_with(app.recovery_store)
        self.assertFalse(app.connected)
        self.assertTrue(app.offline)
        self.assertTrue(app.want_connection)

    def test_native_resume_event_is_queued_and_handled_while_tray_hidden(self):
        root = tk.Tk()
        root.withdraw()
        tray = TrayIcon(root, Path(__file__).with_name('axon-icon.ico'), Mock(), Mock())
        tray.on_resume = Mock()
        try:
            tray.user.PostMessageW(tray.hwnd, 0x218, 18, 0)
            root.after(120, root.quit)
            root.mainloop()
            tray.on_resume.assert_called_once()
        finally:
            tray.close()
            root.destroy()

    def test_auto_connection_respects_manual_disconnect_and_busy_state(self):
        app = audition_app()
        app.root = Mock()
        app.watch_last = time.monotonic()
        app.resume_pending = False
        app.device_scan = queue.Queue()
        app.scan_running = True
        app.next_reconnect = 0
        app.want_connection = False
        app.connected = False
        app._remember_environment = Mock()
        app.connect = Mock()
        app.device_scan.put(True)
        app._device_watch()
        app.connect.assert_not_called()
        app.want_connection = True
        app.device_scan.put(True)
        app.busy = True
        app._device_watch()
        app.connect.assert_not_called()
        app.busy = False
        app._device_watch()
        app.connect.assert_called_once()
        self.assertGreater(app.next_reconnect, time.monotonic())


if __name__ == '__main__':
    unittest.main()
