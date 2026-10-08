"""PCM analysis and capture lifecycle tests. No real sound/MIDI device writes."""
import math
import queue
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np
from audio_capture import AudioMonitor, DEFAULT_SOURCE, capture_worker, source_label
from audio_spectrum import FFT_SIZE, FLOOR_DB, SpectrumAnalyzer


def tone(amplitude=0.5, channels=2, rate=48000, frequency=3000, frames=FFT_SIZE):
    signal = amplitude * np.sin(np.arange(frames) * 2 * math.pi * frequency / rate)
    return np.repeat(signal[:, None], channels, axis=1).astype('<f4')


class AnalysisTests(unittest.TestCase):
    def test_vectorized_groups_match_original_scalar_calculation(self):
        random = np.random.default_rng(71)
        for rate in (8000, 44100, 48000, 192000, 384000):
            analyzer = SpectrumAnalyzer(rate, 2)
            analyzer.feed((random.normal(size=(FFT_SIZE, 2))*.1).astype('<f4').tobytes())
            centered = analyzer.samples - np.mean(analyzer.samples, axis=0)
            fft = np.fft.rfft(centered * analyzer.window[:, None], axis=0)
            power = np.mean(np.abs(fft * analyzer.scale)**2, axis=1)
            expected = np.array([float(power[a:b].max()) if b > a else
                                 float(np.interp(analyzer.frequencies[i], analyzer.fft_frequencies, power))
                                 for i, (a, b) in enumerate(analyzer.slices)])
            analyzer.analyze(.05)
            np.testing.assert_allclose(analyzer.power, expected * (1-math.exp(-.05/.045)), rtol=1e-12)

    def analyze(self, samples, rate=48000):
        analyzer = SpectrumAnalyzer(rate, samples.shape[1])
        analyzer.feed(samples.astype('<f4').tobytes())
        return analyzer.analyze(elapsed=1)

    def test_tone_frequency_and_full_scale_calibration(self):
        frame = self.analyze(tone())
        self.assertAlmostEqual(frame['dominant_hz'], 3000)
        self.assertAlmostEqual(max(frame['levels']), -6.0206, places=2)
        self.assertAlmostEqual(frame['rms'][0], -9.0309, places=3)
        self.assertAlmostEqual(frame['peaks'][0], -6.0206, places=3)
        self.assertFalse(frame['clipped'])

    def test_silence_has_finite_floor_and_no_dominant_frequency(self):
        frame = self.analyze(np.zeros((FFT_SIZE, 2), dtype='<f4'))
        self.assertEqual(set(frame['levels']), {FLOOR_DB})
        self.assertEqual(frame['rms'], [FLOOR_DB, FLOOR_DB])
        self.assertEqual(frame['peaks'], [FLOOR_DB, FLOOR_DB])
        self.assertIsNone(frame['dominant_hz'])

    def test_opposite_phase_channels_do_not_cancel_spectrum(self):
        samples = tone()
        same = self.analyze(samples)
        samples[:, 1] *= -1
        opposite = self.analyze(samples)
        np.testing.assert_allclose(opposite['levels'], same['levels'])
        self.assertAlmostEqual(max(opposite['levels']), -6.0206, places=2)

    def test_silent_right_channel_is_not_copied_from_left(self):
        samples = tone()
        samples[:, 1] = 0
        frame = self.analyze(samples)
        self.assertEqual(frame['peaks'][1], FLOOR_DB)
        self.assertEqual(frame['rms'][1], FLOOR_DB)
        self.assertAlmostEqual(max(frame['levels']), -9.0309, places=2)

    def test_mono_capture_and_non_48k_formats(self):
        for rate in (44100, 96000):
            hz = rate * 256 / FFT_SIZE
            frame = self.analyze(tone(channels=1, rate=rate, frequency=hz), rate)
            self.assertEqual(len(frame['rms']), 1)
            self.assertAlmostEqual(frame['dominant_hz'], hz)
            self.assertAlmostEqual(max(frame['levels']), -6.0206, places=2)

    def test_display_stops_at_nyquist_for_low_rate_capture(self):
        frame = self.analyze(tone(channels=1, rate=8000, frequency=1000), 8000)
        self.assertEqual(frame['frequencies'][-1], 4000)
        self.assertEqual(frame['dominant_hz'], 1000)

    def test_dc_offset_is_removed_from_frequency_graph(self):
        frame = self.analyze(np.full((FFT_SIZE, 2), 0.5, dtype='<f4'))
        self.assertEqual(set(frame['levels']), {FLOOR_DB})
        self.assertIsNone(frame['dominant_hz'])
        self.assertAlmostEqual(frame['rms'][0], -6.0206, places=3)

    def test_nonfinite_driver_samples_do_not_break_display(self):
        samples = np.zeros((FFT_SIZE, 2), dtype='<f4')
        samples[100:103, 0] = [float('nan'), float('inf'), float('-inf')]
        frame = self.analyze(samples)
        self.assertTrue(all(math.isfinite(value) for value in frame['levels'] + frame['rms'] + frame['peaks']))
        self.assertEqual(max(frame['levels']), FLOOR_DB)

    def test_near_full_scale_and_out_of_range_samples_are_bounded(self):
        frame = self.analyze(tone(amplitude=2.0))
        self.assertTrue(frame['clipped'])
        self.assertEqual(frame['peaks'], [0, 0])
        self.assertTrue(all(FLOOR_DB <= value <= 0 for value in frame['levels']))

    def test_chunked_input_matches_continuous_input(self):
        samples = tone()
        analyzer = SpectrumAnalyzer(48000, 2)
        for chunk in np.array_split(samples, 16):
            analyzer.feed(chunk.tobytes())
        chunked = analyzer.analyze(1)
        full = self.analyze(samples)
        np.testing.assert_allclose(chunked['levels'], full['levels'])
        self.assertEqual(chunked['sequence'], FFT_SIZE)

    def test_history_is_bounded_and_new_audio_replaces_old_window(self):
        analyzer = SpectrumAnalyzer(48000, 2)
        analyzer.feed(tone(frames=FFT_SIZE * 4).tobytes())
        self.assertEqual(analyzer.count, FFT_SIZE)
        self.assertEqual(analyzer.samples.shape, (FFT_SIZE, 2))
        analyzer.feed(bytes(FFT_SIZE * 2 * 4))
        self.assertEqual(analyzer.analyze(1)['dominant_hz'], None)

    def test_no_callback_silence_releases_instead_of_freezing_graph(self):
        analyzer = SpectrumAnalyzer(48000, 2)
        analyzer.feed(tone().tobytes())
        analyzer.analyze(1)
        for _ in range(100):
            analyzer.silence(0.05)
            frame = analyzer.analyze(0.05)
        self.assertEqual(max(frame['levels']), FLOOR_DB)
        self.assertEqual(frame['peaks'], [FLOOR_DB, FLOOR_DB])
        self.assertIsNone(frame['dominant_hz'])

    def test_discontinuous_input_reset_clears_old_audio(self):
        analyzer = SpectrumAnalyzer(48000, 2)
        analyzer.feed(tone().tobytes())
        analyzer.analyze(1)
        analyzer.reset()
        frame = analyzer.analyze()
        self.assertEqual(frame['peaks'], [FLOOR_DB, FLOOR_DB])
        self.assertEqual(set(frame['levels']), {FLOOR_DB})

    def test_incomplete_pcm_and_unsupported_formats_are_rejected(self):
        with self.assertRaises(ValueError):
            SpectrumAnalyzer(48000, 2).feed(b'1234')
        for rate, channels in ((4000, 2), (48000, 0), (48000, 33), (float('nan'), 2)):
            with self.subTest(rate=rate, channels=channels), self.assertRaises(ValueError):
                SpectrumAnalyzer(rate, channels)


