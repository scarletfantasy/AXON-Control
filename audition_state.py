"""Two verified working presets for temporary A/B listening."""
from dataclasses import dataclass, replace

from axon_protocol import Preset


@dataclass(frozen=True)
class AuditionPair:
    slot: int
    before: Preset
    after: Preset
    side: str = 'B'

    def __post_init__(self):
        if type(self.slot) is not int or not 0 <= self.slot < 32 or self.side not in ('A', 'B'):
            raise ValueError('试听对比状态不正确')
        if self.before.raw[:14] != self.after.raw[:14]:
            raise ValueError('试听对比必须使用同一个预设')

    @property
    def heard(self):
        return self.before if self.side == 'A' else self.after

    def request(self, side, slot, current):
        if side not in ('A', 'B'):
            raise ValueError('请选择 A 或 B')
        if slot != self.slot or current.raw != self.heard.raw:
            raise ValueError('试听参数已改变，请重新读取后再调节')
        return self.before if side == 'A' else self.after

    def confirmed(self, side, readback):
        target = self.request(side, self.slot, self.heard)
        if readback.raw != target.raw:
            raise ValueError('试听切换读回不一致，请重新读取')
        return replace(self, side=side)

    @classmethod
    def applied(cls, previous, slot, base, readback):
        before = base
        if previous and previous.slot == slot and previous.heard.raw == base.raw:
            before = previous.before
        if before.raw == readback.raw:
            return None
        return cls(slot, before, readback)
