"""Playback-only visual envelopes and a bounded, antialiased neon renderer."""
import math

try:
    from PIL import Image, ImageChops, ImageDraw, ImageFilter
except ImportError:
    Image = ImageChops = ImageDraw = ImageFilter = None

FLOOR = -90.0
STYLES = (('flow', '流光曲线'), ('bars', '能量柱'))
PALETTE = ('#51efd4', '#57cfff', '#7e9bff', '#c398ff')


def level(value):
    value = float(value)
    return min(0.0, max(FLOOR, value)) if math.isfinite(value) else FLOOR


class PeakTrail:
    """Short peak holds decay by elapsed time; no synthetic activity or PCM."""
    def __init__(self):
        self.reset()

    def reset(self):
        self.grid = None
        self.levels, self.peaks = [], []
        self.level_deadlines, self.peak_deadlines = [], []
        self.previous = None

    @staticmethod
    def _step(current, held, deadlines, now, previous, hold, decay):
        for i, value in enumerate(current):
            value = level(value)
            if value >= held[i]:
                held[i], deadlines[i] = value, now + hold
            else:
                elapsed = max(0.0, now - max(previous, deadlines[i]))
                held[i] = max(value, FLOOR, held[i] - decay * elapsed)

    def update(self, frequencies, levels, peaks, now):
        key = (tuple(frequencies), len(peaks))
        if len(frequencies) != len(levels):
            raise ValueError('Spectrum grid and level counts differ')
        if self.grid != key:
            self.grid = key
            self.levels, self.peaks = list(map(level, levels)), list(map(level, peaks))
            self.level_deadlines = [now + .65] * len(levels)
            self.peak_deadlines = [now + 1.0] * len(peaks)
            self.previous = now
            return
        previous = self.previous if self.previous is not None else now
        self._step(levels, self.levels, self.level_deadlines, now, previous, .65, 18)
        self._step(peaks, self.peaks, self.peak_deadlines, now, previous, 1.0, 14)
        self.previous = now


