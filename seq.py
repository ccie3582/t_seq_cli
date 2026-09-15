"""seq — interactive CLI for a multi-track MIDI sequencer.

Command-line shell modeled on viplab_ip.py:
  * hyphenated commands mapped to do_* methods
  * Tab autocompletion (readline when available; a built-in key-by-key
    editor otherwise, so completion also works on Windows consoles)
  * `help` / `help <command>` inline help
  * contextual sub-menus: tracks (1-16) and, inside each track, patterns (1-16)

Run interactively:
    C:\\Python314\\python.exe seq.py

Run a single command non-interactively:
    C:\\Python314\\python.exe seq.py list-ports
"""

import argparse
import cmd
import copy
import math
import os
import re
import shlex
import fractions
import shutil
import subprocess
import sys
import threading
import time

import lfo
import midi_ports
import midiclock
import velocity
import phrases
import player
import sampler
import scales
import scdefs
import supercollider

try:
    import pyreadline3 as readline
except ImportError:
    try:
        import readline
    except ImportError:
        readline = None

HAVE_READLINE = readline is not None and hasattr(readline, "parse_and_bind")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Command name -> helper script that implements it.
COMMAND_SCRIPTS = {
    "list-ports": "list-ports.py",
}

MAX_TRACKS = 16
MAX_PATTERNS = 16
MAX_PHRASES = 8   # phrases are f0..f7
MAX_LFOS = 8

LFO_DEFAULTS = {"frequency": 1.0, "shape": "sin", "phase": 0.0}
LFO_SHAPES = ("sin", "tri", "saw", "square", "ramp", "random")  # ramp 0..1

DEFAULT_BPM = 120
BPM_MIN = 1
BPM_MAX = 300

DEFAULT_CHANNEL = 1
CHANNEL_MIN = 1
CHANNEL_MAX = 16

DEFAULT_DIVISIONS = 16  # default step division denominator (1/16 note)
# A pattern "division" is the note value of one step: 1, 1/2, 1/4, 1/8,
# 1/16, 1/32, 1/64 or any other 1/<integer> (1/3, 1/5, 1/7, ...).
LFO_START_KEY = "_beat_start"  # runtime latch: beat-aligned LFO start time
LFO_LIVE_FPS = 30        # live LFO meter refresh rate (lower = less flicker)


def make_prompt(menu_path):
    """Legacy verbose prompt builder (kept only for compatibility)."""
    return ":".join(menu_path) + "> "


_PATH_TOKEN_RE = re.compile(
    r"(?i)^(?:t(\d+))?(?:p(\d+))?"
    r"(?:(?:([sfvuo])(\d+))|(?:sus(\d+))|(?:mt(\d+)))?$")
_PATH_MAX_SEQ = 9   # sequences are s0..s9
_PATH_MAX_PHRASE = MAX_PHRASES - 1   # f0..f7
_PATH_MAX_ORDER = sampler.MAX_ORDER_LISTS - 1   # order lists are o0..o9
PATTERN_TYPES = ("note", "cc", "loop", "osc")


def parse_compact_path(text, track=None, pattern=None):
    """Parse a compact location like 't1', 't1p2', 't1p2s0', 'p2f3'.

    Missing leading parts are filled from the given context (track/pattern).
    Returns {'track', 'pattern', 'kind', 'index'} where kind is 's', 'f', 'v',
    'u' (sustain, written 'sus'), 'm' (microtiming, written 'mt') or None.
    Raises ValueError with a user-facing message when invalid.
    """
    token = (text or "").strip()
    if not token:
        raise ValueError("empty path")
    m = _PATH_TOKEN_RE.fullmatch(token)
    if not m:
        raise ValueError(f"invalid path '{token}' (use t<n>, t<n>p<m>, "
                         f"t<n>p<m>s<k>|f<k>|v<k>|sus<k>|mt<k>|o<k>)")
    t = int(m.group(1)) if m.group(1) else track
    p = int(m.group(2)) if m.group(2) else pattern
    if m.group(3):
        kind = m.group(3).lower()
        index = int(m.group(4))
    elif m.group(5):
        kind, index = "u", int(m.group(5))
    elif m.group(6):
        kind, index = "m", int(m.group(6))
    else:
        kind = index = None
    if p is None:
        if kind is not None or m.group(2):
            raise ValueError("a pattern is required (use t<n>p<m>)")
    if t is None:
        raise ValueError("a track is required (use t<n>)")
    if not 1 <= t <= MAX_TRACKS:
        raise ValueError(f"track must be 1-{MAX_TRACKS}")
    if p is not None and not 1 <= p <= MAX_PATTERNS:
        raise ValueError(f"pattern must be 1-{MAX_PATTERNS}")
    if kind == "s" and not 0 <= index <= _PATH_MAX_SEQ:
        raise ValueError(f"sequence must be s0..s{_PATH_MAX_SEQ}")
    if kind == "f" and not 0 <= index <= _PATH_MAX_PHRASE:
        raise ValueError(f"phrase must be f0..f{_PATH_MAX_PHRASE}")
    if kind == "v" and not 0 <= index <= _PATH_MAX_SEQ:
        raise ValueError(f"velocity must be v0..v{_PATH_MAX_SEQ}")
    if kind == "u" and not 0 <= index <= _PATH_MAX_SEQ:
        raise ValueError(f"sustain must be sus0..sus{_PATH_MAX_SEQ}")
    if kind == "m" and not 0 <= index <= _PATH_MAX_SEQ:
        raise ValueError(f"microtiming must be mt0..mt{_PATH_MAX_SEQ}")
    if kind == "o" and not 0 <= index <= _PATH_MAX_ORDER:
        raise ValueError(f"order list must be o0..o{_PATH_MAX_ORDER}")
    return {"track": t, "pattern": p, "kind": kind, "index": index}


def old_command_hint(token):
    """Hint text when a removed long command is used (else None)."""
    low = token.lower()
    if low == "track":
        return "use t<n> (e.g. t1) to enter a track"
    if low == "pattern":
        return "use p<n> (e.g. p1) to enter a pattern"
    if low == "phrase":
        return "phrases are defined in the pattern: use f<n> <expression>"
    if low == "amp":
        return "amplitude is fixed at 1; use phase (-1..1) to shift the start point"
    if low in ("list-seq", "list_s", "list_seq"):
        return "inside a pattern, use list-s to list the sequences"
    if low in ("list-phrase", "list_phrase"):
        return "inside a pattern, use list-f to list the phrases"
    if re.fullmatch(r"seq\d+", low):
        return f"use {low.replace('seq', 's')} <degrees> (e.g. s0 0 1 3)"
    return None


def _intervals_text(intervals):
    """'(0,2,4,7,9)' for a scale."""
    return "(" + ",".join(str(iv) for iv in intervals) + ")"


def _vector_text(intervals):
    """Interval vector as digits, e.g. '254361'."""
    return "".join(str(count) for count in scales.interval_vector(intervals))


def parse_bpm(arg):
    """Parse an integer BPM argument (1-300). None on error (already printed)."""
    text = (arg or "").strip()
    try:
        value = int(text)
    except ValueError:
        print(f"Usage: bpm <integer> ({BPM_MIN}-{BPM_MAX})")
        return None
    if not BPM_MIN <= value <= BPM_MAX:
        print(f"Error: BPM must be between {BPM_MIN} and {BPM_MAX}.")
        return None
    return value


_ANSI_CAPABLE = None


# Short command names accepted anywhere (e.g. 'freq 0.5' in an LFO menu).
_CMD_ALIASES = {
    "freq": "frequency",
}


def _enable_ansi():
    """Enable ANSI escape support on this stdout (once). False when redirected."""
    global _ANSI_CAPABLE
    if _ANSI_CAPABLE is None:
        if not sys.stdout.isatty():
            _ANSI_CAPABLE = False
        elif os.name == "nt":
            try:
                import ctypes
                kernel32 = ctypes.windll.kernel32
                handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
                mode = ctypes.c_uint32()
                if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                    kernel32.SetConsoleMode(handle, mode.value | 0x0004)
                _ANSI_CAPABLE = True
            except Exception:
                _ANSI_CAPABLE = False
        else:
            _ANSI_CAPABLE = True
    return _ANSI_CAPABLE


def paint_green(text):
    """Wrap text in ANSI bright-green when the terminal supports colors."""
    if _enable_ansi():
        return f"\x1b[92m{text}\x1b[0m"
    return text


def choose_midi_output_port():
    """Show MIDI output ports and return the selected name (None = cancelled)."""
    ports = midi_ports.get_output_port_info()
    if not ports:
        print("No MIDI output ports available.")
        return None
    print("\nAvailable MIDI output ports:")
    for i, (name, kind) in enumerate(ports, 1):
        if kind:
            print(f"  {i}. {name}  ({kind})")
        else:
            print(f"  {i}. {name}")
    while True:
        try:
            choice = input(f"Select port number 1-{len(ports)} (Enter to cancel): ").strip()
        except EOFError:
            print()
            print("Selection cancelled.")
            return None
        if not choice:
            print("Selection cancelled.")
            return None
        try:
            idx = int(choice)
        except ValueError:
            print(f"Invalid choice '{choice}'. Enter 1-{len(ports)} or Enter to cancel.")
            continue
        if not 1 <= idx <= len(ports):
            print(f"Invalid choice {idx}. Enter 1-{len(ports)} or Enter to cancel.")
            continue
        return ports[idx - 1][0]


def parse_index(arg, lo, hi, what):
    """Parse an integer menu argument enforcing lo <= n <= hi.

    Returns the integer or None (error message already printed).
    """
    text = (arg or "").strip()
    try:
        n = int(text)
    except ValueError:
        print(f"Usage: {what} <number> ({lo}-{hi})")
        return None
    if not lo <= n <= hi:
        print(f"Error: {what} number must be between {lo} and {hi}.")
        return None
    return n


def variation_term_list(variation):
    """Variation as a tuple of terms; each term is a tuple of (coef, ref).

    A term is a product, the whole variation is their sum, e.g.
    ``127*lfo1``      -> (((127.0, 1),),)
    ``2*lfo1*lfo2``   -> (((2.0, 1), (1.0, 2)),)
    ``63+63*lfo1``    -> (((63.0, 1),),)      (the 63 is the entry's degree)
    """
    if not variation:
        return ()
    out = []
    for term in variation:
        try:
            pairs = tuple((float(coef), int(ref)) for coef, ref in term)
        except (TypeError, ValueError):
            continue
        if pairs:
            out.append(pairs)
    return tuple(out)


def variation_terms(variation):
    """All (coefficient, lfo index) pairs of a variation (flattened)."""
    return tuple(pair for term in variation_term_list(variation) for pair in term)


def variation_offset(variation, lfos, bpm, now, unit=None):
    """Offset in scale steps / CC units: sum of coef x lfo1 x lfo2 ...

    Regular algebra: each term is a product (a term's coefficient may itself be
    a product, e.g. 2*3*lfo1 = 6*lfo1) and the terms are added together. The
    result is rounded to an integer unless 'unit' is given (positions are
    fractional, so the caller passes unit=1.0 to keep the float).
    """
    total = 0.0
    for term in variation_term_list(variation):
        product = 1.0
        for coef, ref in term:
            try:
                product *= lfo.evaluate(lfos or {}, ref, now, bpm)
            except ValueError:
                product = 0.0
                break
            product *= coef
        total += product
    if unit is None:
        return int(round(total))
    return total


def variation_text(variation):
    """Render a variation: '+63*lfo1', '-2*lfo1*lfo2' (signs included)."""
    parts = []
    for term in variation_term_list(variation):
        coef = 1.0
        refs = []
        for factor, ref in term:
            coef *= factor
            refs.append(ref)
        sign = "-" if coef < 0 else "+"
        body = "*".join([f"{abs(coef):g}"] + [f"lfo{r}" for r in refs])
        parts.append(sign + body)
    return "".join(parts)


def parse_seq_expr(text):
    """Parse the numeric algebra of a sequence token.

    Products bind tighter than sums (regular algebra):
      '127*lfo1'      -> (0, ((127.0, 1),))         127 x lfo1
      '63+63*lfo1'    -> (63, ((63.0, 1),))         63 + 63 x lfo1
      '2*lfo1*lfo2'   -> (0, ((2.0, 1), (1.0, 2)))  2 x lfo1 x lfo2
      '30-2*lfo1'     -> (30, ((-2.0, 1),))         30 - 2 x lfo1
      '5'             -> (5, ())                    a constant
    Returns (constant degree, variation terms). Raises ValueError when invalid.
    """
    body = (text or "").strip()
    if not body:
        raise ValueError("empty sequence expression")
    constant = 0.0
    terms = []
    pos = 0
    for m in re.finditer(r"([+-]?)([^+-]+)", body):
        if m.start() != pos:  # e.g. '1++2' or a stray sign
            raise ValueError(f"invalid sequence token '{body}' "
                             f"(use e.g. 127*lfo1, 63+63*lfo1, 5*lfo1*lfo2)")
        pos = m.end()
        sign = -1.0 if m.group(1) == "-" else 1.0
        factors = m.group(2).split("*")
        coef = sign
        refs = []
        for factor in factors:
            if not factor:
                raise ValueError(f"invalid sequence token '{body}' "
                                 f"(use e.g. 127*lfo1, 63+63*lfo1)")
            if re.fullmatch(r"(?i)lfo\d+", factor):
                refs.append(int(factor[3:]))
                continue
            try:
                coef *= float(factor)
            except ValueError:
                raise ValueError(
                    f"invalid sequence token '{body}': '{factor}' is not a "
                    f"number or lfo<N> (use e.g. 127*lfo1, 63+63*lfo1)")
        if not refs:
            constant += coef          # a plain number: part of the degree
        elif coef:                    # a term that multiplies an LFO
            terms.append(((coef, refs[0]),) + tuple((1.0, r) for r in refs[1:]))
    if pos != len(body):
        raise ValueError(f"invalid sequence token '{body}'")
    return constant, tuple(terms)


def seq_token_entry(tok):
    """Parse one sequence token -> None (rest) or (degree, shift, variation).

    The value of a step is ``degree + sum(coef x lfo1 x lfo2 x ...)``: products
    first, then sums (see parse_seq_expr). For note patterns the result is a
    scale degree (octaves wrap); for CC patterns it is the controller value.
    """
    if tok.lower() == "r":
        return None
    m = re.fullmatch(r"(?i)([Oo]*)(.+)", (tok or "").strip())
    if not m:
        raise ValueError(f"invalid sequence token {tok!r}")
    shift = m.group(1).count("O") - m.group(1).count("o")
    constant, terms = parse_seq_expr(m.group(2))
    if abs(constant - round(constant)) > 1e-9:
        raise ValueError(f"invalid sequence token '{tok}': the constant part "
                         f"must be a whole number ({constant:g})")
    return (int(round(constant)), shift, terms or None)


def _entry_text(entry):
    if entry is None:
        return "r"
    degree, shift, variation = entry
    prefix = "O" * shift if shift > 0 else "o" * (-shift)
    if not variation:
        return f"{prefix}{degree}"
    text = variation_text(variation)      # '+63*lfo1', '-2*lfo1*lfo2'
    if degree == 0:
        return f"{prefix}{text.lstrip('+')}"
    return f"{prefix}{degree}{text}"


def parse_division(text):
    """Parse a pattern division -> denominator n (step = 1/n note).

    Accepts '1', '1/16', '1/3', ... and a bare integer denominator ('16').
    Raises ValueError with a user-facing message when invalid.
    """
    token = (text or "").strip().replace(" ", "")
    if not token:
        raise ValueError("empty division")
    m = re.fullmatch(r"1(?:/(\d+))?", token)
    if m:
        n = int(m.group(1)) if m.group(1) else 1
    else:
        m = re.fullmatch(r"(\d+)", token)
        if not m:
            raise ValueError(f"invalid division '{text}': use 1, 1/2, 1/4, "
                             f"1/8, 1/16, 1/32, 1/64 or any 1/<integer>")
        n = int(m.group(1))
    if n < 1:
        raise ValueError("division denominator must be 1 or greater")
    if n > 4096:
        raise ValueError("division denominator is too large")
    return n


def division_to_text(n):
    """Canonical text for a division denominator ('1', '1/16', '1/3')."""
    return "1" if n == 1 else f"1/{n}"


def division_step_ms(bpm, n):
    """Length of one step in milliseconds for the given tempo and division."""
    return 1000.0 * player.step_time_for(bpm, n)


_HIST_CELL = 5  # one column per division step
_HIST_MAX = 32  # most columns rendered for very fine divisions
_HIST_SPIN = ("|", "/", "-", chr(92))  # spinner frames
_HIST_VEL_GLYPHS = "\u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"
_HIST_SUS_GLYPHS = "\u258f\u258e\u258d\u258c\u258b\u258a\u2589\u2588"
_HIST_VEL_ASCII = ".:-=+*#@"
_HIST_SUS_ASCII = ".:-=+*#@"


def _frac_text(value, max_denominator=64):
    """Render a step value as a fraction when it is one (1/8, 3/4, 3/2)."""
    frac = fractions.Fraction(value).limit_denominator(max_denominator)
    if frac.denominator > 1 and abs(float(frac) - value) < 1e-9:
        return f"{frac.numerator}/{frac.denominator}"
    return f"{value:.2f}".rstrip("0").rstrip(".") or "0"


def _term_width():
    """Terminal width in columns (0 when unknown)."""
    try:
        return shutil.get_terminal_size().columns
    except Exception:
        return 0


def output_encoding():
    """Encoding of the terminal, even while stdout is redirected to a buffer."""
    for stream in (sys.stdout, getattr(sys, "__stdout__", None)):
        enc = getattr(stream, "encoding", None)
        if enc:
            return enc
    return "utf-8"


def _glyph_sets():
    """Block glyphs when the output can carry them, ASCII otherwise."""
    enc = output_encoding()
    try:
        "█".encode(enc)
        return _HIST_VEL_GLYPHS, _HIST_SUS_GLYPHS, "·"
    except (UnicodeEncodeError, LookupError):
        return _HIST_VEL_ASCII, _HIST_SUS_ASCII, "."



def midi_note_name(midi):
    """Name of a MIDI note number, e.g. 60 -> 'C4'."""
    return f"{scales.CHROMATIC_SHARPS[midi % 12]}{midi // 12 - 1}"


