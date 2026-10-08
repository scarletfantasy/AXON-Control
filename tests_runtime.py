"""UI lifetime, bounded diagnostic logs and device-result recovery; no Tk or MIDI."""
import gc
import math
from pathlib import Path
import queue
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from tests_editor import memory_app
from ui_runtime import UiRuntime


class Root:
    def __init__(self):
        self.owner = threading.get_ident()
        self.report_callback_exception = Mock()
        self.callbacks, self.cancelled = {}, []
        self.alive, self.sequence = True, 0

    def after(self, delay, callback):
        if threading.get_ident() != self.owner:
            raise AssertionError('Diagnostic worker accessed Tk')
        if not self.alive:
            raise AssertionError('Polling continued after window destruction')
        self.sequence += 1
        token = f'after#{self.sequence}'
        self.callbacks[token] = (delay, callback)
        return token

    def after_cancel(self, token):
        self.cancelled.append(token)
        self.callbacks.pop(token, None)


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.previous_gc = gc.isenabled()
        self.runtime = None

    def tearDown(self):
        if self.runtime is not None:
            self.runtime.close()
        (gc.enable if self.previous_gc else gc.disable)()
        self.folder.cleanup()

    def start(self):
        self.root = Root()
        self.original_callback = self.root.report_callback_exception
        self.runtime = UiRuntime(self.root, self.folder.name, 'test')
        return self.runtime

    def test_orphaned_cycles_finalize_on_ui_instead_of_allocating_worker(self):
        runtime = self.start()
        gc.collect()
        finalized = []
        class Orphan:
            def __init__(self):
                self.loop = self
            def __del__(self):
                finalized.append(threading.get_ident())
        Orphan()
        def allocate():
            for _ in range(3000):
                cycle = {}
                cycle['self'] = cycle
        worker = threading.Thread(target=allocate, name='test device worker')
        worker.start()
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(finalized, [])
        self.assertFalse(gc.isenabled())
        runtime.collect()
        self.assertEqual(finalized, [threading.get_ident()])

    def test_wrong_thread_cleanup_is_rejected_without_losing_pending_timer(self):
        runtime = self.start()
        token, errors = runtime.collect_id, []
        def collect():
            try:
                runtime.collect()
            except Exception as error:
                errors.append(error)
        worker = threading.Thread(target=collect)
        worker.start()
        worker.join(timeout=2)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], RuntimeError)
        self.assertEqual(runtime.collect_id, token)

    def test_shutdown_restores_gc_cancels_timers_and_releases_files(self):
        gc.enable()
        runtime = self.start()
        stack_file, timers = runtime.stack_file, (runtime.heartbeat_id, runtime.collect_id)
        runtime.close()
        runtime.close()
        self.assertTrue(gc.isenabled())
        self.assertEqual(self.root.cancelled, list(timers))
        self.assertIs(self.root.report_callback_exception, self.original_callback)
        self.assertFalse(runtime.watcher.is_alive())
        self.assertTrue(stack_file.closed)

    def test_preexisting_disabled_gc_is_not_enabled_on_shutdown(self):
        gc.disable()
        runtime = self.start()
        runtime.close()
        self.assertFalse(gc.isenabled())

    def test_callback_exception_has_traceback_in_log_without_console(self):
        runtime = self.start()
        try:
            raise ValueError('callback example')
        except ValueError as error:
            runtime.callback_error(type(error), error, error.__traceback__)
        log = (Path(self.folder.name) / 'runtime.log').read_text(encoding='utf-8')
        self.assertIn('Tk callback failed', log)
        self.assertIn('ValueError: callback example', log)
        self.assertIn('test_callback_exception_has_traceback', log)
        stacks = (Path(self.folder.name) / 'runtime-stacks.log').read_text(encoding='utf-8')
        self.assertIn('Tk callback exception', stacks)

    def test_watchdog_needs_no_tk_and_dumps_once_until_heartbeat_recovers(self):
        runtime = self.start()
        results = []
        with patch.object(runtime, 'dump_stacks') as dump:
            def check():
                for _ in range(3):
                    results.append(runtime.check_stall(runtime.last_beat+16))
            worker = threading.Thread(target=check)
            worker.start()
            worker.join(timeout=2)
            self.assertEqual(results, [True, False, False])
            self.assertEqual(dump.call_count, 1)
            runtime.heartbeat()
            self.assertTrue(runtime.check_stall(runtime.last_beat+16))
            self.assertEqual(dump.call_count, 2)

    def test_unwritable_diagnostics_directory_does_not_disable_gc_protection(self):
        with patch.object(Path, 'mkdir', side_effect=OSError('read only')):
            runtime = self.start()
        self.assertFalse(gc.isenabled())
        self.assertTrue(self.root.callbacks)
        runtime.record('device.started', operation='test')

    def test_logs_have_bounded_size_and_rotation(self):
        runtime = self.start()
        for _ in range(32):
            runtime.record('test', text='x'*20000)
        files = list(Path(self.folder.name).glob('runtime.log*'))
        self.assertEqual(len(files), 3)
        self.assertTrue(all(path.stat().st_size <= 262144 for path in files))

    def test_preexisting_fault_handler_is_not_redirected_or_disabled(self):
        with patch('ui_runtime.faulthandler.is_enabled', return_value=True), \
                patch('ui_runtime.faulthandler.enable') as enable, \
                patch('ui_runtime.faulthandler.disable') as disable:
            runtime = self.start()
            runtime.close()
            enable.assert_not_called()
            disable.assert_not_called()

    def test_native_watchdog_rearms_on_heartbeat_and_cancels_on_close(self):
        with patch('ui_runtime.faulthandler.dump_traceback_later') as arm, \
                patch('ui_runtime.faulthandler.cancel_dump_traceback_later') as cancel:
            runtime = self.start()
            self.assertTrue(runtime.owns_timeout)
            runtime.heartbeat()
            self.assertEqual(arm.call_count, 2)
            self.assertEqual(arm.call_args.args, (15,))
            self.assertIs(arm.call_args.kwargs['file'], runtime.stack_file)
            runtime.close()
            cancel.assert_called_once()

    def test_native_watchdog_records_stall_when_python_thread_cannot_run(self):
        self.root = Root()
        self.runtime = UiRuntime(self.root, self.folder.name, 'test', stall_seconds=0.001)
        # A CPU-bound C calculation holds the GIL; the Python watcher cannot
        # run here. The native watchdog must still capture the thread stacks.
        math.factorial(100000)
        self.runtime.close()
        stacks = (Path(self.folder.name) / 'runtime-stacks.log').read_text(encoding='utf-8')
        self.assertIn('Timeout', stacks)
        self.assertIn('test_native_watchdog_records_stall', stacks)

    def test_stack_rotation_and_watchdog_rearm_use_the_live_file(self):
        runtime = self.start()
        old_file = runtime.stack_file
        old_file.write('x'*524288)
        old_file.flush()
        rotation_started, allow_rotation = threading.Event(), threading.Event()
        rearm_started, rearm_finished = threading.Event(), threading.Event()
        open_stacks = runtime._open_stacks
        def rotate():
            rotation_started.set()
            if not allow_rotation.wait(2):
                raise AssertionError('Stack rotation was not released')
            return open_stacks()
        def rearm():
            rearm_started.set()
            try:
                runtime._arm_timeout()
            finally:
                rearm_finished.set()
        def live_file(*args, file):
            self.assertFalse(file.closed)
            self.assertIs(file, runtime.stack_file)
        with patch.object(runtime, '_open_stacks', side_effect=rotate), \
                patch('ui_runtime.faulthandler.dump_traceback_later', side_effect=live_file) as arm:
            writer = threading.Thread(target=lambda: runtime.dump_stacks('rotation test'))
            writer.start()
            self.assertTrue(rotation_started.wait(2))
            timer = threading.Thread(target=rearm)
            timer.start()
            try:
                self.assertTrue(rearm_started.wait(2))
                self.assertFalse(rearm_finished.wait(0.03))
                self.assertFalse(arm.called)
            finally:
                allow_rotation.set()
                writer.join(timeout=2)
                timer.join(timeout=2)
            self.assertFalse(writer.is_alive())
            self.assertFalse(timer.is_alive())
            arm.assert_called_once()
        self.assertTrue(old_file.closed)
        self.assertIn('rotation test', (Path(self.folder.name)/'runtime-stacks.log').read_text(encoding='utf-8'))
        self.assertTrue((Path(self.folder.name)/'runtime-stacks.previous.log').exists())


