"""AXON Control: independent USB editor and playback spectrum for NUX AXON 3."""
import ctypes
import sys
import os
from datetime import datetime
import json
import math
from pathlib import Path
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog
from desktop_integration import Preferences, Startup, TrayIcon, WindowIcons, set_taskbar_identity

from axon_protocol import AxonClient, BANDS, Preset, ReadCancelled, name_bytes
from editor_state import (FIELDS, FIELD_LABELS, INPUT_HINTS, EditHistory, FieldError, changed_fields,
                          display_float, editable_import, parse_parameter, preset_form, preview_from_form,
                          offline_document, read_document, rebase_preview, review_form)
from editor_session import SessionStore
from app_lifecycle import SingleInstance
from product_features import ProductFeatures
from user_data import prepare_data
from audition_state import AuditionPair
from curve_view import DRAG_MODES, PLOT_RANGES, PlotTransform, ResponseCache, band_response, drag_values
from curve_reference import REFERENCE_CHOICES, available_references, resolve_reference
from band_clipboard import copy_snippet, paste_snippet
from smooth_render import SmoothCanvas, blend, painted
from ui_runtime import UiRuntime
from audio_view import AudioPanel
from ui_widgets import (BG, PANEL, SURFACE, EDGE, TEXT, MUTED, DIM, ACCENT,
                        Panel, Button, Popup, ReviewPopup, PresetPicker, BandTile, Toggle, ParameterCard, ActionDialog)

HERE = Path(__file__).resolve().parent
DATA = None
BINARY = Path(sys.executable).parent if getattr(sys, 'frozen', False) else HERE
WARNING = '#e6c28a'
COLORS = ('#93aaa0', ACCENT, '#81c9c8', '#b7a0dd', '#e4ba8c', '#e7a6b7', '#9cacbd')
DESCRIPTIONS = ('高通', '低频', '中低频', '中频', '中高频', '高频', '低通')


