"""Small canvas controls for AXON Control. MIT license; no third-party UI kit."""
import math
import tkinter as tk
from tkinter import font as tkfont
from editor_state import parse_parameter
from smooth_render import SmoothCanvas, blend, icon as draw_icon, painted

BG = '#11161b'
PANEL = '#1a2229'
SURFACE = '#24303a'
EDGE = '#35434f'
TEXT = '#eef4f8'
MUTED = '#afbcc8'
DIM = '#81929e'
ACCENT = '#cdebab'
ERROR = '#e6c28a'
FONT = 'Microsoft YaHei UI'


def rounded(canvas, x1, y1, x2, y2, radius=12, **options):
    if isinstance(canvas, SmoothCanvas):
        return canvas.create_rounded(x1, y1, x2, y2, radius, **options)
    radius = min(radius, (x2-x1)/2, (y2-y1)/2)
    points = (x1+radius, y1, x2-radius, y1, x2, y1, x2, y1+radius,
              x2, y2-radius, x2, y2, x2-radius, y2, x1+radius, y2,
              x1, y2, x1, y2-radius, x1, y1+radius, x1, y1)
    return canvas.create_polygon(points, smooth=True, splinesteps=24, **options)


class Panel(SmoothCanvas):
    def __init__(self, parent, *, color=PANEL, radius=20, padding=20, **options):
        super().__init__(parent, bg=parent.cget('bg'), bd=0, highlightthickness=0, **options)
        self.color, self.radius, self.padding = color, radius, padding
        self.body = tk.Frame(self, bg=color)
        self.body_id = self.create_window(padding, padding, window=self.body, anchor='nw')
        self.bind('<Configure>', self._layout)

    @painted(tags='surface')
    def _layout(self, event):
        self.delete('surface')
        shape = rounded(self, 0, 0, event.width, event.height, self.radius,
                        fill=self.color, outline='', tags='surface')
        self.tag_lower(shape)
        self.itemconfigure(self.body_id, width=max(1, event.width-self.padding*2),
                           height=max(1, event.height-self.padding*2))


class Button(SmoothCanvas):
    def __init__(self, parent, text='', command=None, *, width=120, height=42,
                 variant='secondary', font=(FONT, 10), icon_name=None, align='center', **options):
        super().__init__(parent, bg=parent.cget('bg'), width=width, height=height,
                         bd=0, highlightthickness=0, takefocus=True, **options)
        self.text, self.command, self.variant, self.font = text, command, variant, font
        self.icon_name = icon_name
        self.align = align
        self.enabled, self.hover, self.focused = True, False, False
        self.bind('<Configure>', lambda event: self.redraw())
        self.bind('<Enter>', lambda event: self._hover(True))
        self.bind('<Leave>', lambda event: self._hover(False))
        self.bind('<FocusIn>', lambda event: self._focus(True))
        self.bind('<FocusOut>', lambda event: self._focus(False))
        self.bind('<ButtonRelease-1>', self._invoke)
        self.bind('<Return>', self._invoke)
        self.bind('<space>', self._invoke)

    def _hover(self, value):
        self.hover = value
        self.redraw()

    def _focus(self, value):
        self.focused = value
        self.redraw()

    def _invoke(self, event=None):
        if self.enabled and self.command:
            self.command()

    def configure(self, cnf=None, **options):
        if isinstance(cnf, dict):
            options.update(cnf)
        if 'state' in options:
            self.enabled = options.pop('state') != 'disabled'
        if 'text' in options:
            self.text = options.pop('text')
        result = super().configure(**options) if options else None
        self.redraw()
        return result

    config = configure

    @painted()
    def redraw(self):
        self.delete('all')
        width, height = max(1, self.winfo_width()), max(1, self.winfo_height())
        color = ACCENT if self.variant == 'primary' else SURFACE
        foreground = BG if self.variant == 'primary' else TEXT
        if self.variant == 'ghost':
            color, foreground = self.cget('bg'), MUTED
        if self.variant == 'warning':
            color, foreground = blend(self.cget('bg'), ERROR, 0.12), ERROR
        if self.enabled and self.hover:
            color = blend(ACCENT, TEXT, 0.22) if self.variant == 'primary' else blend(color, TEXT, 0.07)
            foreground = BG if self.variant == 'primary' else ERROR if self.variant == 'warning' else TEXT
        if not self.enabled:
            color = self.cget('bg') if self.variant == 'ghost' else blend(self.cget('bg'), SURFACE, 0.55)
            foreground = DIM
        outline = ACCENT if self.focused and self.enabled else (
            blend(color, EDGE, 0.45) if self.variant == 'secondary' else '')
        rounded(self, 1, 1, width-1, height-1, 12, fill=color,
                outline=outline, width=1)
        caption = self.text.rstrip()
        chevron = caption.endswith('▾')
        if chevron:
            caption = caption[:-1].rstrip()
            draw_icon(self, 'chevron', width-17, height/2, 14, foreground)
        if self.icon_name:
            draw_icon(self, self.icon_name, 17 if caption else width/2, height/2,
                      18 if self.icon_name == 'refresh' else 16, foreground)
        center = width/2 + (8 if self.icon_name and caption else 0) - (6 if chevron else 0)
        if caption:
            position = (34 if self.icon_name else 15) if self.align == 'left' else center
            self.create_text(position, height/2, text=caption, fill=foreground,
                             anchor='w' if self.align == 'left' else 'center', font=self.font)
        super().configure(cursor='hand2' if self.enabled else 'arrow')


