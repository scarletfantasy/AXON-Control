"""Visual envelopes are time based and bounded; no playback or MIDI hardware."""
from pathlib import Path
import tempfile
import unittest

from desktop_integration import Preferences
from spectrum_style import Image, NeonRenderer, PeakTrail


class TrailTests(unittest.TestCase):
    def test_holds_then_decays_only_after_deadline(self):
        trail = PeakTrail()
        trail.update([20, 20000], [-20, -40], [-5, -10], 0)
        trail.update([20, 20000], [-70, -70], [-60, -60], .5)
        self.assertEqual(trail.levels, [-20, -40])
        self.assertEqual(trail.peaks, [-5, -10])
        trail.update([20, 20000], [-70, -70], [-60, -60], 1.15)
        self.assertAlmostEqual(trail.levels[0], -29)
        self.assertAlmostEqual(trail.peaks[0], -7.1)

    def test_decay_is_independent_of_frame_count(self):
        a, b = PeakTrail(), PeakTrail()
        for trail in (a, b):
            trail.update([20, 20000], [-10, -30], [-5], 0)
        for stamp in (.1, .3, .5, .7, 1, 2):
            a.update([20, 20000], [-90, -90], [-90], stamp)
        b.update([20, 20000], [-90, -90], [-90], 2)
        for first, second in zip(a.levels + a.peaks, b.levels + b.peaks):
            self.assertAlmostEqual(first, second)

    def test_silent_and_missing_values_never_invent_peaks(self):
        trail = PeakTrail()
        trail.update([20, 20000], [-90, float('nan')], [float('inf')], 0)
        trail.update([20, 20000], [-90, -90], [-90], 10)
        self.assertEqual(trail.levels, [-90, -90])
        self.assertEqual(trail.peaks, [-90])

    def test_grid_and_channel_changes_clear_previous_source(self):
        trail = PeakTrail()
        trail.update([20, 20000], [-5, -10], [-2, -3], 0)
        trail.update([20, 4000], [-80, -85], [-70], .1)
        self.assertEqual(trail.levels, [-80, -85])
        self.assertEqual(trail.peaks, [-70])
        trail.reset()
        self.assertIsNone(trail.grid)
        self.assertEqual(trail.levels, [])

    def test_a_new_louder_peak_is_immediate(self):
        trail = PeakTrail()
        trail.update([20, 20000], [-50, -60], [-40], 0)
        trail.update([20, 20000], [-10, -20], [-5], .1)
        self.assertEqual(trail.levels, [-10, -20])
        self.assertEqual(trail.peaks, [-5])

    def test_long_silence_returns_to_floor(self):
        trail = PeakTrail()
        trail.update([20, 20000], [-1, -5], [0], 0)
        trail.update([20, 20000], [-90, -90], [-90], 100)
        self.assertEqual(trail.levels, [-90, -90])
        self.assertEqual(trail.peaks, [-90])


@unittest.skipIf(Image is None, 'Pillow is not installed')
class NeonTests(unittest.TestCase):
    def test_size_cache_is_reused_and_replaced_on_resize(self):
        renderer = NeonRenderer('#1a2229')
        renderer.render(160, 100, (25, 150, 8, 80), [20, 20000], [-20, -40], [-20, -40])
        cached = renderer.base
        renderer.render(160, 100, (25, 150, 8, 80), [20, 20000], [-30, -50], [-20, -40])
        self.assertIs(renderer.base, cached)
        renderer.render(220, 140, (25, 210, 8, 120), [20, 20000], [-30, -50], [-20, -40])
        self.assertIsNot(renderer.base, cached)
        self.assertEqual(renderer.base.size, (440, 280))

    def test_styles_change_graphics_without_changing_input_or_dimensions(self):
        renderer = NeonRenderer('#1a2229')
        hz, levels = [20, 100, 1000, 20000], [-60, -10, -25, -65]
        args = (260, 170, (30, 245, 8, 145), hz, levels, [-50, -7, -20, -60])
        flow = renderer.render(*args)
        bars = renderer.render(*args, style='bars')
        self.assertEqual(flow.size, bars.size)
        self.assertNotEqual(flow.tobytes(), bars.tobytes())
        self.assertEqual(levels, [-60, -10, -25, -65])

    def test_bars_at_silence_equal_empty_plot(self):
        renderer = NeonRenderer('#1a2229')
        silent = renderer.render(200, 120, (25, 190, 8, 100), [20, 20000], [-90, -90], [-90, -90], style='bars')
        empty = renderer.render(200, 120, (25, 190, 8, 100), [], [], [], style='bars')
        self.assertEqual(silent.tobytes(), empty.tobytes())

    def test_glow_does_not_cover_axis_label_region(self):
        renderer = NeonRenderer('#1a2229')
        frame = renderer.render(260, 170, (30, 245, 8, 145), [20, 100, 20000], [0, -3, -80], [0, -3, -80])
        self.assertEqual(frame.getpixel((10, 75)), (26, 34, 41))
        self.assertEqual(frame.getpixel((120, 165)), (26, 34, 41))

    def test_maximized_plot_bounds_effect_buffers_and_keeps_output_size(self):
        renderer = NeonRenderer('#1a2229')
        frame = renderer.render(960, 900, (30, 945, 8, 875), [20, 100, 20000], [-60, -10, -80], [-55, -8, -70])
        self.assertEqual(frame.size, (960, 900))
        self.assertLessEqual(renderer.base.width * renderer.base.height, 1_285_000)


class StylePreferencesTests(unittest.TestCase):
    def test_legacy_settings_default_to_flow_and_new_style_roundtrips(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'desktop-settings.json'
            path.write_text('{"auto_connect": false, "audio_source": "speakers"}')
            prefs = Preferences(folder)
            self.assertEqual(prefs.audio_style, 'flow')
            prefs.save(prefs.minimize_to_tray, prefs.pause_in_background, audio_style='bars')
            restored = Preferences(folder)
            self.assertEqual(restored.audio_style, 'bars')
            self.assertEqual(restored.audio_source, 'speakers')
            self.assertFalse(restored.auto_connect)

    def test_invalid_style_keeps_previous_preferences(self):
        with tempfile.TemporaryDirectory() as folder:
            prefs = Preferences(folder)
            with self.assertRaises(ValueError):
                prefs.save(False, False, audio_style='invalid')
            self.assertEqual(prefs.audio_style, 'flow')
            self.assertTrue(prefs.minimize_to_tray)


if __name__ == '__main__':
    unittest.main()
