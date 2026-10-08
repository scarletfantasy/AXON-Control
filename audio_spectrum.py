"""Streaming spectrum analysis; independent of Tk, MIDI and audio devices."""
import math

FFT_SIZE = 8192
FLOOR_DB = -90.0
DISPLAY_BINS = 144


class SpectrumAnalyzer:
    def __init__(self, sample_rate, channels):
        import numpy as np
        self.np = np
        if not 8000 <= sample_rate <= 384000 or not 1 <= channels <= 32:
            raise ValueError('不支持的音频格式')
        self.sample_rate, self.channels = int(sample_rate), int(channels)
        self.samples = np.zeros((FFT_SIZE, self.channels), dtype=np.float32)
        self.count = 0
        self.window = np.hanning(FFT_SIZE)
        self.scale = 2.0 / self.window.sum()
        self.fft_frequencies = np.fft.rfftfreq(FFT_SIZE, 1.0 / self.sample_rate)
        self.frequencies = np.geomspace(20, min(20000, self.sample_rate / 2), DISPLAY_BINS)
        edges = np.sqrt(self.frequencies[:-1] * self.frequencies[1:])
        edges = np.concatenate(([20.0], edges, [self.frequencies[-1]]))
        self.slices = [(np.searchsorted(self.fft_frequencies, low, side='left'),
                        np.searchsorted(self.fft_frequencies, high, side='right'))
                       for low, high in zip(edges[:-1], edges[1:])]
        self.occupied = np.array([i for i, (a, b) in enumerate(self.slices) if b > a], dtype=int)
        self.empty = np.array([i for i, (a, b) in enumerate(self.slices) if b <= a], dtype=int)
        lengths = [self.slices[i][1]-self.slices[i][0] for i in self.occupied]
        self.group_starts = np.cumsum([0]+lengths[:-1])
        self.group_indices = np.concatenate([np.arange(*self.slices[i]) for i in self.occupied])
        self.candidates = np.flatnonzero((self.fft_frequencies >= 20) &
                                        (self.fft_frequencies <= self.frequencies[-1]))
        self.power = np.zeros(DISPLAY_BINS)
        self.rms = np.zeros(min(2, self.channels))
        self.peaks = np.zeros(min(2, self.channels))
        self.clipped = False
        self.dominant_hz = None
        self.sequence = 0

    def feed(self, pcm):
        """Accept interleaved float32 PCM, keeping at most one FFT window."""
        np = self.np
        if len(pcm) % (4 * self.channels):
            raise ValueError('音频数据长度与声道数不匹配')
        data = np.frombuffer(pcm, dtype='<f4').reshape(-1, self.channels)
        if not len(data):
            return
        # A broken driver must not propagate NaN/Inf into drawing coordinates.
        data = np.nan_to_num(data, nan=0.0, posinf=0.0, neginf=0.0)
        data = np.clip(data, -8.0, 8.0)[-FFT_SIZE:]
        size = len(data)
        if size < FFT_SIZE:
            self.samples[:-size] = self.samples[size:]
        self.samples[-size:] = data
        self.count = min(FFT_SIZE, self.count + size)
        self.sequence += size

    def reset(self):
        self.samples.fill(0)
        self.count = 0
        self.power.fill(0)
        self.rms.fill(0)
        self.peaks.fill(0)

    def silence(self, seconds):
        frames = min(FFT_SIZE, max(1, round(self.sample_rate * seconds)))
        self.feed(bytes(frames * self.channels * 4))

    @staticmethod
    def _db(np, amplitude):
        return np.clip(20 * np.log10(np.maximum(amplitude, 10 ** (FLOOR_DB / 20))), FLOOR_DB, 0)

    def analyze(self, elapsed=0.05):
        np = self.np
        elapsed = min(1.0, max(0.001, elapsed))
        valid = self.samples[-max(1, self.count):]
        channel_rms = np.sqrt(np.mean(valid.astype(np.float64) ** 2, axis=0))[:2]
        channel_peaks = np.max(np.abs(valid), axis=0)[:2]
        release = math.exp(-elapsed / 0.25)
        self.rms = np.maximum(channel_rms, self.rms * release)
        self.peaks = np.maximum(channel_peaks, self.peaks * release)
        self.clipped = bool(np.any(np.abs(valid) >= 1.0))
        current = np.zeros(DISPLAY_BINS)
        self.dominant_hz = None
        if self.count == FFT_SIZE:
            # Average channel power, rather than audio samples: opposite-phase
            # left/right signals must not disappear from a playback spectrum.
            centered = self.samples - np.mean(self.samples, axis=0)
            fft = np.fft.rfft(centered * self.window[:, None], axis=0)
            power = np.mean(np.abs(fft * self.scale) ** 2, axis=1)
            current[self.occupied] = np.maximum.reduceat(power[self.group_indices], self.group_starts)
            current[self.empty] = np.interp(self.frequencies[self.empty], self.fft_frequencies, power)
            candidates = self.candidates
            if len(candidates):
                peak = int(candidates[np.argmax(power[candidates])])
                if power[peak] > 1e-8:
                    self.dominant_hz = float(self.fft_frequencies[peak])
        attack = 1 - math.exp(-elapsed / 0.045)
        decay = 1 - math.exp(-elapsed / 0.14)
        self.power += np.where(current > self.power, attack, decay) * (current - self.power)
        return {
            'frequencies': self.frequencies.tolist(),
            'levels': self._db(np, np.sqrt(self.power)).tolist(),
            'rms': self._db(np, self.rms).tolist(),
            'peaks': self._db(np, self.peaks).tolist(),
            'clipped': self.clipped, 'dominant_hz': self.dominant_hz,
            'sample_rate': self.sample_rate, 'channels': self.channels,
            'sequence': self.sequence,
        }
