"""Local editing, unit parsing and history. No MIDI operations in this module."""
from decimal import Decimal
from dataclasses import dataclass
import math
import re
import struct
import time

from axon_protocol import BANDS, Preset

FIELDS = ('enabled', 'frequency', 'q', 'gain')
FIELD_LABELS = {'enabled': '开关', 'frequency': '频率', 'q': 'Q 值', 'gain': '增益'}
INPUT_HINTS = {'frequency': '20–20000 Hz · 可输入 1k', 'q': '0.1–10', 'gain': '−12–12 dB'}
NUMBER = re.compile(r'([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?)\s*([a-z]*)', re.I)


def display_float(value):
    """Shortest decimal that preserves the device's original float32 value."""
    encoded = struct.pack('<f', value)
    for precision in range(1, 10):
        text = format(value, f'.{precision}g')
        if struct.pack('<f', float(text)) == encoded:
            return format(Decimal(text), 'f')
    return format(Decimal(format(value, '.9g')), 'f')


def parse_parameter(text, field):
    match = NUMBER.fullmatch(str(text).strip().replace('−', '-'))
    if match is None:
        raise ValueError('请输入有效数值')
    number, unit = match.groups()
    unit = unit.lower()
    units = {'frequency': {'': 1, 'hz': 1, 'k': 1000, 'khz': 1000},
             'q': {'': 1, 'q': 1}, 'gain': {'': 1, 'db': 1}}
    if unit not in units.get(field, {}):
        raise ValueError('数值单位不正确')
    value = float(number) * units[field][unit]
    if not math.isfinite(value):
        raise ValueError('请输入有限数值')
    return value


class FieldError(ValueError):
    def __init__(self, index, field, message):
        self.index, self.field = index, field
        self.short_message = f'{BANDS[index]} · {FIELD_LABELS[field]}有误'
        super().__init__(f'{BANDS[index]} · {FIELD_LABELS[field]}：{message}')


def preset_form(preset):
    return tuple(tuple(getattr(band, field) if field == 'enabled' else display_float(getattr(band, field))
                       for field in FIELDS) for band in preset.bands)


def preview_from_form(base, form):
    if base is None:
        return None
    if len(form) != len(base.bands) or any(len(row) != len(FIELDS) for row in form):
        raise ValueError('参数表结构不正确')
    result = base
    for index, band in enumerate(base.bands):
        for column, field in enumerate(FIELDS):
            if index in (0, 6) and field in ('q', 'gain'):
                continue
            text = form[index][column]
            if field != 'enabled' and str(text).strip() == display_float(getattr(band, field)):
                continue
            try:
                value = text if field == 'enabled' else parse_parameter(text, field)
                result = result.with_field(index, field, value)
            except (ValueError, TypeError, OverflowError) as error:
                raise FieldError(index, field, str(error)) from error
    return result


def changed_fields(base, preview):
    if base is None or preview is None:
        return ()
    return tuple((index, field) for index in range(len(base.bands)) for field in FIELDS
                 if not (index in (0, 6) and field in ('q', 'gain'))
                 and base.field_bytes(index, field) != preview.field_bytes(index, field))


@dataclass(frozen=True)
class ReviewItem:
    index: int
    field: str
    before: str
    after: str
    error: str = ''


@dataclass(frozen=True)
class FormReview:
    preview: Preset | None
    items: tuple

    @property
    def error_count(self):
        return sum(bool(item.error) for item in self.items)


def parameter_text(value, field):
    if field == 'enabled':
        return '已启用' if value else '已关闭'
    return display_float(value) + {'frequency': ' Hz', 'q': ' Q', 'gain': ' dB'}[field]


def review_form(current, form, *, preview_base=None):
    """Review every field independently; invalid input never becomes applicable."""
    if current is None:
        return FormReview(None, ())
    preview = current if preview_base is None else preview_base
    if current.raw[:14] != preview.raw[:14]:
        raise ValueError('比较参数必须来自同一个预设')
    if len(form) != len(preview.bands) or any(len(row) != len(FIELDS) for row in form):
        raise ValueError('参数表结构不正确')
    items = []
    for index, band in enumerate(preview.bands):
        for column, field in enumerate(FIELDS):
            if index in (0, 6) and field in ('q', 'gain'):
                continue
            text = form[index][column]
            before = parameter_text(getattr(current.bands[index], field), field)
            try:
                unchanged = field != 'enabled' and str(text).strip() == display_float(getattr(band, field))
                if not unchanged:
                    value = text if field == 'enabled' else parse_parameter(text, field)
                    preview = preview.with_field(index, field, value)
            except (ValueError, TypeError, OverflowError) as error:
                items.append(ReviewItem(index, field, before, str(text).strip() or '空', str(error)))
                continue
            if current.field_bytes(index, field) != preview.field_bytes(index, field):
                after = parameter_text(getattr(preview.bands[index], field), field)
                items.append(ReviewItem(index, field, before, after))
    return FormReview(preview, tuple(items))