def histogram_lines(view, lfos, bpm, now, phrase_index, live=False,
                    expr=None, label=None, playhead=None, spin=0):
    """Horizontal per-step histogram of one phrase (or an explicit expression).

    One column per division step (division denominator columns per bar) with
    the note name, velocity, sustain share and microtiming offset of the step
    that lands there. Values are evaluated at 'now' (LFO snapshots).
    """
    if expr is None:
        expr = view["phrases"].get(phrase_index)
    if expr is None:
        if phrase_index is None:
            return []  # no target selected: nothing to draw
        return [f"f{phrase_index} is not configured in this pattern "
                f"(hist f<k>|s<k> to pick another)."]
    steps = max(1, int(view["division"]))
    width = _term_width()
    column_budget = _HIST_MAX if not width else max(
        4, (width - 8) // _HIST_CELL)
    truncated = steps > min(_HIST_MAX, column_budget)
    steps = min(steps, _HIST_MAX, column_budget)
    step_ms = player.step_time_for(bpm, view["division"]) * 1000.0
    try:
        plan, _ = player.flatten_phrase(expr, lambda kind, i: view["seqs"].get(i)
                                        if kind == "seq" else None)
    except ValueError as e:
        return [f"cannot render phrase f{phrase_index}: {e}"]
    if not plan:
        return [f"phrase f{phrase_index} yields no steps."]

    intervals = scales.SCALES[view["scale"]]
    size = len(intervals)
    root_midi = view["root"].midi

    def lfo_value(expression, default, lo=None, hi=None):
        if not expression:
            return default
        try:
            if lo is None:
                return velocity.evaluate(expression, lfos, bpm, now)
            return velocity.evaluate_span(expression, lfos, bpm, now, lo, hi)
        except ValueError:
            return default

    cc = view.get("type") == "cc"
    notes, vels, suss, mts, mt_set = [], [], [], [], []
    for i in range(steps):
        seq_index, entry = plan[i % len(plan)]
        if entry is None:
            notes.append(None)
            vels.append(None)
            suss.append(None)
            mts.append(None)
            mt_set.append(False)
            continue
        degree, shift, variation = entry
        offset = variation_offset(variation, lfos, bpm, now)
        mt_expr = view["microtimes"].get(seq_index)
        mts.append(lfo_value(mt_expr, 0.0, -player.MICROTIME_MAX_STEPS,
                             player.MICROTIME_MAX_STEPS) * step_ms)
        mt_set.append(bool(mt_expr))
        if cc:
            notes.append(str(max(0, min(127, degree + offset + 12 * shift))))
            vels.append(None)
            suss.append(None)
            continue
        index = degree + offset
        octave, pos = divmod(index, size)
        midi = fold_midi(root_midi + 12 * (octave + shift) + intervals[pos])
        notes.append(midi_note_name(midi))
        vels.append(lfo_value(view["velocities"].get(seq_index), player.VELOCITY))
        suss.append(lfo_value(view["sustains"].get(seq_index),
                              player.SUSTAIN_FRACTION, 0.0,
                              player.SUSTAIN_MAX_STEPS))

    vel_glyphs, sus_glyphs, rest_glyph = _glyph_sets()

    def row(label, cells):
        return f"  {label:<6}" + "".join(
            f"{cell:<{_HIST_CELL}}" if cell is not None else " " * _HIST_CELL
            for cell in cells)

    def mt_cell(m, defined, step_ms=step_ms):
        """Microtiming in steps: '<1/4' early, '> 1/2' late, '|' on the grid."""
        if m is None:
            return None  # no note
        if not defined:
            return "|"  # note on the grid (no microtiming sequence)
        steps = m / step_ms if step_ms else 0.0
        if abs(steps) < 1e-9:
            return "|  0"
        marker = "<" if steps < 0 else ">"
        width = _HIST_CELL - 1
        txt = _frac_text(abs(steps))          # sign is shown by the marker
        if len(txt) > width:                   # very fine value: two decimals
            txt = f"{abs(steps):.2f}".rstrip("0").rstrip(".") or "0"
        if len(txt) > width:                   # still too fine: show ms
            txt = f"{abs(m):.0f}"
        return marker + f"{txt:>{width}}"

    def mark(i):
        if playhead is not None and i == playhead % steps:
            return ">" + f"{i:<{_HIST_CELL - 1}}"
        return f"{i:<{_HIST_CELL}}"

    header = "  " + " " * 6 + "".join(mark(i) for i in range(steps))
    beat_line = "  " + " " * 6 + "".join(
        ("|" if i and i % 4 == 0 else "-") + "-" * (_HIST_CELL - 1)
        for i in range(steps))
    if cc:
        val_row = row("val", [n if n else "\u00b7" for n in notes])
    else:
        val_row = None
    note_row = row("note", [n if n else "\u00b7" for n in notes])
    vel_row = row("vel", [
        f"{vel_glyphs[min(7, int(round(v / 127.0 * 7)))]}{v:>3}"
        if v is not None else None for v in vels])
    def sus_cell(s):
        """Sustain in steps, shown as a fraction when it is one (1/8, 3/4, 3/2)."""
        txt = _frac_text(s)
        if len(txt) > _HIST_CELL - 1:
            txt = f"{s:.2f}".rstrip("0").rstrip(".") or "0"
        glyph = sus_glyphs[min(7, int(round(min(1.0, s / 2.0) * 7)))]
        if len(txt) <= _HIST_CELL - 1:
            return glyph + f"{txt:>{_HIST_CELL - 1}}"
        return f"{txt:^{_HIST_CELL}}"  # very long value: drop the glyph

    sus_row = row("sus", [sus_cell(s) if s is not None else None for s in suss])
    mt_row = row("mt", [mt_cell(m, d) for m, d in zip(mts, mt_set)])

    # Explain all-zero LFO-linked values (stopped or still waiting for a beat).
    refs = set()
    for expression in (list(view["velocities"].values())
                       + list(view["sustains"].values())
                       + list(view["microtimes"].values())):
        try:
            refs.update(ref for _coef, ref in velocity.parse(expression)
                        if ref is not None)
        except ValueError:
            pass
    stopped = sorted(n for n in refs if not (lfos or {}).get(n, {}).get("running"))
    waiting = sorted(n for n in refs
                     if (lfos or {}).get(n, {}).get("running")
                     and (lfos[n].get("_beat_start") or 0) > now)
    notes_hint = []
    if stopped:
        notes_hint.append("LFO-linked values read 0: "
                          + ", ".join(f"lfo{n} stopped" for n in stopped))
    if waiting:
        notes_hint.append(", ".join(f"lfo{n} starts at the next beat"
                                    for n in waiting))
    lfo_note = ("   (" + "; ".join(notes_hint) + ")") if notes_hint else ""

    if cc:
        mode_text = "  values 0-127"
    else:
        mode_text = f"  {view['scale']} on {view['root'].name()}"
    title = (f"{label or f'phrase f{phrase_index}'}{lfo_note}"
             f"{'  type CC%d' % view['controller'] if cc else ''}"
             f"  division {division_to_text(view['division'])}"
             f"  step {step_ms:g} ms  bpm {view['bpm']}"
             f"{mode_text}"
             f"{'   (live)' if live else ''}"
             f"{'   step ' + str(playhead % steps) if playhead is not None else ''}"
             f"{'  ' + _HIST_SPIN[spin % 4] if live else ''}"
             f"{'   (showing %d of %d steps)' % (steps, view['division'])
                if truncated else ''}")
    if cc:
        lines = [title, "", header, beat_line, val_row, mt_row]
    else:
        lines = [title, "", header, beat_line, note_row, vel_row, sus_row, mt_row]
    if width:
        lines = [line[:max(1, width - 1)] for line in lines]
    return lines


_HIST_POS_ROWS = 16   # most step rows drawn for a long phrase


def loop_histogram_lines(view, lfos, bpm, now, expr=None, label=None,
                         live=False, playhead=None, spin=0):
    """Live histogram of the positions a loop phrase plays.

    The sample's length is the horizontal axis (0 on the left, 1 on the right);
    one row per step shows where that step starts inside the sample (marker) and
    how much of the sample still plays (the line after it, until the next step
    or the end of the file). Values are evaluated at 'now' (LFO snapshots).
    """
    if expr is None:
        return ["Nothing to show: no order list or phrase in this pattern."]
    try:
        plan, _infinite = player.flatten_phrase(
            expr, lambda kind, idx: view["orders"].get(idx)
            if kind == "order" else None)
    except (ValueError, phrases.PhraseError) as e:
        return [f"cannot render {label or 'this loop'}: {e}"]
    if not plan:
        return [f"{label or 'this loop'} yields no steps."]
    step_ms = player.step_time_for(bpm, view["division"]) * 1000.0
    values = [order_entry_value(entry, lfos, bpm, now) for _i, entry in plan]
    refs = sorted({ref for _index, entry in plan if isinstance(entry, str)
                   for _coef, ref in variation_terms(parse_seq_expr(entry)[1])})

    velocities = []
    vel_expressions = view.get("velocities") or {}
    for index, _entry in plan:
        vexpr = vel_expressions.get(index)
        if not vexpr:
            velocities.append(None)
            continue
        try:
            velocities.append(velocity.evaluate(vexpr, lfos, bpm, now))
        except ValueError:
            velocities.append(None)
    uses_velocity = any(v is not None for v in velocities)

    width = _term_width()
    rows = len(values)
    shown = min(rows, _HIST_POS_ROWS) if width else rows
    truncated_rows = shown < rows
    indent, label_w, value_w = 2, 6, 7
    vel_w = 6 if uses_velocity and not truncated_rows else 0
    if width:
        axis_w = max(16, width - indent - label_w - value_w - vel_w - 1)
    else:
        axis_w = 44

    # How much of the sample sounds per step: from the position to either the
    # next step (the step time) or the end of the file, whichever comes first.
    step_s = player.step_time_for(bpm, view["division"])
    sample_s = 0.0
    if view.get("sample"):
        sample_s = sampler.sample_seconds(
            sampler.resolve_sample(view["sample"])[0] or "")

    _vel, _sus, dot = _glyph_sets()
    try:
        "\u25cf\u2500".encode(output_encoding())   # cp1252 lacks these
        marker, trail = "\u25cf", "\u2500"
    except (UnicodeEncodeError, LookupError):
        marker, trail = "*", "-"
    tick = "|"

    def played_share(position):
        """Fraction of the sample that sounds before the next step cuts it."""
        remaining = max(0.0, 1.0 - position)
        if sample_s <= 0:
            return remaining
        return min(remaining, step_s / sample_s)

    def axis_for(position):
        cells = [dot] * axis_w
        if position is None:
            return "".join(cells)
        col = int(round(max(0.0, min(1.0, position)) * (axis_w - 1)))
        end = position + played_share(position)
        last = int(round(max(0.0, min(1.0, end)) * (axis_w - 1)))
        cells[col] = marker
        for i in range(col + 1, min(axis_w, last + 1)):
            cells[i] = trail       # this part of the sample is heard
        return "".join(cells)

    def ruler():
        cells = ["-"] * axis_w
        for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
            cells[int(round(frac * (axis_w - 1)))] = tick
        return "".join(cells)

    def numbers():
        cells = [" "] * axis_w
        for frac, text in ((0.0, "0"), (0.25, "1/4"), (0.5, "1/2"),
                           (0.75, "3/4"), (1.0, "1")):
            col = int(round(frac * (axis_w - 1)))
            if frac == 0.0:
                col = 0
            elif frac == 1.0:
                col = max(0, axis_w - len(text))
            for k, ch in enumerate(text):
                if col + k < axis_w:
                    cells[col + k] = ch
        return "".join(cells)

    lines = []
    head = [f"{label or 'loop'}", f"step {step_ms:g} ms", f"bpm {view['bpm']}",
            f"division {division_to_text(view['division'])}"]
    sample = view.get("sample")
    if sample:
        head.append(sample)
    extra = ""
    if live:
        extra += "   (live)"
    if playhead is not None:
        extra += f"   step {int(playhead) % max(1, rows)}"
        extra += "  " + _HIST_SPIN[spin % 4]
    if truncated_rows:
        extra += f"   (showing {shown} of {rows} steps)"
    if refs:
        stopped = [r for r in refs if not (lfos or {}).get(r, {}).get("running")]
        if stopped:
            extra += ("   (" + ", ".join(f"lfo{r} stopped -> position 0"
                                         for r in stopped) + ")")
    lines.append("  ".join(head) + extra)
    lines.append("")
    gap = " " * (indent + label_w)
    lines.append(gap + numbers())
    lines.append(gap + ruler())
    for i in range(shown):
        tag = f"s{i + 1}"
        if playhead is not None and i == int(playhead) % max(1, rows):
            tag = ">" + tag[1:]
        value = values[i]
        text = "rest" if value is None else f"{value:.3f}".rstrip("0").rstrip(".")
        vel_text = ""
        if vel_w:
            vel = velocities[i]
            vel_text = f"{'v' + str(vel):>{vel_w}}" if vel is not None else " " * vel_w
        lines.append(f"{' ' * indent}{tag:<{label_w}}{axis_for(value)}"
                     f"{text:>{value_w}}{vel_text}")
    if width:
        lines = [line[:max(1, width - 1)] for line in lines]
    return lines


def bars_text(steps, division):
    """How a step count sits in 4/4 bars at a division ('' when unknown)."""
    if not steps or not division or division < 1:
        return ""
    per_bar = int(division)  # 1/16 -> 16 steps per 4/4 bar
    if steps == per_bar:
        return "1 bar (4/4)"
    if per_bar % steps == 0:
        return f"{per_bar // steps} passes per 4/4 bar"
    if steps < per_bar:
        return f"{steps}/{per_bar} of a 4/4 bar"
    return f"{round(steps / per_bar, 2):g} bars (4/4)"


def cc_lfo_note(entries, lfos, bpm):
    """Explain how the LFOs affect a CC sequence (range, saturation, stopped).

    Returns '' when the sequence has no LFO terms. The range is the envelope the
    expression can reach: +/-|product of coefficients| for bipolar shapes, and
    one-sided for a ramp (0..1); a stopped LFO contributes exactly 0.
    """
    lo = hi = None
    refs = []
    for entry in entries:
        if entry is None:
            continue
        degree, shift, variation = entry
        if variation is None:
            continue
        base = degree + 12 * shift
        step_lo = step_hi = 0.0
        for term in variation_term_list(variation):
            product = 1.0
            unipolar = False
            stopped = False
            for coef, ref in term:
                entry_lfo = (lfos or {}).get(ref) or {}
                refs.append(ref)
                if not entry_lfo.get("running", False):
                    stopped = True
                    break
                if str(entry_lfo.get("shape", "sin")).lower() == "ramp":
                    unipolar = True
                product *= coef
            if stopped:
                continue  # a stopped LFO makes this whole term 0
            if unipolar:
                step_lo += min(0.0, product)
                step_hi += max(0.0, product)
            else:
                step_lo += -abs(product)
                step_hi += abs(product)
        v_lo = max(0, min(127, int(round(base + step_lo))))
        v_hi = max(0, min(127, int(round(base + step_hi))))
        lo = v_lo if lo is None else min(lo, v_lo)
        hi = v_hi if hi is None else max(hi, v_hi)
    if lo is None:
        return ""  # no LFO terms at all
    names = ", ".join(f"lfo{n}" for n in sorted(set(refs)))
    stopped_refs = sorted({n for n in refs
                           if not ((lfos or {}).get(n) or {}).get("running")})
    if stopped_refs:
        who = ", ".join(f"lfo{n}" for n in stopped_refs)
        return f"{who} stopped -> contributes 0 (value stays {hi})"
    if lo == hi:
        return (f"LFO range {lo}..{hi}: no headroom left, so {names} looks "
                f"frozen - use a lower base, e.g. 63*30*lfo1")
    return f"LFO range {lo}..{hi}"


def describe_cc_values(entries, lfos, bpm, now, notes=False):
    """Render a CC sequence: current values 0..127 (or 'r' for skipped steps)."""
    offsets = sequence_offsets(entries, lfos, bpm, now)
    out = []
    for entry, offset in zip(entries, offsets):
        if entry is None:
            out.append("r")
            continue
        degree, shift, _variation = entry
        out.append(str(max(0, min(127, degree + offset + 12 * shift))))
    text = " ".join(out)
    if notes:
        note = cc_lfo_note(entries, lfos, bpm)
        if note:
            text += f"   [{note}]"
    return text


def seq_to_text(entries):
    """Encode a stored sequence (list of entries | None) to text."""
    return " ".join(_entry_text(entry) for entry in entries)


def text_to_seq(text):
    """Decode '0 O0 o3 r 0*5*lfo1' text back into a stored sequence."""
    return [seq_token_entry(tok) for tok in (text or "").split()]


def scale_note_name(index, root, scale_key):
    """Note name for a scale index, wrapping across octaves (negatives too)."""
    intervals = scales.SCALES[scale_key]
    size = len(intervals)
    octave, pos = divmod(index, size)
    names = scales.scale_notes(root, intervals)
    m = re.fullmatch(r"(.*?)(-?\d+)$", names[pos])
    return f"{m.group(1)}{int(m.group(2)) + octave}"


def fold_midi(midi):
    """Fold a note number by octaves into 0..127 (never drop a note)."""
    while midi > 127:
        midi -= 12
    while midi < 0:
        midi += 12
    return midi


def sequence_offsets(entries, lfos, bpm, now):
    """Current LFO-step offset per entry (0 for static entries/rests)."""
    offsets = []
    for entry in entries:
        if entry is None or entry[2] is None:
            offsets.append(0)
            continue
        offsets.append(variation_offset(entry[2], lfos, bpm, now))
    return offsets


def resolve_seq_notes(entries, root, scale_key, lfos=None, bpm=DEFAULT_BPM,
                      now=None):
    """Resolve a stored sequence to note names under a given root/scale key.

    LFO variations are resolved at 'now' (a snapshot for display); use
    sequence_offsets() for playback, which recomputes per note.
    """
    if now is None:
        now = getattr(time, "monotonic")()
    offsets = sequence_offsets(entries, lfos, bpm, now)
    names = []
    for entry, offset in zip(entries, offsets):
        if entry is None:  # rest
            names.append("r")
            continue
        degree, shift, variation = entry
        # The degree is relative to the pattern's current scale, so changing
        # the scale later just re-reads the same degree in the new scale.
        # Degrees beyond the scale wrap into the next octave.
        name = scale_note_name(degree + offset, root, scale_key)
        m = re.fullmatch(r"(.*?)(-?\d+)$", name)
        names.append(f"{m.group(1)}{int(m.group(2)) + shift}")
    return " ".join(names)


def position_to_text(value):
    """Compact text for a normalised position ('0.2', '0.75', '0')."""
    return f"{float(value) % 1.0:.4f}".rstrip("0").rstrip(".") or "0"


def order_to_text(order):
    """Text form of an order list: positions, expressions, 'r' for a rest."""
    return " ".join("r" if item is None else
                    item if isinstance(item, str) else position_to_text(item)
                    for item in order)


def order_entry_value(entry, lfos, bpm, now):
    """Normalised position (0..1) to play for one entry, or None for a rest.

    The sample's length is 1, so an entry is a position in it: 0.2 means "start
    at 20 %". An expression entry ('lfo1', '0.3+0.1*lfo1') is evaluated with the
    same algebra as sequences - products first, then sums. Positions wrap
    modulo 1, so negative values work too (-0.2 -> 0.8).
    """
    if entry is None:
        return None
    if isinstance(entry, (int, float)):
        return float(entry) % 1.0
    try:
        constant, terms = parse_seq_expr(str(entry))
    except ValueError:
        return None
    value = constant + variation_offset(terms or None, lfos, bpm, now,
                                        unit=1.0)
    return float(value) % 1.0


def order_has_lfo(order):
    """True when any entry of the order list is an expression."""
    return any(isinstance(item, str) for item in (order or []))


def text_to_order(text):
    """Parse an order list ('0.2 0.3 r lfo1') into [float|str|None, ...]."""
    out = []
    for tok in (text or "").split():
        low = tok.lower()
        if low == "r":
            out.append(None)
            continue
        try:
            constant, terms = parse_seq_expr(low)
        except ValueError:
            out.append(0.0)
            continue
        if not terms:
            out.append(float(constant) % 1.0)
        else:
            out.append(parse_seq_canonical(low))
    return out


def parse_seq_canonical(text):
    """Canonical text of a sequence-style expression (whitespace stripped)."""
    cleaned = re.sub(r"\s+", "", text)
    parse_seq_expr(cleaned)  # validate
    return cleaned


def save_project_file(path, bpm, out_port, tracks, lfos=None, clock=None):
    """Write the whole project state to an INI-style .cfg file."""
    import configparser

    cfg = configparser.ConfigParser(interpolation=None)
    cfg.optionxform = str
    cfg.add_section("global")
    cfg.set("global", "bpm", str(bpm))
    if out_port:
        cfg.set("global", "port", out_port)
    clock = clock or {}
    cfg.set("global", "clock-mode", clock.get("mode") or midiclock.MODE_OFF)
    cfg.set("global", "clock-start",
            clock.get("start_mode") or midiclock.START_INTERNAL)
    if clock.get("port"):
        cfg.set("global", "clock-port", clock["port"])
    for n in sorted(lfos or {}):
        entry = lfos[n]
        if not entry:
            continue
        section = f"lfo-{n}"
        cfg.add_section(section)
        cfg.set(section, "frequency", lfo.param_to_text(entry.get("frequency", 1.0)))
        cfg.set(section, "shape", entry.get("shape", "sin"))
        cfg.set(section, "phase", f"{float(entry.get('phase', 0.0)):g}")
        if entry.get("running"):
            cfg.set(section, "running", "true")
    for track in sorted(tracks):
        for pattern in sorted(tracks[track]):
            pdata = tracks[track][pattern]
            if not pdata:
                continue
            section = f"track-{track}:pattern-{pattern}"
            cfg.add_section(section)
            if pdata.get("scale") not in (None, "chromatic"):
                cfg.set(section, "scale", pdata["scale"])
            root = pdata.get("root")
            if root and root != "C4":
                cfg.set(section, "root", root)
            channel = pdata.get("channel")
            if channel not in (None, DEFAULT_CHANNEL):
                cfg.set(section, "channel", str(channel))
            if pdata.get("bpm-inherited") is False:
                cfg.set(section, "bpm", str(pdata.get("bpm", DEFAULT_BPM)))
            if pdata.get("port"):
                cfg.set(section, "port", pdata["port"])
            for n in sorted(pdata.get("seqs", {})):
                cfg.set(section, f"seq-{n}", seq_to_text(pdata["seqs"][n]))
            for n in sorted(pdata.get("phrases", {})):
                cfg.set(section, f"phrase-{n}", pdata["phrases"][n])
            for n in sorted(pdata.get("velocities", {})):
                cfg.set(section, f"velocity-{n}", pdata["velocities"][n])
            for n in sorted(pdata.get("sustains", {})):
                cfg.set(section, f"sustain-{n}", pdata["sustains"][n])
            for n in sorted(pdata.get("microtimes", {})):
                cfg.set(section, f"microtime-{n}", pdata["microtimes"][n])
            if pdata.get("osc-server"):
                cfg.set(section, "osc-server", pdata["osc-server"])
            if pdata.get("osc-sclang"):
                cfg.set(section, "osc-sclang", pdata["osc-sclang"])
            if pdata.get("osc-latency") is not None:
                cfg.set(section, "osc-latency", str(pdata["osc-latency"]))
            if pdata.get("synth"):
                cfg.set(section, "synth", pdata["synth"])
            for name in sorted(pdata.get("fx", {})):
                cfg.set(section, f"fx-{name}", pdata["fx"][name])
            if pdata.get("sample"):
                cfg.set(section, "sample", pdata["sample"])
            for n in sorted(pdata.get("orders", {})):
                cfg.set(section, f"order-{n}",
                        order_to_text(pdata["orders"][n]))
            if pdata.get("type") not in (None, "note"):
                cfg.set(section, "type", pdata["type"])
            if pdata.get("type") == "cc":
                cfg.set(section, "controller", str(pdata.get("controller", 1)))
            division = pdata.get("division")
            if division is not None and division != DEFAULT_DIVISIONS:
                cfg.set(section, "division", division_to_text(division))
    with open(path, "w", encoding="utf-8") as f:
        cfg.write(f)
    return sum(1 for section in cfg.sections()
               if _PATTERN_SECTION_RE.fullmatch(section))


_PATTERN_SECTION_RE = re.compile(r"track-(\d+):pattern-(\d+)")
_LFO_SECTION_RE = re.compile(r"lfo-(\d+)")


def load_project_file(path):
    """Read an INI-style .cfg and return (bpm, out_port, tracks, lfos).

    Raises OSError for file problems; ValueError / configparser.Error for
    malformed content.
    """
    import configparser

    cfg = configparser.ConfigParser(interpolation=None)
    cfg.optionxform = str
    with open(path, encoding="utf-8") as f:
        cfg.read_file(f)

    bpm = cfg.getint("global", "bpm", fallback=DEFAULT_BPM) if cfg.has_section("global") else DEFAULT_BPM
    port = None
    clock = {"mode": midiclock.MODE_OFF, "port": None,
             "start_mode": midiclock.START_INTERNAL}
    if cfg.has_section("global"):
        if cfg.has_option("global", "port"):
            port = cfg.get("global", "port").strip() or None
        if cfg.has_option("global", "clock-mode"):
            mode = cfg.get("global", "clock-mode").strip().lower()
            if mode in midiclock.MODES:
                clock["mode"] = mode
        if cfg.has_option("global", "clock-port"):
            clock["port"] = cfg.get("global", "clock-port").strip() or None
        if cfg.has_option("global", "clock-start"):
            start_mode = cfg.get("global", "clock-start").strip().lower()
            if start_mode in midiclock.START_MODES:
                clock["start_mode"] = start_mode

    tracks = {}
    lfos = {}
    for section in cfg.sections():
        if section == "global":
            continue
        m = _PATTERN_SECTION_RE.fullmatch(section)
        if m:
            track, pattern = int(m.group(1)), int(m.group(2))
            pdata = tracks.setdefault(track, {}).setdefault(pattern, {})
            if cfg.has_option(section, "scale"):
                pdata["scale"] = cfg.get(section, "scale").strip()
            if cfg.has_option(section, "root"):
                pdata["root"] = cfg.get(section, "root").strip()
            if cfg.has_option(section, "channel"):
                pdata["channel"] = cfg.getint(section, "channel")
            if cfg.has_option(section, "bpm"):
                pdata["bpm"] = cfg.getint(section, "bpm")
                pdata["bpm-inherited"] = False
            if cfg.has_option(section, "port"):
                pdata["port"] = cfg.get(section, "port").strip()
            for option in cfg.options(section):
                if option.startswith("seq-"):
                    n = int(option[4:])
                    pdata.setdefault("seqs", {})[n] = text_to_seq(cfg.get(section, option))
                elif option.startswith("phrase-"):
                    n = int(option[7:])
                    pdata.setdefault("phrases", {})[n] = cfg.get(section, option).strip()
                elif option.startswith("velocity-"):
                    n = int(option[9:])
                    pdata.setdefault("velocities", {})[n] = \
                        velocity.canonical(cfg.get(section, option))
                elif option.startswith("sustain-"):
                    n = int(option[8:])
                    pdata.setdefault("sustains", {})[n] = \
                        velocity.canonical(cfg.get(section, option))
                elif option.startswith("microtime-"):
                    n = int(option[10:])
                    pdata.setdefault("microtimes", {})[n] = \
                        velocity.canonical(cfg.get(section, option))
                elif option == "type":
                    pdata["type"] = cfg.get(section, option).strip().lower()
                elif option == "controller":
                    pdata["controller"] = cfg.getint(section, option)
                elif option == "division":
                    pdata["division"] = parse_division(cfg.get(section, option))
                elif option == "sample":
                    pdata["sample"] = cfg.get(section, option).strip()
                elif option == "osc-server":
                    pdata["osc-server"] = cfg.get(section, option).strip()
                elif option == "osc-sclang":
                    pdata["osc-sclang"] = cfg.get(section, option).strip()
                elif option == "osc-latency":
                    pdata["osc-latency"] = cfg.getfloat(section, option)
                elif option == "synth":
                    pdata["synth"] = cfg.get(section, option).strip()
                elif option.startswith("fx-"):
                    pdata.setdefault("fx", {})[option[3:]] = \
                        cfg.get(section, option).strip()
                elif option.startswith("order-"):
                    n = int(option[6:])
                    pdata.setdefault("orders", {})[n] = \
                        text_to_order(cfg.get(section, option))
            continue
        m = _LFO_SECTION_RE.fullmatch(section)
        if m:
            n = int(m.group(1))
            entry = lfos.setdefault(n, {})
            entry["frequency"] = _load_lfo_param(
                cfg.get(section, "frequency", fallback="1"))
            entry["shape"] = cfg.get(section, "shape", fallback="sin").strip()
            phase = cfg.getfloat(section, "phase", fallback=0.0)
            # Migration: amplitude -1 was a half-cycle shift; fold it into phase.
            if cfg.has_option(section, "amp"):
                legacy = cfg.get(section, "amp").strip()
                try:
                    if float(legacy) == -1.0:
                        phase += 0.5
                        if phase > 1.0:
                            phase -= 1.0
                except ValueError:
                    pass  # old modulation expressions no longer apply
            entry["phase"] = phase
            entry["running"] = cfg.getboolean(section, "running", fallback=False)
            continue
        # ignore any other foreign section
    return bpm, port, tracks, lfos, clock


def _load_lfo_param(raw):
    """Decode a stored LFO parameter value (number or modulation expression)."""
    kind = lfo.parse_param(raw)
    if kind[0] == "lfo":
        coef, ref = kind[1], kind[2]
        return f"{coef:g}*lfo{ref}"
    if kind[0] == "bpm":
        return f"{lfo.factor_to_text(kind[1])}bpm"
    return kind[1]


def _script_path(name):
    """Resolve helper scripts relative to this file (fallback: plain name)."""
    path = os.path.join(BASE_DIR, name)
    return path if os.path.isfile(path) else name


def _setup_output_encoding():
    """Best effort: make the console/pipe able to print the block glyphs."""
    try:
        if os.name == "nt":
            import ctypes
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def run_script(script_name, args):
    """Run a helper script with the same interpreter, streaming its output."""
    try:
        proc = subprocess.run([sys.executable, "-u", _script_path(script_name)] + list(args))
    except OSError as e:
        print(f"Error executing {script_name}: {e}")
        return 1
    if proc.returncode != 0:
        print(f"(exit code: {proc.returncode})")
    return proc.returncode


def initialize_readline():
    """Bind Tab to completion; keep '-' inside a single completion word."""
    if not HAVE_READLINE:
        return
    try:
        if "libedit" in (readline.__doc__ or ""):
            readline.parse_and_bind("bind ^I rl_complete")
        else:
            readline.parse_and_bind("tab: complete")
        delims = readline.get_completer_delims()
        readline.set_completer_delims(delims.replace("-", ""))
    except Exception:
        pass


class SeqCompletingCmd(cmd.Cmd):
    """cmd.Cmd base that provides Tab completion even without readline.

    On Windows consoles without a readline module the standard cmdloop is
    replaced by a key-by-key editor (msvcrt) implementing history, cursor
    movement and Tab completion against the same completenames /
    completedefault hooks that readline would use.
    """

    def cmdloop(self, intro=None):
        if os.name == "nt" and not HAVE_READLINE and sys.stdin.isatty():
            self._windows_cmdloop(intro)
            return
        super().cmdloop(intro)

    # ------------------------------------------------------------------
    # Command completion hooks
    # ------------------------------------------------------------------
    def completenames(self, text, *ignored):
        dotags = [a[3:] for a in self.get_names() if a.startswith("do_")]
        names = [t.replace("_", "-") for t in dotags if t not in ("EOF",)]
        return [a for a in names if a.startswith(text)]

    def complete(self, text, state):
        """readline entry point: same rules as the built-in editor."""
        if state == 0:
            line, cursor = text, len(text)
            if HAVE_READLINE:
                try:
                    line = readline.get_line_buffer()
                    cursor = readline.get_endidx()
                except Exception:
                    pass
            _, matches = self.line_candidates(line, cursor)
            self._completion_matches = list(matches)
        try:
            return self._completion_matches[state]
        except (AttributeError, IndexError):
            return None

    def completedefault(self, text, line, begidx, endidx):
        """Argument completion, aware of '/' prefixes and full paths."""
        head = line[:begidx].split()
        root_mode = False
        if head and head[0].startswith("/"):
            root_mode = True
            head = ([head[0][1:]] if len(head[0]) > 1 else []) + head[1:]
        active = self._root_shell() if root_mode else self
        if head:
            func = getattr(active, "complete_" + head[0].replace("-", "_"), None)
            if func:
                try:
                    matches = func(text, line, begidx, endidx)
                except Exception:
                    matches = []
                if matches:
                    return matches
        return active.argument_candidates(head, text)

    def argument_candidates(self, head, word):
        """Generic argument candidates for 'word' after the tokens in 'head'."""
        first = head[0] if head else ""
        low = word.lower()
        # after an LFO reference: parameters, or another LFO number for cp/rm
        if re.fullmatch(r"(?i)lfo\s*\d+", first):
            params = ["frequency", "shape", "phase", "start", "stop", "live",
                      "show", "help", "exit"]
            return [p for p in params if p.startswith(low)]
        if first.lower() in ("cp", "rm", "lfo") or low.startswith("lfo"):
            numbers = [f"lfo{i}" for i in range(1, MAX_LFOS + 1)]
            if low.startswith("lfo"):
                return [n for n in numbers if n.startswith(low)]
            if first.lower() == "lfo":
                return numbers
        if first.lower() in ("cp", "rm") and low.startswith("t"):
            return [f"t{i}" for i in range(1, MAX_TRACKS + 1)
                    if f"t{i}".startswith(low)]
        if first.lower() == "hist":
            options = ["live", "once", "stop", "static"]
            options += [f"f{i}" for i in range(MAX_PHRASES)]
            options += [f"s{i}" for i in range(10)]
            return [o for o in options if o.startswith(low)]
        # <path> <setting>  e.g. 't1p2 chan' or '/ t1p2 chan'
        if re.match(r"(?i)^(?:t\d+|p\d+)", first):
            return self.path_word_candidates(low)
        if first.lower() in ("start", "stop"):
            # 'start t1p1f' -> t1p1f0..f7 ; 'start f' -> f0..f7 ; 'stop t2' -> t2
            m = re.fullmatch(r"(?i)((?:t\d+)?(?:p\d+)?)([fso])(\d*)", low)
            if m:
                prefix, kind = m.group(1), m.group(2).lower()
                count = (MAX_PHRASES if kind == "f" else
                         sampler.MAX_ORDER_LISTS if kind == "o" else 10)
                return [f"{prefix}{kind}{i}" for i in range(count)
                        if f"{prefix}{kind}{i}".startswith(low)]
            names = ([f"f{i}" for i in range(MAX_PHRASES)]
                     + [f"t{i}" for i in range(1, MAX_TRACKS + 1)]
                     + [f"p{i}" for i in range(1, MAX_PATTERNS + 1)])
            return [n for n in names if n.startswith(low)]
        return []

    def path_word_candidates(self, low):
        """Words that may follow a path: settings, views, leaves, commands."""
        words = ["scale", "root", "bpm", "channel", "division", "type",
                 "controller", "port", "select-port", "hist", "show", "start",
                 "stop", "cp", "rm", "help"]
        words += [f"s{i}" for i in range(10)]
        words += [f"f{i}" for i in range(MAX_PHRASES)]
        words += [f"v{i}" for i in range(10)]
        words += [f"sus{i}" for i in range(10)]
        words += [f"mt{i}" for i in range(10)]
        return [w for w in words if w.startswith(low)]

    def line_candidates(self, line, cursor):
        """Candidates for the word ending at 'cursor' -> (start, matches).

        Understands a leading '/' (run at the root, stay in this menu) and
        path-prefixed commands, e.g. '/ t1p2 chan' -> ['channel'].
        """
        before = line[:cursor]
        start = before.rfind(" ") + 1
        word = before[start:]
        head = before[:start].split()
        root_mode = False
        prefix = ""
        if head and head[0].startswith("/"):
            root_mode = True
            head = ([head[0][1:]] if len(head[0]) > 1 else []) + head[1:]
        elif word.startswith("/"):
            root_mode = True
            prefix, word = "/", word[1:]
        active = self._root_shell() if root_mode else self
        if not head:  # completing the command word itself
            return start, [prefix + m for m in active.completenames(word)]
        func = getattr(active, "complete_" + head[0].replace("-", "_"), None)
        if func:
            try:
                matches = func(word, line, start, cursor)
            except Exception:
                matches = []
            if matches:
                return start, matches
        return start, active.argument_candidates(head, word)

    # ------------------------------------------------------------------
    # Command dispatch
    # ------------------------------------------------------------------
    def onecmd(self, line):
        return self._execute_cmd(line)

    def _execute_cmd(self, line):
        """Dispatch a raw line: hyphenated name -> do_<name> method.

        An '=' assignment in the command word is turned into an argument
        ('v1=64+0.4*lfo1' -> 'v1 64+0.4*lfo1'), and only the command word is
        hyphen-normalized so values keep their signs ('v3=-5').
        """
        try:
            parts = shlex.split(line)
        except ValueError as e:
            print(f"Error parsing command: {e}")
            return None
        if not parts:
            return self.emptyline()
        command = parts[0]
        extra = []
        if "=" in command:
            command, _, value = command.partition("=")
            if value:
                extra.append(value)
        command = command.replace("-", "_")
        command = _CMD_ALIASES.get(command, command)
        raw_args = " ".join(extra + parts[1:])
        return super().onecmd((command + " " + raw_args).strip())

    def run_root_command(self, text):
        """Execute a command at the root level without leaving this menu."""
        self._root_shell().onecmd(text)

    def _root_shell(self):
        """Walk the parent_shell chain up to the top-most shell."""
        node = self
        parent = getattr(node, "parent_shell", None)
        while parent is not None:
            node = parent
            parent = getattr(node, "parent_shell", None)
        return node

    # ------------------------------------------------------------------
    # '/' : return to the root menu from anywhere
    # ------------------------------------------------------------------
    def request_root(self):
        """Ask every menu level to unwind back to the root shell."""
        self._root_shell().return_to_root = True

    def root_requested(self):
        return getattr(self._root_shell(), "return_to_root", False)

    def propagate_root_request(self):
        """After a nested menu returns, True means this level must also stop."""
        return self.root_requested()

    def postcmd(self, stop, line):
        # The root shell clears the unwind flag once control is back with it.
        if not getattr(self, "parent_shell", None):
            self.return_to_root = False
        return stop

    # ------------------------------------------------------------------
    # Windows console fallback (no readline)
    # ------------------------------------------------------------------
    def _windows_cmdloop(self, intro=None):
        if intro is not None:
            self.intro = intro
        if self.intro:
            print(self.intro, end="" if self.intro.endswith("\n") else "\n")
        self.preloop()
        stop = None
        while not stop:
            try:
                line = self._windows_readline()
                line = self.precmd(line)
                stop = self.onecmd(line)
                stop = self.postcmd(stop, line)
            except KeyboardInterrupt:
                stop = None  # a live panel re-anchors itself below the prompt
                paint = getattr(self, "_live_paint", None)
                if paint is not None:
                    paint([])
                print("^C")
            except EOFError:
                paint = getattr(self, "_live_paint", None)
                if paint is not None:
                    paint([])
                print()
                stop = self.onecmd("EOF")
        self.postloop()

    def _windows_readline(self):
        import msvcrt

        buffer = []
        cursor = 0
        history = getattr(self, "_windows_history", [])
        history_index = len(history)
        last_rendered_len = 0
        last_prefix = ""
        last_text = None
        last_block = ()
        panel_rows = 0
        ansi = _enable_ansi()  # cursor movement keeps redraws flicker-free

        def paint_panel(lines):
            """Draw the live panel above the input line.

            The panel occupies the rows directly above the prompt. Repaints move
            up to the panel's first row, clear downward and rewrite it, then
            step onto the prompt row with cursor-down (ESC[B) instead of a
            newline -- a newline on the last screen row is what used to scroll
            the panel's first line away.
            """
            nonlocal panel_rows
            if not lines and not panel_rows:
                return
            out = []
            if panel_rows:
                out.append("\x1b[%dA\x1b[J" % panel_rows)
            if lines:
                out.append("\n".join(lines))
                out.append("\x1b[1B")   # onto the prompt row, no scroll
            sys.stdout.write("".join(out))
            sys.stdout.flush()
            panel_rows = len(lines)

        def redraw():
            """Repaint the live panel (if it changed) and then the input line.

            When only the live prefix changed, just that field is overwritten and
            the cursor jumped back (no clearing, no prompt rewrite).
            """
            nonlocal last_rendered_len, last_prefix, last_text, last_block
            block = tuple(getattr(self, "_live_block", ()) or ())
            if block != last_block:
                paint_panel(block)
                last_block = block
            text = "".join(buffer)
            head = getattr(self, "_live_prefix", "") or ""
            body = head + self.prompt + text
            target = len(head) + len(self.prompt) + cursor
            if (ansi and text == last_text and head != last_prefix
                    and len(head) == len(last_prefix)
                    and last_rendered_len == len(last_prefix) + len(self.prompt) + len(text)):
                # Same layout: only the meter digits changed.
                move = f"\x1b[{target}C" if target else ""
                print("\r" + head + move, end="", flush=True)
                last_prefix = head
                return
            pad = max(0, last_rendered_len - len(body))
            back = (len(body) + pad) - target
            out = "\r" + body + (" " * pad) + ("\b" * back if back > 0 else "")
            print(out, end="", flush=True)
            last_rendered_len = len(body) + pad
            last_prefix = head
            last_text = text

        # A live block (e.g. the pattern histogram) is re-anchored at every new
        # prompt, so it keeps refreshing even after other commands print output.
        initial_block = tuple(getattr(self, "_live_block", ()) or ())
        if initial_block:
            paint_panel(initial_block)
            last_block = initial_block
        self._live_paint = paint_panel  # cleanup hooks can erase the area
        print(self.prompt, end="", flush=True)
        last_rendered_len = len(self.prompt)
        while True:
            tick = getattr(self, "_idle_tick", None)
            if (tick is not None and getattr(self, "_live_active", False)
                    and not msvcrt.kbhit()):
                # Live status is on: refresh the prefix while the user types.
                if tick():
                    redraw()
                time.sleep(0.02)
                continue
            char = msvcrt.getwch()
            if char in ("\r", "\n"):
                if panel_rows:
                    paint_panel([])  # leave no stale copy behind
                print()
                line = "".join(buffer)
                if line:
                    history.append(line)
                    self._windows_history = history[-100:]
                return line
            if char == "\x03":
                raise KeyboardInterrupt
            if char in ("\x04", "\x1a"):
                raise EOFError
            if char == "\t":
                cursor = self._complete_windows_buffer(buffer, cursor)
                redraw()
                continue
            if char == "\b":
                if cursor > 0:
                    del buffer[cursor - 1]
                    cursor -= 1
                    redraw()
                continue
            if char in ("\x00", "\xe0"):  # extended keys
                key = msvcrt.getwch()
                if key == "K" and cursor > 0:
                    cursor -= 1
                    redraw()
                elif key == "M" and cursor < len(buffer):
                    cursor += 1
                    redraw()
                elif key == "H" and history:  # up: older
                    history_index = max(0, history_index - 1)
                    buffer[:] = list(history[history_index])
                    cursor = len(buffer)
                    redraw()
                elif key == "P":  # down: newer
                    history_index = min(len(history), history_index + 1)
                    buffer[:] = list(history[history_index]) if history_index < len(history) else []
                    cursor = len(buffer)
                    redraw()
                elif key == "S" and cursor < len(buffer):  # Delete
                    del buffer[cursor]
                    redraw()
                continue
            if char >= " ":
                buffer.insert(cursor, char)
                cursor += 1
                redraw()

    def _complete_windows_buffer(self, buffer, cursor):
        line = "".join(buffer)
        start, matches = self.line_candidates(line, cursor)
        if not matches:
            return cursor
        word = line[start:cursor]
        if len(matches) == 1:
            replacement = matches[0]
            first_word = start == 0 or (start == 1 and line[:1] == "/")
            if first_word:
                replacement += " "
            buffer[start:cursor] = list(replacement)
            return start + len(replacement)
        common = os.path.commonprefix(matches)
        if common and common != word:
            buffer[start:cursor] = list(common)
            return start + len(common)
        print("\n  " + "  ".join(matches))
        return cursor


class SeqShell(SeqCompletingCmd):
    """Main interactive shell."""

    intro = "Welcome to the seq MIDI sequencer. Type help or ? to list commands.\n"
    menu_path = ["seq"]

    def __init__(self):
        super().__init__()
        self.prompt = "seq> "
        self.bpm = DEFAULT_BPM
        self.out_port = None  # name of the selected MIDI output port
        self.project = {"tracks": {}, "lfos": {}}  # project state + global LFOs
        self.beat_epoch = time.monotonic()  # shared beat grid for LFO start
        self.playback = {}  # (track, pattern, phrase) -> player.Player
        self._init_clock()  # MIDI clock send/receive facade
        initialize_readline()

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------
    def do_list_ports(self, arg):
        "List available MIDI input and output ports"
        run_script("list-ports.py", shlex.split(arg) if arg else [])

    def do_select_port(self, arg):
        "Choose the MIDI output port the sequencer will send notes to"
        port = choose_midi_output_port()
        if port is None:
            return
        self.out_port = port
        print(f"Selected MIDI output port: {self.out_port}")

    def do_lfo(self, arg):
        "Enter an LFO menu (lfo <n>) or start/stop one (lfo <n> start|stop)"
        text = (arg or "").strip()
        m = re.fullmatch(r"(\d+)\s+(start|stop)", text)
        if m:
            self._lfo_set_running(int(m.group(1)), m.group(2).lower() == "start")
            return
        n = parse_index(text, 1, MAX_LFOS, "lfo")
        if n is not None:
            LfoShell(self, n).cmdloop()

    def next_beat_time(self, bpm, now=None):
        """Absolute time of the next BPM beat on the shared beat grid.

        All LFOs share this grid, so ones started within the same beat begin
        together at that beat. Starting exactly on a beat starts immediately.
        """
        beat = 60.0 / max(float(bpm), 1e-9)
        now = time.monotonic() if now is None else now
        elapsed = now - self.beat_epoch
        k = math.floor(elapsed / beat + 1e-9)
        if k * beat < elapsed - 1e-9:
            k += 1
        return self.beat_epoch + k * beat

    def _lfo_set_running(self, n, running):
        """Start/stop LFO n (creating its store entry on demand).

        Starting latches the run to the next main-BPM beat and restarts from the
        configured phase (a restart behaves like the first start).
        """
        if not 1 <= n <= MAX_LFOS:
            print(f"Error: lfo number must be between 1 and {MAX_LFOS}.")
            return
        lfos = self.project.setdefault("lfos", {})
        entry = lfos.get(n, {})
        if bool(entry.get("running", False)) == running:
            state = "running" if running else "stopped"
            print(f"LFO {n} is already {state}.")
            return
        entry = lfos.setdefault(n, {})
        entry["running"] = running
        if running:
            entry[LFO_START_KEY] = self.next_beat_time(self.bpm)
        else:
            # Forget the run so a later start begins fresh (not from this value).
            entry.pop(LFO_START_KEY, None)
        print(f"LFO {n} {'started' if running else 'stopped'}.")

    def _lfo_start_stop(self, text):
        """Handle 'lfo<n> start|stop' and 'lfo <n> start|stop'."""
        m = re.fullmatch(r"(?i)lfo\s*(\d+)\s+(start|stop)", text.strip())
        if not m:
            print("Usage: lfo<n> start|stop   (e.g. lfo3 start)  "
                  "or  lfo <n> start|stop")
            return
        self._lfo_set_running(int(m.group(1)), m.group(2).lower() == "start")

    def do_status_lfo(self, arg):
        "Show the parameters of all started LFOs"
        lfos = self.project.get("lfos", {})
        started = sorted(n for n, e in lfos.items() if e.get("running"))
        if not started:
            print("No LFO is currently running.")
            return
        for n in started:
            entry = lfos[n]
            print(f"LFO {n}: running, frequency "
                  f"{lfo.describe_frequency(entry.get('frequency', 1.0), self.bpm)}, "
                  f"shape {entry.get('shape', 'sin')}, "
                  f"phase {float(entry.get('phase', 0.0)):g}")

    # ------------------------------------------------------------------
    # MIDI clock (24 PPQN) send / receive
    # ------------------------------------------------------------------
    def _init_clock(self):
        """Create the MIDI clock facade (called from __init__)."""
        self.clock_mode = midiclock.MODE_OFF
        self.clock_port = None
        self.clock_start = midiclock.START_INTERNAL
        self.midi_clock = midiclock.MidiClock(
            bpm_getter=lambda: self.bpm,
            playback_getter=lambda: bool(self.playback),
            on_external_bpm=self._bpm_from_clock,
            on_external_start=self.start_all_patterns,
            on_external_stop=self._stop_all_playback)

    def _bpm_from_clock(self, bpm):
        """Follow a received MIDI clock: it sets the global BPM."""
        self.bpm = max(BPM_MIN, min(BPM_MAX, int(round(bpm))))

    def _apply_clock(self, announce=True):
        """(Re)configure the clock from the stored settings."""
        ok, message = self.midi_clock.configure(mode=self.clock_mode,
                                               port=self.clock_port,
                                               start_mode=self.clock_start)
        if announce:
            print(message if ok else f"Error: {message}")
        return ok

    def do_start_all(self, arg):
        "Start every configured pattern (same as a received MIDI Start)"
        self.start_all_patterns()

    def start_all_patterns(self):
        """Start every configured pattern (used by a received MIDI Start)."""
        started, skipped = [], []
        tracks = self.project.get("tracks", {})
        for track in sorted(tracks):
            for pattern in sorted(tracks[track]):
                view = self._pattern_view(track, pattern)
                kind = view["type"]
                if kind == "loop":
                    if view["phrases"]:
                        self.launch_loop(track, pattern,
                                         phrase=min(view["phrases"]))
                    elif view["orders"]:
                        self.launch_loop(track, pattern,
                                         order_index=min(view["orders"]))
                    else:
                        skipped.append(f"t{track}p{pattern}")
                        continue
                elif view["phrases"]:
                    self.launch_phrase(track, pattern, min(view["phrases"]))
                else:
                    skipped.append(f"t{track}p{pattern}")
                    continue
                started.append(f"t{track}p{pattern}")
        if started:
            print(f"Clock start: started {len(started)} pattern(s): "
                  f"{', '.join(started)}")
        else:
            print("Clock start: nothing to start (no pattern has a phrase).")
        if skipped:
            print(f"  (no phrase configured in: {', '.join(skipped)})")
        return len(started)

    def do_clock(self, arg):
        """MIDI clock: clock [send|receive|off|port <name>|start internal|received]"""
        parts = (arg or "").split()
        head = parts[0].lower() if parts else ""
        rest = " ".join(parts[1:]).strip()

        if not head:
            for line in self.midi_clock.status_lines():
                print(line)
            inputs = midiclock.input_port_names()
            outputs = midiclock.output_port_names()
            print(f"  input ports:  {', '.join(inputs) if inputs else '(none)'}")
            print(f"  output ports: {', '.join(outputs) if outputs else '(none)'}")
            print("  Usage: clock send|receive|off, clock port <name|index>, "
                  "clock start internal|received")
            return

        if head in ("off", "none", "stop"):
            self.clock_mode = midiclock.MODE_OFF
            self.midi_clock.mode = midiclock.MODE_OFF
            self.midi_clock.stop()
            print("MIDI clock off.")
            return
        if head in ("send", "out", "master"):
            self.clock_mode = midiclock.MODE_SEND
            names = midiclock.output_port_names()
            if self.clock_port and self.clock_port not in names:
                print(f"Error: '{self.clock_port}' is not an available MIDI "
                      f"OUTPUT port.")
                print(f"  Choose one: clock port out <name|index>   "
                      f"({', '.join(names) if names else 'none found'})")
                return
            self._apply_clock()
            return
        if head in ("receive", "in", "slave", "follow"):
            self.clock_mode = midiclock.MODE_RECEIVE
            names = midiclock.input_port_names()
            if self.clock_port and self.clock_port not in names:
                print(f"Error: '{self.clock_port}' is not an available MIDI "
                      f"INPUT port.")
                print(f"  Choose one: clock port in <name|index>   "
                      f"({', '.join(names) if names else 'none found'})")
                return
            self._apply_clock()
            return
        if head in ("start", "startmode", "start-mode"):
            value = rest.lower()
            if value not in midiclock.START_MODES:
                print(f"Usage: clock start internal|received   "
                      f"(currently {self.clock_start})")
                print("  internal: our 'start'/'stop' commands control playback")
                print("  received: a MIDI Start from the clock source starts "
                      "every configured pattern")
                return
            self.clock_start = value
            self.midi_clock.start_mode = value
            print(f"clock start: {value}"
                  + ("  (MIDI Start starts all configured patterns)"
                     if value == midiclock.START_RECEIVED else ""))
            return
        if head in ("port", "device"):
            if not rest:
                print(f"clock port: {self.clock_port or '(none)'}")
                print(f"  Usage: clock port [in|out] <name|index>   "
                      f"(with no direction: "
                      f"{'in' if self.clock_mode == midiclock.MODE_RECEIVE else 'out'})")
                outputs = midiclock.output_port_names()
                inputs = midiclock.input_port_names()
                print(f"  output ports (clock send): "
                      f"{', '.join(f'{i}: {n}' for i, n in enumerate(outputs)) or '(none)'}")
                print(f"  input ports  (clock receive): "
                      f"{', '.join(f'{i}: {n}' for i, n in enumerate(inputs)) or '(none)'}")
                return
            text = rest
            direction = None
            for word in ("in", "out"):
                if text.lower().startswith(word + " "):
                    direction = word
                    text = text[len(word):].strip()
                    break
            if direction is None:
                direction = ("in" if self.clock_mode == midiclock.MODE_RECEIVE
                             else "out")
            names = (midiclock.input_port_names() if direction == "in"
                     else midiclock.output_port_names())
            kind = ("MIDI input" if direction == "in" else "MIDI output")
            if text.isdigit():
                index = int(text)
                if not names:
                    print(f"Error: no {kind} ports are enumerated right now "
                          f"(MIDI devices can come and go); nothing changed.")
                    return
                if index >= len(names):
                    print(f"Error: there is no {kind} port {index} - choose "
                          f"0..{len(names) - 1}:")
                    for i, name in enumerate(names):
                        print(f"    {i}: {name}")
                    return
                self.clock_port = names[index]
            else:
                self.clock_port = text
                if names and text not in names:
                    print(f"Note: '{text}' is not in the current {kind} list "
                          f"({', '.join(names)}).")
            print(f"clock port: {self.clock_port} ({kind})")
            if self.clock_mode == midiclock.MODE_OFF:
                print("  (clock is off; use 'clock send' or 'clock receive')")
                return
            self._apply_clock()
            return
        print(f"Error: unknown clock option '{head}'. Use send, receive, off, "
              f"port <name> or start internal|received.")

    def do_bpm(self, arg):
        "Show or set the global BPM (default: 120, 1-300). Patterns inherit it"
        if not (arg or "").strip():
            print(f"Global BPM: {self.bpm} (patterns inherit this value by default)")
            return
        value = parse_bpm(arg)
        if value is None:
            return
        self.bpm = value
        print(f"Global BPM set to: {value}")

    def do_save(self, arg):
        "Save the whole configuration to a .cfg file (default: seq.cfg)"
        filename = (arg or "").strip() or "seq.cfg"
        try:
            written = save_project_file(
                filename, self.bpm, self.out_port, self.project["tracks"],
                self.project.get("lfos"),
                clock={"mode": self.clock_mode, "port": self.clock_port,
                       "start_mode": self.clock_start})
        except OSError as e:
            print(f"Error writing {filename}: {e}")
            return
        print(f"Configuration saved to {filename} "
              f"({written} pattern section{'s' if written != 1 else ''}).")

    def do_load(self, arg):
        "Load the whole configuration from a .cfg file (default: seq.cfg)"
        filename = (arg or "").strip() or "seq.cfg"
        try:
            bpm, port, tracks, lfos, clock = load_project_file(filename)
        except OSError as e:
            print(f"Error reading {filename}: {e}")
            return
        except Exception as e:
            print(f"Error loading {filename}: {e}")
            return
        self.bpm = bpm
        self.out_port = port
        self.project = {"tracks": tracks, "lfos": lfos}
        if isinstance(clock, dict):
            self.clock_mode = clock.get("mode", midiclock.MODE_OFF)
            self.clock_port = clock.get("port")
            self.clock_start = clock.get("start_mode",
                                         midiclock.START_INTERNAL)
        for entry in lfos.values():
            if entry.get("running"):
                entry[LFO_START_KEY] = self.next_beat_time(bpm)
        count = sum(len(patterns) for patterns in tracks.values())
        print(f"Configuration loaded from {filename} "
              f"({count} pattern section{'s' if count != 1 else ''}).")

    # ------------------------------------------------------------------
    # Phrase playback (usable from seq / track / pattern menus)
    # ------------------------------------------------------------------
    def _pattern_view(self, track, pattern):
        """Effective playback values of a stored pattern (defaults merged)."""
        entry = self.project.get("tracks", {}).get(track, {}).get(pattern, {}) or {}
        root = scales.parse_note(entry.get("root", "C4")) or scales.parse_note("C4")
        if entry.get("bpm-inherited") is False:
            bpm = entry.get("bpm", self.bpm)
        else:
            bpm = self.bpm
        return {
            "scale": entry.get("scale", "chromatic"),
            "root": root,
            "channel": entry.get("channel", DEFAULT_CHANNEL),
            "bpm": bpm,
            "port": entry.get("port") or self.out_port,
            "microtimes": entry.get("microtimes", {}),
            "type": str(entry.get("type", "note")).lower(),
            "controller": int(entry.get("controller", 1)),
            "division": entry.get("division", DEFAULT_DIVISIONS),
            "seqs": entry.get("seqs", {}),
            "phrases": entry.get("phrases", {}),
            "velocities": entry.get("velocities", {}),
            "sustains": entry.get("sustains", {}),
            "osc_server": entry.get("osc-server"),
            "osc_sclang": entry.get("osc-sclang"),
            "osc_latency": entry.get("osc-latency"),
            "synth": entry.get("synth"),
            "fx": entry.get("fx", {}),
            "sample": entry.get("sample"),
            "orders": entry.get("orders", {}),
        }

    def launch_phrase(self, track, pattern, phrase):
        """Start playback of phrase <phrase> of track/pattern. Returns Player or None.

        The pass data is re-read from the project store before every cycle, so
        edits to sequences/phrases (and scale/root/BPM/channel) are picked up
        at the start of the next division cycle while playing.
        """
        view0 = self._pattern_view(track, pattern)
        is_osc = view0["type"] == "osc"
        if not is_osc and not view0["port"]:
            print("No MIDI output port selected. Use 'select-port' (seq or pattern level).")
            return None
        self.stop_pattern(track, pattern, announce=False)

        def build():
            """Snapshot of the current pass: (plan, infinite, step_time, resolver, channel)."""
            view = self._pattern_view(track, pattern)
            expr = view["phrases"].get(phrase)
            if expr is None:
                raise LookupError(
                    f"phrase {phrase} of pattern {pattern} is no longer configured")
            plan, infinite = player.flatten_phrase(
                expr, lambda kind, idx: view["seqs"].get(idx)
                if kind == "seq" else None)
            if not plan:
                raise ValueError("the phrase yields no steps to play")
            step_time = player.step_time_for(view["bpm"], view["division"])
            intervals = scales.SCALES[view["scale"]]
            root_midi = view["root"].midi

            def resolver(entry, now):
                if entry is None:
                    return None
                degree, shift, variation = entry
                if view["type"] == "cc":
                    # CC pattern: the sequence holds raw values 0..127.
                    offset = variation_offset(variation,
                                              self.project.get("lfos", {}),
                                              view["bpm"], now)
                    return max(0, min(127, degree + offset + 12 * shift))
                # Static or LFO-driven: the degree indexes the *current*
                # scale, wrapping across octaves, so scale changes re-map it.
                index = degree + variation_offset(
                    variation, self.project.get("lfos", {}), view["bpm"], now)
                size = len(intervals)
                octave, pos = divmod(index, size)
                return fold_midi(root_midi + 12 * (octave + shift)
                                 + intervals[pos])

            osc = None
            if view["type"] == "osc":
                osc = {"synth": view["synth"], "fx": dict(view["fx"]),
                       "lfos": self.project.get("lfos", {}),
                       "bpm": view["bpm"], "latency": view["osc_latency"]}
            return (plan, infinite, step_time, resolver, view["channel"],
                    view["type"], view["controller"], osc)

        def velocity_for(seq_index, now):
            """Per-note velocity from the sequence's velocity expression."""
            view = self._pattern_view(track, pattern)
            expr = view["velocities"].get(seq_index)
            if not expr:
                return player.VELOCITY
            try:
                return velocity.evaluate(expr, self.project.get("lfos", {}),
                                         view["bpm"], now)
            except ValueError:
                return player.VELOCITY

        def microtime_for(seq_index, now):
            """Per-note microtiming in steps, signed (+/-MICROTIME_MAX_STEPS)."""
            view = self._pattern_view(track, pattern)
            expr = view["microtimes"].get(seq_index)
            if not expr:
                return 0.0
            try:
                return velocity.evaluate_span(
                    expr, self.project.get("lfos", {}), view["bpm"], now,
                    -player.MICROTIME_MAX_STEPS,
                    player.MICROTIME_MAX_STEPS)
            except ValueError:
                return 0.0

        def sustain_for(seq_index, now):
            """Per-note note length in steps (0..SUSTAIN_MAX_STEPS)."""
            view = self._pattern_view(track, pattern)
            expr = view["sustains"].get(seq_index)
            if not expr:
                return player.SUSTAIN_FRACTION
            try:
                return velocity.evaluate_span(
                    expr, self.project.get("lfos", {}), view["bpm"], now,
                    0.0, player.SUSTAIN_MAX_STEPS)
            except ValueError:
                return player.SUSTAIN_FRACTION

        try:
            (plan, infinite, step_time, resolver, channel, mode, controller,
             osc) = build()
        except (phrases.PhraseError, LookupError, ValueError) as e:
            print(f"Error: {e}")
            return None

        emitter = None
        engine = None
        if is_osc:
            try:
                server = (supercollider.parse_target(
                    view0["osc_server"], supercollider.DEFAULT_SERVER_PORT)
                    if view0["osc_server"] else supercollider.server_target())
                sclang = (supercollider.parse_target(
                    view0["osc_sclang"], supercollider.DEFAULT_SCLANG_PORT)
                    if view0["osc_sclang"] else supercollider.sclang_target())
            except ValueError as e:
                print(f"Error: {e}")
                return None
            engine = supercollider.engine_for(server, sclang)
            emitter = supercollider.OscEmitter(
                engine, synth=(osc or {}).get("synth"),
                fx=(osc or {}).get("fx"), lfos=(osc or {}).get("lfos"),
                bpm=(osc or {}).get("bpm", view0["bpm"]),
                latency=(osc or {}).get("latency"))

        try:
            pl = player.Player(view0["port"], channel, step_time, plan, infinite,
                               resolver, provider=build,
                               velocity_resolver=velocity_for,
                               sustain_resolver=sustain_for,
                               microtime_resolver=microtime_for,
                               mode=mode, controller=controller,
                               emitter=emitter, owns_port=not is_osc)
        except (ValueError, OSError) as e:
            print(f"Error starting playback: {e}")
            return None
        self.playback[(track, pattern, phrase)] = pl
        kind = "infinite loop" if infinite else f"one pass ({len(plan)} steps)"
        if is_osc:
            target = (f"{engine.target_text} synth '{emitter.synth}'"
                      if engine else "SuperCollider")
            fx_text = (" fx " + ", ".join(f"{k}={v}" for k, v
                                          in sorted((osc or {}).get("fx", {}).items()))
                       if (osc or {}).get("fx") else "")
            print(f"Playing phrase {phrase} of pattern {pattern}, track {track} "
                  f"({view0['phrases'][phrase]}) -> OSC {target}{fx_text}, "
                  f"{view0['bpm']} BPM ({kind}, step {step_time * 1000:.1f} ms). "
                  f"Type 'stop' to end.")
            return pl
        what = (f"CC{controller}" if mode == "cc" else "notes")
        print(f"Playing phrase {phrase} of pattern {pattern}, track {track} "
              f"({self._pattern_view(track, pattern)['phrases'][phrase]}) -> "
              f"port '{view0['port']}', channel {channel} ({what}), "
              f"{view0['bpm']} BPM ({kind}, step {step_time * 1000:.1f} ms). "
              f"Type 'stop' to end.")
        return pl

    def launch_loop(self, track, pattern, order_index=None, phrase=None):
        """Play a loop pattern: phrase f<k> (like note patterns) or order o<k>.

        With a phrase, the phrase decides the arrangement (``f0 2*o0+o1``) and
        the order lists are its units; without one the order list plays as-is,
        which is shorthand for ``inf*o<k>``.
        """
        if not sampler.audio_available():
            print("Error: sample playback needs Windows (winmm).")
            return None
        view = self._pattern_view(track, pattern)
        if view["type"] != "loop":
            print(f"Error: pattern {pattern} of track {track} is type "
                  f"'{view['type']}', not 'loop' "
                  f"(use 'type loop' in that pattern).")
            return None
        if not view["sample"]:
            print(f"Error: no sample selected for pattern {pattern} of track "
                  f"{track} (use 'sample <file>' from the samples folder).")
            return None
        path, err = sampler.resolve_sample(view["sample"])
        if err:
            print(f"Error: {err}")
            return None
        if phrase is None and order_index is None:
            print("Error: give a phrase (f<k>) or an order list (o<k>).")
            return None

        def build():
            """Fresh sample/order per cycle, so edits apply on the next pass."""
            view = self._pattern_view(track, pattern)
            sample = sampler.load_sample(path)
            if phrase is None:
                entries = list((view["orders"] or {}).get(order_index) or [])
                if not entries:
                    raise LookupError(f"order list o{order_index} of pattern "
                                      f"{pattern} is not configured")
                steps = [(order_index, entry) for entry in entries]
                label = f"o{order_index}"
            else:
                expr = (view["phrases"] or {}).get(phrase)
                if expr is None:
                    raise LookupError(
                        f"phrase {phrase} of pattern {pattern} is not configured")
                plan, _infinite = player.flatten_phrase(
                    expr, lambda kind, idx: (view["orders"] or {}).get(idx)
                    if kind == "order" else None)
                steps = list(plan)
                if not steps:
                    raise LookupError(f"phrase f{phrase} yields no steps")
                label = f"f{phrase} ({expr})"
            step_time = player.step_time_for(view["bpm"], view["division"])
            label = (f"{label} of pattern {pattern}, track {track} "
                     f"('{view['sample']}', {sample.duration_text()})")
            order = [entry for _index, entry in steps]
            return sample, steps, order, step_time, label

        def live_order(steps, bpm):
            """Resolve positions and velocities at every step.

            Each item becomes (position, velocity) - or a callable returning it -
            so an LFO moves the position while v<k> scales the volume, exactly
            like velocity works for a note pattern's s<k>.
            """
            lfos = self.project.get("lfos", {})
            view = self._pattern_view(track, pattern)
            velocities = view.get("velocities") or {}
            out = []
            for index, entry in steps:
                vexpr = velocities.get(index)

                def value(now, e=entry, v=vexpr):
                    position = order_entry_value(e, lfos, bpm, now)
                    if position is None:
                        return None
                    if not v:
                        return position
                    try:
                        return (position, velocity.evaluate(v, lfos, bpm, now))
                    except ValueError:
                        return position

                if isinstance(entry, str) or vexpr:
                    out.append(value)
                else:
                    out.append(entry)
            return out

        def prepare(spec):
            """-> ((sample, live order, step_time, label), printable text)."""
            sample, steps, order, step_time, label = spec
            view = self._pattern_view(track, pattern)
            text = order_to_text(order)
            return ((sample, steps, live_order(steps, view["bpm"]),
                     step_time, label), text)

        try:
            spec, order_text = prepare(build())
        except (ValueError, OSError, LookupError, phrases.PhraseError) as e:
            print(f"Error: {e}")
            return None
        self.stop_pattern(track, pattern, announce=False)
        sample, _steps, order, step_time, label = spec
        key = ((track, pattern, phrase) if phrase is not None
               else (track, pattern, "o", order_index))
        def player_spec():
            """What the LoopPlayer needs: sample, live order, step, label."""
            sample, _steps, order, step_time, label = prepare(build())[0]
            return sample, order, step_time, label

        pl = sampler.LoopPlayer(player_spec, key=key)
        self.playback[key] = pl
        pl.start()
        what = f"f{phrase}" if phrase is not None else f"o{order_index}"
        bars = bars_text(len(order), view["division"])
        bars_txt = f" = {bars}" if bars else ""
        vel_text = ""
        used = sorted({index for index, _entry in _steps})
        ves = (view.get("velocities") or {})
        if any(ves.get(i) for i in used):
            shown = ", ".join(f"v{i}={ves[i]}" for i in used if ves.get(i))
            vel_text = f", velocity {shown}"
        print(f"Playing {label}: {len(order)} steps "
              f"({order_text}) = positions in the sample, {view['bpm']} BPM, "
              f"step {step_time * 1000:.1f} ms{vel_text}{bars_txt}, looped. "
              f"Type 'stop {what}' to end.")
        return pl

    def launch(self, track, pattern, kind, index):
        """Start playback of a phrase (f) or loop order (o), by pattern type."""
        if kind == "o":
            return self.launch_loop(track, pattern, order_index=index)
        if self._pattern_view(track, pattern)["type"] == "loop":
            return self.launch_loop(track, pattern, phrase=index)
        return self.launch_phrase(track, pattern, index)

    def stop_target(self, track, pattern, kind, index, announce=True):
        """Stop a phrase or loop order, whichever the pattern type uses."""
        if kind == "o":
            return self.stop_loop(track, pattern, index, announce=announce)
        if self._pattern_view(track, pattern)["type"] == "loop":
            return self.stop_loop(track, pattern, None, phrase=index,
                                  announce=announce)
        return self.stop_phrase(track, pattern, index, announce=announce)

    def stop_loop(self, track, pattern, order_index, phrase=None,
                  announce=True):
        """Stop a running loop order list or loop phrase."""
        if phrase is not None:
            key = (track, pattern, phrase)
            what = f"f{phrase}"
        else:
            key = (track, pattern, "o", order_index)
            what = f"o{order_index}"
        pl = self.playback.pop(key, None)
        if pl is None:
            if announce:
                print(f"No loop playing for {what} of pattern "
                      f"{pattern}, track {track}.")
            return False
        try:
            pl.stop()
        except Exception:
            pass
        sampler.stop_audio()
        if announce:
            print("Loop stopped.")
        return True

    def stop_pattern(self, track, pattern, announce=True):
        """Stop any phrase currently playing for a track/pattern."""
        keys = [k for k in self.playback if k[0] == track and k[1] == pattern]
        if not keys:
            if announce:
                print(f"Nothing is playing for pattern {pattern} of track {track}.")
            return False
        for key in keys:
            try:
                self.playback.pop(key).stop()
            except Exception:
                pass
        if announce:
            print("Playback stopped.")
        return True

    def stop_phrase(self, track, pattern, phrase, announce=True):
        """Stop phrase playback of a specific (track, pattern, phrase)."""
        key = (track, pattern, phrase)
        pl = self.playback.pop(key, None)
        if pl is None:
            if announce:
                print(f"No playback running for p{phrase} of pattern {pattern}, "
                      f"track {track}.")
            return False
        try:
            pl.stop()
        except Exception:
            pass
        if announce:
            print("Playback stopped.")
        return True

    def do_start(self, arg):
        """Play a phrase or a loop order: start t1p1f1 / start t1p1o0"""
        try:
            path = parse_compact_path(arg, None, None)
            if path["pattern"] is None or path["kind"] not in ("f", "o"):
                raise ValueError("use start t<n>p<m>f<k> (phrase) or "
                                 "t<n>p<m>o<k> (loop order)")
        except ValueError as e:
            print(f"Error: {e}")
            return
        self.launch(path["track"], path["pattern"], path["kind"],
                    path["index"])

    def do_stop(self, arg):
        """Stop a phrase or a loop order: stop t1p1f1 / stop t1p1o0"""
        try:
            path = parse_compact_path(arg, None, None)
            if path["pattern"] is None or path["kind"] not in ("f", "o"):
                raise ValueError("use stop t<n>p<m>f<k> (phrase) or "
                                 "t<n>p<m>o<k> (loop order)")
        except ValueError as e:
            print(f"Error: {e}")
            return
        self.stop_target(path["track"], path["pattern"], path["kind"],
                         path["index"])

    def _pattern_block(self, track, pattern, running, running_orders=()):
        """Lines describing one pattern and its leaves (color marks running)."""
        view = self._pattern_view(track, pattern)
        seqs = view["seqs"]
        phrases = view["phrases"]
        velocities = view["velocities"]
        sustains = view["sustains"]
        microtimes = view["microtimes"]
        orders = view["orders"]
        sample = view["sample"]
        color_used = False
        is_loop = view["type"] == "loop"
        if not seqs and not phrases and not velocities and not sustains \
                and not microtimes and not orders and not sample:
            return None
        port_text = f"{view['port']}" if view["port"] else "(none selected)"
        what = ("type note" if view["type"] not in ("cc", "loop", "osc")
                else f"type CC{view['controller']}" if view["type"] == "cc"
                else "type osc" if view["type"] == "osc"
                else "type loop")
        block = [f"  Pattern {pattern}: {what}, scale {view['scale']}, root "
                 f"{view['root'].name()}, channel {view['channel']}, "
                 f"bpm {view['bpm']}, "
                 f"division {division_to_text(view['division'])}, "
                 f"port {port_text}"]
        if view["type"] == "osc":
            server = view["osc_server"] or (
                f"{supercollider.DEFAULT_HOST}:"
                f"{supercollider.DEFAULT_SERVER_PORT} (default)")
            fx = (", ".join(f"{k}={v}" for k, v in sorted(view["fx"].items()))
                  if view["fx"] else "no fx parameters")
            block.append(f"    osc: {server}, synth "
                         f"'{view['synth'] or supercollider.DEFAULT_SYNTH}', "
                         f"fx {fx}")
        if is_loop:
            step_ms = division_step_ms(view["bpm"], view["division"])
            seconds = (sampler.sample_seconds(
                sampler.resolve_sample(sample)[0]) if sample else 0.0)
            block.append(f"    sample: {sample or '(none selected)'}  "
                         f"[{seconds:.2f} s, positions 0..1, step "
                         f"{step_ms:g} ms, {sampler.samples_dir()}]")
            for n in sorted(orders):
                line = (f"    o{n}: {order_to_text(orders[n])}   "
                        f"({len(orders[n])} steps)")
                if (track, pattern, n) in running_orders:
                    line = paint_green(line)
                    color_used = True
                block.append(line)
            for n in sorted(phrases):
                line = f"    f{n}: {phrases[n]}"
                if (track, pattern, n) in running:
                    line = paint_green(line)
                    color_used = True
                block.append(line)
            return block, color_used
        for n in sorted(seqs):
            seq = seqs[n]
            if view["type"] == "cc":
                notes_text = describe_cc_values(
                    seq, self.project.get('lfos', {}), view['bpm'],
                    time.monotonic(), notes=True)
            else:
                notes_text = resolve_seq_notes(
                    seq, view['root'], view['scale'],
                    self.project.get('lfos', {}), view['bpm'],
                    time.monotonic())
            line = f"    seq{n}: {seq_to_text(seq):<28} -> {notes_text}"
            extra = []
            if n in velocities:
                extra.append(f"v{n}: {velocities[n]}")
            if n in sustains:
                extra.append(f"sus{n}: {sustains[n]}")
            if n in microtimes:
                extra.append(f"mt{n}: {microtimes[n]}")
            if extra:
                line += "   [" + "; ".join(extra) + "]"
            block.append(line)
        for n in sorted(velocities):
            if n not in seqs:
                block.append(f"    v{n}: {velocities[n]}")
        for n in sorted(sustains):
            if n not in seqs:
                block.append(f"    sus{n}: {sustains[n]}")
        for n in sorted(microtimes):
            if n not in seqs:
                block.append(f"    mt{n}: {microtimes[n]}")
        for n in sorted(phrases):
            line = f"    f{n}: {phrases[n]}"
            if (track, pattern, n) in running:
                line = paint_green(line)
                color_used = True
            block.append(line)
        return block, color_used

    def do_show(self, arg):
        "Show the configuration tree, or a path: show [t<n>[p<m>[s<k>|f<k>|o<k>]]]"
        tracks = self.project.get("tracks", {})
        text = (arg or "").strip()
        selected = None
        if text:
            try:
                selected = parse_compact_path(text, None, None)
            except ValueError as e:
                print(f"Error: {e}")
                print("Usage: show                     (whole configuration)")
                print("       show t<n>                 (one track)")
                print("       show t<n>p<m>             (one pattern)")
                print("       show t<n>p<m>s<k>|f<k>|v<k>|sus<k>|mt<k>|o<k>  (one leaf)")
                return

        # Global settings first (BPM, output port, MIDI clock)
        global_line = f"Global: bpm {self.bpm}"
        if self.out_port:
            global_line += f", port {self.out_port}"
        if self.clock_mode != midiclock.MODE_OFF:
            global_line += f", clock {self.clock_mode}"
            if self.clock_port:
                global_line += f" on {self.clock_port}"
            if (self.clock_mode == midiclock.MODE_RECEIVE
                    and self.clock_start == midiclock.START_RECEIVED):
                global_line += " (start received)"
        if self.clock_mode != midiclock.MODE_OFF:
            print(global_line)

        running = set()
        running_orders = set()
        for key, pl in self.playback.items():
            if not getattr(pl, "running", False):
                continue
            if len(key) == 4 and key[2] == "o":
                running_orders.add((key[0], key[1], key[3]))
            elif len(key) == 3:
                running.add(key)

        # Leaf views -----------------------------------------------------
        if selected and selected["kind"] == "o":
            track, pattern = selected["track"], selected["pattern"]
            view = self._pattern_view(track, pattern)
            n = selected["index"]
            orders = view["orders"]
            print(f"\nTrack {track}, Pattern {pattern} "
                  f"(type {view['type']}, division "
                  f"{division_to_text(view['division'])}, positions 0..1):")
            if n not in orders:
                print(f"  o{n} of t{track}p{pattern} is not configured.\n")
                return
            line = (f"  o{n}: {order_to_text(orders[n])}   "
                    f"({len(orders[n])} steps)")
            if (track, pattern, n) in running_orders:
                line = paint_green(line)
                print(line)
                print(f"{paint_green('green')} = currently playing")
            else:
                print(line)
            print()
            return

        if selected and selected["kind"] in ("s", "f", "v", "u", "m"):
            track, pattern = selected["track"], selected["pattern"]
            if track not in tracks or pattern not in tracks[track]:
                print(f"\n(nothing configured for t{track}p{pattern})\n")
                return
            view = self._pattern_view(track, pattern)
            if view["type"] == "cc":
                desc = f"type CC{view['controller']}, values 0-127"
            elif view["type"] == "osc":
                desc = (f"type osc, synth "
                        f"'{view['synth'] or supercollider.DEFAULT_SYNTH}' "
                        f"-> {view['osc_server'] or 'default server'}")
            elif view["type"] == "loop":
                length = (sampler.sample_seconds(
                    sampler.resolve_sample(view["sample"])[0])
                    if view["sample"] else 0.0)
                desc = (f"type loop, sample {view['sample'] or '(none)'}, "
                        f"{length:.2f} s")
            else:
                desc = f"{view['scale']} on {view['root'].name()}"
            head = f"Track {track}, Pattern {pattern} ({desc}):"
            n = selected["index"]
            if selected["kind"] == "v":
                if n not in view["velocities"]:
                    print(f"\nv{n} of t{track}p{pattern} is not configured.\n")
                    return
                print(f"\n{head}")
                print(f"  v{n}: {view['velocities'][n]}")
                print()
                return
            if selected["kind"] == "u":
                if n not in view["sustains"]:
                    print(f"\nsus{n} of t{track}p{pattern} is not configured.\n")
                    return
                print(f"\n{head}")
                print(f"  sus{n}: {view['sustains'][n]}")
                print()
                return
            if selected["kind"] == "m":
                if n not in view["microtimes"]:
                    print(f"\nmt{n} of t{track}p{pattern} is not configured.\n")
                    return
                print(f"\n{head}")
                print(f"  mt{n}: {view['microtimes'][n]}")
                print()
                return
            if selected["kind"] == "s":
                if n not in view["seqs"]:
                    print(f"\nseq{n} of t{track}p{pattern} is not configured.\n")
                    return
                seq = view["seqs"][n]
                print(f"\n{head}")
                if view["type"] == "cc":
                    notes_text = describe_cc_values(
                        seq, self.project.get('lfos', {}), view['bpm'],
                        time.monotonic(), notes=True)
                else:
                    notes_text = resolve_seq_notes(
                        seq, view['root'], view['scale'],
                        self.project.get('lfos', {}), view['bpm'],
                        time.monotonic())
                print(f"  seq{n}: {seq_to_text(seq):<28} -> {notes_text}")
            else:
                if n not in view["phrases"]:
                    print(f"\nf{n} of t{track}p{pattern} is not configured.\n")
                    return
                line = f"  f{n}: {view['phrases'][n]}"
                if (track, pattern, n) in running:
                    line = paint_green(line)
                print(f"\n{head}")
                print(line)
                if (track, pattern, n) in running:
                    print(f"{paint_green('green')} = currently playing")
            print()
            return

        # Tree views -----------------------------------------------------
        if selected and selected["pattern"] is not None:
            track, pattern = selected["track"], selected["pattern"]
            built = self._pattern_block(track, pattern, running,
                                        running_orders)
            if built is None:
                print(f"\n(nothing configured for t{track}p{pattern})\n")
                return
            block, color_used = built
            print(f"Track {track}")
            print("\n".join(block))
            print()
            if color_used:
                print(f"{paint_green('green')} = currently playing")
            print()
            return

        track_ids = sorted(tracks)
        if selected:
            if selected["track"] not in tracks:
                print(f"\n(nothing configured for track {selected['track']})\n")
                return
            track_ids = [selected["track"]]
            port = self.out_port or "(none)"
            print(f"\nGlobal: BPM {self.bpm}, output port {port}\n")
        else:
            port = self.out_port or "(none)"
            print(f"\nGlobal: BPM {self.bpm}, output port {port}\n")
        shown_any = False
        color_used = False
        for track in track_ids:
            pattern_blocks = []
            for pattern in sorted(tracks[track]):
                built = self._pattern_block(track, pattern, running,
                                            running_orders)
                if built is None:
                    continue
                block, colored = built
                color_used = color_used or colored
                pattern_blocks.append(block)
            if not pattern_blocks:
                continue
            shown_any = True
            print(f"Track {track}")
            for block in pattern_blocks:
                print("\n".join(block))
                print()
        if color_used:
            print(f"{paint_green('green')} = currently playing")
        if not shown_any:
            print("(nothing configured yet)")
        print()

    def do_help(self, arg):
        if arg:
            # cmd.Cmd parseline splits on non-identifier chars, so normalize hyphens first
            return super().do_help(arg.strip().replace("-", "_"))
        print("\nAvailable Commands:")
        print(f"  {'list-ports':<20} - list available MIDI input and output ports")
        print(f"  {'clock [...]':<20} - MIDI clock 24 PPQN: clock send|receive|off, clock port <name>, clock start internal|received")
        print(f"  {'start-all':<20} - start every configured pattern (what a received MIDI Start does)")
        print(f"  {'select-port':<20} - choose the MIDI output port for playback")
        print(f"  {'bpm [<value>]':<20} - show or set the global BPM (default 120)")
        print(f"  {'t<n>':<20} - enter a track menu (n = 1-16)")
        print(f"  {'t<n>p<m>':<20} - enter a pattern menu (e.g. t1p1)")
        print(f"  {'t<n>p<m>s<k> [deg...]':<20} - set a sequence (s0..s9) e.g. t1p1s1 0 0 O1")
        print(f"  {'t<n>p<m>f<k> <expr>':<20} - define a phrase (f0..f7) e.g. t1p1f0 inf*s0")
        print(f"  {'t<n>p<m> scale|root|bpm|channel':<20} - edit a pattern setting")
        print(f"  {'t<n>p<m> division 1/16':<20} - step note value (1, 1/2, 1/3, 1/16 ...)")
        print(f"  {'t<n>p<m> type note|CC|loop':<20} - pattern kind (default note)")
        print(f"  {'t<n>p<m> sample <file.wav>':<20} - loop pattern: choose a wav from samples/")
        print(f"  {'t<n>p<m>o<k> <slice...>':<20} - loop slice order, e.g. t1p1o0 0 1 4 2 5")
        print(f"  {'show [t<n>[p<m>[s<k>|f<k>|o<k>]]]':<20} - config tree or one leaf")
        print(f"  {'<path> ...':<20} - edit by path, e.g. t1p1s0 0 0 5, "
              f"t1p1v0 64+0.4*lfo1, t1p1sus0 0.5 or t1p1mt0 -0.05")
        print(f"  {'start t<n>p<m>f<k>':<20} - play a phrase (e.g. start t1p1f1)")
        print(f"  {'start t<n>p<m>o<k>':<20} - play a loop order (e.g. start t1p1o0)")
        print(f"  {'stop t<n>p<m>f<k>|o<k>':<20} - stop a phrase or loop order")
        print(f"  {'lfo <n>':<20} - enter an LFO definition menu (n = 1-8)")
        print(f"  {'lfo<n> start|stop':<20} - start or stop an LFO from here (e.g. lfo3 start)")
        print(f"  {'status-lfo':<20} - show the parameters of all started LFOs")
        print(f"  {'cp <src> <dst>':<20} - copy a track, pattern, leaf or LFO (cp lfo1 lfo2)")
        print(f"  {'rm <path>|lfo<n>':<20} - delete a track/pattern/sequence/phrase, reset an LFO or a setting")
        print(f"  {'panic':<20} - stop all playback (phrases, loops) and silence outputs")
        print(f"  {'save [<file>]':<20} - save the whole configuration to a .cfg file")
        print(f"  {'load [<file>]':<20} - load the whole configuration from a .cfg file")
        print(f"  {'help':<20} - show this help (help <command> for details)")
        print(f"  {'exit':<20} - exit the shell")
        print()
        if self.out_port:
            print(f"  Output port: {self.out_port}")
            print()
        print("Context:\n  Tracks hold 16 patterns each; patterns hold sequences\n"
              "  (s0..s9) and phrases (f0..f7). Loop patterns add order lists\n"
              "  (o0..o9) of positions in the sample. Shortcuts: t=track,\n"
              "  p=pattern,\n"
              "  s=sequence, f=phrase, o=order.")
        print()

    def free_supercollider(self, announce=False):
        """Free every playing node on the SuperCollider servers in use."""
        engines = list(supercollider._engines.values())
        freed = 0
        for engine in engines:
            if engine.free_all():
                freed += 1
        if announce:
            if freed:
                print(f"Sent /g_freeAll to {freed} SuperCollider "
                      f"server(s).")
            else:
                print("No SuperCollider server was reachable.")
        return freed

    def _stop_all_playback(self):
        """Stop every running player (phrases, loops) - exit and panic."""
        for key, pl in list(self.playback.items()):
            try:
                pl.stop()
            except Exception:
                pass
            self.playback.pop(key, None)
        sampler.stop_audio()

    def panic(self):
        """Stop phrases/loops, silence MIDI and free SuperCollider nodes."""
        ports = {self.out_port} if self.out_port else set()
        ports.update(pl.port_name for pl in self.playback.values())
        count = len(self.playback)
        had_osc = bool(supercollider._engines)
        self._stop_all_playback()
        sent = player.panic_ports(ports)
        sc = self.free_supercollider() if had_osc else 0
        if count or sent or sc:
            extra = (f"; sent All Sound Off on "
                     f"{len(sent)} port{'s' if len(sent) != 1 else ''}" if sent
                     else "")
            if sc:
                extra += f"; freed SuperCollider nodes on {sc} server(s)"
            print(f"Panic: stopped {count} playback{'s' if count != 1 else ''}"
                  f"{extra}.")
        else:
            print("Panic: nothing playing.")

    def do_panic(self, arg):
        "Stop everything (phrases, loops) and silence MIDI/audio outputs"
        self.panic()

    def do_status(self, arg):
        "Ask a SuperCollider server for /status (default: 127.0.0.1:57110)"
        targets = [engine.target_text
                   for engine in supercollider._engines.values()]
        target = (arg or "").strip() or (
            targets[0] if targets else supercollider.DEFAULT_HOST + ":"
            + str(supercollider.DEFAULT_SERVER_PORT))
        try:
            server = supercollider.parse_target(
                target, supercollider.DEFAULT_SERVER_PORT)
        except ValueError as e:
            print(f"Error: {e}")
            return
        engine = supercollider.engine_for(server, None)
        print(f"Pinging {engine.target_text} ...")
        info = engine.status(timeout=1.5)
        if not info:
            print("  No /status.reply: no SuperCollider server is listening "
                  "there (start scsynth / 's.boot', or name one: "
                  "'status <host>:<port>').")
            return
        print(f"  Synths {info['synths']}, groups {info['groups']}, "
              f"SynthDefs {info['synthdefs']}, UGens {info['ugens']}, "
              f"CPU {info['avg_cpu']:.1f}%, SR {info['sample_rate']:.0f} Hz")
        if arg and targets:
            print(f"  (patterns use: {', '.join(targets)})")

    def do_dump(self, arg):
        "Record/show the OSC traffic of the pattern(s): dump [on|off|clear]"
        if not supercollider._engines:
            print("No OSC traffic yet (use 'type osc' in a pattern and play it).")
            return
        text = (arg or "").strip().lower()
        engines = list(supercollider._engines.values())
        if text in ("on", "start"):
            for engine in engines:
                engine.recording = True
            print(f"OSC recording on for {len(engines)} target(s).")
            return
        if text in ("off", "stop"):
            for engine in engines:
                engine.recording = False
            print("OSC recording off.")
            return
        if text in ("clear", "reset"):
            for engine in engines:
                engine.log = []
            print("OSC log cleared.")
            return
        for engine in engines:
            state = "on" if engine.recording else "off"
            print(f"{engine.target_text}: recording {state}, "
                  f"{engine.sent_notes} notes sent")
            for line in engine.log[-8:]:
                print(f"    {line}")

    def do_cp(self, arg):
        "Copy a track, pattern, leaf or LFO: cp <source> <destination>"
        parts = (arg or "").split()
        if len(parts) != 2:
            print("Usage: cp <source> <destination>")
            print("       cp t1 t2             copy a whole track")
            print("       cp t1p1 t3p4         copy a pattern")
            print("       cp t1p1s1 t3p4s4     copy a sequence")
            print("       cp t1p1f1 t3p4f3     copy a phrase")
            print("       cp t1p1v1 t3p4v4     copy a velocity")
            print("       cp t1p1sus1 t3p4sus4 copy a sustain")
            print("       cp t1p1mt1 t3p4mt4   copy a microtiming")
            print("       cp lfo1 lfo2         copy all LFO parameters")
            print("       cp /lfo1 /lfo2       same, from any menu")
            return
        # LFO operands, Unix-style ('lfo1' or '/lfo1', any menu).
        operands = [re.sub(r"^/+", "", part) for part in parts]
        m = [re.fullmatch(r"(?i)lfo\s*(\d+)", op) for op in operands]
        if any(m):
            if not all(m):
                print("Error: both operands must be LFOs, e.g. cp lfo1 lfo2.")
                return
            src, dst = (int(hit.group(1)) for hit in m)
            for n in (src, dst):
                if not 1 <= n <= MAX_LFOS:
                    print(f"Error: LFO number must be between 1 and {MAX_LFOS}.")
                    return
            self.copy_lfo(src, dst)
            return
        try:
            src = parse_compact_path(parts[0], None, None)
            dst = parse_compact_path(parts[1], None, None)
        except ValueError as e:
            print(f"Error: {e}")
            return
        tracks = self.project.setdefault("tracks", {})

        # --- whole track ------------------------------------------------
        if src["pattern"] is None and dst["pattern"] is None:
            st, dt = src["track"], dst["track"]
            source = tracks.get(st) or {}
            if st == dt:
                print(f"Copied track t{st} -> t{dt} "
                      f"({len(source)} pattern{'s' if len(source) != 1 else ''}); "
                      f"unchanged.")
                return
            for pattern in list(tracks.get(dt, {})):
                self.stop_pattern(dt, pattern, announce=False)
            if source:
                tracks[dt] = copy.deepcopy(source)
                print(f"Copied track t{st} -> t{dt} "
                      f"({len(source)} pattern{'s' if len(source) != 1 else ''}).")
            else:
                tracks.pop(dt, None)  # an empty source clears the destination
                print(f"Copied track t{st} -> t{dt}; t{dt} is empty "
                      f"(t{st} has nothing configured).")
            return

        # --- pattern ----------------------------------------------------
        if src["kind"] is None and dst["kind"] is None:
            if src["pattern"] is None or dst["pattern"] is None:
                print("Error: source and destination must both be tracks, both "
                      "patterns, both sequences or both phrases.")
                return
            st, sp = src["track"], src["pattern"]
            dt, dp = dst["track"], dst["pattern"]
            entry = tracks.get(st, {}).get(sp) or {}
            if (st, sp) == (dt, dp):
                print(f"Copied pattern t{st}p{sp} -> t{dt}p{dp}; unchanged.")
                return
            self.stop_pattern(dt, dp, announce=False)
            if entry:
                tracks.setdefault(dt, {})[dp] = copy.deepcopy(entry)
                print(f"Copied pattern t{st}p{sp} -> t{dt}p{dp}.")
            else:
                tracks.get(dt, {}).pop(dp, None)
                self._prune_pattern(tracks, dt, dp)
                print(f"Copied pattern t{st}p{sp} -> t{dt}p{dp}; "
                      f"t{dt}p{dp} reset to defaults (t{st}p{sp} is empty).")
            return

        # --- sequence / phrase / velocity / sustain / microtiming --------
        if src["kind"] != dst["kind"] or src["kind"] not in ("s", "f", "v", "u", "m", "o"):
            print("Error: source and destination must both be tracks, both "
                  "patterns, both sequences, both phrases, both velocities, "
                  "both sustains or both microtimings.")
            return
        bucket = {"s": "seqs", "f": "phrases", "v": "velocities",
                  "u": "sustains", "m": "microtimes", "o": "orders"}[src["kind"]]
        kind_name = {"s": "sequence", "f": "phrase", "v": "velocity",
                     "u": "sustain", "m": "microtiming",
                     "o": "order list"}[src["kind"]]
        shown = {"s": f"s{src['index']}", "f": f"f{src['index']}",
                 "v": f"v{src['index']}", "u": f"sus{src['index']}",
                 "m": f"mt{src['index']}", "o": f"o{src['index']}"}[src["kind"]]
        dest_shown = {"s": f"s{dst['index']}", "f": f"f{dst['index']}",
                      "v": f"v{dst['index']}", "u": f"sus{dst['index']}",
                      "m": f"mt{dst['index']}", "o": f"o{dst['index']}"}[dst["kind"]]
        src_name = f"t{src['track']}p{src['pattern']}{shown}"
        dst_name = f"t{dst['track']}p{dst['pattern']}{dest_shown}"
        source_map = (tracks.get(src["track"], {}).get(src["pattern"], {})
                      .get(bucket, {}))
        if (src["track"], src["pattern"], src["kind"], src["index"]) == \
                (dst["track"], dst["pattern"], dst["kind"], dst["index"]):
            print(f"Copied {kind_name} {src_name} -> {dst_name}; unchanged.")
            return
        # A sequence carries its velocity / sustain / microtiming entries
        # (same index): copying s0 -> s1 also copies v0 -> v1, sus0 -> sus1,
        # mt0 -> mt1, and clears destination companions the source lacks.
        companions = ("velocities", "sustains", "microtimes") \
            if src["kind"] == "s" else ()
        source_entry = tracks.get(src["track"], {}).get(src["pattern"]) or {}
        dest_entry = tracks.get(dst["track"], {}).get(dst["pattern"])
        copied_with = []
        for bucket_name in companions:
            if src["index"] in (source_entry.get(bucket_name) or {}):
                copied_with.append(bucket_name)

        if src["index"] in source_map or copied_with:
            dest_entry = tracks.setdefault(dst["track"], {}).setdefault(
                dst["pattern"], {})
            if src["index"] in source_map:
                dest_entry.setdefault(bucket, {})[dst["index"]] = \
                    copy.deepcopy(source_map[src["index"]])
            elif dest_entry.get(bucket, {}).pop(dst["index"], None) is not None:
                pass  # companion-only copy: no sequence text to write
            written = []
            for bucket_name in companions:
                target = dest_entry.setdefault(bucket_name, {})
                if src["index"] in (source_entry.get(bucket_name) or {}):
                    target[dst["index"]] = copy.deepcopy(
                        source_entry[bucket_name][src["index"]])
                    written.append({"velocities": f"v{dst['index']}",
                                    "sustains": f"sus{dst['index']}",
                                    "microtimes": f"mt{dst['index']}"}[bucket_name])
                else:
                    target.pop(dst["index"], None)
                if not target:
                    dest_entry.pop(bucket_name, None)
            extra = f" ({', '.join(written)} too)" if written else ""
            print(f"Copied {kind_name} {src_name} -> {dst_name}{extra}.")
        else:
            if dest_entry:
                for name, bucket_name in (("seqs", bucket),) + tuple(
                        (b, b) for b in companions):
                    mapping = dest_entry.get(bucket_name) or {}
                    mapping.pop(dst["index"], None)
                    if not mapping:
                        dest_entry.pop(bucket_name, None)
                self._prune_pattern(tracks, dst["track"], dst["pattern"])
            print(f"Copied {kind_name} {src_name} -> {dst_name}; "
                  f"{dst_name} cleared ({src_name} is not configured).")

    def _prune_pattern(self, tracks, track, pattern):
        """Drop empty pattern/track entries after a deletion."""
        tmap = tracks.get(track)
        if tmap is None:
            return
        if pattern in tmap and not tmap[pattern]:
            del tmap[pattern]
        if not tmap:
            del tracks[track]

    def reset_lfo_param(self, n, param, announce=True):
        """Reset one LFO parameter (frequency/shape/phase/running)."""
        param = {"freq": "frequency"}.get(param.lower(), param.lower())
        entry = self.project.setdefault("lfos", {}).setdefault(n, {})
        if param == "running":
            entry.pop("running", None)
            entry.pop(LFO_START_KEY, None)
            shown = "stopped"
        else:
            entry.pop(param, None)
            shown = {"frequency": "1 Hz", "shape": "sin", "phase": "0"}[param]
        if announce:
            suffix = " (default)" if shown != "stopped" else " (LFO stopped)"
            print(f"Reset LFO {n} {param} to {shown}{suffix}.")
        return shown

    def copy_lfo(self, src, dst):
        """Copy one LFO onto another; defaults count as values (cp is total)."""
        lfos = self.project.setdefault("lfos", {})
        source = lfos.get(src) or {}
        target = lfos.get(dst) or {}
        was_running = bool(source.get("running"))
        values = {key: source.get(key, default)
                  for key, default in LFO_DEFAULTS.items()}
        summary = ("frequency " + lfo.param_to_text(values["frequency"])
                   + f", shape {values['shape']}"
                   + f", phase {float(values['phase']):g}")
        # Nothing to do? (same LFO, or identical parameters) -- still a success.
        current = {key: target.get(key, default)
                   for key, default in LFO_DEFAULTS.items()}
        if src == dst or (current == values
                          and bool(target.get("running")) == was_running):
            state = ("running" if was_running else "stopped")
            note = "unchanged" if src == dst else "no change"
            print(f"Copied LFO {src} -> LFO {dst} ({summary}); "
                  f"{state}, {note}.")
            return
        if values == dict(LFO_DEFAULTS):
            lfos.pop(dst, None)  # all-default source: destination becomes default
        else:
            lfos[dst] = dict(values)
        self._lfo_set_running(dst, was_running)
        if was_running:
            print(f"Copied LFO {src} -> LFO {dst} ({summary}); "
                  f"LFO {dst} started (LFO {src} is running).")
        else:
            print(f"Copied LFO {src} -> LFO {dst} ({summary}); LFO {dst} stopped.")

    def reset_lfo(self, n, announce=True):
        """Reset one global LFO to its defaults (and stop it)."""
        entry = self.project.setdefault("lfos", {}).get(n)
        was_running = bool(entry and entry.get("running"))
        self.project.get("lfos", {}).pop(n, None)
        if announce:
            print(f"Reset LFO {n} to defaults (frequency 1 Hz, shape sin, "
                  f"phase 0, stopped).")
            if was_running:
                print(f"  Note: LFO {n} was running and has been stopped.")
        return was_running

    def do_rm(self, arg):
        "Delete/zeroize a track, pattern, sequence, phrase or pattern setting"
        parts = (arg or "").split()
        if not 1 <= len(parts) <= 2:
            print("Usage: rm t1                 remove a whole track")
            print("       rm t1p1               remove a pattern")
            print("       rm t1p1s1 / t1p1f1    remove a sequence / phrase")
            print("       rm t1p1v1             remove a velocity")
            print("       rm t1p1sus1           remove a sustain")
            print("       rm t1p1mt1            remove a microtiming")
            print("       rm t1p1o1             remove a loop order list")
            print("       rm t1p1 scale|root|bpm|channel|division|type|controller|port")
            print("       rm lfo1               reset an LFO (rm lfo = all of them)")
            return
        # LFO targets are not paths: 'lfo1', 'lfo 1' or 'lfo' (all LFOs).
        text = " ".join(parts)
        m = re.fullmatch(
            r"(?i)lfo\s*(\d+|all)?(?:\s+(frequency|freq|shape|phase|running))?",
            text)
        if m:
            if not m.group(1) or m.group(1).lower() == "all":
                for n in range(1, MAX_LFOS + 1):
                    self.reset_lfo(n, announce=False)
                print(f"Reset all {MAX_LFOS} LFOs to their defaults (stopped).")
                return
            n = int(m.group(1))
            if not 1 <= n <= MAX_LFOS:
                print(f"Error: LFO number must be between 1 and {MAX_LFOS}.")
                return
            if m.group(2):
                self.reset_lfo_param(n, m.group(2))
            else:
                self.reset_lfo(n)
            return
        try:
            path = parse_compact_path(parts[0], None, None)
        except ValueError as e:
            print(f"Error: {e}")
            return
        verb = parts[1].lower() if len(parts) == 2 else None
        tracks = self.project.setdefault("tracks", {})

        # --- whole track -------------------------------------------------
        if path["pattern"] is None:
            if verb:
                print("Error: settings can only be reset on a pattern "
                      "(use t<n>p<m> <setting>).")
                return
            if path["track"] not in tracks:
                print(f"Error: track {path['track']} is not configured.")
                return
            for key in [k for k in self.playback if k[0] == path["track"]]:
                self.stop_pattern(key[0], key[1], announce=False)
            removed = len(tracks.pop(path["track"]))
            print(f"Removed track t{path['track']} "
                  f"({removed} pattern{'s' if removed != 1 else ''}).")
            return

        track, pattern = path["track"], path["pattern"]
        entry = tracks.get(track, {}).get(pattern)

        # --- reset one setting -------------------------------------------
        if verb is not None:
            keys = {"scale": ["scale"], "root": ["root"], "channel": ["channel"],
                    "bpm": ["bpm", "bpm-inherited"], "division": ["division"],
                    "type": ["type", "controller"], "controller": ["controller"],
                    "port": ["port"], "select-port": ["port"],
                    "sample": ["sample"],
                    "osc": ["osc-server", "osc-sclang", "osc-latency"],
                    "synth": ["synth"],
                    "fx": ["fx"]}
            if verb not in keys:
                print(f"Error: unknown setting '{verb}'. Use scale, root, bpm, "
                      f"channel, division, type, controller, sample, "
                      f"osc, synth, fx or port.")
                return
            if not entry:
                print(f"Error: pattern t{track}p{pattern} is not configured.")
                return
            self.stop_pattern(track, pattern, announce=False)
            for key in keys[verb]:
                entry.pop(key, None)
            self._prune_pattern(tracks, track, pattern)
            defaults = {"scale": "chromatic", "root": "C4", "channel": "1",
                        "bpm": "inherits the global BPM",
                        "division": division_to_text(DEFAULT_DIVISIONS),
                        "type": "note", "controller": "1",
                        "port": "inherits the global port",
                        "select-port": "inherits the global port",
                        "sample": "no sample",
                        "osc": "the default 127.0.0.1:57110",
                        "synth": f"the default '{supercollider.DEFAULT_SYNTH}'",
                        "fx": "no effect parameters"}
            print(f"Reset {verb} of t{track}p{pattern} ({defaults[verb]}).")
            return

        # --- sequence / phrase / whole pattern ---------------------------
        if not entry:
            print(f"Error: pattern t{track}p{pattern} is not configured.")
            return
        self.stop_pattern(track, pattern, announce=False)
        if path["kind"] == "s":
            if path["index"] not in entry.get("seqs", {}):
                print(f"Error: s{path['index']} of t{track}p{pattern} "
                      f"is not configured.")
                return
            del entry["seqs"][path["index"]]
            self._prune_pattern(tracks, track, pattern)
            print(f"Removed sequence t{track}p{pattern}s{path['index']}.")
        elif path["kind"] == "f":
            if path["index"] not in entry.get("phrases", {}):
                print(f"Error: f{path['index']} of t{track}p{pattern} "
                      f"is not configured.")
                return
            del entry["phrases"][path["index"]]
            self._prune_pattern(tracks, track, pattern)
            print(f"Removed phrase t{track}p{pattern}f{path['index']}.")
        elif path["kind"] == "v":
            if path["index"] not in entry.get("velocities", {}):
                print(f"Error: v{path['index']} of t{track}p{pattern} "
                      f"is not configured.")
                return
            del entry["velocities"][path["index"]]
            self._prune_pattern(tracks, track, pattern)
            print(f"Removed velocity t{track}p{pattern}v{path['index']}.")
        elif path["kind"] == "u":
            if path["index"] not in entry.get("sustains", {}):
                print(f"Error: sus{path['index']} of t{track}p{pattern} "
                      f"is not configured.")
                return
            del entry["sustains"][path["index"]]
            self._prune_pattern(tracks, track, pattern)
            print(f"Removed sustain t{track}p{pattern}sus{path['index']}.")
        elif path["kind"] == "m":
            if path["index"] not in entry.get("microtimes", {}):
                print(f"Error: mt{path['index']} of t{track}p{pattern} "
                      f"is not configured.")
                return
            del entry["microtimes"][path["index"]]
            self._prune_pattern(tracks, track, pattern)
            print(f"Removed microtiming t{track}p{pattern}mt{path['index']}.")
        elif path["kind"] == "o":
            if path["index"] not in entry.get("orders", {}):
                print(f"Error: o{path['index']} of t{track}p{pattern} "
                      f"is not configured.")
                return
            del entry["orders"][path["index"]]
            self._prune_pattern(tracks, track, pattern)
            print(f"Removed order list t{track}p{pattern}o{path['index']}.")
        else:
            del tracks[track][pattern]
            self._prune_pattern(tracks, track, pattern)
            print(f"Removed pattern t{track}p{pattern}.")

    def do_exit(self, arg):
        "Exit the shell"
        self.midi_clock.shutdown()
        self._stop_all_playback()
        if supercollider._engines:
            self.free_supercollider()
        supercollider.close_all()
        print("Goodbye!")
        return True

    def do_EOF(self, arg):
        self.midi_clock.shutdown()
        self._stop_all_playback()
        if supercollider._engines:
            self.free_supercollider()
        supercollider.close_all()
        print()
        return True

    def emptyline(self):
        pass

    def _handle_root_path(self, path, rest):
        """Navigate or edit via a compact path typed at the root."""
        track, pattern = path["track"], path["pattern"]
        kind, index = path["kind"], path["index"]
        if rest and kind is None:
            first_word = rest.split()[0]
            m = re.fullmatch(r"(?i)([sfvo])(\d+)", first_word)
            if m:
                kind, index = m.group(1).lower(), int(m.group(2))
                rest = rest[len(first_word):].strip()
            else:
                m = (re.fullmatch(r"(?i)sus(\d+)", first_word)
                     or re.fullmatch(r"(?i)mt(\d+)", first_word))
                if m:
                    kind = "u" if first_word.lower().startswith("sus") else "m"
                    index = int(m.group(1))
                    rest = rest[len(first_word):].strip()
        if pattern is None:
            if rest:
                raise ValueError("nothing to do with that path")
            TrackShell(self, track).cmdloop()
            return
        shell = PatternShell(TrackShell(self, track), pattern)
        if kind == "s":
            if not 0 <= index <= _PATH_MAX_SEQ:
                raise ValueError(f"sequence must be s0..s{_PATH_MAX_SEQ}")
            if rest:
                shell.set_sequence(index, rest.split())
            else:
                shell.show_sequence(index)
            return
        if kind == "f":
            if not 0 <= index <= _PATH_MAX_PHRASE:
                raise ValueError(f"phrase must be f0..f{_PATH_MAX_PHRASE}")
            shell.set_phrase(index, rest)
            return
        if kind == "v":
            shell.set_velocity(index, rest)
            return
        if kind == "u":
            shell.set_sustain(index, rest)
            return
        if kind == "m":
            shell.set_microtime(index, rest)
            return
        if kind == "o":
            if not 0 <= index <= _PATH_MAX_ORDER:
                raise ValueError(f"order list must be o0..o{_PATH_MAX_ORDER}")
            if rest.lower() in ("sample",):
                raise ValueError("use 'sample <file>' to choose the wav file")
            if rest.lower().startswith("sample"):
                shell.do_sample(rest.split(None, 1)[1] if " " in rest else "")
            elif rest:
                shell.set_order(index, rest.split())
            else:
                shell.show_order(index)
            return
        if rest:
            shell._edit_verb(rest)
            return
        shell.cmdloop()

    def run_navigation(self, token, rest=""):
        """Jump to (or act on) a path from any submenu. Returns True if handled."""
        try:
            path = parse_compact_path(token, None, None)
        except ValueError as e:
            print(f"Error: {e}")
            return False
        try:
            self._handle_root_path(path, rest)
        except ValueError as e:
            print(f"Error: {e}")
        return True

    def default(self, line):
        text = line.strip()
        if text == "/":  # already at the root menu
            return
        if text.startswith("/"):  # '/ <command>' just runs the command
            command = text[1:].strip()
            if command:
                self.onecmd(command)
            return
        parts = text.split()
        if not parts:
            return
        first = parts[0].lower()
        if first == "lfo":  # accept LFO n and LFO n start|stop in any case
            rest_tokens = parts[1:]
            if rest_tokens and rest_tokens[0].lower() in ("start", "stop"):
                self._lfo_start_stop(text)
                return
            self.do_lfo(" ".join(rest_tokens))
            return
        if re.fullmatch(r"(?i)lfo\d+\s+(start|stop)", text):
            self._lfo_start_stop(text)
            return
        if re.fullmatch(r"(?i)lfo\d+", text):  # compact form: lfo1 == lfo 1
            self.do_lfo(re.sub(r"(?i)^lfo", "", text))
            return
        m = re.fullmatch(r"(?i)lfo\s*(\d+)\s+(.+)", text)
        if m:  # e.g. 'lfo1 shape square' applied without entering the menu
            n = int(m.group(1))
            if not 1 <= n <= MAX_LFOS:
                print(f"Error: lfo number must be between 1 and {MAX_LFOS}.")
                return
            LfoShell(self, n).onecmd(m.group(2))
            return
        token = parts[0]
        rest = text[len(token):].strip()
        try:
            path = parse_compact_path(token, None, None)
        except ValueError as e:
            if _PATH_TOKEN_RE.fullmatch(token) or re.match(r"(?i)^t\d+", token):
                print(f"Error: {e}")
                return
            hint = old_command_hint(token)
            print(f"*** Unknown command: {line}")
            if hint:
                print(f"    Hint: {hint}")
            return
        try:
            self._handle_root_path(path, rest)
        except ValueError as e:
            print(f"Error: {e}")


class TrackShell(SeqCompletingCmd):
    """Context sub-menu of one track (holds 16 patterns)."""

    def __init__(self, parent, track_number):
        super().__init__()
        self.parent_shell = parent
        self.track_number = track_number
        self.prompt = f"seq:t{track_number}> "
        initialize_readline()

    def _abs_operand(self, operand):
        """Resolve a relative operand against this track: 'p1' -> 't1p1'."""
        if not operand or operand.startswith("/"):
            return operand
        if re.match(r"(?i)^t\d+", operand) or re.match(r"(?i)^lfo", operand):
            return operand
        if re.match(r"(?i)^p\d+", operand):
            return f"t{self.track_number}{operand}"
        return operand

    def _cp_rm_paths(self, text):
        """Make cp/rm operands absolute relative to this track menu."""
        parts = text.split()
        if len(parts) < 2:
            return text
        if parts[0].lower() == "rm":
            return " ".join([parts[0], self._abs_operand(parts[1])] + parts[2:])
        return " ".join([parts[0]] + [self._abs_operand(p) for p in parts[1:]])

    def _switch_track(self, n):
        self.track_number = n
        self.prompt = f"seq:t{n}> "

    def _pattern_shell(self, pattern):
        return PatternShell(self, pattern)

    def _handle_path(self, path, rest):
        """Apply a relative path command typed inside this track."""
        if path["pattern"] is None:
            raise ValueError("a pattern is required (use p<m> or t<n>p<m>)")
        pattern = path["pattern"]
        kind, index = path["kind"], path["index"]
        if rest:
            first = rest.split()[0]
            m = re.fullmatch(r"(?i)([sfv])(\d+)", first)
            if kind is None and m:
                kind, index = m.group(1).lower(), int(m.group(2))
                rest = rest[len(first):].strip()
            elif kind is None and re.fullmatch(r"(?i)sus(\d+)", first):
                kind = "u"
                index = int(re.fullmatch(r"(?i)sus(\d+)", first).group(1))
                rest = rest[len(first):].strip()
            elif kind is None and re.fullmatch(r"(?i)mt(\d+)", first):
                kind = "m"
                index = int(re.fullmatch(r"(?i)mt(\d+)", first).group(1))
                rest = rest[len(first):].strip()
        if kind is None and rest:
            return PatternShell(self, pattern)._edit_verb(rest)
        shell = self._pattern_shell(pattern)
        if kind == "s":
            shell.set_sequence(index, rest.split(), via_path=True)
        elif kind == "f":
            shell.set_phrase(index, rest, via_path=True)
        elif kind == "v":
            shell.set_velocity(index, rest, via_path=True)
        elif kind == "u":
            shell.set_sustain(index, rest, via_path=True)
        elif kind == "m":
            shell.set_microtime(index, rest, via_path=True)
        else:
            raise ValueError("nothing to do: give s<k>, f<k>, v<k>, sus<k>, "
                             "mt<k> or scale/root/bpm/division/channel")

    def do_start(self, arg):
        """Play a phrase or loop order: start p<m>f<k> / p<m>o<k> (paths work too)"""
        try:
            path = parse_compact_path(arg, track=self.track_number)
            if path["kind"] not in ("f", "o"):
                raise ValueError("use start p<m>f<k> (phrase, e.g. start p1f1) "
                                 "or p<m>o<k> (loop order, e.g. start p1o0)")
        except ValueError as e:
            print(f"Error: {e}")
            return
        self._root_shell().launch(path["track"], path["pattern"],
                                  path["kind"], path["index"])

    def do_stop(self, arg):
        """Stop a phrase or loop order of this track (paths work too)"""
        try:
            path = parse_compact_path(arg, track=self.track_number)
            if path["kind"] not in ("f", "o"):
                raise ValueError("use stop p<m>f<k> (phrase) or p<m>o<k> "
                                 "(loop order)")
        except ValueError as e:
            print(f"Error: {e}")
            return
        self._root_shell().stop_target(path["track"], path["pattern"],
                                       path["kind"], path["index"])

    def do_panic(self, arg):
        "Stop everything (phrases, loops) and silence MIDI/audio outputs"
        self._root_shell().panic()

    def do_help(self, arg):
        if arg:
            return super().do_help(arg.strip().replace("-", "_"))
        print(f"\nTrack {self.track_number} Commands:")
        print(f"  {'p<m>':<18} - enter the menu of a pattern (m = 1-16)")
        print(f"  {'p<m>s<k> [deg...]':<18} - set a sequence of that pattern (s0..s9)")
        print(f"  {'p<m>f<k> <expr>':<18} - define a phrase of that pattern (f0..f7)")
        print(f"  {'p<m> scale|root|bpm|channel':<18} - edit that pattern setting")
        print(f"  {'p<m> division 1/16':<18} - step note value (1, 1/2, 1/3, 1/16 ...)")
        print(f"  {'p<m> type note|CC':<18} - pattern kind (default note)")
        print(f"  {'p<m> controller <n>':<18} - CC number 0-127 for a CC pattern")
        print(f"  {'show p<m>[s<k>|f<k>]':<18} - show a pattern/sequence/phrase")
        print(f"  {'start p<m>f<k>':<18} - play a phrase of this track")
        print(f"  {'stop p<m>f<k>':<18} - stop a phrase of this track")
        print(f"  {'t<n>':<18} - switch to another track menu")
        print(f"  {'cp <src> <dst>':<18} - copy a track/pattern/leaf, relative: cp p1 p2")
        print(f"  {'rm <path> [setting]':<18} - delete a leaf/setting, relative: rm p1, rm p3s0")
        print(f"  {'panic':<18} - stop all phrases and silence all MIDI outputs")
        print(f"  {'/':<18} - return to the main menu from anywhere")
        print(f"  {'/ <command>':<18} - run a main-menu command, staying in this menu")
        print(f"  {'help':<18} - show this help")
        print(f"  {'exit':<18} - return to the main menu (playback keeps running)")
        print(f"\n  Current track: {self.track_number}")
        print()

    def do_exit(self, arg):
        "Return to the main menu"
        return True

    def do_EOF(self, arg):
        print()
        return True

    def emptyline(self):
        pass

    def default(self, line):
        text = line.strip()
        parts = text.split()
        if not parts:
            return
        if text.startswith("/"):
            command = text[1:].strip()
            if not command:
                self.request_root()
                return True
            # '/ <root command>' runs at the root; the menu stays put.
            self.run_root_command(command)
            return
        token, rest = parts[0], text[len(parts[0]):].strip()
        if token.lower().startswith("lfo"):  # jump to an LFO menu from here
            self._root_shell().onecmd(text)
            return True if self.propagate_root_request() else None
        if token.lower() in ("cp", "rm"):  # copy / remove by path
            self._root_shell().onecmd(self._cp_rm_paths(text))
            return
        # 'show ...' is handled by the root; make it relative to this track.
        if token.lower() == "show":
            target = rest or ""
            if target and not target.lower().startswith("t"):
                target = f"t{self.track_number}{target}"
            elif not target:
                target = f"t{self.track_number}"
            self._root_shell().do_show(target)
            return
        try:
            path = parse_compact_path(token, track=self.track_number)
        except ValueError as e:
            if _PATH_TOKEN_RE.fullmatch(token):
                print(f"Error: {e}")
                return
            hint = old_command_hint(token)
            print(f"*** Unknown command: {line}")
            if hint:
                print(f"    Hint: {hint}")
            return
        if path["pattern"] is None:  # t<n> alone: switch track
            self._switch_track(path["track"])
            print(f"Track context set to: {self.track_number}")
            return
        if path["track"] != self.track_number:  # jumping to another track/pattern
            self.parent_shell.run_navigation(token, rest)
            if self.propagate_root_request():
                return True
            return
        if path["kind"] is None and not rest:  # p<m> alone: enter pattern
            PatternShell(self, path["pattern"]).cmdloop()
            if self.propagate_root_request():
                return True
            return
        try:
            self._handle_path(path, rest)
        except ValueError as e:
            print(f"Error: {e}")



class LfoShell(SeqCompletingCmd):
    """Context sub-menu of one global LFO definition (n = 1-8)."""

    def __init__(self, parent, lfo_number):
        super().__init__()
        self.parent_shell = parent  # the root SeqShell
        self.lfo_number = lfo_number
        self.menu_path = parent.menu_path + ["lfo", str(lfo_number)]
        self.prompt = make_prompt(self.menu_path)
        self._load_from_store()
        self._live_thread = None   # background renderer (readline environments)
        self._live_active = False  # tick renderer (built-in editor)
        self._live_prefix = ""
        self._live_t0 = 0.0
        self._live_last = 0.0
        initialize_readline()

    def _entry(self):
        """The project-store dict for this LFO (created on demand)."""
        root = self.parent_shell
        lfos = root.project.setdefault("lfos", {})
        return lfos.setdefault(self.lfo_number, {})

    def precmd(self, line):
        """Re-read this LFO before each command (its values may have changed)."""
        self._load_from_store()
        return line

    def _load_from_store(self):
        entry = self.parent_shell.project.get("lfos", {}).get(self.lfo_number, {})
        self.frequency = entry.get("frequency", LFO_DEFAULTS["frequency"])
        self.shape = entry.get("shape", LFO_DEFAULTS["shape"])
        self.phase = entry.get("phase", LFO_DEFAULTS["phase"])
        self.running = entry.get("running", False)

    def _commit(self):
        entry = self._entry()
        entry["frequency"] = self.frequency
        entry["shape"] = self.shape
        entry["phase"] = self.phase
        entry["running"] = self.running

    def do_start(self, arg):
        "Start this LFO (no argument) or play a phrase by path (start t1p2f1)"
        text = (arg or "").strip()
        if text:  # e.g. 'start t1p2f1' typed from the LFO menu
            self.parent_shell.onecmd(f"start {text}")
            return
        if self.running:
            print(f"LFO {self.lfo_number} is already running.")
            return
        # The root latches the start to the next main-BPM beat.
        self.parent_shell._lfo_set_running(self.lfo_number, True)
        self.running = True

    def do_stop(self, arg):
        "Stop this LFO (no argument) or a phrase by path (stop t1p2f1)"
        text = (arg or "").strip()
        if text:  # e.g. 'stop t1p2f1' typed from the LFO menu
            self.parent_shell.onecmd(f"stop {text}")
            return
        if not self.running:
            print(f"LFO {self.lfo_number} is already stopped.")
            return
        self.parent_shell._lfo_set_running(self.lfo_number, False)
        self.running = False

    def _running_text(self):
        return "running" if self.running else "stopped"

    # ------------------------------------------------------------------
    # Live visualization of the instantaneous normalized LFO value
    # ------------------------------------------------------------------
    def _meter(self, v, unipolar=False):
        """Single-line level meter: centered, or left-anchored for ramp."""
        half = 12
        cells = ["\u00b7"] * (2 * half + 1)  # midpoint dots
        if unipolar:  # ramp: 0..1, drawn like a progress bar
            vv = max(0.0, min(1.0, v))
            level = int(vv * (2 * half) + 0.5)
            for i in range(level):
                cells[i] = "#"
            if level < len(cells):
                cells[level] = "|"
            return "[" + "".join(cells) + "]"
        cells[half] = "|"
        vv = max(-1.0, min(1.0, v))
        level = int(abs(vv) * half + 0.5)
        if vv >= 0:
            for i in range(1, level + 1):
                cells[half + i] = "#"
        else:
            for i in range(1, level + 1):
                cells[half - i] = "#"
        return "[" + "".join(cells) + "]"

    def _live_value(self, _unused=None):
        """Current normalized value of this LFO (0 when it is stopped).

        Time is absolute (monotonic) so the beat-aligned start latch works; the
        argument is ignored and kept for call-site compatibility.
        """
        if not self.running:
            return 0.0
        lfos = self.parent_shell.project.setdefault("lfos", {})
        try:
            return lfo.evaluate(lfos, self.lfo_number, time.monotonic(),
                                 getattr(self.parent_shell, "bpm", DEFAULT_BPM))
        except ValueError as e:
            self.error_text = str(e)
            return 0.0

    def _idle_tick(self):
        """Called by the input editor while idle: refresh the live prefix.

        Returns True only when the displayed value actually changed, so an
        unchanged meter (e.g. a stopped LFO) is not redrawn.
        """
        if not getattr(self, "_live_active", False):
            return False
        now = time.time()
        if now - getattr(self, "_live_last", 0.0) < 1 / LFO_LIVE_FPS:
            return False
        self._live_last = now
        v = self._live_value(self._live_t0)
        prefix = f"{self._meter(v, self.shape == 'ramp')} {v:+.2f}  "
        if prefix == self._live_prefix:
            return False
        self._live_prefix = prefix
        return True

    def _live_loop(self):
        """Fallback renderer used only when a readline input() is in charge."""
        start = time.time()
        try:
            while not self._live_stop.is_set():
                v = self._live_value(start)
                frame = (f"\r\x1b[2K"
                         f"{self._meter(v, self.shape == 'ramp')} "
                         f"{v:+.2f}  {self.prompt}")
                sys.stdout.write(frame)
                sys.stdout.flush()
                time.sleep(1 / LFO_LIVE_FPS)
        finally:
            sys.stdout.write("\r\x1b[2K")
            sys.stdout.flush()

    def _stop_live(self):
        self._live_active = False
        self._live_prefix = ""
        if self._live_thread is not None:
            self._live_stop.set()
            try:
                self._live_thread.join(timeout=0.5)
            except Exception:
                pass
            self._live_thread = None

    def _live_is_running(self):
        return bool(getattr(self, "_live_active", False)) or \
            self._live_thread is not None

    def do_live(self, arg):
        "Show the live instantaneous LFO value next to the prompt (live stop ends it)"
        text = (arg or "").strip().lower()
        if text:
            if text != "stop":
                print("Usage: live            start the live view")
                print("       live stop       stop the live view")
                return
            if not self._live_is_running():
                print("The live view is not running.")
                return
            self._stop_live()
            print("Live view stopped.")
            return
        if self._live_is_running():
            print("The live view is already running.")
            return
        if not sys.stdout.isatty():
            print("Live view requires an interactive terminal.")
            return
        _enable_ansi()
        self._live_prefix = ""
        self._live_t0 = time.time()
        self._live_last = 0.0
        if HAVE_READLINE:
            # input() owns the line: a background renderer is the only option.
            self._live_stop = threading.Event()
            self._live_thread = threading.Thread(target=self._live_loop, daemon=True)
            self._live_thread.start()
        else:
            # The built-in editor calls _idle_tick() and keeps typed text visible.
            self._live_thread = None
            self._live_active = True
        print("Live LFO visualization started (type 'live stop' to end).")

    def _check_modulation(self, ref):
        """Reject self-references and cycles when modulating from another LFO."""
        if ref == self.lfo_number:
            raise ValueError(f"LFO {self.lfo_number} cannot modulate itself.")
        if not 1 <= ref <= MAX_LFOS:
            raise ValueError(f"LFO reference must be between 1 and {MAX_LFOS}.")
        # Walk the existing modulation graph from <ref>; if it reaches this
        # LFO the new reference would create a cycle.
        lfos = self.parent_shell.project.setdefault("lfos", {})
        stack = [ref]
        seen = {ref}
        while stack:
            cur = stack.pop()
            if cur == self.lfo_number:
                raise ValueError(f"that reference would create a cycle: "
                                 f"LFO {ref} depends on LFO {self.lfo_number}.")
            entry = lfos.get(cur, {}) or {}
            for param in ("frequency",):
                try:
                    kind = lfo.parse_param(entry.get(param, 1.0))
                except ValueError:
                    continue
                if kind[0] == "lfo" and kind[2] not in seen:
                    seen.add(kind[2])
                    stack.append(kind[2])

    def _set_param(self, text):
        """Validate and store the frequency parameter.

        Accepts a number or '<coef>*lfo<n>' (also plain 'lfo<n>' = 1*lfo<n>).
        Returns the canonical stored value (float constant or expression text)
        or prints the error and returns None.
        """
        text = (text or "").strip()
        try:
            kind = lfo.parse_param(text)
        except ValueError as e:
            print(f"Error: {e}")
            return None
        if kind[0] == "bpm":
            factor = kind[1]
            if not (factor > 0 and factor == factor and abs(factor) != float("inf")):
                print("Error: the bpm multiple must be a positive finite number.")
                return None
            return f"{lfo.factor_to_text(factor)}bpm"
        if kind[0] == "lfo":
            coef, ref = kind[1], kind[2]
            try:
                if not (coef > 0 and coef == coef and abs(coef) != float("inf")):
                    raise ValueError("frequency modulation coefficient must be "
                                     "a positive finite number")
                self._check_modulation(ref)
            except ValueError as e:
                print(f"Error: {e}")
                return None
            return f"{coef:g}*lfo{ref}"
        value = kind[1]
        if not (value > 0 and value == value and abs(value) != float("inf")):
            print("Error: frequency must be a positive finite number of Hertz.")
            return None
        return value

    def do_frequency(self, arg):
        "Show or set the LFO frequency: Hertz, <coef>*lfo<n> or <k>bpm / 1/<k>bpm"
        bpm = getattr(self.parent_shell, "bpm", DEFAULT_BPM)
        text = (arg or "").strip()
        if not text:
            print(f"Frequency: {lfo.describe_frequency(self.frequency, bpm)}")
            return
        value = self._set_param(text)
        if value is None:
            return
        self.frequency = value
        self._commit()
        print(f"Frequency set to {lfo.describe_frequency(value, bpm)}")

    def do_shape(self, arg):
        "Show or set the LFO shape: sin, tri, saw or square"
        name = (arg or "").strip().lower()
        if not name:
            print(f"Shape: {self.shape}")
            return
        if name == "?":
            print("Available shapes: " + ", ".join(LFO_SHAPES))
            return
        if name not in LFO_SHAPES:
            print(f"Error: unknown shape '{name}'. Available shapes: "
                  f"{', '.join(LFO_SHAPES)}.")
            return
        self.shape = name
        self._commit()
        print(f"Shape set to {name}")

    def complete_shape(self, text, line, begidx, endidx):
        return [s for s in LFO_SHAPES if s.startswith(text.lower())]

    def do_phase(self, arg):
        "Show or set the LFO phase (start point): a value between -1 and 1"
        text = (arg or "").strip()
        if not text:
            print(f"Phase: {self.phase:g}")
            return
        try:
            value = float(text)
        except ValueError:
            print(f"Error: invalid phase '{text}'.")
            return
        if not -1.0 <= value <= 1.0:
            print("Error: phase must be between -1 and 1.")
            return
        self.phase = value
        self._commit()
        print(f"Phase set to {value:g}")

    def do_rm(self, arg):
        "Reset this LFO (or another: rm lfo2) to its default values"
        text = (arg or "").strip()
        if not text:
            self.parent_shell.reset_lfo(self.lfo_number)
            self._load_from_store()
            return
        if text.lower() in ("frequency", "freq", "shape", "phase", "running"):
            self.parent_shell.reset_lfo_param(self.lfo_number, text)
            self._load_from_store()
            return
        m = re.fullmatch(r"(?i)lfo\s*(\d+)?", text)
        if m and m.group(1):
            n = int(m.group(1))
            if not 1 <= n <= MAX_LFOS:
                print(f"Error: LFO number must be between 1 and {MAX_LFOS}.")
                return
            self.parent_shell.reset_lfo(n)
            if n == self.lfo_number:
                self._load_from_store()
            return
        if m:  # 'rm lfo' -> all
            for n in range(1, MAX_LFOS + 1):
                self.parent_shell.reset_lfo(n, announce=False)
            print(f"Reset all {MAX_LFOS} LFOs to their defaults (stopped).")
            self._load_from_store()
            return
        self.parent_shell.onecmd("rm " + text)

    def do_show(self, arg):
        "Show all parameters of this LFO"
        print(f"\nLFO {self.lfo_number} parameters:")
        print(f"  state:     {self._running_text()}")
        print(f"  frequency: "
              f"{lfo.describe_frequency(self.frequency, getattr(self.parent_shell, 'bpm', DEFAULT_BPM))}")
        print(f"  shape:     {self.shape}")
        print(f"  phase:     {self.phase:g}")
        print()

    def do_panic(self, arg):
        "Stop everything (phrases, loops) and silence MIDI/audio outputs"
        self.parent_shell.panic()

    def do_help(self, arg):
        if arg:
            return super().do_help(arg.strip().replace("-", "_"))
        print(f"\nLFO {self.lfo_number} Commands:")
        print(f"  {'frequency [<hz>|expr]':<22} - Hz or a tempo multiple, e.g. 0.5, 2bpm, freq 2bpm")
        print(f"  {'shape [<name>]':<20} - sin, tri, saw, square, ramp (0..1), random")
        print(f"  {'phase [<-1..1>]':<20} - start point of the waveform (-1 to 1)")
        print(f"  {'show':<20} - show all parameters of this LFO")
        print(f"  {'rm [lfo<n>] [param]':<20} - reset this LFO (or another, or one parameter)")
        print(f"  {'start':<20} - start this LFO (stopped by default)")
        print(f"  {'stop':<20} - stop this LFO")
        print(f"  {'live':<20} - show the live instantaneous value next to the prompt")
        print(f"  {'live stop':<20} - stop the live view")
        print(f"  {'cp <src> <dst>':<20} - copy a track, pattern, leaf or LFO (cp lfo1 lfo2)")
        print(f"  {'rm <path>|lfo<n>':<20} - delete a track/pattern/sequence/phrase, reset an LFO or a setting")
        print(f"  {'panic':<20} - stop all playback (phrases, loops) and silence outputs")
        print(f"  {'/':<20} - return to the main menu from anywhere")
        print(f"  {'/ <command>':<20} - run a main-menu command, staying in this menu")
        print(f"  {'help':<20} - show this help")
        print(f"  {'exit':<20} - return to the main menu")
        print(f"\n  LFO {self.lfo_number}: {self._running_text()}, frequency "
              f"{lfo.describe_frequency(self.frequency, getattr(self.parent_shell, 'bpm', DEFAULT_BPM))}, "
              f"shape {self.shape}, phase {self.phase:g}")
        print()

    def do_exit(self, arg):
        "Return to the main menu"
        self._stop_live()
        return True

    def do_EOF(self, arg):
        self._stop_live()
        print()
        return True

    def emptyline(self):
        pass

    def default(self, line):
        text = line.strip()
        parts = text.split()
        if text.startswith("/"):
            command = text[1:].strip()
            if not command:
                self.request_root()
                return True
            self.parent_shell.run_root_command(command)
            return
        if parts:
            token = parts[0]
            rest = text[len(token):].strip()
            if token.lower().startswith("lfo"):  # jump to another LFO menu
                self.parent_shell.onecmd(text)
                return True if self.propagate_root_request() else None
            if token.lower() in ("start", "stop", "cp", "rm"):  # global commands
                self.parent_shell.onecmd(text)
                return True if self.propagate_root_request() else None
            # Allow jumping to any menu by path (e.g. t1p2) from here too.
            if re.fullmatch(r"(?i)(?:t\d+(?:p\d+(?:[sf]\d+)?)?|p\d+(?:[sf]\d+)?"
                            r"|[sf]\d+)", token):
                if self.parent_shell.run_navigation(token, rest):
                    return True if self.propagate_root_request() else None
                return
        hint = old_command_hint(parts[0]) if parts else None
        print(f"*** Unknown command: {line}")
        if hint:
            print(f"    Hint: {hint}")


class PatternShell(SeqCompletingCmd):
    """Context sub-menu of one pattern inside a track."""

    def __init__(self, parent, pattern_number):
        super().__init__()
        self.parent_shell = parent
        self.pattern_number = pattern_number
        self.prompt = f"seq:t{parent.track_number}p{pattern_number}> "
        self._player = None  # active phrase playback (player.Player) if any
        # Live histogram view state (kept across reloads, see precmd).
        self._live_block = []       # panel lines drawn below the prompt
        self._hist_phrase = None    # phrase (or sequence) index being shown
        self._hist_expr = None      # explicit expression override
        self._hist_label = None
        self._hist_cached = None
        self._hist_last = 0.0
        self._hist_spin = 0
        self._hist_spin_last = 0.0
        self._live_active = False
        self._load_from_store()
        initialize_readline()

    def _track_number(self):
        return self.parent_shell.track_number

    def _entry(self):
        """The project-store dict for this pattern (created on demand)."""
        root = self._root_shell()
        tracks = root.project.setdefault("tracks", {})
        tmap = tracks.setdefault(self._track_number(), {})
        return tmap.setdefault(self.pattern_number, {})

    def _apply_defaults(self):
        """Fresh pattern values: chromatic scale, C4 root, channel 1, no data."""
        self.scale = "chromatic"
        self.root = scales.parse_note("C4")
        self.seqs = {}  # seq index (0-9) -> list of (degree, octave_shift)
        self.phrases = {}  # phrase number (1-16) -> canonical expression text
        self.velocities = {}  # seq index (0-9) -> velocity expression text
        self.sustains = {}  # seq index (0-9) -> sustain expression text
        self.microtimes = {}  # seq index (0-9) -> microtiming expression text
        self.osc_server = None   # osc pattern: 'host:port' of scsynth
        self.osc_sclang = None   # osc pattern: 'host:port' of sclang
        self.osc_latency = None  # osc pattern: scheduling latency in seconds
        self.synth = None        # osc pattern: SynthDef name
        self.fx = {}             # osc pattern: fx name -> expression
        self.sample = None   # loop pattern: .wav file name inside samples/
        self.orders = {}     # loop pattern: o index (0-9) -> [position|expr|None]
        self.pattern_type = "note"  # "note", "cc" or "loop"
        self.controller = 1  # CC number used by cc patterns
        self.division = DEFAULT_DIVISIONS  # step = 1/division note
        self.channel = DEFAULT_CHANNEL
        # BPM defaults to the global value; 'bpm' in this menu overrides it locally.
        self.bpm = getattr(self._root_shell(), "bpm", DEFAULT_BPM)
        self.bpm_inherited = True
        # MIDI output port: None means inherit the global selection.
        self.out_port = None

    def precmd(self, line):
        """Re-read the pattern before each command (cp/rm/load may have run)."""
        self._load_from_store()
        return line

    def _load_from_store(self):
        """Load this pattern's stored values, or defaults if never configured."""
        self._apply_defaults()
        entry = self._entry()
        if entry.get("scale"):
            self.scale = entry["scale"]
        if entry.get("root"):
            self.root = scales.parse_note(entry["root"]) or self.root
        if entry.get("channel") is not None:
            self.channel = entry["channel"]
        if entry.get("bpm-inherited") is False:
            self.bpm = entry.get("bpm", self.bpm)
            self.bpm_inherited = False
        else:
            self.bpm = getattr(self._root_shell(), "bpm", DEFAULT_BPM)
        if entry.get("port"):
            self.out_port = entry["port"]
        self.seqs = dict(entry.get("seqs", {}))
        self.phrases = dict(entry.get("phrases", {}))
        self.velocities = dict(entry.get("velocities", {}))
        self.sustains = dict(entry.get("sustains", {}))
        self.microtimes = dict(entry.get("microtimes", {}))
        self.pattern_type = str(entry.get("type", "note")).lower()
        self.controller = int(entry.get("controller", 1))
        self.division = entry.get("division", DEFAULT_DIVISIONS)
        self.osc_server = entry.get("osc-server")
        self.osc_sclang = entry.get("osc-sclang")
        self.osc_latency = entry.get("osc-latency")
        self.synth = entry.get("synth")
        self.fx = dict(entry.get("fx", {}))
        self.sample = entry.get("sample")
        self.orders = dict(entry.get("orders", {}))

    def _commit(self):
        """Write the current pattern values back into the project store."""
        entry = self._entry()
        entry.clear()
        entry.update({
            "scale": self.scale,
            "root": self.root.name(),
            "channel": self.channel,
            "bpm": self.bpm,
            "bpm-inherited": self.bpm_inherited,
            "port": self.out_port,
            "seqs": dict(self.seqs),
            "phrases": dict(self.phrases),
            "velocities": dict(self.velocities),
            "sustains": dict(self.sustains),
            "microtimes": dict(self.microtimes),
            "type": self.pattern_type,
            "controller": self.controller,
            "division": self.division,
            "osc-server": self.osc_server,
            "osc-sclang": self.osc_sclang,
            "osc-latency": self.osc_latency,
            "synth": self.synth,
            "fx": dict(self.fx),
            "sample": self.sample,
            "orders": dict(self.orders),
        })

    def _switch_pattern(self, n):
        # Navigating between patterns does not stop playback: any phrase that
        # was started keeps playing in the background.
        if getattr(self, "_live_active", False):
            self._stop_hist_live(announce=False)  # panel belongs to this pattern
        self.pattern_number = n
        self.prompt = f"seq:t{self._track_number()}p{n}> "
        self._load_from_store()

    def _key_line(self):
        """One-line description of the current scale over the current root."""
        notes = scales.scale_notes(self.root, scales.SCALES[self.scale])
        return f"{self.scale} on {self.root.name()}: {' '.join(notes)}"

    def _live_hint(self):
        """Suffix shown when this pattern's phrase is currently playing."""
        root = self._root_shell()
        if any(k[0] == self._track_number() and k[1] == self.pattern_number
               and pl.running for k, pl in root.playback.items()):
            return " (playing: effective from the next cycle)"
        return ""

    # ------------------------------------------------------------------
    # Sequence / phrase definitions and path helpers
    # ------------------------------------------------------------------
    def set_sequence(self, index, tokens, via_path=False):
        """Set sequence s<index>; used by 's<k> ...' and path commands."""
        return self._set_seq(index, tokens)

    def set_phrase(self, index, expr_text, via_path=False):
        """Define phrase f<index>; used by 'f<k> <expr>' and path commands."""
        expr_text = (expr_text or "").strip()
        if not expr_text:
            if index in self.phrases:
                print(f"f{index}: {self.phrases[index]}")
            else:
                print(f"f{index}: not configured. "
                      f"Usage: f{index} <expression>  e.g. f{index} 4*(s0+2*s1)")
            return False
        try:
            canonical = phrases.parse_phrase_expr(expr_text)
            refs = phrases.unit_refs(canonical)
        except phrases.PhraseError as e:
            print(f"Error: {e}")
            return False
        if self.pattern_type == "loop":
            bad = [f"s{i}" for kind, i in refs if kind == "seq"]
            if bad:
                print(f"Error: a loop pattern arrangement uses order lists, "
                      f"not sequences ({', '.join(bad)}). "
                      f"Write e.g. f{index} inf*o0")
                return False
        else:
            bad = [f"o{i}" for kind, i in refs if kind == "order"]
            if bad:
                print(f"Error: a '{self.pattern_type}' pattern uses sequences, "
                      f"not order lists ({', '.join(bad)}). "
                      f"Write e.g. f{index} inf*s0")
                return False
        self.phrases[index] = canonical
        self._commit()
        print(f"f{index} set: {canonical}{self._live_hint()}")
        return True

    def _abs_operand(self, operand):
        """Resolve a relative operand: 's0' -> 't1p1s0', 'p2' -> 't1p2'."""
        if not operand or operand.startswith("/"):
            return operand
        if re.match(r"(?i)^t\d+", operand) or re.match(r"(?i)^lfo", operand):
            return operand
        base = f"t{self._track_number()}"
        if re.match(r"(?i)^p\d+", operand):
            return base + operand
        if re.match(r"(?i)^(?:s|f|v|sus|mt)\d+", operand):
            return f"{base}p{self.pattern_number}{operand}"
        return operand

    def _cp_rm_paths(self, text):
        """Make cp/rm operands absolute relative to this pattern menu."""
        parts = text.split()
        if len(parts) < 2:
            return text
        if parts[0].lower() == "rm":
            settings = ("scale", "root", "bpm", "channel", "division", "type",
                        "controller", "port", "select-port")
            if parts[1].lower() in settings:
                # 'rm division' means this pattern's division, keep the word
                return " ".join([parts[0], f"t{self._track_number()}"
                                 f"p{self.pattern_number}"] + parts[1:])
            return " ".join([parts[0], self._abs_operand(parts[1])] + parts[2:])
        return " ".join([parts[0]] + [self._abs_operand(p) for p in parts[1:]])

    def _hist_target(self, arg):
        """Pick what to render: (index, expr, label).

        Explicit 'f<k>' (phrase) or 's<k>'/'o<k>' (one sequence / order list);
        otherwise whatever is playing for this pattern, else the lowest
        configured phrase, else the lowest configured sequence / order list.
        """
        text = (arg or "").strip()
        if self.pattern_type == "loop":
            m = re.fullmatch(r"(?i)o(\d+)", text)
            if m:
                n = int(m.group(1))
                if n not in self.orders:
                    return None, None, f"o{n} is not configured in this pattern."
                return n, f"inf*o{n}", f"o{n}"
            m = re.fullmatch(r"(?i)f(\d+)", text)
            if m:
                return int(m.group(1)), None, None
            playing = [key[2] for key, pl in
                       self._root_shell().playback.items()
                       if key[0] == self._track_number()
                       and key[1] == self.pattern_number and pl.running
                       and len(key) == 3]
            if playing:
                return playing[0], None, None
            if self.phrases:
                return min(self.phrases), None, None
            if self.orders:
                n = min(self.orders)
                return n, f"inf*o{n}", f"o{n} (no phrase configured)"
            return None, None, None
        m = re.fullmatch(r"(?i)f(\d+)", text)
        if m:
            return int(m.group(1)), None, None
        m = re.fullmatch(r"(?i)s(\d+)", text)
        if m:
            n = int(m.group(1))
            seq = self.seqs.get(n)
            if seq is None:
                return None, None, f"seq{n} is not configured in this pattern."
            return n, f"inf*s{n}", f"seq{n}"
        root = self._root_shell()
        playing = [key[2] for key, pl in root.playback.items()
                   if key[0] == self._track_number()
                   and key[1] == self.pattern_number and pl.running]
        if playing:
            return playing[0], None, None
        if self.phrases:
            return min(self.phrases), None, None
        if self.seqs:
            n = min(self.seqs)
            return n, f"inf*s{n}", f"seq{n} (no phrase configured)"
        return None, None, None

    def _hist_view(self):
        root = self._root_shell()
        return root._pattern_view(self._track_number(), self.pattern_number)

    def _hist_playhead(self):
        """Step index currently sounding for this pattern (None when stopped)."""
        for key, pl in self._root_shell().playback.items():
            if (key[0] == self._track_number() and key[1] == self.pattern_number
                    and pl.running):
                return pl.position
        return None

    def _hist_lines(self, phrase_index, live=False, expr=None, label=None,
                    spin=0):
        root = self._root_shell()
        lfos = root.project.get("lfos", {})
        view = self._hist_view()
        if self.pattern_type == "loop":
            if expr is None:
                expr = self.phrases.get(phrase_index)
            if expr is None:
                if phrase_index is None:
                    return []
                return [f"f{phrase_index} is not configured in this pattern "
                        f"(hist f<k>|o<k> to pick another)."]
            if label is None and phrase_index is not None:
                label = f"f{phrase_index} ({expr})"
            return loop_histogram_lines(view, lfos, self.bpm,
                                        time.monotonic(), expr=expr,
                                        label=label, live=live,
                                        playhead=self._hist_playhead(),
                                        spin=spin)
        return histogram_lines(view, lfos,
                               self.bpm, time.monotonic(), phrase_index,
                               live=live, expr=expr, label=label,
                               playhead=self._hist_playhead(), spin=spin)

    def _sample_seconds(self):
        """Length of the selected sample in seconds, or None."""
        path, err = sampler.resolve_sample(self.sample)
        if err:
            return None
        return sampler.sample_seconds(path)

    def _bars_text(self, steps):
        """How a step count sits in 4/4 bars at this division ('' if unknown)."""
        return bars_text(steps, self.division)

    def _sample_line(self):
        """Human-readable summary of the selected sample."""
        if not self.sample:
            found = sampler.list_samples()
            extra = (f"  Available: {', '.join(found)}" if found
                     else "  (the samples folder is empty)")
            return f"no sample selected  [{sampler.samples_dir()}]{extra}"
        path, err = sampler.resolve_sample(self.sample)
        if err:
            return f"{self.sample} (missing: {err})"
        seconds = sampler.sample_seconds(path)
        return f"{self.sample}  ({seconds:.2f} s, {sampler.samples_dir()})"

    def do_sample(self, arg):
        "Show or set the .wav file of a loop pattern (looked up in samples/)"
        text = (arg or "").strip()
        if not text:
            print(f"Sample: {self._sample_line()}")
            if self.pattern_type != "loop":
                print(f"  (this pattern is type '{self.pattern_type}'; "
                      f"'type loop' makes it play the sample)")
            if self.sample:
                print(f"  Positions are normalised to the sample length: "
                      f"'o0 0.2 0.3 0.6' starts from 20 %, 30 % and 60 % "
                      f"(negatives wrap: -0.2 = 0.8).")
            return
        path, err = sampler.resolve_sample(text)
        if err:
            print(f"Error: {err}")
            return
        try:
            sample = sampler.load_sample(path)   # validate it can be played
        except (ValueError, OSError) as e:
            print(f"Error: {e}")
            return
        self.sample = os.path.basename(path)
        self._commit()
        print(f"Sample set to {self.sample} ({sampler.samples_dir()})")
        print(f"  Length {sample.duration_text()}.")
        if self.pattern_type != "loop":
            print(f"  Note: this pattern is type '{self.pattern_type}'; use "
                  f"'type loop' to play it.")

    def set_order(self, index, tokens):
        """Set order list o<index>: positions in the sample, or 'r' (rest).

        The sample length is 1, so '0.2' means "start at 20 %". An entry may be
        an expression with LFOs ('lfo1', '0.3+0.1*lfo1'); it is evaluated at
        every step and wrapped modulo 1, so negative values work too.
        """
        if not tokens:
            self.show_order(index)
            return False
        order = []
        for tok in tokens:
            low = tok.lower()
            if low == "r":
                order.append(None)
                continue
            try:
                constant, terms = parse_seq_expr(low)
            except ValueError as e:
                print(f"Error: invalid position '{tok}': {e}")
                print(f"  Use a position 0..1 (e.g. 0.2, 0.75, -0.1), 'r' for "
                      f"a rest, or an LFO expression like lfo1 or 0.5*lfo2.")
                return False
            if not terms:
                order.append(float(constant) % 1.0)
                continue
            for _coef, ref in variation_terms(terms):
                if not 1 <= ref <= MAX_LFOS:
                    print(f"Error: LFO reference lfo{ref} must be between 1 "
                          f"and {MAX_LFOS}.")
                    return False
            order.append(parse_seq_canonical(low))
        self.orders[index] = order
        self._commit()
        step_ms = division_step_ms(self.bpm, self.division)
        bars = self._bars_text(len(order))
        bars_txt = f", {bars}" if bars else ""
        print(f"o{index} set: {order_to_text(order)}   "
              f"({len(order)} steps x {step_ms:g} ms = "
              f"{len(order) * step_ms:g} ms per pass{bars_txt}; positions in "
              f"the sample)")
        now = self._order_now(order)
        if order_has_lfo(order):
            print(f"  LFO entries set the position at each step; now: {now}")
            lfos = self._root_shell().project.get("lfos", {})
            refs = sorted({ref for item in order if isinstance(item, str)
                           for _c, ref in variation_terms(
                               parse_seq_expr(item)[1])})
            stopped = [r for r in refs
                       if not (lfos.get(r) or {}).get("running")]
            if stopped:
                print(f"  Note: {', '.join(f'lfo{r}' for r in stopped)} "
                      f"stopped -> contributes 0 (position 0 here).")
        outside = [item for item in order
                   if isinstance(item, float) and not 0.0 <= item <= 1.0]
        if outside:
            print(f"  Note: positions are wrapped modulo 1 "
                  f"({', '.join(f'{v:g} -> {v % 1.0:g}' for v in outside)}).")
        return True

    def _order_now(self, order):
        """Resolve an order list at this instant, for display."""
        lfos = self._root_shell().project.get("lfos", {})
        out = []
        for item in order:
            if item is None:
                out.append("r")
            else:
                value = order_entry_value(item, lfos, self.bpm,
                                          time.monotonic())
                out.append("--" if value is None else f"{value:.2f}")
        return " ".join(out)

    def show_order(self, n):
        """Print one stored order list."""
        if n in self.orders:
            order = self.orders[n]
            step_ms = division_step_ms(self.bpm, self.division)
            extra = ""
            if order_has_lfo(order):
                extra = f"   -> now {self._order_now(order)}"
            print(f"o{n}: {order_to_text(order)}   "
                  f"({len(order)} steps = {len(order) * step_ms:g} ms)"
                  f"{extra}")
        else:
            print(f"o{n}: not configured. Usage: o{n} <position ...>  "
                  f"e.g. o{n} 0 0.25 0.5 (the sample length is 1), or an LFO "
                  f"entry like o{n} 0.2 lfo1 ('r' = rest)")

    def do_list_o(self, arg):
        "List the loop orders of this pattern (o0..o9) and the sample"
        step_ms = division_step_ms(self.bpm, self.division)
        print(f"\nLoop (pattern {self.pattern_number}, "
              f"step {step_ms:g} ms at division "
              f"{division_to_text(self.division)}, bpm {self.bpm}):")
        print(f"  sample: {self._sample_line()}")
        if not self.orders:
            print("  (no order list configured - use e.g. 'o0 0 0.25 0.5')")
            print()
            return
        for k in sorted(self.orders):
            order = self.orders[k]
            bars = self._bars_text(len(order))
            bars_txt = f", {bars}" if bars else ""
            extra = ""
            if order_has_lfo(order):
                extra = f"   -> now {self._order_now(order)}"
            print(f"  o{k}: {order_to_text(order):<20} -> {len(order)} steps = "
                  f"{len(order) * step_ms:g} ms per pass{bars_txt}{extra}")
        print()

    def do_hist(self, arg):
        """Per-step histogram: note/CC values, or sample positions for a loop

        Usage: hist [f<k>|s<k>|o<k>] [live|once|stop]
        """
        is_loop = self.pattern_type == "loop"
        text = (arg or "").strip().lower()
        if text.startswith("live "):
            text = text[5:].strip()  # accept 'live stop' / 'live off'
        if text in ("stop", "off"):
            if not getattr(self, "_live_block", None):
                print("The histogram live view is not running.")
                return
            self._stop_hist_live(announce=True)
            return
        live = False
        if text.endswith("live"):
            live = True
            text = text[:-4].strip()
        elif text.endswith("once") or text.endswith("static"):
            text = text.rsplit(None, 1)[0] if " " in text else ""
        phrase, expr, label = self._hist_target(text)
        if phrase is None:
            if label:
                print(label)
            else:
                print("Nothing to show: no phrase or sequence is configured "
                      "in this pattern.")
            return
        usage = (r"(?i)[fs]\d+" if not is_loop else r"(?i)[fo]\d+")
        if text and not re.fullmatch(usage, text):
            example = "hist o0" if is_loop else "hist f1"
            print(f"Usage: hist [f<k>|{'o' if is_loop else 's'}<k>] "
                  f"[live|stop]   e.g. hist, {example} live")
            return
        lines = self._hist_lines(phrase, live=live, expr=expr, label=label)
        if live:
            if not sys.stdout.isatty():
                print("Live histogram requires an interactive terminal.")
                print("\n".join(lines))
                return
            _enable_ansi()
            self._hist_phrase = phrase if phrase is not None else 0
            self._hist_expr = expr
            self._hist_label = label
            self._hist_cached = None
            self._live_block = []
            self._live_active = True
            self._hist_tick()
            print("Histogram live view started (hist stop to end).")
            return
        print("\n".join(lines))

    def _hist_tick(self):
        """Refresh the histogram block (called by the input editor while idle).

        Repaints when any value changes; the spinner spins at ~4 fps so the
        view is visibly alive even when every value is static.
        """
        if not getattr(self, "_live_active", False):
            return False
        if getattr(self, "_hist_phrase", None) is None:
            return False  # nothing to show (e.g. the phrase was removed)
        now = time.monotonic()
        if now - getattr(self, "_hist_last", 0.0) < 1 / 10.0:
            return False
        self._hist_last = now
        if now - getattr(self, "_hist_spin_last", 0.0) >= 0.25:
            # Spin slowly: proves the view is live without repainting too often.
            self._hist_spin_last = now
            self._hist_spin = (getattr(self, "_hist_spin", 0) + 1) % 4
        lines = self._hist_lines(self._hist_phrase, live=True,
                                 expr=getattr(self, "_hist_expr", None),
                                 label=getattr(self, "_hist_label", None),
                                 spin=self._hist_spin)
        if lines == getattr(self, "_hist_cached", None):
            return False
        self._hist_cached = lines
        self._live_block = lines
        return True

    def _idle_tick(self):
        return self._hist_tick()

    def _stop_hist_live(self, announce=False):
        lines = getattr(self, "_live_block", None) or []
        paint = getattr(self, "_live_paint", None)
        if paint is not None:
            paint([])  # erase the panel area below the input line
        elif lines:
            # Fallback: erase a block drawn above the prompt line.
            sys.stdout.write(f"\x1b[{len(lines)}A\x1b[J")
            sys.stdout.flush()
        self._live_block = []
        self._hist_cached = None
        self._live_active = False
        if announce:
            print("Histogram live view stopped.")



    # ----------------------------------------------------------------- osc --
    def _osc_engine(self):
        """SoundEngine for this pattern's targets (created on demand)."""
        server = (supercollider.parse_target(
            self.osc_server, supercollider.DEFAULT_SERVER_PORT)
            if self.osc_server else supercollider.server_target())
        sclang = (supercollider.parse_target(
            self.osc_sclang, supercollider.DEFAULT_SCLANG_PORT)
            if self.osc_sclang else supercollider.sclang_target())
        return supercollider.engine_for(server, sclang)

    def _fx_text(self):
        if not self.fx:
            return "no fx parameters"
        return ", ".join(f"{k}={v}" for k, v in sorted(self.fx.items()))

    def do_osc(self, arg):
        "Show or set the SuperCollider server target of an osc pattern"
        text = (arg or "").strip()
        if not text:
            engine = self._osc_engine()
            print(f"OSC: {'signal to ' + engine.target_text if self.osc_server else 'default target'}"
                  f"{' (default 127.0.0.1:57110)' if not self.osc_server else ''}")
            print(f"  synthdefs to sclang at "
                  f"{self.osc_sclang or '127.0.0.1:57120 (default)'}")
            print(f"  synth '{self.synth or supercollider.DEFAULT_SYNTH}'"
                  f"   fx {self._fx_text()}")
            if self.pattern_type != "osc":
                print(f"  (this pattern is type '{self.pattern_type}'; "
                      f"'type osc' makes it send SuperCollider notes)")
            print("  Usage: osc [<host>:<port>]   e.g. osc 127.0.0.1:57110")
            return
        try:
            host, port = supercollider.parse_target(
                text, supercollider.DEFAULT_SERVER_PORT)
        except ValueError as e:
            print(f"Error: {e}")
            return
        self.osc_server = f"{host}:{port}"
        self._commit()
        print(f"OSC target set to {self.osc_server} (scsynth). Notes go there "
              f"with 'synth {self.synth or supercollider.DEFAULT_SYNTH}'.")

    def do_sclang(self, arg):
        "Show or set the sclang target used to install SynthDefs"
        text = (arg or "").strip()
        if not text:
            print(f"sclang: {self.osc_sclang or '127.0.0.1:57120 (default)'}"
                  f"   (responder '/seqd' from {supercollider.HELPER_FILE})")
            print("  Usage: sclang [<host>:<port>]")
            return
        try:
            host, port = supercollider.parse_target(
                text, supercollider.DEFAULT_SCLANG_PORT)
        except ValueError as e:
            print(f"Error: {e}")
            return
        self.osc_sclang = f"{host}:{port}"
        self._commit()
        print(f"sclang target set to {self.osc_sclang} (SynthDefs via /seqd).")

    def do_synth(self, arg):
        "Show or set the SynthDef name an osc pattern plays"
        text = (arg or "").strip()
        if not text:
            current = self.synth or supercollider.DEFAULT_SYNTH
            described = supercollider.catalogue_description(current)
            print(f"synth: {current}"
                  f"{('   (' + described + ')') if described else ''}")
            print(f"  Usage: synth <name>   e.g. synth bass, synth bell")
            print(f"  Built-in: {', '.join(supercollider.catalogue_names())}")
            print(f"  Install them with 'synthdef all', list with 'synthdef'.")
            for line in self._available_params():
                print(line)
            return
        try:
            name = supercollider.safe_name(text)
        except ValueError as e:
            print(f"Error: {e}")
            return
        self.synth = name
        self._commit()
        described = supercollider.catalogue_description(name)
        extra = f"   ({described})" if described else ""
        print(f"synth set to '{name}'{extra} (per-note /s_new on "
              f"{self._osc_engine().target_text})")
        known = supercollider.list_synthdefs()
        if not described and name not in known:
            print(f"  Note: nothing local defines '{name}'. Install it with "
                  f"'synthdef all' or 'synthdef {name}', or make sure it is "
                  f"already loaded in SuperCollider.")

    def _synthdef_listing(self):
        """Show the built-in catalogue and the files in synthdefs/."""
        files = set(supercollider.list_synthdefs())
        print(f"Built-in SynthDefs (send them all with 'synthdef all'):")
        for name in supercollider.catalogue_names():
            mark = "*" if name in files else " "
            print(f"  {mark} {name:<7} {supercollider.catalogue_description(name)}")
        extra = sorted(files - set(supercollider.catalogue_names()))
        if extra:
            print(f"  Your files: {', '.join(extra)}")
        print(f"  (* = written in {supercollider.SYNTHDEF_DIR}; "
              f"'synth <name>' picks one per pattern)")
        print(f"  Run {supercollider.HELPER_FILE} once in SuperCollider so the "
              f"/seqd responder exists.")

    def _send_all_synthdefs(self):
        """Write the whole catalogue and install it with one /seqd message."""
        engine = self._osc_engine()
        path, count, err = engine.send_all_synthdefs()
        if err:
            print(f"Error: {err}")
            print(f"  Everything is written in {supercollider.SYNTHDEF_DIR}; "
                  f"run {supercollider.HELPER_FILE} in SuperCollider and "
                  f"send it again.")
            return
        print(f"Sent {count} SynthDefs to sclang at {engine.sclang_text} as one "
              f"file:")
        print(f"  {path}")
        print(f"  Choose one per pattern with 'synth <name>' (currently "
              f"'{self.synth or supercollider.DEFAULT_SYNTH}').")

    def do_synthdef(self, arg):
        """Install SynthDefs: 'synthdef all' (one shot) or 'synthdef <name> [file]'"""
        parts = (arg or "").split()
        if not parts:
            self._synthdef_listing()
            return
        if parts[0].lower() in ("all", "*", "everything"):
            self._send_all_synthdefs()
            return
        try:
            name = supercollider.safe_name(parts[0])
        except ValueError as e:
            print(f"Error: {e}")
            return
        source = None
        if len(parts) > 1:
            candidate = parts[1]
            path = candidate if os.path.isabs(candidate) else os.path.join(
                supercollider.SYNTHDEF_DIR, candidate)
            if not os.path.isfile(path):
                print(f"Error: '{candidate}' not found "
                      f"(looked in {supercollider.SYNTHDEF_DIR}).")
                return
            with open(path, encoding="utf-8") as handle:
                source = handle.read()
        engine = self._osc_engine()
        path, err = engine.send_synthdef(name, source)
        if err:
            print(f"Error: {err}")
            print(f"  The file was written to {path}; run "
                  f"{supercollider.HELPER_FILE} in SuperCollider first.")
            return
        described = supercollider.catalogue_description(name)
        print(f"SynthDef '{name}' sent to sclang at {engine.sclang_text} "
              f"({path}).")
        if described:
            print(f"  {described}")
        if not self.synth:
            self.synth = name
            self._commit()
            print(f"  This pattern now plays 'synth {name}'.")

    def do_latency(self, arg):
        "Show or set the OSC scheduling latency in seconds (default 0.2)"
        text = (arg or "").strip()
        current = (self.osc_latency if self.osc_latency is not None
                   else supercollider.DEFAULT_LATENCY)
        if not text:
            print(f"latency: {current:g} s "
                  f"({'set' if self.osc_latency is not None else 'default'})")
            print(f"  Bundles are scheduled this far in the future; scsynth "
                  f"prints 'late <t>' when one arrives too late.")
            print(f"  Raise it if you see 'late' messages, lower it for tighter "
                  f"timing (e.g. latency 0.2 or latency 0.1).")
            return
        try:
            value = float(text)
        except ValueError:
            print(f"Error: '{text}' is not a number of seconds.")
            return
        if not supercollider.MIN_LATENCY <= value <= supercollider.MAX_LATENCY:
            print(f"Error: latency must be between "
                  f"{supercollider.MIN_LATENCY:g} and "
                  f"{supercollider.MAX_LATENCY:g} seconds.")
            return
        self.osc_latency = value
        self._commit()
        print(f"latency set to {value:g} s "
              f"(applies at the next cycle of any playing osc pattern)")

    def _available_params(self):
        """Which controls can be sent to this pattern's SynthDef, as text."""
        synth = self.synth or supercollider.DEFAULT_SYNTH
        shared, extras = scdefs.controls(synth)
        lines = [f"  {synth} accepts (set them with 'fx <name> <value|expr>'):",
                 f"    note-driven  midinote freq amp dur    "
                 f"(amp from v<k>, dur from sus<k>)",
                 f"    every synth  {' '.join(shared)}"]
        if extras:
            lines.append(f"    {synth} only   {' '.join(extras)}")
        known = [p for p in shared + extras if p in supercollider.FX_RANGES]
        if known:
            ranges = ", ".join(f"{p} {supercollider.fx_range(p)[0]:g}.."
                               f"{supercollider.fx_range(p)[1]:g}" for p in known)
            lines.append(f"    clamped      {ranges}")
        if not extras:
            lines.append("    (a SynthDef you write yourself can declare any "
                         "extra control and it will be sent as-is)")
        d = supercollider.catalogue_description(synth)
        if not d:
            lines.append(f"    note: '{synth}' is not a built-in name, so the "
                         f"list above is just the common set")
        return lines

    def do_fx(self, arg):
        "List, set or clear the per-note effect parameters of an osc pattern"
        text = (arg or "").strip()
        if not text:
            if self.fx:
                print(f"FX parameters of pattern {self.pattern_number} "
                      f"({self.bpm} BPM):")
                lfos = self._root_shell().project.get("lfos", {})
                for name in sorted(self.fx):
                    low, high = supercollider.fx_range(name)
                    try:
                        now = velocity.evaluate_span(self.fx[name], lfos,
                                                     self.bpm,
                                                     time.monotonic(), low, high)
                    except ValueError:
                        now = "?"
                    print(f"  {name}: {self.fx[name]}   "
                          f"(now {now}, range {low:g}..{high:g})")
            else:
                print("No fx parameters configured.")
            print("  Usage: fx <name> <value|expr>   e.g. fx cutoff 2000+1500*lfo1")
            print("         rm t1p1 fx cutoff        (clear one)")
            for line in self._available_params():
                print(line)
            return
        parts = text.split(maxsplit=1)
        if len(parts) == 1:
            name = parts[0]
            if name in self.fx:
                print(f"{name}: {self.fx[name]}")
            else:
                print(f"{name}: not configured. Usage: fx {name} <value|expr>")
            return
        name, expr_text = parts[0], parts[1].strip()
        if expr_text.startswith("="):
            expr_text = expr_text[1:].strip()
        try:
            canonical = velocity.canonical(expr_text)
        except ValueError as e:
            print(f"Error: {e}")
            return
        low, high = supercollider.fx_range(name)
        try:
            preview = velocity.evaluate_span(canonical,
                                             self._root_shell().project.get("lfos", {}),
                                             self.bpm, time.monotonic(), low, high)
        except ValueError as e:
            print(f"Error: {e}")
            return
        self.fx[name] = canonical
        self._commit()
        known = "" if name.lower() in supercollider.FX_RANGES else \
            " (custom parameter: your SynthDef decides what it does)"
        print(f"fx {name} set: {canonical}   (now {preview:g}, range "
              f"{low:g}..{high:g}){known}")

    def do_dump(self, arg):
        "Record and show the OSC traffic (dump on|off, dump = show the log)"
        text = (arg or "").strip().lower()
        engine = self._osc_engine()
        if text in ("on", "start"):
            engine.recording = True
            print(f"OSC recording on (up to {engine.log_limit} packets). "
                  f"Type 'dump' to show them.")
            return
        if text in ("off", "stop"):
            engine.recording = False
            print("OSC recording off.")
            return
        if text in ("clear", "reset"):
            engine.log = []
            print("OSC log cleared.")
            return
        print(f"OSC recording: {'on' if engine.recording else 'off'}   "
              f"target {engine.target_text}, {engine.sent_notes} notes sent")
        if not engine.log:
            print(f"  (nothing recorded yet - 'dump on', play, then 'dump')")
            return
        print(f"  last {len(engine.log)} packet(s):")
        for line in engine.log:
            print(f"    {line}")

    def do_status(self, arg):
        "Ask the SuperCollider server for its /status (is it running?)"
        text = (arg or "").strip()
        if text:
            try:
                server = supercollider.parse_target(
                    text, supercollider.DEFAULT_SERVER_PORT)
            except ValueError as e:
                print(f"Error: {e}")
                return
            engine = supercollider.engine_for(server, None)
        else:
            engine = self._osc_engine()
        print(f"Pinging {engine.target_text} ...")
        info = engine.status(timeout=1.5)
        if not info:
            print("  No /status.reply: no SuperCollider server is listening "
                  "there.")
            print("  Start it (scsynth / 's.boot' in the SC IDE) or set the "
                  "target with 'osc <host>:<port>'.")
            return
        print(f"  Synths {info['synths']}, groups {info['groups']}, "
              f"SynthDefs {info['synthdefs']}, UGens {info['ugens']}, "
              f"CPU {info['avg_cpu']:.1f}%, SR {info['sample_rate']:.0f} Hz")

    def _osc_line(self):
        """One-line summary for views."""
        engine_text = self.osc_server or (
            f"{supercollider.DEFAULT_HOST}:{supercollider.DEFAULT_SERVER_PORT}"
            f" (default)")
        latency = (self.osc_latency if self.osc_latency is not None
                   else supercollider.DEFAULT_LATENCY)
        return (f"OSC {engine_text}, synth "
                f"'{self.synth or supercollider.DEFAULT_SYNTH}', "
                f"{self._fx_text()}, latency {latency:g} s")


    def do_type(self, arg):
        "Show or set the pattern type: note (default), CC, loop or osc"
        text = (arg or "").strip().lower()
        if not text:
            extra = (f" (controller {self.controller})"
                     if self.pattern_type == "cc" else "")
            if self.pattern_type == "loop":
                sample = self.sample or "(no sample selected)"
                length = self._sample_seconds()
                secs = f", {length:.2f} s" if length is not None else ""
                extra = f" (sample {sample}{secs})"
            elif self.pattern_type == "osc":
                extra = f" ({self._osc_line()})"
            shown = {"cc": "CC", "loop": "loop"}.get(self.pattern_type, "note")
            print(f"Type: {shown}{extra}")
            return
        if text in ("note", "notes"):
            self.pattern_type = "note"
        elif text in ("cc", "control", "controller-change"):
            self.pattern_type = "cc"
        elif text in ("loop", "sample", "sampler"):
            self.pattern_type = "loop"
        elif text in ("osc", "sc", "supercollider"):
            self.pattern_type = "osc"
        else:
            print(f"Error: unknown type '{arg.strip()}'. Use 'note', 'CC', "
                  f"'loop' or 'osc'.")
            return
        self._commit()
        if self.pattern_type == "cc":
            print(f"Type set to CC (controller {self.controller}); "
                  f"sequences hold raw values 0..127")
        elif self.pattern_type == "loop":
            if self.sample:
                length = self._sample_seconds()
                secs = f", {length:.2f} s" if length is not None else ""
                print(f"Type set to loop (sample {self.sample}{secs}); order "
                      f"positions with 'o0 <0..1 ...>' and play with "
                      f"'start o0' (or arrange them in a phrase)")
            else:
                print("Type set to loop; choose a file with 'sample <name.wav>' "
                      f"from {sampler.samples_dir()}")
        elif self.pattern_type == "osc":
            print(f"Type set to osc ({self._osc_line()}); sequences hold scale "
                  f"degrees, 'v<k>'/'sus<k>' become amp/dur, 'fx' adds "
                  f"parameters. Send definitions with 'synthdef <name>'.")
        else:
            print("Type set to note")

    def do_controller(self, arg):
        "Show or set the CC number sent by a CC pattern (0-127)"
        text = (arg or "").strip()
        if not text:
            note = "" if self.pattern_type == "cc" else "  (pattern type is 'note')"
            print(f"Controller: {self.controller}{note}")
            return
        try:
            value = int(text, 0)
        except ValueError:
            print(f"Error: invalid controller '{text}'. Use 0-127.")
            return
        if not 0 <= value <= 127:
            print("Error: controller must be between 0 and 127.")
            return
        self.controller = value
        self._commit()
        print(f"Controller set to {value}")

    def do_division(self, arg):
        "Show or set the pattern step division: 1, 1/2, 1/4, 1/8, 1/16, 1/32 ..."
        text = (arg or "").strip()
        if not text:
            n = self.division
            print(f"Division: {division_to_text(n)} "
                  f"(step {division_step_ms(self.bpm, n):g} ms at {self.bpm} BPM)")
            return
        try:
            n = parse_division(text)
        except ValueError as e:
            print(f"Error: {e}")
            return
        self.division = n
        self._commit()
        print(f"Division set to {division_to_text(n)} "
              f"(step {division_step_ms(self.bpm, n):g} ms at {self.bpm} BPM)"
              f"{self._live_hint()}")

    def launch_loop(self, track, pattern, index):
        """Start a loop order list through the root (owns the player)."""
        self._root_shell().launch_loop(track, pattern, order_index=index)

    def launch_loop_phrase(self, track, pattern, phrase):
        """Start a loop arrangement (phrase of order lists)."""
        self._root_shell().launch_loop(track, pattern, phrase=phrase)

    def stop_loop(self, track, pattern, index, phrase=None):
        self._root_shell().stop_loop(track, pattern, index, phrase=phrase)

    def _edit_verb(self, rest):
        """Dispatch scale/root/bpm/channel edits (used by path commands)."""
        parts = (rest or "").split(maxsplit=1)
        if not parts:
            raise ValueError("missing setting")
        verb = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""
        if verb == "scale":
            self.do_scale(arg)
        elif verb == "root":
            self.do_root(arg)
        elif verb == "bpm":
            self.do_bpm(arg)
        elif verb == "channel":
            self.do_channel(arg)
        elif verb == "select-port":
            self.do_select_port(arg)
        elif verb == "division":
            self.do_division(arg)
        elif verb == "type":
            self.do_type(arg)
        elif verb == "controller":
            self.do_controller(arg)
        elif verb in ("sample", "samples"):
            self.do_sample(arg)
        elif verb in ("osc", "sc"):
            self.do_osc(arg)
        elif verb == "sclang":
            self.do_sclang(arg)
        elif verb == "synth":
            self.do_synth(arg)
        elif verb in ("synthdefs", "list-synth"):
            self.do_synthdef(arg)
        elif verb in ("synthdef", "synthdefs", "list-synth"):
            self.do_synthdef(arg)
        elif verb in ("fx", "effect"):
            self.do_fx(arg)
        elif verb == "latency":
            self.do_latency(arg)
        elif verb == "dump":
            self.do_dump(arg)
        elif verb in ("help", "?", "h"):
            self.do_help(arg)
        elif verb == "status":
            self.do_status(arg)
        elif verb in ("hist", "histogram"):
            self.do_hist(arg)
        elif verb in ("list-s", "list-sequences"):
            self.do_list_s(arg)
        elif verb in ("list-f", "list-phrases"):
            self.do_list_f(arg)
        elif verb == "list-v":
            self.do_list_v(arg)
        elif verb == "list-sus":
            self.do_list_sus(arg)
        elif verb == "list-mt":
            self.do_list_mt(arg)
        elif verb in ("list-o", "list-orders"):
            self.do_list_o(arg)
        else:
            raise ValueError(f"unknown pattern setting '{verb}' (use scale, "
                             f"root, bpm, channel, division, type, controller, "
                             f"sample, osc, synth, fx, latency, synthdef, hist, "
                             f"list-o, select-port, s<k>, f<k> or o<k>)")

    def do_start(self, arg):
        """Play a phrase (f<k>) or a loop order (o<k>): start f0 / start o0"""
        text = (arg or "").strip()
        if not text and self.pattern_type == "loop":
            # convenience: the first phrase, else the first order list
            if self.phrases:
                text = f"f{min(self.phrases)}"
            elif self.orders:
                text = f"o{min(self.orders)}"
        try:
            path = parse_compact_path(text, track=self._track_number(),
                                      pattern=self.pattern_number)
            if path["kind"] not in ("f", "o"):
                raise ValueError("use start f<k> (phrase, e.g. start f0) or "
                                 "start o<k> (loop shortcut, e.g. start o0)")
        except ValueError as e:
            print(f"Error: {e}")
            return
        root = self._root_shell()
        if path["kind"] == "o" or self.pattern_type == "loop":
            root.launch(path["track"], path["pattern"], path["kind"],
                        path["index"])
            self._player = None
        else:
            self._player = root.launch_phrase(path["track"], path["pattern"],
                                              path["index"])

    def do_stop(self, arg):
        """Stop a phrase (f<k>) or loop order (o<k>); no arg stops this pattern"""
        text = (arg or "").strip()
        root = self._root_shell()
        if text:
            try:
                path = parse_compact_path(text, track=self._track_number(),
                                          pattern=self.pattern_number)
                if path["kind"] not in ("f", "o"):
                    raise ValueError("use stop f<k> (phrase) or stop o<k> "
                                     "(loop order)")
            except ValueError as e:
                print(f"Error: {e}")
                return
            root.stop_target(path["track"], path["pattern"], path["kind"],
                             path["index"])
        else:
            root.stop_pattern(self._track_number(), self.pattern_number)
        self._player = None

    def do_scale(self, arg):
        """Show or set the scale: name, catalogue class (hex-07@3) or offsets (0,2,4,7,9). 'scale ?' lists them"""
        name = (arg or "").strip()
        if name in ("?", "list") or name.startswith("?"):
            self._list_scales(name[1:].strip())
            return
        if not name:
            print(f"Scale: {self._key_line()}")
            return
        key = scales.resolve_scale(name)
        if key is None:
            print(f"Error: unknown scale '{name}'.")
            print("Hint: 'scale ?' lists known scales, 'scale ? 6' the hexatonic "
                  "ones, or give offsets like 'scale 0,2,4,7,9'.")
            return
        self.scale = key
        self._commit()
        extra = ""
        if key in scales.CATALOGUE:
            extra = (f"   (class {_intervals_text(scales.CATALOGUE[key])}, "
                     f"vector {_vector_text(scales.CATALOGUE[key])})")
        elif key.startswith("custom-"):
            extra = "   (custom interval list)"
        print(f"Scale set: {self._key_line()}{extra}{self._live_hint()}")

    def _list_scales(self, filt=""):
        """'scale ?' listing: named scales, or catalogue classes with a filter."""
        import textwrap

        text = (filt or "").strip().lower()
        if not text:
            named = sorted(n for n in scales.SCALES
                           if n not in scales.CATALOGUE)
            print("Named scales (aliases accepted, 5/6/7 notes):")
            print(textwrap.fill(" ".join(named), width=78, initial_indent="  ",
                                subsequent_indent="  "))
            print(f"Catalogue of set classes ({len(scales.CATALOGUE)} generated, "
                  f"complete for those note counts):")
            for cardinality in scales.CATALOGUE_CARDINALITIES:
                keys = scales.catalogue_names(cardinality)
                print(f"  {cardinality} notes: {keys[0]}..{keys[-1]}   "
                      f"({len(keys)} classes, prefix "
                      f"'{scales.CARDINALITY_NAMES[cardinality]}')")
            print("  'scale ? 6'       list one cardinality (5, 6 or 7)")
            print("  'scale ? <text>'  filter by name")
            print("  'scale hex-07@3'  pick a mode of a class")
            print("  'scale 0,2,4,7,9' set any interval list")
            return

        keys = []
        if text.isdigit() and int(text) in scales.CARDINALITY_NAMES:
            keys = scales.catalogue_names(int(text))
        elif text in scales.CARDINALITY_NAMES.values():
            keys = scales.catalogue_names(prefix=text)
        else:
            keys = [k for k in scales.catalogue_names() if text in k]
            if not keys:
                named = [n for n in scales.scale_choices() if text in n]
                if named:
                    print(f"Scales matching '{filt}':")
                    print(textwrap.fill(" ".join(named), width=78,
                                        initial_indent="  ",
                                        subsequent_indent="  "))
                    return
                print(f"No scale matches '{filt}'.")
                return
        print(f"Set classes matching '{filt}' ({len(keys)}):")
        for key in keys:
            form = scales.CATALOGUE[key]
            named = [n for n in scales.named_scales_for(form) if n != key]
            extra = f"   [{', '.join(named)}]" if named else ""
            print(f"  {key:<8} {_intervals_text(form):<24} "
                  f"vector {_vector_text(form)}  modes "
                  f"{len(scales.rotations(form))}{extra}")

    def complete_scale(self, text, line, begidx, endidx):
        choices = scales.scale_choices()
        prefix = scales.normalize_scale_name(text)
        return [c for c in choices if c.startswith(prefix)]

    def do_root(self, arg):
        "Show or set the pattern root note (default: C4). Notation: C4, F#3, Bb2"
        name = (arg or "").strip()
        if not name:
            print(f"Root: {self.root.name()}")
            print(f"Scale: {self._key_line()}")
            return
        note = scales.parse_note(name)
        if note is None:
            print(f"Error: invalid note '{name}'. Use 'Nm' notation, e.g. C4, F#3, Bb2.")
            return
        self.root = note
        self._commit()
        print(f"Root set: {self._key_line()}{self._live_hint()}")

    def complete_root(self, text, line, begidx, endidx):
        # Offer chromatic note names in the octave of the current root.
        prefix = text.upper()
        return [f"{pc}{self.root.octave}" for pc in scales.CHROMATIC_SHARPS
                if f"{pc}{self.root.octave}".startswith(prefix)]

    def do_bpm(self, arg):
        "Show or set the BPM of this pattern only (default: inherits the global BPM)"
        global_bpm = getattr(self._root_shell(), "bpm", DEFAULT_BPM)
        if not (arg or "").strip():
            if self.bpm_inherited:
                print(f"BPM: {self.bpm} (inherited from the global {global_bpm})")
            else:
                print(f"BPM: {self.bpm} (pattern override; global is {global_bpm})")
            return
        value = parse_bpm(arg)
        if value is None:
            return
        self.bpm = value
        self.bpm_inherited = False
        self._commit()
        print(f"BPM set to {value} for pattern {self.pattern_number} "
              f"(global: {global_bpm}){self._live_hint()}")

    def do_channel(self, arg):
        "Show or set the MIDI channel of this pattern (default: 1, valid 1-16)"
        if not (arg or "").strip():
            print(f"Channel: {self.channel}")
            return
        n = parse_index(arg, CHANNEL_MIN, CHANNEL_MAX, "channel")
        if n is None:
            return
        self.channel = n
        self._commit()
        print(f"Channel set to {n} for pattern {self.pattern_number}{self._live_hint()}")

    def do_select_port(self, arg):
        "Set this pattern's MIDI output port (default: the global output port)"
        port = choose_midi_output_port()
        if port is None:
            return
        self.out_port = port
        self._commit()
        print(f"Pattern output port set to: {self.out_port}")

    # ------------------------------------------------------------------
    # Sequences: seq0..seq9 (scale degrees with optional O/o octave shift)
    # ------------------------------------------------------------------
    def _seq_tokens(self, seq):
        """Render a stored sequence back to '0 O0 o3 r' style tokens."""
        return seq_to_text(seq)

    def _seq_notes(self, seq):
        """Resolve a stored sequence (note names, or CC values in cc mode)."""
        lfos = self._root_shell().project.get("lfos", {})
        if self.pattern_type == "cc":
            return describe_cc_values(seq, lfos, self.bpm, time.monotonic(),
                                      notes=True)
        return resolve_seq_notes(seq, self.root, self.scale,
                                 lfos, self.bpm, time.monotonic())

    def _set_seq(self, n, tokens):
        cc = self.pattern_type == "cc"
        if not tokens:
            if cc:
                print(f"Usage: s{n} <value|r> [...]  e.g. s{n} 0 64 127 r")
                print(f"  CC pattern: values 0..127 sent to controller "
                      f"{self.controller}; 'r' skips a step.")
            else:
                print(f"Usage: s{n} <degree|r> [...]  e.g. s{n} 0 0 r 3")
                print(f"  Degrees index the pattern's scale (0 = root, "
                      f"currently {self.scale}) and wrap across octaves, so "
                      f"they follow any later scale change; 'r' is a rest.")
                print("  Prefix O = next octave up, o = next octave down (e.g. O0, o3).")
            print("  LFO terms use regular algebra (products first, then "
                  "sums): 127*lfo1 multiplies, 63+63*lfo1 offsets,")
            print("     5*lfo1*lfo2 multiplies by two LFOs, 30-2*lfo1 subtracts. "
                  "Bipolar LFOs go -1..1, ramp 0..1: pick 64+63*lfo1")
            print("     for a centred sweep (33..127 becomes 1..127), 0*127*lfo1 "
                  "with a ramp for 0..127. Note degrees wrap octaves;")
            print("     CC values clamp to 0..127.")
            return False
        parsed = []
        max_degree = 127  # degrees are scale-relative and wrap octaves
        zero_lfo = []
        for tok in tokens:
            try:
                entry = seq_token_entry(tok)
            except ValueError:
                what = ("a value 0..127" if cc else
                        "a scale degree like 0, an octave prefixed one like O0 / o3")
                print(f"Error: invalid token '{tok}'. Use {what}, 'r' for a "
                      f"rest, or an LFO term like 5*lfo1, 2*lfo1*lfo2 or "
                      f"63+63*lfo1.")
                return False
            if entry is None:
                parsed.append(None)  # rest
                continue
            if entry[2] is None and re.search(r"(?i)lfo", tok):
                zero_lfo.append(tok)   # e.g. 0*5*lfo1 multiplies out to 0
            degree, shift, variation = entry
            if degree > max_degree:
                if cc:
                    print(f"Error: value {degree} is out of range for a CC "
                          f"pattern (values 0-127).")
                else:
                    print(f"Error: degree {degree} is out of range "
                          f"(degrees 0-127; they index the scale and wrap "
                          f"across octaves).")
                return False
            if variation is not None:
                for _coef, ref in variation_terms(variation):
                    if not 1 <= ref <= MAX_LFOS:
                        print(f"Error: LFO reference lfo{ref} must be between "
                              f"1 and {MAX_LFOS}.")
                        return False
            parsed.append(entry)
        self.seqs[n] = parsed
        self._commit()
        print(f"s{n} set: {self._seq_tokens(parsed)}   -> {self._seq_notes(parsed)}")
        for bad in zero_lfo:
            print(f"  Note: '{bad}' multiplies out to 0 (products come first). "
                  f"For an offset write it with a sum, e.g. 5*lfo1 or 0+5*lfo1.")
        return True

    def show_sequence(self, n):
        """Print one stored sequence (used by 's<k>' and show paths)."""
        if n in self.seqs:
            seq = self.seqs[n]
            print(f"s{n}: {self._seq_tokens(seq)}   -> {self._seq_notes(seq)}")
        else:
            print(f"s{n}: not configured. Usage: s{n} <degree|r> [...]  (r = rest)")

    def set_velocity(self, index, text, via_path=False):
        """Set velocity v<index> (constant or LFO-linked expression)."""
        text = (text or "").strip()
        if text.startswith("="):
            text = text[1:].strip()
        if not text:
            self.show_velocity(index)
            return False
        try:
            canonical = velocity.canonical(text)
        except ValueError as e:
            print(f"Error: {e}")
            print(f"       Usage: v{index} <value|expr>   e.g. v{index} 45 "
                  f"or v{index} 64+0.4*lfo1  (0..127 is enforced when playing)")
            return False
        self.velocities[index] = canonical
        self._commit()
        preview = velocity.evaluate(canonical,
                                    self._root_shell().project.get("lfos", {}),
                                    self.bpm, time.monotonic())
        target = f"o{index}" if self.pattern_type == "loop" else f"s{index}"
        print(f"v{index} set: {canonical}   (now {preview}; scales {target})")
        return True

    def show_velocity(self, n):
        """Print one stored velocity expression."""
        target = f"o{n} (volume)" if self.pattern_type == "loop" else f"s{n}"
        if n in self.velocities:
            print(f"v{n}: {self.velocities[n]}   (scales {target})")
        else:
            print(f"v{n}: not configured (scales {target}). "
                  f"Usage: v{n} <value|expr>  e.g. v{n} 45 or v{n} 64+0.4*lfo1")

    def set_sustain(self, index, text, via_path=False):
        """Set sustain sus<index> (share of the step; constant or LFO-linked)."""
        text = (text or "").strip()
        if text.startswith("="):
            text = text[1:].strip()
        if not text:
            self.show_sustain(index)
            return False
        try:
            canonical = velocity.canonical(text)
        except ValueError as e:
            print(f"Error: {e}")
            print(f"       Usage: sus{index} <length|expr>  length in steps: "
                  f"0.5 (half a step), 2 (two steps), 1/4 (a quarter step), "
                  f"or an expression like 0.5+0.5*lfo1")
            return False
        self.sustains[index] = canonical
        self._commit()
        now = velocity.evaluate_span(canonical,
                                     self._root_shell().project.get("lfos", {}),
                                     self.bpm, time.monotonic(),
                                     0.0, player.SUSTAIN_MAX_STEPS)
        step_ms = division_step_ms(self.bpm, self.division)
        print(f"sus{index} set: {canonical}   "
              f"(now {now:g} step{'s' if now != 1 else ''} = "
              f"{now * step_ms:g} ms at {division_to_text(self.division)}, "
              f"{self.bpm} BPM)")
        return True

    def show_sustain(self, n):
        """Print one stored sustain expression."""
        if n in self.sustains:
            print(f"sus{n}: {self.sustains[n]}")
        else:
            print(f"sus{n}: not configured. Usage: sus{n} <length|expr>  "
                  f"e.g. sus{n} 0.5 (half a step) or sus{n} 1/4; "
                  f"default is {player.SUSTAIN_FRACTION:g} of the step")

    def set_microtime(self, index, text, via_path=False):
        """Set microtiming mt<index>: signed offset in steps (early/late)."""
        text = (text or "").strip()
        if text.startswith("="):
            text = text[1:].strip()
        if not text:
            self.show_microtime(index)
            return False
        try:
            canonical = velocity.canonical(text)
        except ValueError as e:
            print(f"Error: {e}")
            print(f"       Usage: mt{index} <offset|expr>  offset in steps: "
                  f"1/4 (a quarter step late), -1/8 (an eighth early), 0.5, "
                  f"or an expression like 0.01*lfo1 "
                  f"(clamped to +/-{player.MICROTIME_MAX_STEPS} steps)")
            return False
        self.microtimes[index] = canonical
        self._commit()
        now = velocity.evaluate_span(canonical,
                                     self._root_shell().project.get("lfos", {}),
                                     self.bpm, time.monotonic(),
                                     -player.MICROTIME_MAX_STEPS,
                                     player.MICROTIME_MAX_STEPS)
        step = division_step_ms(self.bpm, self.division)
        print(f"mt{index} set: {canonical}   (now {now:+.4g} steps = "
              f"{now * step:+.1f} ms "
              f"{'late' if now > 0 else ('early' if now < 0 else 'on the grid')} "
              f"at {division_to_text(self.division)}, {self.bpm} BPM)")
        return True

    def show_microtime(self, n):
        """Print one stored microtiming expression."""
        if n in self.microtimes:
            print(f"mt{n}: {self.microtimes[n]}")
        else:
            print(f"mt{n}: not configured. Usage: mt{n} <value|expr>  "
                  f"e.g. mt{n} -0.05 or mt{n} 0.01*lfo1 (0 = on the grid)")

    def do_list_mt(self, arg):
        "List all configured microtiming sequences of this pattern (mt0..mt9)"
        step_ms = division_step_ms(self.bpm, self.division)
        print(f"\nMicrotiming (pattern {self.pattern_number}, "
              f"{division_to_text(self.division)} = {step_ms:g} ms, "
              f"bpm {self.bpm}):   negative = earlier")
        if not self.microtimes:
            print("  (none configured - notes land exactly on the grid)")
            print()
            return
        step = division_step_ms(self.bpm, self.division)
        for n in sorted(self.microtimes):
            expr = self.microtimes[n]
            now = velocity.evaluate_span(expr,
                                         self._root_shell().project.get("lfos", {}),
                                         self.bpm, time.monotonic(),
                                         -player.MICROTIME_MAX_STEPS,
                                         player.MICROTIME_MAX_STEPS)
            print(f"  mt{n}: {expr:<20} -> {now:+.4g} steps = "
                  f"{now * step_ms:+.1f} ms")
        print()

    def do_list_sus(self, arg):
        "List all configured sustain sequences of this pattern (sus0..sus9)"
        step_ms = division_step_ms(self.bpm, self.division)
        print(f"\nSustains (pattern {self.pattern_number}, "
              f"{division_to_text(self.division)} = {step_ms:g} ms, "
              f"bpm {self.bpm}):")
        if not self.sustains:
            print(f"  (none configured - notes are held "
                  f"{player.SUSTAIN_FRACTION:g} of the step = "
                  f"{player.SUSTAIN_FRACTION * step_ms:g} ms)")
            print()
            return
        lfos = self._root_shell().project.get("lfos", {})
        for n in sorted(self.sustains):
            expr = self.sustains[n]
            now = velocity.evaluate_span(expr, lfos, self.bpm,
                                         time.monotonic(), 0.0,
                                         player.SUSTAIN_MAX_STEPS)
            print(f"  sus{n}: {expr:<20} -> {now:g} steps = "
                  f"{now * step_ms:g} ms")
        print()

    def do_list_v(self, arg):
        "List all configured velocity sequences of this pattern (v0..v9)"
        print(f"\nVelocities (pattern {self.pattern_number}, bpm {self.bpm}):")
        if not self.velocities:
            print("  (none configured - notes are sent with velocity 100)")
            print()
            return
        for n in sorted(self.velocities):
            print(f"  v{n}: {self.velocities[n]}")
        print()

    def do_list_s(self, arg):
        "List all configured sequences of this pattern (s0..s9)"
        print(f"\nSequences (scale: {self.scale}, root: {self.root.name()}):")
        if not self.seqs:
            print("  (none configured)")
            print()
            return
        for n in sorted(self.seqs):
            seq = self.seqs[n]
            print(f"  s{n}: {self._seq_tokens(seq):<28} -> {self._seq_notes(seq)}")
        print()

    def do_list_f(self, arg):
        "List all configured phrases of this pattern (f0..f7)"
        print(f"\nPhrases of pattern {self.pattern_number}:")
        if not self.phrases:
            print("  (none configured)")
            print()
            return
        for n in sorted(self.phrases):
            print(f"  f{n}: {self.phrases[n]}")
        print()

    def completenames(self, text, *ignored):
        names = super().completenames(text, *ignored)
        names += [f"s{i}" for i in range(10) if f"s{i}".startswith(text)]
        names += [f"f{i}" for i in range(MAX_PHRASES)
                  if f"f{i}".startswith(text)]
        names += [f"o{i}" for i in range(sampler.MAX_ORDER_LISTS)
                  if f"o{i}".startswith(text)]
        names += [f"p{i}" for i in range(1, MAX_PATTERNS + 1)
                  if f"p{i}".startswith(text)]
        return names

    def do_panic(self, arg):
        "Stop everything (phrases, loops) and silence MIDI/audio outputs"
        self._root_shell().panic()

    def do_help(self, arg):
        if arg:
            return super().do_help(arg.strip().replace("-", "_"))
        print(f"\nPattern {self.pattern_number} Commands:")
        print(f"  {'s0..s9 [deg...]':<20} - set a sequence of scale degrees, e.g. s0 0 0 r 3")
        print(f"  {'':<20}   LFO algebra: 127*lfo1 (product), 63+63*lfo1 (offset)")
        print(f"  {'':<20}   in a CC pattern the values are raw controller data 0..127")
        print(f"  {'':<20}   O/o prefixes shift octaves up/down: O0 = root +1 oct, o3 = degree 3 -1 oct")
        print(f"  {'':<20}   'r' inserts a rest")
        print(f"  {'s<k>':<20} - show one sequence of this pattern")
        print(f"  {'f0..f7 <expr>':<20} - define a phrase (arrangement), e.g. f0 inf*s0")
        print(f"  {'':<20}   note/CC: units are sequences (4*(s0+2*s1)); loop: order lists (2*o0+o1)")
        print(f"  {'f<k>':<20} - show one phrase of this pattern")
        print(f"  {'v<k> <expr>':<20} - velocity of sequence k (v0..v9): 45 or 64+0.4*lfo1")
        print(f"  {'':<20}   'v<k>=<expr>' is accepted too; values are clamped to 0..127")
        print(f"  {'sus<k> <length>':<20} - note length in steps (sus0..sus9): 0.5, 2, 1/4")
        print(f"  {'':<20}   expressions too: 0.5+0.5*lfo1; clamped to 0..{player.SUSTAIN_MAX_STEPS} steps")
        print(f"  {'list-s':<20} - list all configured sequences")
        print(f"  {'list-f':<20} - list all configured phrases")
        print(f"  {'list-v':<20} - list all configured velocities")
        print(f"  {'mt<k> <offset>':<20} - microtiming in steps (mt0..mt9): 1/4, -1/8, 0.5")
        print(f"  {'':<20}   +/-: late/early; clamped to +/-{player.MICROTIME_MAX_STEPS} steps")
        print(f"  {'list-sus':<20} - list all configured sustains")
        print(f"  {'list-mt':<20} - list all configured microtimings")
        print(f"  {'hist [f<k>|s<k>|o<k>]':<20} - per-step histogram (loop: sample positions); add 'live'")
        print(f"  {'v0..v9 [value|expr]':<20} - velocity: note loudness, or the volume of loop order o<k>")
        print(f"  {'osc [<host>:<port>]':<20} - osc pattern: SuperCollider server (default 127.0.0.1:57110)")
        print(f"  {'synth [<name>]':<20} - SynthDef the osc pattern plays (default '{supercollider.DEFAULT_SYNTH}')")
        print(f"  {'synthdef [all|<name>]':<20} - list built-ins, or install them (all = one shot)")
        print(f"  {'synth <name>':<20} - which SynthDef this pattern plays (seq, bass, bell, pad, ...)")
        print(f"  {'fx [<name> <expr>]':<20} - per-note effect parameter, e.g. fx cutoff 2000+1500*lfo1")
        print(f"  {'latency [<seconds>]':<20} - OSC scheduling slack (default {supercollider.DEFAULT_LATENCY:g} s; raise it if SC says 'late')")
        print(f"  {'dump [on|off]':<20} - record and show the OSC packets (self-test without SC)")
        print(f"  {'status':<20} - ping the SuperCollider server for /status.reply")
        print(f"  {'':<20}   note/CC: f<k> phrase or s<k> sequence; loop: f<k> arrangement or o<k> order")
        print(f"  {'':<20}   (loop patterns have no histogram: use list-o)")
        print(f"  {'start f<k>':<20} - play phrase k to the MIDI output (velocity 100)")
        print(f"  {'stop [f<k>]':<20} - stop the running phrase playback")
        print(f"  {'scale [<name>]':<20} - show or set the pattern scale")
        print(f"  {'root [<note>]':<20} - show or set the root note (e.g. F#3)")
        print(f"  {'channel [<n>]':<20} - set this pattern's MIDI channel (default: 1)")
        print(f"  {'select-port':<20} - set this pattern's MIDI output port")
        print(f"  {'bpm [<value>]':<20} - set this pattern's BPM (default: inherits global)")
        print(f"  {'division [1/16]':<20} - step note value (1, 1/2, 1/3, 1/16 ...)")
        print(f"  {'type [note|CC|loop|osc]':<20} - pattern kind (default: note); CC = controller data")
        print(f"  {'sample [<file.wav>]':<20} - loop pattern: choose a .wav from the samples folder")
        print(f"  {'o0..o9 [slice...]':<20} - loop slice order, e.g. o0 0 1 4 2 5 ('r' = rest)")
        print(f"  {'':<20}   positions are normalised (0.2 = 20 %); LFO entries work too: o0 0.2 0.5*lfo1")
        print(f"  {'list-o':<20} - list the loop orders and the selected sample")
        print(f"  {'start o<k> / stop o<k>':<20} - shortcut: play/stop one order list (inf*o<k>)")
        print(f"  {'start f<k> / stop f<k>':<20} - play/stop a loop arrangement, e.g. f0 inf*o0")
        print(f"  {'controller [<n>]':<20} - CC number 0-127 used by a CC pattern")
        print(f"  {'p<m>':<20} - switch to another pattern (m = 1-16)")
        print(f"  {'t<n>':<20} - switch to another track menu")
        print(f"  {'cp <src> <dst>':<20} - copy a leaf/pattern; cp s0 s1 also copies v/sus/mt")
        print(f"  {'rm <path>|lfo<n>':<20} - delete a track/pattern/sequence/phrase, reset an LFO or a setting")
        print(f"  {'panic':<20} - stop all playback (phrases, loops) and silence outputs")
        print(f"  {'/':<20} - return to the main menu from anywhere")
        print(f"  {'/ <command>':<20} - run a main-menu command, staying in this menu")
        print(f"  {'help':<20} - show this help")
        print(f"  {'exit':<20} - return to track {self.parent_shell.track_number} "
              f"(playback keeps running)")
        global_bpm = getattr(self._root_shell(), "bpm", DEFAULT_BPM)
        global_port = getattr(self._root_shell(), "out_port", None)
        inherit_mark = "" if self.bpm_inherited else " (override)"
        if self.out_port:
            port_text = f"{self.out_port} (override)"
        elif global_port:
            port_text = f"{global_port} (global)"
        else:
            port_text = "(none selected)"
        print(f"\n  Track: {self.parent_shell.track_number}   "
              f"Pattern: {self.pattern_number}   "
              f"Root: {self.root.name()}   Scale: {self.scale}   "
              f"Channel: {self.channel}   Port: {port_text}")
        print(f"  BPM: {self.bpm}{inherit_mark}   Global BPM: {global_bpm}   "
              f"Sequences: {len(self.seqs)}   Phrases: {len(self.phrases)}")
        print()

    def do_exit(self, arg):
        "Return to the track menu (started playback keeps running)"
        self._stop_hist_live()
        return True

    def do_EOF(self, arg):
        self._stop_hist_live()
        print()
        return True

    def emptyline(self):
        pass

    def default(self, line):
        text = line.strip()
        parts = text.split()
        if not parts:
            return
        if text.startswith("/"):
            command = text[1:].strip()
            if not command:
                self.request_root()
                return True
            self.run_root_command(command)
            return
        token, rest = parts[0], text[len(parts[0]):].strip()
        low = token.lower()
        if low.startswith("lfo"):  # jump to an LFO menu from here
            self._root_shell().onecmd(text)
            return True if self.propagate_root_request() else None
        if low in ("cp", "rm"):  # copy / remove by path (relative allowed)
            self._root_shell().onecmd(self._cp_rm_paths(text))
            return
        if low == "show":
            target = (rest or "").strip()
            if re.match(r"(?i)^t\d+", target):
                # an absolute path was given: do not prefix it again
                self._root_shell().do_show(target)
            else:
                self._root_shell().do_show(f"t{self._track_number()}p"
                                           f"{self.pattern_number}{target}")
            return
        # s<k> [degrees] / f<k> [expression] / v<k> [=][expression]
        name, rest = ((token.split("=", 1)[0],
                       (token.split("=", 1)[1] + " " + rest).strip())
                      if "=" in token else (token, rest))
        m = re.fullmatch(r"(?i)([sfvo])(\d+)", name)
        sus = None if m else re.fullmatch(r"(?i)sus(\d+)", name)
        mt = None if (m or sus) else re.fullmatch(r"(?i)mt(\d+)", name)
        if m or sus or mt:
            if sus:
                kind, index = "u", int(sus.group(1))
            elif mt:
                kind, index = "m", int(mt.group(1))
            else:
                kind, index = m.group(1).lower(), int(m.group(2))
            if kind == "m":
                if not 0 <= index <= _PATH_MAX_SEQ:
                    print(f"Error: microtiming must be mt0..mt{_PATH_MAX_SEQ}")
                    return
                self.set_microtime(index, rest)
                return
            if kind == "u":
                if not 0 <= index <= _PATH_MAX_SEQ:
                    print(f"Error: sustain must be sus0..sus{_PATH_MAX_SEQ}")
                    return
                self.set_sustain(index, rest)
                return
            if kind == "v":
                if not 0 <= index <= _PATH_MAX_SEQ:
                    print(f"Error: velocity must be v0..v{_PATH_MAX_SEQ}")
                    return
                self.set_velocity(index, rest)
                return
            if kind == "o":
                if not 0 <= index <= _PATH_MAX_ORDER:
                    print(f"Error: order list must be o0..o{_PATH_MAX_ORDER}")
                    return
                if rest:
                    self.set_order(index, rest.split())
                else:
                    self.show_order(index)
                return
            if kind == "s":
                if not 0 <= index <= _PATH_MAX_SEQ:
                    print(f"Error: sequence must be s0..s{_PATH_MAX_SEQ}")
                    return
                if rest:
                    self.set_sequence(index, rest.split())
                else:
                    self.show_sequence(index)
            else:
                if not 0 <= index <= _PATH_MAX_PHRASE:
                    print(f"Error: phrase must be f0..f{_PATH_MAX_PHRASE}")
                    return
                self.set_phrase(index, rest)
            return
        # Paths: p<m> (switch pattern), t<n> (track menu), t<n>p<m>s<k>/f<k>.
        if re.fullmatch(r"(?i)t\d+", token):  # jump to a track menu
            self._root_shell().run_navigation(token, rest)
            if self.propagate_root_request():
                return True
            return
        try:
            path = parse_compact_path(token, track=self._track_number(),
                                      pattern=self.pattern_number)
        except ValueError as e:
            if _PATH_TOKEN_RE.fullmatch(token):
                print(f"Error: {e}")
                return
            hint = old_command_hint(token)
            print(f"*** Unknown command: {line}")
            if hint:
                print(f"    Hint: {hint}")
            return
        same_track = path["track"] == self._track_number()
        same_pattern = same_track and path["pattern"] == self.pattern_number
        if not same_pattern:
            if same_track and path["kind"] is None and not rest:
                self._switch_pattern(path["pattern"])
                print(f"Pattern context set to: {self.pattern_number}")
            else:
                self._root_shell().run_navigation(token, rest)
                if self.propagate_root_request():
                    return True
            return
        if path["kind"] == "s":
            if rest:
                self.set_sequence(path["index"], rest.split())
            else:
                self.show_sequence(path["index"])
            return
        if path["kind"] == "f":
            self.set_phrase(path["index"], rest)
            return
        if path["kind"] == "o":
            if rest:
                self.set_order(path["index"], rest.split())
            else:
                self.show_order(path["index"])
            return
        if rest:
            try:
                self._edit_verb(rest)
            except ValueError as e:
                print(f"Error: {e}")


def main():
    _setup_output_encoding()
    parser = argparse.ArgumentParser(prog="seq", description="Multi-track MIDI sequencer CLI.")
    parser.add_argument("command", nargs="?", help="run a single command and exit")
    parser.add_argument("args", nargs="*", help="arguments for that command")
    args, unknown = parser.parse_known_args()

    if args.command:
        all_args = args.args + unknown
        script = COMMAND_SCRIPTS.get(args.command)
        if script:
            run_script(script, all_args)
        elif args.command in ("help", "?"):
            SeqShell().do_help("")
        else:
            print(f"Unknown command: {args.command}")
            print("Available commands: " + ", ".join(sorted(COMMAND_SCRIPTS)))
            sys.exit(2)
        return

    try:
        SeqShell().cmdloop()
    except KeyboardInterrupt:
        print("\nGoodbye!")


if __name__ == "__main__":
    main()
