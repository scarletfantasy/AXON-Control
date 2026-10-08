"""Choose a read-only curve reference from readback, saved data or verified A/B."""
from dataclasses import dataclass

from axon_protocol import Preset

REFERENCE_CHOICES = ('current', 'saved', 'before', 'after', 'none')


def _pair_matches(pair, current, slot, offline):
    if offline or pair is None or current is None:
        return False
    try:
        pair.request(pair.side, slot, current)
    except ValueError:
        return False
    return True


def available_references(current, saved=None, *, pair=None, slot=None, offline=False):
    if current is None:
        return ('none',)
    choices = ['current']
    if saved is not None:
        choices.append('saved')
    if _pair_matches(pair, current, slot, offline):
        choices.extend(('before', 'after'))
    return tuple(choices) + ('none',)


@dataclass(frozen=True)
class CurveReference:
    key: str
    label: str
    description: str
    preset: Preset | None
    color: str
    dash: tuple = (6, 4)


def resolve_reference(choice, current, saved=None, *, pair=None, slot=None, offline=False):
    if not isinstance(choice, str) or choice not in REFERENCE_CHOICES:
        raise ValueError('曲线对照来源不正确')
    available = available_references(current, saved, pair=pair, slot=slot, offline=offline)
    if choice not in available:
        choice = 'current' if current is not None else 'none'
    if choice == 'none':
        return CurveReference('none', '关闭', '隐藏对照', None, '#89968d')
    if choice == 'saved':
        return CurveReference(choice, '已保存', '当前槽位已保存', saved, '#89968d', (5, 5))
    if choice == 'before':
        return CurveReference(choice, 'A', 'A 调节前', pair.before, '#e6c28a', (3, 4))
    if choice == 'after':
        return CurveReference(choice, 'B', 'B 最近应用', pair.after, '#b7a0dd', (9, 4))
    return CurveReference(choice, '原参数' if offline else '读回',
                          '离线原参数' if offline else '音箱读回参数', current, '#88c5ce')
