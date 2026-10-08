"""Curve view, precise local editing and recovery; no Tk window or MIDI port."""
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from axon_control import App
from curve_view import PlotTransform, ResponseCache, drag_values
from editor_session import SESSION_KIND, SessionStore, read_session
from editor_state import changed_fields
from tests_editor import ORIGINAL, Variable, memory_app
from tests_session import document


def graph_app():
    app = memory_app()
    app.offline = True
    app.snapshot['eq_enabled'] = True
    app.compare_saved = False
    app.curve_cache = ResponseCache()
    app.plot_pointer = app.plot_transform = app.plot_preset = None
    app.plot_bypassed = False
    app.drag_index = app.drag_origin = None
    app.drag_last, app.drag_fine = None, False
    app.canvas = Mock()
    app.canvas.winfo_width.return_value = 1000
    app.canvas.winfo_height.return_value = 240
    app.graph_hint = Variable()
    app.device_label, app.eq_label, app.preset_label = Variable(), Variable(), Variable()
    app.combo = Mock()
    for name in ('band_title', 'band_description', 'band_hint'):
        setattr(app, name, Variable())
    app.band_name_label, app.selected_hint, app.enable_switch = Mock(), Mock(), Mock()
    app.cards = {field: Mock() for field in ('frequency', 'q', 'gain')}
    app.draw = App.draw.__get__(app)
    app._changed(record=False)
    return app


def node_event(app, index=1, **options):
    node = next(node for node in app.plot_nodes if node[0] == index)
    return SimpleNamespace(x=node[1], y=node[2], delta=120, state=0, **options)