class Popup(tk.Frame):
    def __init__(self, owner, items, x, y, width=280):
        host = owner.winfo_toplevel()
        super().__init__(host, bg=EDGE, bd=0)
        self.items = items
        self.row_height, self.padding, self.hover = 38, 8, -1
        self.hover = next((i for i, item in enumerate(items) if item[3]), 0)
        height = len(items)*self.row_height + self.padding*2
        x = max(8, min(x-host.winfo_rootx(), host.winfo_width()-width-8))
        y = max(38, min(y-host.winfo_rooty(), host.winfo_height()-height-12))
        self.place(x=x, y=y, width=width, height=height)
        self.canvas = SmoothCanvas(self, bg=PANEL, bd=0, highlightthickness=1,
                                highlightbackground=EDGE, width=width, height=height)
        self.canvas.pack(fill='both', expand=True)
        self.canvas.bind('<Motion>', self._motion)
        self.canvas.bind('<ButtonRelease-1>', self._pick)
        self.bind('<Escape>', lambda event: self.destroy())
        self.bind('<Down>', lambda event: self._step(1))
        self.bind('<Up>', lambda event: self._step(-1))
        self.bind('<Return>', lambda event: self._choose(self.hover))
        self.bind('<FocusOut>', lambda event: host.after_idle(self._focus_out))
        self.bind('<ButtonPress-1>', self._outside)
        self.lift()
        self.update_idletasks()
        self.focus_force()
        self.grab_set()
        self.redraw()

    def _focus_out(self):
        if self.winfo_exists() and self.focus_get() is None:
            self.destroy()

    def _outside(self, event):
        if not (self.winfo_rootx() <= event.x_root < self.winfo_rootx()+self.winfo_width()
                and self.winfo_rooty() <= event.y_root < self.winfo_rooty()+self.winfo_height()):
            self.destroy()

    def _motion(self, event):
        row = (event.y-self.padding)//self.row_height
        row = row if 0 <= row < len(self.items) else -1
        if row != self.hover:
            self.hover = row
            self.redraw()

    def _pick(self, event):
        row = (event.y-self.padding)//self.row_height
        self._choose(row)

    def _step(self, direction):
        if self.items:
            self.hover = (self.hover+direction) % len(self.items)
            self.redraw()

    def _choose(self, row):
        if 0 <= row < len(self.items):
            label, command, enabled, selected = self.items[row]
            if enabled:
                self.destroy()
                command()

    @painted('canvas')
    def redraw(self):
        self.canvas.delete('all')
        width = int(self.canvas.cget('width'))
        for index, (label, command, enabled, selected) in enumerate(self.items):
            top = self.padding + index*self.row_height
            if selected or (index == self.hover and enabled):
                rounded(self.canvas, 7, top, width-7, top+self.row_height, 9,
                        fill=SURFACE if not selected else blend(PANEL, ACCENT, 0.16), outline='')
            self.canvas.create_text(19, top+self.row_height/2, text=label, anchor='w',
                                    fill=TEXT if enabled else DIM, font=(FONT, 10))
            if selected:
                self.canvas.create_oval(width-25, top+16, width-19, top+22, fill=ACCENT, outline='')


