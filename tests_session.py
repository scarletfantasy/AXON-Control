"""Recovery across app restarts and close failures, without opening MIDI."""
from pathlib import Path
import queue
import tempfile
import unittest
from unittest.mock import patch

from axon_control import App
from editor_session import SESSION_KIND, SessionStore, read_session
from editor_state import preset_form, read_document
from tests_editor import ORIGINAL, memory_app, replace_field


def document(current=ORIGINAL):
    return {'device': {'model': 'NFM-3'}, 'current': current.as_dict(),
            'presets': [ORIGINAL.as_dict()], 'active_slot': 0, 'eq_enabled': True}


class Root:
    def __init__(self):
        self.destroyed = False
        self.callbacks = []

    def destroy(self):
        self.destroyed = True

    def after(self, delay, callback):
        self.callbacks.append(callback)


class ClosingClient:
    def __init__(self):
        self.closed = False
        self.port = None

    def close(self):
        self.closed = True

    def __getattr__(self, name):
        raise AssertionError(f'Recovery must not use MIDI: {name}')


class SessionTests(unittest.TestCase):
    def test_roundtrip_preserves_unfinished_units_and_device_context(self):
        form = replace_field(preset_form(ORIGINAL), 1, 1, '1.6 k')
        form = replace_field(form, 2, 3, '-')
        with tempfile.TemporaryDirectory() as folder:
            store = SessionStore(Path(folder) / 'last-session.json')
            store.save(document(), form, 2, False)
            result = store.load()
        self.assertEqual(result['form'], form)
        self.assertEqual(result['document'], read_document(document()))
        self.assertEqual((result['selected_band'], result['compare_saved']), (2, False))

    def test_failed_atomic_save_retains_the_previous_session(self):
        with tempfile.TemporaryDirectory() as folder:
            store = SessionStore(Path(folder) / 'last-session.json')
            store.save(document(), preset_form(ORIGINAL), 1, True)
            previous = store.path.read_bytes()
            with patch('editor_session.os.fsync', side_effect=OSError('disk full')):
                with self.assertRaisesRegex(OSError, 'disk full'):
                    store.save(document(), replace_field(preset_form(ORIGINAL), 1, 3, '-2'), 1, True)
            self.assertEqual(store.path.read_bytes(), previous)
            self.assertEqual(list(Path(folder).glob('*.tmp')), [])

    def test_replace_failure_also_retains_the_previous_session(self):
        with tempfile.TemporaryDirectory() as folder:
            store = SessionStore(Path(folder) / 'last-session.json')
            store.save(document(), preset_form(ORIGINAL), 1, True)
            previous = store.path.read_bytes()
            with patch.object(Path, 'replace', side_effect=PermissionError('file in use')):
                with self.assertRaises(PermissionError):
                    store.save(document(), preset_form(ORIGINAL), 2, False)
            self.assertEqual(store.path.read_bytes(), previous)
            self.assertEqual(list(Path(folder).glob('*.tmp')), [])

    def test_bad_session_fields_are_rejected_before_use(self):
        valid = {'kind': SESSION_KIND, 'document': document(), 'form': preset_form(ORIGINAL),
                 'selected_band': 1, 'compare_saved': True}
        bad_forms = [[], replace_field(valid['form'], 1, 0, 1),
                     replace_field(valid['form'], 1, 1, 160),
                     replace_field(valid['form'], 1, 3, 'x' * 513)]
        for key, value in [('kind', 'unknown'), ('document', None), ('selected_band', True),
                           ('selected_band', 7), ('compare_saved', 'yes')] + [('form', f) for f in bad_forms]:
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    read_session(dict(valid, **{key: value}))

    def test_close_saves_invalid_input_and_current_audition_separately(self):
        app = memory_app()
        app.root, app.client = Root(), ClosingClient()
        app.busy, app.closing, app.compare_saved = False, False, True
        applied = ORIGINAL.with_field(1, 'gain', -2)
        app.current, app.snapshot = applied, document(applied)
        app._set_form(preset_form(applied))
        app.values[1]['frequency'].set('1kH')
        with tempfile.TemporaryDirectory() as folder:
            app.session_store = SessionStore(Path(folder) / 'last-session.json')
            self.assertTrue(app.close())
            session = app.session_store.load()
        self.assertEqual(session['form'][1][1], '1kH')
        self.assertEqual(session['document']['current'], applied.as_dict())
        self.assertEqual(session['document']['presets'][0], ORIGINAL.as_dict())
        self.assertTrue(app.root.destroyed)
        self.assertTrue(app.client.closed)

    def test_failed_close_keeps_the_editor_and_midi_port_open(self):
        app = memory_app()
        app.root, app.client = Root(), ClosingClient()
        app.busy, app.closing = False, True
        app._write_session = lambda: (_ for _ in ()).throw(OSError('disk full'))
        self.assertFalse(app.close())
        self.assertFalse(app.root.destroyed)
        self.assertFalse(app.client.closed)
        self.assertFalse(app.closing)
        self.assertIn('disk full', app.status.get())

    def test_close_during_apply_preserves_the_successful_readback(self):
        app = memory_app()
        app.root, app.client = Root(), ClosingClient()
        app.busy, app.closing = True, False
        app.results = queue.Queue()
        applied = ORIGINAL.with_field(1, 'gain', -2)
        saved = []
        app._write_session = lambda: saved.append(app.current.raw)
        self.assertFalse(app.close())
        self.assertFalse(app.root.destroyed)
        app.results.put((app._populate, applied, None))
        app._poll()
        self.assertEqual(saved, [applied.raw])
        self.assertTrue(app.root.destroyed)

    def test_restore_is_offline_and_never_opens_or_writes_midi(self):
        app = memory_app()
        app.compare_saved = True
        app.device_label = type(app.status)()
        chosen = []
        app._snapshot = lambda data: app._populate(ORIGINAL)
        app.choose_band = lambda index: chosen.append(index)
        app.toggle_comparison = lambda: setattr(app, 'compare_saved', not app.compare_saved)
        form = replace_field(preset_form(ORIGINAL), 1, 3, '-2dB')
        form = replace_field(form, 2, 1, '-')
        with tempfile.TemporaryDirectory() as folder:
            app.session_store = SessionStore(Path(folder) / 'last-session.json')
            app.session_store.save(document(), form, 2, False)
            self.assertTrue(app.restore_session())
        self.assertTrue(app.offline)
        self.assertFalse(app.connected)
        self.assertEqual(app._form(), form)
        self.assertEqual(chosen, [2])
        self.assertFalse(app.compare_saved)
        self.assertEqual(app.current.raw, ORIGINAL.raw)
        app.undo()
        self.assertEqual(app.target().raw, ORIGINAL.raw)

    def test_error_resets_picker_and_preserves_offline_form(self):
        app = memory_app()
        app.root, app.client = Root(), ClosingClient()
        app.busy, app.closing = True, False
        app.results = queue.Queue()
        app.device_label, app.eq_label = type(app.status)(), type(app.status)()
        app.snapshot = document()
        restored_slots = []
        app.combo = type('Picker', (), {'current': lambda _, slot: restored_slots.append(slot)})()
        app.values[1]['gain'].set('-2dB')
        app.results.put((None, None, TimeoutError('connection lost')))
        app._poll()
        self.assertEqual(restored_slots, [0])
        self.assertTrue(app.offline)
        self.assertEqual(app._form()[1][3], '-2dB')
        self.assertIn('connection lost', app.status.get())


if __name__ == '__main__':
    unittest.main()
