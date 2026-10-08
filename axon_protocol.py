"""Independently implemented AXON 3 USB-MIDI protocol. MIT license.

Only commands identified from AXON STUDIO behavior and protocol observations
are supported. See docs/protocol.md for the evidence and validation limits.
No firmware update or unclassified CMD 07 action is implemented.
"""
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
import json
import math
from pathlib import Path
import struct
import threading
import time

PREFIX = bytes((0xf0, 0x43, 0x58, 0x70))
BANDS = ('HP', 'LF', 'LMF', 'MF', 'HMF', 'HF', 'LP')
FIELD_IDS = {'enabled': 0, 'frequency': 1, 'q': 2, 'gain': 3}
LOCAL_TZ = timezone(timedelta(hours=8))
READ_TIMEOUT = 1.0
READ_ATTEMPTS = 2


class ReadCancelled(RuntimeError):
    def __init__(self):
        super().__init__('已取消读取；本地编辑保留。')


class WriteUnconfirmed(RuntimeError):
    """An action may have reached the speaker; never automatically repeat it."""
    def __init__(self, message, *, snapshot=None, backup=None):
        super().__init__(message)
        self.snapshot, self.backup = snapshot, backup


def state_payload(state, *, slot=None, eq_enabled=None, reset=False, save=False):
    """Build fresh status bytes exactly as the original writer does."""
    if (len(state) != 8 or not 1 <= state[0] <= 32 or state[1] not in (0, 1)
            or not 0 <= state[2] < state[0] or any(byte > 127 for byte in state)):
        raise ValueError('音箱状态与已验证的格式不符')
    if reset and save:
        raise ValueError('重置与保存不能同时执行')
    slot = state[2] if slot is None else slot
    eq_enabled = state[1] if eq_enabled is None else eq_enabled
    if type(slot) is not int or not 0 <= slot < state[0] or eq_enabled not in (0, 1):
        raise ValueError('预设槽位或 EQ 状态不正确')
    # Readback flags are actions, not persistent settings. Never echo them or
    # the unknown trailing bytes into a new request.
    return bytes((state[0], int(eq_enabled), slot, int(bool(reset)), int(bool(save)), 0, 0, 0))


def name_bytes(name):
    if not isinstance(name, str) or name != name.strip() or not 1 <= len(name) <= 14:
        raise ValueError('名称需为 1–14 个英文字符，首尾不能有空格')
    if any(not 32 <= ord(char) <= 126 for char in name):
        raise ValueError('名称仅支持英文、数字、空格和英文符号')
    return name.encode('ascii').ljust(14, b'\0')


def rename_frame(slot, name):
    if type(slot) is not int or not 0 <= slot < 32:
        raise ValueError('预设槽位不正确')
    return frame(0x12, 1, bytes((slot,)) + name_bytes(name))

def encode_words(raw):
    raw = bytes(raw)
    if len(raw) % 2:
        raise ValueError('The AXON word payload must have an even length')
    result = bytearray()
    for offset in range(0, len(raw), 2):
        word = int.from_bytes(raw[offset:offset+2], 'big')
        result.extend((word >> 14, (word >> 7) & 127, word & 127))
    return bytes(result)

def decode_words(payload):
    payload = bytes(payload)
    if len(payload) % 3:
        raise ValueError('Invalid AXON word payload length')
    result = bytearray()
    for offset in range(0, len(payload), 3):
        high, middle, low = payload[offset:offset+3]
        if high > 3 or middle > 127 or low > 127:
            raise ValueError('Invalid AXON word encoding')
        word = (high << 14) | (middle << 7) | low
        result.extend(word.to_bytes(2, 'big'))
    return bytes(result)

def frame(command, mode, data=b''):
    if any(byte > 127 for byte in bytes((command, mode)) + bytes(data)):
        raise ValueError('MIDI data bytes must fit in seven bits')
    return PREFIX + bytes((command, mode)) + bytes(data) + b'\xf7'

@dataclass(frozen=True)
class Band:
    enabled: bool
    reserved: int
    frequency: float
    q: float
    gain: float

