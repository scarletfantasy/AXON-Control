"""Bounded identity retries with fake ports; no MIDI hardware is opened."""
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from axon_protocol import AxonClient


def identity(model=b'NFM-3'):
    packet = bytearray(45)
    packet[:4], packet[36:36+len(model)], packet[-1] = bytes.fromhex('f0 43 58 10'), model, 0xf7
    return bytes(packet)


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        self.port = Mock()
        self.module = SimpleNamespace(
            MidiPort=Mock(return_value=self.port),
            devices=lambda: {'inputs': [{'name': 'NUX AXON-3', 'id': 0}],
                             'outputs': [{'name': 'NUX AXON-3', 'id': 1}]})
        self.client = AxonClient()
        self.client.snapshot = Mock(return_value={'connected': True})
        self.patch = patch.dict(sys.modules, {'winmidi': self.module})
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.addCleanup(self.client.close)

    def test_successful_identity_is_not_retried(self):
        self.client._exchange = Mock(return_value=identity())
        self.assertEqual(self.client.connect(), {'connected': True})
        self.assertEqual(self.client._exchange.call_count, 1)
        self.assertEqual(self.client.device_info['identity_attempts'], 1)
        self.module.MidiPort.assert_called_once_with(0, 1)

    def test_identity_timeout_retries_once_on_the_same_port(self):
        self.client._exchange = Mock(side_effect=[TimeoutError('first read lost'), identity()])
        self.assertEqual(self.client.connect(), {'connected': True})
        self.assertEqual(self.client._exchange.call_count, 2)
        self.assertEqual(self.client.device_info['identity_attempts'], 2)
        self.assertEqual([call.args[0] for call in self.client._exchange.call_args_list],
                         [bytes.fromhex('f0 43 58 00 f7')] * 2)
        self.module.MidiPort.assert_called_once_with(0, 1)
        self.port.close.assert_not_called()

    def test_repeated_timeouts_close_the_port_without_reading_presets(self):
        self.client._exchange = Mock(side_effect=TimeoutError('no response'))
        with self.assertRaises(TimeoutError):
            self.client.connect()
        self.assertEqual(self.client._exchange.call_count, 2)
        self.client.snapshot.assert_not_called()
        self.port.close.assert_called_once()
        self.assertIsNone(self.client.port)

    def test_wrong_or_malformed_identity_is_rejected_without_retry(self):
        wrong = bytearray(identity(b'NFM-2'))
        wrong[10:15] = b'NFM-3'  # Model text elsewhere must not authorize writes.
        for packet in (bytes(wrong), identity()[:-1], identity(b'NFM-30')):
            with self.subTest(packet=packet):
                client = AxonClient()
                client.snapshot = Mock()
                client._exchange = Mock(return_value=packet)
                self.port.reset_mock()
                with self.assertRaisesRegex(RuntimeError, '设备身份'):
                    client.connect()
                self.assertEqual(client._exchange.call_count, 1)
                client.snapshot.assert_not_called()
                self.port.close.assert_called_once()
                self.assertIsNone(client.port)

    def test_port_errors_are_not_retried_as_identity_timeouts(self):
        self.client._exchange = Mock(side_effect=OSError('MIDI output failed'))
        with self.assertRaises(OSError):
            self.client.connect()
        self.assertEqual(self.client._exchange.call_count, 1)
        self.port.close.assert_called_once()
        self.client.snapshot.assert_not_called()

    def test_identity_version_fields_are_preserved_without_inventing_a_firmware_version(self):
        packet = bytearray(identity())
        packet[4:12], packet[28] = b'20200409', 0x0a
        self.client._exchange = Mock(return_value=bytes(packet))
        self.client.connect()
        self.assertEqual(self.client.device_info['version_field'], '20200409')
        self.assertEqual(self.client.device_info['fm_version_raw'], 10)
        self.assertNotIn('firmware_version', self.client.device_info)


if __name__ == '__main__':
    unittest.main()
