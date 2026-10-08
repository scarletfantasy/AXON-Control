"""Deterministic offline examples, invented for tests; contains no device captures.

Construct wire bytes independently from the production codec. Reserved words and
HP/LP fields deliberately differ from ordinary defaults to exercise preservation.
The ordinary LF values also support the editor's existing boundary/rounding tests.
"""
import struct

from axon_protocol import Preset


def preset_bytes(name='SyntheticEQ', *, empty=False):
    rows = (
        (True, 0x2300, 25.0, 0.625, 0.25),
        (True, 0x2301, 160.0, 1.0, -4.0),
        (True, 0x2302, 2000.0, 1.25, 0.0),
        (True, 0x2303, 3000.0, 0.8, 0.0),
        (True, 0x2304, 6500.0, 0.9, 0.0),
        (True, 0x2305, 11000.0, 1.25, 0.0),
        (False, 0x2306, 19500.0, 0.6, -0.25),
    )
    return name.encode('ascii').ljust(14, b'\0') + b''.join(
        struct.pack('<HHfff', False if empty else enabled, reserved, frequency, q, gain)
        for enabled, reserved, frequency, q, gain in rows)


def wire_packet(raw, command, slot):
    """Reference bit packing, using shifts of individual bytes rather than words."""
    payload = bytes(value for high, low in zip(raw[::2], raw[1::2])
                    for value in (high >> 6, ((high & 63) << 1) | (low >> 7), low & 127))
    return bytes((0xf0, 0x43, 0x58, 0x70, command, 2, slot)) + payload + b'\xf7'


ORIGINAL = Preset(preset_bytes())
PACKETS = {f'0b/{slot}': wire_packet(
    preset_bytes('EmptyDemo' if slot >= 8 else f'Example{slot + 1:02}', empty=slot >= 8),
    0x0b, slot) for slot in range(10)}
PACKETS['0c/0'] = wire_packet(ORIGINAL.raw, 0x0c, 0)
