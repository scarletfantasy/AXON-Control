"""Durable local editor recovery. Loading a session never connects to MIDI."""
import json
import os
from pathlib import Path
import tempfile

from editor_state import FIELDS, read_document
from curve_view import DRAG_MODES, PLOT_RANGES
from curve_reference import REFERENCE_CHOICES

SESSION_KIND = 'axon-editor-session-v1'


def read_session(data):
    if not isinstance(data, dict) or data.get('kind') != SESSION_KIND:
        raise ValueError('本地编辑记录格式不正确')
    document = read_document(data.get('document'))
    form = data.get('form')
    if not isinstance(form, (list, tuple)) or len(form) != 7:
        raise ValueError('本地参数表结构不正确')
    for row in form:
        if not isinstance(row, (list, tuple)) or len(row) != len(FIELDS):
            raise ValueError('本地参数表结构不正确')
        if type(row[0]) is not bool or any(not isinstance(text, str) or len(text) > 512 for text in row[1:]):
            raise ValueError('本地参数输入格式不正确')
    selected = data.get('selected_band')
    compare = data.get('compare_saved')
    plot_range = data.get('plot_gain_range', 12)
    drag_mode = data.get('plot_drag_mode', 'free')
    reference = data.get('plot_reference', 'saved' if compare is True else 'none')
    if type(selected) is not int or not 0 <= selected < 7 or type(compare) is not bool:
        raise ValueError('本地界面状态不正确')
    if type(plot_range) is not int or plot_range not in PLOT_RANGES:
        raise ValueError('本地曲线纵轴范围不正确')
    if not isinstance(drag_mode, str) or drag_mode not in DRAG_MODES:
        raise ValueError('本地曲线拖动方式不正确')
    if not isinstance(reference, str) or reference not in REFERENCE_CHOICES:
        raise ValueError('本地曲线对照来源不正确')
    return {'kind': SESSION_KIND, 'document': document,
            'form': tuple(tuple(row) for row in form),
            'selected_band': selected, 'compare_saved': reference == 'saved', 'plot_gain_range': plot_range,
            'plot_drag_mode': drag_mode, 'plot_reference': reference}


class SessionStore:
    def __init__(self, path):
        self.path = Path(path)

    def load(self):
        if self.path.stat().st_size > 1024 * 1024:
            raise ValueError('本地编辑记录过大')
        return read_session(json.loads(self.path.read_text(encoding='utf-8-sig')))

    def save(self, document, form, selected_band, compare_saved, *, plot_gain_range=12, plot_drag_mode='free',
             plot_reference=None):
        data = read_session({'kind': SESSION_KIND, 'document': document, 'form': form,
                             'selected_band': selected_band, 'compare_saved': compare_saved,
                             'plot_gain_range': plot_gain_range, 'plot_drag_mode': plot_drag_mode,
                             'plot_reference': plot_reference if plot_reference is not None else
                             'saved' if compare_saved else 'none'})
        text = json.dumps(data, ensure_ascii=False, indent=2) + '\n'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            # Replacing a complete file retains the previous session if writing fails.
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                                             dir=self.path.parent, prefix=self.path.name + '.',
                                             suffix='.tmp', delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(self.path)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()
        return self.path