class CurveTests(unittest.TestCase):
    def test_axis_scales_preserve_frequency_coordinates_and_gain_units(self):
        bounds = (40, 1040, 20, 260)
        for limit in (3, 6, 12, 24):
            transform = PlotTransform(bounds, limit)
            for frequency in (20, 160, 1000, 12000, 20000):
                self.assertAlmostEqual(transform.frequency_at(transform.frequency_x(frequency)), frequency)
            for gain in (-limit, 0, limit):
                self.assertAlmostEqual(transform.gain_at(transform.gain_y(gain)), gain)
            self.assertEqual(transform.ticks(compact=True), (-limit, 0, limit))
        self.assertEqual(PlotTransform(bounds, 3).ticks(), (-3, -1.5, 0, 1.5, 3))

    def test_invalid_geometry_and_scale_are_rejected(self):
        for bounds, limit in [((0, 0, 0, 10), 12), ((0, 10, 10, 0), 12),
                               ((0, float('nan'), 0, 10), 12), ((0, 10, 0, 10), True),
                               ((0, 10, 0, 10), 0), ((0, 10, 0, 10), 12.0)]:
            with self.assertRaises(ValueError):
                PlotTransform(bounds, limit)

    def test_wide_display_still_clamps_edits_to_physical_editor_limits(self):
        bounds = (40, 1040, 20, 260)
        self.assertEqual(drag_values(1, -1000, -1000, bounds, gain_range=24), {'frequency': 20, 'gain': 12})
        self.assertEqual(drag_values(1, 3000, 3000, bounds, gain_range=24), {'frequency': 20000, 'gain': -12})

    def test_relative_drag_preserves_original_values_at_clipped_nodes_and_click_offsets(self):
        transform = PlotTransform((40, 1040, 20, 260), 3)
        origin = (450, 260, 161.125, -4.375)
        self.assertEqual(transform.drag(1, 450, 260, origin=origin), {'frequency': 161.125, 'gain': -4.375})
        moved = transform.drag(1, 450, 250, origin=origin, fine=True)
        self.assertEqual(moved['frequency'], 161.125)
        self.assertEqual(moved['gain'], -4.35)

    def test_shift_drag_uses_smaller_motion_and_finer_resolution(self):
        transform = PlotTransform((40, 1040, 20, 260), 12)
        origin = (340, 140, 200, -4)
        normal = transform.drag(1, 440, 100, origin=origin)
        fine = transform.drag(1, 440, 100, origin=origin, fine=True)
        self.assertEqual(normal, {'frequency': 399, 'gain': 0})
        self.assertEqual(fine, {'frequency': 214.3, 'gain': -3.6})

    def test_cutoff_relative_drag_only_edits_frequency(self):
        for index in (0, 6):
            change = drag_values(index, 2000, -2000, (40, 1040, 20, 260), gain_range=3,
                                  origin=(300, 140, 200, -4), fine=True)
            self.assertEqual(set(change), {'frequency'})
            target = ORIGINAL.with_field(index, 'frequency', change['frequency'])
            self.assertEqual(target.field_bytes(index, 'q'), ORIGINAL.field_bytes(index, 'q'))
            self.assertEqual(target.field_bytes(index, 'gain'), ORIGINAL.field_bytes(index, 'gain'))

    def test_view_changes_preserve_draft_history_and_working_parameters(self):
        app = graph_app()
        app.values[1]['gain'].set('-3')
        app._changed()
        form, target = app._form(), app.target()
        app.set_plot_range(3)
        app.set_plot_range(24)
        self.assertEqual(app._form(), form)
        self.assertEqual(app.target().raw, target.raw)
        self.assertEqual(app.current.raw, ORIGINAL.raw)
        app.undo()
        self.assertEqual(app.target().raw, ORIGINAL.raw)
        self.assertEqual(app.plot_gain_range, 24)

    def test_redraw_and_zoom_retain_valid_edits_while_another_field_is_invalid(self):
        app = graph_app()
        app.values[1]['frequency'].set('21k')
        app.values[2]['frequency'].set('2.4k')
        app._changed()
        expected = ORIGINAL.with_field(2, 'frequency', 2400)
        app.draw()
        self.assertEqual(app.plot_preset.raw, expected.raw)
        app.canvas.winfo_height.return_value = 110
        app.set_plot_range(6)
        self.assertEqual(app.plot_preset.raw, expected.raw)
        self.assertEqual(app._form()[1][1], '21k')
        self.assertEqual(app.review.error_count, 1)
        self.assertEqual(app.current.raw, ORIGINAL.raw)

    def test_view_redraws_reuse_samples_until_parameters_change(self):
        app = graph_app()
        cached = app.curve_cache.get(ORIGINAL)
        with patch('curve_view.band_response', side_effect=AssertionError('view unnecessarily resampled')):
            app.choose_band(3)
            app.canvas.winfo_width.return_value = 1100
            app.set_plot_range(3)
            app.draw()
        self.assertIs(app.curve_cache.get(ORIGINAL), cached)
        changed = ORIGINAL.with_field(1, 'gain', -2)
        self.assertNotEqual(app.curve_cache.get(changed).totals, cached.totals)

    def test_cache_keeps_bypass_separate_and_evicts_old_entries(self):
        cache = ResponseCache(capacity=2)
        original = cache.get(ORIGINAL)
        bypassed = cache.get(ORIGINAL, True)
        self.assertEqual(set(bypassed.totals), {0.0})
        self.assertTrue(all(set(band) == {0.0} for band in bypassed.bands))
        self.assertIs(cache.get(ORIGINAL), original)
        cache.get(ORIGINAL.with_field(1, 'gain', -2))
        self.assertIsNot(cache.get(ORIGINAL, True), bypassed)

    def test_node_wheel_edits_only_q_and_coalesces_into_one_undo(self):
        app = graph_app()
        event = node_event(app)
        app._graph_wheel(event)
        app._graph_wheel(event)
        self.assertEqual(changed_fields(ORIGINAL, app.target()), ((1, 'q'),))
        self.assertAlmostEqual(app.target().bands[1].q, 1.2, places=6)
        app.undo()
        self.assertEqual(app.target().raw, ORIGINAL.raw)
        self.assertFalse(app.history.can_undo)
        app.redo()
        self.assertAlmostEqual(app.target().bands[1].q, 1.2, places=6)
        self.assertEqual(app.current.raw, ORIGINAL.raw)

    def test_shift_wheel_and_q_limits_preserve_other_fields(self):
        app = graph_app()
        event = node_event(app)
        event.state = 1
        app._graph_wheel(event)
        self.assertAlmostEqual(app.target().bands[1].q, 1.01, places=6)
        app.values[1]['q'].set('10')
        app._changed()
        app._graph_wheel(event)
        self.assertEqual(app.values[1]['q'].get(), '10')
        app.values[1]['q'].set('0.1')
        app._changed()
        event.delta = -120
        app._graph_wheel(event)
        self.assertEqual(app.values[1]['q'].get(), '0.1')
        self.assertEqual(changed_fields(ORIGINAL, app.target()), ((1, 'q'),))

    def test_wheel_over_another_peak_selects_and_edits_that_peak(self):
        app = graph_app()
        app._graph_wheel(node_event(app, 3))
        self.assertEqual(app.selected_band, 3)
        self.assertEqual(changed_fields(ORIGINAL, app.target()), ((3, 'q'),))
        self.assertEqual(app.current.raw, ORIGINAL.raw)

    def test_wheel_guards_cutoff_background_busy_closing_and_unconnected_states(self):
        app = graph_app()
        original = app._form()
        for index in (0, 6):
            app._graph_wheel(node_event(app, index))
        app._graph_wheel(SimpleNamespace(x=10, y=10, state=0, delta=120))
        for flag in ('busy', 'closing'):
            setattr(app, flag, True)
            app._graph_wheel(node_event(app))
            setattr(app, flag, False)
        app.offline = False
        app._graph_wheel(node_event(app))
        self.assertEqual(app._form(), original)
        self.assertEqual(app.current.raw, ORIGINAL.raw)

    def test_wheel_keeps_invalid_q_and_unfinished_other_input(self):
        app = graph_app()
        app.values[1]['q'].set('bad')
        app._changed()
        event = node_event(app)
        app._graph_wheel(event)
        self.assertEqual(app.values[1]['q'].get(), 'bad')
        self.assertIn('请先修正', app.status.get())
        app.values[1]['q'].set('1')
        app.values[1]['frequency'].set('-')
        app._changed()
        app._graph_wheel(node_event(app))
        self.assertEqual(app._form()[1][1], '-')
        self.assertEqual(app._form()[1][2], '1.1')
        self.assertEqual(app.current.raw, ORIGINAL.raw)

    def test_real_graph_handler_uses_shift_and_preserves_protected_fields(self):
        app = graph_app()
        start = node_event(app)
        app._graph_press(start)
        event = SimpleNamespace(x=start.x+30, y=start.y-5, state=1)
        app._graph_drag(event)
        self.assertEqual(float(app.values[1]['frequency'].get()), 163.6)
        self.assertEqual(float(app.values[1]['gain'].get()), -3.94)
        self.assertEqual(app.target().field_bytes(1, 'q'), ORIGINAL.field_bytes(1, 'q'))
        self.assertEqual(app.current.raw, ORIGINAL.raw)
        app._graph_release()
        self.assertIsNone(app.drag_index)
        app.undo()
        self.assertEqual(app.target().raw, ORIGINAL.raw)

    def test_probe_reports_response_without_editing_and_clears_on_leave(self):
        app = graph_app()
        original = app._form()
        x = app.plot_transform.frequency_x(160)
        app._graph_motion(SimpleNamespace(x=x, y=120))
        self.assertIn('160.0 Hz', app.graph_hint.get())
        self.assertIn('LF -4.00 dB', app.graph_hint.get())
        self.assertEqual(app._form(), original)
        app._graph_leave()
        self.assertIsNone(app.plot_pointer)
        self.assertIn('Shift', app.graph_hint.get())
        self.assertEqual(app.current.raw, ORIGINAL.raw)

    def test_toggling_shift_mid_drag_does_not_jump_and_pauses_stay_in_one_undo(self):
        app = graph_app()
        start = node_event(app)
        app._graph_press(start)
        with patch('editor_state.time.monotonic', side_effect=[1, 5, 8]):
            app._graph_drag(SimpleNamespace(x=start.x+30, y=start.y-5, state=0))
            self.assertEqual((app._form()[1][1], app._form()[1][3]), ('200', '-3.4'))
            app._graph_drag(SimpleNamespace(x=start.x+31, y=start.y-6, state=1))
            self.assertEqual((app._form()[1][1], app._form()[1][3]), ('200.1', '-3.39'))
            app._graph_drag(SimpleNamespace(x=start.x+32, y=start.y-7, state=0))
            self.assertEqual((app._form()[1][1], app._form()[1][3]), ('202', '-3.3'))
        app._graph_release()
        app.undo()
        self.assertEqual(app.target().raw, ORIGINAL.raw)
        self.assertFalse(app.history.can_undo)

    def test_bypassed_probe_reports_zero_without_changing_parameters(self):
        app = graph_app()
        app.snapshot['eq_enabled'] = False
        app.draw()
        app._show_probe(500, 120)
        self.assertIn('总 +0.00 dB', app.graph_hint.get())
        self.assertIn('LF +0.00 dB', app.graph_hint.get())
        self.assertEqual(app.current.raw, ORIGINAL.raw)

    def test_session_range_roundtrip_and_legacy_default_preserve_input(self):
        app = graph_app()
        app.snapshot = document()
        app.values[1]['frequency'].set('-')
        app._changed()
        app.set_plot_range(3)
        with tempfile.TemporaryDirectory() as folder:
            app.session_store = SessionStore(Path(folder) / 'last-session.json')
            app._write_session()
            saved = app.session_store.load()
            app.set_plot_range(24)
            self.assertTrue(app.restore_session())
        self.assertEqual(saved['plot_gain_range'], 3)
        self.assertEqual(app.plot_gain_range, 3)
        self.assertEqual(app.values[1]['frequency'].get(), '-')
        self.assertTrue(app.offline)
        self.assertFalse(app.connected)
        legacy = dict(saved)
        legacy.pop('plot_gain_range')
        self.assertEqual(read_session(legacy)['plot_gain_range'], 12)

    def test_bad_session_range_is_rejected(self):
        app = graph_app()
        valid = {'kind': SESSION_KIND, 'document': document(), 'form': app._form(),
                 'selected_band': 1, 'compare_saved': False}
        for value in (True, 12.0, '12', None, 0, 36):
            with self.assertRaises(ValueError):
                read_session(dict(valid, plot_gain_range=value))


if __name__ == '__main__':
    unittest.main()
