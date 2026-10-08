"""Antialiased drawing layers. Native Tk text, entries and hit coordinates stay intact."""
from contextlib import contextmanager
from functools import wraps
import math
import tkinter as tk

try:
    from PIL import Image, ImageColor, ImageDraw, ImageTk
except ImportError:
    Image = ImageColor = ImageDraw = ImageTk = None

ANTIALIAS_AVAILABLE = Image is not None


def blend(background, foreground, amount):
    """Mix opaque UI colors without introducing black transparent-edge fringes."""
    amount = min(1, max(0, amount))
    channels = [round(int(background[i:i+2], 16)*(1-amount) +
                      int(foreground[i:i+2], 16)*amount) for i in (1, 3, 5)]
    return '#' + ''.join(f'{channel:02x}' for channel in channels)


def dashed_paths(points, pattern):
    """Split a polyline by distance; dash phase continues across sample boundaries."""
    pattern = tuple(float(value) for value in pattern)
    if not pattern or any(not math.isfinite(value) or value <= 0 for value in pattern):
        raise ValueError('Dash lengths must be finite and positive')
    if len(pattern) % 2:
        pattern *= 2
    position, remaining, run = 0, pattern[0], []
    for start, end in zip(points, points[1:]):
        dx, dy = end[0]-start[0], end[1]-start[1]
        length = math.hypot(dx, dy)
        if length == 0:
            continue
        travelled = 0.0
        while travelled < length-1e-9:
            step = min(remaining, length-travelled)
            a = (start[0]+dx*travelled/length, start[1]+dy*travelled/length)
            travelled += step
            b = (start[0]+dx*travelled/length, start[1]+dy*travelled/length)
            if position % 2 == 0:
                if not run:
                    run.append(a)
                run.append(b)
            remaining -= step
            if remaining <= 1e-9:
                if run:
                    yield tuple(run)
                    run = []
                position = (position+1) % len(pattern)
                remaining = pattern[position]
    if run:
        yield tuple(run)


class RasterLayer:
    """Lazy supersampling: unchanged geometry reuses its existing Tk image."""
    def __init__(self, width, height, background, scale=None):
        self.width, self.height = max(1, int(width)), max(1, int(height))
        self.background = background
        # Large maximized graphs use 2x to bound the temporary pixel allocation.
        self.scale = scale if scale is not None else (3 if self.width*self.height <= 500_000 else 2)
        if type(self.scale) is not int or not 1 <= self.scale <= 4:
            raise ValueError('Invalid supersampling scale')
        self.commands = []

    @property
    def signature(self):
        return (self.width, self.height, self.background, self.scale, tuple(self.commands))

    def add(self, kind, coordinates, fill, *, outline='', width=1, radius=0,
            dash=(), cap='butt', start=0, extent=90):
        coordinates = tuple(float(value) for value in coordinates)
        if any(not math.isfinite(value) for value in coordinates):
            raise ValueError('Drawing coordinates must be finite')
        if kind not in ('rounded', 'oval', 'polygon', 'arc', 'line'):
            raise ValueError('Unknown drawing primitive')
        if len(coordinates) % 2 or len(coordinates) < (6 if kind == 'polygon' else 4):
            raise ValueError('Incomplete drawing coordinates')
        if kind in ('rounded', 'oval', 'arc') and len(coordinates) != 4:
            raise ValueError('A bounding box needs four coordinates')
        if not math.isfinite(width) or width <= 0:
            raise ValueError('Stroke width must be finite and positive')
        self.commands.append((kind, coordinates, fill or '', outline or '', float(width),
                              float(radius), tuple(dash), cap, float(start), float(extent)))

    def render(self):
        if not ANTIALIAS_AVAILABLE:
            raise RuntimeError('Pillow is unavailable')
        scale = self.scale
        picture = Image.new('RGB', (self.width*scale, self.height*scale), self.background)
        draw = ImageDraw.Draw(picture)
        for kind, coordinates, fill, outline, width, radius, dash, cap, start, extent in self.commands:
            xy = tuple(value*scale for value in coordinates)
            stroke = max(1, round(width*scale))
            if kind == 'rounded':
                draw.rounded_rectangle(xy, radius=radius*scale, fill=fill or None,
                                       outline=outline or None, width=stroke)
            elif kind == 'oval':
                draw.ellipse(xy, fill=fill or None, outline=outline or None, width=stroke)
            elif kind == 'polygon':
                draw.polygon(xy, fill=fill or None)
                if outline:
                    vertices = list(zip(xy[::2], xy[1::2]))
                    draw.line(vertices + vertices[:1], fill=outline, width=stroke, joint='curve')
            elif kind == 'arc':
                if fill:
                    # Tk angles start at 3 o'clock and increase counterclockwise.
                    draw.arc(xy, start=-start-extent, end=-start, fill=fill, width=stroke)
            elif kind == 'line' and fill:
                vertices = tuple(zip(coordinates[::2], coordinates[1::2]))
                paths = dashed_paths(vertices, dash) if dash else (vertices,)
                for path in paths:
                    scaled = [(x*scale, y*scale) for x, y in path]
                    if len(scaled) < 2:
                        continue
                    draw.line(scaled, fill=fill, width=stroke, joint='curve')
                    if cap == 'round':
                        half = width*scale/2
                        for x, y in (scaled[0], scaled[-1]):
                            draw.ellipse((x-half, y-half, x+half, y+half), fill=fill)
        if scale > 1:
            picture = picture.resize((self.width, self.height), Image.Resampling.LANCZOS)
            # Lanczos can undershoot dark backgrounds around a bright corner.
            # Bound output channels to the actual scene colors to avoid a dark
            # fringe while retaining intermediate antialias coverage values.
            colors = {self.background}
            for command in self.commands:
                colors.update(color for color in command[2:4] if color)
            rgb = [ImageColor.getrgb(color) for color in colors]
            bounds = [(min(c[i] for c in rgb), max(c[i] for c in rgb)) for i in range(3)]
            picture = picture.point([min(high, max(low, value))
                                     for low, high in bounds for value in range(256)])
        return picture


