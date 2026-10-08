"""Live playback spectrum and level meters. All drawing runs on the Tk owner."""
import math
import time
import tkinter as tk
from tkinter import font as tkfont

from audio_capture import AudioMonitor, DEFAULT_SOURCE
from smooth_render import ANTIALIAS_AVAILABLE, ImageTk, SmoothCanvas, blend, painted
from spectrum_style import NeonRenderer, PeakTrail, STYLES
from ui_widgets import PANEL, EDGE, TEXT, MUTED, DIM, ACCENT, Button, Popup

AUDIO_COLOR = '#81c9c8'


def frequency_label(frequency):
    return f'{frequency / 1000:.1f} kHz' if frequency >= 1000 else f'{frequency:.0f} Hz'


class AudioPanel(tk.Frame):
    def __init__(self, parent, *, runtime=None, monitor=None, autostart=True):
        super().__init__(parent, bg=PANEL, width=368)
        self.pack_propagate(False)
        self.runtime = runtime
        self.monitor = monitor or AudioMonitor()
        self.source = DEFAULT_SOURCE
        self.source_name = '默认播放设备'
        self.devices = []
        self.frame = None
        self.popup = None
        self.running = False
        self.visible = True
        self.closed = False
        self.pointer = None
        self.bounds = None
        self.compact = False
        self.layout_width = None
        self.style = 'flow'
        self.visual = PeakTrail()
        self.visual_key = None
        self.renderer = NeonRenderer(PANEL)
        self.timer = self.start_timer = None
        self.state = tk.StringVar(value='电脑播放')
        self.readout = tk.StringVar(value='')
        self.note = tk.StringVar(value='电脑播放 · dBFS · 非音箱实测')
        body = tk.Frame(self, bg=PANEL)
        body.pack(fill='both', expand=True)
        header = self.header = tk.Frame(body, bg=PANEL)
        header.pack(fill='x')
        tk.Label(header, text='实时频谱', bg=PANEL, fg=TEXT,
                 font=('Microsoft YaHei UI', 11, 'bold')).pack(side='left')
        self.pause_button = Button(header, '开始', self.toggle, width=64, height=28,
                                   variant='secondary', font=('Microsoft YaHei UI', 9))
        self.pause_button.pack(side='right')
        self.readout_label = tk.Label(header, textvariable=self.readout, bg=PANEL, fg=MUTED,
                                      font=('Segoe UI', 9))
        self.readout_label.pack(side='right', padx=(4, 8))
        self.compact_source_button = Button(header, '音源 ▾', self.source_menu, width=180, height=26,
                                            variant='ghost', font=('Microsoft YaHei UI', 8), align='left')
        self.source_row = tk.Frame(body, bg=PANEL)
        self.source_row.pack(fill='x', pady=(8, 6))
        self.style_button = Button(self.source_row, '流光 ▾', self.style_menu, width=64, height=30,
                                   variant='secondary', font=('Microsoft YaHei UI', 8))
        self.style_button.pack(side='right', padx=(6, 0))
        self.source_button = Button(self.source_row, '默认播放设备 ▾', self.source_menu, width=276, height=30,
                                    variant='secondary', font=('Microsoft YaHei UI', 9), align='left')
        self.source_button.pack(side='left', fill='x', expand=True)
        self.canvas = SmoothCanvas(body, bg=PANEL, height=140, bd=0, highlightthickness=0)
        # Live plots keep 2x antialiasing; static editor geometry retains 3x.
        self.canvas.raster_scale = 2
        self.spectrum_signature = self.meter_signature = None
        self.canvas.bind('<Configure>', lambda event: self.draw())
        self.canvas.bind('<Motion>', self._motion)
        self.canvas.bind('<Leave>', self._leave)
        self.meters = SmoothCanvas(body, bg=PANEL, height=42, bd=0, highlightthickness=0)
        self.meters.bind('<Configure>', lambda event: self.draw_meters())
        self.note_label = tk.Label(body, textvariable=self.note, bg=PANEL, fg=DIM, anchor='w',
                                   font=('Microsoft YaHei UI', 8), wraplength=340, justify='left')
        self.note_label.pack(side='bottom', fill='x', pady=(1, 0))
        self.meters.pack(side='bottom', fill='x', pady=(3, 0))
        self.canvas.pack(fill='both', expand=True)
        self.label_font = tkfont.Font(font=('Microsoft YaHei UI', 9))
        self.source_button.bind('<Configure>', lambda event: self._source_caption(), add='+')
        self.bind('<Configure>', self._layout)
        self.timer = self.after(50, self.tick)
        if autostart:
            self.start_timer = self.after(350, self.start)

    def _record(self, event, **fields):
        if self.runtime:
            self.runtime.record(event, **fields)

    def start(self):
        if self.start_timer is not None:
            self.after_cancel(self.start_timer)
        self.start_timer = None
        if self.closed or not self.visible:
            return
        self.frame = None
        self.visual.reset()
        self.visual_key = None
        self.readout.set('')
        self.running = True
        self.state.set('正在打开音源…')
        self.note.set('电脑播放 · 正在打开音源…')
        self.pause_button.configure(text='暂停')
        self.monitor.start(self.source)
        self.draw()
        self.draw_meters()

    def toggle(self):
        if self.running:
            self.monitor.stop()
            self.running = False
            self.state.set('已暂停')
            self.note.set('已暂停 · 点击继续恢复实时显示')
            self.pause_button.configure(text='继续')
            self.draw()
            self.draw_meters()
        else:
            self.start()

    def set_visible(self, visible):
        self.visible = bool(visible)
        if self.visible:
            self.start()
        else:
            self.monitor.stop()
            self.running = False
            if self.popup and self.popup.winfo_exists():
                self.popup.destroy()

    def _source_caption(self):
        label = ('默认 · ' if self.source == DEFAULT_SOURCE else '') + self.source_name
        if self.source_button.winfo_width() < 260 and 'NUX AXON-3' in self.source_name:
            label = ('默认 · ' if self.source == DEFAULT_SOURCE else '') + 'NUX AXON-3'
        maximum = max(90, self.source_button.winfo_width() - 35)
        while len(label) > 2 and self.label_font.measure(label + ' ▾') > maximum:
            label = label[:-2] + '…'
        self.source_button.configure(text=label + ' ▾')
        short = 'NUX AXON-3' if 'NUX AXON-3' in self.source_name else self.source_name
        short = ('默认 · ' if self.source == DEFAULT_SOURCE else '') + short
        while len(short) > 2 and self.label_font.measure(short + ' ▾') > 153:
            short = short[:-2] + '…'
        self.compact_source_button.configure(text=short + ' ▾')

    def _layout(self, event=None):
        if self.closed:
            return
        width = self.winfo_width()
        if width != self.layout_width:
            self.layout_width = width
            self.note_label.configure(wraplength=max(100, width-4))
            self._source_caption()
        compact = self.winfo_height() < 210
        if compact == self.compact:
            return
        self.compact = compact
        if compact:
            self.source_row.pack_forget()
            self.source_button.pack_forget()
            self.readout_label.pack_forget()
            self.compact_source_button.pack(side='right', padx=(2, 4))
            self.meters.configure(height=22)
        else:
            self.compact_source_button.pack_forget()
            self.readout_label.pack(side='right', padx=(4, 8))
            self.source_row.pack(fill='x', pady=(8, 6), after=self.header)
            self.source_button.pack(side='left', fill='x', expand=True)
            self.meters.configure(height=42)
        self._source_caption()
        self.draw()
        self.draw_meters()

    def choose_source(self, source, label):
        self.source, self.source_name = source, label
        self._source_caption()
        self.start()

    def choose_style(self, style):
        if style not in dict(STYLES):
            raise ValueError('Unknown spectrum style')
        if self.closed:
            return
        self.style = style
        self.style_button.configure(text=('流光' if style == 'flow' else '能量柱') + ' ▾')
        self.draw()

    def style_menu(self):
        if self.popup and self.popup.winfo_exists():
            self.popup.destroy()
        owner = self.style_button
        self.popup = Popup(owner, [(label, lambda value=value: self.choose_style(value), True,
                                    value == self.style) for value, label in STYLES],
                           owner.winfo_rootx(), owner.winfo_rooty() + owner.winfo_height() + 4,
                           width=190)

    def source_menu(self, page=0):
        if self.popup and self.popup.winfo_exists():
            self.popup.destroy()
            self.popup = None
        items = [('系统默认播放设备（重新获取）',
                  lambda: self.choose_source(DEFAULT_SOURCE, '默认播放设备'),
                  True, self.source == DEFAULT_SOURCE)]
        for device in self.devices[page * 8:(page + 1) * 8]:
            items.append((device['label'],
                          lambda d=device: self.choose_source(d['id'], d['label']),
                          True, self.source == device['id']))
        if page:
            items.append(('‹ 上一页音源', lambda: self.source_menu(page - 1), True, False))
        if (page + 1) * 8 < len(self.devices):
            items.append(('更多播放音源 ›', lambda: self.source_menu(page + 1), True, False))
        items.append(('刷新播放设备列表', self.start, True, False))
        owner = self.compact_source_button if self.compact else self.source_button
        self.popup = Popup(owner, items,
                           owner.winfo_rootx(), owner.winfo_rooty() + owner.winfo_height() + 4,
                           width=480)

    def tick(self):
        self.timer = None
        if self.closed:
            return
        try:
            updates, frame = self.monitor.poll()
            for update in updates:
                kind = update['kind']
                if kind == 'devices':
                    self.devices = update['devices']
                elif kind == 'ready':
                    self.source_name = update['label']
                    self._source_caption()
                    self.state.set('等待播放声音')
                    self.note.set(f"电脑播放 · {update['sample_rate'] / 1000:g} kHz · 非音箱实测")
                    self._record('audio.ready', source=update['label'],
                                 sample_rate=update['sample_rate'], channels=update['channels'])
                elif kind == 'error':
                    self.running = False
                    self.frame = None
                    self.readout.set('')
                    self.state.set('音源不可用')
                    self.note.set(update['message'])
                    self.pause_button.configure(text='重试')
                    self._record('audio.error', message=update['message'])
                    frame = None
                    self.draw()
                    self.draw_meters()
            if frame is not None and self.running:
                self.frame = frame
                self.state.set('实时' if max(frame['peaks']) > -80 else '等待播放声音')
                self._readout()
                if self.winfo_viewable():
                    self.draw()
                    self.draw_meters()
        except Exception as error:
            self.monitor.stop()
            self.running = False
            self.state.set('音源不可用')
            self.note.set(f'音频显示暂停：{error}')
            self.pause_button.configure(text='重试')
            self._record('audio.error', message=str(error))
        finally:
            if not self.closed:
                idle = (not self.running and self.monitor.process is None
                        and self.monitor.desired is None)
                self.timer = self.after(250 if idle else 50, self.tick)

    def _readout(self):
        if not self.frame:
            self.readout.set('')
        elif self.pointer is not None and self.bounds:
            left, right, _, _ = self.bounds
            position = min(1, max(0, (self.pointer - left) / max(1, right - left)))
            index = round(position * (len(self.frame['levels']) - 1))
            self.readout.set(f"{frequency_label(self.frame['frequencies'][index])} · {self.frame['levels'][index]:.0f} dB")
        else:
            hz = self.frame['dominant_hz']
            self.readout.set(frequency_label(hz) if hz is not None else '')

    def _motion(self, event):
        self.pointer = event.x
        self._readout()
        self._probe()

    def _leave(self, event=None):
        self.pointer = None
        self.canvas.delete('probe')
        self._readout()

    def _probe(self):
        self.canvas.delete('probe')
        if self.pointer is not None and self.bounds:
            left, right, top, bottom = self.bounds
            if left <= self.pointer <= right:
                self.canvas.create_line(self.pointer, top, self.pointer, bottom,
                                        fill=MUTED, width=1, dash=(2, 4), tags='probe')
                if self.compact:
                    self.canvas.create_text(right - 3, top + 3, text=self.readout.get(), anchor='ne',
                                            fill=TEXT, font=('Segoe UI', 8), tags='probe')

    def draw(self):
        frame = self.frame
        self._sync_visual()
        signature = (self.canvas.winfo_width(), self.canvas.winfo_height(), self.running,
                     self.compact, self.state.get(), self.compact and self.pointer is None, self.style,
                     tuple(frame['frequencies']) if frame else None,
                     tuple(frame['levels']) if frame else None, tuple(self.visual.levels))
        if signature != self.spectrum_signature:
            self._draw_spectrum()
            self.spectrum_signature = signature
        # Cursor guides must remain outside the cached image, including while
        # paused, so leaving the graph removes them immediately.
        self._probe()

    def _sync_visual(self):
        if self.frame is None:
            if self.visual_key is not None:
                self.visual.reset()
                self.visual_key = None
            return
        frame = self.frame
        key = (tuple(frame['frequencies']), tuple(frame['levels']), tuple(frame['peaks']),
               frame.get('sequence'), frame.get('captured_at'))
        if key != self.visual_key:
            if self.running or not self.visual.levels:
                self.visual.update(frame['frequencies'], frame['levels'], frame['peaks'], time.monotonic())
            self.visual_key = key

    def _draw_spectrum(self):
        canvas = self.canvas
        canvas.delete('all')
        width, height = max(1, canvas.winfo_width()), max(1, canvas.winfo_height())
        left, right, top, bottom = 33, width - 12, 7, height - 21
        self.bounds = (left, right, top, bottom)
        if right <= left or bottom - top < 25:
            return
        maximum = self.frame['frequencies'][-1] if self.frame else 20000
        x = lambda hz: left + math.log(hz / 20) / math.log(maximum / 20) * (right - left)
        y = lambda db: top + min(90, max(0, -db)) / 90 * (bottom - top)
        if ANTIALIAS_AVAILABLE:
            picture = self.renderer.render(width, height, self.bounds,
                                            self.frame['frequencies'] if self.frame else [],
                                            self.frame['levels'] if self.frame else [], self.visual.levels,
                                            style=self.style, running=self.running)
            canvas._paint_photo = ImageTk.PhotoImage(picture, master=canvas)
            canvas.create_image(0, 0, image=canvas._paint_photo, anchor='nw', tags='_smooth_layer')
        for level in ((0, -45, -90) if bottom - top < 75 else (0, -30, -60, -90)):
            if not ANTIALIAS_AVAILABLE:
                canvas.create_line(left, y(level), right, y(level), fill='#2b353d', width=1)
            canvas.create_text(left - 7, y(level), text=str(level), fill=DIM, anchor='e',
                               font=('Segoe UI', 8))
        if self.compact and self.pointer is None:
            canvas.create_text(right - 3, top + 3, text='dBFS', anchor='ne',
                               fill=DIM, font=('Segoe UI', 8))
        for hz, label in ((20, '20'), (100, '100'), (1000, '1k'), (10000, '10k'), (20000, '20k')):
            if hz > maximum:
                continue
            if not ANTIALIAS_AVAILABLE:
                canvas.create_line(x(hz), top, x(hz), bottom, fill='#283139', width=1)
            canvas.create_text(x(hz), bottom + 13, text=label, fill=DIM, font=('Segoe UI', 8))
        if self.frame is not None and not ANTIALIAS_AVAILABLE:
            points = [coordinate for hz, db in zip(self.frame['frequencies'], self.frame['levels'])
                      for coordinate in (x(hz), y(db))]
            if self.style == 'bars':
                for i in range(0, len(points)-4, 4):
                    bx, by = points[i], min(points[i+1], points[i+3])
                    canvas.create_rectangle(bx+1, by, max(bx+2, points[i+2]-1), bottom,
                                            fill=AUDIO_COLOR if self.running else DIM, outline='')
            else:
                canvas.create_polygon(left, bottom, *points, right, bottom,
                                      fill=blend(PANEL, AUDIO_COLOR, 0.11), outline='')
                canvas.create_line(*points, fill=AUDIO_COLOR if self.running else DIM,
                                   width=1.8, capstyle='round', joinstyle='round')
        if self.state.get() != '实时':
            canvas.create_text((left + right) / 2, (top + bottom) / 2,
                               text=self.state.get(), fill=MUTED, font=('Microsoft YaHei UI', 9))

    def draw_meters(self):
        self._sync_visual()
        signature = (self.meters.winfo_width(), self.meters.winfo_height(), self.compact, self.running,
                     tuple(self.frame['rms']) if self.frame else None,
                     tuple(self.frame['peaks']) if self.frame else None, tuple(self.visual.peaks))
        if signature != self.meter_signature:
            self._draw_meters()
            self.meter_signature = signature

    @painted('meters')
    def _draw_meters(self):
        meter = self.meters
        meter.delete('all')
        width = max(1, meter.winfo_width())
        left, right = 24, width - 69
        if right <= left:
            return
        rms = self.frame['rms'] if self.frame else [-90, -90]
        peaks = self.frame['peaks'] if self.frame else [-90, -90]
        for index in range(min(2, len(rms))):
            if self.compact:
                column = width / min(2, len(rms))
                left, right = column * index + 20, column * (index + 1) - 43
                label_x, value_x, cy = column * index + 8, column * (index + 1) - 5, 11
            else:
                label_x, value_x, cy = 8, width - 4, 10 + index * 20
            meter.create_text(label_x, cy, text=('L', 'R')[index] if len(rms) > 1 else 'M',
                              fill=MUTED, font=('Segoe UI', 8))
            amount = min(1, max(0, (rms[index] + 60) / 60))
            color = '#e7a6b7' if peaks[index] >= -0.1 else AUDIO_COLOR
            segments = min(36, max(8, int((right-left) / 7)))
            for segment in range(segments):
                bx = left + (right-left) * segment / segments
                ex = left + (right-left) * (segment+1) / segments - 1.6
                meter.create_rounded(bx, cy-3.5, ex, cy+3.5, 1.3, fill='#2a3945', outline='')
                active = min(ex, left + (right-left) * amount)
                if active > bx:
                    hue = '#f0a5ba' if segment / segments > .92 else '#eacb94' if segment / segments > .8 else (
                        '#87baff' if segment / segments > .6 else '#61dacb')
                    meter.create_rounded(bx, cy-3.5, active, cy+3.5, 1.3,
                                         fill=hue if self.running else DIM, outline='')
            px = left + (right - left) * min(1, max(0, (peaks[index] + 60) / 60))
            if peaks[index] > -60:
                meter.create_line(px, cy - 5, px, cy + 5, fill=TEXT, width=1)
            if index < len(self.visual.peaks) and self.visual.peaks[index] > max(-60, peaks[index] + .5):
                hx = left + (right-left) * min(1, max(0, (self.visual.peaks[index] + 60) / 60))
                meter.create_line(hx, cy-5, hx, cy+5, fill='#a3b8f1' if self.running else DIM, width=2)
            caption = f'{peaks[index]:.1f}' if peaks[index] > -90 else '−∞'
            meter.create_text(value_x, cy, text=caption + ('' if self.compact else ' dBFS'),
                              fill=color if peaks[index] >= -0.1 else MUTED,
                              font=('Segoe UI', 8), anchor='e')

    def close(self):
        if self.closed:
            return
        self.closed = True
        for timer in (self.timer, self.start_timer):
            if timer is not None:
                self.after_cancel(timer)
        self.timer = self.start_timer = None
        if self.popup and self.popup.winfo_exists():
            self.popup.destroy()
        self.monitor.close()

    def destroy(self):
        self.close()
        super().destroy()
