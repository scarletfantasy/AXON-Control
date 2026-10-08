"""Offline tests: synthetic protocol packets and write-failure recovery.

Run: python -m unittest -v tests_protocol
These tests never open a MIDI port or modify a speaker.
"""
from pathlib import Path
import struct
import unittest

from axon_protocol import AxonClient, Preset, decode_words, encode_words, parameter_frame
from axon_control import App, band_response, display_float, drag_values

from synthetic_fixtures import ORIGINAL, PACKETS


class MemoryDevice(AxonClient):
    def __init__(self, fail_on=None):
        super().__init__()
        self.state_data = bytearray((10, 1, 0, 0, 0, 0, 0, 0))
        self.working = [ORIGINAL] * 10
        self.saved = [ORIGINAL] * 10
        self.actual = ORIGINAL
        self.writes = []
        self.parameter_slots = []
        self.state_writes = []
        self.backups = 0
        self.fail_on = fail_on
        self.on_backup = self.on_current = self.on_write = None

    @property
    def active(self):
        return self.state_data[2]

    @active.setter
    def active(self, slot):
        self.state_data[2] = slot

    @property
    def actual(self):
        return self.working[self.active]

    @actual.setter
    def actual(self, preset):
        self.working[self.active] = preset

    def state(self):
        return bytes(self.state_data)

    def current(self):
        result = self.actual
        if self.on_current:
            self.on_current(self)
        return result

    def preset(self, slot):
        return self.saved[slot]

    def backup(self, reason, *, refresh_presets=True):
        self.backups += 1
        if self.on_backup:
            self.on_backup(self)
        return Path('memory.json')

    def _write_state(self, state):
        self.state_writes.append(bytes(state))
        if state[2] != self.active:
            self.active = state[2]
            self.actual = self.saved[self.active]
        self.state_data[:] = state
        if state[4] == 1:
            self.saved[self.active] = self.actual
        return bytes(state)

    def _set_parameter(self, target, index, field):
        raw = bytearray(self.actual.raw)
        from axon_protocol import FIELD_IDS
        offset = 14 + index*16 + FIELD_IDS[field]*4
        raw[offset:offset+4] = target.field_bytes(index, field)
        self.actual = Preset(raw)
        self.writes.append((index, field))
        self.parameter_slots.append(self.active)
        # Model a write that reaches the device but whose ACK is lost.
        if len(self.writes) == self.fail_on:
            raise TimeoutError('ACK lost after device accepted write')
        if self.on_write:
            self.on_write(self)


class Var:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value


