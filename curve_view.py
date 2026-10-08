"""Curve sampling and graph coordinates; no GUI or device operations."""
import cmath
from collections import OrderedDict
from dataclasses import dataclass
import math

PLOT_RANGES = (3, 6, 12, 24)
DRAG_MODES = ('free', 'frequency', 'gain')


def band_response(band, index, frequency, sample_rate=48000):
    """RBJ biquad estimate; this is not a measurement of the speaker DSP."""
    if not band.enabled:
        return 0.0
    f0 = min(max(band.frequency, 1), sample_rate * 0.49)
    q = max(band.q, 0.1)
    w0 = 2 * math.pi * f0 / sample_rate
    alpha = math.sin(w0) / (2 * q)
    cosine = math.cos(w0)
    if index == 0:
        b = ((1+cosine)/2, -(1+cosine), (1+cosine)/2)
        a = (1+alpha, -2*cosine, 1-alpha)
    elif index == 6:
        b = ((1-cosine)/2, 1-cosine, (1-cosine)/2)
        a = (1+alpha, -2*cosine, 1-alpha)
    else:
        amplitude = 10 ** (band.gain / 40)
        b = (1+alpha*amplitude, -2*cosine, 1-alpha*amplitude)
        a = (1+alpha/amplitude, -2*cosine, 1-alpha/amplitude)
    z = cmath.exp(-2j * math.pi * frequency / sample_rate)
    numerator = b[0] + b[1]*z + b[2]*z*z
    denominator = a[0] + a[1]*z + a[2]*z*z
    return 20 * math.log10(max(abs(numerator / denominator), 1e-12))


@dataclass(frozen=True)
class PlotTransform:
    bounds: tuple
    gain_range: int = 12

    def __post_init__(self):
        left, right, top, bottom = self.bounds
        if (not all(math.isfinite(value) for value in self.bounds) or right <= left or bottom <= top
                or type(self.gain_range) is not int or self.gain_range not in PLOT_RANGES):
            raise ValueError('Invalid graph bounds or gain range')

    def frequency_x(self, frequency):
        left, right, top, bottom = self.bounds
        ratio = math.log10(min(20000, max(20, frequency))/20)/3
        return left+ratio*(right-left)

    def frequency_at(self, x):
        left, right, top, bottom = self.bounds
        ratio = min(1, max(0, (x-left)/(right-left)))
        return 20*1000**ratio

    def gain_y(self, gain):
        left, right, top, bottom = self.bounds
        gain = min(self.gain_range, max(-self.gain_range, gain))
        return top+(self.gain_range-gain)/(2*self.gain_range)*(bottom-top)

    def gain_at(self, y):
        left, right, top, bottom = self.bounds
        return self.gain_range-min(1, max(0, (y-top)/(bottom-top)))*2*self.gain_range

    def ticks(self, compact=False):
        limit = self.gain_range
        return (-limit, 0, limit) if compact else (-limit, -limit/2, 0, limit/2, limit)

    def drag(self, index, x, y, *, origin=None, fine=False, mode='free'):
        """Relative dragging keeps clipped nodes and click offsets from jumping."""
        if mode not in DRAG_MODES:
            raise ValueError('Invalid graph drag mode')
        left, right, top, bottom = self.bounds
        if origin is None:
            frequency, gain = self.frequency_at(x), self.gain_at(y)
            moved_x = moved_y = True
        else:
            start_x, start_y, original_frequency, original_gain = origin
            factor = 0.1 if fine else 1
            dx, dy = (x-start_x)*factor, (y-start_y)*factor
            log_frequency = math.log10(original_frequency)+dx/(right-left)*3
            frequency = 10**min(math.log10(20000), max(math.log10(20), log_frequency)) if dx else original_frequency
            gain = original_gain-dy/(bottom-top)*2*self.gain_range if dy else original_gain
            moved_x, moved_y = bool(dx), bool(dy)
        if moved_x:
            frequency = round(frequency, 1 if fine or frequency < 100 else 0)
        result = {'frequency': frequency} if mode != 'gain' or index in (0, 6) else {}
        if index not in (0, 6) and mode != 'frequency':
            result['gain'] = round(min(12, max(-12, gain)), 2 if fine else 1) if moved_y else gain
        return result


def drag_values(index, x, y, bounds, *, gain_range=12, origin=None, fine=False, mode='free'):
    return PlotTransform(bounds, gain_range).drag(index, x, y, origin=origin, fine=fine, mode=mode)


@dataclass(frozen=True)
class CurveSamples:
    frequencies: tuple
    totals: tuple
    bands: tuple


class ResponseCache:
    """Reuse raw responses when only the selection, scale or window size changes."""
    def __init__(self, capacity=4):
        self.capacity = capacity
        self.entries = OrderedDict()

    def get(self, preset, bypassed=False):
        key = (preset.raw, bool(bypassed))
        if key in self.entries:
            self.entries.move_to_end(key)
            return self.entries[key]
        bands = preset.bands
        frequencies = tuple(20*1000**(step/400) for step in range(401))
        responses = tuple(tuple(0.0 if bypassed else band_response(band, index, frequency)
                                for frequency in frequencies) for index, band in enumerate(bands))
        totals = tuple(sum(values) for values in zip(*responses))
        result = CurveSamples(frequencies, totals, responses)
        self.entries[key] = result
        if len(self.entries) > self.capacity:
            self.entries.popitem(last=False)
        return result