def painted(attribute=None, *, tags='paint'):
    """Wrap a complete redraw, with a no-op path for ordinary canvases and mocks."""
    def decorate(method):
        @wraps(method)
        def call(owner, *args, **kwargs):
            canvas = getattr(owner, attribute, None) if attribute else owner
            if isinstance(canvas, SmoothCanvas):
                with canvas.paint(tags=tags):
                    return method(owner, *args, **kwargs)
            return method(owner, *args, **kwargs)
        return call
    return decorate


def framed(attribute):
    """Stage a caller-supplied image and its native text as one complete frame."""
    def decorate(method):
        @wraps(method)
        def call(owner, *args, **kwargs):
            canvas = getattr(owner, attribute, None)
            if isinstance(canvas, SmoothCanvas):
                with canvas.frame():
                    return method(owner, *args, **kwargs)
            return method(owner, *args, **kwargs)
        return call
    return decorate


class SmoothCanvas(tk.Canvas):
    """One raster graphic layer beneath native text and embedded input widgets."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._raster = None
        self._paint_photo = None
        self._paint_signature = None
        self._frame_state = None
        self._resize_timer = None
        self._resize_size = None
        self._resize_draw = None

    def bind_resize(self, callback):
        """Collapse a layout burst into one redraw after Tk's geometry settles."""
        def configured(event):
            size = (event.width, event.height)
            if size != self._resize_size:
                self._resize_size = size
                self.redraw_later(callback)
        self.bind('<Configure>', configured)

    def redraw_later(self, callback):
        self._resize_draw = callback
        if self._resize_timer is None:
            self._resize_timer = self.after_idle(self._flush_resize)

    def _flush_resize(self):
        self._resize_timer = None
        callback, self._resize_draw = self._resize_draw, None
        if callback is not None:
            callback()

    def destroy(self):
        timer = getattr(self, '_resize_timer', None)
        if timer is not None:
            self.after_cancel(timer)
        self._resize_timer = self._resize_draw = None
        # Release the Tk image now, on the same thread that destroys the
        # widget, instead of retaining it in an orphaned popup reference cycle.
        self._paint_photo = None
        self._paint_signature = None
        self._raster = None
        self._frame_state = None
        super().destroy()

    @contextmanager
    def frame(self):
        if getattr(self, '_frame_state', None) is not None:
            yield
            return
        state = self._frame_state = {'created': {}, 'deleted': set(), 'photo': None}
        try:
            yield
        except BaseException:
            if state['created']:
                super().delete(*state['created'])
            raise
        else:
            image_item = None
            if state['photo'] is not None:
                photo, signature, tags = state['photo']
                image_item = self._publish_photo(photo, signature, tags)
            deleted = state['deleted'] - {image_item}
            if deleted:
                super().delete(*deleted)
            for item, desired_state in state['created'].items():
                if item not in deleted:
                    super().itemconfigure(item, state=desired_state)
        finally:
            self._frame_state = None

    def delete(self, *targets):
        state = getattr(self, '_frame_state', None)
        if state is None:
            return super().delete(*targets)
        for target in targets:
            state['deleted'].update(super().find_withtag(target))

    def _create_item(self, method, *args, **options):
        state = getattr(self, '_frame_state', None)
        desired = options.get('state', 'normal')
        if state is not None:
            options['state'] = 'hidden'
        item = method(*args, **options)
        if state is not None:
            state['created'][item] = desired
        return item

    def create_text(self, *coordinates, **options):
        return self._create_item(super().create_text, *coordinates, **options)

    def create_rectangle(self, *coordinates, **options):
        return self._create_item(super().create_rectangle, *coordinates, **options)

    def _publish_photo(self, photo, signature, tags):
        existing = super().find_withtag('_smooth_layer')
        if existing:
            item = existing[0]
            super().itemconfigure(item, image=photo, tags=('_smooth_layer', tags))
        else:
            item = self._create_item(super().create_image, 0, 0, image=photo, anchor='nw',
                                     tags=('_smooth_layer', tags))
        super().tag_lower(item)
        # Retain the old PhotoImage until the canvas has switched to the new one.
        self._paint_photo, self._paint_signature = photo, signature
        return item

    def present(self, picture, *, tags='paint', signature=None):
        photo = ImageTk.PhotoImage(picture, master=self)
        if getattr(self, '_frame_state', None) is not None:
            self._frame_state['photo'] = (photo, signature, tags)
        else:
            self._publish_photo(photo, signature, tags)

    @contextmanager
    def paint(self, *, tags='paint'):
        if not ANTIALIAS_AVAILABLE or self._raster is not None:
            yield
            return
        layer = RasterLayer(self.winfo_width(), self.winfo_height(), self.cget('bg'),
                            scale=getattr(self, 'raster_scale', None))
        self._raster = layer
        try:
            with self.frame():
                yield
                if layer.signature != self._paint_signature or self._paint_photo is None:
                    self.present(layer.render(), tags=tags, signature=layer.signature)
                else:
                    self._frame_state['photo'] = (self._paint_photo, layer.signature, tags)
        finally:
            self._raster = None

    @staticmethod
    def _coordinates(coordinates):
        return tuple(coordinates[0]) if len(coordinates) == 1 else tuple(coordinates)

    def create_rounded(self, x1, y1, x2, y2, radius, **options):
        # New Tk widgets are initially 1x1 until their first Configure event.
        if x2 <= x1 or y2 <= y1:
            return self._create_item(super().create_rectangle, x1, y1, x2, y2, fill='', outline='',
                                     tags=options.get('tags', ()))
        radius = max(0, min(radius, (x2-x1)/2, (y2-y1)/2))
        if self._raster is not None:
            self._raster.add('rounded', (x1, y1, x2, y2), options.get('fill', ''),
                             outline=options.get('outline', ''), width=options.get('width', 1), radius=radius)
            return self._create_item(super().create_rectangle, x1, y1, x2, y2, fill='', outline='',
                                     tags=options.get('tags', ()))
        points = (x1+radius, y1, x2-radius, y1, x2, y1, x2, y1+radius,
                  x2, y2-radius, x2, y2, x2-radius, y2, x1+radius, y2,
                  x1, y2, x1, y2-radius, x1, y1+radius, x1, y1)
        return self._create_item(super().create_polygon, points, smooth=True, splinesteps=24, **options)

    def create_line(self, *coordinates, **options):
        if self._raster is None:
            return self._create_item(super().create_line, *coordinates, **options)
        self._raster.add('line', self._coordinates(coordinates), options.get('fill', 'black'),
                         width=options.get('width', 1), dash=options.get('dash', ()),
                         cap=options.get('capstyle', 'butt'))
        return self._create_item(super().create_line, *coordinates, fill='', tags=options.get('tags', ()))

    def create_oval(self, *coordinates, **options):
        if self._raster is None:
            return self._create_item(super().create_oval, *coordinates, **options)
        self._raster.add('oval', self._coordinates(coordinates), options.get('fill', ''),
                         outline=options.get('outline', 'black'), width=options.get('width', 1))
        return self._create_item(super().create_oval, *coordinates, fill='', outline='', tags=options.get('tags', ()))

    def create_polygon(self, *coordinates, **options):
        if self._raster is None:
            return self._create_item(super().create_polygon, *coordinates, **options)
        self._raster.add('polygon', self._coordinates(coordinates), options.get('fill', 'black'),
                         outline=options.get('outline', ''), width=options.get('width', 1))
        return self._create_item(super().create_polygon, *coordinates, fill='', outline='', tags=options.get('tags', ()))

    def create_arc(self, *coordinates, **options):
        if self._raster is None:
            return self._create_item(super().create_arc, *coordinates, **options)
        self._raster.add('arc', self._coordinates(coordinates), options.get('outline', 'black'),
                         width=options.get('width', 1), start=options.get('start', 0), extent=options.get('extent', 90))
        return self._create_item(super().create_arc, *coordinates, outline='', tags=options.get('tags', ()))