class DevicePollingTests(unittest.TestCase):
    def app(self):
        app = memory_app()
        from axon_protocol import AxonClient
        app.client = AxonClient()
        app.root, app.results = Root(), queue.Queue()
        app.runtime = Mock()
        return app

    def test_worker_error_reaches_ui_queue_without_invoking_ui_callback(self):
        app, failure, done = self.app(), OSError('device gone'), Mock()
        def work():
            self.assertNotEqual(threading.get_ident(), app.root.owner)
            raise failure
        app._run('connect', work, done)
        callback, result, error = app.results.get(timeout=2)
        self.assertIs(callback, done)
        self.assertIsNone(result)
        self.assertIs(error, failure)
        self.assertTrue(app.busy)
        done.assert_not_called()

    def test_failed_readback_callback_keeps_later_device_results_polling(self):
        app, handled = self.app(), []
        app.busy = True
        app.results.put((Mock(side_effect=ValueError('bad UI callback')), None, None))
        app.results.put((handled.append, 'next readback', None))
        with self.assertRaisesRegex(ValueError, 'bad UI callback'):
            app._poll()
        self.assertFalse(app.busy)
        delay, scheduled = next(iter(app.root.callbacks.values()))
        self.assertEqual(delay, 60)
        scheduled()
        self.assertEqual(handled, ['next readback'])

    def test_completed_close_does_not_poll_a_destroyed_window(self):
        app = self.app()
        app.closing = True
        app.results.put((lambda result: None, None, None))
        def close():
            app.root.alive = False
            return True
        app.close = close
        app._poll()
        self.assertEqual(app.root.callbacks, {})


if __name__ == '__main__':
    unittest.main()