class App(ProductFeatures):
    def __init__(self, root, runtime=None):
        self.root = root
        self.runtime = runtime
        self.data_dir = DATA or HERE
        self.desktop = Preferences(self.data_dir)
        self.startup = Startup(BINARY)
        self.tray = None
        self.window_icons = None
        self.client = AxonClient(getattr(self, 'data_dir', HERE) / 'backups')
        self.session_store = SessionStore(getattr(self, 'data_dir', HERE) / 'last-session.json')
        self.recovery_store = SessionStore(getattr(self, 'data_dir', HERE) / 'recovery-session.json')
        self.autosave_signature = None
        self.background_audio_resume = False
        self.settings_window = None
        self.results = queue.Queue()
        self.connected = False
        self.offline = False
        self.busy = False
        self.cancellable = False
        self.needs_readback = False
        self.closing = False
        self.suppress = False
        self.snapshot = None
        self.current = None
        self.review = None
        self.active_review = None
        self.audition = None
        self.slot = 0
        self.editor_controls = []
        self.status = tk.StringVar(value='用 USB 连接音箱后开始。请先关闭 AXON STUDIO。')
        self.device_label = tk.StringVar(value='未连接')
        self.eq_label = tk.StringVar(value='')
        self.edit_label = tk.StringVar(value='')
        self.preset_label = tk.StringVar()
        self.values = []
        self.history = EditHistory()
        self.compare_saved = False
        self.plot_reference = 'current'
        self.plot_reference_view = None
        self.curve_cache = ResponseCache()
        self.plot_gain_range = 12
        self.plot_drag_mode = 'free'
        self.plot_transform = None
        self.plot_preset = None
        self.plot_pointer = None
        self.plot_bypassed = False
        self.selected_band = 1
        self.drag_index = None
        self.drag_origin = None
        self.drag_last = None
        self.drag_fine = False
        self.drag_active_mode = None
        self.plot_bounds = None
        self.plot_nodes = []
        self.window_maximized = False
        self.normal_geometry = None
        self.native_hwnd = None
        self.band_title = tk.StringVar(value='LF')
        self.band_description = tk.StringVar(value='低频')
        self.band_hint = tk.StringVar(value='LF · 低频')
        self.curve_note = tk.StringVar(value='虚线：已保存  ·  曲线为估算')
        self.graph_hint = tk.StringVar(value='拖动调节 · Shift 精细 · 节点滚轮调 Q')
        self._style()
        self._build()
        self._bind_shortcuts()
        self._controls()
        self.root.protocol('WM_DELETE_WINDOW', self.close)
        self.root.after(60, self._poll)
        self.root.after(2000, self._autosave)
        self.root.after(300, self._background_tick)
        self.init_features()

    def _style(self):
        self.root.title('AXON Control 0.20 · AXON 3')
        self.root.configure(bg=BG)
        self.root.overrideredirect(True)
        width = min(1120, self.root.winfo_screenwidth()-96)
        height = min(850, self.root.winfo_screenheight()-96)
        x = max(0, (self.root.winfo_screenwidth()-width)//2)
        y = max(0, (self.root.winfo_screenheight()-height-32)//2)
        self.root.geometry(f'{width}x{height}+{x}+{y}')
        self.root.minsize(980, 740)
        self.root.option_add('*Font', ('Microsoft YaHei UI', 10))
        self.app_icon = tk.PhotoImage(file=str(HERE / 'axon-icon.png'))
        self.root.iconphoto(True, self.app_icon)
        self.root.iconbitmap(str(HERE / 'axon-icon.ico'))
        self.root.bind('<Alt-F4>', lambda event: self.close())
        self.root.bind('<Map>', lambda event: self.root.after(10, self._window_theme)
                       if event.widget == self.root else None)
        self.root.after(100, self._window_theme)

    def _window_theme(self):
        # Microsoft DWM attributes, applied only to this application's window.
        # https://learn.microsoft.com/windows/win32/api/dwmapi/ne-dwmapi-dwmwindowattribute
        try:
            user32 = ctypes.WinDLL('user32')
            user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            user32.GetAncestor.restype = ctypes.c_void_p
            hwnd = user32.GetAncestor(self.root.winfo_id(), 2) or self.root.winfo_id()
            self.native_hwnd = hwnd
            if self.window_icons is None:
                self.window_icons = WindowIcons(HERE / 'axon-icon.ico')
            self.window_icons.apply(hwnd)
            user32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
            user32.GetWindowLongW.restype = ctypes.c_long
            user32.SetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_long]
            user32.SetWindowLongW.restype = ctypes.c_long
            extended = user32.GetWindowLongW(hwnd, -20)
            user32.SetWindowLongW(hwnd, -20, (extended | 0x40000) & ~0x80)
            dwm = ctypes.WinDLL('dwmapi')
            dwm.DwmSetWindowAttribute.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                                 ctypes.c_void_p, ctypes.c_uint]
            color = int(BG[1:3], 16) | (int(BG[3:5], 16)<<8) | (int(BG[5:7], 16)<<16)
            for attribute, value in ((20, 1), (33, 2), (34, color), (35, color)):
                setting = ctypes.c_uint(value)
                dwm.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(setting), ctypes.sizeof(setting))
        except (AttributeError, OSError):
            pass

    def _move_start(self, event):
        if not self.window_maximized:
            self.move_origin = (event.x_root, event.y_root, self.root.winfo_x(), self.root.winfo_y())

    def _move_window(self, event):
        if self.window_maximized or not hasattr(self, 'move_origin'):
            return
        x, y, window_x, window_y = self.move_origin
        self.root.geometry(f'+{max(0, window_x+event.x_root-x)}+{max(0, window_y+event.y_root-y)}')

    def _minimize(self):
        if self.desktop.minimize_to_tray:
            self._hide_to_tray()
            return
        self._minimize_taskbar()

    def _hide_to_tray(self):
        if self.root.grab_current() is not None:
            return
        try:
            if self.tray is None:
                self.tray = TrayIcon(self.root, HERE / 'axon-icon.ico', self._restore_window, self.close)
            self.tray.show()
        except (OSError, AttributeError) as error:
            self.status.set(f'无法最小化到托盘：{error}')
            return
        self.status.set('已恢复窗口；托盘期间的编辑内容已保留。')
        self.root.withdraw()

    def _restore_window(self):
        self.root.deiconify()
        self._window_theme()
        if self.native_hwnd:
            user32 = ctypes.WinDLL('user32')
            user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
            user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
            user32.ShowWindow(self.native_hwnd, 9)
            user32.SetForegroundWindow(self.native_hwnd)
        self.root.lift()
        self.root.focus_force()
        if self.tray:
            self.tray.hide()

    def _toggle_startup(self):
        try:
            enabled = not self.startup.enabled()
            self.startup.set_enabled(enabled)
            self.status.set('已开启开机自启：登录 Windows 后启动到托盘。' if enabled else '已关闭开机自启。')
        except OSError as error:
            self.status.set(f'自启设置未保存：{error}')

    def _toggle_minimize_to_tray(self):
        try:
            self.desktop.set_minimize(not self.desktop.minimize_to_tray)
            self.status.set('最小化时收起到托盘；点击托盘图标恢复，右键可退出。'
                            if self.desktop.minimize_to_tray else '最小化时保留在任务栏。')
        except OSError as error:
            self.status.set(f'托盘设置未保存：{error}')

    def _minimize_taskbar(self):
        if self.native_hwnd:
            user32 = ctypes.WinDLL('user32')
            user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
            user32.ShowWindow(self.native_hwnd, 6)

    def _maximize(self):
        if self.window_maximized:
            self.root.geometry(self.normal_geometry)
            self.window_maximized = False
        else:
            self.normal_geometry = self.root.geometry()
            class Rect(ctypes.Structure):
                _fields_ = [('left', ctypes.c_long), ('top', ctypes.c_long),
                            ('right', ctypes.c_long), ('bottom', ctypes.c_long)]
            area = Rect()
            ok = ctypes.windll.user32.SystemParametersInfoW(0x30, 0, ctypes.byref(area), 0)
            if ok:
                self.root.geometry(f'{area.right-area.left}x{area.bottom-area.top}+{area.left}+{area.top}')
            else:
                self.root.geometry(f'{self.root.winfo_screenwidth()}x{self.root.winfo_screenheight()-48}+0+0')
            self.window_maximized = True

    def _resize_start(self, event):
        self.resize_origin = (event.x_root, event.y_root, self.root.winfo_width(), self.root.winfo_height())

    def _resize_window(self, event):
        if self.window_maximized or not hasattr(self, 'resize_origin'):
            return
        x, y, width, height = self.resize_origin
        self.root.geometry(f'{max(980, width+event.x_root-x)}x{max(740, height+event.y_root-y)}')

    def _label(self, parent, text='', **options):
        options.setdefault('fg', TEXT)
        return tk.Label(parent, text=text, bg=parent.cget('bg'), **options)

    def _bind_shortcuts(self):
        commands = {'<Control-z>': self.undo, '<Control-y>': self.redo,
                    '<Control-Shift-Z>': self.redo, '<Control-Return>': self.apply,
                    '<Control-s>': self.save, '<Control-Shift-R>': self.restore_band,
                    '<F6>': self.toggle_audition, '<Control-Shift-L>': self.review_changes,
                    '<Control-Shift-C>': self.copy_band, '<Control-Shift-V>': self.paste_band,
                    '<Control-Shift-G>': self.zero_gain, '<F7>': self.cycle_reference,
                    '<Control-Prior>': lambda: self.step_preset(-1),
                    '<Control-Next>': lambda: self.step_preset(1), '<F5>': self.read}
        def run(command):
            if ((self.connected or self.offline) and not self.busy and not self.closing
                    and not isinstance(self.root.grab_current(), ActionDialog)):
                command()
            return 'break'
        for key, command in commands.items():
            self.root.bind(key, lambda event, action=command: run(action))
        # Handle these before Entry's class bindings can copy or paste raw text.
        for card in self.cards.values():
            for key in ('<Control-Shift-C>', '<Control-Shift-V>', '<Control-Shift-G>'):
                card.entry.bind(key, lambda event, action=commands[key]: run(action))
        for index in range(7):
            self.root.bind(f'<Alt-Key-{index+1}>',
                           lambda event, i=index: run(lambda: self.choose_band(i)))

    def _build(self):
        for index in range(7):
            variables = {'enabled': tk.BooleanVar(), 'frequency': tk.StringVar(value='—'),
                         'q': tk.StringVar(value='—'), 'gain': tk.StringVar(value='—')}
            self.values.append(variables)
            for field, variable in variables.items():
                variable.trace_add('write', lambda *_, key=(index, field): self._changed(key))

        chrome = tk.Frame(self.root, bg=BG, height=32)
        chrome.pack(fill='x')
        chrome.pack_propagate(False)
        chrome_caption = self._label(chrome, 'AXON Control', fg=DIM, font=('Segoe UI', 9))
        chrome_caption.pack(side='left', padx=28)
        for surface in (chrome, chrome_caption):
            surface.bind('<ButtonPress-1>', self._move_start)
            surface.bind('<B1-Motion>', self._move_window)
            surface.bind('<Double-Button-1>', lambda event: self._maximize())
        for name, command in (('close', self.close), ('maximize', self._maximize), ('minimize', self._minimize)):
            Button(chrome, '', command, icon_name=name, width=42, height=30, variant='ghost',
                   font=('Segoe UI', 14)).pack(side='right', padx=(0, 4))
        self.resize_grip = SmoothCanvas(self.root, bg=BG, width=18, height=18,
                                     highlightthickness=0, cursor='size_nw_se')
        def draw_grip(event=None):
            with self.resize_grip.paint():
                self.resize_grip.delete('all')
                for offset in (5, 10, 15):
                    self.resize_grip.create_line(offset, 16, 16, offset, fill=DIM, width=1)
        self.resize_grip.bind('<Configure>', draw_grip)
        self.resize_grip.place(relx=1, rely=1, anchor='se')
        self.resize_grip.bind('<ButtonPress-1>', self._resize_start)
        self.resize_grip.bind('<B1-Motion>', self._resize_window)

        shell = tk.Frame(self.root, bg=BG)
        shell.pack(fill='both', expand=True, padx=24, pady=(8, 12))
        header = tk.Frame(shell, bg=BG)
        header.pack(fill='x')
        mark = SmoothCanvas(header, width=31, height=36, bg=BG, highlightthickness=0)
        mark.pack(side='left', padx=(0, 9))
        def draw_mark(event=None):
            with mark.paint():
                mark.delete('all')
                for x, size in ((4, 12), (12, 24), (20, 17), (28, 28)):
                    mark.create_line(x, 18-size/2, x, 18+size/2, fill=ACCENT, width=2.5, capstyle='round')
        mark.bind('<Configure>', draw_mark)
        brand = tk.Frame(header, bg=BG)
        brand.pack(side='left')
        self._label(brand, 'AXON', font=('Segoe UI', 18, 'bold')).pack(side='left')
        self._label(brand, 'CONTROL', fg=DIM, font=('Segoe UI', 10)).pack(side='left', padx=(9, 0), pady=(4, 0))
        self.more_button = Button(header, '', self.more, icon_name='more', width=34, height=36,
                                  variant='ghost', font=('Segoe UI', 18))
        self.more_button.pack(side='right')
        self.connect_button = Button(header, '连接音箱', self.connect, width=100, height=36,
                                     font=('Microsoft YaHei UI', 9))
        self.connect_button.pack(side='right', padx=(12, 6))
        self._label(header, textvariable=self.device_label, fg=ACCENT,
                    font=('Microsoft YaHei UI', 9)).pack(side='right', padx=(16, 0))

        preset_controls = tk.Frame(header, bg=BG)
        preset_controls.pack(side='right')
        self._label(preset_controls, '预设', fg=DIM, font=('Microsoft YaHei UI', 9)).pack(
            side='left', padx=(0, 8))
        self.previous_button = Button(preset_controls, '‹', lambda: self.step_preset(-1),
                                      width=26, height=36, variant='ghost', font=('Segoe UI', 18))
        self.previous_button.pack(side='left', padx=(0, 2))
        self.combo = PresetPicker(preset_controls, self.preset_label, width=218, height=36,
                                  font=('Microsoft YaHei UI', 9))
        self.combo.pack(side='left')
        self.combo.bind('<<ComboboxSelected>>', self.select)
        self.next_button = Button(preset_controls, '›', lambda: self.step_preset(1),
                                  width=26, height=36, variant='ghost', font=('Segoe UI', 18))
        self.next_button.pack(side='left', padx=(2, 0))

        toolbar = tk.Frame(shell, bg=BG)
        toolbar.pack(fill='x', pady=(10, 12))
        self._label(toolbar, '音色编辑', font=('Microsoft YaHei UI', 14, 'bold')).pack(side='left')
        self.audition_buttons = {}
        audition_buttons = tk.Frame(toolbar, bg=BG)
        audition_buttons.pack(side='left', padx=(20, 10))
        for side, caption in (('A', 'A 调节前'), ('B', 'B 调节后')):
            button = Button(audition_buttons, caption, lambda choice=side: self.listen(choice),
                            width=84, height=30, font=('Microsoft YaHei UI', 9))
            button.pack(side='left', padx=(0, 4))
            self.audition_buttons[side] = button
        self.compare_button = Button(toolbar, '对照：读回 ▾', self.reference_menu,
                                      width=128, height=30, variant='ghost', font=('Microsoft YaHei UI', 9))
        self.compare_button.pack(side='left')
        self.review_button = Button(toolbar, '改动清单', self.review_changes, width=104,
                                     height=30, variant='ghost', font=('Microsoft YaHei UI', 9))
        self.review_button.pack(side='left', padx=(6, 0))
        self.read_button = Button(toolbar, '', self.read, icon_name='refresh', width=34, height=30,
                                  variant='ghost')
        self.read_button.pack(side='right')
        self.audio_button = Button(toolbar, '收起频谱', self.toggle_audio_view,
                                   width=102, height=30, variant='ghost', font=('Microsoft YaHei UI', 9))
        self.audio_button.pack(side='right', padx=(0, 8))

        self.graph_content = tk.Frame(shell, bg=BG)
        self.graph_content.rowconfigure(0, weight=1)
        self.graph_content.columnconfigure(0, weight=5, uniform='plots')
        self.graph_content.columnconfigure(1, weight=3, uniform='plots')
        graph = self.eq_panel = Panel(self.graph_content, height=270, padding=16, radius=18)
        graph.grid(row=0, column=0, sticky='nsew', padx=(0, 6))
        self.audio_card = Panel(self.graph_content, height=270, padding=16, radius=18)
        self.audio_card.grid(row=0, column=1, sticky='nsew', padx=(6, 0))
        graph_header = tk.Frame(graph.body, bg=PANEL)
        graph_header.pack(fill='x')
        self._label(graph_header, '响应曲线', font=('Microsoft YaHei UI', 11, 'bold')).pack(side='left')
        self.response_label = self._label(graph_header, '·  总响应', fg=MUTED,
                                           font=('Microsoft YaHei UI', 9))
        self.response_label.pack(side='left', padx=10)
        self.eq_button = Button(graph_header, 'EQ 开启', self.toggle_eq, width=88, height=28,
                                variant='primary', font=('Microsoft YaHei UI', 9))
        self.eq_button.pack(side='right')
        graph_meta = tk.Frame(graph.body, bg=PANEL)
        graph_meta.pack(fill='x', pady=(3, 6))
        self.selected_hint = self._label(graph_meta, textvariable=self.band_hint, fg=COLORS[1],
                                         font=('Microsoft YaHei UI', 9))
        self.selected_hint.pack(side='left')
        self._label(graph_meta, textvariable=self.curve_note, fg=DIM,
                    font=('Microsoft YaHei UI', 8)).pack(side='right')
        self.canvas = SmoothCanvas(graph.body, bg=PANEL, bd=0, highlightthickness=0)
        self.audio_panel = AudioPanel(self.audio_card.body, runtime=self.runtime)
        self.audio_panel.pack(fill='both', expand=True)
        self.canvas.bind('<Configure>', lambda event: self.draw())
        self.canvas.bind('<ButtonPress-1>', self._graph_press)
        self.canvas.bind('<B1-Motion>', self._graph_drag)
        self.canvas.bind('<ButtonRelease-1>', self._graph_release)
        self.canvas.bind('<Motion>', self._graph_motion)
        self.canvas.bind('<Leave>', self._graph_leave)
        self.canvas.bind('<MouseWheel>', self._graph_wheel)
        graph_hint = tk.Frame(graph.body, bg=PANEL)
        graph_hint.pack(side='bottom', fill='x', pady=(6, 0))
        self.canvas.pack(fill='both', expand=True)
        self.plot_range_button = Button(graph_hint, '纵轴 ±12 dB ▾', self.plot_range_menu,
                                        width=116, height=26, variant='ghost', font=('Microsoft YaHei UI', 9))
        self.plot_range_button.pack(side='right')
        self.drag_mode_button = Button(graph_hint, '拖动 · 自由 ▾', self.drag_mode_menu,
                                       width=108, height=26, variant='ghost', font=('Microsoft YaHei UI', 9))
        self.drag_mode_button.pack(side='right', padx=(0, 4))
        self._label(graph_hint, textvariable=self.graph_hint, fg=MUTED, anchor='w',
                    font=('Microsoft YaHei UI', 8)).pack(side='left', fill='x', expand=True)

        bands = tk.Frame(shell, bg=BG)
        self.band_tiles = []
        for index, label in enumerate(BANDS):
            bands.columnconfigure(index, weight=1, uniform='bands')
            tile = BandTile(bands, label, DESCRIPTIONS[index], COLORS[index],
                            lambda i=index: self.choose_band(i))
            tile.grid(row=0, column=index, sticky='nsew', padx=(0 if index == 0 else 4,
                                                              0 if index == 6 else 4))
            self.band_tiles.append(tile)

        detail = tk.Frame(shell, bg=BG)
        identity_card = Panel(detail, color=SURFACE, width=156, height=122, radius=16, padding=12)
        identity_card.pack(side='left', fill='y', padx=(0, 12))
        identity = identity_card.body
        self.band_name_label = self._label(identity, textvariable=self.band_title,
                                           fg=COLORS[1], font=('Segoe UI', 19, 'bold'), pady=0)
        self.band_name_label.pack(anchor='w')
        self._label(identity, textvariable=self.band_description, fg=MUTED,
                    font=('Microsoft YaHei UI', 8), pady=0).pack(anchor='w')
        toggle_row = tk.Frame(identity, bg=SURFACE)
        toggle_row.pack(anchor='w')
        self.enable_switch = Toggle(toggle_row, self.values[1]['enabled'])
        self.enable_switch.pack(side='left')
        self._label(toggle_row, '启用', fg=MUTED, font=('Microsoft YaHei UI', 8)).pack(
            side='left', padx=6)
        band_actions = tk.Frame(identity, bg=SURFACE)
        band_actions.pack(anchor='w')
        self.copy_band_button = Button(band_actions, '复制', self.copy_band, width=38, height=24,
                                       variant='ghost', font=('Microsoft YaHei UI', 9))
        self.paste_band_button = Button(band_actions, '粘贴', self.paste_band, width=38, height=24,
                                        variant='ghost', font=('Microsoft YaHei UI', 9))
        self.zero_gain_button = Button(band_actions, '0 dB', self.zero_gain, width=42, height=24,
                                       variant='ghost', font=('Microsoft YaHei UI', 9))
        for column, button in enumerate((self.copy_band_button, self.paste_band_button, self.zero_gain_button)):
            button.pack(side='left', padx=(0, 2 if column < 2 else 0))
        parameters = tk.Frame(detail, bg=BG)
        parameters.pack(side='left', fill='both', expand=True)
        self.cards = {}
        specs = (('frequency', '频率', 'Hz', 20, 20000, 10),
                 ('q', 'Q 值', 'Q', 0.1, 10, 0.1), ('gain', '增益', 'dB', -12, 12, 0.1))
        for col, (field, label, unit, minimum, maximum, step) in enumerate(specs):
            parameters.columnconfigure(col, weight=1, uniform='parameters')
            card = ParameterCard(parameters, field, label, unit, minimum, maximum, step, display_float)
            card.on_edit_boundary = self.history.end_group
            card.grid(row=0, column=col, sticky='nsew', padx=(0 if col == 0 else 6,
                                                          0 if col == 2 else 6))
            self.cards[field] = card
        self.editor_controls = list(self.cards.values()) + [self.enable_switch] + self.band_tiles

        footer = tk.Frame(shell, bg=BG)
        tk.Frame(footer, bg=blend(BG, EDGE, 0.5), height=1).pack(fill='x', pady=(0, 10))
        actions = tk.Frame(footer, bg=BG)
        actions.pack(fill='x')
        self.save_button = Button(actions, '保存预设', self.save, width=128, height=40)
        self.apply_button = Button(actions, '应用试听', self.apply, width=128, height=40, variant='primary')
        self.save_button.pack(side='right')
        self.apply_button.pack(side='right', padx=(0, 10))
        self.undo_button = Button(actions, '撤销', self.undo, icon_name='undo', width=68, height=40, variant='ghost')
        self.redo_button = Button(actions, '重做', self.redo, icon_name='redo', width=68, height=40, variant='ghost')
        self.restore_button = Button(actions, '恢复已保存', self.restore, width=112, height=40, variant='ghost')
        self.undo_button.pack(side='left')
        self.redo_button.pack(side='left', padx=(3, 0))
        self.restore_button.pack(side='left', padx=(5, 14))
        self.edit_status_label = self._label(actions, textvariable=self.edit_label, fg=ACCENT,
                                              font=('Microsoft YaHei UI', 9))
        self.edit_status_label.pack(side='left')
        information = tk.Frame(footer, bg=BG)
        information.pack(fill='x', pady=(8, 0))
        self._label(information, textvariable=self.status, fg=MUTED, anchor='w',
                    font=('Microsoft YaHei UI', 9)).pack(side='left', fill='x', expand=True)
        self._label(information, 'F6 A/B  ·  Ctrl+Enter 应用  ·  Ctrl+S 保存', fg=DIM,
                    font=('Microsoft YaHei UI', 9)).pack(side='right', padx=(10, 0))
        self.buttons = (self.read_button, self.apply_button, self.save_button, self.restore_button,
                        self.undo_button, self.redo_button, self.more_button, self.compare_button,
                        self.review_button, self.plot_range_button, self.drag_mode_button,
                        self.copy_band_button, self.paste_band_button, self.zero_gain_button)
        footer.pack(side='bottom', fill='x', pady=(12, 0))
        detail.pack(side='bottom', fill='x', pady=(10, 0))
        bands.pack(side='bottom', fill='x', pady=(10, 0))
        self.graph_content.pack(fill='both', expand=True)
        self.choose_band(1)

    def _controls(self):
        device_ready = self.connected and not self.busy and not self.closing and not getattr(self, 'needs_readback', False)
        usable = (self.connected or self.offline) and not self.busy and not self.closing
        can_cancel = self.busy and getattr(self, 'cancellable', False)
        self.connect_button.configure(state='normal' if can_cancel else 'disabled' if self.busy or self.closing else 'normal',
                                      text='取消读取' if can_cancel else '断开连接' if self.connected else '连接音箱')
        self.combo.configure(state='normal' if usable and (self.offline or device_ready) else 'disabled')
        for control in self.buttons + tuple(self.editor_controls):
            control.configure(state='normal' if usable else 'disabled')
        valid, pending, preview = True, False, None
        if self.current:
            try:
                preview = self.target()
                pending = preview.raw != self.current.raw
            except ValueError:
                valid = False
        unsaved = bool(self.current and self.snapshot and
                       self.current.raw.hex() != self.snapshot['presets'][self.slot]['raw_hex'])
        count = len(changed_fields(self.current, preview))
        self.apply_button.configure(state='normal' if device_ready and valid and pending else 'disabled',
                                    text=f'{"应用 B" if self.audition and self.audition.side == "A" else "应用试听"} · {count}'
                                    if pending else '应用试听')
        self.save_button.variant = 'primary' if self.offline or (unsaved and not pending) else 'secondary'
        can_save = usable and valid if self.offline else device_ready and valid and not pending and unsaved
        if self.audition and self.audition.side == 'A':
            can_save = False
        self.save_button.configure(state='normal' if can_save else 'disabled',
                                   text='导出草稿' if self.offline else '保存预设')
        self.undo_button.configure(state='normal' if usable and self.history.can_undo else 'disabled')
        self.redo_button.configure(state='normal' if usable and self.history.can_redo else 'disabled')
        self.read_button.configure(state='normal' if self.connected and not self.busy and not self.closing else 'disabled')
        self.restore_button.configure(state='normal' if device_ready and unsaved else 'disabled')
        if hasattr(self, 'eq_button'):
            enabled = bool(self.snapshot and self.snapshot['eq_enabled'])
            self.eq_button.variant = 'primary' if enabled else 'secondary'
            self.eq_button.configure(state='normal' if device_ready else 'disabled',
                                     text='EQ 开启' if enabled else 'EQ 旁通')
            for button in (self.previous_button, self.next_button):
                button.configure(state='normal' if usable and (self.offline or device_ready) else 'disabled')
        self.more_button.configure(state='disabled' if self.busy or self.closing else 'normal')
        self.plot_range_button.configure(state='disabled' if self.busy or self.closing else 'normal')
        self.drag_mode_button.configure(state='disabled' if self.busy or self.closing else 'normal')
        reference = self._reference()
        if self.current is not None:
            self.plot_reference = reference.key
            self.compare_saved = reference.key == 'saved'
        self.compare_button.configure(state='normal' if usable and self.current else 'disabled',
                                      text=f'对照：{reference.label} ▾')
        band_error = bool(self.review and any(item.index == self.selected_band and item.error
                                             for item in self.review.items))
        self.copy_band_button.configure(state='normal' if usable and self.current and not band_error else 'disabled')
        self.paste_band_button.configure(state='normal' if usable and self.current else 'disabled')
        self.zero_gain_button.configure(state='normal' if usable and self.current and self.selected_band not in (0, 6)
                                        else 'disabled')
        errors = self.review.error_count if self.review else 0
        self.review_button.variant = 'warning' if errors else 'ghost'
        self.review_button.configure(state='normal' if usable and self.review and self.review.items else 'disabled',
                                     text=f'需修正 · {errors}' if errors else
                                     f'改动 · {len(self.review.items)} ▾' if self.review and self.review.items else '改动清单')
        can_compare = bool(device_ready and self.audition and self.snapshot and self.snapshot['eq_enabled'])
        for side, button in self.audition_buttons.items():
            button.variant = 'primary' if self.audition and self.audition.side == side else 'secondary'
            button.configure(state='normal' if can_compare else 'disabled')
        hearing_a = self.audition and self.audition.side == 'A'
        self.response_label.configure(text='·  B 预览' if hearing_a else '·  总响应')
        self.curve_note.set(('试听 A · ' if hearing_a else '') +
                            (f'虚线：{reference.label} · ' if reference.preset is not None else '') + '曲线为估算')

    def choose_band(self, index):
        self.history.end_group()
        self.selected_band = index
        self.band_title.set(BANDS[index])
        self.band_description.set(DESCRIPTIONS[index] + (' · 截止滤波' if index in (0, 6) else ' · 峰值 EQ'))
        self.band_hint.set(f'{BANDS[index]} · {DESCRIPTIONS[index]}')
        self.band_name_label.configure(fg=COLORS[index])
        self.selected_hint.configure(fg=COLORS[index])
        self.enable_switch.set_variable(self.values[index]['enabled'])
        for field, card in self.cards.items():
            card.set_variable(self.values[index][field], index not in (0, 6) or field == 'frequency', COLORS[index])
        self._changed()

    def more(self):
        usable = (self.connected or self.offline) and not self.busy
        device_ready = self.connected and not self.busy and not getattr(self, 'needs_readback', False)
        items = [('设置…', self.settings, True, False),
                 ('本地预设库…', self.preset_library, True, False),
                 ('离线打开最近备份', self.load_recent, not self.connected and not self.busy, False),
                 ('载入备份或草稿…', self.load_backup, not self.busy, False),
                 ('导出本地草稿', self.export_draft, usable, False),
                 ('手动备份音箱全部参数', self.backup, device_ready, False),
                 ('取回本频段保存值   Ctrl+Shift+R', self.restore_band, usable, False),
                 ('清空本地输入', self.discard, usable, False),
                 ('恢复整份保存预设', self.restore, device_ready, False),
                 ('重命名当前预设…', self.rename_preset, device_ready, False),
                 ('设备信息', self.device_info, self.snapshot is not None, False),
                 ('重置当前预设…', lambda: self.reset_presets(False), device_ready, False),
                 ('重置音箱全部预设…', lambda: self.reset_presets(True), device_ready, False)]
        Popup(self.more_button, items, self.more_button.winfo_rootx()-220,
              self.more_button.winfo_rooty()+self.more_button.winfo_height()+8)

    def settings(self):
        if self.settings_window and self.settings_window.winfo_exists():
            self.settings_window.lift()
            return
        window = self.settings_window = tk.Toplevel(self.root)
        window.title('AXON Control · 设置')
        window.configure(bg=PANEL)
        window.resizable(False, False)
        window.transient(self.root)
        window.iconbitmap(str(HERE / 'axon-icon.ico'))
        body = tk.Frame(window, bg=PANEL, padx=24, pady=20)
        body.pack(fill='both', expand=True)
        tk.Label(body, text='启动与后台运行', bg=PANEL, fg=TEXT,
                 font=('Microsoft YaHei UI', 14, 'bold')).pack(anchor='w', pady=(0, 16))
        startup = tk.BooleanVar(value=self.startup.enabled())
        minimize = tk.BooleanVar(value=self.desktop.minimize_to_tray)
        pause = tk.BooleanVar(value=self.desktop.pause_in_background)
        auto_connect = tk.BooleanVar(value=self.desktop.auto_connect)
        reconnect = tk.BooleanVar(value=self.desktop.auto_reconnect)
        for caption, variable in (('登录 Windows 后启动到托盘', startup),
                                  ('最小化到托盘', minimize),
                                  ('最小化或进入托盘时暂停频谱', pause),
                                  ('启动时自动连接音箱（只读核对）', auto_connect),
                                  ('USB 重插或休眠后自动重连', reconnect)):
            tk.Checkbutton(body, text=caption, variable=variable, bg=PANEL, fg=TEXT,
                           selectcolor=SURFACE, activebackground=PANEL, activeforeground=TEXT,
                           font=('Microsoft YaHei UI', 10)).pack(anchor='w', pady=5)
        tk.Label(body, text='后台暂停后，恢复窗口会继续频谱；手动暂停的频谱保持暂停。\n'
                 '草稿每 2 秒检查并自动保存；异常退出后会离线恢复。\n'
                 '关闭窗口仍退出程序，自动保存不会写入音箱。', bg=PANEL, fg=MUTED,
                 justify='left', font=('Microsoft YaHei UI', 9)).pack(anchor='w', pady=16)
        error = tk.StringVar()
        tk.Label(body, textvariable=error, bg=PANEL, fg=WARNING, wraplength=390).pack(anchor='w')
        def save():
            try:
                self.desktop.save(minimize.get(), pause.get(), auto_connect=auto_connect.get(), auto_reconnect=reconnect.get())
                if startup.get() != self.startup.enabled():
                    self.startup.set_enabled(startup.get())
            except OSError as exc:
                error.set(f'部分设置未保存：{exc}；请修正后重试。')
                return
            self.status.set('设置已保存。')
            window.destroy()
        Button(body, '打开数据目录', lambda: os.startfile(self.data_dir), width=130).pack(anchor='w')
        Button(body, '保存设置', save, variant='primary', width=120).pack(anchor='e', pady=(8, 0))

    def _background_tick(self):
        if self.closing:
            self.root.after(300, self._background_tick)
            return
        hidden = self.root.state() in ('withdrawn', 'iconic')
        if self.native_hwnd:
            user = ctypes.WinDLL('user32')
            user.IsIconic.argtypes = [ctypes.c_void_p]
            hidden = hidden or bool(user.IsIconic(self.native_hwnd))
        panel = self.audio_panel
        if hidden and self.desktop.pause_in_background:
            if panel.start_timer is not None:
                panel.after_cancel(panel.start_timer)
                panel.start_timer = None
                self.background_audio_resume = True
            if panel.running:
                self.background_audio_resume = True
                panel.toggle()
        elif self.background_audio_resume:
            self.background_audio_resume = False
            if panel.visible and not panel.running:
                panel.start()
        self.root.after(300, self._background_tick)

    def _autosave(self):
        try:
            if self.current is not None and not self.busy and not self.closing:
                signature = json.dumps([self.snapshot, self._form(), self.selected_band,
                                        self.plot_gain_range, self.plot_drag_mode, self.plot_reference],
                                       sort_keys=True)
                if signature != self.autosave_signature:
                    self._write_session(self.recovery_store)
                    self.autosave_signature = signature
        except (ValueError, OSError) as error:
            self.status.set(f'草稿自动保存失败：{error}')
        finally:
            self.root.after(2000, self._autosave)

    def _device_action_ready(self):
        return (self.connected and not self.busy and not self.closing
                and not getattr(self, 'needs_readback', False) and self.current is not None)

    def toggle_eq(self):
        if not self._device_action_ready():
            return
        enabled, slot, base = self.snapshot['eq_enabled'], self.slot, self.current
        def done(result):
            state, _ = result
            self.snapshot['eq_enabled'], self.snapshot['state_hex'] = bool(state[1]), state.hex()
            self.eq_label.set('EQ 已启用' if state[1] else 'EQ 已旁通')
            self._changed(record=False)
            self.status.set('EQ 已启用，参数与本地输入保留。' if state[1] else 'EQ 已旁通，可对比未经过 EQ 的声音。')
        self._run('正在切换 EQ 并读回…',
                  lambda: self.client.set_eq(not enabled, expected_slot=slot, expected=base, expected_enabled=enabled), done)

    def device_info(self):
        if not self.snapshot:
            return
        device = self.snapshot['device']
        version = device.get('version_field', '未读取')
        fm = device.get('fm_version_raw')
        text = (f'型号  {device.get("model", "未读取")}\nUSB  {device.get("name", "NUX AXON-3")}\n'
                f'版本字段  {version}\nfmVersion 原始值  {f"0x{fm:02X}" if type(fm) is int else "未读取"}\n\n'
                '版本字段按设备原值显示，实际固件版本含义尚未确认。')
        ActionDialog(self.root, '设备信息', text, lambda _: None, confirm='知道了')

    def rename_preset(self):
        if not self._device_action_ready():
            return
        try:
            if self._pending():
                raise ValueError('请先应用或清空本地输入，再重命名预设。')
        except ValueError as error:
            self.status.set(str(error))
            return
        slot, base = self.slot, self.current
        def renamed(name):
            def done(snapshot):
                self._snapshot(snapshot)
                self.status.set('名称已读回确认；工作参数与保存参数均保留，未额外执行保存。')
            self._run('正在备份、改名并读回…', lambda: self.client.rename(name, expected_slot=slot, expected=base), done)
        ActionDialog(self.root, '重命名当前预设', f'{slot+1:02d}  {base.name}\n修改前自动备份，参数值保持原样。', renamed,
                     confirm='重命名', initial=base.name, validate=name_bytes,
                     hint='1–14 个英文字符 · 支持数字、空格和英文符号')

    def reset_presets(self, all_presets=False):
        if not self._device_action_ready():
            return
        try:
            if self._pending():
                raise ValueError('请先导出并清空本地输入，再执行重置。')
        except ValueError as error:
            self.status.set(str(error))
            return
        slot, base = self.slot, self.current
        title = '重置音箱全部预设' if all_presets else f'重置当前预设 · {slot+1:02d} {base.name}'
        detail = ('会重置音箱全部预设，现有调节可能被覆盖。' if all_presets else
                  '会重置当前预设，现有调节可能被覆盖。此操作与“恢复已保存”不同。')
        detail += '\n此重置功能尚未完成实机验证。执行前会完整备份，之后重新读取并显示音箱返回的参数。'
        def reset(_):
            action = self.client.reset_all if all_presets else self.client.reset_current
            def done(result):
                self._snapshot(result['snapshot'])
                self.status.set(f'重置指令已收到回执并重新读取，请核对参数。备份：{result["backup"].name}')
            self._run('正在完整备份、重置并读取…', lambda: action(expected_slot=slot, expected=base), done)
        def validate(value):
            if value != 'RESET':
                raise ValueError('请输入 RESET 后确认')
        ActionDialog(self.root, title, detail, reset, confirm='确认重置', warning=True,
                     initial='' if all_presets else None,
                     hint='输入 RESET 确认重置全部预设', validate=validate if all_presets else None)

    def toggle_comparison(self):
        return self.set_reference('none' if self._reference().key == 'saved' else 'saved')

    def _reference_context(self):
        saved = None
        if self.snapshot and self.current:
            saved = Preset(bytes.fromhex(self.snapshot['presets'][self.slot]['raw_hex']))
        return {'pair': self.audition, 'slot': self.slot, 'offline': self.offline}, saved

    def _reference(self):
        context, saved = self._reference_context()
        choice = self.plot_reference
        if choice is None:
            choice = 'saved' if self.compare_saved else 'none'
        return resolve_reference(choice, self.current, saved, **context)

    def reference_menu(self):
        if not (self.connected or self.offline) or self.busy or self.closing or self.current is None:
            return
        context, saved = self._reference_context()
        available = available_references(self.current, saved, **context)
        selected = self._reference().key
        labels = ('离线原参数' if self.offline else '音箱读回参数', '当前槽位已保存',
                  'A 调节前', 'B 最近应用', '隐藏对照   F7 切换')
        items = [(f'{"✓ " if choice == selected else ""}{label}',
                  lambda value=choice: self.set_reference(value), choice in available, False)
                 for choice, label in zip(REFERENCE_CHOICES, labels)]
        Popup(self.compare_button, items, self.compare_button.winfo_rootx(),
              self.compare_button.winfo_rooty()+self.compare_button.winfo_height()+8, width=240)

    def set_reference(self, choice):
        if not isinstance(choice, str) or choice not in REFERENCE_CHOICES:
            raise ValueError('曲线对照来源不正确')
        if not (self.connected or self.offline) or self.busy or self.closing or self.current is None:
            return False
        context, saved = self._reference_context()
        if choice not in available_references(self.current, saved, **context):
            self.status.set('先应用一次修改，再对照本轮 A/B 曲线。')
            return False
        self.plot_reference = choice
        self.compare_saved = choice == 'saved'
        self._controls()
        self.draw()
        self.status.set(f'当前对照：{self._reference().description}。F7 切换。')
        return True

    def cycle_reference(self):
        if not (self.connected or self.offline) or self.busy or self.closing or self.current is None:
            return False
        context, saved = self._reference_context()
        choices = available_references(self.current, saved, **context)
        current = self._reference().key
        return self.set_reference(choices[(choices.index(current)+1) % len(choices)])

    def plot_range_menu(self):
        if self.busy or self.closing:
            return
        items = [(f'{"✓ " if limit == self.plot_gain_range else ""}±{limit} dB',
                  lambda value=limit: self.set_plot_range(value), True, False) for limit in PLOT_RANGES]
        Popup(self.plot_range_button, items, self.plot_range_button.winfo_rootx(),
              self.plot_range_button.winfo_rooty()-len(items)*38-22, width=190)

    def set_plot_range(self, limit):
        if type(limit) is not int or limit not in PLOT_RANGES:
            raise ValueError('曲线纵轴范围不正确')
        self._close_review()
        self.plot_gain_range = limit
        self.plot_range_button.configure(text=f'纵轴 ±{limit} dB ▾')
        self.draw()

    def _plot_help(self):
        return ('拖动调节频率 · Shift 精细' if self.selected_band in (0, 6) else
                'Shift 精细 · 节点滚轮调 Q')

    def drag_mode_menu(self):
        if self.busy or self.closing:
            return
        labels = {'free': '自由调整频率与增益', 'frequency': '只调频率', 'gain': '只调增益'}
        items = [(f'{"✓ " if mode == self.plot_drag_mode else ""}{labels[mode]}',
                  lambda value=mode: self.set_drag_mode(value), True, False) for mode in DRAG_MODES]
        Popup(self.drag_mode_button, items, self.drag_mode_button.winfo_rootx(),
              self.drag_mode_button.winfo_rooty()-len(items)*38-22, width=210)

    def set_drag_mode(self, mode):
        if mode not in DRAG_MODES:
            raise ValueError('曲线拖动方式不正确')
        if self.busy or self.closing:
            return False
        if self.drag_index is not None:
            self._graph_release()
        self._close_review()
        self.plot_drag_mode = mode
        caption = {'free': '自由', 'frequency': '频率', 'gain': '增益'}[mode]
        self.drag_mode_button.configure(text=f'拖动 · {caption} ▾')
        self.draw()
        return True

    def _graph_leave(self, event=None):
        self.plot_pointer = None
        self.canvas.delete('probe')
        self.graph_hint.set(self._plot_help())

    def _graph_motion(self, event):
        self.plot_pointer = (event.x, event.y)
        self._show_probe(event.x, event.y)

    def _show_probe(self, px, py):
        self.canvas.delete('probe')
        if self.plot_transform is None or self.plot_preset is None:
            self.graph_hint.set(self._plot_help())
            return
        left, right, top, bottom = self.plot_transform.bounds
        if not (left <= px <= right and top <= py <= bottom):
            self.graph_hint.set(self._plot_help())
            return
        frequency = self.plot_transform.frequency_at(px)
        bands = self.plot_preset.bands
        responses = [0.0 if self.plot_bypassed else band_response(band, index, frequency)
                     for index, band in enumerate(bands)]
        caption = f'{frequency/1000:.2f} kHz' if frequency >= 1000 else f'{frequency:.1f} Hz'
        self.graph_hint.set(f'{caption} · 总 {sum(responses):+.2f} dB · '
                            f'{BANDS[self.selected_band]} {responses[self.selected_band]:+.2f} dB')
        self.canvas.create_line(px, top, px, bottom, fill=EDGE, dash=(2, 5), tags='probe')
        reference = self.plot_reference_view
        if reference is not None and reference.preset is not None:
            total = sum(responses)
            reference_total = sum(0.0 if self.plot_bypassed else band_response(band, index, frequency)
                                  for index, band in enumerate(reference.preset.bands))
            difference = total-reference_total
            if abs(difference) < 0.0005:
                difference = 0.0
            anchor, text_x = ('ne', right-8) if px < (left+right)/2 else ('nw', left+8)
            caption = f'{reference.label} {reference_total:+.2f} dB · 差 {difference:+.2f} dB'
            text_id = self.canvas.create_text(text_x, top+8, text=caption, anchor=anchor,
                                              fill=reference.color, font=('Microsoft YaHei UI', 9),
                                              width=min(280, right-left-16), tags=('probe', 'reference-probe'))
            bounds = self.canvas.bbox(text_id)
            if bounds:
                x0, y0, x1, y1 = bounds
                self.canvas.create_rectangle(x0-5, y0-3, x1+5, y1+3,
                                              fill=PANEL, outline=EDGE, tags='probe')
                self.canvas.tag_raise(text_id)

    def _graph_press(self, event):
        if not (self.connected or self.offline) or self.busy or self.closing or not self.plot_nodes:
            return
        nearest = min(self.plot_nodes, key=lambda node: (node[1]-event.x)**2+(node[2]-event.y)**2)
        if math.hypot(nearest[1]-event.x, nearest[2]-event.y) <= 22:
            band = self.plot_preset.bands[nearest[0]]
            self.choose_band(nearest[0])
            self.drag_index = nearest[0]
            self.drag_origin = (event.x, event.y, band.frequency, band.gain)
            self.drag_last = (event.x, event.y)
            self.drag_fine = bool(event.state & 1)
            self.drag_active_mode = self.plot_drag_mode

    def _graph_release(self, event=None):
        self.drag_index = None
        self.drag_origin = None
        self.drag_last = None
        self.drag_active_mode = None
        self.history.end_group()

    def _graph_drag(self, event):
        if (self.drag_index is None or not (self.connected or self.offline) or self.busy or self.closing
                or not self.plot_bounds):
            return
        fine = bool(event.state & 1)
        if fine != self.drag_fine and self.drag_last is not None:
            band = self.plot_preset.bands[self.drag_index]
            self.drag_origin = (*self.drag_last, band.frequency, band.gain)
        self.drag_fine = fine
        values = drag_values(self.drag_index, event.x, event.y, self.plot_bounds,
                             gain_range=self.plot_gain_range, origin=self.drag_origin, fine=fine,
                             mode=self.drag_active_mode)
        self.drag_last = (event.x, event.y)
        self.suppress = True
        try:
            for field, value in values.items():
                self.values[self.drag_index][field].set(display_float(value))
        finally:
            self.suppress = False
        self.plot_pointer = (event.x, event.y)
        self._changed(('graph', self.drag_index))

    def _graph_wheel(self, event):
        if not (self.connected or self.offline) or self.busy or self.closing or not event.delta or not self.plot_nodes:
            return 'break'
        nearest = min(self.plot_nodes, key=lambda node: (node[1]-event.x)**2+(node[2]-event.y)**2)
        index = nearest[0]
        if index in (0, 6) or math.hypot(nearest[1]-event.x, nearest[2]-event.y) > 22:
            return 'break'
        if index != self.selected_band:
            self.choose_band(index)
        try:
            q = parse_parameter(self.values[index]['q'].get(), 'q')
        except ValueError:
            self.status.set(f'请先修正 {BANDS[index]} 的 Q 值。')
            return 'break'
        step = 0.01 if event.state & 1 else 0.1
        value = min(10, max(0.1, round(q + (step if event.delta > 0 else -step), 2)))
        self.suppress = True
        try:
            self.values[index]['q'].set(display_float(value))
        finally:
            self.suppress = False
        self._changed(('graph-wheel', index))
        return 'break'

    def _run(self, message, work, done, *, cancellable=False, allow_recovery=False):
        if self.busy or self.closing or (getattr(self, 'needs_readback', False) and not cancellable
                                        and not allow_recovery):
            return
        self._close_review()
        self.busy = True
        self.cancellable = cancellable
        self.client.begin_operation()
        self.status.set(message)
        self._controls()
        runtime = getattr(self, 'runtime', None)
        if runtime:
            runtime.record('device.started', operation=message)
        def worker():
            started = time.monotonic()
            try:
                result, error = work(), None
            except Exception as failure:
                result, error = None, failure
            if runtime:
                runtime.record('device.finished', operation=message,
                               elapsed_seconds=round(time.monotonic()-started, 3),
                               error=str(error) if error else None)
            self.results.put((done, result, error))
        threading.Thread(target=worker, name='AXON MIDI', daemon=False).start()

    def _poll(self):
        reschedule = True
        try:
            try:
                done, result, error = self.results.get_nowait()
            except queue.Empty:
                pass
            else:
                self.busy = False
                self.cancellable = False
                if error:
                    if not isinstance(error, ReadCancelled):
                        self.audition = None
                        self.needs_readback = True
                    actual = getattr(error, 'snapshot', None)
                    if actual is not None:
                        self._snapshot(actual)
                    self.status.set(str(error))
                    self.connected = self.client.port is not None
                    if not self.connected:
                        self.offline = self.current is not None
                        self.device_label.set('○  离线编辑' if self.offline else '未连接')
                        if self.snapshot:
                            self.eq_label.set('离线：EQ 已启用' if self.snapshot['eq_enabled'] else '离线：EQ 已旁通')
                    if self.snapshot:
                        self.combo.current(self.slot)
                    self._changed(record=False)
                else:
                    done(result)
                # Complete readback before preserving an in-flight edit.
                if self.closing and self.close():
                    reschedule = False
                    return
                self._controls()
        finally:
            # A failed UI callback must not silently stop all future device
            # result polling. Tk's callback hook records the original error.
            if reschedule:
                self.root.after(60, self._poll)

    def connect(self):
        if self.busy:
            if getattr(self, 'cancellable', False):
                self.client.cancel_read()
                self.status.set('正在取消读取…')
            return
        if hasattr(self, 'want_connection'):
            self.want_connection = (not self.connected) and self.desktop.auto_reconnect
        if self.connected:
            def disconnected(_):
                self.audition = None
                self.connected = False
                self.needs_readback = False
                self.offline = self.current is not None
                self.device_label.set('○  离线编辑' if self.offline else '未连接')
                self.eq_label.set('离线：EQ 已启用' if self.snapshot and self.snapshot['eq_enabled'] else '离线：EQ 已旁通')
                self._changed(record=False)
                self.status.set('已断开，可继续编辑并导出草稿。重新连接时读取实际参数。')
            self._run('正在断开…', self.client.close, disconnected, allow_recovery=True)
            return
        local_base, local_target, local_slot, draft_path = None, None, None, None
        if self.offline and self.current is not None:
            try:
                local_target = self.target()
                local_base, local_slot = self.current, self.slot
                if local_target.raw != local_base.raw:
                    draft_path = self._write_draft(local_target)
            except (ValueError, OSError) as error:
                self.status.set(f'请先修正本地输入：{error}')
                return
        def work():
            try:
                snapshot = self.client.connect()
                self.client.backup_snapshot(snapshot, 'connected')
                return snapshot
            except Exception:
                self.client.close()
                raise
        def done(snapshot):
            self.connected = True
            self.needs_readback = False
            self.offline = False
            self.device_label.set('●  AXON 3 / USB')
            self._snapshot(snapshot)
            self.status.set('已连接，当前参数与全部预设已备份。')
            if draft_path:
                try:
                    if local_slot != self.slot:
                        raise ValueError('音箱当前槽位与草稿不同')
                    preview = rebase_preview(local_base, local_target, self.current)
                    self._set_form(preset_form(preview))
                    self._changed()
                    self.history.end_group()
                    self.status.set('已连接，草稿已接续为本地预览；点击“应用试听”后生效。')
                except ValueError as error:
                    self.status.set(f'{error}；草稿保留在 drafts/{draft_path.name}，请核对后载入。')
        self._run('正在连接并备份…', work, done, cancellable=True)

    def _snapshot(self, snapshot):
        self.audition = None
        self.snapshot = snapshot
        self.slot = snapshot['active_slot']
        labels = [f'{i+1:02d}  {p["name"]}' for i, p in enumerate(snapshot['presets'])]
        self.combo.configure(values=labels)
        self.preset_label.set(labels[self.slot])
        self.eq_label.set(('离线：' if self.offline else '') +
                          ('EQ 已启用' if snapshot['eq_enabled'] else 'EQ 已旁通'))
        self._populate(Preset(bytes.fromhex(snapshot['current']['raw_hex'])))

    def _populate(self, preset):
        self.current = preset
        self._set_form(preset_form(preset))
        self.history.reset(self._form())
        self._changed(record=False)

    def _form(self):
        return tuple(tuple(row[field].get() for field in FIELDS) for row in self.values)

    def _set_form(self, state):
        self.suppress = True
        try:
            for variables, row in zip(self.values, state):
                for field, value in zip(FIELDS, row):
                    variables[field].set(value)
        finally:
            self.suppress = False

    def _editor_base(self):
        return self.audition.after if self.audition and self.audition.side == 'A' else self.current

    def target(self):
        return preview_from_form(self._editor_base(), self._form())

    def _changed(self, group=None, *, record=True):
        if self.suppress:
            return
        self._close_review()
        if self.current is not None and record:
            self.history.record(self._form(), group,
                                 continuous=self.drag_index is not None and group == ('graph', self.drag_index))
        for card in self.cards.values():
            card.set_error('')
        self.edit_status_label.configure(fg=ACCENT)
        self.review = None
        try:
            self.review = review_form(self.current, self._form(), preview_base=self._editor_base())
            target = self.target()
        except ValueError as error:
            self.edit_status_label.configure(fg=WARNING)
            errors = self.review.error_count if self.review else 0
            self.edit_label.set(f'{errors} 项输入有误' if errors > 1 else
                                error.short_message if isinstance(error, FieldError) else str(error))
            if self.review:
                for item in self.review.items:
                    if item.error and item.index == self.selected_band and item.field in self.cards:
                        self.cards[item.field].set_error(INPUT_HINTS[item.field])
            preview = self.review.preview if self.review else self._editor_base()
            self._refresh_tiles(preview)
            self._controls()
            self.draw(preview)
            return
        if target is None:
            self.edit_label.set('')
        elif self.audition and not self.snapshot['eq_enabled']:
            self.edit_label.set(f'{self.audition.side} 参数 · EQ 已旁通')
        elif self.audition and self.audition.side == 'A':
            count = len(changed_fields(self.audition.after, target))
            self.edit_label.set(f'试听 A · B 有 {count} 项待应用' if count else '试听 A · 编辑器保留 B')
        elif target.raw != self.current.raw:
            self.edit_label.set(f'{"草稿" if self.offline else "待应用"} · {len(changed_fields(self.current, target))} 项改动')
        elif self.offline:
            self.edit_label.set('离线 · 本地预览')
        elif self.snapshot and self.current.raw.hex() != self.snapshot['presets'][self.slot]['raw_hex']:
            self.edit_label.set('试听 B · 尚未保存' if self.audition else '试听中 · 尚未保存')
        else:
            self.edit_label.set('已同步')
        self._refresh_tiles(target)
        self._controls()
        self.draw(target)

    def _pending(self):
        target = self.target()
        return target is not None and target.raw != self.current.raw

    def review_changes(self):
        if not (self.connected or self.offline) or self.busy or self.closing:
            return
        if self.review is None or not self.review.items:
            self.status.set('没有待应用的参数改动。')
            return
        items = [(f'{BANDS[item.index]} · {FIELD_LABELS[item.field]}', item.before, item.after, bool(item.error),
                  lambda entry=item: self.jump_to_parameter(entry)) for item in self.review.items]
        self._close_review()
        self.active_review = ReviewPopup(self.review_button, items, self.review_button.winfo_rootx(),
                                          self.review_button.winfo_rooty()+self.review_button.winfo_height()+8)

    def _close_review(self):
        if self.active_review is not None:
            if self.active_review.winfo_exists():
                self.active_review.destroy()
            self.active_review = None

    def jump_to_parameter(self, item):
        if not (self.connected or self.offline) or self.busy or self.closing:
            return
        self.choose_band(item.index)
        if item.field == 'enabled':
            self.enable_switch.focus_set()
        else:
            entry = self.cards[item.field].entry
            entry.focus_set()
            entry.selection_range(0, 'end')
        self.status.set(f'已定位 {BANDS[item.index]} · {FIELD_LABELS[item.field]}。')

    def read(self):
        if not self.connected or self.busy or self.closing:
            self.status.set('请先连接音箱，再读取实际参数。')
            return
        base, slot, preview, draft = self.current, self.slot, None, None
        try:
            preview = self.target()
            if preview.raw != base.raw:
                draft = self._write_draft(preview)
        except (ValueError, OSError) as error:
            self.status.set(f'请先修正或导出本地输入：{error}')
            return
        def done(snapshot):
            self.needs_readback = False
            self._snapshot(snapshot)
            self.status.set('已重新读取音箱实际参数。')
            if draft:
                try:
                    if slot != self.slot:
                        raise ValueError('音箱槽位已改变')
                    self._set_form(preset_form(rebase_preview(base, preview, self.current)))
                    self._changed()
                    self.status.set('已读取实际参数，本地编辑已接续；尚未应用的输入保留。')
                except ValueError as error:
                    self.status.set(f'{error}；本地编辑已保留为草稿：{draft.name}')
        self._run('正在读取…', self.client.snapshot, done, cancellable=True)

    def step_preset(self, direction):
        if not self.snapshot or self.busy or self.closing or getattr(self, 'needs_readback', False):
            return
        target = self.slot + direction
        if 0 <= target < len(self.snapshot['presets']):
            self.combo.current(target)
            self.select()

    def select(self, _event=None):
        if self.busy or self.closing or getattr(self, 'needs_readback', False):
            if self.snapshot:
                self.combo.current(self.slot)
            return
        slot = self.combo.current()
        if slot == self.slot or slot < 0:
            return
        self.combo.current(self.slot)
        try:
            target = self.target()
            pending = target.raw != self.current.raw
        except ValueError as error:
            self.status.set(f'请先修正本地输入再切换：{error}')
            return
        unsaved = self.current.raw.hex() != self.snapshot['presets'][self.slot]['raw_hex']
        if pending or (self.connected and unsaved):
            source, expected = self.slot, self.current
            def proceed(_):
                try:
                    draft = self._write_draft(target)
                except OSError as error:
                    self.status.set(f'草稿未保存，切换已停止：{error}')
                    return
                self._select_slot(slot, source, expected, draft)
            ActionDialog(self.root, '切换预设',
                         f'当前预设有{"未应用的输入" if pending else "未保存的试听参数"}。\n'
                         f'将先保存为本地草稿，再切换到 {slot+1:02d} {self.snapshot["presets"][slot]["name"]}。\n'
                         '需要将调节存入音箱时，先取消并保存预设。', proceed, confirm='保留草稿并切换')
            return
        self._select_slot(slot, self.slot, self.current)

    def _select_slot(self, slot, source_slot, expected, draft=None):
        if self.offline and not self.connected:
            self.snapshot['active_slot'] = slot
            self.snapshot['current'] = self.snapshot['presets'][slot]
            self._snapshot(self.snapshot)
            self.status.set(f'已切换离线预设。草稿：{draft.name}' if draft else '已切换离线预设；音箱参数不受影响。')
            return
        if not self._device_action_ready():
            return
        def work():
            self.client.select(slot, expected_slot=source_slot, expected=expected)
            return self.client.snapshot(refresh_presets=False)
        def done(snapshot):
            self._snapshot(snapshot)
            self.status.set(f'已切换预设。上份编辑保留在草稿：{draft.name}' if draft else '已切换预设并核对当前参数。')
        self._run('正在切换预设…', work, done)

    def apply(self):
        if not self.connected:
            self.status.set('离线编辑中。连接音箱后才能应用试听。')
            return
        try:
            target = self.target()
        except ValueError as error:
            self.status.set(str(error))
            return
        if target is None or target.raw == self.current.raw:
            self.status.set('参数没有变化。')
            return
        base, slot, previous = self.current, self.slot, self.audition
        def done(preset):
            self.audition = AuditionPair.applied(previous, slot, base, preset)
            self._populate(preset)
            self.status.set('已应用并读回确认。F6 切换 A/B，满意后保存预设。' if self.audition else
                            '已应用并读回确认；与本轮原参数一致，A/B 对比已结束。')
        self._run('正在备份、应用并读回…', lambda: self.client.apply(base, target, expected_slot=slot), done)

    def listen(self, side):
        if not self.connected or self.busy or self.closing:
            return
        pair = self.audition
        if pair is None:
            self.status.set('先应用一次修改，即可用 A/B 对比本轮调节前后。')
            return
        if not self.snapshot['eq_enabled']:
            self.status.set('EQ 已旁通。启用 EQ 并重新读取后再调节试听。')
            return
        base, slot = self.current, self.slot
        try:
            target = pair.request(side, slot, base)
        except ValueError as error:
            self.status.set(str(error))
            return
        if target.raw == base.raw:
            self.status.set(f'当前正在试听 {side}。')
            return
        def work():
            state = self.client.state()
            if state[2] != slot:
                raise RuntimeError('音箱预设槽位已改变，请重新读取后再试听。')
            if not state[1]:
                return None, state
            preset = self.client.apply(base, target, expected_slot=slot)
            state = self.client.state()
            if state[2] != slot:
                raise RuntimeError('音箱预设槽位已改变，请重新读取后再试听。')
            return preset, state
        def done(result):
            preset, state = result
            self.snapshot['eq_enabled'] = bool(state[1])
            self.snapshot['state_hex'] = state.hex()
            self.eq_label.set('EQ 已启用' if state[1] else 'EQ 已旁通')
            if preset is None:
                self._changed(record=False)
                self.status.set('音箱 EQ 已旁通。启用并重新读取后再调节试听。')
                return
            self.audition = pair.confirmed(side, preset)
            self.current = preset
            self._changed(record=False)
            self.status.set(f'正在试听 {side} {"调节前" if side == "A" else "调节后"}；B 的本地输入保留。'
                            if state[1] else f'{side} 参数已读回；EQ 已旁通，启用后再试听。')
        self._run(f'正在切换试听 {side} 并读回…', work, done)

    def toggle_audition(self):
        self.listen('B' if self.audition and self.audition.side == 'A' else 'A')

    def save(self):
        if not self.connected:
            if self.offline:
                self.export_draft()
            else:
                self.status.set('请先连接音箱，或离线打开备份。')
            return
        if self.audition and self.audition.side == 'A':
            self.status.set('正在试听 A。先切换到 B 并应用待编辑参数，再保存预设。')
            return
        try:
            if self._pending():
                raise ValueError('请先点击“应用试听”，再保存预设。')
        except ValueError as error:
            self.status.set(str(error))
            return
        expected, slot = self.current, self.slot
        def done(preset):
            self.snapshot['presets'][slot] = preset.as_dict()
            self.audition = None
            self._populate(preset)
            self.status.set('已保存预设，并读回预设确认。')
        self._run('正在备份并保存预设…', lambda: self.client.save(expected, expected_slot=slot), done)

    def restore(self):
        if not self.connected:
            self.status.set('离线编辑中，可取回单频段保存值到本地预览。')
            return
        base, slot = self.current, self.slot
        def work():
            state = self.client.state()
            if state[2] != slot:
                raise RuntimeError('音箱预设已改变，请重新读取。')
            saved = self.client.preset(slot)
            self.client.apply(base, saved, expected_slot=slot)
            return self.client.snapshot()
        def done(snapshot):
            self._snapshot(snapshot)
            self.status.set('已恢复当前预设中保存的参数。')
        self._run('正在恢复已保存参数…', work, done)

    def undo(self):
        state = self.history.undo()
        if state is not None:
            self._set_form(state)
            self._changed(record=False)
            self.status.set('已撤销一步；音箱参数不受影响。')

    def redo(self):
        state = self.history.redo()
        if state is not None:
            self._set_form(state)
            self._changed(record=False)
            self.status.set('已重做一步；点击“应用试听”后生效。')

    def discard(self):
        self.history.end_group()
        self._set_form(preset_form(self.current))
        self._changed()
        self.history.end_group()
        self.status.set('已清空本地输入。可用撤销取回；音箱参数不受影响。')

    def restore_band(self):
        saved = Preset(bytes.fromhex(self.snapshot['presets'][self.slot]['raw_hex']))
        form = list(self._form())
        form[self.selected_band] = preset_form(saved)[self.selected_band]
        self.history.end_group()
        self._set_form(tuple(form))
        self._changed()
        self.history.end_group()
        self.status.set(f'已取回 {BANDS[self.selected_band]} 保存值到本地预览；可撤销。')

    def backup(self):
        if not self.connected:
            self.status.set('离线模式可导出草稿；音箱备份需要 USB 连接。')
            return
        def done(path):
            self.status.set(f'已备份全部预设：backups/{path.name}')
        self._run('正在备份全部参数…', lambda: self.client.backup('manual'), done)

    def _band_action_ready(self):
        return bool(self.current and (self.connected or self.offline) and not self.busy and not self.closing)

    def copy_band(self):
        if not self._band_action_ready():
            return False
        try:
            text = copy_snippet(self._editor_base(), self._form(), self.selected_band)
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
        except (ValueError, tk.TclError) as error:
            self.status.set(f'复制未完成：{error}')
            return False
        self.status.set(f'已复制 {BANDS[self.selected_band]} 本地参数；Ctrl+Shift+V 粘贴到同类频段。')
        return True

    def paste_band(self):
        if not self._band_action_ready():
            return False
        try:
            form, source = paste_snippet(self._form(), self.selected_band, self.root.clipboard_get())
        except tk.TclError:
            self.status.set('剪贴板中没有可粘贴的 AXON 频段参数。')
            return False
        except ValueError as error:
            self.status.set(f'粘贴未完成：{error}')
            return False
        self.history.end_group()
        self._set_form(form)
        self._changed()
        self.history.end_group()
        self.status.set(f'已将 {source} 参数粘贴到 {BANDS[self.selected_band]} 本地预览；可撤销。')
        return True

    def zero_gain(self):
        if not self._band_action_ready():
            return False
        if self.selected_band in (0, 6):
            self.status.set('HP / LP 不提供增益归零。')
            return False
        form = [list(row) for row in self._form()]
        form[self.selected_band][FIELDS.index('gain')] = '0'
        self.history.end_group()
        self._set_form(tuple(tuple(row) for row in form))
        self._changed()
        self.history.end_group()
        self.status.set(f'已将 {BANDS[self.selected_band]} 增益归零到本地预览；可撤销。')
        return True

    def load_backup(self):
        path = filedialog.askopenfilename(parent=self.root, title='载入 AXON Control 备份或草稿',
                                           initialdir=getattr(self, 'data_dir', HERE), filetypes=[('JSON 备份或草稿', '*.json')])
        if not path:
            return
        self.load_document_path(path)

    def load_recent(self):
        paths = sorted((getattr(self, 'data_dir', HERE) / 'backups').glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True)
        if not paths:
            self.status.set('还没有备份。连接音箱读取一次后即可离线编辑。')
            return
        self.load_document_path(paths[0])

    def load_document_path(self, path):
        try:
            raw = json.loads(Path(path).read_text(encoding='utf-8-sig'))
            if not self.connected:
                data, preview = offline_document(raw)
                self.offline = True
                self.device_label.set('○  离线编辑')
                self._snapshot(data)
                if preview is not None:
                    self._set_form(preset_form(preview))
                    self._changed()
                    self.history.end_group()
                self.status.set(f'已离线打开 {Path(path).name}；本地编辑不会影响音箱。')
                return
            data = read_document(raw)
            entry = data['current'] if data['active_slot'] == self.slot else data['presets'][self.slot]
            preset = Preset(bytes.fromhex(entry['raw_hex']))
            editable = editable_import(self.current, preset)
            self.history.end_group()
            self._set_form(preset_form(editable))
            self._changed()
            self.history.end_group()
            self.status.set(f'已载入 {Path(path).name} 的当前槽位参数；点击“应用试听”后生效。')
        except (ValueError, KeyError, IndexError, TypeError, OSError) as error:
            self.status.set(f'备份载入失败：{error}')

    def _write_draft(self, preview):
        folder = getattr(self, 'data_dir', HERE) / 'drafts'
        folder.mkdir(parents=True, exist_ok=True)
        now = datetime.now().astimezone()
        path = folder / f'{now:%Y%m%d-%H%M%S-%f}-slot{self.slot+1:02d}-draft.json'
        data = {'kind': 'axon-local-draft-v2', 'created_at': now.isoformat(),
                'device': self.snapshot['device'], 'active_slot': self.slot,
                'eq_enabled': self.snapshot['eq_enabled'], 'current': preview.as_dict(),
                'source_current': self.current.as_dict(),
                'presets': self.snapshot['presets'], 'state_hex': self.snapshot.get('state_hex', '')}
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        return path

    def export_draft(self):
        if self.current is None:
            self.status.set('请先连接音箱，或离线打开备份。')
            return
        try:
            path = self._write_draft(self.target())
            self.status.set(f'草稿已导出：drafts/{path.name}；未写入音箱。')
        except (ValueError, OSError) as error:
            self.status.set(f'草稿导出失败：{error}')

    def _refresh_tiles(self, preset):
        for index, tile in enumerate(self.band_tiles):
            if preset is None:
                frequency, enabled = '—', False
            else:
                band = preset.bands[index]
                frequency = f'{band.frequency/1000:g} kHz' if band.frequency >= 1000 else f'{band.frequency:g} Hz'
                enabled = band.enabled
            entries = [item for item in self.review.items if item.index == index] if self.review else []
            tile.update_band(frequency, enabled, index == self.selected_band,
                             changes=len(entries), invalid=any(item.error for item in entries))

    def toggle_audio_view(self):
        visible = not self.audio_panel.visible
        if visible:
            self.graph_content.columnconfigure(0, weight=5, uniform='plots')
            self.graph_content.columnconfigure(1, weight=3, uniform='plots')
            self.eq_panel.grid_configure(padx=(0, 6))
            self.audio_card.grid()
        else:
            self.audio_card.grid_remove()
            self.graph_content.columnconfigure(0, weight=1, uniform='')
            self.graph_content.columnconfigure(1, weight=0, uniform='')
            self.eq_panel.grid_configure(padx=0)
        self.audio_panel.set_visible(visible)
        self.audio_button.configure(text='收起频谱' if visible else '显示频谱')
        self.draw()

    def draw(self, preset=None):
        self._draw_canvas(preset)
        # Keep transient probe graphics outside the cached curve layer, so
        # leaving the plot can remove them completely without redrawing it.
        if hasattr(self, 'canvas') and self.plot_pointer is not None:
            self._show_probe(*self.plot_pointer)

    @painted('canvas')
    def _draw_canvas(self, preset=None):
        if not hasattr(self, 'canvas'):
            return
        if preset is None:
            try:
                preset = self.target()
            except ValueError:
                preset = self.review.preview if self.review else self._editor_base()
        canvas = self.canvas
        canvas.delete('all')
        width, height = max(canvas.winfo_width(), 1), max(canvas.winfo_height(), 1)
        left, right = 43, width-18
        top, bottom = (10, height-22) if height < 120 else (16, height-26)
        self.plot_nodes = []
        self.plot_bounds = (left, right, top, bottom)
        self.plot_preset = preset
        self.plot_reference_view = self._reference()
        self.plot_transform = None
        self.graph_hint.set(self._plot_help())
        if right <= left or bottom-top < 20:
            return
        self.plot_transform = transform = PlotTransform(self.plot_bounds, self.plot_gain_range)
        x, y = transform.frequency_x, transform.gain_y
        gains = transform.ticks(compact=bottom-top < 100)
        for gain in gains:
            canvas.create_line(left, y(gain), right, y(gain), fill='#515d66' if gain == 0 else '#30373e',
                               width=1)
            canvas.create_text(left-13, y(gain), text=f'{gain:+g}' if gain else '0',
                               anchor='e', fill=MUTED, font=('Segoe UI', 10))
        for frequency, label in ((20, '20'), (50, '50'), (100, '100'), (200, '200'), (500, '500'),
                                 (1000, '1k'), (2000, '2k'), (5000, '5k'), (10000, '10k'), (20000, '20k')):
            canvas.create_line(x(frequency), top, x(frequency), bottom, fill='#2b3238', dash=(2, 5))
            canvas.create_text(x(frequency), bottom+16, text=label, fill=MUTED, font=('Segoe UI', 10))
        if preset is None:
            canvas.create_line(left, y(0), right, y(0), fill='#52604f', width=2)
            canvas.create_text((left+right)/2, y(0)-27, text='连接后显示音箱当前 EQ',
                               fill=MUTED, font=('Microsoft YaHei UI', 12))
            return
        bypassed = self.snapshot is not None and not self.snapshot['eq_enabled']
        self.plot_bypassed = bypassed
        samples = self.curve_cache.get(preset, bypassed)
        points = [coordinate for frequency, gain in zip(samples.frequencies, samples.totals)
                  for coordinate in (x(frequency), y(gain))]
        selected_points = [coordinate for frequency, gain in zip(samples.frequencies, samples.bands[self.selected_band])
                           for coordinate in (x(frequency), y(gain))]
        band_color = COLORS[self.selected_band]
        canvas.create_polygon(left, y(0), *selected_points, right, y(0),
                               fill=blend(PANEL, band_color, 0.10), outline='')
        reference = self.plot_reference_view
        if reference.preset is not None:
            compared = self.curve_cache.get(reference.preset, bypassed)
            reference_points = [coordinate for frequency, gain in zip(compared.frequencies, compared.totals)
                            for coordinate in (x(frequency), y(gain))]
            canvas.create_line(*reference_points, fill=reference.color, width=1.7,
                               dash=reference.dash, tags='reference')
        canvas.create_line(*selected_points, fill=blend(EDGE, band_color, 0.50), width=1, dash=(3, 5))
        canvas.create_line(*points, fill=ACCENT, width=2.8, capstyle='round', joinstyle='round')
        for index, band in enumerate(preset.bands):
            if not 20 <= band.frequency <= 20000:
                continue
            gain = -3 if index in (0, 6) else band.gain
            cx, cy = x(band.frequency), y(gain)
            self.plot_nodes.append((index, cx, cy))
            selected = index == self.selected_band
            color = COLORS[index] if band.enabled and not bypassed else DIM
            if selected:
                canvas.create_line(cx, top, cx, bottom, fill=blend(EDGE, color, 0.40), dash=(2, 5))
                canvas.create_oval(cx-13, cy-13, cx+13, cy+13, fill=blend(PANEL, color, 0.19), outline='')
            radius = 7 if selected else 5
            canvas.create_oval(cx-radius, cy-radius, cx+radius, cy+radius,
                               fill=color if selected else PANEL, outline=color, width=2)
            text_x = min(right-12, max(left+12, cx))
            canvas.create_text(text_x, max(top+8, cy-22), text=BANDS[index], fill=color,
                               font=('Segoe UI', 9, 'bold' if selected else 'normal'))
        canvas.configure(cursor='crosshair' if (self.connected or self.offline) and not self.busy else 'arrow')

    def _write_session(self, store=None):
        if self.current is None:
            return
        document = dict(self.snapshot, active_slot=self.slot, current=self.current.as_dict())
        reference = self._reference().key
        # A/B pairs are temporary and are rebuilt after the next explicit apply.
        if reference in ('before', 'after'):
            reference = 'current'
        (store or self.session_store).save(document, self._form(), self.selected_band, self.compare_saved,
                                plot_gain_range=self.plot_gain_range, plot_drag_mode=self.plot_drag_mode,
                                plot_reference=reference)

    def restore_session(self):
        try:
            recovery = getattr(self, 'recovery_store', None)
            recovered = False
            if recovery and recovery.path.exists():
                try:
                    session = recovery.load()
                    recovered = True
                except (ValueError, OSError):
                    session = self.session_store.load()
            else:
                session = self.session_store.load()
        except FileNotFoundError:
            return False
        except (ValueError, OSError) as error:
            self.status.set(f'上次编辑恢复失败：{error}；原文件仍保留。')
            return False
        self.offline = True
        self.device_label.set('○  离线编辑')
        self._snapshot(session['document'])
        self.choose_band(session['selected_band'])
        self.set_plot_range(session['plot_gain_range'])
        self.set_drag_mode(session['plot_drag_mode'])
        self.set_reference(self._reference_for_session(session))
        self._set_form(session['form'])
        self._changed()
        self.history.end_group()
        self.status.set('已恢复异常退出前自动保存的草稿；连接后核对，再应用试听。' if recovered else
                        '已离线恢复上次编辑；连接音箱后核对实际参数，再应用试听。')
        return True

    @staticmethod
    def _reference_for_session(session):
        choice = session['plot_reference']
        return 'current' if choice in ('before', 'after') else choice

    def close(self):
        if self.busy:
            self.closing = True
            if getattr(self, 'cancellable', False):
                self.client.cancel_read()
            self.status.set('完成当前操作后关闭…')
            self._controls()
            return False
        try:
            if hasattr(self, 'library'):
                self._remember_environment()
            self._write_session()
            self.client.close()
            if getattr(self, 'recovery_store', None):
                self.recovery_store.path.unlink(missing_ok=True)
        except (ValueError, OSError, RuntimeError) as error:
            self.closing = False
            self.status.set(f'关闭未完成：{error}；编辑内容仍在窗口中，请修正或导出后重试。')
            self._controls()
            return False
        if hasattr(self, 'audio_panel'):
            self.audio_panel.close()
        if getattr(self, 'tray', None):
            self.tray.close()
        self.root.destroy()
        if getattr(self, 'window_icons', None):
            self.window_icons.close()
        return True


