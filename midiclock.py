"""MIDI clock (24 PPQN) send and receive over winmm, plus Start/Stop.

The sequencer can either **send** its tempo as MIDI clock to an output port, or
**receive** clock from an input port and follow it (the received ticks set the
global BPM). `clock start received` additionally means an incoming MIDI Start
starts every configured pattern, and Stop stops them.

Standard MIDI clock/transport bytes:

    0xF8 clock   - 24 of them per quarter note (PPQN)
    0xFA start   - begin from the first step
    0xFB continue- resume (treated as start here)
    0xFC stop    - stop playback

No third-party MIDI library: midiOut* / midiIn* come from winmm.dll via ctypes,
as the rest of the project does.
"""

import ctypes
import queue
import threading
import time
from ctypes import wintypes

import midi_ports
import player

CLOCK = 0xF8
START = 0xFA
CONTINUE = 0xFB
STOP = 0xFC
PPQN = 24

MODE_OFF = "off"
MODE_SEND = "send"
MODE_RECEIVE = "receive"
MODES = (MODE_OFF, MODE_SEND, MODE_RECEIVE)

START_INTERNAL = "internal"
START_RECEIVED = "received"
START_MODES = (START_INTERNAL, START_RECEIVED)

# How many tick intervals are averaged to estimate an incoming tempo
TEMPO_WINDOW = 24
# Ticks stop arriving for this long -> the external clock is considered lost
CLOCK_TIMEOUT = 0.5

_winmm = ctypes.WinDLL("winmm")

midi_in_open = _winmm.midiInOpen
midi_in_open.argtypes = [ctypes.POINTER(ctypes.c_void_p), wintypes.UINT,
                         ctypes.c_size_t, ctypes.c_size_t, wintypes.DWORD]
midi_in_open.restype = wintypes.UINT

midi_in_start = _winmm.midiInStart
midi_in_start.argtypes = [ctypes.c_void_p]
midi_in_start.restype = wintypes.UINT

midi_in_stop = _winmm.midiInStop
midi_in_stop.argtypes = [ctypes.c_void_p]
midi_in_stop.restype = wintypes.UINT

midi_in_reset = _winmm.midiInReset
midi_in_reset.argtypes = [ctypes.c_void_p]
midi_in_reset.restype = wintypes.UINT

midi_in_close = _winmm.midiInClose
midi_in_close.argtypes = [ctypes.c_void_p]
midi_in_close.restype = wintypes.UINT

CALLBACK_FUNCTION = 0x00030000
CALLBACK_NULL = 0x00000000
MIM_DATA = 0x3C3
MIM_LONGDATA = 0x3C4
MMSYSERR_NOERROR = 0

_MIDIINPROC = ctypes.WINFUNCTYPE(None, ctypes.c_void_p, wintypes.UINT,
                                 ctypes.c_size_t, ctypes.c_size_t,
                                 ctypes.c_size_t)


def input_port_names():
    """Names of the MIDI input ports."""
    return midi_ports.get_input_port_names()


def output_port_names():
    """Names of the MIDI output ports."""
    return midi_ports.get_output_port_names()


def find_input_index(name):
    """Device index of a MIDI input port by name, or None."""
    for index, port_name in enumerate(input_port_names()):
        if port_name == name:
            return index
    return None


class MidiInput:
    """An open MIDI input port; calls on_message(status, data1, data2)."""

    def __init__(self, device_index, on_message):
        self.device_index = device_index
        self.on_message = on_message
        self.handle = ctypes.c_void_p()
        self.error = None
        self._callback = _MIDIINPROC(self._dispatch)   # keep a reference!
        # The callback parameter is a DWORD_PTR: pass the function pointer's
        # address as an integer (ctypes cannot cast to a non-pointer type).
        address = ctypes.cast(self._callback, ctypes.c_void_p).value or 0
        result = midi_in_open(ctypes.byref(self.handle), device_index,
                              address, 0, CALLBACK_FUNCTION)
        if result != MMSYSERR_NOERROR:
            self.error = (f"cannot open MIDI input port "
                          f"(winmm error {result}; another program may be "
                          f"using it)")
            raise RuntimeError(self.error)
        midi_in_start(self.handle)

    def _dispatch(self, _handle, message, _instance, param1, _param2):
        """winmm callback thread: forward the bytes, never do work here."""
        if message == MIM_DATA:
            data = int(param1)
            self.on_message(data & 0xFF, (data >> 8) & 0xFF, (data >> 16) & 0xFF)

    def close(self):
        try:
            midi_in_stop(self.handle)
            midi_in_reset(self.handle)
            midi_in_close(self.handle)
        except OSError:
            pass


