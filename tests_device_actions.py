"""Real packet construction over a memory transport; never opens USB/MIDI.

Reset/name effects in the fake are test scenarios, not hardware evidence.
"""
from collections import deque
from pathlib import Path
import queue
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from axon_protocol import (AxonClient, FIELD_IDS, Preset, ReadCancelled, WriteUnconfirmed,
                           decode_words, encode_words, frame, name_bytes, rename_frame, state_payload)
from axon_control import App
from editor_state import preset_form
from tests_editor import ORIGINAL, Variable, memory_app
from tests_protocol import MemoryDevice


class MemoryPort:
    def __init__(self):
        self.state = bytearray((10, 1, 0, 0, 0, 0, 0, 0))
        self.saved, self.working = [ORIGINAL] * 10, [ORIGINAL] * 10
        self.messages, self.replies = [], deque()
        self.drop_ack, self.drop_query = set(), set()
        self.rename_saved_only = False
        self.closed = False

    def send(self, message):
        message = bytes(message)
        self.messages.append(message)
        command, mode, data = message[4], message[5], message[6:-1]
        slot = self.state[2]
        if mode == 0:
            if command in self.drop_query:
                return
            if command == 0x15:
                answer = frame(command, 2, self.state)
            else:
                preset = self.working[slot] if command == 0x0c else self.saved[data[0]]
                answer = frame(command, 2, bytes((0 if command == 0x0c else data[0],)) + encode_words(preset.raw))
        elif command == 0x15:
            self.state[:] = data
            if data[2] != slot:
                self.working[data[2]] = self.saved[data[2]]
            if data[3]:
                self.working[data[2]] = ORIGINAL
            if data[4]:
                self.saved[data[2]] = self.working[data[2]]
            answer = frame(command, 3, self.state)
        elif command == 0x12:
            renamed_slot, name = data[0], data[1:]
            self.saved[renamed_slot] = Preset(name + self.saved[renamed_slot].raw[14:])
            if not self.rename_saved_only:
                self.working[renamed_slot] = Preset(name + self.working[renamed_slot].raw[14:])
            answer = frame(command, 3)
        elif command == 0x7b:
            self.saved, self.working = [ORIGINAL] * 10, [ORIGINAL] * 10
            answer = frame(command, 3)
        elif command == 0x0c:
            band, field = data[:2]
            raw = bytearray(self.working[slot].raw)
            offset = 14 + band * 16 + field * 4
            raw[offset:offset+4] = decode_words(data[2:])
            self.working[slot] = Preset(raw)
            answer = frame(command, 3, bytes((band,)) + b'\0' * 7)
        else:
            raise AssertionError(f'Unclassified command {command:x}')
        if mode != 1 or command not in self.drop_ack:
            self.replies.append(answer)

    def receive(self, timeout=0):
        if self.replies:
            return self.replies.popleft()
        if timeout:
            time.sleep(min(timeout, 0.001))
        return None

    def close(self):
        self.closed = True

    @property
    def writes(self):
        return [p for p in self.messages if p[5] == 1]


class WireDevice(AxonClient):
    def __init__(self, folder):
        super().__init__(folder)
        self.port = MemoryPort()
        self.device_info = {'name': 'NUX AXON-3', 'model': 'NFM-3'}

    def _exchange(self, message, accept, timeout=3):
        return super()._exchange(message, accept, timeout=0.005)


class StateActionTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.client = WireDevice(self.folder.name)
        self.addCleanup(self.client.close)

    def test_fresh_state_never_replays_action_flags_or_unknown_bytes(self):
        state = bytes((10, 1, 3, 1, 1, 40, 50, 60))
        self.assertEqual(state_payload(state, slot=5), bytes((10, 1, 5, 0, 0, 0, 0, 0)))
        self.assertEqual(state_payload(state, save=True), bytes((10, 1, 3, 0, 1, 0, 0, 0)))
        self.assertEqual(state_payload(state, reset=True), bytes((10, 1, 3, 1, 0, 0, 0, 0)))
        self.assertEqual(state_payload(state, eq_enabled=False), bytes((10, 0, 3, 0, 0, 0, 0, 0)))
        with self.assertRaises(ValueError):
            state_payload(state, reset=True, save=True)

    def test_all_status_actions_clear_old_flags_on_wire(self):
        for operation in ('select', 'save', 'eq'):
            with self.subTest(operation=operation):
                device = MemoryDevice()
                device.state_data[3:] = b'\x01\x01\x05\x06\x07'
                if operation == 'select':
                    device.select(1, expected_slot=0, expected=ORIGINAL)
                elif operation == 'save':
                    device.save(ORIGINAL, expected_slot=0)
                else:
                    device.set_eq(False, expected_slot=0, expected=ORIGINAL, expected_enabled=True)
                request = device.state_writes[0]
                self.assertEqual(request[3], 0)
                self.assertEqual(request[4], int(operation == 'save'))
                self.assertEqual(request[5:], b'\0\0\0')

    def test_eq_changes_only_global_flag_without_parameter_or_save_commands(self):
        working = ORIGINAL.with_field(2, 'gain', -2)
        self.client.port.working[0] = working
        state, actual = self.client.set_eq(False, expected_slot=0, expected=working, expected_enabled=True)
        self.assertEqual((state[1], actual.raw), (0, working.raw))
        self.assertEqual(self.client.port.saved[0].raw, ORIGINAL.raw)
        self.assertEqual(self.client.port.writes, [frame(0x15, 1, bytes((10, 0, 0, 0, 0, 0, 0, 0)))])

    def test_eq_no_change_does_not_backup_or_write(self):
        self.client.set_eq(True, expected_slot=0, expected=ORIGINAL)
        self.assertFalse(self.client.port.writes)
        self.assertFalse(list(Path(self.folder.name).glob('*.json')))

    def test_changed_slot_or_eq_before_toggle_blocks_write(self):
        for mutate in (lambda: self.client.port.state.__setitem__(2, 1),
                       lambda: self.client.port.state.__setitem__(1, 0)):
            self.client.port.state[:] = bytes((10, 1, 0, 0, 0, 0, 0, 0))
            mutate()
            with self.assertRaises(RuntimeError):
                self.client.set_eq(False, expected_slot=0, expected=ORIGINAL, expected_enabled=True)
            self.assertFalse(self.client.port.writes)

    def test_lost_status_ack_is_verified_without_repeating_any_write(self):
        for action in ('eq', 'select', 'save'):
            with self.subTest(action=action):
                client = WireDevice(self.folder.name)
                client.port.drop_ack.add(0x15)
                if action == 'eq':
                    state, _ = client.set_eq(False, expected_slot=0, expected=ORIGINAL)
                    self.assertEqual(state[1], 0)
                elif action == 'select':
                    self.assertEqual(client.select(1, expected_slot=0, expected=ORIGINAL), ORIGINAL)
                    self.assertEqual(client.state()[2], 1)
                else:
                    target = ORIGINAL.with_field(1, 'gain', -2)
                    client.port.working[0] = target
                    self.assertEqual(client.save(target, expected_slot=0), target)
                self.assertEqual(len(client.port.writes), 1)
                client.close()

    def test_quick_snapshot_reads_active_preset_and_marks_other_slots_cached(self):
        full = self.client.snapshot()
        self.assertEqual(full['preset_read_scope'], 'all')
        self.client.port.messages.clear()
        saved = ORIGINAL.with_field(1, 'gain', -1)
        self.client.port.saved[0] = saved
        quick = self.client.snapshot(refresh_presets=False)
        reads = [p[6] for p in self.client.port.messages if p[4:6] == bytes((0x0b, 0))]
        self.assertEqual(reads, [0])
        self.assertEqual(quick['cached_slots'], list(range(1, 10)))
        self.assertEqual(quick['presets'][0]['raw_hex'], saved.raw.hex())
        self.assertFalse(self.client.port.writes)

    def test_missing_or_changed_count_cache_falls_back_to_full_read(self):
        self.client.snapshot(refresh_presets=False)
        self.client.port.messages.clear()
        self.client.port.state[0] = 9
        result = self.client.snapshot(refresh_presets=False)
        self.assertEqual(result['preset_read_scope'], 'all')
        self.assertEqual(len([p for p in self.client.port.messages if p[4:6] == b'\x0b\0']), 9)

    def test_apply_backup_reads_current_and_active_saved_preset_without_scanning_others(self):
        self.client.snapshot()
        self.client.port.messages.clear()
        target = ORIGINAL.with_field(1, 'gain', -3)
        self.assertEqual(self.client.apply(ORIGINAL, target, expected_slot=0), target)
        reads = [p for p in self.client.port.messages if p[4:6] == b'\x0b\0']
        self.assertEqual(len(reads), 1)
        import json
        backup = json.loads(next(Path(self.folder.name).glob('*-before-apply.json')).read_text(encoding='utf-8'))
        self.assertEqual(backup['current']['raw_hex'], ORIGINAL.raw.hex())
        self.assertEqual(backup['cached_slots'], list(range(1, 10)))

    def test_ascii_name_payload_matches_binary_layout_and_rejects_truncation(self):
        self.assertEqual(rename_frame(2, 'Desk 160'), bytes.fromhex('f0 43 58 70 12 01 02') + b'Desk 160' + b'\0' * 6 + b'\xf7')
        self.assertEqual(len(name_bytes('12345678901234')), 14)
        for invalid in ('', '名字', 'x' * 15, ' leading', 'trailing ', 'a\nb', 'a\0b', 'a\x7fb'):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.client.rename(invalid, expected_slot=0, expected=ORIGINAL)
        self.assertFalse(self.client.port.writes)

    def test_rename_preserves_distinct_saved_and_unsaved_band_bytes(self):
        working = ORIGINAL.with_field(1, 'gain', -2)
        self.client.port.working[0] = working
        snapshot = self.client.rename('Studio', expected_slot=0, expected=working)
        self.assertEqual(snapshot['current']['name'], 'Studio')
        self.assertEqual(bytes.fromhex(snapshot['current']['raw_hex'])[14:], working.raw[14:])
        self.assertEqual(bytes.fromhex(snapshot['presets'][0]['raw_hex'])[14:], ORIGINAL.raw[14:])
        self.assertEqual([p[4] for p in self.client.port.writes], [0x12])
        self.assertTrue(list(Path(self.folder.name).glob('*-before-rename.json')))

    def test_rename_lost_ack_can_succeed_only_when_names_and_all_band_bytes_match(self):
        self.client.port.drop_ack.add(0x12)
        result = self.client.rename('Studio', expected_slot=0, expected=ORIGINAL)
        self.assertFalse(result['action_acknowledged'])
        self.assertEqual(len(self.client.port.writes), 1)

    def test_partial_rename_reports_actual_snapshot_instead_of_claiming_success(self):
        self.client.port.rename_saved_only = True
        with self.assertRaises(WriteUnconfirmed) as caught:
            self.client.rename('Studio', expected_slot=0, expected=ORIGINAL)
        self.assertEqual(caught.exception.snapshot['current']['name'], ORIGINAL.name)
        self.assertEqual(caught.exception.snapshot['presets'][0]['name'], 'Studio')
        self.assertTrue(caught.exception.backup.exists())
        self.assertEqual(len(self.client.port.writes), 1)

    def test_reset_payloads_and_backups_are_distinct_from_restore_saved(self):
        working = ORIGINAL.with_field(1, 'gain', -1)
        self.client.port.working[0] = working
        result = self.client.reset_current(expected_slot=0, expected=working)
        self.assertEqual(self.client.port.writes[-1], frame(0x15, 1, bytes((10, 1, 0, 1, 0, 0, 0, 0))))
        self.assertTrue(result['backup'].exists())
        self.client.reset_all(expected_slot=0, expected=ORIGINAL)
        self.assertEqual(self.client.port.writes[-1], bytes.fromhex('f0 43 58 70 7b 01 01 f7'))
        self.assertTrue(list(Path(self.folder.name).glob('*-before-reset-all.json')))

    def test_lost_reset_ack_is_never_retried_or_inferred_from_arbitrary_current_bytes(self):
        for all_presets in (False, True):
            with self.subTest(all_presets=all_presets):
                client = WireDevice(self.folder.name)
                client.port.drop_ack.add(0x7b if all_presets else 0x15)
                action = client.reset_all if all_presets else client.reset_current
                with self.assertRaises(WriteUnconfirmed) as caught:
                    action(expected_slot=0, expected=ORIGINAL)
                self.assertIsNotNone(caught.exception.snapshot)
                self.assertTrue(caught.exception.backup.exists())
                self.assertEqual(len(client.port.writes), 1)
                client.close()

    def test_backup_failure_stops_all_new_writes(self):
        for action in (lambda: self.client.set_eq(False, expected_slot=0, expected=ORIGINAL),
                       lambda: self.client.rename('Studio', expected_slot=0, expected=ORIGINAL),
                       lambda: self.client.reset_current(expected_slot=0, expected=ORIGINAL),
                       lambda: self.client.reset_all(expected_slot=0, expected=ORIGINAL)):
            with patch.object(self.client, 'backup', side_effect=OSError('disk full')):
                with self.assertRaises(OSError):
                    action()
            self.assertFalse(self.client.port.writes)

    def test_reset_and_rename_stop_when_slot_changes_during_backup(self):
        for action in (self.client.reset_current, self.client.reset_all,
                       lambda **args: self.client.rename('Studio', **args)):
            self.client.port.state[2] = 0
            with patch.object(self.client, 'backup', side_effect=lambda *_: self.client.port.state.__setitem__(2, 1)):
                with self.assertRaisesRegex(RuntimeError, '槽位已改变'):
                    action(expected_slot=0, expected=ORIGINAL)
            self.assertFalse(self.client.port.writes)


