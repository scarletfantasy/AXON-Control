"""Small, validated band snippets for local copy and paste; no GUI or MIDI."""
import json

from axon_protocol import BANDS, validate_value
from editor_state import FIELDS, display_float, preset_form, preview_from_form

SNIPPET_KIND = 'axon-band-v1'
MAX_SNIPPET_LENGTH = 4096


def band_kind(index):
    if type(index) is not int or not 0 <= index < 7:
        raise ValueError('频段不正确')
    return 'cutoff' if index in (0, 6) else 'peak'


def read_snippet(text):
    if not isinstance(text, str) or len(text) > MAX_SNIPPET_LENGTH:
        raise ValueError('剪贴板不是有效的 AXON 频段参数')
    try:
        data = json.loads(text)
    except (ValueError, RecursionError) as error:
        raise ValueError('剪贴板不是有效的 AXON 频段参数') from error
    if (not isinstance(data, dict) or set(data) != {'kind', 'source_band', 'band_kind', 'values'}
            or data['kind'] != SNIPPET_KIND or not isinstance(data['source_band'], str)
            or data['source_band'] not in BANDS):
        raise ValueError('剪贴板不是有效的 AXON 频段参数')
    index = BANDS.index(data['source_band'])
    expected_kind = band_kind(index)
    values = data['values']
    fields = ('enabled', 'frequency') if expected_kind == 'cutoff' else FIELDS
    if data['band_kind'] != expected_kind or not isinstance(values, dict) or set(values) != set(fields):
        raise ValueError('频段参数的类型或字段不正确')
    for field, value in values.items():
        if (field == 'enabled' and type(value) is not bool
                or field != 'enabled' and type(value) not in (int, float)):
            raise ValueError('频段参数必须使用有效的数值与开关')
        try:
            validate_value(index, field, value)
        except (ValueError, OverflowError) as error:
            raise ValueError(f'频段参数有误：{error}') from error
    return data


def copy_snippet(base, form, index):
    kind = band_kind(index)
    if base is None:
        raise ValueError('请先打开音箱参数或离线备份')
    if len(form) != 7 or any(len(row) != len(FIELDS) for row in form):
        raise ValueError('参数表结构不正确')
    # Other bands may contain unfinished input; copy only the selected band.
    selected_form = list(preset_form(base))
    selected_form[index] = form[index]
    band = preview_from_form(base, tuple(selected_form)).bands[index]
    fields = ('enabled', 'frequency') if kind == 'cutoff' else FIELDS
    data = {'kind': SNIPPET_KIND, 'source_band': BANDS[index], 'band_kind': kind,
            'values': {field: getattr(band, field) for field in fields}}
    text = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False)
    read_snippet(text)
    return text


def paste_snippet(form, index, text):
    kind = band_kind(index)
    if len(form) != 7 or any(len(row) != len(FIELDS) for row in form):
        raise ValueError('参数表结构不正确')
    data = read_snippet(text)
    if data['band_kind'] != kind:
        raise ValueError('峰值 EQ 与截止频段不能互相粘贴')
    rows = [list(row) for row in form]
    for field, value in data['values'].items():
        rows[index][FIELDS.index(field)] = value if field == 'enabled' else display_float(value)
    return tuple(tuple(row) for row in rows), data['source_band']