class ClockSender(threading.Thread):
    """Sends 24 PPQN clock, with Start/Stop around the sequencer's playback."""

    def __init__(self, port_name, bpm_getter, playback_getter):
        super().__init__(daemon=True)
        self.port_name = port_name
        self.bpm_getter = bpm_getter
        self.playback_getter = playback_getter
        self.ticks = 0
        self.playing = False
        self.error = None
        self._stop = threading.Event()
        self._handle = ctypes.c_void_p()
        index = player.find_device_index(port_name)
        if index is None:
            raise RuntimeError(f"MIDI output port '{port_name}' not found")
        result = player.midi_out_open(ctypes.byref(self._handle), index, 0, 0, 0)
        if result != MMSYSERR_NOERROR:
            raise RuntimeError(f"cannot open MIDI output port '{port_name}' "
                               f"(winmm error {result}; another program may be "
                               f"using it)")
        self._index = index

    def _send(self, status):
        player.midi_out_short_msg(self._handle, status)

    def run(self):
        next_tick = time.perf_counter()
        try:
            while not self._stop.is_set():
                bpm = max(1.0, float(self.bpm_getter()))
                interval = 60.0 / (bpm * PPQN)
                next_tick += interval
                delay = next_tick - time.perf_counter()
                if delay > 0:
                    if self._stop.wait(delay):
                        break
                else:
                    # Late (the machine hiccupped): re-anchor rather than
                    # sending a burst of catch-up ticks.
                    next_tick = time.perf_counter()
                # Start/Stop follow the sequencer's playback state
                playing = bool(self.playback_getter())
                if playing != self.playing:
                    self.playing = playing
                    self._send(START if playing else STOP)
                self._send(CLOCK)
                self.ticks += 1
        finally:
            try:
                player.midi_out_close(self._handle)
            except OSError:
                pass

    def stop(self):
        self._stop.set()


class ClockReceiver(threading.Thread):
    """Reads clock ticks: estimates the tempo and reacts to Start/Stop."""

    def __init__(self, port_index, on_bpm, on_start, on_stop, on_tick=None):
        super().__init__(daemon=True)
        self.on_bpm = on_bpm
        self.on_start = on_start
        self.on_stop = on_stop
        self.on_tick = on_tick
        self.bpm = None
        self.ticks = 0
        self.last_tick = None
        self.running = False
        self.error = None
        self._events = queue.Queue()
        self._stop = threading.Event()
        self._intervals = []
        self._last_time = None
        self._port = MidiInput(port_index, self._on_message)
        self._port.error = None

    # -- winmm callback thread: only queue the bytes --------------------------
    def _on_message(self, status, data1, data2):
        self._events.put((status, data1, data2))

    # -- worker ---------------------------------------------------------------
    def run(self):
        while not self._stop.is_set():
            try:
                status, data1, data2 = self._events.get(timeout=0.2)
            except queue.Empty:
                # Clock lost?
                if (self.running and self._last_time
                        and time.monotonic() - self._last_time > CLOCK_TIMEOUT):
                    self.running = False
                continue
            now = time.monotonic()
            if status == CLOCK:
                self.ticks += 1
                if self._last_time is not None:
                    interval = now - self._last_time
                    if 0.0005 < interval < 2.0:      # ignore glitches
                        self._intervals.append(interval)
                        del self._intervals[:-TEMPO_WINDOW]
                        average = sum(self._intervals) / len(self._intervals)
                        bpm = 60.0 / (average * PPQN)
                        if self.bpm is None or abs(bpm - self.bpm) >= 0.5:
                            self.bpm = bpm
                            try:
                                self.on_bpm(bpm)
                            except Exception:
                                pass
                self._last_time = now
                if not self.running:
                    self.running = True
                if self.on_tick:
                    try:
                        self.on_tick()
                    except Exception:
                        pass
            elif status in (START, CONTINUE):
                self._last_time = None               # restart the tempo window
                self._intervals = []
                self.running = True
                try:
                    self.on_start()
                except Exception:
                    pass
            elif status == STOP:
                self.running = False
                try:
                    self.on_stop()
                except Exception:
                    pass

    def stop(self):
        self._stop.set()
        self._port.close()