class ReadQueueTests(unittest.TestCase):
    def test_read_timeout_retries_once_but_io_failure_does_not_retry(self):
        client = AxonClient()
        packet = frame(0x15, 2, bytes((10, 1, 0, 0, 0, 0, 0, 0)))
        client._exchange = Mock(side_effect=[TimeoutError(), packet])
        self.assertEqual(client.state()[0], 10)
        self.assertEqual(client._exchange.call_count, 2)
        self.assertEqual([c.kwargs['timeout'] for c in client._exchange.call_args_list], [1.0, 1.0])
        client._exchange = Mock(side_effect=OSError('port lost'))
        with self.assertRaises(OSError):
            client.state()
        self.assertEqual(client._exchange.call_count, 1)

    def test_cancel_wakes_waiting_query_and_prevents_retry(self):
        client = AxonClient()
        port = client.port = MemoryPort()
        port.drop_query.add(0x15)
        results = queue.Queue()
        def query_state():
            try:
                client.state()
            except Exception as error:
                results.put(error)
        worker = threading.Thread(target=query_state)
        worker.start()
        deadline = time.monotonic() + 1
        while not port.messages and time.monotonic() < deadline:
            time.sleep(0.001)
        client.cancel_read()
        self.assertIsInstance(results.get(timeout=1), ReadCancelled)
        worker.join(timeout=1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(port.messages), 1)
        client.begin_operation()
        port.drop_query.clear()
        self.assertEqual(client.state()[0], 10)
        client.close()

    def test_infinite_stale_midi_input_is_bounded_before_sending(self):
        client = AxonClient()
        client.port = Mock()
        client.port.receive.return_value = b'\xf8'
        with self.assertRaisesRegex(RuntimeError, 'MIDI 消息'):
            client.state()
        self.assertLessEqual(client.port.receive.call_count, 256)
        client.port.send.assert_not_called()


