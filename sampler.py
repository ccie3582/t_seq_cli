"""Sample playback for 'loop' patterns: positions in a normalised sample.

A loop pattern points at a .wav file inside the ``samples`` folder next to this
script. The sample's length is treated as **1**: an order entry such as ``0.2``
means "play from 20 % of the sample", and entries wrap modulo 1 so negative
values work too (``-0.2`` -> ``0.8``).

Seeking is done by feeding the audio device the sample's frames starting at the
wanted position, through winmm's ``waveOut`` API (ctypes, the same library the
MIDI output uses) - ``winsound`` can only play a file from its beginning. One
device is opened per sample format; every step moves the buffer pointer and the
previous playback is cut off, like a sampler.

Only plain PCM .wav files are supported. Windows only (winmm).
"""

import atexit
import ctypes
import os
import threading
import time
import wave

MAX_ORDER_LISTS = 10          # o0..o9
MAX_POSITION_ENTRIES = 4096   # sanity limit for one order list

SAMPLES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "samples")

WAVE_MAPPER = 0xFFFFFFFF
CALLBACK_NULL = 0x00000000
WAVE_FORMAT_PCM = 1

try:  # Windows only
    _winmm = ctypes.WinDLL("winmm")
except (OSError, AttributeError):  # pragma: no cover - non-Windows
    _winmm = None

_sample_cache = {}   # (path, mtime, size) -> Sample


class _WaveFormat(ctypes.Structure):
    """WAVEFORMATEX."""

    _fields_ = [("wFormatTag", ctypes.c_ushort),
                ("nChannels", ctypes.c_ushort),
                ("nSamplesPerSec", ctypes.c_uint),
                ("nAvgBytesPerSec", ctypes.c_uint),
                ("nBlockAlign", ctypes.c_ushort),
                ("wBitsPerSample", ctypes.c_ushort),
                ("cbSize", ctypes.c_ushort)]


class _WaveHdr(ctypes.Structure):
    """WAVEHDR."""

    _fields_ = [("lpData", ctypes.c_void_p),
                ("dwBufferLength", ctypes.c_uint),
                ("dwBytesRecorded", ctypes.c_uint),
                ("dwUser", ctypes.c_void_p),
                ("dwFlags", ctypes.c_uint),
                ("dwLoops", ctypes.c_uint),
                ("lpNext", ctypes.c_void_p),
                ("reserved", ctypes.c_void_p)]


def samples_dir():
    """Absolute path of the samples folder."""
    return SAMPLES_DIR


def list_samples():
    """Sorted .wav file names available in the samples folder."""
    try:
        names = os.listdir(SAMPLES_DIR)
    except OSError:
        return []
    return sorted(n for n in names if n.lower().endswith(".wav"))


def resolve_sample(name):
    """Resolve a user-supplied file name to a path inside samples/.

    Returns (path, None) or (None, error message).
    """
    text = (name or "").strip().strip('"')
    if not text:
        return None, "no file name given"
    tail = os.path.basename(text.replace("\\", "/"))
    path = os.path.join(SAMPLES_DIR, tail)
    if not os.path.isfile(path):
        found = list_samples()
        extra = (f" Available: {', '.join(found)}" if found else
                 " The folder is empty (put .wav files there).")
        return None, f"'{tail}' was not found in {SAMPLES_DIR}.{extra}"
    return path, None


def audio_available():
    """True when the platform can play samples."""
    return _winmm is not None


class Sample:
    """A PCM .wav decoded in memory, addressed by normalised position."""

    def __init__(self, path):
        try:
            with wave.open(path, "rb") as src:
                params = src.getparams()
                frames = src.readframes(params.nframes)
        except wave.Error as e:
            raise ValueError(f"'{os.path.basename(path)}' is not a PCM .wav "
                             f"file ({e}).")
        except OSError as e:
            raise ValueError(f"cannot read '{path}': {e}")
        if params.comptype != "NONE":
            raise ValueError(f"'{os.path.basename(path)}' is compressed; only "
                             f"plain PCM .wav files can be played.")
        self.path = path
        self.name = os.path.basename(path)
        self.params = params
        self.frames = frames
        self.frame_size = max(1, params.nchannels * params.sampwidth)
        self.nframes = max(1, params.nframes)
        self.seconds = params.nframes / float(params.framerate or 1)

    def format_struct(self):
        fmt = _WaveFormat()
        fmt.wFormatTag = WAVE_FORMAT_PCM
        fmt.nChannels = self.params.nchannels
        fmt.nSamplesPerSec = self.params.framerate
        fmt.wBitsPerSample = self.params.sampwidth * 8
        fmt.nBlockAlign = self.params.nchannels * self.params.sampwidth
        fmt.nAvgBytesPerSec = fmt.nSamplesPerSec * fmt.nBlockAlign
        fmt.cbSize = 0
        return fmt

    def frame_offset(self, position):
        """Byte offset of a normalised position (wrapped into 0..1)."""
        pos = float(position) % 1.0
        index = int(pos * self.nframes)
        index = max(0, min(self.nframes - 1, index))
        return index * self.frame_size

    def duration_text(self):
        """'3.10 s' style length of the sample."""
        return f"{self.seconds:.2f} s"


