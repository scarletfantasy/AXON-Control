"""Window-state and complete-frame regressions; no audio capture or speaker access."""
import ctypes as C
from ctypes import wintypes as W
from pathlib import Path
import tempfile
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

import axon_control
from desktop_integration import Preferences
from smooth_render import SmoothCanvas, RasterLayer
from window_manager import NativeWindow, MonitorInfo, maximize_bounds


class MonitorBoundsTests(unittest.TestCase):
    def test_bottom_and_side_taskbars_use_monitor_relative_bounds(self):
        self.assertEqual(maximize_bounds(W.RECT(0, 0, 2560, 1440), W.RECT(0, 0, 2560, 1392)),
                         (2560, 1392, 0, 0))
        self.assertEqual(maximize_bounds(W.RECT(-1920, -1080, 0, 0), W.RECT(-1872, -1080, 0, 0)),
                         (1872, 1080, 48, 0))

    def test_native_callback_failure_keeps_the_original_result(self):
        window = NativeWindow.__new__(NativeWindow)
        window.common = Mock()
        window.common.DefSubclassProc.return_value = 17
        window.user = Mock()
        window.user.MonitorFromWindow.side_effect = OSError('Synthetic failure')
        window.callback_error = None
        self.assertEqual(window._procedure(1, 0x24, 0, 123, 0, 0), 17)
        self.assertEqual(window.callback_error, 'OSError')
        window.common.DefSubclassProc.assert_called_once()


class CompleteFrameTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.canvas = SmoothCanvas(self.root, width=80, height=60, bg='#11161b')
        self.canvas.pack()
        with self.canvas.paint():
            self.canvas.create_line(2, 2, 12, 12, fill='#ffffff')
            self.canvas.create_text(20, 20, text='Old', tags='label')

    def tearDown(self):
        self.root.destroy()

    def test_render_failure_keeps_the_last_complete_frame(self):
        original = self.canvas.find_all()
        image = self.canvas.itemcget('_smooth_layer', 'image')
        with patch.object(RasterLayer, 'render', side_effect=RuntimeError('Synthetic render failure')):
            with self.assertRaises(RuntimeError):
                with self.canvas.paint():
                    self.canvas.delete('all')
                    self.canvas.create_text(20, 20, text='New', tags='label')
                    # The old frame is still visible while new text remains hidden.
                    self.assertEqual(self.canvas.itemcget(original[-1], 'state'), 'normal')
                    self.assertEqual(self.canvas.itemcget(self.canvas.find_withtag('label')[-1], 'state'), 'hidden')
                    self.assertEqual(self.canvas.itemcget('_smooth_layer', 'image'), image)
        self.assertEqual(self.canvas.find_all(), original)
        self.assertEqual(self.canvas.itemcget('label', 'text'), 'Old')
        self.assertEqual(self.canvas.itemcget('_smooth_layer', 'image'), image)

    def test_repeated_frames_reuse_the_image_item_without_item_or_photo_growth(self):
        original_item = self.canvas.find_withtag('_smooth_layer')
        image_count = len(self.root.tk.call('image', 'names'))
        for index in range(25):
            with self.canvas.paint():
                self.canvas.delete('all')
                self.canvas.create_line(2, 2, 12+index, 12, fill='#ffffff')
                self.canvas.create_text(20, 20, text=str(index), tags='label')
            self.assertEqual(self.canvas.find_withtag('_smooth_layer'), original_item)
            self.assertEqual(len(self.canvas.find_all()), 3)
            self.assertEqual(self.canvas.itemcget('label', 'state'), 'normal')
        self.assertEqual(len(self.root.tk.call('image', 'names')), image_count)

    def test_resize_burst_draws_once_and_destroy_cancels_a_pending_redraw(self):
        draw = Mock()
        for _ in range(40):
            self.canvas.redraw_later(draw)
        self.root.update_idletasks()
        draw.assert_called_once()
        self.canvas.redraw_later(draw)
        self.canvas.destroy()
        self.root.update_idletasks()
        self.assertEqual(draw.call_count, 1)


class NativeWindowTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = tk.Tk()
        self.root.attributes('-alpha', 0)
        self.monitor = Mock(process=None, desired=None)
        self.monitor.poll.return_value = ([], None)
        with patch.object(axon_control, 'DATA', Path(self.folder.name)), \
                patch('audio_view.AudioMonitor', return_value=self.monitor):
            self.app = axon_control.App(self.root)
        self.app.audio_panel.after_cancel(self.app.audio_panel.start_timer)
        self.app.audio_panel.start_timer = None
        self.root.geometry('1000x760+30+30')
        self.pump()
        self.app._window_theme()

    def pump(self):
        self.root.after(30, self.root.quit)
        self.root.mainloop()

    def tearDown(self):
        if not self.app.closing:
            self.app.close()
        self.folder.cleanup()

    def test_maximize_restore_icon_bounds_and_normal_geometry_stay_in_sync(self):
        normal = self.app.native_window.bounds
        normal_geometry = self.app.native_window.geometry
        for _ in range(4):
            self.app.maximize_button._invoke()
            self.assertEqual(self.root.winfo_width(), self.app.native_window.bounds[2])
            self.assertEqual(self.app.eq_panel.body.winfo_width(),
                             self.app.eq_panel.winfo_width()-self.app.eq_panel.padding*2)
            self.assertEqual(self.app.canvas.winfo_width(), self.app.eq_panel.body.winfo_width())
            self.pump()
            self.assertTrue(self.app.native_window.maximized)
            self.assertEqual(self.app.maximize_button.icon_name, 'restore')
            self.assertFalse(self.app.resize_grip.winfo_ismapped())
            info = MonitorInfo()
            info.size = C.sizeof(info)
            window = self.app.native_window
            self.assertTrue(window.user.GetMonitorInfoW(window.user.MonitorFromWindow(window.hwnd, 2), C.byref(info)))
            self.assertEqual(window.bounds, (info.work.left, info.work.top,
                                             info.work.right-info.work.left, info.work.bottom-info.work.top))
            self.app._remember_environment()
            self.assertEqual(Preferences(self.folder.name).window_geometry, normal_geometry)
            self.app.maximize_button._invoke()
            self.pump()
            self.assertFalse(window.maximized)
            self.assertEqual(window.bounds, normal)
            self.assertEqual(self.app.maximize_button.icon_name, 'maximize')
            self.assertTrue(self.app.resize_grip.winfo_ismapped())
        self.monitor.start.assert_not_called()

    def test_layout_settle_runs_idle_work_without_dispatching_timers(self):
        timer, layout = Mock(), Mock()
        self.root.after(0, timer)
        self.root.after_idle(layout)
        self.app._settle_window_layout()
        layout.assert_called_once()
        timer.assert_not_called()
        self.pump()
        timer.assert_called_once()

    def test_queued_maximize_click_cannot_reenter_the_transition(self):
        self.root.bind('<<RepeatMaximize>>', lambda event: self.app._maximize())
        self.root.event_generate('<<RepeatMaximize>>', when='tail')
        with patch.object(self.app.native_window, 'show', wraps=self.app.native_window.show) as show:
            self.app._maximize()
            show.assert_called_once_with(True)
        self.assertTrue(self.app.native_window.maximized)
        self.assertFalse(self.app.window_transition)

    def test_close_event_during_transition_stops_layout_work_cleanly(self):
        self.root.bind('<<CloseDuringTransition>>', lambda event: self.app.close())
        self.root.event_generate('<<CloseDuringTransition>>', when='tail')
        self.app._maximize()
        self.assertTrue(self.app.closing)
        self.assertFalse(self.app.window_transition)
        self.monitor.close.assert_called_once()

    def test_external_native_actions_also_update_the_button(self):
        self.app.native_window.show(True)
        self.pump()
        self.assertEqual(self.app.maximize_button.icon_name, 'restore')
        self.app.native_window.show(False)
        self.pump()
        self.assertEqual(self.app.maximize_button.icon_name, 'maximize')

    def test_geometry_burst_applies_only_the_latest_size(self):
        with patch.object(self.app.native_window, 'resize', wraps=self.app.native_window.resize) as resize:
            for offset in range(25):
                self.app._queue_geometry('resize', 1000+offset, 760+offset)
            self.pump()
            resize.assert_called_once_with(1024, 784)
        self.assertEqual(self.app.native_window.bounds[2:], (1024, 784))
        self.assertEqual(self.root.winfo_width(), 1024)
        self.assertEqual(self.app.eq_panel.body.winfo_width(),
                         self.app.eq_panel.winfo_width()-self.app.eq_panel.padding*2)
        self.assertEqual(self.app.canvas.winfo_width(), self.app.eq_panel.body.winfo_width())

    def test_tray_and_taskbar_restore_preserve_maximized_and_normal_placement(self):
        normal = self.app.native_window.bounds
        for tray in (True, False):
            self.app._maximize()
            self.pump()
            self.app.desktop.minimize_to_tray = tray
            with patch.object(self.app.tray, 'show'):
                self.app._minimize()
            self.pump()
            self.app._restore_window()
            self.pump()
            self.assertTrue(self.app.native_window.maximized)
            self.assertEqual(self.app.maximize_button.icon_name, 'restore')
            self.app._maximize()
            self.pump()
            self.assertEqual(self.app.native_window.bounds, normal)


if __name__ == '__main__':
    unittest.main()