class Channel(queue.Queue):
    def cancel_join_thread(self):
        pass

    def close(self):
        pass

    def join_thread(self):
        pass


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.stopped = Mock()
        self.stopped.is_set.return_value = False
        self.stopped.wait.side_effect = [False, True]
        self.frames, self.events = Channel(2), Channel(16)
        self.devices = [dict(name='AXON [Loopback]', index=7, maxInputChannels=2, defaultSampleRate=48000),
                        dict(name='Other [Loopback]', index=8, maxInputChannels=2, defaultSampleRate=48000)]
        self.stream = Mock()
        self.stream.is_active.return_value = True
        self.backend = Mock()
        self.backend.get_loopback_device_info_generator.return_value = iter(self.devices)
        self.backend.get_default_wasapi_loopback.return_value = self.devices[0]

        def open_stream(**options):
            options['stream_callback'](tone().tobytes(), FFT_SIZE, {}, 0)
            return self.stream

        self.backend.open.side_effect = open_stream
        module = SimpleNamespace(PyAudio=lambda: self.backend, paFloat32=1, paComplete=1, paContinue=0)
        self.patch = patch.dict(sys.modules, {'pyaudiowpatch': module})
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_default_playback_capture_never_opens_an_output_stream(self):
        capture_worker(DEFAULT_SOURCE, self.stopped, self.frames, self.events)
        options = self.backend.open.call_args.kwargs
        self.assertTrue(options['input'])
        self.assertFalse(options.get('output', False))
        self.assertEqual(options['input_device_index'], 7)
        self.assertEqual(options['rate'], 48000)
        self.assertEqual(self.frames.get_nowait()['dominant_hz'], 3000)
        self.stream.close.assert_called_once()
        self.backend.terminate.assert_called_once()

    def test_explicit_source_uses_name_from_fresh_loopback_catalog(self):
        capture_worker('Other [Loopback]', self.stopped, self.frames, self.events)
        self.assertEqual(self.backend.open.call_args.kwargs['input_device_index'], 8)
        self.backend.get_default_wasapi_loopback.assert_not_called()

    def test_removed_source_reports_error_and_does_not_switch_to_another_device(self):
        capture_worker('missing [Loopback]', self.stopped, self.frames, self.events)
        updates = AudioMonitor._drain(self.events)
        self.assertEqual(updates[-1]['kind'], 'error')
        self.backend.open.assert_not_called()
        self.backend.terminate.assert_called_once()

    def test_missing_optional_dependency_returns_actionable_message(self):
        with patch.dict(sys.modules, {'pyaudiowpatch': None}):
            capture_worker(DEFAULT_SOURCE, self.stopped, self.frames, self.events)
        update = self.events.get_nowait()
        self.assertEqual(update['kind'], 'error')
        self.assertIn('requirements.txt', update['message'])
        self.backend.open.assert_not_called()

    def test_stop_failure_still_closes_stream_and_backend(self):
        self.stream.stop_stream.side_effect = OSError('device removed')
        capture_worker(DEFAULT_SOURCE, self.stopped, self.frames, self.events)
        self.stream.close.assert_called_once()
        self.backend.terminate.assert_called_once()


