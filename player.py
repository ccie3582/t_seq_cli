"""Phrase playback: schedule MIDI notes to a winmm output port.

python-rtmidi's cp314 wheel is broken on this machine, so real MIDI output
uses winmm.dll directly (midiOutOpen / midiOutShortMsg / midiOutClose).

Timing model: BPM counts quarter notes; the default grid is 16 divisions
(sixteenth notes) per 4/4 bar, so one step = quarter/4 = 15/bpm seconds at
120 bpm. Each step's note sustains half the step and then releases.
"""

import ctypes
import threading
import time
from ctypes import wintypes

import midi_ports
import phrases

VELOCITY = 100
SUSTAIN_FRACTION = 0.5

_winmm = ctypes.WinDLL("winmm")

midi_out_open = _winmm.midiOutOpen
midi_out_open.argtypes = [ctypes.POINTER(ctypes.c_void_p), wintypes.UINT,
                          ctypes.c_size_t, ctypes.c_size_t, wintypes.DWORD]
midi_out_open.restype = wintypes.UINT

midi_out_short_msg = _winmm.midiOutShortMsg
midi_out_short_msg.argtypes = [ctypes.c_void_p, wintypes.DWORD]
midi_out_short_msg.restype = wintypes.UINT

midi_out_close = _winmm.midiOutClose
midi_out_close.argtypes = [ctypes.c_void_p]
midi_out_close.restype = wintypes.UINT


def step_time_for(bpm, divisions=16):
    """Seconds per step: sixteenths in a 4/4 bar at the given BPM."""
    quarter = 60.0 / bpm
    return (quarter * 4.0) / divisions


def find_device_index(name):
    """Device index (0-based) of a MIDI output port by name, or None."""
    for i, (port_name, _kind) in enumerate(midi_ports.get_output_port_info()):
        if port_name == name:
            return i
    return None


def panic_ports(port_names):
    """Send All Sound Off (CC120) and All Notes Off (CC123) on all channels.

    Returns the list of ports that were reachable and received the messages.
    Uses the shared output handles (so it never opens a device twice).
    """
    sent = []
    for name in sorted({n for n in port_names if n}):
        try:
            handle = _output_pool.acquire(name)
        except (ValueError, OSError):
            continue
        try:
            for channel in range(1, 17):
                midi_out_short_msg(handle, _msg(0xB0, channel, 120, 0))
                midi_out_short_msg(handle, _msg(0xB0, channel, 123, 0))
        finally:
            _output_pool.release(name)
        sent.append(name)
    return sent


def _flatten_terms(terms, get_seq):
    """Flatten to steps tagged with their source sequence index.

    Each step is (seq_index, entry) where entry is None for a rest.
    """
    out = []
    for count, unit in terms:
        if unit[0] == "seq":
            index = unit[1]
            seq = get_seq(index)
            if seq is None:
                raise ValueError(
                    f"Phrase references seq{index} but it is not configured "
                    f"in this pattern.")
            tagged = [(index, entry) for entry in seq]
            for _ in range(count):
                out.extend(tagged)
        else:  # ('group', terms-list)
            inner = _flatten_terms(unit[1], get_seq)
            for _ in range(count):
                out.extend(inner)
    return out


def flatten_phrase(text, get_seq):
    """Flatten a phrase expression into a step plan.

    get_seq(index) -> list of entries (None = rest, else (degree, shift)) or
    None when the sequence is not configured.

    Returns (plan, infinite) where plan is the ordered list of
    (sequence_index, entry) steps of one pass; infinite True means the caller
    must loop the plan until stopped.
    Raises phrases.PhraseError / ValueError for invalid input.
    """
    tree = phrases.parse_phrase_tree(text)
    if tree[0] == "inf":
        inner = tree[1]
        if isinstance(inner, list):
            plan = _flatten_terms(inner, get_seq)
        else:
            index = inner[1]
            seq = get_seq(index)
            if seq is None:
                raise ValueError(
                    f"Phrase references seq{index} but it is not configured "
                    f"in this pattern.")
            plan = [(index, entry) for entry in seq]
        return plan, True
    return _flatten_terms(tree, get_seq), False


def _msg(status, channel, note, velocity):
    """Pack a short MIDI message (status nibble, note, velocity)."""
    return status | ((channel - 1) & 0x0F) | ((note & 0x7F) << 8) | ((velocity & 0x7F) << 16)


