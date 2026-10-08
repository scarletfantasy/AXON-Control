"""Axis locking and parameter-card controls without a Tk window or MIDI port."""
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from axon_protocol import Preset
from curve_view import DRAG_MODES, PlotTransform
from editor_session import SESSION_KIND, SessionStore, read_session
from editor_state import changed_fields, display_float
from tests_curve import graph_app, node_event
from tests_editor import ORIGINAL, Variable, memory_app
from tests_session import document
from ui_widgets import ParameterCard


def moved(event, dx=50, dy=-20, state=0):
    return SimpleNamespace(x=event.x+dx, y=event.y+dy, state=state)


def parameter_card(app, field):
    class TracedVariable(Variable):
        def set(self, value):
            super().set(value)
            app._changed((app.selected_band, field))

    card = ParameterCard.__new__(ParameterCard)
    card.field = field
    specs = {'frequency': (20, 20000, 10), 'q': (0.1, 10, 0.1), 'gain': (-12, 12, 0.1)}
    card.minimum, card.maximum, card.step = specs[field]
    card.variable = TracedVariable(app.values[app.selected_band][field].get())
    app.values[app.selected_band][field] = card.variable
    card.enabled = card.supported = True
    card.format_value = display_float
    card.on_edit_boundary = app.history.end_group
    card.winfo_width = lambda: 238
    card.entry = Mock()
    return card


def wheel(delta=120, state=0):
    return SimpleNamespace(delta=delta, state=state)


