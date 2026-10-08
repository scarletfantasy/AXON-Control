"""Local band transfer and gain reset without a real clipboard, Tk or MIDI."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import tkinter as tk
import unittest
from unittest.mock import Mock

from axon_control import App
from band_clipboard import copy_snippet, paste_snippet, read_snippet
from editor_session import SessionStore
from editor_state import changed_fields, preset_form
from tests_audition import install_controls
from tests_editor import ORIGINAL, memory_app, replace_field
from tests_session import document


class ClipboardRoot:
    def grab_current(self):
        return None

    def __init__(self, text=''):
        self.text = text
        self.clears = self.reads = 0
        self.fail_read = self.fail_write = False
        self.bindings = {}

    def clipboard_clear(self):
        if self.fail_write:
            raise tk.TclError('clipboard unavailable')
        self.clears += 1
        self.text = ''

    def clipboard_append(self, text):
        self.text += text

    def clipboard_get(self):
        self.reads += 1
        if self.fail_read:
            raise tk.TclError('no string data')
        return self.text

    def bind(self, key, callback):
        self.bindings[key] = callback


def clipboard_app():
    app = memory_app()
    app.offline = True
    app.root = ClipboardRoot()
    return app


def modified_form():
    form = preset_form(ORIGINAL)
    for column, value in ((0, False), (1, '1.6kHz'), (2, '2Q'), (3, '-1.5dB')):
        form = replace_field(form, 1, column, value)
    return form


class ClipboardTests(unittest.TestCase):
    def test_peak_snippet_has_only_supported_values_and_normalizes_units(self):
        form = modified_form()
        text = copy_snippet(ORIGINAL, form, 1)
        data = read_snippet(text)
        self.assertEqual(data, {'kind': 'axon-band-v1', 'source_band': 'LF', 'band_kind': 'peak',
                                'values': {'enabled': False, 'frequency': 1600, 'q': 2, 'gain': -1.5}})
        self.assertNotIn('raw_hex', text)
        self.assertNotIn('reserved', text)
        self.assertEqual(form[1], (False, '1.6kHz', '2Q', '-1.5dB'))

    def test_copy_reads_only_selected_band_and_keeps_other_unfinished_input(self):
        app = clipboard_app()
        app.values[3]['gain'].set('-')
        app._changed()
        before = app._form()
        self.assertTrue(app.copy_band())
        self.assertEqual(read_snippet(app.root.text)['source_band'], 'LF')
        self.assertEqual(app._form(), before)
        self.assertEqual(app.review.error_count, 1)
        self.assertEqual(app.current.raw, ORIGINAL.raw)

    def test_copy_does_not_create_history_or_discard_redo(self):
        app = clipboard_app()
        app.values[1]['q'].set('2')
        app._changed()
        app.undo()
        self.assertTrue(app.copy_band())
        self.assertFalse(app.history.can_undo)
        self.assertTrue(app.history.can_redo)
        app.redo()
        self.assertEqual(app.values[1]['q'].get(), '2')

    def test_copy_invalid_selected_band_retains_previous_clipboard(self):
        app = clipboard_app()
        app.root.text = copy_snippet(ORIGINAL, app._form(), 3)
        previous = app.root.text
        app.values[1]['q'].set('-')
        app._changed()
        self.assertFalse(app.copy_band())
        self.assertEqual(app.root.text, previous)
        self.assertEqual(app.root.clears, 0)
        self.assertIn('复制未完成', app.status.get())

    def test_peak_paste_changes_four_target_fields_as_one_undo(self):
        app = clipboard_app()
        app._set_form(modified_form())
        app._changed()
        before = app._form()
        self.assertTrue(app.copy_band())
        app.selected_band = 3
        self.assertTrue(app.paste_band())
        self.assertEqual(app._form()[3], (False, '1600', '2', '-1.5'))
        self.assertEqual(app._form()[1], before[1])
        target = app.target()
        self.assertEqual(changed_fields(ORIGINAL, target),
                         tuple((index, field) for index in (1, 3) for field in ('enabled', 'frequency', 'q', 'gain')))
        app.undo()
        self.assertEqual(app._form(), before)
        app.redo()
        self.assertEqual(app.target().raw, target.raw)
        self.assertEqual(app.current.raw, ORIGINAL.raw)

    def test_two_pastes_are_separate_undo_steps(self):
        app = clipboard_app()
        app.selected_band = 3
        for q in (2, 3):
            form = preset_form(ORIGINAL.with_field(1, 'q', q))
            app.root.text = copy_snippet(ORIGINAL, form, 1)
            self.assertTrue(app.paste_band())
        app.undo()
        self.assertEqual(app.values[3]['q'].get(), '2')
        app.undo()
        self.assertEqual(app.target().raw, ORIGINAL.raw)

    def test_identical_paste_does_not_discard_redo(self):
        app = clipboard_app()
        app.values[1]['q'].set('2')
        app._changed()
        app.undo()
        self.assertTrue(app.copy_band())
        self.assertTrue(app.paste_band())
        self.assertFalse(app.history.can_undo)
        self.assertTrue(app.history.can_redo)

    def test_paste_preserves_other_invalid_text_and_repairs_only_selected_band(self):
        app = clipboard_app()
        self.assertTrue(app.copy_band())
        app.values[2]['frequency'].set('21k')
        app.values[3]['q'].set('-')
        app._changed()
        before = app._form()
        app.selected_band = 3
        self.assertTrue(app.paste_band())
        self.assertEqual(app.values[2]['frequency'].get(), '21k')
        self.assertEqual(app.review.error_count, 1)
        self.assertEqual(app.values[3]['q'].get(), '1')
        app.undo()
        self.assertEqual(app._form(), before)
        self.assertEqual(app.review.error_count, 2)

    def test_paste_rejects_peak_cutoff_mismatch_without_any_changes(self):
        form = preset_form(ORIGINAL)
        for source, target in ((1, 0), (1, 6), (0, 1), (6, 3)):
            app = clipboard_app()
            app.root.text = copy_snippet(ORIGINAL, form, source)
            app.selected_band = target
            self.assertFalse(app.paste_band())
            self.assertEqual(app._form(), form)
            self.assertFalse(app.history.can_undo)
            self.assertIn('不能互相粘贴', app.status.get())

    def test_cutoff_transfer_keeps_target_hidden_q_gain_and_reserved_bytes(self):
        form = preset_form(ORIGINAL)
        text = copy_snippet(ORIGINAL, form, 0)
        self.assertEqual(set(read_snippet(text)['values']), {'enabled', 'frequency'})
        pasted, source = paste_snippet(form, 6, text)
        app = clipboard_app()
        app._set_form(pasted)
        target = app.target()
        self.assertEqual(source, 'HP')
        self.assertEqual(pasted[6][2:], form[6][2:])
        self.assertEqual(target.field_bytes(6, 'q'), ORIGINAL.field_bytes(6, 'q'))
        self.assertEqual(target.field_bytes(6, 'gain'), ORIGINAL.field_bytes(6, 'gain'))
        self.assertEqual(target.raw[14+6*16+2:14+6*16+4], ORIGINAL.raw[14+6*16+2:14+6*16+4])

    def test_transfer_keeps_preset_name_unselected_bands_and_target_reserved_bytes(self):
        app = clipboard_app()
        app.root.text = copy_snippet(ORIGINAL, modified_form(), 1)
        app.selected_band = 3
        self.assertTrue(app.paste_band())
        target = app.target()
        self.assertEqual(target.raw[:14], ORIGINAL.raw[:14])
        for index in range(7):
            if index != 3:
                self.assertEqual(target.raw[14+index*16:14+(index+1)*16], ORIGINAL.raw[14+index*16:14+(index+1)*16])
        self.assertEqual(target.raw[14+3*16+2:14+3*16+4], ORIGINAL.raw[14+3*16+2:14+3*16+4])

    def test_invalid_json_shape_version_and_unknown_fields_are_rejected(self):
        data = read_snippet(copy_snippet(ORIGINAL, preset_form(ORIGINAL), 1))
        candidates = [None, [], dict(data, kind='axon-band-v2'), dict(data, source_band='unknown'),
                      dict(data, band_kind='cutoff'), dict(data, raw_hex=ORIGINAL.raw.hex()),
                      dict(data, values=dict(data['values'], reserved=1)),
                      dict(data, values={'enabled': True, 'frequency': 160})]
        for candidate in candidates:
            with self.assertRaises(ValueError):
                read_snippet(json.dumps(candidate))
        for text in ('', '-', '{', 'ordinary text', '['*1000, 'x'*4097):
            with self.assertRaises(ValueError):
                read_snippet(text)

    def test_invalid_value_types_nonfinite_and_out_of_range_numbers_are_rejected(self):
        data = read_snippet(copy_snippet(ORIGINAL, preset_form(ORIGINAL), 1))
        for field, value in [('enabled', 1), ('frequency', True), ('frequency', '1k'),
                              ('frequency', 21000), ('q', 0), ('q', 11), ('q', None),
                              ('gain', 13), ('gain', float('nan')), ('gain', float('inf')),
                              ('frequency', 10**1000)]:
            candidate = dict(data, values=dict(data['values'], **{field: value}))
            with self.assertRaises(ValueError):
                read_snippet(json.dumps(candidate))

    def test_bad_clipboard_does_not_mutate_form_or_history(self):
        app = clipboard_app()
        app.values[1]['frequency'].set('1.6kHz')
        app._changed()
        before = app._form()
        undo_count = len(app.history._undo)
        app.root.text = 'ordinary text'
        self.assertFalse(app.paste_band())
        self.assertEqual(app._form(), before)
        self.assertEqual(len(app.history._undo), undo_count)
        self.assertEqual(app.current.raw, ORIGINAL.raw)

    def test_clipboard_failures_leave_local_form_and_device_unchanged(self):
        app = clipboard_app()
        app.root.fail_write = True
        self.assertFalse(app.copy_band())
        app.root.fail_read = True
        self.assertFalse(app.paste_band())
        self.assertEqual(app.target().raw, ORIGINAL.raw)
        self.assertFalse(app.history.can_undo)

    def test_all_band_actions_guard_busy_closing_unconnected_and_missing_data(self):
        app = clipboard_app()
        for flag in ('busy', 'closing'):
            setattr(app, flag, True)
            for action in (app.copy_band, app.paste_band, app.zero_gain):
                self.assertFalse(action())
            setattr(app, flag, False)
        app.offline = False
        for action in (app.copy_band, app.paste_band, app.zero_gain):
            self.assertFalse(action())
        app.offline, app.current = True, None
        for action in (app.copy_band, app.paste_band, app.zero_gain):
            self.assertFalse(action())
        self.assertEqual((app.root.clears, app.root.reads), (0, 0))

    def test_zero_gain_changes_only_gain_and_undo_restores_original_value(self):
        app = clipboard_app()
        self.assertTrue(app.zero_gain())
        self.assertEqual(changed_fields(ORIGINAL, app.target()), ((1, 'gain'),))
        self.assertEqual(app.target().bands[1].gain, 0)
        app.undo()
        self.assertEqual(app.target().raw, ORIGINAL.raw)
        app.redo()
        self.assertEqual(app.target().bands[1].gain, 0)
        self.assertEqual(app.current.raw, ORIGINAL.raw)

    def test_zero_gain_keeps_other_unfinished_text_and_can_undo_error_repair(self):
        app = clipboard_app()
        app.values[1]['frequency'].set('-')
        app.values[1]['gain'].set('-')
        app._changed()
        before = app._form()
        self.assertTrue(app.zero_gain())
        self.assertEqual(app.values[1]['frequency'].get(), '-')
        self.assertEqual(app.values[1]['gain'].get(), '0')
        self.assertEqual(app.review.error_count, 1)
        app.undo()
        self.assertEqual(app._form(), before)
        self.assertEqual(app.review.error_count, 2)

    def test_zero_gain_on_cutoff_keeps_every_byte_and_creates_no_history(self):
        app = clipboard_app()
        for index in (0, 6):
            app.selected_band = index
            self.assertFalse(app.zero_gain())
            self.assertEqual(app.target().raw, ORIGINAL.raw)
            self.assertFalse(app.history.can_undo)

    def test_copy_and_paste_use_retained_b_preview_while_hearing_a(self):
        app = clipboard_app()
        app.snapshot['eq_enabled'] = True
        after = ORIGINAL.with_field(1, 'q', 2).with_field(1, 'gain', -1.5)
        app.audition = SimpleNamespace(after=after, side='A')
        app._set_form(preset_form(after))
        self.assertTrue(app.copy_band())
        self.assertEqual(read_snippet(app.root.text)['values']['gain'], -1.5)
        app.selected_band = 3
        self.assertTrue(app.paste_band())
        self.assertEqual(app.target().bands[3].gain, -1.5)
        self.assertEqual(app.target().bands[1].gain, -1.5)
        self.assertEqual(app.current.raw, ORIGINAL.raw)
        self.assertEqual(app.audition.after.raw, after.raw)

    def test_controls_disable_invalid_copy_and_cutoff_zero_but_allow_repair_paste(self):
        app = clipboard_app()
        install_controls(app)
        app.values[1]['q'].set('-')
        app._changed()
        App._controls(app)
        self.assertEqual(app.copy_band_button.options['state'], 'disabled')
        self.assertEqual(app.paste_band_button.options['state'], 'normal')
        self.assertEqual(app.zero_gain_button.options['state'], 'normal')
        app.selected_band = 0
        App._controls(app)
        self.assertEqual(app.copy_band_button.options['state'], 'normal')
        self.assertEqual(app.zero_gain_button.options['state'], 'disabled')

    def test_new_shortcuts_handle_entry_before_class_paste_and_leave_normal_copy_keys_alone(self):
        app = clipboard_app()
        app.cards = {'q': Mock()}
        app._bind_shortcuts()
        bindings = {call.args[0]: call.args[1] for call in app.cards['q'].entry.bind.call_args_list}
        self.assertNotIn('<Control-c>', bindings)
        self.assertNotIn('<Control-v>', bindings)
        self.assertEqual(bindings['<Control-Shift-C>'](SimpleNamespace()), 'break')
        app.selected_band = 3
        self.assertEqual(bindings['<Control-Shift-V>'](SimpleNamespace()), 'break')
        self.assertEqual(app.values[3]['frequency'].get(), '160')
        self.assertEqual(app.values[3]['gain'].get(), '-4')

    def test_pasted_form_persists_as_local_input_with_original_device_document(self):
        app = clipboard_app()
        app.snapshot, app.compare_saved = document(), False
        app.root.text = copy_snippet(ORIGINAL, modified_form(), 1)
        app.selected_band = 3
        self.assertTrue(app.paste_band())
        with tempfile.TemporaryDirectory() as folder:
            app.session_store = SessionStore(Path(folder) / 'last-session.json')
            app._write_session()
            saved = app.session_store.load()
        self.assertEqual(saved['form'][3], (False, '1600', '2', '-1.5'))
        self.assertEqual(saved['document']['current']['raw_hex'], ORIGINAL.raw.hex())
        self.assertEqual(saved['selected_band'], 3)


if __name__ == '__main__':
    unittest.main()