class _OutputPool:
    """Reference-counted winmm output handles, one per port name.

    Several patterns may play at the same time over the same port and channel;
    sharing a single device handle keeps that reliable (and avoids relying on
    the driver supporting multiple simultaneous opens).
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._handles = {}  # name -> [handle, refcount]

    def acquire(self, name):
        with self._lock:
            entry = self._handles.get(name)
            if entry is not None:
                entry[1] += 1
                return entry[0]
            index = find_device_index(name)
            if index is None:
                raise ValueError(f"MIDI output port not found: {name}")
            handle = ctypes.c_void_p()
            rc = midi_out_open(ctypes.byref(handle), index, 0, 0, 0)
            if rc != 0:
                raise OSError(f"midiOutOpen failed for '{name}' (error {rc}).")
            self._handles[name] = [handle, 1]
            return handle

    def release(self, name):
        with self._lock:
            entry = self._handles.get(name)
            if entry is None:
                return
            entry[1] -= 1
            if entry[1] <= 0:
                del self._handles[name]
                try:
                    midi_out_close(entry[0])
                except Exception:
                    pass

    def open_ports(self):
        with self._lock:
            return list(self._handles)


_output_pool = _OutputPool()


class Player:
    """Plays a step plan on a MIDI output port in a background thread.

    resolver(step) maps a plan entry to a MIDI note number; it may return
    None (e.g. for rests) to skip the note while keeping the step timing.
    """

    def __init__(self, port_name, channel, step_time, plan, infinite, resolver,
                 provider=None, velocity_resolver=None,
                 sustain_resolver=None, microtime_resolver=None,
                 mode="note", controller=1):
        self.port_name = port_name
        self.channel = channel
        self.step_time = step_time
        self.sustain = step_time * SUSTAIN_FRACTION
        self.plan = plan
        self.position = 0  # index of the step currently playing (for views)
        self.infinite = infinite
        self.resolver = resolver
        # provider() -> (plan, infinite, step_time, resolver, channel), invoked
        # before every pass; on error the previous pass data is kept.
        self.provider = provider
        # velocity_resolver(seq_index, now) -> 0..127 for each note (optional).
        self.velocity_resolver = velocity_resolver
        # sustain_resolver(seq_index, now) -> 0..1 share of the step (optional).
        self.sustain_resolver = sustain_resolver
        # microtime_resolver(seq_index, now) -> -0.5..0.5 offset in steps.
        self.microtime_resolver = microtime_resolver
        # "note" sends note on/off; "cc" sends control change messages.
        self.mode = mode
        self.controller = controller
        self.error = None
        self._stop_event = threading.Event()
        self._finished = threading.Event()

        # Share one device handle per port so several players can run together.
        self._handle = _output_pool.acquire(port_name)

        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self, join=True):
        self._stop_event.set()
        if join:
            self._thread.join(timeout=max(2.0, 2 * self.step_time * max(len(self.plan), 1) + 1.0))

    @property
    def running(self):
        return not self._finished.is_set() and not self._stop_event.is_set()

    @property
    def finished(self):
        return self._finished.is_set()

    def _send(self, status, note, velocity=0):
        midi_out_short_msg(self._handle, _msg(status, self.channel, note, velocity))

    def _refresh(self):
        """Update pass data from the provider; keep previous data on failure."""
        if self.provider is None:
            return
        try:
            (self.plan, self.infinite, self.step_time, self.resolver,
             self.channel, self.mode, self.controller) = self.provider()
            self.sustain = self.step_time * SUSTAIN_FRACTION
        except Exception as e:
            print(f"Note: could not refresh the playing phrase ({e}); "
                  f"continuing with the previous cycle.")
            time.sleep(0.05)

    def _run(self):
        last_note = None
        try:
            def note_off():
                nonlocal last_note
                if last_note is not None:
                    self._send(0x80, last_note, 0)
                    last_note = None

            grid = time.monotonic()  # ideal time of the current step
            last_step_time = None
            while True:
                self._refresh()  # pick up edits at the start of the new cycle
                if self.step_time != last_step_time:
                    # Grid re-anchors only when the step length changes,
                    # so microtiming stays aligned across identical cycles.
                    grid = time.monotonic()
                    last_step_time = self.step_time
                if not self.plan:
                    if not self.infinite:
                        break
                    time.sleep(self.step_time)
                    continue
                for position, step in enumerate(self.plan):
                    self.position = position
                    if self._stop_event.is_set():
                        return
                    seq_index, entry = step
                    if self.microtime_resolver is not None:
                        offset = self.microtime_resolver(seq_index, time.monotonic())
                        offset = max(-0.5, min(0.5, offset)) * self.step_time
                    else:
                        offset = 0.0
                    target = grid + offset
                    delay = target - time.monotonic()
                    if delay > 0:
                        time.sleep(delay)  # early/late note (grid never drifts)
                    now = time.monotonic()
                    note = self.resolver(entry, now)
                    if note is None or not 0 <= note <= 127:
                        grid += self.step_time  # rest / out-of-range step
                        continue
                    if self.mode == "cc":
                        # Control change: the resolved value is the datum.
                        self._send(0xB0, max(0, min(127, self.controller)),
                                   note)
                        grid += self.step_time
                        continue
                    if self.velocity_resolver is not None:
                        velocity = self.velocity_resolver(seq_index, now)
                    else:
                        velocity = VELOCITY
                    if self.sustain_resolver is not None:
                        share = self.sustain_resolver(seq_index, now)
                    else:
                        share = SUSTAIN_FRACTION
                    hold = self.step_time * max(0.0, min(1.0, share))
                    self._send(0x90, note, velocity)
                    last_note = note
                    time.sleep(hold)
                    note_off()
                    grid += self.step_time
                if not self.infinite:
                    break
            # drain a little so the last release reaches the driver
            time.sleep(0.01)
        except Exception as e:  # pragma: no cover - defensive
            self.error = e
        finally:
            try:
                note_off()
            except Exception:
                pass
            # The device handle is shared; keep it open while other players
            # still use the same port.
            _output_pool.release(self.port_name)
            self._finished.set()