def load_sample(path):
    """Load (and cache) a .wav as a Sample."""
    try:
        stat = os.stat(path)
    except OSError as e:
        raise ValueError(f"cannot read '{path}': {e}")
    key = (os.path.abspath(path), stat.st_mtime, stat.st_size)
    sample = _sample_cache.get(key)
    if sample is None:
        sample = Sample(path)
        if len(_sample_cache) > 8:
            _sample_cache.clear()
        _sample_cache[key] = sample
    return sample


def sample_seconds(path):
    """Duration of a .wav file in seconds (0.0 when it cannot be read)."""
    try:
        return load_sample(path).seconds
    except (ValueError, OSError):
        return 0.0


class _Device:
    """A winmm waveOut device opened for one sample's format."""

    def __init__(self, sample):
        if _winmm is None:
            raise RuntimeError("sample playback needs Windows (winmm)")
        self.sample = sample
        self.handle = ctypes.c_void_p()
        self._fmt = sample.format_struct()
        self._buffer = ctypes.create_string_buffer(sample.frames,
                                                   len(sample.frames))
        self._base = ctypes.addressof(self._buffer)
        self._header = _WaveHdr()
        self._prepared = False
        self._closed = False
        result = _winmm.waveOutOpen(ctypes.byref(self.handle), WAVE_MAPPER,
                                    ctypes.byref(self._fmt), 0, 0,
                                    CALLBACK_NULL)
        if result != 0:
            raise RuntimeError("cannot open the audio device "
                               f"(waveOutOpen error {result})")

    def play(self, position):
        """Start playing from a normalised position, cutting the previous one."""
        if self._closed:
            return
        self._release()
        offset = self.sample.frame_offset(position)
        length = len(self.sample.frames) - offset
        if length <= 0:
            return
        self._header.lpData = ctypes.c_void_p(self._base + offset)
        self._header.dwBufferLength = length
        self._header.dwFlags = 0
        if _winmm.waveOutPrepareHeader(self.handle, ctypes.byref(self._header),
                                       ctypes.sizeof(self._header)) != 0:
            return
        self._prepared = True
        _winmm.waveOutWrite(self.handle, ctypes.byref(self._header),
                            ctypes.sizeof(self._header))

    def _release(self):
        """Stop playback and free the prepared header."""
        if self._prepared:
            _winmm.waveOutReset(self.handle)
            _winmm.waveOutUnprepareHeader(self.handle,
                                          ctypes.byref(self._header),
                                          ctypes.sizeof(self._header))
            self._prepared = False

    def stop(self):
        self._release()

    def close(self):
        if self._closed:
            return
        self._release()
        _winmm.waveOutClose(self.handle)
        self._closed = True


_devices = []


def stop_audio():
    """Silence every open device (used by stop/panic/exit)."""
    for device in list(_devices):
        try:
            device.stop()
        except Exception:
            pass


def _close_all():
    for device in list(_devices):
        try:
            device.close()
        except Exception:
            pass
    _devices.clear()


atexit.register(_close_all)


class LoopPlayer(threading.Thread):
    """Background thread playing a sample from positions, on the BPM grid.

    provider() is called once per cycle and returns
    (sample, order, step_time, label) where
      sample    a sampler.Sample
      order     list of normalised positions (None = rest); an item may also be
                a callable(now) -> position|None, evaluated at each step
      step_time seconds per step
      label     short description used in messages
    Returning None (or an empty order) pauses the loop until the next cycle.
    """

    def __init__(self, provider, key=None):
        super().__init__(daemon=True)
        self.provider = provider
        self.key = key
        self._stop = threading.Event()
        self.port_name = None      # kept for the shared playback registry
        self.label = key
        self.position = 0
        self.error = None
        self._device = None

    @property
    def running(self):
        """Same interface as the MIDI Player: True while the thread plays."""
        return self.is_alive() and not self._stop.is_set()

    def stop(self):
        self._stop.set()
        if self._device is not None:
            try:
                self._device.stop()
            except Exception:
                pass

    def stop_requested(self):
        return self._stop.is_set()

    def run(self):
        try:
            self._run()
        finally:
            device, self._device = self._device, None
            if device is not None:
                if device in _devices:
                    _devices.remove(device)
                device.close()

    def _run(self):
        grid = time.monotonic()
        while not self._stop.is_set():
            try:
                spec = self.provider()
            except (ValueError, OSError, LookupError) as e:
                print(f"\n[loop] {e}")
                return
            if not spec:
                time.sleep(0.05)
                continue
            sample, order, step_time, label = spec
            self.label = label
            self.position = 0
            if not order or step_time <= 0:
                time.sleep(0.05)
                continue
            if self._device is None or self._device.sample is not sample:
                if self._device is not None:
                    if self._device in _devices:
                        _devices.remove(self._device)
                    self._device.close()
                try:
                    self._device = _Device(sample)
                except RuntimeError as e:
                    print(f"\n[loop] {e}")
                    return
                _devices.append(self._device)
            for item in order:
                if self._stop.is_set():
                    return
                try:
                    value = item(time.monotonic()) if callable(item) else item
                except Exception:
                    value = None      # a bad entry is silence, not a crash
                if value is not None:
                    self._device.play(float(value))
                grid += step_time
                delay = grid - time.monotonic()
                if delay > 0:
                    self._stop.wait(delay)
                self.position += 1