class PrecisionTests(unittest.TestCase):
    def test_locked_modes_emit_only_the_chosen_field(self):
        transform = PlotTransform((0, 1000, 0, 200), 6)
        origin = (300, 100, 161.125, -3.375)
        frequency = transform.drag(1, 450, 20, origin=origin, mode='frequency')
        gain = transform.drag(1, 450, 20, origin=origin, mode='gain')
        free = transform.drag(1, 450, 20, origin=origin)
        self.assertEqual(frequency, {'frequency': 454})
        self.assertEqual(gain, {'gain': 1.4})
        self.assertEqual(free, dict(frequency, **gain))

    def test_locked_mode_limits_and_shift_precision(self):
        transform = PlotTransform((0, 1000, 0, 200), 24)
        origin = (300, 100, 200, -4)
        self.assertEqual(transform.drag(1, 500, 80, origin=origin, fine=True, mode='gain'), {'gain': -3.52})
        self.assertEqual(transform.drag(1, 50000, -50000, origin=origin, mode='gain'), {'gain': 12})
        self.assertEqual(transform.drag(1, -50000, 50000, origin=origin, mode='frequency'), {'frequency': 20})

    def test_cutoffs_remain_frequency_only_in_every_mode(self):
        transform = PlotTransform((0, 1000, 0, 200), 6)
        for mode in DRAG_MODES:
            for index in (0, 6):
                values = transform.drag(index, 700, -1000, origin=(300, 100, 200, -4), mode=mode)
                self.assertEqual(set(values), {'frequency'})
                target = ORIGINAL.with_field(index, 'frequency', values['frequency'])
                self.assertEqual(target.field_bytes(index, 'q'), ORIGINAL.field_bytes(index, 'q'))
                self.assertEqual(target.field_bytes(index, 'gain'), ORIGINAL.field_bytes(index, 'gain'))

    def test_gain_drag_preserves_exact_frequency_q_and_reserved_bytes(self):
        app = graph_app()
        app.values[1]['frequency'].set('0.16 kHz')
        app._changed()
        app.set_drag_mode('gain')
        start = node_event(app)
        app._graph_press(start)
        app._graph_drag(moved(start))
        app._graph_release()
        target = app.target()
        self.assertEqual(changed_fields(ORIGINAL, target), ((1, 'gain'),))
        self.assertEqual(app.values[1]['frequency'].get(), '0.16 kHz')
        self.assertEqual(target.field_bytes(1, 'frequency'), ORIGINAL.field_bytes(1, 'frequency'))
        self.assertEqual(target.field_bytes(1, 'q'), ORIGINAL.field_bytes(1, 'q'))
        app.undo()
        self.assertEqual(app.target().raw, ORIGINAL.raw)
        app.redo()
        self.assertEqual(app.target().raw, target.raw)
        self.assertEqual(app.plot_drag_mode, 'gain')
        self.assertEqual(app.current.raw, ORIGINAL.raw)

    def test_frequency_drag_retains_unfinished_gain_input(self):
        app = graph_app()
        app.values[1]['gain'].set('-')
        app._changed()
        app.set_drag_mode('frequency')
        start = node_event(app)
        app._graph_press(start)
        app._graph_drag(moved(start))
        app._graph_release()
        self.assertEqual(app.values[1]['gain'].get(), '-')
        self.assertEqual(changed_fields(ORIGINAL, app.review.preview), ((1, 'frequency'),))
        self.assertEqual(app.review.error_count, 1)
        app.undo()
        self.assertEqual(app.values[1]['gain'].get(), '-')
        self.assertEqual(app.review.preview.raw, ORIGINAL.raw)

    def test_gain_drag_retains_invalid_frequency_input(self):
        app = graph_app()
        app.values[1]['frequency'].set('21k')
        app._changed()
        app.set_drag_mode('gain')
        start = node_event(app)
        app._graph_press(start)
        app._graph_drag(moved(start))
        app._graph_release()
        self.assertEqual(app.values[1]['frequency'].get(), '21k')
        self.assertEqual(changed_fields(ORIGINAL, app.review.preview), ((1, 'gain'),))
        self.assertEqual(app.review.error_count, 1)
        self.assertEqual(app.current.raw, ORIGINAL.raw)

    def test_mode_changes_preserve_draft_and_redo(self):
        app = graph_app()
        app.values[1]['gain'].set('-2')
        app._changed()
        app.undo()
        form = app._form()
        for mode in DRAG_MODES:
            self.assertTrue(app.set_drag_mode(mode))
            self.assertEqual(app._form(), form)
            self.assertTrue(app.history.can_redo)
        app.redo()
        self.assertAlmostEqual(app.target().bands[1].gain, -2)
        self.assertEqual(app.current.raw, ORIGINAL.raw)

    def test_changing_mode_ends_in_progress_drag_without_changing_its_last_value(self):
        app = graph_app()
        start = node_event(app)
        app._graph_press(start)
        app._graph_drag(moved(start))
        previous = app.target().raw
        app.set_drag_mode('frequency')
        self.assertIsNone(app.drag_index)
        app._graph_drag(moved(start, dx=150, dy=-60))
        self.assertEqual(app.target().raw, previous)
        app.undo()
        self.assertEqual(app.target().raw, ORIGINAL.raw)

    def test_invalid_modes_and_operation_guards(self):
        app = graph_app()
        for value in ('q', '', None, True, 1, []):
            with self.assertRaises(ValueError):
                app.set_drag_mode(value)
        for flag in ('busy', 'closing'):
            setattr(app, flag, True)
            self.assertFalse(app.set_drag_mode('gain'))
            self.assertEqual(app.plot_drag_mode, 'free')
            setattr(app, flag, False)
        self.assertEqual(app.current.raw, ORIGINAL.raw)

    def test_drag_mode_session_roundtrip_and_old_default(self):
        app = graph_app()
        app.snapshot = document()
        app.values[1]['gain'].set('-')
        app._changed()
        app.set_drag_mode('frequency')
        with tempfile.TemporaryDirectory() as folder:
            app.session_store = SessionStore(Path(folder) / 'last-session.json')
            app._write_session()
            saved = app.session_store.load()
            app.set_drag_mode('gain')
            self.assertTrue(app.restore_session())
        self.assertEqual(saved['plot_drag_mode'], 'frequency')
        self.assertEqual(app.plot_drag_mode, 'frequency')
        self.assertEqual(app.values[1]['gain'].get(), '-')
        self.assertTrue(app.offline)
        self.assertFalse(app.connected)
        legacy = dict(saved)
        legacy.pop('plot_drag_mode')
        self.assertEqual(read_session(legacy)['plot_drag_mode'], 'free')

    def test_invalid_stored_mode_is_rejected(self):
        valid = {'kind': SESSION_KIND, 'document': document(), 'form': memory_app()._form(),
                 'selected_band': 1, 'compare_saved': False}
        for value in ('q', '', None, True, 1, []):
            with self.assertRaises(ValueError):
                read_session(dict(valid, plot_drag_mode=value))

    def test_card_wheel_changes_only_one_field_and_coalesces_undo(self):
        app = memory_app()
        card = parameter_card(app, 'gain')
        card._wheel(wheel())
        card._wheel(wheel())
        self.assertEqual(changed_fields(ORIGINAL, app.target()), ((1, 'gain'),))
        self.assertAlmostEqual(app.target().bands[1].gain, -3.8, places=6)
        app.undo()
        self.assertEqual(app.target().raw, ORIGINAL.raw)
        self.assertFalse(app.history.can_undo)
        app.redo()
        self.assertAlmostEqual(app.target().bands[1].gain, -3.8, places=6)
        self.assertEqual(app.current.raw, ORIGINAL.raw)

    def test_card_wheel_modifiers_match_frequency_q_and_gain_steps(self):
        for field, original, changes in [('frequency', 160, (10, 1, 100)), ('q', 1, (0.1, 0.01, 1)),
                                         ('gain', -4, (0.1, 0.01, 1))]:
            for state, change in zip((0, 1, 4), changes):
                app = memory_app()
                card = parameter_card(app, field)
                card._wheel(wheel(state=state))
                self.assertAlmostEqual(getattr(app.target().bands[1], field), original+change, places=5)
                self.assertEqual(changed_fields(ORIGINAL, app.target()), ((1, field),))
                self.assertEqual(app.current.raw, ORIGINAL.raw)

    def test_shift_precedes_ctrl_for_fine_wheel(self):
        app = memory_app()
        card = parameter_card(app, 'frequency')
        card._wheel(wheel(delta=-120, state=5))
        self.assertEqual(app.values[1]['frequency'].get(), '159')

    def test_card_wheel_retains_invalid_text_and_ignores_disabled_or_zero_events(self):
        app = memory_app()
        card = parameter_card(app, 'gain')
        for text in ('-', '', 'bad', 'nan', '1k'):
            card.variable.set(text)
            card._wheel(wheel())
            self.assertEqual(card.variable.get(), text)
        card.variable.set('-4')
        for attribute in ('enabled', 'supported'):
            setattr(card, attribute, False)
            card._wheel(wheel())
            self.assertEqual(card.variable.get(), '-4')
            setattr(card, attribute, True)
        card._wheel(wheel(delta=0))
        self.assertEqual(card.variable.get(), '-4')
        self.assertEqual(app.current.raw, ORIGINAL.raw)

    def test_card_wheel_limits_protect_other_and_hidden_fields(self):
        app = memory_app()
        card = parameter_card(app, 'q')
        for value, delta in [('10', 120), ('0.1', -120)]:
            card.variable.set(value)
            card._wheel(wheel(delta=delta, state=4))
            self.assertEqual(card.variable.get(), value)
        self.assertEqual(changed_fields(ORIGINAL, app.target()), ((1, 'q'),))
        self.assertEqual(app.target().raw[:14], ORIGINAL.raw[:14])
        for index in (0, 6):
            self.assertEqual(app.target().raw[14+index*16:14+(index+1)*16], ORIGINAL.raw[14+index*16:14+(index+1)*16])

    def test_plus_minus_buttons_use_same_modifier_steps_as_keys(self):
        app = memory_app()
        card = parameter_card(app, 'gain')
        card._press(SimpleNamespace(x=215, y=22, state=1))
        self.assertEqual(card.variable.get(), '-3.99')
        card._press(SimpleNamespace(x=187, y=22, state=4))
        self.assertEqual(card.variable.get(), '-4.99')
        card._keyboard_nudge(SimpleNamespace(state=4), 1)
        self.assertEqual(card.variable.get(), '-3.99')
        app.undo()
        self.assertEqual(card.variable.get(), '-4.99')
        self.assertEqual(app.current.raw, ORIGINAL.raw)

    def test_card_wheel_keeps_another_unfinished_field_and_its_error(self):
        app = memory_app()
        app.values[1]['gain'].set('-')
        app._changed()
        card = parameter_card(app, 'frequency')
        card._wheel(wheel())
        self.assertEqual(app.values[1]['gain'].get(), '-')
        self.assertEqual(app.review.error_count, 1)
        self.assertEqual(changed_fields(ORIGINAL, app.review.preview), ((1, 'frequency'),))
        self.assertEqual(app.current.raw, ORIGINAL.raw)


if __name__ == '__main__':
    unittest.main()