def device_app():
    app = memory_app()
    app.connected = True
    app.needs_readback, app.cancellable = False, False
    app.root, app.combo = Mock(), Mock()
    app.eq_label, app.device_label = Variable(), Variable()
    app.client = MemoryDevice()
    app.client.device_info = {'model': 'NFM-3', 'name': 'NUX AXON-3'}
    app.snapshot = app.client.snapshot()
    app._write_draft = Mock(return_value=Path('preserved-draft.json'))
    app._run = lambda message, work, done, **options: done(work())
    def snapshot(data):
        app.snapshot, app.slot = data, data['active_slot']
        app.audition = None
        app._populate(Preset(bytes.fromhex(data['current']['raw_hex'])))
    app._snapshot = snapshot
    return app


class DeviceUiTests(unittest.TestCase):
    def test_failed_device_operation_preserves_preview_and_requires_readback_before_new_writes(self):
        app = device_app()
        app.client.port = Mock()
        app.values[1]['gain'].set('-2')
        app.results = queue.Queue()
        app.busy = True
        app.results.put((Mock(), None, TimeoutError('请刷新')))
        app._poll()
        self.assertTrue(app.needs_readback)
        self.assertEqual(app.target().bands[1].gain, -2)
        work, done = Mock(), Mock()
        App._run(app, 'write', work, done)
        work.assert_not_called()
        done.assert_not_called()

    def test_cancelled_refresh_keeps_a_b_context_and_all_local_values(self):
        from audition_state import AuditionPair
        app = device_app()
        app.client.port = Mock()
        after = ORIGINAL.with_field(1, 'gain', -3)
        pair = app.audition = AuditionPair(0, ORIGINAL, after, 'A')
        app.values[1]['gain'].set('-2')
        before = app._form()
        app.results = queue.Queue()
        app.busy, app.cancellable = True, True
        app.results.put((Mock(), None, ReadCancelled()))
        app._poll()
        self.assertIs(app.audition, pair)
        self.assertEqual(app._form(), before)
        self.assertFalse(app.needs_readback)
        self.assertFalse(app.cancellable)

    def test_partial_rename_snapshot_remains_recoverable_as_a_session(self):
        from editor_session import SessionStore
        from editor_state import read_document
        app = device_app()
        app.snapshot['presets'][0] = Preset(name_bytes('Other name') + ORIGINAL.raw[14:]).as_dict()
        self.assertEqual(read_document(app.snapshot)['current']['name'], ORIGINAL.name)
        with tempfile.TemporaryDirectory() as folder:
            store = SessionStore(Path(folder) / 'session.json')
            store.save(app.snapshot, app._form(), 1, False)
            result = store.load()
        self.assertEqual(result['document']['presets'][0]['name'], 'Other name')
        self.assertEqual(result['document']['current']['name'], ORIGINAL.name)

    def test_eq_toggle_keeps_local_preview_and_applied_values_separate(self):
        app = device_app()
        app.values[1]['gain'].set('-2')
        preview = app.target()
        app.toggle_eq()
        self.assertFalse(app.snapshot['eq_enabled'])
        self.assertEqual(app.target().raw, preview.raw)
        self.assertEqual(app.current.raw, ORIGINAL.raw)
        self.assertFalse(app.client.writes)

    def test_pending_switch_is_reviewed_and_draft_is_written_before_device_switch(self):
        app = device_app()
        app.values[1]['gain'].set('-2')
        app.combo.current.return_value = 1
        with patch('axon_control.ActionDialog') as dialog:
            app.select()
            self.assertFalse(app.client.state_writes)
            app._write_draft.assert_not_called()
            dialog.call_args.args[3]('')
        app._write_draft.assert_called_once()
        self.assertEqual(app.slot, 1)
        self.assertEqual(len(app.client.state_writes), 1)
        self.assertIn('preserved-draft.json', app.status.get())

    def test_draft_io_failure_blocks_pending_switch_without_discarding_input(self):
        app = device_app()
        app.values[1]['gain'].set('-2')
        app.combo.current.return_value = 1
        app._write_draft.side_effect = OSError('disk full')
        with patch('axon_control.ActionDialog') as dialog:
            app.select()
            dialog.call_args.args[3]('')
        self.assertEqual(app.slot, 0)
        self.assertFalse(app.client.state_writes)
        self.assertEqual(app.values[1]['gain'].get(), '-2')

    def test_refresh_rebases_local_edits_while_preserving_unrelated_device_changes(self):
        app = device_app()
        app.values[1]['gain'].set('-2')
        app.client.actual = ORIGINAL.with_field(2, 'frequency', 2600)
        app.read()
        self.assertEqual(app.target().bands[1].gain, -2)
        self.assertEqual(app.target().bands[2].frequency, 2600)
        self.assertEqual(app.current.bands[1].gain, ORIGINAL.bands[1].gain)
        self.assertFalse(app.client.writes)
        app._write_draft.assert_called_once()

    def test_conflicting_refresh_preserves_draft_and_reports_conflict(self):
        app = device_app()
        app.values[1]['gain'].set('-2')
        app.client.actual = ORIGINAL.with_field(1, 'gain', -1)
        app.read()
        self.assertEqual(app.current.bands[1].gain, -1)
        self.assertIn('preserved-draft.json', app.status.get())
        self.assertFalse(app.client.writes)

    def test_invalid_local_input_is_never_silently_discarded_on_refresh(self):
        app = device_app()
        app.values[1]['gain'].set('unfinished')
        app._run = Mock()
        app.read()
        app._run.assert_not_called()
        self.assertEqual(app.values[1]['gain'].get(), 'unfinished')

    def test_new_actions_are_disabled_offline_busy_closing_and_before_reconciliation(self):
        for attribute in ('connected', 'busy', 'closing', 'needs_readback'):
            for action in ('toggle_eq', 'rename_preset', 'reset_presets'):
                app = device_app()
                setattr(app, attribute, attribute != 'connected')
                app._run = Mock()
                with patch('axon_control.ActionDialog') as dialog:
                    getattr(app, action)()
                    dialog.assert_not_called()
                app._run.assert_not_called()

    def test_resets_are_confirmed_and_all_reset_requires_exact_phrase(self):
        app = device_app()
        app._run = Mock()
        with patch('axon_control.ActionDialog') as dialog:
            app.reset_presets(True)
            app._run.assert_not_called()
            options = dialog.call_args.kwargs
            for invalid in ('', 'reset', ' RESET', 'RESET ALL'):
                with self.assertRaises(ValueError):
                    options['validate'](invalid)
            options['validate']('RESET')
            self.assertTrue(options['warning'])
        self.assertFalse(app.client.state_writes)

    def test_connection_uses_one_initial_snapshot_for_backup(self):
        app = device_app()
        app.connected = False
        snapshot = app.snapshot
        app.client = Mock()
        app.client.connect.return_value = snapshot
        app.connect()
        app.client.backup_snapshot.assert_called_once_with(snapshot, 'connected')
        app.client.backup.assert_not_called()


if __name__ == '__main__':
    unittest.main()