class Process:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.alive = False
        self.started = False
        self.terminated = False
        self.was_closed = False

    def start(self):
        self.started = self.alive = True

    def is_alive(self):
        return self.alive

    def terminate(self):
        self.terminated = True
        self.alive = False

    def join(self, timeout):
        pass

    def close(self):
        self.was_closed = True


class Context:
    def __init__(self):
        self.processes = []

    Queue = staticmethod(lambda maxsize: Channel(maxsize))
    Event = staticmethod(threading.Event)

    def Process(self, **kwargs):
        process = Process(**kwargs)
        self.processes.append(process)
        return process


class CaptureLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.context = Context()
        self.now = 0.0
        self.monitor = AudioMonitor(context=self.context, clock=lambda: self.now)
        self.addCleanup(self.monitor.close)

    def start(self):
        self.monitor.start()
        self.monitor.poll()
        return self.context.processes[-1]

    def test_start_does_not_initialize_driver_on_ui_thread(self):
        self.monitor.start()
        self.assertEqual(self.context.processes, [])
        self.monitor.poll()
        process = self.context.processes[0]
        self.assertTrue(process.started)
        self.assertEqual(process.kwargs['args'][0], DEFAULT_SOURCE)
        self.assertTrue(process.kwargs['daemon'])

    def test_display_queue_is_bounded_and_poll_uses_latest_frame(self):
        self.start()
        self.assertEqual(self.monitor.frames.maxsize, 2)
        self.monitor.frames.put_nowait({'frame': 1})
        self.monitor.frames.put_nowait({'frame': 2})
        with self.assertRaises(queue.Full):
            self.monitor.frames.put_nowait({'frame': 3})
        self.assertEqual(self.monitor.poll(), ([], {'frame': 2}))

    def test_source_change_ignores_old_frames_and_starts_only_after_stop(self):
        old = self.start()
        self.monitor.frames.put_nowait({'old_source': True})
        self.monitor.events.put_nowait({'kind': 'ready', 'old_source': True})
        self.monitor.start('new playback source')
        self.assertTrue(self.monitor.stopped.is_set())
        self.assertEqual(self.monitor.poll(), ([], None))
        self.assertEqual(len(self.context.processes), 1)
        old.alive = False
        self.monitor.poll()
        self.assertTrue(old.was_closed)
        self.assertEqual(self.context.processes[-1].kwargs['args'][0], 'new playback source')

    def test_rapid_switches_open_only_the_last_requested_source(self):
        old = self.start()
        self.monitor.start('A')
        self.monitor.start('B')
        self.monitor.start('C')
        old.alive = False
        self.monitor.poll()
        self.assertEqual(len(self.context.processes), 2)
        self.assertEqual(self.context.processes[-1].kwargs['args'][0], 'C')

    def test_pause_does_not_automatically_restart(self):
        process = self.start()
        self.monitor.stop()
        process.alive = False
        self.assertEqual(self.monitor.poll(), ([], None))
        self.assertIsNone(self.monitor.process)
        self.monitor.poll()
        self.assertEqual(len(self.context.processes), 1)

    def test_hung_driver_is_terminated_after_nonblocking_grace_period(self):
        process = self.start()
        self.monitor.stop()
        self.now = 0.5
        self.monitor.poll()
        self.assertFalse(process.terminated)
        self.now = 0.61
        self.monitor.poll()
        self.assertTrue(process.terminated)
        self.monitor.poll()
        self.assertIsNone(self.monitor.process)

    def test_capture_error_is_reported_without_restart_loop(self):
        process = self.start()
        self.monitor.events.put_nowait({'kind': 'error', 'message': 'device removed'})
        updates, _ = self.monitor.poll()
        self.assertEqual(updates[0]['message'], 'device removed')
        self.assertIsNone(self.monitor.desired)
        process.alive = False
        self.monitor.poll()
        self.assertEqual(len(self.context.processes), 1)

    def test_unexpected_native_crash_keeps_main_process_alive(self):
        process = self.start()
        process.alive = False
        updates, _ = self.monitor.poll()
        self.assertEqual(updates[0]['kind'], 'error')
        self.assertIsNone(self.monitor.desired)
        self.assertTrue(process.was_closed)

    def test_no_frames_timeout_can_be_retried(self):
        self.start()
        self.now = 8.1
        updates, _ = self.monitor.poll()
        self.assertEqual(updates[0]['kind'], 'error')
        self.now = 9
        self.monitor.poll()
        self.monitor.poll()
        self.monitor.start()
        self.monitor.poll()
        self.assertEqual(len(self.context.processes), 2)

    def test_regular_silent_frames_do_not_trigger_timeout(self):
        self.start()
        for count in range(12):
            self.now = count * 5
            self.monitor.frames.put_nowait({'silence': True})
            updates, _ = self.monitor.poll()
            self.assertEqual(updates, [])
        self.assertIsNotNone(self.monitor.desired)

    def test_close_terminates_stuck_child_and_is_idempotent(self):
        process = self.start()
        self.monitor.close()
        self.monitor.close()
        self.assertTrue(process.terminated)
        self.assertIsNone(self.monitor.process)
        self.monitor.start()
        self.monitor.poll()
        self.assertEqual(len(self.context.processes), 1)

    def test_failed_process_start_leaves_retry_available(self):
        self.context.Process = Mock(side_effect=OSError('spawn unavailable'))
        self.monitor.start()
        updates, _ = self.monitor.poll()
        self.assertEqual(updates[0]['kind'], 'error')
        self.assertIsNone(self.monitor.process)
        self.assertIsNone(self.monitor.desired)
        self.assertEqual(self.monitor.poll(), ([], None))

    def test_ipc_initialization_failure_is_reported_without_repeated_errors(self):
        self.context.Event = Mock(side_effect=OSError('IPC unavailable'))
        self.monitor.start()
        updates, _ = self.monitor.poll()
        self.assertEqual(updates[0]['kind'], 'error')
        self.assertIsNone(self.monitor.frames)
        self.assertEqual(self.monitor.poll(), ([], None))

    def test_spawn_failure_is_an_audio_error(self):
        process = Process()
        process.start = Mock(side_effect=OSError('spawn unavailable'))
        self.context.Process = Mock(return_value=process)
        self.monitor.start()
        updates, _ = self.monitor.poll()
        self.assertEqual(updates[0]['kind'], 'error')
        self.assertIsNone(self.monitor.process)
        self.assertIsNone(self.monitor.desired)

    def test_friendly_labels_keep_unicode_and_remove_loopback_suffix(self):
        self.assertEqual(source_label('扬声器 (NUX AXON-3) [Loopback]'), '扬声器 (NUX AXON-3)')
        self.assertEqual(source_label('��� (NUX AXON-3) [Loopback]'), 'NUX AXON-3')


if __name__ == '__main__':
    unittest.main()