@dataclass(frozen=True)
class Preset:
    raw: bytes

    def __post_init__(self):
        object.__setattr__(self, 'raw', bytes(self.raw))
        if len(self.raw) != 126:
            raise ValueError('AXON 3 preset size is not 126 bytes')
        for index in range(7):
            enabled, reserved, frequency, q, gain = struct.unpack_from('<HHfff', self.raw, 14 + 16*index)
            if enabled not in (0, 1) or not all(math.isfinite(v) for v in (frequency, q, gain)):
                raise ValueError('Invalid AXON 3 preset data')

    @property
    def name(self):
        return self.raw[:14].split(b'\0', 1)[0].decode('utf-8', errors='replace')

    @property
    def bands(self):
        return tuple(Band(bool(e), r, f, q, g) for e, r, f, q, g in
                     (struct.unpack_from('<HHfff', self.raw, 14 + 16*i) for i in range(7)))

    def with_field(self, index, field, value):
        validate_value(index, field, value)
        raw = bytearray(self.raw)
        offset = 14 + index*16
        if field == 'enabled':
            struct.pack_into('<H', raw, offset, int(bool(value)))
        else:
            struct.pack_into('<f', raw, offset + FIELD_IDS[field]*4, float(value))
        return Preset(bytes(raw))

    def field_bytes(self, index, field):
        offset = 14 + index*16 + FIELD_IDS[field]*4
        return self.raw[offset:offset+4]

    def as_dict(self):
        return {'name': self.name, 'raw_hex': self.raw.hex(),
                'bands': [dict(label=label, enabled=band.enabled, reserved=band.reserved,
                               frequency=band.frequency, q=band.q, gain=band.gain)
                          for label, band in zip(BANDS, self.bands)]}

def validate_value(index, field, value):
    if index not in range(7) or field not in FIELD_IDS:
        raise ValueError('Unknown AXON band or parameter')
    if field == 'enabled':
        if value not in (False, True, 0, 1):
            raise ValueError('Enabled must be 0 or 1')
        return
    value = float(value)
    if not math.isfinite(value):
        raise ValueError('参数必须是有限数值')
    if field == 'frequency' and not 20 <= value <= 20000:
        raise ValueError('频率范围：20–20000 Hz')
    if field == 'q' and (index in (0, 6) or not 0.1 <= value <= 10):
        raise ValueError('Q 值范围：0.1–10；HP/LP 仅支持调整频率')
    if field == 'gain' and (index in (0, 6) or not -12 <= value <= 12):
        raise ValueError('增益范围：−12–12 dB；HP/LP 没有增益参数')

def parameter_frame(preset, index, field):
    return frame(0x0c, 1, bytes((index, FIELD_IDS[field])) + encode_words(preset.field_bytes(index, field)))