def editable_import(base, imported):
    """Import only verified editable fields, retaining this device's internal bytes."""
    if base.raw[:14] != imported.raw[:14]:
        raise ValueError('预设名称不匹配。请先选择相同的预设。')
    result = base
    for index, field in changed_fields(base, imported):
        result = result.with_field(index, field, getattr(imported.bands[index], field))
    return result


def rebase_preview(source, preview, device):
    """Continue a local draft on a fresh read; reject conflicting external changes."""
    if source.raw[:14] != device.raw[:14]:
        raise ValueError('音箱当前预设与草稿不同')
    result = device
    for index, field in changed_fields(source, preview):
        actual = device.field_bytes(index, field)
        if actual not in (source.field_bytes(index, field), preview.field_bytes(index, field)):
            raise FieldError(index, field, '音箱中的此项已改变，请重新核对草稿')
        result = result.with_field(index, field, getattr(preview.bands[index], field))
    return result


def read_document(data):
    """Normalize backup/draft data before it becomes an editable offline document."""
    device = data.get('device') if isinstance(data, dict) else None
    if not isinstance(device, dict) or device.get('model') != 'NFM-3':
        raise ValueError('此文件不是 AXON 3 参数备份或草稿')
    entries, slot = data.get('presets'), data.get('active_slot')
    if not isinstance(entries, list) or not 1 <= len(entries) <= 32:
        raise ValueError('预设列表不正确')
    if type(slot) is not int or not 0 <= slot < len(entries):
        raise ValueError('当前预设槽位不正确')
    if type(data.get('eq_enabled')) is not bool:
        raise ValueError('EQ 状态不正确')
    try:
        current = Preset(bytes.fromhex(data['current']['raw_hex']))
        presets = [Preset(bytes.fromhex(entry['raw_hex'])).as_dict() for entry in entries]
    except (ValueError, KeyError, TypeError) as error:
        raise ValueError('参数数据损坏') from error
    # Rename behavior can differ between working and stored copies. Preserve
    # both observed values so even an unconfirmed rename can be backed up and
    # recovered locally. Import/rebase still require matching working names.
    return {'device': {'name': 'NUX AXON-3', 'model': 'NFM-3'},
            'active_slot': slot, 'eq_enabled': data['eq_enabled'],
            'current': current.as_dict(), 'presets': presets,
            'state_hex': data.get('state_hex', '')}


def offline_document(data):
    """Keep an exported draft's source separate from its pending preview."""
    document = read_document(data)
    kind = data.get('kind', '')
    if kind not in ('axon-local-draft-v1', 'axon-local-draft-v2'):
        if isinstance(kind, str) and kind.startswith('axon-local-draft-'):
            raise ValueError('此草稿版本暂不支持')
        return document, None
    if kind == 'axon-local-draft-v2':
        try:
            base = Preset(bytes.fromhex(data['source_current']['raw_hex']))
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError('草稿原参数损坏') from error
    else:
        # Older drafts lack a working-parameter source. Use their saved preset
        # as a conservative baseline; reconnect still rejects changed fields.
        base = Preset(bytes.fromhex(document['presets'][document['active_slot']]['raw_hex']))
    preview = editable_import(base, Preset(bytes.fromhex(document['current']['raw_hex'])))
    return dict(document, current=base.as_dict()), preview


class EditHistory:
    """Bounded undo/redo with typing and drag coalescing; states are immutable tuples."""
    def __init__(self, limit=100, coalesce_seconds=1.2):
        if limit < 1:
            raise ValueError('History limit must be positive')
        self.limit, self.coalesce_seconds = limit, coalesce_seconds
        self.reset(None)

    def reset(self, state):
        self.current = state
        self._undo, self._redo = [], []
        self.end_group()

    @property
    def can_undo(self):
        return bool(self._undo)

    @property
    def can_redo(self):
        return bool(self._redo)

    def end_group(self):
        self._last_group, self._last_time = None, None

    def record(self, state, group=None, now=None, *, continuous=False):
        if self.current is None:
            self.reset(state)
            return False
        if state == self.current:
            return False
        now = time.monotonic() if now is None else now
        coalescing = (group is not None and group == self._last_group and self._undo
                      and self._last_time is not None
                      and (continuous or 0 <= now-self._last_time <= self.coalesce_seconds))
        if not coalescing:
            self._undo.append(self.current)
            self._undo = self._undo[-self.limit:]
        self.current = state
        self._redo.clear()
        self._last_group, self._last_time = group, now
        if coalescing and self._undo[-1] == state:
            self._undo.pop()
            self.end_group()
        return True

    def undo(self):
        if not self.can_undo:
            return None
        self._redo.append(self.current)
        self.current = self._undo.pop()
        self.end_group()
        return self.current

    def redo(self):
        if not self.can_redo:
            return None
        self._undo.append(self.current)
        self.current = self._redo.pop()
        self.end_group()
        return self.current
