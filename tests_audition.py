"""A/B listening through the real guarded protocol with an in-memory speaker."""
from pathlib import Path
import queue
import tempfile
import unittest
from unittest.mock import Mock

from axon_control import App
from editor_session import SessionStore
from editor_state import FieldError, preset_form
from tests_editor import ORIGINAL, Variable, memory_app
from tests_protocol import MemoryDevice
from tests_session import Root


WORKING = ORIGINAL.with_field(1, 'frequency', 161).with_field(1, 'gain', -3.3)


def audition_app():
    app = memory_app()
    app.busy, app.closing, app.connected = False, False, True
    app.compare_saved = True
    app.eq_label = Variable()
    app.client = MemoryDevice()
    app.client.actual = WORKING
    app.snapshot = {'device': {'model': 'NFM-3'}, 'current': WORKING.as_dict(),
                    'presets': [preset.as_dict() for preset in app.client.saved],
                    'active_slot': 0, 'eq_enabled': True}
    app._populate(WORKING)
    app._run = lambda message, work, done: done(work())
    return app


def first_apply(app, gain='-3.4'):
    app.values[1]['gain'].set(gain)
    app.apply()
    return app.current


class Control:
    def __init__(self):
        self.options = {}

    def configure(self, **options):
        self.options.update(options)


def install_controls(app):
    names = ('read_button', 'apply_button', 'save_button', 'restore_button',
                'undo_button', 'redo_button', 'more_button', 'compare_button', 'review_button', 'plot_range_button', 'drag_mode_button',
                'copy_band_button', 'paste_band_button', 'zero_gain_button')
    for name in names + ('connect_button', 'combo', 'response_label'):
        setattr(app, name, Control())
    app.buttons = tuple(getattr(app, name) for name in names)
    app.editor_controls = []
    app.audition_buttons = {side: Control() for side in ('A', 'B')}
    app.curve_note, app.compare_saved = Variable(), True


