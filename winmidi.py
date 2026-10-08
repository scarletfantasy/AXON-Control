"""Small standard-library WinMM MIDI transport for Windows (no driver install)."""
import ctypes as C
import queue
import threading
import time

U32 = C.c_uint32
UINTPTR = C.c_size_t
HANDLE = C.c_void_p

class MidiCaps(C.Structure):
    _fields_ = [('wMid', C.c_uint16), ('wPid', C.c_uint16),
                ('vDriverVersion', U32), ('szPname', C.c_wchar * 32),
                ('wTechnology', C.c_uint16), ('wVoices', C.c_uint16),
                ('wNotes', C.c_uint16), ('wChannelMask', C.c_uint16),
                ('dwSupport', U32)]

class MidiInCaps(C.Structure):
    _fields_ = [('wMid', C.c_uint16), ('wPid', C.c_uint16),
                ('vDriverVersion', U32), ('szPname', C.c_wchar * 32),
                ('dwSupport', U32)]

class MidiHeader(C.Structure):
    pass
MidiHeader._fields_ = [('lpData', C.c_void_p), ('dwBufferLength', U32),
                      ('dwBytesRecorded', U32), ('dwUser', UINTPTR),
                      ('dwFlags', U32), ('lpNext', C.POINTER(MidiHeader)),
                      ('reserved', UINTPTR), ('dwOffset', U32),
                      ('dwReserved', UINTPTR * 8)]

winmm = C.WinDLL('winmm')
for name, arguments in {
    'midiInGetDevCapsW': [UINTPTR, C.POINTER(MidiInCaps), U32],
    'midiOutGetDevCapsW': [UINTPTR, C.POINTER(MidiCaps), U32],
    'midiInOpen': [C.POINTER(HANDLE), U32, UINTPTR, UINTPTR, U32],
    'midiOutOpen': [C.POINTER(HANDLE), U32, UINTPTR, UINTPTR, U32],
    'midiInPrepareHeader': [HANDLE, C.POINTER(MidiHeader), U32],
    'midiInUnprepareHeader': [HANDLE, C.POINTER(MidiHeader), U32],
    'midiOutPrepareHeader': [HANDLE, C.POINTER(MidiHeader), U32],
    'midiOutUnprepareHeader': [HANDLE, C.POINTER(MidiHeader), U32],
    'midiInAddBuffer': [HANDLE, C.POINTER(MidiHeader), U32],
    'midiOutLongMsg': [HANDLE, C.POINTER(MidiHeader), U32],
    'midiOutShortMsg': [HANDLE, U32],
    'midiInStart': [HANDLE], 'midiInStop': [HANDLE], 'midiInReset': [HANDLE],
    'midiInClose': [HANDLE], 'midiOutClose': [HANDLE], 'midiOutReset': [HANDLE],
    'midiInGetErrorTextW': [U32, C.c_wchar_p, U32],
}.items():
    function = getattr(winmm, name)
    function.argtypes = arguments
    function.restype = U32

def check(code, operation):
    if code:
        description = C.create_unicode_buffer(256)
        winmm.midiInGetErrorTextW(code, description, 256)
        raise OSError(code, operation + ': ' + description.value)

def devices():
    result = {'inputs': [], 'outputs': []}
    for index in range(winmm.midiInGetNumDevs()):
        caps = MidiInCaps()
        check(winmm.midiInGetDevCapsW(index, C.byref(caps), C.sizeof(caps)), 'Read MIDI input')
        result['inputs'].append({'id': index, 'name': caps.szPname})
    for index in range(winmm.midiOutGetNumDevs()):
        caps = MidiCaps()
        check(winmm.midiOutGetDevCapsW(index, C.byref(caps), C.sizeof(caps)), 'Read MIDI output')
        result['outputs'].append({'id': index, 'name': caps.szPname})
    return result

CALLBACK = C.WINFUNCTYPE(None, HANDLE, U32, UINTPTR, UINTPTR, UINTPTR)