class ActionDialog(tk.Frame):
    """An in-window, keyboard-friendly device action with inline validation."""
    def __init__(self, owner, title, detail, accept, *, confirm='确认', initial=None,
                 hint='', validate=None, warning=False):
        host = owner.winfo_toplevel()
        super().__init__(host, bg=EDGE, bd=0)
        self.accept, self.validate, self.host = accept, validate, host
        self.previous_focus = host.focus_get()
        self.previous_grab = host.grab_current()
        self.place(relx=0.5, rely=0.5, anchor='center', width=510)
        body = tk.Frame(self, bg=PANEL)
        body.pack(fill='both', expand=True, padx=1, pady=1)
        tk.Label(body, text=title, bg=PANEL, fg=TEXT, anchor='w',
                 font=(FONT, 13, 'bold')).pack(fill='x', padx=24, pady=(22, 12))
        tk.Label(body, text=detail, bg=PANEL, fg=MUTED, justify='left', anchor='w',
                 wraplength=460, font=(FONT, 10)).pack(fill='x', padx=24)
        self.value = tk.StringVar(master=self, value=initial or '')
        self.entry = None
        if initial is not None:
            self.entry = tk.Entry(body, textvariable=self.value, bg=SURFACE, fg=TEXT,
                                  insertbackground=ACCENT, relief='flat', bd=8,
                                  highlightthickness=1, highlightcolor=ACCENT,
                                  highlightbackground=EDGE, font=('Segoe UI', 13))
            self.entry.pack(fill='x', padx=24, pady=(16, 6))
            self.entry.bind('<Return>', lambda event: self.submit())
            self.entry.bind('<Escape>', lambda event: self.dismiss())
            tk.Label(body, text=hint, bg=PANEL, fg=DIM, anchor='w',
                     font=(FONT, 9)).pack(fill='x', padx=24)
        self.error = tk.StringVar(master=self)
        tk.Label(body, textvariable=self.error, bg=PANEL, fg=ERROR, anchor='w',
                 wraplength=460, justify='left', font=(FONT, 9)).pack(fill='x', padx=24, pady=(8, 0))
        actions = tk.Frame(body, bg=PANEL)
        actions.pack(fill='x', padx=24, pady=(12, 22))
        self.confirm_button = Button(actions, confirm, self.submit, width=150, height=38,
                                     variant='warning' if warning else 'primary')
        self.confirm_button.pack(side='right')
        self.cancel_button = Button(actions, '取消', self.dismiss, width=86, height=38, variant='ghost')
        self.cancel_button.pack(side='right', padx=8)
        self.focus_controls = ([self.entry] if self.entry is not None else []) + [self.cancel_button, self.confirm_button]
        for control in [self, *self.focus_controls]:
            control.bind('<Escape>', self._escape)
            control.bind('<Tab>', lambda event: self._tab(1))
            control.bind('<Shift-Tab>', lambda event: self._tab(-1))
        self.bind('<Return>', lambda event: self.submit())
        self.lift()
        self.grab_set()
        (self.entry or self).focus_set()
        if self.entry:
            self.entry.selection_range(0, 'end')

    def _escape(self, event=None):
        self.dismiss()
        return 'break'

    def _tab(self, direction):
        current = self.focus_get()
        index = self.focus_controls.index(current) if current in self.focus_controls else -1
        self.focus_controls[(index + direction) % len(self.focus_controls)].focus_set()
        return 'break'

    def submit(self):
        value = self.value.get()
        try:
            if self.validate:
                self.validate(value)
        except ValueError as error:
            self.error.set(str(error))
            return
        accept = self.accept
        self.dismiss()
        accept(value)

    def dismiss(self):
        self.grab_release()
        if self.previous_grab is not None and self.previous_grab.winfo_exists():
            self.previous_grab.grab_set()
        if self.previous_focus is not None and self.previous_focus.winfo_exists():
            self.previous_focus.focus_set()
        self.destroy()