def icon(canvas, name, cx, cy, size=16, color='#adb6bd', tags=()):
    """Shared outline icons; independent of the font's symbol glyphs."""
    half, stroke = size/2, 1.5
    def line(*points):
        canvas.create_line(*points, fill=color, width=stroke, capstyle='round', joinstyle='round', tags=tags)
    if name == 'close':
        line(cx-half*.55, cy-half*.55, cx+half*.55, cy+half*.55)
        line(cx-half*.55, cy+half*.55, cx+half*.55, cy-half*.55)
    elif name == 'minimize':
        line(cx-half*.55, cy, cx+half*.55, cy)
    elif name == 'maximize':
        radius = half*.55
        line(cx-radius, cy-radius, cx+radius, cy-radius, cx+radius, cy+radius,
             cx-radius, cy+radius, cx-radius, cy-radius)
    elif name == 'restore':
        radius = half*.45
        offset = half*.3
        line(cx-radius+offset, cy+radius-offset, cx+radius+offset, cy+radius-offset,
             cx+radius+offset, cy-radius-offset, cx-radius+offset, cy-radius-offset,
             cx-radius+offset, cy-radius)
        line(cx-radius-offset/2, cy-radius+offset/2, cx+radius-offset/2, cy-radius+offset/2,
             cx+radius-offset/2, cy+radius+offset/2, cx-radius-offset/2, cy+radius+offset/2,
             cx-radius-offset/2, cy-radius+offset/2)
    elif name == 'more':
        for x in (cx-half*.6, cx, cx+half*.6):
            canvas.create_oval(x-1, cy-1, x+1, cy+1, fill=color, outline='', tags=tags)
    elif name == 'chevron':
        line(cx-half*.4, cy-half*.2, cx, cy+half*.2, cx+half*.4, cy-half*.2)
    elif name in ('plus', 'minus'):
        line(cx-half*.45, cy, cx+half*.45, cy)
        if name == 'plus':
            line(cx, cy-half*.45, cx, cy+half*.45)
    elif name in ('refresh', 'undo', 'redo'):
        radius = half*.72
        if name == 'refresh':
            canvas.create_arc(cx-radius, cy-radius, cx+radius, cy+radius, start=45, extent=285,
                              style='arc', outline=color, width=stroke, tags=tags)
            x, y = cx+radius*.7, cy-radius*.7
            line(x-4, y, x, y, x, y-4)
        else:
            canvas.create_arc(cx-radius, cy-radius, cx+radius, cy+radius, start=20, extent=140,
                              style='arc', outline=color, width=stroke, tags=tags)
            direction = -1 if name == 'undo' else 1
            x, y = cx+direction*radius*.94, cy-radius*.34
            line(x, y-3, x, y+1, x-direction*4, y+1)