class AuditionTests(unittest.TestCase):
    def test_a_records_unsaved_working_parameters_and_never_saves_a_preset(self):
        app = audition_app()
        applied = first_apply(app)
        self.assertEqual(app.audition.before.raw, WORKING.raw)
        self.assertNotEqual(app.audition.before.raw, ORIGINAL.raw)
        self.assertEqual(app.audition.after.raw, applied.raw)
        self.assertEqual(app.audition.side, 'B')
        self.assertEqual(app.client.saved, [ORIGINAL] * 10)
        self.assertEqual(app.client.state_writes, [])

    def test_repeated_apply_from_either_side_keeps_original_a_and_updates_b(self):
        app = audition_app()
        first_apply(app)
        for side, gain in [('B', '-3.5'), ('A', '-3.6')]:
            app.listen(side)
            app.values[1]['gain'].set(gain)
            app.apply()
            self.assertEqual(app.audition.before.raw, WORKING.raw)
            self.assertEqual(app.audition.after.raw, WORKING.with_field(1, 'gain', float(gain)).raw)
            self.assertEqual(app.current.raw, app.audition.after.raw)
            self.assertEqual(app.audition.side, 'B')
        self.assertEqual(app.client.state_writes, [])

    def test_switch_preserves_draft_selection_and_undo_redo_history(self):
        app = audition_app()
        applied = first_apply(app)
        app.selected_band = 2
        app.values[1]['gain'].set('-3.8dB')
        app._changed()
        draft_form = app._form()
        app.toggle_audition()
        self.assertEqual(app.current.raw, WORKING.raw)
        self.assertEqual(app._form(), draft_form)
        self.assertEqual(app.selected_band, 2)
        self.assertEqual(app.target().raw, WORKING.with_field(1, 'gain', -3.8).raw)
        app.undo()
        self.assertEqual(app.target().raw, applied.raw)
        self.assertTrue(app.history.can_redo)
        app.toggle_audition()
        self.assertEqual(app.current.raw, applied.raw)
        self.assertTrue(app.history.can_redo)
        app.redo()
        self.assertEqual(app._form(), draft_form)
        self.assertEqual(app.client.state_writes, [])

    def test_unfinished_input_does_not_block_switching_verified_a_and_b(self):
        app = audition_app()
        applied = first_apply(app)
        app.values[2]['frequency'].set('-')
        app._changed()
        form = app._form()
        for side, expected in [('A', WORKING), ('B', applied)]:
            app.listen(side)
            self.assertEqual(app.current.raw, expected.raw)
            self.assertEqual(app._form(), form)
            with self.assertRaises(FieldError):
                app.target()

    def test_clicking_active_side_does_not_write_or_backup_again(self):
        app = audition_app()
        first_apply(app)
        before = (len(app.client.writes), app.client.backups)
        app.listen('B')
        self.assertEqual((len(app.client.writes), app.client.backups), before)

    def test_offline_busy_closing_and_bypassed_states_do_not_dispatch_writes(self):
        for flags in ({'connected': False}, {'busy': True}, {'closing': True}, {'bypass': True}):
            with self.subTest(flags=flags):
                app = audition_app()
                first_apply(app)
                app._run = Mock()
                for name, value in flags.items():
                    if name == 'bypass':
                        app.snapshot['eq_enabled'] = False
                    else:
                        setattr(app, name, value)
                app.listen('A')
                app._run.assert_not_called()

    def test_changed_cached_slot_rejects_switch_before_dispatch(self):
        app = audition_app()
        first_apply(app)
        app.slot = 1
        app._run = Mock()
        app.listen('A')
        app._run.assert_not_called()
        self.assertIn('重新读取', app.status.get())

    def test_fresh_device_bypass_state_blocks_write_and_refreshes_display(self):
        app = audition_app()
        applied = first_apply(app)
        app.client.state_data[1] = 0
        app.client.writes.clear()
        app.listen('A')
        self.assertEqual(app.client.writes, [])
        self.assertEqual(app.current.raw, applied.raw)
        self.assertEqual(app.audition.side, 'B')
        self.assertFalse(app.snapshot['eq_enabled'])
        self.assertEqual(app.eq_label.get(), 'EQ 已旁通')
        self.assertIn('EQ 已旁通', app.status.get())

    def test_bypass_during_switch_keeps_verified_parameters_and_reports_actual_state(self):
        app = audition_app()
        first_apply(app)
        app.client.on_write = lambda device: device.state_data.__setitem__(1, 0)
        app.listen('A')
        self.assertEqual(app.current.raw, WORKING.raw)
        self.assertEqual(app.client.actual.raw, WORKING.raw)
        self.assertEqual(app.audition.side, 'A')
        self.assertFalse(app.snapshot['eq_enabled'])
        self.assertEqual(app.eq_label.get(), 'EQ 已旁通')
        self.assertEqual(app.edit_label.get(), 'A 参数 · EQ 已旁通')
        self.assertIn('EQ 已旁通', app.status.get())

    def test_external_slot_or_parameter_changes_block_a_b_writes(self):
        for change in ('identical_other_slot', 'changed_parameter'):
            with self.subTest(change=change):
                app = audition_app()
                first_apply(app)
                app.client.writes.clear()
                if change == 'identical_other_slot':
                    app.client.working[1] = app.current
                    app.client.active = 1
                else:
                    app.client.actual = app.current.with_field(2, 'frequency', 2400)
                actual = app.client.actual.raw
                with self.assertRaisesRegex(RuntimeError, '已改变|已发生变化'):
                    app.listen('A')
                self.assertEqual(app.client.writes, [])
                self.assertEqual(app.client.actual.raw, actual)
                self.assertEqual(app.audition.side, 'B')

    def test_lost_ack_rolls_back_without_promoting_the_requested_side(self):
        app = audition_app()
        applied = first_apply(app)
        app.values[2]['frequency'].set('2.4k')
        form = app._form()
        app.client.fail_on = len(app.client.writes) + 1
        with self.assertRaisesRegex(RuntimeError, '已恢复原参数'):
            app.listen('A')
        self.assertEqual(app.client.actual.raw, applied.raw)
        self.assertEqual(app.current.raw, applied.raw)
        self.assertEqual(app.audition.side, 'B')
        self.assertEqual(app._form(), form)

    def test_error_callback_ends_a_b_and_keeps_local_input(self):
        app = audition_app()
        first_apply(app)
        app.root, app.results = Root(), queue.Queue()
        app.device_label, app.eq_label, app.combo = Variable(), Variable(), Mock()
        app.client.port = object()
        app.values[2]['frequency'].set('2.4k')
        form = app._form()
        app.results.put((None, None, RuntimeError('readback failed')))
        app._poll()
        self.assertIsNone(app.audition)
        self.assertEqual(app._form(), form)
        self.assertIn('readback failed', app.status.get())

    def test_save_while_hearing_a_is_blocked_even_if_editor_matches_a(self):
        app = audition_app()
        first_apply(app)
        app.listen('A')
        app._set_form(preset_form(WORKING))
        app.save()
        self.assertEqual(app.client.saved, [ORIGINAL] * 10)
        self.assertEqual(app.client.state_writes, [])
        self.assertIn('先切换到 B', app.status.get())

    def test_explicit_save_b_ends_pair_and_next_apply_starts_a_new_baseline(self):
        app = audition_app()
        applied = first_apply(app)
        app.save()
        self.assertEqual(app.client.saved[0].raw, applied.raw)
        self.assertIsNone(app.audition)
        first_apply(app, '-3.5')
        self.assertEqual(app.audition.before.raw, applied.raw)

    def test_close_session_keeps_actual_a_separate_from_b_editor_values(self):
        app = audition_app()
        applied = first_apply(app)
        app.listen('A')
        with tempfile.TemporaryDirectory() as folder:
            app.session_store = SessionStore(Path(folder) / 'session.json')
            app._write_session()
            result = app.session_store.load()
        self.assertEqual(result['document']['current']['raw_hex'], WORKING.raw.hex())
        self.assertEqual(result['form'], preset_form(applied))
        self.assertEqual(result['document']['presets'][0]['raw_hex'], ORIGINAL.raw.hex())

    def test_controls_allow_verified_comparison_with_invalid_draft_and_block_save_a(self):
        app = audition_app()
        first_apply(app)
        app.listen('A')
        app.values[2]['frequency'].set('-')
        install_controls(app)
        App._controls(app)
        self.assertEqual(app.audition_buttons['A'].options['state'], 'normal')
        self.assertEqual(app.audition_buttons['B'].options['state'], 'normal')
        self.assertEqual(app.audition_buttons['A'].variant, 'primary')
        self.assertEqual(app.apply_button.options['state'], 'disabled')
        self.assertEqual(app.save_button.options['state'], 'disabled')
        self.assertEqual(app.response_label.options['text'], '·  B 预览')

    def test_apply_original_values_ends_redundant_comparison(self):
        app = audition_app()
        first_apply(app)
        app._set_form(preset_form(WORKING))
        app.apply()
        self.assertIsNone(app.audition)
        self.assertEqual(app.current.raw, WORKING.raw)


if __name__ == '__main__':
    unittest.main()
