"""Bound WinMM queue/callback work without opening a native MIDI handle."""
import ctypes as C
import queue
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import patch

from winmidi import MidiHeader, MidiPort, winmm


def memory_port():
    port = MidiPort.__new__(MidiPort)
    port.received, port.recycle = queue.Queue(maxsize=1), queue.Queue()
    port.input_overflow = threading.Event()
    port.lock, port.closed = threading.RLock(), False
    port.input = C.c_void_p()
    port.buffers = []
    return port


class TransportTests(unittest.TestCase):
    def test_full_input_queue_does_not_block_native_callback_and_is_reported_to_worker(self):
        port = memory_port()
        port._enqueue(b'first')
        port._enqueue(b'overflow')
        self.assertTrue(port.input_overflow.is_set())
        self.assertEqual(port.received.get_nowait(), b'first')
        with self.assertRaisesRegex(RuntimeError, '重新连接'):
            port.receive(timeout=0)

    def test_short_midi_notifications_do_not_fill_control_response_queue(self):
        port = memory_port()
        for _ in range(2048):
            port._callback(None, 0x3c3, 0, 0x7f4090, 0)
            port._callback(None, 0x3c5, 0, 0xf8, 0)
        self.assertTrue(port.received.empty())
        self.assertFalse(port.input_overflow.is_set())

    def test_long_sysex_callback_preserves_data_and_recycles_its_own_buffer(self):
        port = memory_port()
        data = C.create_string_buffer(bytes.fromhex('f0 43 58 70 15 02 f7'))
        header = MidiHeader(lpData=C.cast(data, C.c_void_p).value, dwBytesRecorded=7, dwUser=3)
        port._callback(None, 0x3c4, 0, C.addressof(header), 0)
        self.assertEqual(port.received.get_nowait(), data.raw[:7])
        self.assertEqual(port.recycle.get_nowait(), 3)

    def test_requeue_pass_finishes_even_if_every_recycled_buffer_completes_immediately(self):
        port = memory_port()
        port.buffers = [(None, MidiHeader()) for _ in range(4)]
        port.recycle.put(0)
        def complete_again(*args):
            port.recycle.put(0)
            return 0
        with patch.object(winmm, 'midiInAddBuffer', side_effect=complete_again) as add:
            port._requeue()
        self.assertEqual(add.call_count, 4)
        self.assertEqual(port.recycle.qsize(), 1)

    def test_closed_or_empty_port_does_not_call_native_requeue(self):
        port = memory_port()
        port.buffers = [(None, MidiHeader()) for _ in range(4)]
        with patch.object(winmm, 'midiInAddBuffer') as add:
            port._requeue()
            port.closed = True
            port.recycle.put(0)
            port._requeue()
            add.assert_not_called()


if __name__ == '__main__':
    unittest.main()