class MidiPort:
    def __init__(self, input_id, output_id):
        self.received = queue.Queue(maxsize=1024)
        self.input_overflow = threading.Event()
        self.recycle = queue.Queue()
        self.input = HANDLE()
        self.output = HANDLE()
        self.buffers = []
        self.pending_output = []
        self.closed = False
        self.lock = threading.RLock()
        self.callback = CALLBACK(self._callback)
        try:
            check(winmm.midiOutOpen(C.byref(self.output), output_id, 0, 0, 0), 'Open MIDI output')
            check(winmm.midiInOpen(C.byref(self.input), input_id, C.cast(self.callback, C.c_void_p).value, 0, 0x30000), 'Open MIDI input')
            for index in range(4):
                buffer = C.create_string_buffer(65536)
                header = MidiHeader(lpData=C.cast(buffer, C.c_void_p).value,
                                    dwBufferLength=len(buffer), dwUser=index)
                self.buffers.append((buffer, header))
                check(winmm.midiInPrepareHeader(self.input, C.byref(header), C.sizeof(header)), 'Prepare MIDI input')
                check(winmm.midiInAddBuffer(self.input, C.byref(header), C.sizeof(header)), 'Queue MIDI input')
            check(winmm.midiInStart(self.input), 'Start MIDI input')
        except Exception:
            self.close()
            raise

    def _callback(self, handle, message, instance, parameter1, parameter2):
        if message in (0x3c4, 0x3c6):
            header = C.cast(parameter1, C.POINTER(MidiHeader)).contents
            data = C.string_at(header.lpData, header.dwBytesRecorded)
            if data:
                self._enqueue(data)
            self.recycle.put(header.dwUser)
        # The editor consumes SysEx only. MIDI clock/note notifications must
        # not accumulate while it is idle or delay a control transaction.

    def _enqueue(self, data):
        try:
            self.received.put_nowait(data)
        except queue.Full:
            # Never wait inside WinMM's native callback. A lost response makes
            # this connection uncertain and requires reopening the port.
            self.input_overflow.set()

    def _requeue(self):
        # A newly requeued buffer can complete again while this function runs.
        # Limit one pass so a continuous device stream cannot trap the worker.
        for _ in range(len(self.buffers)):
            if self.closed:
                break
            try:
                index = self.recycle.get_nowait()
            except queue.Empty:
                break
            header = self.buffers[index][1]
            header.dwBytesRecorded = 0
            check(winmm.midiInAddBuffer(self.input, C.byref(header), C.sizeof(header)), 'Requeue MIDI input')

    def send(self, message):
        data = bytes(message)
        with self.lock:
            if self.closed:
                raise RuntimeError('MIDI port is closed')
            buffer = C.create_string_buffer(data)
            header = MidiHeader(lpData=C.cast(buffer, C.c_void_p).value, dwBufferLength=len(data))
            check(winmm.midiOutPrepareHeader(self.output, C.byref(header), C.sizeof(header)), 'Prepare MIDI output')
            owned = (buffer, header)
            self.pending_output.append(owned)
            try:
                check(winmm.midiOutLongMsg(self.output, C.byref(header), C.sizeof(header)), 'Send MIDI')
                deadline = time.monotonic() + 3
                while not (header.dwFlags & 1):
                    if time.monotonic() >= deadline:
                        raise TimeoutError('MIDI output completion timed out')
                    time.sleep(0.002)
            finally:
                # Unprepare also handles a prepared buffer whose send was rejected.
                # MIDIERR_STILLPLAYING (65) means Windows still owns the buffer.
                code = winmm.midiOutUnprepareHeader(self.output, C.byref(header), C.sizeof(header))
                if code == 0:
                    self.pending_output.remove(owned)
                elif code != 65:
                    check(code, 'Release MIDI output')

    def receive(self, timeout=1):
        with self.lock:
            if self.input_overflow.is_set():
                raise RuntimeError('MIDI 输入积压，已暂停操作。请断开连接后重新连接。')
            self._requeue()
        try:
            return self.received.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            if self.input.value:
                winmm.midiInStop(self.input)
                winmm.midiInReset(self.input)
                for buffer, header in self.buffers:
                    winmm.midiInUnprepareHeader(self.input, C.byref(header), C.sizeof(header))
                winmm.midiInClose(self.input)
                self.input = HANDLE()
            if self.output.value:
                winmm.midiOutReset(self.output)
                for buffer, header in self.pending_output:
                    winmm.midiOutUnprepareHeader(self.output, C.byref(header), C.sizeof(header))
                winmm.midiOutClose(self.output)
                self.output = HANDLE()

if __name__ == '__main__':
    import json
    print(json.dumps(devices(), ensure_ascii=False, indent=2))
