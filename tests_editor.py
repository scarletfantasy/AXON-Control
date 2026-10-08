"""Offline checks of editing history, unit parsing and preview-only operations."""
import json
from pathlib import Path
import unittest
from unittest.mock import Mock

from axon_protocol import Preset
from axon_control import App
from editor_state import (EditHistory, FieldError, changed_fields, parse_parameter,
                          editable_import, preset_form, preview_from_form,
                          offline_document, read_document, rebase_preview)

from synthetic_fixtures import ORIGINAL


def replace_field(form, band, column, value):
    rows = [list(row) for row in form]
    rows[band][column] = value
    return tuple(tuple(row) for row in rows)


class Variable:
    def __init__(self, value=''):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class ForbiddenMidi:
    def __getattr__(self, name):
        raise AssertionError(f'Local editing attempted a MIDI operation: {name}')


def memory_app():
    app = App.__new__(App)
    app.current, app.suppress, app.selected_band = ORIGINAL, False, 1
    app.offline, app.connected = False, False
    app.audition = None
    app.review = None
    app.active_review = None
    app.plot_gain_range = 12
    app.plot_range_button = Mock()
    app.plot_drag_mode = 'free'
    app.plot_reference = None
    app.plot_reference_view = None
    app.drag_mode_button = Mock()
    app.copy_band_button, app.paste_band_button, app.zero_gain_button = Mock(), Mock(), Mock()
    app.drag_index = None
    app.busy, app.closing = False, False
    from editor_state import FIELDS
    app.values = [dict(zip(FIELDS, map(Variable, row))) for row in preset_form(ORIGINAL)]
    app.history = EditHistory()
    app.history.reset(app._form())
    app.snapshot = {'presets': [ORIGINAL.as_dict()]}
    app.slot = 0
    app.client = ForbiddenMidi()
    app.cards = {}
    app.edit_label, app.status = Variable(), Variable()
    app.edit_status_label = type('Label', (), {'configure': lambda *args, **kwargs: None})()
    app._controls = lambda: None
    app._refresh_tiles = lambda preset: None
    app.draw = lambda preset=None: None
    return app