class NeonRenderer:
    """One size cache, 2x coverage, and graphic layers clipped to the plot."""
    def __init__(self, background):
        self.background = background
        self.key = None
        self.base = self.gradient = self.fade = self.bar_fade = self.clip = None

    @staticmethod
    def color(position):
        position = min(1.0, max(0.0, position)) * (len(PALETTE) - 1)
        index = min(len(PALETTE) - 2, int(position))
        amount = position - index
        a, b = PALETTE[index], PALETTE[index + 1]
        return tuple(round(int(a[i:i+2], 16) * (1 - amount) + int(b[i:i+2], 16) * amount)
                     for i in (1, 3, 5))

    def _prepare(self, width, height, bounds, maximum):
        key = (width, height, tuple(bounds), maximum)
        if self.key == key:
            return
        scale = 2
        size = (max(1, width * scale), max(1, height * scale))
        left, right, top, bottom = bounds
        box = tuple(round(v * scale) for v in bounds)
        l, r, t, b = box
        self.base = Image.new('RGB', size, self.background)
        grid = ImageDraw.Draw(self.base)
        grid.rectangle((l, t, r, b), fill='#131e28')
        for db in ((0, -45, -90) if bottom-top < 75 else (0, -30, -60, -90)):
            y = round((top - db / 90 * (bottom - top)) * scale)
            grid.line((l, y, r, y), fill='#2a3946', width=scale)
        for hz in (20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000):
            if hz > maximum:
                continue
            x = round((left + math.log(hz / 20) / math.log(maximum / 20) * (right - left)) * scale)
            grid.line((x, t, x, b), fill='#202f3b', width=1 if hz in (50, 200, 500, 2000, 5000) else scale)
        strip = Image.new('RGB', (256, 1))
        strip.putdata([self.color(i / 255) for i in range(256)])
        self.gradient = Image.new('RGB', size, self.color(0))
        self.gradient.paste(strip.resize((max(1, r-l+1), size[1]), Image.Resampling.BILINEAR), (l, 0))
        ramp = Image.linear_gradient('L').transpose(Image.Transpose.FLIP_TOP_BOTTOM)
        ramp = ramp.point([round(10 + i * .63) for i in range(256)])
        self.fade = Image.new('L', size, 0)
        self.fade.paste(ramp.resize((max(1, r-l+1), max(1, b-t+1))), (l, t))
        self.bar_fade = self.fade.point(lambda v: min(255, v+65))
        self.clip = Image.new('L', size, 0)
        ImageDraw.Draw(self.clip).rectangle((l, t, r, b), fill=255)
        self.key = key

    def render(self, width, height, bounds, frequencies, levels, trail, *, style='flow', running=True):
        if Image is None:
            raise RuntimeError('Pillow is unavailable')
        output_size = (width, height)
        # Bound effect buffers in maximized views. Native axis text remains at
        # the window's full resolution; only the antialiased graphic is scaled.
        if width * height > 320_000:
            ratio = math.sqrt(320_000 / (width * height))
            width, height = max(1, round(width*ratio)), max(1, round(height*ratio))
            bounds = tuple(value*ratio for value in bounds)
        left, right, top, bottom = bounds
        maximum = frequencies[-1] if frequencies else 20000
        self._prepare(width, height, bounds, maximum)
        picture = self.base.copy()
        if frequencies and levels and right > left and bottom > top:
            x = lambda hz: (left + math.log(hz / 20) / math.log(maximum / 20) * (right-left)) * 2
            y = lambda db: (top - level(db) / 90 * (bottom-top)) * 2
            points = [(x(hz), y(db)) for hz, db in zip(frequencies, levels)]
            fill = Image.new('L', picture.size, 0)
            painter = ImageDraw.Draw(fill)
            bars = []
            if style == 'bars':
                count = min(60, max(12, round((right-left) / 7)), len(levels))
                spacing = (right-left) * 2 / count
                for i in range(count):
                    start, end = i * len(levels) // count, (i+1) * len(levels) // count
                    db = max(levels[start:end])
                    if db <= FLOOR + .1:
                        continue
                    bx = left * 2 + i * spacing + 1.5
                    painter.rounded_rectangle((bx, y(db), bx + max(2, spacing-3), bottom * 2),
                                              radius=2, fill=235)
                    bars.append((bx, y(db), bx + max(2, spacing-3), y(db)))
                alpha = ImageChops.multiply(fill, self.bar_fade)
            else:
                painter.polygon([(left*2, bottom*2), *points, (right*2, bottom*2)], fill=255)
                alpha = ImageChops.multiply(fill, self.fade)
                glow = Image.new('L', picture.size, 0)
                ImageDraw.Draw(glow).line(points, fill=120 if running else 45, width=9, joint='curve')
                alpha = ImageChops.lighter(alpha, glow.filter(ImageFilter.BoxBlur(4)))
            # Peak trail is a held envelope, deliberately fainter than live data.
            if trail and max(levels) > -86:
                held = [(x(hz), y(db)) for hz, db in zip(frequencies, trail)]
                ImageDraw.Draw(alpha).line(held, fill=76 if running else 36, width=2, joint='curve')
            ink = ImageDraw.Draw(alpha)
            for cap in bars:
                ink.line(cap, fill=255, width=3)
            if style == 'flow':
                ink.line(points, fill=255 if running else 100, width=4, joint='curve')
            # A single color composite avoids multiple full-frame blend passes.
            picture.paste(self.gradient, (0, 0), ImageChops.multiply(alpha, self.clip))
            if style == 'flow':
                ImageDraw.Draw(picture).line(points, fill='#dcffff' if running else '#91a5af',
                                             width=1, joint='curve')
                if max(levels) > -80:
                    px, py = points[max(range(len(levels)), key=levels.__getitem__)]
                    marker = Image.new('RGBA', (20, 20), (0, 0, 0, 0))
                    dot = ImageDraw.Draw(marker)
                    dot.ellipse((1, 1, 19, 19), fill=(95, 236, 228, 40))
                    dot.ellipse((6, 6, 14, 14), fill=(227, 255, 252, 230))
                    mx, my = round(px)-10, round(py)-10
                    region = (max(0, round(left*2)-mx), max(0, round(top*2)-my),
                              min(20, round(right*2)-mx+1), min(20, round(bottom*2)-my+1))
                    if region[2] > region[0] and region[3] > region[1]:
                        marker = marker.crop(region)
                        picture.paste(marker, (mx+region[0], my+region[1]), marker.getchannel('A'))
            if not running:
                tint = Image.new('RGB', picture.size, self.background)
                picture = Image.blend(picture, tint, .28)
        picture = picture.resize((width, height), Image.Resampling.LANCZOS)
        return picture.resize(output_size, Image.Resampling.BICUBIC) if picture.size != output_size else picture
