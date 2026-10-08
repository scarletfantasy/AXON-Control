"""Keep cyclic Tk cleanup on the UI thread and record callback or hang diagnostics."""
import faulthandler
import gc
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import threading
import time


class UiRuntime:
    def __init__(self, root, folder, version, *, safe_gc=True, stall_seconds=15):
        self.root, self.folder, self.version = root, Path(folder), version
        self.owner = threading.get_ident()
        self.safe_gc, self.stall_seconds = safe_gc, stall_seconds
        self.previous_gc = gc.isenabled()
        self.closed = False
        self.stopped = threading.Event()
        self.state_lock, self.stack_lock = threading.Lock(), threading.Lock()
        self.last_beat, self.phase = time.monotonic(), 'starting'
        self.stalled = False
        self.heartbeat_id = self.collect_id = None
        self.previous_callback = root.report_callback_exception
        self.logger = logging.getLogger(f'axon.runtime.{os.getpid()}.{id(self)}')
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(self.folder / 'runtime.log', maxBytes=262144,
                                          backupCount=2, encoding='utf-8')
            handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s [%(threadName)s] %(message)s'))
        except OSError:
            handler = logging.NullHandler()
        self.logger.addHandler(handler)
        self.stack_file = None
        self.owns_fault_handler = False
        self.owns_timeout = False
        try:
            self.stack_file = self._open_stacks()
            if not faulthandler.is_enabled():
                faulthandler.enable(file=self.stack_file, all_threads=True)
                self.owns_fault_handler = True
        except (OSError, RuntimeError):
            pass
        if self.safe_gc:
            # CPython's cyclic collector can otherwise run on an AXON MIDI or
            # WinMM callback thread and finalize orphaned Tk images/variables.
            gc.disable()
        root.report_callback_exception = self.callback_error
        self.record('started', version=version, pid=os.getpid(), safe_gc=safe_gc)
        self._arm_timeout()
        self.heartbeat_id = root.after(1000, self.heartbeat)
        if self.safe_gc:
            self.collect_id = root.after(2000, self.collect)
        self.watcher = threading.Thread(target=self._watch, name='AXON diagnostics', daemon=True)
        self.watcher.start()

    def _open_stacks(self):
        path = self.folder / 'runtime-stacks.log'
        if path.exists() and path.stat().st_size >= 524288:
            path.replace(self.folder / 'runtime-stacks.previous.log')
        return path.open('a', encoding='utf-8', buffering=1)

    def record(self, event, **details):
        if self.closed:
            return
        with self.state_lock:
            self.phase = event
        self.logger.info('%s %s', event, json.dumps(details, ensure_ascii=False, default=str))

    def heartbeat(self):
        self.heartbeat_id = None
        if self.closed:
            return
        recovered = False
        with self.state_lock:
            self.last_beat = time.monotonic()
            if self.stalled:
                self.stalled, recovered = False, True
        if recovered:
            self.record('ui.recovered')
        self._arm_timeout()
        self.heartbeat_id = self.root.after(1000, self.heartbeat)

    def _arm_timeout(self):
        # The diagnostic worker may rotate this file while a heartbeat rearms
        # the native watchdog. Never give the watchdog a closing descriptor.
        with self.stack_lock:
            if self.closed or not self.owns_fault_handler or self.stack_file is None:
                return
            try:
                # CPython's C watchdog can dump even when a native call holds the
                # GIL and prevents the Python diagnostic thread from running.
                faulthandler.dump_traceback_later(self.stall_seconds, file=self.stack_file)
                self.owns_timeout = True
            except (OSError, RuntimeError, ValueError):
                self.logger.exception('Could not arm native stall diagnostics')

    def collect(self):
        if threading.get_ident() != self.owner:
            raise RuntimeError('Tk cleanup must run on the UI thread')
        self.collect_id = None
        if self.closed:
            return
        try:
            collected = gc.collect()
            if collected:
                self.logger.info('ui.gc collected=%d', collected)
        finally:
            if not self.closed:
                self.collect_id = self.root.after(2000, self.collect)

    def callback_error(self, exception_type, exception, traceback):
        self.logger.error('Tk callback failed', exc_info=(exception_type, exception, traceback))
        self.dump_stacks('Tk callback exception')

    def check_stall(self, now=None):
        now = time.monotonic() if now is None else now
        with self.state_lock:
            elapsed, phase = now-self.last_beat, self.phase
            if self.closed or self.stalled or elapsed < self.stall_seconds:
                return False
            self.stalled = True
        self.logger.error('ui.stalled seconds=%.2f last_phase=%s', elapsed, phase)
        self.dump_stacks(f'UI heartbeat stalled {elapsed:.2f}s; last phase: {phase}')
        return True

    def dump_stacks(self, reason):
        with self.stack_lock:
            if self.closed or self.stack_file is None:
                return
            try:
                # One stack dump per stall, bounded independently of runtime.log.
                if self.stack_file.tell() >= 524288:
                    if self.owns_timeout:
                        faulthandler.cancel_dump_traceback_later()
                        self.owns_timeout = False
                    if self.owns_fault_handler:
                        faulthandler.disable()
                    self.stack_file.close()
                    self.stack_file = self._open_stacks()
                    if self.owns_fault_handler:
                        faulthandler.enable(file=self.stack_file, all_threads=True)
                stamp = time.strftime('%Y-%m-%d %H:%M:%S')
                self.stack_file.write(f'\n{stamp} {reason}\n')
                self.stack_file.flush()
                faulthandler.dump_traceback(file=self.stack_file, all_threads=True)
            except (OSError, RuntimeError, ValueError):
                self.logger.exception('Could not write thread stacks')

    def _watch(self):
        # No Tk access, no device calls, no attempts to interrupt a write.
        while not self.stopped.wait(1):
            self.check_stall()

    def close(self):
        if self.closed:
            return
        if threading.get_ident() != self.owner:
            raise RuntimeError('UI runtime must close on its owning thread')
        self.record('stopped')
        self.closed = True
        self.stopped.set()
        if self.owns_timeout:
            faulthandler.cancel_dump_traceback_later()
            self.owns_timeout = False
        self.watcher.join(timeout=1)
        for token in (self.heartbeat_id, self.collect_id):
            if token is not None:
                try:
                    self.root.after_cancel(token)
                except Exception:
                    pass  # The normal close may already have destroyed the root.
        self.root.report_callback_exception = self.previous_callback
        if self.safe_gc:
            try:
                gc.collect()
            finally:
                if self.previous_gc:
                    gc.enable()
        with self.stack_lock:
            if self.owns_fault_handler:
                faulthandler.disable()
            if self.stack_file is not None:
                self.stack_file.close()
        for handler in list(self.logger.handlers):
            handler.close()
            self.logger.removeHandler(handler)
