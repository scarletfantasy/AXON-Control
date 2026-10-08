"""Read-only WASAPI playback capture, isolated from the Tk/MIDI process."""
import multiprocessing as mp
import queue
import time

DEFAULT_SOURCE = '__default_playback__'


def source_label(name):
    label = name.removesuffix(' [Loopback]').strip('� ')
    if label.startswith('(') and label.endswith(')'):
        label = label[1:-1]
    return label or '播放设备'


def _send(channel, value):
    try:
        channel.put_nowait(value)
    except queue.Full:
        pass


def capture_worker(source, stopped, frames, events):
    """No Tk objects and no audio files. Native driver errors stay here."""
    from app_lifecycle import supervise_parent
    supervise_parent()
    backend = stream = None
    try:
        try:
            import pyaudiowpatch as pa
            from audio_spectrum import SpectrumAnalyzer
            import numpy  # validate optional dependency before opening capture
        except ImportError:
            raise RuntimeError('实时频谱需要音频组件。请按 README 安装 requirements.txt 后重新打开。') from None
        if stopped.is_set():
            return
        backend = pa.PyAudio()
        devices = list(backend.get_loopback_device_info_generator())
        _send(events, {'kind': 'devices', 'devices': [
            {'id': d['name'], 'label': source_label(d['name'])} for d in devices
        ]})
        if source == DEFAULT_SOURCE:
            device = backend.get_default_wasapi_loopback()
        else:
            device = next((d for d in devices if d['name'] == source), None)
            if device is None:
                raise RuntimeError('所选播放设备已不可用，请重新选择音源。')
        rate, channels = int(device['defaultSampleRate']), int(device['maxInputChannels'])
        analyzer = SpectrumAnalyzer(rate, channels)
        chunks = queue.Queue(maxsize=8)

        def callback(data, count, timing, status):
            if stopped.is_set():
                return (None, pa.paComplete)
            # Neither a native callback nor a slow UI may grow a PCM backlog.
            try:
                chunks.put_nowait((data, bool(status)))
            except queue.Full:
                pass
            return (None, pa.paContinue)

        stream = backend.open(format=pa.paFloat32, channels=channels, rate=rate,
                              input=True, input_device_index=int(device['index']),
                              frames_per_buffer=1024, stream_callback=callback)
        _send(events, {'kind': 'ready', 'label': source_label(device['name']),
                       'sample_rate': rate, 'channels': channels})
        previous = time.monotonic()
        last_received = previous
        while not stopped.wait(0.05):
            now = time.monotonic()
            received = False
            for _ in range(8):
                try:
                    data, discontinuity = chunks.get_nowait()
                except queue.Empty:
                    break
                if discontinuity:
                    analyzer.reset()
                analyzer.feed(data)
                received = True
            if received:
                last_received = now
            elif now - last_received > 0.1:
                analyzer.silence(now - previous)
            if not stream.is_active():
                raise RuntimeError('音频连接已中断。请确认播放设备后点击重试。')
            frame = analyzer.analyze(now - previous)
            frame['captured_at'] = now
            _send(frames, frame)
            previous = now
    except Exception as error:
        _send(events, {'kind': 'error', 'message': str(error)[:280] or '无法打开电脑播放音源。'})
    finally:
        if stream is not None:
            for cleanup in (stream.stop_stream, stream.close):
                try:
                    cleanup()
                except Exception:
                    pass
        if backend is not None:
            try:
                backend.terminate()
            except Exception:
                pass
        # A parent that has closed its window must never make us wait for a
        # pipe containing obsolete display frames to drain.
        frames.cancel_join_thread()
        if stopped.is_set():
            events.cancel_join_thread()
        else:
            # Deliver the explanatory error before a failed child exits.
            events.close()
            events.join_thread()


class AudioMonitor:
    """Nonblocking UI-side lifecycle. A stalled driver can be stopped safely."""
    def __init__(self, *, context=None, worker=capture_worker, clock=time.monotonic):
        self.context = context or mp.get_context('spawn')
        self.worker, self.clock = worker, clock
        self.process = None
        self.frames = self.events = self.stopped = None
        self.desired = None
        self.stop_deadline = None
        self.closed = False
        self.started_at = self.last_frame_at = None

    def start(self, source=DEFAULT_SOURCE):
        if self.closed:
            return
        self.desired = source
        self._request_stop()

    def stop(self):
        self.desired = None
        self._request_stop()

    def _request_stop(self):
        if self.process is not None and self.stop_deadline is None:
            self.stopped.set()
            self.stop_deadline = self.clock() + 0.6

    @staticmethod
    def _drain(channel):
        values = []
        if channel is not None:
            for _ in range(32):
                try:
                    values.append(channel.get_nowait())
                except (queue.Empty, EOFError, OSError):
                    break
        return values

    def _dispose(self):
        self.process.join(0)
        self.process.close()
        for channel in (self.frames, self.events):
            channel.cancel_join_thread()
            channel.close()
        self.process = self.frames = self.events = self.stopped = None
        self.stop_deadline = None

    def poll(self):
        updates, latest = [], None
        if self.process is not None:
            # Ignore late frames and messages from a source being replaced.
            if self.stop_deadline is None:
                updates = self._drain(self.events)
                values = self._drain(self.frames)
                if values:
                    latest = values[-1]
                    self.last_frame_at = self.clock()
                if any(item['kind'] == 'error' for item in updates):
                    self.stop()
            if not self.process.is_alive():
                if self.stop_deadline is None:
                    updates.extend(self._drain(self.events))
                    if not any(item['kind'] == 'error' for item in updates):
                        updates.append({'kind': 'error', 'message': '音频采集已停止，请点击重试。'})
                    self.desired = None
                self._dispose()
            elif self.stop_deadline is not None and self.clock() >= self.stop_deadline:
                self.process.terminate()
            elif self.stop_deadline is None:
                # A silent device still produces analyzed zero frames. No
                # frames at all indicates initialization/driver trouble.
                if self.clock() - self.last_frame_at > 8.0:
                    updates.append({'kind': 'error', 'message': '播放设备没有响应，请切换音源或重试。'})
                    self.stop()
        if self.process is None and self.desired is not None and not self.closed:
            try:
                self.frames = self.context.Queue(maxsize=2)
                self.events = self.context.Queue(maxsize=16)
                self.stopped = self.context.Event()
                self.process = self.context.Process(target=self.worker,
                                                    args=(self.desired, self.stopped, self.frames, self.events),
                                                    name='AXON playback analyzer', daemon=True)
                self.process.start()
                self.started_at = self.last_frame_at = self.clock()
            except Exception as error:
                self.desired = None
                updates.append({'kind': 'error', 'message': f'无法启动音频采集：{error}'})
                for channel in (self.frames, self.events):
                    if channel is not None:
                        channel.cancel_join_thread()
                        channel.close()
                if self.process is not None:
                    self.process.close()
                self.process = self.frames = self.events = self.stopped = None
        return updates, latest

    def close(self):
        self.closed = True
        self.stop()
        if self.process is not None:
            self.process.join(0.15)
            if self.process.is_alive():
                self.process.terminate()
                self.process.join(0.3)
            if not self.process.is_alive():
                self._dispose()
