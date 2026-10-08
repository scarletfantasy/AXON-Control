"""Reference sources, plotted deltas and recovery without real Tk or MIDI."""
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from axon_control import App
from audition_state import AuditionPair
from curve_reference import REFERENCE_CHOICES, available_references, resolve_reference
from editor_session import SESSION_KIND, SessionStore, read_session
from editor_state import preset_form
from tests_audition import install_controls
from tests_clipboard import ClipboardRoot
from tests_curve import graph_app
from tests_editor import ORIGINAL
from tests_session import document


WORKING = ORIGINAL.with_field(1, 'frequency', 161).with_field(1, 'gain', -3.3)
AFTER = WORKING.with_field(1, 'gain', -2)


def reference_app():
    app = graph_app()
    app.canvas.bbox.return_value = (50, 18, 250, 34)
    app.snapshot = document(WORKING)
    app.current = WORKING
    app._set_form(preset_form(WORKING))
    app.plot_reference = 'none'
    app.compare_button = Mock()
    app.history.reset(app._form())
    app._changed(record=False)
    return app


def probe_caption(app):
    calls = [call for call in app.canvas.create_text.call_args_list
             if 'reference-probe' in call.kwargs.get('tags', ())]
    return calls[-1].kwargs['text'] if calls else None


def reference_line(app):
    return [call for call in app.canvas.create_line.call_args_list if call.kwargs.get('tags') == 'reference']