class ReviewPopup(tk.Frame):
    """Scrollable changes with old/new values and direct field navigation."""
    def __init__(self, owner, items, x, y, width=440):
        host = owner.winfo_toplevel()
        super().__init__(host, bg=EDGE, bd=0)
        self.items, self.top = items, 0
        self.hover = next((i for i, item in enumerate(items) if item[3]), 0)
        self.row_height = 56
        self.visible = max(1, min(6, len(items), (host.winfo_height()-148)//self.row_height))
        height = 52 + self.visible*self.row_height + 30
        x = max(8, min(x-host.winfo_rootx(), host.winfo_width()-width-8))
        y = max(38, min(y-host.winfo_rooty(), host.winfo_height()-height-12))
        self.place(x=x, y=y, width=width, height=height)
        header = tk.Frame(self, bg=PANEL, height=50)
        header.pack(fill='x', padx=1, pady=(1, 0))
        header.pack_propagate(False)
        errors = sum(bool(item[3]) for item in items)
        title = f'本次应用改动 · {len(items)}' + (f'  /  {errors} 项需修正' if errors else '')
        tk.Label(header, text=title, bg=PANEL, fg=TEXT, font=(FONT, 10, 'bold')).pack(anchor='w', padx=17)
        tk.Label(header, text='上次读回 → 本地预览 · 点击条目定位参数', bg=PANEL,
                 fg=MUTED, font=(FONT, 8)).pack(anchor='w', padx=17, pady=(2, 0))
        body = tk.Frame(self, bg=PANEL)
        body.pack(fill='both', expand=True, padx=1)
        self.rail = SmoothCanvas(body, width=12, height=self.visible*self.row_height,
                              bg=PANEL, highlightthickness=0, bd=0)
        self.rail.pack(side='right', fill='y')
        self.canvas = SmoothCanvas(body, bg=PANEL, highlightthickness=0, bd=0,
                                width=width-14, height=self.visible*self.row_height)
        self.canvas.pack(side='left', fill='both', expand=True)
        self.small_font = tkfont.Font(root=self, family=FONT, size=9)
        footer = tk.Frame(self, bg=PANEL, height=29)
        footer.pack(fill='x', padx=1, pady=(0, 1))
        footer.pack_propagate(False)
        self.range_label = tk.Label(footer, bg=PANEL, fg=DIM, font=('Segoe UI', 8))
        self.range_label.pack(side='left', padx=17)
        tk.Label(footer, text='↑↓ 浏览 · Enter 定位 · Esc 关闭', bg=PANEL,
                 fg=MUTED, font=(FONT, 8)).pack(side='right', padx=12)
        self.canvas.bind('<Motion>', self._motion)
        self.canvas.bind('<ButtonRelease-1>', self._pick)
        self.canvas.bind('<Configure>', lambda event: self.redraw())
        self.rail.bind('<ButtonPress-1>', self._rail_move)
        self.rail.bind('<B1-Motion>', self._rail_move)
        for surface in (self, self.canvas, self.rail):
            surface.bind('<MouseWheel>', self._wheel)
        self.bind('<Up>', lambda event: self._step(-1))
        self.bind('<Down>', lambda event: self._step(1))
        self.bind('<Home>', lambda event: self._highlight(0))
        self.bind('<End>', lambda event: self._highlight(len(items)-1))
        self.bind('<Prior>', lambda event: self._step(-self.visible))
        self.bind('<Next>', lambda event: self._step(self.visible))
        self.bind('<Return>', lambda event: self._choose(self.hover))
        self.bind('<Escape>', lambda event: self.destroy())
        self.bind('<FocusOut>', lambda event: host.after_idle(self._focus_out))
        self.bind('<ButtonPress-1>', self._outside)
        self.lift()
        self.update_idletasks()
        self.focus_force()
        self.grab_set()
        self._highlight(self.hover)

    def _focus_out(self):
        if self.winfo_exists() and self.focus_get() is None:
            self.destroy()

    def _outside(self, event):
        if not (self.winfo_rootx() <= event.x_root < self.winfo_rootx()+self.winfo_width()
                and self.winfo_rooty() <= event.y_root < self.winfo_rooty()+self.winfo_height()):
            self.destroy()

    def _highlight(self, index):
        self.hover = max(0, min(len(self.items)-1, index))
        if self.hover < self.top:
            self.top = self.hover
        elif self.hover >= self.top+self.visible:
            self.top = self.hover-self.visible+1
        self.redraw()
        return 'break'

    def _step(self, direction):
        return self._highlight(self.hover+direction)

    def _wheel(self, event):
        self.top = max(0, min(len(self.items)-self.visible, self.top+(-1 if event.delta > 0 else 1)))
        self.hover = max(self.top, min(self.hover, self.top+self.visible-1))
        self.redraw()
        return 'break'

    def _rail_move(self, event):
        height = max(1, self.rail.winfo_height())
        self.top = max(0, min(len(self.items)-self.visible,
                             round(event.y/height*len(self.items)-self.visible/2)))
        self.hover = self.top
        self.redraw()

    def _motion(self, event):
        index = self.top+event.y//self.row_height
        if self.top <= index < min(len(self.items), self.top+self.visible) and index != self.hover:
            self.hover = index
            self.redraw()

    def _pick(self, event):
        self._choose(self.top+event.y//self.row_height)

    def _choose(self, index):
        if 0 <= index < len(self.items):
            command = self.items[index][4]
            self.destroy()
            command()
        return 'break'

    def _fit(self, text, width):
        text = ' '.join(str(text).split())
        if self.small_font.measure(text) <= width:
            return text
        left, right = 0, len(text)
        while left < right:
            middle = (left+right+1)//2
            if self.small_font.measure(text[:middle]+'…') <= width:
                left = middle
            else:
                right = middle-1
        return text[:left]+'…'

    @painted('canvas')
    def redraw(self):
        self.canvas.delete('all')
        width = max(1, self.canvas.winfo_width())
        for row, index in enumerate(range(self.top, min(len(self.items), self.top+self.visible))):
            title, before, after, invalid, command = self.items[index]
            top = row*self.row_height
            if index == self.hover:
                rounded(self.canvas, 7, top+1, width-3, top+self.row_height-1, 9, fill=SURFACE, outline='')
            self.canvas.create_text(17, top+14, text=title, anchor='w', fill=TEXT,
                                    font=(FONT, 9, 'bold'))
            if invalid:
                self.canvas.create_text(width-14, top+14, text='输入有误', anchor='e', fill=ERROR, font=(FONT, 8))
            self.canvas.create_text(17, top+37, text=self._fit(before, 112), anchor='w', fill=MUTED,
                                    font=self.small_font)
            self.canvas.create_text(146, top+37, text='→', fill=DIM, font=self.small_font)
            self.canvas.create_text(172, top+37, text=self._fit(after, width-190), anchor='w',
                                    fill=ERROR if invalid else ACCENT, font=self.small_font)
        self.range_label.configure(text=f'{self.top+1}–{min(len(self.items), self.top+self.visible)} / {len(self.items)}')
        with self.rail.paint():
            self.rail.delete('all')
            if len(self.items) > self.visible:
                height = max(1, self.rail.winfo_height())
                thumb_height = height*self.visible/len(self.items)
                top = height*self.top/len(self.items)
                rounded(self.rail, 3, top+1, 8, top+thumb_height-1, 2, fill=DIM, outline='')


class PresetPicker(Button):
    def __init__(self, parent, variable, **options):
        super().__init__(parent, command=self.open, **options)
        self.variable, self.values, self.popup = variable, [], None
        variable.trace_add('write', lambda *_: self._text())
        self._text()

    def _text(self):
        self.text = (self.variable.get() or '选择预设') + '   ▾'
        self.redraw()

    def configure(self, cnf=None, **options):
        if 'values' in options:
            self.values = list(options.pop('values'))
        return super().configure(cnf, **options)

    config = configure

    def current(self, index=None):
        if index is not None:
            self.variable.set(self.values[index])
            return index
        try:
            return self.values.index(self.variable.get())
        except ValueError:
            return -1

    def open(self):
        if self.popup and self.popup.winfo_exists():
            self.popup.destroy()
            self.popup = None
            return
        def choose(index):
            self.current(index)
            self.event_generate('<<ComboboxSelected>>')
        items = [(label, lambda i=index: choose(i), True, index == self.current())
                 for index, label in enumerate(self.values)]
        self.popup = Popup(self, items, self.winfo_rootx(), self.winfo_rooty()+self.winfo_height()+6,
                           max(290, self.winfo_width()))


class BandTile(SmoothCanvas):
    def __init__(self, parent, label, description, color, command):
        super().__init__(parent, bg=parent.cget('bg'), width=112, height=64,
                         highlightthickness=0, bd=0, takefocus=True)
        self.label, self.description, self.color, self.command = label, description, color, command
        self.selected, self.band_enabled, self.enabled, self.hover = False, True, False, False
        self.frequency = '—'
        self.change_count, self.invalid = 0, False
        self.bind('<Configure>', lambda event: self.redraw())
        self.bind('<Enter>', lambda event: self._hover(True))
        self.bind('<Leave>', lambda event: self._hover(False))
        self.bind('<ButtonRelease-1>', lambda event: self.command() if self.enabled else None)
        self.bind('<Return>', lambda event: self.command() if self.enabled else None)

    def _hover(self, value):
        self.hover = value
        self.redraw()

    def configure(self, cnf=None, **options):
        if 'state' in options:
            self.enabled = options.pop('state') != 'disabled'
        result = super().configure(cnf, **options) if cnf or options else None
        self.redraw()
        return result

    config = configure

    def update_band(self, frequency, band_enabled, selected, *, changes=0, invalid=False):
        self.frequency, self.band_enabled, self.selected = frequency, band_enabled, selected
        self.change_count, self.invalid = changes, invalid
        self.redraw()

    @painted()
    def redraw(self):
        self.delete('all')
        width, height = max(1, self.winfo_width()), max(1, self.winfo_height())
        color = blend(PANEL, self.color, 0.14) if self.selected else PANEL
        if self.hover and self.enabled and not self.selected:
            color = SURFACE
        rounded(self, 1, 1, width-1, height-1, 15, fill=color,
                outline=blend(EDGE, self.color, 0.70) if self.selected else blend(PANEL, EDGE, 0.22), width=1)
        self.create_text(13, 17, text=self.label, anchor='w', fill=self.color if self.band_enabled else DIM,
                         font=('Segoe UI', 12, 'bold'))
        self.create_text(width-12, 17, text=self.description, anchor='e', fill=MUTED, font=(FONT, 8))
        self.create_text(13, 44, text=self.frequency, anchor='w', fill=TEXT if self.band_enabled else DIM,
                         font=('Segoe UI', 11))
        if self.change_count:
            self.create_oval(width-29, 36, width-13, 52, fill=ERROR if self.invalid else self.color, outline='')
            self.create_text(width-21, 44, text='!' if self.invalid else str(self.change_count),
                             fill=BG, font=('Segoe UI', 9, 'bold'))
        else:
            self.create_oval(width-19, 41, width-13, 47, fill=self.color if self.band_enabled else SURFACE,
                             outline='' if self.band_enabled else DIM)
        super().configure(cursor='hand2' if self.enabled else 'arrow')


class Toggle(SmoothCanvas):
    def __init__(self, parent, variable=None):
        super().__init__(parent, bg=parent.cget('bg'), width=48, height=28,
                         bd=0, highlightthickness=0, takefocus=True)
        self.variable, self.trace_id, self.enabled = None, None, False
        self.bind('<Configure>', lambda event: self.redraw())
        self.bind('<ButtonRelease-1>', self._toggle)
        self.bind('<space>', self._toggle)
        self.set_variable(variable)

    def set_variable(self, variable):
        if self.variable is not None and self.trace_id:
            self.variable.trace_remove('write', self.trace_id)
        self.variable = variable
        self.trace_id = variable.trace_add('write', lambda *_: self.redraw()) if variable is not None else None
        self.redraw()

    def _toggle(self, event=None):
        if self.enabled and self.variable is not None:
            self.variable.set(not self.variable.get())

    def configure(self, cnf=None, **options):
        if 'state' in options:
            self.enabled = options.pop('state') != 'disabled'
        result = super().configure(cnf, **options) if cnf or options else None
        self.redraw()
        return result

    config = configure

    @painted()
    def redraw(self):
        self.delete('all')
        on = self.variable is not None and self.variable.get()
        rounded(self, 1, 3, 47, 25, 11, fill=ACCENT if on and self.enabled else EDGE, outline='')
        x = 35 if on else 13
        self.create_oval(x-8, 6, x+8, 22, fill=BG if on and self.enabled else MUTED, outline='')
        super().configure(cursor='hand2' if self.enabled else 'arrow')


class ParameterCard(SmoothCanvas):
    def __init__(self, parent, field, label, unit, minimum, maximum, step, format_value):
        super().__init__(parent, bg=parent.cget('bg'), height=122, width=210,
                         bd=0, highlightthickness=0)
        self.field, self.label, self.unit = field, label, unit
        self.minimum, self.maximum, self.step = minimum, maximum, step
        self.format_value = format_value
        self.variable, self.trace_id = None, None
        self.empty = tk.StringVar(value='—')
        self.enabled, self.supported, self.color = False, True, ACCENT
        self.error = ''
        self.on_edit_boundary = None
        self.entry = tk.Entry(self, textvariable=self.empty, bg=SURFACE, fg=TEXT, bd=0,
                              highlightthickness=0, relief='flat', insertbackground=ACCENT,
                              selectbackground=ACCENT, selectforeground=BG,
                              font=('Segoe UI', 23), justify='left', disabledbackground=SURFACE,
                              disabledforeground=DIM)
        self.entry_id = self.create_window(20, 33, anchor='nw', window=self.entry)
        self.entry.bind('<FocusIn>', self._focus_entry)
        self.entry.bind('<Button-1>', lambda event: self.entry.after_idle(self.entry.selection_range, 0, 'end'))
        self.entry.bind('<FocusOut>', self._blur_entry)
        self.entry.bind('<Return>', self._finish)
        self.entry.bind('<Up>', lambda event: self._keyboard_nudge(event, 1))
        self.entry.bind('<Down>', lambda event: self._keyboard_nudge(event, -1))
        self.entry.bind('<MouseWheel>', self._wheel)
        self.bind('<Configure>', lambda event: self.redraw())
        self.bind('<ButtonPress-1>', self._press)
        self.bind('<B1-Motion>', self._slide)
        self.bind('<ButtonRelease-1>', self._boundary)
        self.bind('<MouseWheel>', self._wheel)

    def _focus_entry(self, event):
        self.entry.after_idle(self.entry.selection_range, 0, 'end')
        self.redraw()

    def _blur_entry(self, event):
        self._boundary(event)
        self.redraw()

    def _boundary(self, event=None):
        if self.on_edit_boundary:
            self.on_edit_boundary()

    def set_error(self, message):
        self.error = message
        self.redraw()

    def _finish(self, event):
        if event.state & 4:
            return None
        value = self._value()
        if value is not None and self.minimum <= value <= self.maximum:
            self.variable.set(self.format_value(value))
            self.entry.selection_range(0, 'end')
        self._boundary()
        return 'break'

    def _keyboard_nudge(self, event, direction):
        self._boundary()
        self._nudge(self._step_change(event, direction))
        return 'break'

    def _step_change(self, event, direction):
        multiplier = 0.1 if event.state & 1 else 10 if event.state & 4 else 1
        return direction * self.step * multiplier

    def _wheel(self, event):
        if self.enabled and self.supported and event.delta:
            self._nudge(self._step_change(event, 1 if event.delta > 0 else -1))
        return 'break'

    def _nudge(self, change):
        value = self._value()
        if self.enabled and self.supported and value is not None:
            value = min(self.maximum, max(self.minimum, round(value+change, 6)))
            self.variable.set(self.format_value(value))

    def set_variable(self, variable, supported=True, color=ACCENT):
        if self.variable is not None and self.trace_id:
            self.variable.trace_remove('write', self.trace_id)
        self.variable, self.supported, self.color = variable, supported, color
        self.trace_id = variable.trace_add('write', lambda *_: self.redraw()) if variable is not None else None
        self.entry.configure(textvariable=variable if supported else self.empty)
        self.configure(state='normal' if self.enabled else 'disabled')

    def configure(self, cnf=None, **options):
        if 'state' in options:
            self.enabled = options.pop('state') != 'disabled'
            self.entry.configure(state='normal' if self.enabled and self.supported else 'disabled')
        result = super().configure(cnf, **options) if cnf or options else None
        self.redraw()
        return result

    config = configure

    def _value(self):
        try:
            return parse_parameter(self.variable.get(), self.field) if self.variable is not None else None
        except (ValueError, tk.TclError):
            return None

    def _press(self, event):
        if not self.enabled or not self.supported:
            return
        width = self.winfo_width()
        self._boundary()
        if event.y < 40 and event.x >= width-66:
            self._nudge(self._step_change(event, -1 if event.x < width-36 else 1))
        elif event.y >= self.winfo_height()-35:
            self._slide(event)

    def _slide(self, event):
        if not self.enabled or not self.supported or event.y < self.winfo_height()-54:
            return
        ratio = min(1, max(0, (event.x-20)/max(1, self.winfo_width()-40)))
        if self.field == 'frequency':
            value = self.minimum*(self.maximum/self.minimum)**ratio
            value = round(value, 1 if value < 100 else 0)
        else:
            value = round(self.minimum+(self.maximum-self.minimum)*ratio, 1)
        self.variable.set(self.format_value(value))

    @painted(tags='decor')
    def redraw(self):
        self.delete('decor')
        width, height = max(1, self.winfo_width()), max(1, self.winfo_height())
        outline = ERROR if self.error and self.supported else (
            self.color if self.enabled and self.supported and self.focus_get() == self.entry else blend(SURFACE, EDGE, 0.30))
        shape = rounded(self, 1, 1, width-1, height-1, 16, fill=SURFACE,
                        outline=outline, width=1.5, tags='decor')
        self.tag_lower(shape)
        self.entry.configure(fg=ERROR if self.error else TEXT)
        self.create_text(20, 22, text=self.label, anchor='w', fill=MUTED, font=(FONT, 10), tags='decor')
        self.itemconfigure(self.entry_id, width=max(40, width-70), height=40)
        self.create_text(width-22, 58, text=self.unit if self.supported else '', anchor='e',
                         fill=MUTED, font=('Segoe UI', 10), tags='decor')
        if not self.supported:
            self.create_text(20, height-26, text='此频段不适用', anchor='w', fill=DIM,
                             font=(FONT, 9), tags='decor')
            return
        for x, text in ((width-51, '−'), (width-23, '+')):
            draw_icon(self, 'minus' if text == '−' else 'plus', x, 22, 15,
                      MUTED if self.enabled else DIM, tags='decor')
        if self.error:
            self.create_text(20, height-26, text=self.error, anchor='w', fill=ERROR,
                             font=(FONT, 9), width=max(40, width-40), tags='decor')
            return
        y = height-26
        self.create_line(20, y, width-20, y, fill=EDGE, width=3, capstyle='round', tags='decor')
        value = self._value()
        if value is not None and math.isfinite(value):
            if self.field == 'frequency':
                ratio = math.log(max(self.minimum, value)/self.minimum)/math.log(self.maximum/self.minimum)
            else:
                ratio = (value-self.minimum)/(self.maximum-self.minimum)
            ratio = min(1, max(0, ratio))
            x = 20+(width-40)*ratio
            self.create_line(20, y, x, y, fill=self.color if self.enabled else DIM,
                             width=3, capstyle='round', tags='decor')
            self.create_oval(x-5, y-5, x+5, y+5, fill=self.color if self.enabled else DIM,
                             outline=SURFACE, width=1, tags='decor')
