"""Review accuracy, independent invalid fields and preview-only navigation."""
import struct
import unittest
from unittest.mock import Mock

from axon_control import App
from axon_protocol import Preset
from editor_state import (FieldError, changed_fields, preset_form, preview_from_form, review_form)
from tests_editor import ORIGINAL, memory_app, replace_field
from tests_audition import audition_app, install_controls


class ReviewTests(unittest.TestCase):
    def test_unit_aliases_do_not_create_phantom_changes(self):
        form = preset_form(ORIGINAL)
        for index, column, text in [(1, 1, '0.16 kHz'), (1, 2, '1.0Q'), (1, 3, '−4dB')]:
            form = replace_field(form, index, column, text)
        result = review_form(ORIGINAL, form)
        self.assertEqual(result.items, ())
        self.assertEqual(result.preview.raw, ORIGINAL.raw)

    def test_review_exactly_matches_the_bytes_that_full_apply_would_change(self):
        form = preset_form(ORIGINAL)
        for index, column, text in [(1, 1, '200Hz'), (2, 2, '1.2 Q'), (3, 3, '-0.5dB'), (6, 0, True)]:
            form = replace_field(form, index, column, text)
        result = review_form(ORIGINAL, form)
        target = preview_from_form(ORIGINAL, form)
        self.assertEqual(result.preview.raw, target.raw)
        self.assertEqual(tuple((item.index, item.field) for item in result.items), changed_fields(ORIGINAL, target))
        self.assertEqual([(item.before, item.after) for item in result.items],
                         [('160 Hz', '200 Hz'), ('1.25 Q', '1.2 Q'), ('0 dB', '-0.5 dB'), ('已关闭', '已启用')])

    def test_multiple_invalid_fields_keep_other_valid_edits_visible_but_not_applicable(self):
        form = preset_form(ORIGINAL)
        for index, column, text in [(1, 1, '21k'), (1, 3, '-'), (2, 1, '2.4k'), (3, 2, '0')]:
            form = replace_field(form, index, column, text)
        result = review_form(ORIGINAL, form)
        self.assertEqual(result.error_count, 3)
        self.assertEqual([(item.index, item.field) for item in result.items if item.error],
                         [(1, 'frequency'), (1, 'gain'), (3, 'q')])
        self.assertEqual(result.preview.raw, ORIGINAL.with_field(2, 'frequency', 2400).raw)
        with self.assertRaises(FieldError):
            preview_from_form(ORIGINAL, form)

    def test_cutoff_and_reserved_bytes_remain_unchanged_in_partial_preview(self):
        raw = bytearray(ORIGINAL.raw)
        struct.pack_into('<H', raw, 16, 77)
        struct.pack_into('<f', raw, 22, 1.3)
        base = Preset(raw)
        form = preset_form(base)
        form = replace_field(form, 0, 2, 'bad hidden Q')
        form = replace_field(form, 6, 3, '+30dB')
        form = replace_field(form, 1, 3, '-3.8')
        result = review_form(base, form)
        self.assertEqual([(item.index, item.field) for item in result.items], [(1, 'gain')])
        self.assertEqual(result.preview.raw[14:30], base.raw[14:30])
        self.assertEqual(result.preview.raw[110:126], base.raw[110:126])

    def test_hearing_a_reviews_actual_a_against_the_retained_b_editor(self):
        before = ORIGINAL.with_field(1, 'gain', -3.3)
        after = before.with_field(1, 'gain', -3.4)
        form = replace_field(preset_form(after), 2, 1, '2.4k')
        result = review_form(before, form, preview_base=after)
        self.assertEqual(result.preview.raw, after.with_field(2, 'frequency', 2400).raw)
        self.assertEqual([(item.index, item.field, item.before, item.after) for item in result.items],
                         [(1, 'gain', '-3.3 dB', '-3.4 dB'), (2, 'frequency', '2000 Hz', '2400 Hz')])
        self.assertEqual(before.raw, ORIGINAL.with_field(1, 'gain', -3.3).raw)

    def test_unchanged_original_values_outside_editor_range_are_retained(self):
        raw = bytearray(ORIGINAL.raw)
        struct.pack_into('<f', raw, 14+16*2+8, 12)
        base = Preset(raw)
        form = replace_field(preset_form(base), 1, 3, '-3.8')
        result = review_form(base, form)
        self.assertEqual(result.error_count, 0)
        self.assertEqual(result.preview.bands[2].q, 12)
        self.assertEqual(result.preview.raw, preview_from_form(base, form).raw)

    def test_editor_highlights_all_selected_errors_and_draws_valid_other_changes(self):
        app = memory_app()
        app.offline = True
        app.values[1]['frequency'].set('21k')
        app.values[1]['gain'].set('-')
        app.values[2]['frequency'].set('2.4k')
        app.cards = {field: Mock() for field in ('frequency', 'q', 'gain')}
        app.draw, app._refresh_tiles = Mock(), Mock()
        app._changed()
        expected = ORIGINAL.with_field(2, 'frequency', 2400)
        self.assertEqual(app.edit_label.get(), '2 项输入有误')
        app.cards['frequency'].set_error.assert_called_with('20–20000 Hz · 可输入 1k')
        app.cards['gain'].set_error.assert_called_with('−12–12 dB')
        self.assertEqual(app.draw.call_args.args[0].raw, expected.raw)
        self.assertEqual(app._refresh_tiles.call_args.args[0].raw, expected.raw)
        app.connected = True
        app.apply()
        app.save()
        self.assertEqual(app.current.raw, ORIGINAL.raw)

    def test_invalid_input_keeps_review_available_while_apply_and_save_are_disabled(self):
        app = audition_app()
        app.values[1]['frequency'].set('21k')
        app.values[2]['frequency'].set('2.4k')
        install_controls(app)
        app._changed()
        App._controls(app)
        self.assertEqual(app.review_button.options['state'], 'normal')
        self.assertEqual(app.review_button.options['text'], '需修正 · 1')
        self.assertEqual(app.apply_button.options['state'], 'disabled')
        self.assertEqual(app.save_button.options['state'], 'disabled')
        self.assertEqual(app.client.writes, [])
        self.assertEqual(app.client.state_writes, [])

    def test_navigation_only_focuses_the_selected_field_and_retains_local_input(self):
        app = memory_app()
        app.offline = True
        app.values[2]['frequency'].set('21k')
        app._changed()
        item = app.review.items[0]
        form = app._form()
        app.choose_band = Mock()
        app.cards = {'frequency': Mock()}
        app.jump_to_parameter(item)
        app.choose_band.assert_called_once_with(2)
        app.cards['frequency'].entry.focus_set.assert_called_once()
        app.cards['frequency'].entry.selection_range.assert_called_once_with(0, 'end')
        self.assertEqual(app._form(), form)
        self.assertEqual(app.current.raw, ORIGINAL.raw)
        self.assertIn('LMF · 频率', app.status.get())


if __name__ == '__main__':
    unittest.main()