class ReferenceTests(unittest.TestCase):
    def test_current_and_saved_sources_are_their_exact_distinct_bytes(self):
        current = resolve_reference('current', WORKING, ORIGINAL)
        saved = resolve_reference('saved', WORKING, ORIGINAL)
        self.assertEqual(current.preset.raw, WORKING.raw)
        self.assertEqual(saved.preset.raw, ORIGINAL.raw)
        self.assertNotEqual(current.preset.raw, saved.preset.raw)
        self.assertNotEqual(current.color, saved.color)
        self.assertEqual((current.label, saved.label), ('读回', '已保存'))

    def test_offline_reference_has_original_parameter_label(self):
        reference = resolve_reference('current', WORKING, ORIGINAL, offline=True)
        self.assertEqual((reference.label, reference.description), ('原参数', '离线原参数'))
        self.assertEqual(reference.preset.raw, WORKING.raw)

    def test_verified_pair_exposes_exact_a_and_applied_b_on_either_side(self):
        for side in ('A', 'B'):
            pair = AuditionPair(0, WORKING, AFTER, side)
            context = {'pair': pair, 'slot': 0}
            self.assertEqual(resolve_reference('before', pair.heard, ORIGINAL, **context).preset.raw, WORKING.raw)
            self.assertEqual(resolve_reference('after', pair.heard, ORIGINAL, **context).preset.raw, AFTER.raw)
            self.assertEqual(available_references(pair.heard, ORIGINAL, **context), REFERENCE_CHOICES)

    def test_missing_stale_or_offline_pair_falls_back_to_readback(self):
        pair = AuditionPair(0, WORKING, AFTER)
        contexts = [dict(pair=None, slot=0), dict(pair=pair, slot=1),
                    dict(pair=pair, slot=0), dict(pair=pair, slot=0, offline=True)]
        for context in contexts:
            for choice in ('before', 'after'):
                result = resolve_reference(choice, WORKING, ORIGINAL, **context)
                self.assertEqual((result.key, result.preset.raw), ('current', WORKING.raw))
                self.assertNotIn(choice, available_references(WORKING, ORIGINAL, **context))

    def test_hidden_missing_current_and_missing_saved_sources(self):
        hidden = resolve_reference('none', WORKING, ORIGINAL)
        self.assertIsNone(hidden.preset)
        self.assertEqual(available_references(None, ORIGINAL), ('none',))
        self.assertEqual(resolve_reference('saved', None, ORIGINAL).key, 'none')
        self.assertEqual(resolve_reference('saved', WORKING).key, 'current')
        self.assertEqual(available_references(WORKING), ('current', 'none'))

    def test_invalid_reference_values_are_rejected(self):
        for choice in (None, True, 1, 'A', 'live', [], {}):
            with self.subTest(choice=choice), self.assertRaises(ValueError):
                resolve_reference(choice, WORKING, ORIGINAL)

    def test_view_change_keeps_unfinished_inputs_undo_redo_and_all_device_data(self):
        app = reference_app()
        app.values[1]['frequency'].set('-')
        app._changed()
        app.values[3]['q'].set('2Q')
        app._changed()
        app.undo()
        before, snapshot = app._form(), dict(app.snapshot)
        self.assertTrue(app.history.can_redo)
        for choice in ('current', 'saved', 'none'):
            self.assertTrue(app.set_reference(choice))
            self.assertEqual(app._form(), before)
            self.assertEqual(app.current.raw, WORKING.raw)
            self.assertEqual(app.snapshot, snapshot)
            self.assertEqual(app.review.error_count, 1)
            self.assertTrue(app.history.can_redo)
        app.redo()
        self.assertEqual(app.values[3]['q'].get(), '2Q')
        app.undo()
        app.undo()
        self.assertEqual(app.target().raw, WORKING.raw)

    def test_cycle_uses_only_available_sources_and_never_creates_edits(self):
        app = reference_app()
        form = app._form()
        observed = []
        for _ in range(4):
            self.assertTrue(app.cycle_reference())
            observed.append(app.plot_reference)
        self.assertEqual(observed, ['current', 'saved', 'none', 'current'])
        self.assertEqual(app._form(), form)
        self.assertFalse(app.history.can_undo)

    def test_cycle_includes_a_b_only_during_a_matching_connected_pair(self):
        app = reference_app()
        app.offline, app.connected = False, True
        app.current, app.audition = AFTER, AuditionPair(0, WORKING, AFTER)
        app._set_form(preset_form(AFTER))
        observed = []
        for _ in range(5):
            self.assertTrue(app.cycle_reference())
            observed.append(app.plot_reference)
        self.assertEqual(observed, ['current', 'saved', 'before', 'after', 'none'])
        self.assertEqual(app.current.raw, AFTER.raw)
        self.assertEqual(app.audition.side, 'B')

    def test_unavailable_a_b_is_refused_without_applying_or_editing(self):
        app = reference_app()
        for choice in ('before', 'after'):
            self.assertFalse(app.set_reference(choice))
            self.assertEqual(app.plot_reference, 'none')
            self.assertIn('先应用一次修改', app.status.get())
        self.assertEqual(app.current.raw, WORKING.raw)
        self.assertFalse(app.history.can_undo)

    def test_busy_closing_unloaded_and_unconnected_states_guard_view_actions(self):
        for attribute, value in (('busy', True), ('closing', True), ('current', None), ('offline', False)):
            app = reference_app()
            setattr(app, attribute, value)
            self.assertFalse(app.set_reference('saved'))
            self.assertFalse(app.cycle_reference())
            self.assertIsNone(app.reference_menu())
            self.assertEqual(app.plot_reference, 'none')

    def test_plotted_reference_coordinates_use_selected_source_and_axis(self):
        app = reference_app()
        app.values[1]['gain'].set('-1.5')
        app._changed()
        app.set_plot_range(6)
        for choice, expected in (('current', WORKING), ('saved', ORIGINAL)):
            app.canvas.reset_mock()
            app.set_reference(choice)
            line, = reference_line(app)
            samples, transform = app.curve_cache.get(expected), app.plot_transform
            expected_points = tuple(coordinate for frequency, gain in zip(samples.frequencies, samples.totals)
                                    for coordinate in (transform.frequency_x(frequency), transform.gain_y(gain)))
            self.assertEqual(line.args, expected_points)
            self.assertEqual(line.kwargs['fill'], app.plot_reference_view.color)
            self.assertEqual(app.plot_reference_view.preset.raw, expected.raw)
            self.assertEqual(app.plot_preset.raw, WORKING.with_field(1, 'gain', -1.5).raw)

    def test_reference_has_no_editable_nodes_and_hiding_removes_only_the_reference(self):
        app = reference_app()
        nodes = list(app.plot_nodes)
        app.set_reference('saved')
        self.assertEqual(app.plot_nodes, nodes)
        app.canvas.reset_mock()
        app.set_reference('none')
        self.assertEqual(reference_line(app), [])
        self.assertEqual(app.plot_nodes, nodes)
        self.assertEqual(app.plot_preset.raw, WORKING.raw)

    def test_probe_reports_preview_minus_reference_at_the_same_frequency(self):
        app = reference_app()
        app.values[3]['gain'].set('1.5')
        app._changed()
        app.set_reference('current')
        app.selected_band = 3
        x = app.plot_transform.frequency_x(3000)
        app._show_probe(x, 80)
        self.assertIn('3.00 kHz', app.graph_hint.get())
        self.assertIn('MF +1.50 dB', app.graph_hint.get())
        self.assertIn('原参数', probe_caption(app))
        self.assertIn('差 +1.50 dB', probe_caption(app))
        app.canvas.reset_mock()
        app._graph_leave()
        self.assertIsNone(app.plot_pointer)
        self.assertEqual(app.canvas.delete.call_args.args, ('probe',))

    def test_bypassed_reference_and_probe_report_zero_response_and_delta(self):
        app = reference_app()
        app.values[3]['gain'].set('5')
        app._changed()
        app.snapshot['eq_enabled'] = False
        app.set_reference('saved')
        app._show_probe(app.plot_transform.frequency_x(3000), 80)
        self.assertEqual(probe_caption(app), '已保存 +0.00 dB · 差 +0.00 dB')
        samples = app.curve_cache.get(ORIGINAL, True)
        self.assertEqual(set(samples.totals), {0.0})
        self.assertEqual(app.values[3]['gain'].get(), '5')

    def test_invalid_input_keeps_other_valid_preview_and_reference_curve(self):
        app = reference_app()
        app.values[1]['frequency'].set('-')
        app.values[3]['gain'].set('1.5')
        app._changed()
        app.set_reference('current')
        app.draw()
        self.assertEqual(app.plot_preset.raw, WORKING.with_field(3, 'gain', 1.5).raw)
        self.assertEqual(app.plot_reference_view.preset.raw, WORKING.raw)
        self.assertEqual(app.values[1]['frequency'].get(), '-')
        self.assertEqual(app.review.error_count, 1)

    def test_hearing_a_retains_b_draft_and_after_reference_is_applied_b(self):
        app = reference_app()
        app.offline, app.connected = False, True
        app.current, app.audition = WORKING, AuditionPair(0, WORKING, AFTER, 'A')
        app._set_form(preset_form(AFTER))
        app.values[1]['gain'].set('-1.5')
        app._changed()
        for choice, expected in (('current', WORKING), ('before', WORKING), ('after', AFTER)):
            self.assertTrue(app.set_reference(choice))
            self.assertEqual(app.plot_reference_view.preset.raw, expected.raw)
            self.assertEqual(app.plot_preset.raw, WORKING.with_field(1, 'gain', -1.5).raw)
            self.assertEqual(app.values[1]['gain'].get(), '-1.5')
            self.assertEqual(app.audition.side, 'A')
        self.assertEqual(app.current.raw, WORKING.raw)

    def test_controls_label_the_source_and_normalize_a_b_after_pair_ends(self):
        app = reference_app()
        install_controls(app)
        app._controls = App._controls.__get__(app)
        app.offline, app.connected = False, True
        app.current, app.audition = AFTER, AuditionPair(0, WORKING, AFTER)
        app._set_form(preset_form(AFTER))
        app.set_reference('before')
        self.assertEqual(app.compare_button.options['text'], '对照：A ▾')
        self.assertIn('虚线：A', app.curve_note.get())
        app.audition = None
        app._controls()
        self.assertEqual(app.plot_reference, 'current')
        self.assertEqual(app.compare_button.options['text'], '对照：读回 ▾')
        app.offline, app.connected = True, False
        app._controls()
        self.assertEqual(app.compare_button.options['text'], '对照：原参数 ▾')

    def test_popup_marks_source_and_disables_unavailable_a_b(self):
        app = reference_app()
        app.compare_button.winfo_rootx.return_value = 40
        app.compare_button.winfo_rooty.return_value = 40
        app.compare_button.winfo_height.return_value = 24
        app.plot_reference = 'saved'
        with patch('axon_control.Popup') as popup:
            app.reference_menu()
        items = popup.call_args.args[1]
        self.assertEqual(len(items), 5)
        self.assertEqual([row[2] for row in items], [True, True, False, False, True])
        self.assertTrue(items[1][0].startswith('✓ '))
        self.assertIn('F7', items[-1][0])
        items[0][1]()
        self.assertEqual(app.plot_reference, 'current')

    def test_f7_binding_from_parameter_entry_cycles_view_without_editing(self):
        app = reference_app()
        app.root = ClipboardRoot()
        before = app._form()
        app._bind_shortcuts()
        self.assertEqual(app.root.bindings['<F7>'](SimpleNamespace(widget=app.cards['q'].entry)), 'break')
        self.assertEqual(app.plot_reference, 'current')
        self.assertEqual(app._form(), before)
        self.assertFalse(app.history.can_undo)

    def test_session_reference_roundtrip_keeps_units_and_legacy_visibility(self):
        app = reference_app()
        app.values[1]['frequency'].set('-')
        app._changed()
        with tempfile.TemporaryDirectory() as folder:
            app.session_store = SessionStore(Path(folder) / 'last-session.json')
            for choice in ('current', 'saved', 'none'):
                app.set_reference(choice)
                app._write_session()
                saved = app.session_store.load()
                self.assertEqual(saved['plot_reference'], choice)
                self.assertEqual(saved['compare_saved'], choice == 'saved')
                app.set_reference('none')
                self.assertTrue(app.restore_session())
                self.assertEqual(app.plot_reference, choice)
                self.assertEqual(app.values[1]['frequency'].get(), '-')
                self.assertFalse(app.connected)
        valid = {'kind': SESSION_KIND, 'document': document(), 'form': preset_form(ORIGINAL),
                 'selected_band': 1, 'compare_saved': True}
        self.assertEqual(read_session(valid)['plot_reference'], 'saved')
        self.assertEqual(read_session(dict(valid, compare_saved=False))['plot_reference'], 'none')

    def test_bad_session_reference_is_rejected_without_replacing_previous_file(self):
        with tempfile.TemporaryDirectory() as folder:
            store = SessionStore(Path(folder) / 'last-session.json')
            store.save(document(), preset_form(ORIGINAL), 1, False, plot_reference='current')
            original = store.path.read_bytes()
            for value in (True, 1, 'A', 'live', []):
                with self.assertRaises(ValueError):
                    store.save(document(), preset_form(ORIGINAL), 1, False, plot_reference=value)
                self.assertEqual(store.path.read_bytes(), original)
            invalid = dict(store.load(), plot_reference=None)
            with self.assertRaises(ValueError):
                read_session(invalid)

    def test_temporary_a_b_reference_restarts_using_original_parameter_context(self):
        app = reference_app()
        app.offline, app.connected = False, True
        app.current, app.audition = WORKING, AuditionPair(0, WORKING, AFTER, 'A')
        app._set_form(preset_form(AFTER))
        app.values[1]['frequency'].set('1.6kHz')
        app._changed()
        app.set_reference('after')
        with tempfile.TemporaryDirectory() as folder:
            app.session_store = SessionStore(Path(folder) / 'last-session.json')
            app._write_session()
            saved = app.session_store.load()
            self.assertEqual(saved['plot_reference'], 'current')
            self.assertEqual(saved['document']['current']['raw_hex'], WORKING.raw.hex())
            self.assertEqual(saved['form'][1][1], '1.6kHz')
            app.connected = False
            self.assertTrue(app.restore_session())
        self.assertIsNone(app.audition)
        self.assertEqual(app.plot_reference, 'current')
        self.assertEqual(app._reference().label, '原参数')
        self.assertEqual(app.target().raw, AFTER.with_field(1, 'frequency', 1600).raw)


if __name__ == '__main__':
    unittest.main()