class MidiClock:
    """Facade the shell talks to: mode, port, start mode and status."""

    def __init__(self, bpm_getter, playback_getter, on_external_bpm,
                 on_external_start, on_external_stop):
        self.bpm_getter = bpm_getter
        self.playback_getter = playback_getter
        self.on_external_bpm = on_external_bpm
        self.on_external_start = on_external_start
        self.on_external_stop = on_external_stop
        self.mode = MODE_OFF
        self.port = None
        self.start_mode = START_INTERNAL
        self.sender = None
        self.receiver = None
        self.last_error = None
        self.external_bpm = None

    # -- configuration --------------------------------------------------------
    def configure(self, mode=None, port=None, start_mode=None):
        """Change the settings; returns (ok, message)."""
        if mode is not None:
            if mode not in MODES:
                return False, f"unknown clock mode '{mode}' (off, send, receive)"
            self.mode = mode
        if start_mode is not None:
            if start_mode not in START_MODES:
                return False, (f"unknown start mode '{start_mode}' "
                               f"(internal, received)")
            self.start_mode = start_mode
        if port is not None:
            self.port = port or None
        self.stop()
        self.last_error = None
        if self.mode == MODE_OFF:
            return True, "MIDI clock off"
        if not self.port:
            return True, "choose a port with 'clock port <name>'"
        if self.mode == MODE_SEND:
            index = player.find_device_index(self.port)
            if index is None:
                return False, (f"'{self.port}' is not an available MIDI "
                               f"OUTPUT port")
            try:
                self.sender = ClockSender(self.port, self.bpm_getter,
                                          self.playback_getter)
            except RuntimeError as e:
                self.last_error = str(e)
                return False, str(e)
            self.sender.start()
            return True, (f"sending MIDI clock (24 PPQN) to '{self.port}' "
                          f"(Start/Stop follow the sequencer)")
        index = find_input_index(self.port)
        if index is None:
            return False, (f"'{self.port}' is not an available MIDI INPUT port")
        try:
            self.receiver = ClockReceiver(
                index,
                on_bpm=self._bpm_from_clock,
                on_start=self._start_from_clock,
                on_stop=self._stop_from_clock)
        except RuntimeError as e:
            self.last_error = str(e)
            return False, str(e)
        self.receiver.start()
        start_text = ("starts all patterns on MIDI Start"
                      if self.start_mode == START_RECEIVED
                      else "ignores MIDI Start (clock start internal)")
        return True, (f"receiving MIDI clock (24 PPQN) from '{self.port}', "
                      f"{start_text}")

    def _bpm_from_clock(self, bpm):
        self.external_bpm = bpm
        if self.on_external_bpm:
            self.on_external_bpm(bpm)

    def _start_from_clock(self):
        if self.start_mode == START_RECEIVED and self.on_external_start:
            self.on_external_start()

    def _stop_from_clock(self):
        if self.start_mode == START_RECEIVED and self.on_external_stop:
            self.on_external_stop()

    def stop(self):
        """Stop sending/receiving (keeps the configured mode and port)."""
        if self.sender is not None:
            self.sender.stop()
            self.sender = None
        if self.receiver is not None:
            self.receiver.stop()
            self.receiver = None

    def shutdown(self):
        self.stop()
        self.mode = MODE_OFF

    # -- status ---------------------------------------------------------------
    @property
    def active(self):
        return bool(self.sender or self.receiver)

    def status_lines(self):
        """Human-readable state, for the 'clock' command and views."""
        lines = [f"MIDI clock: {self.mode}"
                 + (f" on '{self.port}'" if self.port else
                    " (no port chosen)")]
        if self.mode == MODE_OFF:
            lines.append("  off: choose 'clock send', 'clock receive' or "
                         "'clock port <name>'")
        if self.mode == MODE_SEND:
            if self.sender:
                lines.append(f"  sending 24 PPQN to '{self.port}' "
                             f"({self.sender.ticks} ticks), Start/Stop follow "
                             f"the sequencer")
            else:
                lines.append(f"  NOT running"
                             + (f": {self.last_error}" if self.last_error
                                else " (no port opened yet)"))
        if self.mode == MODE_RECEIVE:
            if self.receiver:
                state = ("receiving" if self.receiver.running
                         else "waiting for ticks")
                lines.append(f"  {state} from '{self.port}' "
                             f"({self.receiver.ticks} ticks)")
                if self.receiver.bpm:
                    lines.append(f"  tempo from clock: "
                                 f"{self.receiver.bpm:.2f} BPM")
            else:
                lines.append(f"  NOT running"
                             + (f": {self.last_error}" if self.last_error
                                else " (no port opened yet)"))
            lines.append(f"  start: {self.start_mode}"
                         + (" (MIDI Start starts every configured pattern)"
                            if self.start_mode == START_RECEIVED else
                            " (MIDI Start is ignored)"))
        return lines
