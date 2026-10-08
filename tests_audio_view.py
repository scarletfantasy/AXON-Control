"""Hidden Tk checks for constrained layout and paused cursor removal."""
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

from audio_spectrum import SpectrumAnalyzer
from audio_view import AudioPanel


class AudioViewTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.update()
        # Windows defers geometry for withdrawn top levels. A transparent,
        # disabled test window exercises real allocation without visible UI.
        self.root.attributes('-alpha', 0.0)
        self.root.attributes('-disabled', True)
        self.root.overrideredirect(True)
        self.root.geometry('368x143')
        self.root.deiconify()
        self.monitor = Mock()
        self.monitor.poll.return_value = ([], None)
        self.panel = AudioPanel(self.root, monitor=self.monitor, autostart=False)
        self.panel.pack(fill='both', expand=True)
        self.root.update()
        self.addCleanup(self.root.destroy)

    def test_short_panel_keeps_spectrum_and_both_meters_visible(self):
        self.assertTrue(self.panel.compact)
        self.assertGreaterEqual(self.panel.canvas.winfo_height(), 65)
        self.assertEqual(self.panel.meters.winfo_height(), 22)
        self.assertEqual(self.panel.compact_source_button.winfo_manager(), 'pack')
        self.assertEqual(self.panel.source_button.winfo_manager(), '')
        self.root.geometry('368x254')
        self.root.update()
        self.assertFalse(self.panel.compact)
        self.assertEqual(self.panel.source_button.winfo_manager(), 'pack')
        self.assertEqual(self.panel.meters.winfo_height(), 42)

    def test_leaving_paused_spectrum_removes_cursor_without_repainting_image(self):
        self.panel.frame = SpectrumAnalyzer(48000, 2).analyze()
        self.panel.state.set('已暂停')
        self.panel.draw()
        image = self.panel.canvas._paint_photo
        self.panel._motion(Mock(x=150))
        self.assertTrue(self.panel.canvas.find_withtag('probe'))
        self.assertIs(self.panel.canvas._paint_photo, image)
        self.panel._leave()
        self.assertEqual(self.panel.canvas.find_withtag('probe'), ())
        self.assertIs(self.panel.canvas._paint_photo, image)

    def test_widget_close_cancels_audio_lifecycle_once(self):
        self.panel.close()
        self.panel.close()
        self.root.update()
        self.monitor.close.assert_called_once()
        self.assertIsNone(self.panel.timer)

    def test_identical_frames_skip_repaint_but_changed_values_redraw(self):
        self.panel.frame = SpectrumAnalyzer(48000, 2).analyze()
        with patch.object(self.panel, '_draw_spectrum', wraps=self.panel._draw_spectrum) as graph:
            self.panel.draw()
            self.panel.draw()
            self.assertEqual(graph.call_count, 1)
            self.panel.frame['levels'][10] += 1
            self.panel.draw()
            self.assertEqual(graph.call_count, 2)
        with patch.object(self.panel, '_draw_meters', wraps=self.panel._draw_meters) as meter:
            self.panel.draw_meters()
            self.panel.draw_meters()
            self.assertEqual(meter.call_count, 1)
            self.panel.frame['peaks'][0] += 1
            self.panel.draw_meters()
            self.assertEqual(meter.call_count, 2)

    def test_style_switch_keeps_paused_frame_and_does_not_restart_audio(self):
        self.panel.frame = SpectrumAnalyzer(48000, 2).analyze()
        frame = self.panel.frame
        self.panel.draw()
        self.panel.choose_style('bars')
        self.assertIs(self.panel.frame, frame)
        self.assertEqual(self.panel.style, 'bars')
        self.monitor.start.assert_not_called()
        self.monitor.stop.assert_not_called()
        self.assertFalse(self.panel.running)


if __name__ == '__main__':
    unittest.main()
