"""Device lifecycle and local preset-library UI."""
import ctypes
import json
import os
import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk, messagebox
from axon_protocol import Preset, BANDS
from editor_state import editable_import, preset_form
from preset_library import PresetLibrary
from ui_widgets import PANEL, SURFACE, TEXT, MUTED, ACCENT, Button


class ProductFeatures:
    def init_features(self):
        from desktop_integration import TrayIcon
        self.tray = TrayIcon(self.root, Path(__file__).with_name('axon-icon.ico'), self._restore_window, self.close)
        self.tray.on_resume = self._power_resume
        self.tray.on_devices_changed = self._device_changed
        self.library = PresetLibrary(self.data_dir / 'preset-library.json')
        self.library_window = None
        self.device_scan = queue.Queue(maxsize=1)
        self.scan_running = False
        self.want_connection = self.desktop.auto_connect
        self.resume_pending = False
        self.watch_last = time.monotonic()
        self.next_reconnect = 0
        self.audio_panel.source = self.desktop.audio_source
        self.audio_panel.source_name = self.desktop.audio_source_label
        self.audio_panel.choose_style(self.desktop.audio_style)
        self.audio_panel._source_caption()
        geometry = self.desktop.window_geometry
        if geometry:
            try:
                # Restore dimensions and keep the window on the virtual desktop.
                import re
                match = re.fullmatch(r'(\d+)x(\d+)([+-]\d+)([+-]\d+)', geometry)
                if match:
                    w, h, x, y = map(int, match.groups())
                    u = ctypes.windll.user32
                    left, top = u.GetSystemMetrics(76), u.GetSystemMetrics(77)
                    vw, vh = u.GetSystemMetrics(78), u.GetSystemMetrics(79)
                    w, h = min(max(980, w), vw), min(max(740, h), vh)
                    x, y = max(left, min(x, left+vw-w)), max(top, min(y, top+vh-h))
                    self.root.geometry(f'{w}x{h}')
                    self.root.after(150, lambda: self._position_window(x, y))
            except (ValueError, tk.TclError):
                pass
        self.root.after(1000, self._device_watch)

    def _power_resume(self):
        self.resume_pending = True
        if self.audio_panel.running:
            self.audio_panel.start()

    def _device_changed(self):
        if self.connected:
            self.resume_pending = True

    def _position_window(self, x, y):
        self._window_theme()
        user = ctypes.WinDLL('user32')
        user.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                     ctypes.c_int, ctypes.c_int, ctypes.c_uint]
        user.SetWindowPos(self.native_hwnd, None, x, y, 0, 0, 0x15)

    def _remember_environment(self):
        geometry = self.normal_geometry if self.window_maximized else self.root.geometry()
        user = ctypes.WinDLL('user32')
        user.IsIconic.argtypes = [ctypes.c_void_p]
        if self.root.state() != 'normal' or (self.native_hwnd and user.IsIconic(self.native_hwnd)):
            geometry = self.desktop.window_geometry
        values = (geometry, self.audio_panel.source, self.audio_panel.source_name, self.audio_panel.style)
        if values != (self.desktop.window_geometry, self.desktop.audio_source, self.desktop.audio_source_label,
                      self.desktop.audio_style):
            self.desktop.save(self.desktop.minimize_to_tray, self.desktop.pause_in_background,
                              window_geometry=geometry, audio_source=values[1], audio_source_label=values[2],
                              audio_style=values[3])

    def _device_watch(self):
        try:
            now = time.monotonic()
            if now-self.watch_last > 15:
                self.resume_pending = True
                if self.audio_panel.running:
                    self.audio_panel.start()
            self.watch_last = now
            if not self.busy and not self.closing:
                if self.resume_pending and self.connected:
                    self.resume_pending = False
                    self._lost_device('系统或 USB 状态变化，重新核对音箱状态…')
                else:
                    if not self.connected:
                        self.resume_pending = False
                    try:
                        available = self.device_scan.get_nowait()
                    except queue.Empty:
                        available = None
                    if available is False and self.connected:
                        self._lost_device('USB 已断开，草稿已保留。')
                    elif (available is True and not self.connected and self.want_connection
                          and now >= self.next_reconnect):
                        self.next_reconnect = now+10
                        self.connect()
                self._remember_environment()
            if not self.scan_running and (self.connected or self.want_connection):
                self.scan_running = True
                def scan():
                    try:
                        from winmidi import devices
                        found = devices()
                        available = all(sum('NUX AXON' in d['name'].upper() for d in found[k]) == 1
                                        for k in ('inputs', 'outputs'))
                        try:
                            self.device_scan.put_nowait(available)
                        except queue.Full:
                            pass
                    except OSError:
                        pass
                    finally:
                        self.scan_running = False
                threading.Thread(target=scan, name='AXON USB discovery', daemon=True).start()
        except (OSError, ValueError) as error:
            self.status.set(f'后台状态检查：{error}')
        finally:
            self.root.after(3000, self._device_watch)

    def _lost_device(self, message):
        self.want_connection = self.desktop.auto_reconnect
        if self.current is not None:
            self._write_session(self.recovery_store)
        def done(_):
            self.connected = False
            self.offline = self.current is not None
            self.audition = None
            self.needs_readback = True
            self.device_label.set('○  离线编辑' if self.offline else '未连接')
            self.status.set(message)
            self._changed(record=False)
        self._run(message, self.client.close, done, allow_recovery=True)

    def preset_library(self):
        if self.library_window and self.library_window.winfo_exists():
            self.library_window.lift()
            return
        win = self.library_window = tk.Toplevel(self.root)
        win.title('AXON Control · 本地预设库')
        win.configure(bg=PANEL)
        win.geometry('760x590')
        win.minsize(690, 530)
        body = tk.Frame(win, bg=PANEL, padx=18, pady=18)
        body.pack(fill='both', expand=True)
        query, category, favorite = tk.StringVar(), tk.StringVar(), tk.BooleanVar()
        name, group = tk.StringVar(), tk.StringVar()
        status = tk.StringVar(value='载入仅改变本地预览；点击主窗口“应用试听”才写入音箱。')
        filters = tk.Frame(body, bg=PANEL)
        filters.pack(fill='x')
        tk.Label(filters, text='搜索', bg=PANEL, fg=TEXT).pack(side='left')
        tk.Entry(filters, textvariable=query, width=22).pack(side='left', padx=8)
        categories = ttk.Combobox(filters, textvariable=category, width=12, state='readonly')
        categories.pack(side='left')
        tk.Checkbutton(filters, text='只看收藏', variable=favorite, bg=PANEL, fg=TEXT,
                       selectcolor=SURFACE).pack(side='left', padx=12)
        listing = tk.Listbox(body, bg=SURFACE, fg=TEXT, selectbackground='#465b43',
                             exportselection=False, height=8)
        listing.pack(fill='both', expand=True, pady=12)
        detail = tk.StringVar(value='选择预设查看七个频段的参数。')
        tk.Label(body, textvariable=detail, bg=PANEL, fg=MUTED, justify='left',
                 font=('Consolas', 10), anchor='w').pack(fill='x')
        edits = tk.Frame(body, bg=PANEL)
        edits.pack(fill='x', pady=10)
        for label, variable in (('名称', name), ('分类', group)):
            tk.Label(edits, text=label, bg=PANEL, fg=TEXT).pack(side='left', padx=(0, 6))
            tk.Entry(edits, textvariable=variable, width=24).pack(side='left', padx=(0, 12))
        shown = []
        def selected():
            if not listing.curselection():
                raise ValueError('请先选择一项预设')
            return shown[listing.curselection()[0]]
        def refresh(*_):
            try:
                all_items = self.library.load()
                categories['values'] = ['']+sorted({i['category'] for i in all_items if i['category']})
                shown[:] = self.library.search(query.get(), category.get(), favorite.get())
                listing.delete(0, 'end')
                for item in shown:
                    listing.insert('end', f'{"★" if item.get("favorite") else "☆"}  {item["name"]}   ·  {item["category"] or "未分类"}')
            except (ValueError, OSError) as error:
                status.set(str(error))
        def preview(_=None):
            try:
                item = selected()
                name.set(item['name'])
                group.set(item['category'])
                preset = Preset(bytes.fromhex(item['document']['current']['raw_hex']))
                detail.set('\n'.join(f'{label:3}  {band.frequency:8g} Hz   Q {band.q:5g}   {band.gain:+5g} dB   {"ON" if band.enabled else "OFF"}'
                                     for label, band in zip(BANDS, preset.bands)))
            except (ValueError, KeyError) as error:
                status.set(str(error))
        def action(kind):
            try:
                if kind == 'save':
                    if self.current is None:
                        raise ValueError('请先连接设备或打开备份')
                    document = dict(self.snapshot, active_slot=self.slot, current=self.target().as_dict())
                    self.library.save(name.get(), group.get(), document)
                else:
                    item = selected()
                    if kind == 'load':
                        if self.busy:
                            raise ValueError('请等待当前设备操作完成')
                        preset = Preset(bytes.fromhex(item['document']['current']['raw_hex']))
                        if self.current is None:
                            self.offline = True
                            self._snapshot(item['document'])
                            self.device_label.set('○  离线编辑')
                        else:
                            self.history.end_group()
                            self._set_form(preset_form(editable_import(self.current, preset)))
                            self._changed()
                            self.history.end_group()
                        self.status.set('已载入预设库到本地预览；尚未写入音箱。')
                        status.set('已载入，可在主窗口预览曲线。')
                        return
                    if kind == 'edit':
                        self.library.update(item['id'], name=name.get().strip(), category=group.get().strip())
                    elif kind == 'favorite':
                        self.library.update(item['id'], favorite=not item.get('favorite', False))
                    elif kind == 'delete':
                        if not messagebox.askyesno('删除本地预设', f'删除“{item["name"]}”？不会修改音箱。', parent=win):
                            return
                        self.library.delete(item['id'])
                refresh()
                status.set('预设库已保存。')
            except (ValueError, OSError, KeyError, StopIteration) as error:
                status.set(str(error))
        buttons = tk.Frame(body, bg=PANEL)
        buttons.pack(fill='x')
        for caption, kind in (('保存当前参数', 'save'), ('载入预览', 'load'), ('修改名称/分类', 'edit'),
                              ('切换收藏', 'favorite'), ('删除', 'delete')):
            Button(buttons, caption, lambda k=kind: action(k), width=124, height=34).pack(side='left', padx=2)
        tk.Label(body, textvariable=status, bg=PANEL, fg=ACCENT, wraplength=700, justify='left').pack(fill='x', pady=(12, 0))
        listing.bind('<<ListboxSelect>>', preview)
        for variable in (query, category, favorite):
            variable.trace_add('write', refresh)
        refresh()