class ProtocolTests(unittest.TestCase):
    def test_synthetic_preset_layout(self):
        self.assertEqual(ORIGINAL.name, 'SyntheticEQ')
        lf = ORIGINAL.bands[1]
        self.assertEqual((lf.frequency, lf.q, lf.gain, lf.enabled), (160, 1, -4, True))
        self.assertFalse(ORIGINAL.bands[6].enabled)
        self.assertEqual(ORIGINAL.raw[:14], b'SyntheticEQ\0\0\0')

    def test_all_synthetic_packets_roundtrip(self):
        for packet in PACKETS.values():
            encoded = bytes(packet)[7:-1]
            preset = Preset(decode_words(encoded))
            self.assertEqual(encode_words(preset.raw), encoded)

    def test_frames_match_known_parameter_encoding(self):
        cases = (
            (1, 'frequency', 170, 'f0 43 58 70 0c 01 01 01 00 00 00 00 54 43 f7'),
            (1, 'q', 1.1, 'f0 43 58 70 0c 01 01 02 03 1b 4c 02 18 3f f7'),
            (1, 'gain', -3.8, 'f0 43 58 70 0c 01 01 03 00 66 33 01 67 40 f7'),
            (3, 'enabled', False, 'f0 43 58 70 0c 01 03 00 00 00 00 00 06 23 f7'),
            (3, 'enabled', True, 'f0 43 58 70 0c 01 03 00 00 02 00 00 06 23 f7'),
        )
        for index, field, value, expected in cases:
            target = ORIGINAL.with_field(index, field, value)
            self.assertEqual(parameter_frame(target, index, field), bytes.fromhex(expected))

    def test_rejects_corrupted_encoding(self):
        for payload in (b'\x00', b'\x04\x00\x00', b'\x00\x80\x00'):
            with self.assertRaises(ValueError):
                decode_words(payload)

    def test_rejects_invalid_device_values(self):
        for index, field, value in ((1, 'gain', 13), (1, 'gain', float('nan')),
                                    (1, 'frequency', 1), (1, 'q', 0), (0, 'q', 1)):
            with self.assertRaises(ValueError):
                ORIGINAL.with_field(index, field, value)

    def test_single_edit_preserves_unsupported_bytes(self):
        changed = ORIGINAL.with_field(1, 'gain', -3.9)
        self.assertEqual(changed.raw[:42], ORIGINAL.raw[:42])
        self.assertEqual(changed.raw[46:], ORIGINAL.raw[46:])

    def test_no_change_does_not_write_or_backup(self):
        device = MemoryDevice()
        self.assertEqual(device.apply(ORIGINAL, ORIGINAL), ORIGINAL)
        self.assertEqual((device.writes, device.backups), ([], 0))

    def test_successful_apply_is_verified(self):
        device = MemoryDevice()
        target = ORIGINAL.with_field(1, 'gain', -3.9).with_field(3, 'enabled', False)
        self.assertEqual(device.apply(ORIGINAL, target), target)
        self.assertEqual(device.backups, 1)
        self.assertEqual(len(device.writes), 2)

    def test_ack_timeout_rolls_back_applied_write(self):
        device = MemoryDevice(fail_on=2)
        target = ORIGINAL.with_field(1, 'frequency', 170).with_field(1, 'gain', -3.9)
        with self.assertRaisesRegex(RuntimeError, '已恢复原参数'):
            device.apply(ORIGINAL, target)
        self.assertEqual(device.actual.raw, ORIGINAL.raw)
        self.assertEqual(len(device.writes), 4)

    def test_concurrent_change_blocks_write(self):
        device = MemoryDevice()
        device.actual = ORIGINAL.with_field(1, 'gain', -3)
        with self.assertRaisesRegex(RuntimeError, '已发生变化'):
            device.apply(ORIGINAL, ORIGINAL.with_field(1, 'gain', -3.9))
        self.assertEqual(device.writes, [])

    def test_identical_presets_in_different_slots_still_block_operations(self):
        empty8 = Preset(decode_words(bytes(PACKETS['0b/8'])[7:-1]))
        empty9 = Preset(decode_words(bytes(PACKETS['0b/9'])[7:-1]))
        self.assertEqual(empty8.raw, empty9.raw)
        for operation in ('apply', 'save', 'select'):
            with self.subTest(operation=operation):
                device = MemoryDevice()
                device.active, device.actual = 9, empty9
                with self.assertRaisesRegex(RuntimeError, '槽位已改变'):
                    if operation == 'apply':
                        device.apply(empty8, empty8.with_field(1, 'gain', -2), expected_slot=8)
                    elif operation == 'save':
                        device.save(empty8, expected_slot=8)
                    else:
                        device.select(0, expected_slot=8, expected=empty8)
                self.assertEqual((device.backups, device.writes, device.state_writes), (0, [], []))

    def test_slot_change_during_read_blocks_byte_identical_preset(self):
        device = MemoryDevice()
        device.on_current = lambda d: setattr(d, 'active', 1)
        with self.assertRaisesRegex(RuntimeError, '槽位已改变'):
            device.apply(ORIGINAL, ORIGINAL.with_field(1, 'gain', -2), expected_slot=0)
        self.assertEqual(device.writes, [])

    def test_slot_change_during_backup_blocks_every_write_action(self):
        for operation in ('apply', 'save', 'select'):
            with self.subTest(operation=operation):
                device = MemoryDevice()
                device.on_backup = lambda d: setattr(d, 'active', 1)
                with self.assertRaisesRegex(RuntimeError, '槽位已改变'):
                    if operation == 'apply':
                        device.apply(ORIGINAL, ORIGINAL.with_field(1, 'gain', -2), expected_slot=0)
                    elif operation == 'save':
                        device.save(ORIGINAL, expected_slot=0)
                    else:
                        device.select(2, expected_slot=0, expected=ORIGINAL)
                self.assertEqual((device.writes, device.state_writes), ([], []))

    def test_parameter_change_during_backup_is_rechecked(self):
        for operation in ('apply', 'save', 'select'):
            with self.subTest(operation=operation):
                device = MemoryDevice()
                device.on_backup = lambda d: setattr(d, 'actual', ORIGINAL.with_field(1, 'gain', -3))
                with self.assertRaisesRegex(RuntimeError, '已发生变化'):
                    if operation == 'apply':
                        device.apply(ORIGINAL, ORIGINAL.with_field(1, 'gain', -2), expected_slot=0)
                    elif operation == 'save':
                        device.save(ORIGINAL, expected_slot=0)
                    else:
                        device.select(2, expected_slot=0, expected=ORIGINAL)
                self.assertEqual((device.writes, device.state_writes), ([], []))

    def test_slot_change_mid_apply_never_rolls_back_onto_another_slot(self):
        device = MemoryDevice()
        device.on_write = lambda d: setattr(d, 'active', 1)
        target = ORIGINAL.with_field(1, 'frequency', 170).with_field(1, 'gain', -2)
        with self.assertRaisesRegex(RuntimeError, '恢复也未确认'):
            device.apply(ORIGINAL, target, expected_slot=0)
        self.assertEqual(device.parameter_slots, [0])
        self.assertEqual(device.working[1].raw, ORIGINAL.raw)
        self.assertEqual(device.working[0].bands[1].frequency, 170)

    def test_slot_change_mid_rollback_stops_recovery(self):
        device = MemoryDevice(fail_on=2)
        def change_after_first_recovery(d):
            if len(d.writes) == 3:
                d.active = 1
        device.on_write = change_after_first_recovery
        target = ORIGINAL.with_field(1, 'frequency', 170).with_field(1, 'gain', -2)
        with self.assertRaisesRegex(RuntimeError, '恢复也未确认'):
            device.apply(ORIGINAL, target, expected_slot=0)
        self.assertEqual(device.parameter_slots, [0, 0, 0])
        self.assertEqual(device.working[1].raw, ORIGINAL.raw)

    def test_save_and_select_preserve_latest_eq_state(self):
        for operation in ('save', 'select'):
            with self.subTest(operation=operation):
                device = MemoryDevice()
                device.on_backup = lambda d: d.state_data.__setitem__(1, 0)
                if operation == 'save':
                    target = ORIGINAL.with_field(1, 'gain', -2)
                    device.actual = target
                    self.assertEqual(device.save(target, expected_slot=0), target)
                    self.assertEqual(device.saved[0], target)
                else:
                    self.assertEqual(device.select(2, expected_slot=0, expected=ORIGINAL), ORIGINAL)
                    self.assertEqual(device.active, 2)
                self.assertEqual(device.state_writes[0][1], 0)

    def test_select_rejects_unexpected_unsaved_device_changes(self):
        device = MemoryDevice()
        device.actual = ORIGINAL.with_field(1, 'gain', -2)
        with self.assertRaisesRegex(RuntimeError, '已发生变化'):
            device.select(1, expected_slot=0, expected=ORIGINAL)
        self.assertEqual(device.state_writes, [])

    def test_save_requires_matching_slot_ack(self):
        device = MemoryDevice()
        device._write_state = lambda state: bytes((10, 1, 1, 0, 1, 0, 0, 0))
        with self.assertRaisesRegex(RuntimeError, '保存未得到确认'):
            device.save(ORIGINAL, expected_slot=0)

    def test_snapshot_refuses_a_changed_active_slot(self):
        device = MemoryDevice()
        device.on_current = lambda d: setattr(d, 'active', 1)
        with self.assertRaisesRegex(RuntimeError, '槽位已改变'):
            device.snapshot()

    def test_unknown_field_and_rename_are_blocked(self):
        for offset in (0, 16):
            raw = bytearray(ORIGINAL.raw)
            raw[offset] ^= 1
            device = MemoryDevice()
            with self.assertRaises(ValueError):
                device.apply(ORIGINAL, Preset(raw))
            self.assertEqual(device.writes, [])

    def test_fragmented_and_multiple_sysex_packets(self):
        device = AxonClient()
        packet = bytes(PACKETS['0c/0'])
        self.assertEqual(device._packets(b'\x90\x40' + packet[:80]), [])
        self.assertEqual(device._packets(packet[80:] + packet), [packet, packet])

    def test_float_display_preserves_float32(self):
        for value in (1.100000023841858, 0.7070000171661377, 1023.98779296875, -3.9000000953674316):
            self.assertEqual(struct.pack('<f', float(display_float(value))), struct.pack('<f', value))
        self.assertEqual(display_float(1.100000023841858), '1.1')
        self.assertEqual([display_float(value) for value in (20, 160, 2000, 12000)],
                         ['20', '160', '2000', '12000'])

    def test_unchanged_form_preserves_every_device_byte(self):
        form = App.__new__(App)
        form.current = ORIGINAL
        form.audition = None
        form.values = [{field: Var(getattr(band, field) if field == 'enabled' else display_float(getattr(band, field)))
                        for field in ('enabled', 'frequency', 'q', 'gain')} for band in ORIGINAL.bands]
        self.assertEqual(App.target(form).raw, ORIGINAL.raw)
        form.values[1]['gain'] = Var('-3.9')
        self.assertEqual(App.target(form).raw, ORIGINAL.with_field(1, 'gain', -3.9).raw)

    def test_peaking_preview_is_zero_for_zero_gain(self):
        band = ORIGINAL.bands[3]
        for frequency in (20, 100, 3000, 10000, 20000):
            self.assertAlmostEqual(band_response(band, 3, frequency), 0, places=10)

    def test_graph_drag_clamps_frequency_and_gain(self):
        bounds = (40, 1040, 20, 260)
        self.assertEqual(drag_values(1, -100, -100, bounds), {'frequency': 20, 'gain': 12})
        self.assertEqual(drag_values(1, 2000, 2000, bounds), {'frequency': 20000, 'gain': -12})
        self.assertEqual(drag_values(3, 40, 140, bounds)['gain'], 0)

    def test_cutoff_drag_preserves_unsupported_fields(self):
        bounds = (40, 1040, 20, 260)
        for index in (0, 6):
            change = drag_values(index, 540, 20, bounds)
            self.assertEqual(set(change), {'frequency'})
            target = ORIGINAL.with_field(index, 'frequency', change['frequency'])
            self.assertEqual(target.field_bytes(index, 'q'), ORIGINAL.field_bytes(index, 'q'))
            self.assertEqual(target.field_bytes(index, 'gain'), ORIGINAL.field_bytes(index, 'gain'))


if __name__ == '__main__':
    unittest.main()
