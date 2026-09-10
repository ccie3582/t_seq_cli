"""Normalized LFO waveform evaluation (values in [-1, 1]) and parameter
expressions such as '0.5*lfo2' (modulated by another LFO).

Frequencies may also be tied to the global tempo: '8bpm' means 8 cycles per
beat (8 * BPM / 60 Hz), '1/8bpm' means 1/8 cycle per beat (one cycle every
8 beats). Both forms and plain Hertz numbers can be mixed freely.
"""

import math
import re

_PARAM_MOD_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*\*\s*lfo\s*(\d+)\s*$",
                           re.IGNORECASE)
_PARAM_LFO_ONLY_RE = re.compile(r"^\s*lfo\s*(\d+)\s*$", re.IGNORECASE)
_BPM_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*bpm\s*$", re.IGNORECASE)
_BPM_FRAC_RE = re.compile(
    r"^\s*(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)\s*bpm\s*$", re.IGNORECASE)


def parse_param(value):
    """Parse an LFO parameter value.

    Accepts a plain number ('2.5', '1', '-1'), an LFO-modulated expression
    ('0.5*lfo2', 'lfo2') or a tempo-relative multiple ('8bpm', '1/8bpm').

    Returns ('const', number), ('lfo', coefficient, index) or
    ('bpm', factor) where factor is cycles per beat.
    Raises ValueError for invalid input.
    """
    if isinstance(value, bool):
        return ("const", float(value))
    if isinstance(value, (int, float)):
        return ("const", float(value))
    text = str(value).strip()
    m = _PARAM_MOD_RE.match(text)
    if m:
        return ("lfo", float(m.group(1)), int(m.group(2)))
    m = _PARAM_LFO_ONLY_RE.match(text)
    if m:
        return ("lfo", 1.0, int(m.group(1)))
    m = _BPM_FRAC_RE.match(text)
    if m:
        return ("bpm", float(m.group(1)) / float(m.group(2)))
    m = _BPM_RE.match(text)
    if m:
        return ("bpm", float(m.group(1)))
    try:
        return ("const", float(text))
    except ValueError:
        raise ValueError(f"invalid parameter '{value}': expected a number or "
                         f"'<coef>*lfo<n>' (e.g. 0.5*lfo2)")


def factor_to_text(factor):
    """Render a cycles-per-beat factor as '2bpm', '1/8bpm', ..."""
    if factor == 1:
        return "1"
    if 0 < factor < 1:
        recip = 1.0 / factor
        if abs(recip - round(recip)) < 1e-9:
            return f"1/{int(round(recip))}"
    return f"{factor:g}"


def param_to_text(value):
    """Human-readable form of a stored parameter (number or expression)."""
    kind = parse_param(value)
    if kind[0] == "lfo":
        coef, n = kind[1], kind[2]
        return f"{coef:g}*lfo{n}"
    if kind[0] == "bpm":
        return f"{factor_to_text(kind[1])}bpm"
    return f"{kind[1]:g}"


def frequency_hz(value, bpm):
    """Effective frequency in Hertz for a constant or tempo-relative value.

    Returns None when the value is LFO-modulated (depends on time).
    """
    kind = parse_param(value)
    if kind[0] == "bpm":
        return kind[1] * bpm / 60.0
    if kind[0] == "const":
        return kind[1]
    return None


def _display_factor(factor):
    """Compact display of a derived cycles-per-beat factor."""
    if factor <= 0:
        return "0"
    recip = 1.0 / factor
    if recip > 1 and abs(recip - round(recip)) <= 0.005 * recip:
        return f"1/{int(round(recip))}"
    text = f"{factor:.3f}".rstrip("0").rstrip(".")
    return text or "0"


def describe_frequency(value, bpm):
    """Show a frequency both in Hertz and as a multiple of the global BPM."""
    kind = parse_param(value)
    if kind[0] == "bpm":
        return f"{param_to_text(value)} ({kind[1] * bpm / 60.0:g} Hz)"
    if kind[0] == "const":
        hz = kind[1]
        factor = hz * 60.0 / bpm if bpm else 0.0
        return f"{hz:g} Hz ({_display_factor(factor)}bpm)"
    return f"{param_to_text(value)} (modulated)"


def _wave(shape, frequency, phase, t):
    """Waveform value in [-1, 1]; phase is the start point in cycles."""
    if frequency <= 0:
        return 0.0
    pos = frequency * t + phase
    frac = pos - math.floor(pos)
    if shape == "sin":
        return math.sin(2 * math.pi * pos)
    if shape == "tri":
        return 1.0 - 4.0 * abs(frac - 0.5)
    if shape == "square":
        return 1.0 if frac < 0.5 else -1.0
    return 2.0 * frac - 1.0  # saw


def value(shape, frequency, phase, t):
    """Instantaneous normalized LFO value at time t (seconds).

    shape: 'sin', 'tri', 'saw' or 'square'; phase (cycles, -1..1) is the start
    point:
    the oscillator begins at that point of the waveform at t = 0.
    """
    return _wave(shape, frequency, phase, t)


def evaluate(lfos, index, t, bpm=120, _seen=None):
    """Evaluate the instantaneous normalized value of LFO <index> at time t.

    lfos is the project's {n: entry} store. t is an absolute (monotonic) time.
    The frequency may be a Hertz number, a tempo multiple ('8bpm') or an
    expression referencing another LFO. bpm is the global tempo used to convert
    tempo multiples.

    A stopped LFO outputs 0. A running LFO with a beat-aligned start latch
    ('_beat_start') outputs 0 until that beat, then runs from its configured
    phase — so restarting always begins like a first start.

    Raises ValueError on dependency cycles / invalid references.
    """
    if _seen is None:
        _seen = set()
    if index in _seen:
        raise ValueError("cyclic LFO modulation reference detected")
    _seen = _seen | {index}

    entry = lfos.get(index, {}) or {}
    if not entry.get("running", False):
        return 0.0
    start = entry.get("_beat_start")
    if start is not None:
        if t < start:
            return 0.0  # waiting for the next beat after being started
        elapsed = t - start
    else:
        elapsed = t  # legacy: running with no latched start

    def resolve(param, default):
        kind = parse_param(entry.get(param, default))
        if kind[0] == "lfo":
            coef, ref = kind[1], kind[2]
            if not 1 <= ref <= 8:
                raise ValueError(f"lfo reference out of range: lfo{ref}")
            return coef * evaluate(lfos, ref, t, bpm, _seen)
        if kind[0] == "bpm":
            return kind[1] * bpm / 60.0
        return kind[1]

    freq = resolve("frequency", 1.0)
    phase = float(entry.get("phase", 0.0))
    phase = max(-1.0, min(1.0, phase))
    return _wave(entry.get("shape", "sin"), freq, phase, elapsed)