class EditorTests(unittest.TestCase):
    def test_frequency_and_gain_units(self):
        for text, field, value in [('1k', 'frequency', 1000), ('1.6 kHz', 'frequency', 1600),
                                   ('160Hz', 'frequency', 160), ('1e3 Hz', 'frequency', 1000),
                                   ('−3.3dB', 'gain', -3.3), ('1.2 Q', 'q', 1.2)]:
            with self.subTest(text=text):
                self.assertEqual(parse_parameter(text, field), value)

    def test_invalid_units_and_nonfinite_numbers_are_rejected(self):
        for text, field in [('1db', 'frequency'), ('1k', 'gain'), ('1e999 Hz', 'frequency'),
                            ('nan', 'gain'), ('inf', 'q'), ('1,5k', 'frequency'), ('', 'q')]:
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    parse_parameter(text, field)

    def test_units_only_reformatting_preserves_every_byte(self):
        form = preset_form(ORIGINAL)
        form = replace_field(form, 1, 1, '0.16kHz')
        form = replace_field(form, 1, 3, '-4dB')
        result = preview_from_form(ORIGINAL, form)
        self.assertEqual(result.raw, ORIGINAL.raw)
        self.assertEqual(changed_fields(ORIGINAL, result), ())

    def test_invalid_range_identifies_the_field(self):
        for band, column, field, value in [(1, 1, 'frequency', '21k'), (2, 2, 'q', '0'),
                                           (3, 3, 'gain', '+13dB')]:
            with self.subTest(field=field):
                with self.assertRaises(FieldError) as caught:
                    preview_from_form(ORIGINAL, replace_field(preset_form(ORIGINAL), band, column, value))
                self.assertEqual((caught.exception.index, caught.exception.field), (band, field))

    def test_changes_count_and_cutoff_internal_fields(self):
        form = preset_form(ORIGINAL)
        form = replace_field(form, 1, 1, '200 Hz')
        form = replace_field(form, 1, 3, '-2dB')
        form = replace_field(form, 6, 2, 'invalid hidden field')
        result = preview_from_form(ORIGINAL, form)
        self.assertEqual(changed_fields(ORIGINAL, result), ((1, 'frequency'), (1, 'gain')))
        self.assertEqual(result.raw[14+6*16:], ORIGINAL.raw[14+6*16:])

    def test_typing_and_drag_are_coalesced_into_one_undo(self):
        history = EditHistory()
        history.reset(('160',))
        for now, text in [(1, '1'), (1.2, '1k'), (1.4, '1.2k')]:
            history.record((text,), group='frequency', now=now)
        self.assertEqual(history.undo(), ('160',))
        self.assertFalse(history.can_undo)
        self.assertEqual(history.redo(), ('1.2k',))

    def test_boundary_and_elapsed_time_create_separate_steps(self):
        history = EditHistory()
        history.reset((0,))
        history.record((1,), group='gain', now=1)
        history.end_group()
        history.record((2,), group='gain', now=1.1)
        history.record((3,), group='gain', now=4)
        self.assertEqual([history.undo(), history.undo(), history.undo()], [(2,), (1,), (0,)])

    def test_new_edit_after_undo_discards_redo(self):
        history = EditHistory()
        history.reset((0,))
        history.record((1,))
        history.undo()
        history.record((2,))
        self.assertFalse(history.can_redo)
        self.assertEqual(history.undo(), (0,))

    def test_noop_does_not_discard_redo(self):
        history = EditHistory()
        history.reset((0,))
        history.record((1,))
        history.undo()
        self.assertFalse(history.record((0,)))
        self.assertEqual(history.redo(), (1,))

    def test_history_limit_and_cancelled_drag(self):
        history = EditHistory(limit=2)
        history.reset((0,))
        for value in (1, 2, 3):
            history.record((value,))
        self.assertEqual([history.undo(), history.undo(), history.undo()], [(2,), (1,), None])
        history.reset((0,))
        history.record((1,), group='drag', now=1)
        history.record((0,), group='drag', now=1.1)
        self.assertFalse(history.can_undo)

    def test_invalid_input_is_undoable_without_midi(self):
        app = memory_app()
        app.values[1]['frequency'].set('bad')
        app._changed((1, 'frequency'))
        self.assertIn('频率有误', app.edit_label.get())
        app.undo()
        self.assertEqual(app.target().raw, ORIGINAL.raw)
        app.redo()
        self.assertEqual(app.values[1]['frequency'].get(), 'bad')
        self.assertEqual(app.current.raw, ORIGINAL.raw)

    def test_discard_can_be_undone_without_midi(self):
        app = memory_app()
        app.values[1]['gain'].set('-2')
        app._changed((1, 'gain'))
        changed = app.target().raw
        app.discard()
        self.assertEqual(app.target().raw, ORIGINAL.raw)
        app.undo()
        self.assertEqual(app.target().raw, changed)
        self.assertEqual(app.current.raw, ORIGINAL.raw)

    def test_restore_one_band_preserves_other_edits_without_midi(self):
        app = memory_app()
        app.values[1]['gain'].set('-2')
        app._changed((1, 'gain'))
        app.values[2]['frequency'].set('2.5k')
        app._changed((2, 'frequency'))
        app.restore_band()
        preview = app.target()
        self.assertEqual(preview.bands[1].gain, ORIGINAL.bands[1].gain)
        self.assertEqual(preview.bands[2].frequency, 2500)
        app.undo()
        self.assertEqual(app.target().bands[1].gain, -2)
        self.assertEqual(app.current.raw, ORIGINAL.raw)

    def test_reconnect_keeps_unrelated_device_changes(self):
        draft = ORIGINAL.with_field(1, 'gain', -2)
        actual = ORIGINAL.with_field(2, 'frequency', 2500)
        result = rebase_preview(ORIGINAL, draft, actual)
        self.assertEqual(result.bands[1].gain, -2)
        self.assertEqual(result.bands[2].frequency, 2500)
        self.assertEqual(changed_fields(actual, result), ((1, 'gain'),))

    def test_reconnect_rejects_conflicting_device_change(self):
        draft = ORIGINAL.with_field(1, 'gain', -2)
        actual = ORIGINAL.with_field(1, 'gain', -3)
        with self.assertRaises(FieldError) as caught:
            rebase_preview(ORIGINAL, draft, actual)
        self.assertEqual((caught.exception.index, caught.exception.field), (1, 'gain'))

    def test_reconnect_accepts_already_matching_device_value(self):
        draft = ORIGINAL.with_field(1, 'gain', -2)
        self.assertEqual(rebase_preview(ORIGINAL, draft, draft).raw, draft.raw)

    def test_exported_draft_retains_its_working_parameter_source(self):
        source = ORIGINAL.with_field(1, 'gain', -3)
        preview = source.with_field(1, 'gain', -2)
        data = {'kind': 'axon-local-draft-v2', 'device': {'model': 'NFM-3'},
                'current': preview.as_dict(), 'source_current': source.as_dict(),
                'presets': [ORIGINAL.as_dict()], 'active_slot': 0, 'eq_enabled': True}
        document, loaded = offline_document(data)
        self.assertEqual(document['current'], source.as_dict())
        self.assertEqual(loaded.raw, preview.raw)
        actual = source.with_field(2, 'frequency', 2500)
        rebased = rebase_preview(source, loaded, actual)
        self.assertEqual(rebased.bands[1].gain, -2)
        self.assertEqual(rebased.bands[2].frequency, 2500)

    def test_legacy_draft_retains_pending_preview_with_saved_baseline(self):
        preview = ORIGINAL.with_field(1, 'gain', -2)
        data = {'kind': 'axon-local-draft-v1', 'device': {'model': 'NFM-3'},
                'current': preview.as_dict(), 'presets': [ORIGINAL.as_dict()],
                'active_slot': 0, 'eq_enabled': True}
        document, loaded = offline_document(data)
        self.assertEqual(document['current'], ORIGINAL.as_dict())
        self.assertEqual(loaded.raw, preview.raw)
        with self.assertRaises(FieldError):
            rebase_preview(ORIGINAL, loaded, ORIGINAL.with_field(1, 'gain', -3))

    def test_malformed_and_unknown_drafts_are_rejected(self):
        data = {'kind': 'axon-local-draft-v2', 'device': {'model': 'NFM-3'},
                'current': ORIGINAL.as_dict(), 'presets': [ORIGINAL.as_dict()],
                'active_slot': 0, 'eq_enabled': True}
        with self.assertRaisesRegex(ValueError, '原参数损坏'):
            offline_document(data)
        with self.assertRaisesRegex(ValueError, '版本暂不支持'):
            offline_document(dict(data, kind='axon-local-draft-v99'))

    def test_open_draft_then_reconnect_preserves_preview_without_applying(self):
        import tempfile
        source = ORIGINAL.with_field(1, 'gain', -3)
        preview = source.with_field(1, 'gain', -2)
        data = {'kind': 'axon-local-draft-v2', 'device': {'model': 'NFM-3'},
                'current': preview.as_dict(), 'source_current': source.as_dict(),
                'presets': [ORIGINAL.as_dict()], 'active_slot': 0, 'eq_enabled': True}
        app = memory_app()
        app.device_label = Variable()
        def load_snapshot(snapshot):
            app.snapshot, app.slot = snapshot, snapshot['active_slot']
            app._populate(Preset(bytes.fromhex(snapshot['current']['raw_hex'])))
        app._snapshot = load_snapshot
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'draft.json'
            path.write_text(json.dumps(data), encoding='utf-8')
            app.load_document_path(path)
        self.assertEqual(app.current.raw, source.raw)
        self.assertEqual(app.target().raw, preview.raw)
        self.assertTrue(app._pending())
        actual = source.with_field(2, 'frequency', 2500)
        app.client = Mock()
        app.client.connect.return_value = dict(app.snapshot, current=actual.as_dict())
        app._write_draft = Mock(return_value=Path('local-preserved-draft.json'))
        app._run = lambda message, work, done, **options: done(work())
        app.connect()
        app._write_draft.assert_called_once_with(preview)
        self.assertEqual(app.current.raw, actual.raw)
        self.assertEqual(app.target().bands[1].gain, -2)
        self.assertEqual(app.target().bands[2].frequency, 2500)
        app.client.apply.assert_not_called()
        app.client.save.assert_not_called()

    def test_draft_import_retains_reserved_and_cutoff_internal_bytes(self):
        import struct
        raw = bytearray(ORIGINAL.raw)
        struct.pack_into('<H', raw, 16, 77)
        struct.pack_into('<f', raw, 22, 1.3)
        actual = Preset(raw)
        result = editable_import(actual, ORIGINAL.with_field(1, 'gain', -2))
        self.assertEqual(result.raw[14:30], actual.raw[14:30])
        self.assertEqual(result.bands[1].gain, -2)

    def test_draft_for_different_name_is_rejected(self):
        raw = bytearray(ORIGINAL.raw)
        raw[0] = ord('X')
        with self.assertRaises(ValueError):
            rebase_preview(ORIGINAL, ORIGINAL.with_field(1, 'gain', -2), Preset(raw))
        with self.assertRaises(ValueError):
            editable_import(ORIGINAL, Preset(raw))

    def test_document_uses_names_from_raw_data(self):
        data = {'device': {'model': 'NFM-3'}, 'current': ORIGINAL.as_dict(),
                'presets': [ORIGINAL.as_dict()], 'active_slot': 0, 'eq_enabled': True}
        data['presets'][0]['name'] = 'Incorrect supplied label'
        self.assertEqual(read_document(data)['presets'][0]['name'], ORIGINAL.name)

    def test_malformed_documents_are_rejected(self):
        base = {'device': {'model': 'NFM-3'}, 'current': ORIGINAL.as_dict(),
                'presets': [ORIGINAL.as_dict()], 'active_slot': 0, 'eq_enabled': True}
        for key, value in [('device', None), ('active_slot', True), ('active_slot', 5),
                           ('eq_enabled', 'yes'), ('presets', []), ('current', None)]:
            with self.subTest(key=key):
                data = dict(base, **{key: value})
                with self.assertRaises(ValueError):
                    read_document(data)
        with self.assertRaises(ValueError):
            read_document([])

    def test_offline_hardware_actions_never_access_midi(self):
        app = memory_app()
        app.offline = True
        for action in (app.apply, app.restore, app.backup, app.read):
            action()
        self.assertEqual(app.current.raw, ORIGINAL.raw)

    def test_offline_save_exports_local_preview(self):
        app = memory_app()
        app.offline = True
        app.values[1]['gain'].set('-2dB')
        exported = []
        def write_draft(preview):
            exported.append(preview.raw)
            return Path('local-draft.json')
        app._write_draft = write_draft
        app.save()
        self.assertEqual(exported, [ORIGINAL.with_field(1, 'gain', -2).raw])
        self.assertEqual(app.current.raw, ORIGINAL.raw)

    def test_gui_passes_observed_slot_to_every_device_write(self):
        app = memory_app()
        app.connected = True
        app.client = Mock()
        app._run = lambda message, work, done: work()
        app.values[1]['gain'].set('-2')
        app.apply()
        self.assertEqual(app.client.apply.call_args.kwargs, {'expected_slot': 0})
        self.assertEqual(app.client.apply.call_args.args,
                         (ORIGINAL, ORIGINAL.with_field(1, 'gain', -2)))
        app._set_form(preset_form(ORIGINAL))
        app.save()
        app.client.save.assert_called_once_with(ORIGINAL, expected_slot=0)
        app.client.state.return_value = bytes((10, 1, 0, 0, 0, 0, 0, 0))
        app.client.preset.return_value = ORIGINAL
        app.restore()
        app.client.apply.assert_called_with(ORIGINAL, ORIGINAL, expected_slot=0)
        app.combo = Mock()
        app.combo.current.return_value = 1
        app.select()
        app.client.select.assert_called_once_with(1, expected_slot=0, expected=ORIGINAL)

    def test_offline_controls_allow_editing_and_disable_device_writes(self):
        class Control:
            def __init__(self):
                self.options = {}
            def configure(self, **options):
                self.options.update(options)
        app = memory_app()
        app.offline, app.busy, app.closing = True, False, False
        controls = ('read_button', 'apply_button', 'save_button', 'restore_button',
                    'undo_button', 'redo_button', 'more_button', 'compare_button', 'review_button', 'plot_range_button', 'drag_mode_button',
                    'copy_band_button', 'paste_band_button', 'zero_gain_button')
        for name in controls + ('connect_button', 'combo'):
            setattr(app, name, Control())
        app.audition_buttons = {side: Control() for side in ('A', 'B')}
        app.response_label, app.curve_note, app.compare_saved = Control(), Variable(), True
        app.buttons, app.editor_controls = tuple(getattr(app, name) for name in controls), []
        app.values[1]['gain'].set('-2')
        app._changed((1, 'gain'))
        App._controls(app)
        self.assertEqual(app.apply_button.options['state'], 'disabled')
        self.assertEqual(app.restore_button.options['state'], 'disabled')
        self.assertEqual(app.read_button.options['state'], 'disabled')
        self.assertEqual(app.save_button.options['text'], '导出草稿')
        self.assertEqual(app.save_button.options['state'], 'normal')
        self.assertEqual(app.undo_button.options['state'], 'normal')


if __name__ == '__main__':
    unittest.main()