class AxonClient:
    def __init__(self, backup_dir=None):
        self.port = None
        self.lock = threading.RLock()
        self.stream = bytearray()
        self.device_info = None
        self.backup_dir = Path(backup_dir or Path(__file__).resolve().parent / 'backups')
        self.cancelled = threading.Event()
        self._preset_cache = {}
        self._cache_count = None

    def begin_operation(self):
        self.cancelled.clear()

    def cancel_read(self):
        # Does not acquire the MIDI lock: the UI can cancel a waiting query.
        # Callers expose cancellation only for operations that contain no write.
        self.cancelled.set()

    def _check_cancelled(self):
        if self.cancelled.is_set():
            raise ReadCancelled()

    def connect(self):
        from winmidi import MidiPort, devices
        with self.lock:
            if self.port is not None:
                raise RuntimeError('音箱已经连接，请先断开再连接。')
            self._check_cancelled()
            self.stream.clear()
            self._preset_cache.clear()
            self._cache_count = None
            available = devices()
            matching = {key: [device for device in available[key]
                              if 'NUX AXON' in device['name'].upper()] for key in ('inputs', 'outputs')}
            if any(len(matching[key]) != 1 for key in matching):
                raise RuntimeError('需要恰好一台通过 USB 连接的 AXON 3。请检查 USB 连接。')
            try:
                self.port = MidiPort(matching['inputs'][0]['id'], matching['outputs'][0]['id'])
                for attempt in range(2):
                    try:
                        identity = self._exchange(bytes.fromhex('f0 43 58 00 f7'),
                                                  lambda packet: packet[:4] == bytes.fromhex('f0 43 58 10'),
                                                  timeout=READ_TIMEOUT)
                        break
                    except TimeoutError:
                        if attempt == 1:
                            raise
                model = identity[36:44].split(b'\0')[0].decode('ascii', errors='replace')
                if len(identity) != 45 or model != 'NFM-3':
                    raise RuntimeError('设备身份与已验证的 AXON 3 不符，已停止连接。')
                self.device_info = {'name': matching['inputs'][0]['name'], 'identity_hex': identity.hex(' '),
                                    'model': model, 'identity_attempts': attempt + 1,
                                    'version_field': identity[4:12].decode('ascii', errors='replace'),
                                    'fm_version_raw': identity[28]}
                return self.snapshot()
            except Exception:
                self.close()
                raise

    def _packets(self, chunk):
        self.stream.extend(chunk)
        packets = []
        while self.stream:
            try:
                start = self.stream.index(0xf0)
            except ValueError:
                self.stream.clear()
                break
            if start:
                del self.stream[:start]
            try:
                end = self.stream.index(0xf7, 1)
            except ValueError:
                if len(self.stream) > 65536:
                    self.stream.clear()
                    raise RuntimeError('Unexpected MIDI stream length')
                break
            packets.append(bytes(self.stream[:end+1]))
            del self.stream[:end+1]
        return packets

    def _exchange(self, message, accept, timeout=3):
        if self.port is None:
            raise RuntimeError('请先连接音箱')
        self._check_cancelled()
        # Drop old responses before starting a new serial request.
        drain_deadline = time.monotonic() + 0.1
        for _ in range(256):
            self._check_cancelled()
            if self.port.receive(timeout=0) is None:
                break
            if time.monotonic() >= drain_deadline:
                raise RuntimeError('MIDI 消息持续涌入，请关闭其他控制软件后重新连接。')
        else:
            raise RuntimeError('MIDI 消息过多，请关闭其他控制软件后重新连接。')
        self.stream.clear()
        self._check_cancelled()
        self.port.send(message)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._check_cancelled()
            chunk = self.port.receive(timeout=min(0.05, max(0, deadline-time.monotonic())))
            if chunk:
                for packet in self._packets(chunk):
                    if accept(packet):
                        return packet
        raise TimeoutError('音箱响应超时。请关闭官方软件，并检查 USB 连接。')

    def _query(self, message, accept):
        """Only side-effect-free requests can be retried."""
        for attempt in range(READ_ATTEMPTS):
            self._check_cancelled()
            try:
                return self._exchange(message, accept, timeout=READ_TIMEOUT)
            except TimeoutError:
                if attempt + 1 == READ_ATTEMPTS:
                    raise

    def state(self):
        with self.lock:
            packet = self._query(frame(0x15, 0), lambda p: p[:6] == PREFIX + b'\x15\x02' and len(p) == 15)
            state = packet[6:-1]
            if not (1 <= state[0] <= 32 and state[2] < state[0] and state[1] in (0, 1)):
                raise RuntimeError('音箱状态与已验证的格式不符')
            return state

    def current(self):
        with self.lock:
            packet = self._query(frame(0x0c, 0, b'\x00'),
                                    lambda p: p[:7] == PREFIX + b'\x0c\x02\x00' and len(p) == 197)
            return Preset(decode_words(packet[7:-1]))

    def preset(self, slot):
        if not 0 <= slot < 32:
            raise ValueError('Invalid preset slot')
        with self.lock:
            packet = self._query(frame(0x0b, 0, bytes((slot,))),
                                    lambda p: p[:7] == PREFIX + bytes((0x0b, 2, slot)) and len(p) == 197)
            result = Preset(decode_words(packet[7:-1]))
            self._preset_cache[slot] = result
            return result

    def snapshot(self, *, refresh_presets=True):
        with self.lock:
            state = self.state()
            full = (refresh_presets or self._cache_count != state[0]
                    or any(index not in self._preset_cache for index in range(state[0])))
            if full:
                presets = [self.preset(index) for index in range(state[0])]
            else:
                self._preset_cache[state[2]] = self.preset(state[2])
                presets = [self._preset_cache[index] for index in range(state[0])]
            latest, current = self._checked_current(state[2])
            if latest[:2] != state[:2]:
                raise RuntimeError('读取期间音箱状态已改变，请重新读取。')
            state = latest
            self._preset_cache = dict(enumerate(presets))
            self._cache_count = len(presets)
            return {'device': self.device_info, 'state_hex': state.hex(), 'active_slot': state[2],
                    'eq_enabled': bool(state[1]), 'current': current.as_dict(),
                    'presets': [preset.as_dict() for preset in presets],
                    'preset_read_scope': 'all' if full else 'active-and-cache',
                    'cached_slots': [] if full else [i for i in range(state[0]) if i != state[2]]}

    def backup(self, reason, *, refresh_presets=True):
        with self.lock:
            return self.backup_snapshot(self.snapshot(refresh_presets=refresh_presets), reason)

    def backup_snapshot(self, snapshot, reason):
        """Persist an already-read connection snapshot without a second USB scan."""
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(LOCAL_TZ).strftime('%Y%m%d-%H%M%S-%f')
        path = self.backup_dir / (stamp + '-' + reason + '.json')
        path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding='utf-8')
        return path

    def _set_parameter(self, target, index, field):
        packet = self._exchange(parameter_frame(target, index, field),
                                lambda p: p[:7] == PREFIX + bytes((0x0c, 3, index)) and len(p) == 15)
        if any(packet[7:-1]):
            raise RuntimeError('音箱拒绝了参数修改')

    def _slot_state(self, expected_slot):
        state = self.state()
        if state[2] != expected_slot:
            raise RuntimeError('音箱预设槽位已改变，请重新读取后再操作。')
        return state

    def _checked_current(self, expected_slot=None, expected=None):
        """Bracket a read with slot checks, including byte-identical presets."""
        state = self.state()
        slot = state[2] if expected_slot is None else expected_slot
        if state[2] != slot:
            raise RuntimeError('音箱预设槽位已改变，请重新读取后再操作。')
        actual = self.current()
        state = self._slot_state(slot)
        if expected is not None and actual.raw != expected.raw:
            raise RuntimeError('音箱参数已发生变化，请重新读取后再操作。')
        return state, actual

    def apply(self, base, target, *, expected_slot=None):
        with self.lock:
            if base.name != target.name or base.raw[:14] != target.raw[:14]:
                raise ValueError('参数应用不能修改名称，请使用“重命名当前预设”')
            state, actual = self._checked_current(expected_slot, base)
            slot = state[2]
            changes = []
            for index in range(7):
                if target.bands[index].reserved != base.bands[index].reserved:
                    raise ValueError('不能修改未验证的内部字段')
                for field in FIELD_IDS:
                    if index in (0, 6) and field in ('q', 'gain'):
                        if target.field_bytes(index, field) != base.field_bytes(index, field):
                            raise ValueError('HP/LP 的内部参数不能修改')
                        continue
                    if target.field_bytes(index, field) != base.field_bytes(index, field):
                        band = target.bands[index]
                        validate_value(index, field, getattr(band, field))
                        changes.append((index, field))
            if not changes:
                return actual
            self.backup('before-apply', refresh_presets=False)
            self._checked_current(slot, base)
            sent = []
            try:
                for index, field in changes:
                    self._slot_state(slot)
                    # Include a possibly-applied timed-out command in rollback.
                    sent.append((index, field))
                    self._set_parameter(target, index, field)
                _, actual = self._checked_current(slot, target)
                return actual
            except Exception as error:
                if not sent:
                    raise
                try:
                    for index, field in reversed(sent):
                        # Never restore fields onto a different active slot.
                        self._slot_state(slot)
                        self._set_parameter(base, index, field)
                    self._checked_current(slot, base)
                except Exception as recovery_error:
                    raise RuntimeError(f'修改失败，恢复也未确认：{error}；{recovery_error}') from error
                raise RuntimeError(f'修改未完成，已恢复原参数：{error}') from error

    def _write_state(self, state):
        packet = self._exchange(frame(0x15, 1, state),
                                lambda p: p[:6] == PREFIX + b'\x15\x03' and len(p) == 15)
        return packet[6:-1]

    def select(self, slot, *, expected_slot=None, expected=None):
        with self.lock:
            state, current = self._checked_current(expected_slot, expected)
            if not 0 <= slot < state[0]:
                raise ValueError('Invalid preset slot')
            if slot != state[2]:
                self.backup('before-select', refresh_presets=False)
                state, _ = self._checked_current(state[2], current)
                try:
                    response = self._write_state(state_payload(state, slot=slot))
                except TimeoutError:
                    # A lost ACK must not cause another switch or flag replay.
                    pass
                else:
                    if response[2] != slot:
                        raise RuntimeError('预设切换未得到确认')
                _, current = self._checked_current(slot)
            return current

    def save(self, expected, *, expected_slot=None):
        with self.lock:
            state, _ = self._checked_current(expected_slot, expected)
            slot = state[2]
            self.backup('before-save')
            state, _ = self._checked_current(slot, expected)
            try:
                response = self._write_state(state_payload(state, save=True))
            except TimeoutError:
                pass  # Verify saved bytes below; never send a second save.
            else:
                if response[2] != slot:
                    raise RuntimeError('预设保存未得到确认，请重新读取。')
            self._slot_state(slot)
            saved = self.preset(slot)
            self._checked_current(slot, expected)
            if saved.raw != expected.raw:
                raise RuntimeError('保存后的预设与当前参数不一致')
            self._preset_cache[slot] = saved
            return saved

    def set_eq(self, enabled, *, expected_slot=None, expected=None, expected_enabled=None):
        if type(enabled) is not bool:
            raise ValueError('EQ 开关值不正确')
        with self.lock:
            state, current = self._checked_current(expected_slot, expected)
            slot = state[2]
            if expected_enabled is not None and bool(state[1]) != expected_enabled:
                raise RuntimeError('音箱 EQ 状态已改变，请重新读取。')
            if bool(state[1]) == enabled:
                return state, current
            self.backup('before-eq', refresh_presets=False)
            state, _ = self._checked_current(slot, current)
            if expected_enabled is not None and bool(state[1]) != expected_enabled:
                raise RuntimeError('音箱 EQ 状态已改变，请重新读取。')
            try:
                response = self._write_state(state_payload(state, eq_enabled=enabled))
            except TimeoutError:
                pass
            else:
                if response[2] != slot or bool(response[1]) != enabled:
                    raise RuntimeError('EQ 切换未得到确认，请重新读取。')
            latest, actual = self._checked_current(slot, current)
            if bool(latest[1]) != enabled:
                raise RuntimeError('EQ 状态读回不一致，请重新读取。')
            return latest, actual

    @staticmethod
    def _action_ack(packet, command):
        # The rename/reset ACK payload is not fully characterized. Treat this
        # only as receipt, then read actual state rather than invent error bits.
        return (len(packet) >= 7 and packet[:6] == PREFIX + bytes((command, 3))
                and packet[-1] == 0xf7 and all(byte <= 127 for byte in packet[4:-1]))

    def rename(self, name, *, expected_slot, expected):
        encoded = name_bytes(name)
        with self.lock:
            state, current = self._checked_current(expected_slot, expected)
            slot = state[2]
            saved = self.preset(slot)
            if current.raw[:14] == saved.raw[:14] == encoded:
                return self.snapshot(refresh_presets=False)
            backup = self.backup('before-rename')
            self._checked_current(slot, current)
            if self.preset(slot).raw != saved.raw:
                raise RuntimeError('已保存预设发生变化，请重新读取。')
            acknowledged = True
            try:
                self._exchange(rename_frame(slot, name), lambda p: self._action_ack(p, 0x12))
            except TimeoutError:
                acknowledged = False
            snapshot = self.snapshot()
            if snapshot['active_slot'] != slot or not slot < len(snapshot['presets']):
                raise WriteUnconfirmed('改名期间音箱槽位已改变，已读取实际状态；请核对后刷新。',
                                       snapshot=snapshot, backup=backup)
            actual = bytes.fromhex(snapshot['current']['raw_hex'])
            stored = bytes.fromhex(snapshot['presets'][slot]['raw_hex'])
            if (snapshot['active_slot'] != slot or actual != encoded + current.raw[14:]
                    or stored != encoded + saved.raw[14:]):
                raise WriteUnconfirmed('名称或参数读回不一致，已读取实际状态。请核对后刷新；未重复改名。',
                                       snapshot=snapshot, backup=backup)
            snapshot['action_acknowledged'] = acknowledged
            return snapshot

    def reset_current(self, *, expected_slot, expected):
        with self.lock:
            state, current = self._checked_current(expected_slot, expected)
            backup = self.backup('before-reset-current')
            state, _ = self._checked_current(state[2], current)
            acknowledged = True
            try:
                response = self._write_state(state_payload(state, reset=True))
                acknowledged = response[2] == state[2]
            except TimeoutError:
                acknowledged = False
            snapshot = self.snapshot()
            if not acknowledged or snapshot['active_slot'] != state[2]:
                raise WriteUnconfirmed('重置结果未得到确认，已读取实际参数；请核对，未重复发送重置。',
                                       snapshot=snapshot, backup=backup)
            return {'snapshot': snapshot, 'backup': backup}

    def reset_all(self, *, expected_slot, expected):
        with self.lock:
            state, current = self._checked_current(expected_slot, expected)
            backup = self.backup('before-reset-all')
            self._checked_current(state[2], current)
            acknowledged = True
            try:
                self._exchange(frame(0x7b, 1, b'\x01'), lambda p: self._action_ack(p, 0x7b))
            except TimeoutError:
                acknowledged = False
            self._preset_cache.clear()
            snapshot = self.snapshot()
            if not acknowledged:
                raise WriteUnconfirmed('全部重置的回执未收到，已读取实际参数；请核对，未重复发送重置。',
                                       snapshot=snapshot, backup=backup)
            return {'snapshot': snapshot, 'backup': backup}

    def close(self):
        with self.lock:
            if self.port is not None:
                self.port.close()
                self.port = None
            self.stream.clear()
            self._preset_cache.clear()
            self._cache_count = None

if __name__ == '__main__':
    client = AxonClient()
    try:
        print(json.dumps(client.connect(), ensure_ascii=False, indent=2))
    finally:
        client.close()