def main():
    global DATA
    import argparse
    parser = argparse.ArgumentParser(description='AXON Control 参数编辑器')
    parser.add_argument('--offline-backup', type=Path, help='离线打开参数备份或草稿')
    parser.add_argument('--start-in-tray', action='store_true', help='启动后收起到托盘')
    args = parser.parse_args()
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass
    instance = SingleInstance(os.environ.get('AXON_CONTROL_INSTANCE', 'Local\\AXONControl.Desktop'))
    if not instance.primary:
        instance.close()
        return
    DATA = prepare_data(BINARY)
    migration_file = BINARY / 'legacy-data-locations.json'
    if migration_file.exists():
        try:
            Startup(BINARY).relocate_from(json.loads(migration_file.read_text(encoding='utf-8')))
        except (ValueError, OSError):
            pass
    set_taskbar_identity()
    root = tk.Tk()
    runtime = UiRuntime(root, DATA, '0.20')
    try:
        app = App(root, runtime)
        def activate_existing():
            if instance.requested():
                app._restore_window()
            root.after(150, activate_existing)
        root.after(150, activate_existing)
        if args.offline_backup:
            root.after(100, lambda: app.load_document_path(args.offline_backup))
        else:
            root.after(100, app.restore_session)
        if args.start_in_tray:
            root.after(250, app._hide_to_tray)
        root.mainloop()
    finally:
        runtime.close()
        instance.close()


if __name__ == '__main__':
    main()
