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
import shutil
import subprocess
import sys
import threading
import time

import lfo
import midi_ports
import velocity
import phrases
import player
import scales

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
MAX_PHRASES = 16
MAX_LFOS = 8

LFO_DEFAULTS = {"frequency": 1.0, "shape": "sin", "phase": 0.0}
LFO_SHAPES = ("sin", "tri", "saw", "square")

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
    r"(?:(?:([sfvu])(\d+))|(?:sus(\d+))|(?:mt(\d+)))?$")
_PATH_MAX_SEQ = 9   # sequences are s0..s9
_PATH_MAX_PHRASE = 16


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
                         f"t<n>p<m>s<k>|f<k>|v<k>|sus<k>|mt<k>)")
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
    if kind == "f" and not 1 <= index <= _PATH_MAX_PHRASE:
        raise ValueError(f"phrase must be f1..f{_PATH_MAX_PHRASE}")
    if kind == "v" and not 0 <= index <= _PATH_MAX_SEQ:
        raise ValueError(f"velocity must be v0..v{_PATH_MAX_SEQ}")
    if kind == "u" and not 0 <= index <= _PATH_MAX_SEQ:
        raise ValueError(f"sustain must be sus0..sus{_PATH_MAX_SEQ}")
    if kind == "m" and not 0 <= index <= _PATH_MAX_SEQ:
        raise ValueError(f"microtiming must be mt0..mt{_PATH_MAX_SEQ}")
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


_SEQ_VAR_RE = re.compile(
    r"(?i)^([Oo]*)(\d+)((?:\*(?:(?:[+-]?(?:\d+(?:\.\d+)?))\*)?lfo\d+)+)$")
_SEQ_VAR_TERM_RE = re.compile(
    r"(?i)\*(?:(?P<coef>[+-]?(?:\d+(?:\.\d+)?))\*)?lfo(?P<ref>\d+)")


def variation_terms(variation):
    """Variation as a tuple of (coefficient, lfo index) pairs (empty if none)."""
    if not variation:
        return ()
    return tuple((float(coef), int(ref)) for coef, ref in variation)


def variation_offset(variation, lfos, bpm, now):
    """Offset in scale steps / CC units: round(amp x lfo1 x lfo2 x ...)."""
    terms = variation_terms(variation)
    if not terms:
        return 0
    product = 1.0
    for coef, ref in terms:
        try:
            product *= lfo.evaluate(lfos or {}, ref, now, bpm)
        except ValueError:
            return 0
        product *= coef
    return int(round(product))


def variation_text(variation):
    """Render a variation back to '<coef>*lfoN*lfoM' style text."""
    parts = []
    for coef, ref in variation_terms(variation):
        parts.append(f"*{coef:g}*lfo{ref}" if coef != 1 else f"*lfo{ref}")
    return "".join(parts)


def seq_token_entry(tok):
    """Parse one sequence token -> None (rest) or (degree, shift, variation).

    variation is None, or a tuple of (coefficient, lfo index) pairs; any number
    of LFOs may be multiplied together: the value moves by
    round(amp x lfo1 x lfo2 x ...) scale steps (octaves wrap) or CC units.
    """
    if tok.lower() == "r":
        return None
    m = _SEQ_VAR_RE.match(tok)
    if m:
        shift = m.group(1).count("O") - m.group(1).count("o")
        terms = []
        for coef, ref in _SEQ_VAR_TERM_RE.findall(m.group(3)):
            terms.append((1.0 if coef in ("", None) else float(coef), int(ref)))
        return (int(m.group(2)), shift, tuple(terms))
    m = re.fullmatch(r"([Oo]*)(\d+)", tok)
    if m:
        shift = m.group(1).count("O") - m.group(1).count("o")
        return (int(m.group(2)), shift, None)
    raise ValueError(f"invalid sequence token {tok!r}")


def _entry_text(entry):
    if entry is None:
        return "r"
    degree, shift, variation = entry
    prefix = "O" * shift if shift > 0 else "o" * (-shift)
    if variation is None:
        return f"{prefix}{degree}"
    return f"{prefix}{degree}{variation_text(variation)}"


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


def _term_width():
    """Terminal width in columns (0 when unknown)."""
    try:
        return shutil.get_terminal_size().columns
    except Exception:
        return 0


def _glyph_sets():
    """Block glyphs when the output can carry them, ASCII otherwise."""
    enc = getattr(sys.stdout, "encoding", None) or "utf-8"
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
        return [f"phrase f{phrase_index} is not configured in this pattern."]
    steps = max(1, int(view["division"]))
    width = _term_width()
    column_budget = _HIST_MAX if not width else max(
        4, (width - 8) // _HIST_CELL)
    truncated = steps > min(_HIST_MAX, column_budget)
    steps = min(steps, _HIST_MAX, column_budget)
    step_ms = player.step_time_for(bpm, view["division"]) * 1000.0
    try:
        plan, _ = player.flatten_phrase(expr, lambda i: view["seqs"].get(i))
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
        mts.append(lfo_value(mt_expr, 0.0, -0.5, 0.5) * step_ms)
        mt_set.append(bool(mt_expr))
        if cc:
            notes.append(str(max(0, min(127, degree + offset + 12 * shift))))
            vels.append(None)
            suss.append(None)
            continue
        index = degree + offset
        octave, pos = divmod(index, size)
        midi = root_midi + 12 * (octave + shift) + intervals[pos]
        notes.append(midi_note_name(midi))
        vels.append(lfo_value(view["velocities"].get(seq_index), player.VELOCITY))
        suss.append(lfo_value(view["sustains"].get(seq_index),
                              player.SUSTAIN_FRACTION, 0.0, 1.0))

    vel_glyphs, sus_glyphs, rest_glyph = _glyph_sets()

    def row(label, cells):
        return f"  {label:<6}" + "".join(
            f"{cell:<{_HIST_CELL}}" if cell is not None else " " * _HIST_CELL
            for cell in cells)

    def mt_cell(m, defined):
        if m is None:
            return None  # no note
        if not defined:
            return "|"  # note on the grid (no microtiming sequence)
        if abs(m) < 0.5:
            return "|  0"
        marker = "<" if m < 0 else ">"
        size_txt = f"{abs(m):>3.1f}" if abs(m) < 10 else f"{abs(m):>3.0f}"
        return marker + size_txt

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
    sus_row = row("sus", [
        f"{sus_glyphs[min(7, int(round(s * 7)))]}{int(round(s * 100)):>3}"
        if s is not None else None for s in suss])
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


def describe_cc_values(entries, lfos, bpm, now):
    """Render a CC sequence: current values 0..127 (or 'r' for skipped steps)."""
    offsets = sequence_offsets(entries, lfos, bpm, now)
    out = []
    for entry, offset in zip(entries, offsets):
        if entry is None:
            out.append("r")
            continue
        degree, shift, _variation = entry
        out.append(str(max(0, min(127, degree + offset + 12 * shift))))
    return " ".join(out)


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
        index = degree + offset
        if variation is None and index >= len(scales.SCALES[scale_key]):
            names.append("(out of scale)")
            continue
        if variation is None:
            base = scales.scale_notes(root, scales.SCALES[scale_key])
            m = re.fullmatch(r"(.*?)(-?\d+)$", base[index])
            names.append(f"{m.group(1)}{int(m.group(2)) + shift}")
        else:
            name = scale_note_name(index, root, scale_key)
            m = re.fullmatch(r"(.*?)(-?\d+)$", name)
            names.append(f"{m.group(1)}{int(m.group(2)) + shift}")
    return " ".join(names)


def save_project_file(path, bpm, out_port, tracks, lfos=None):
    """Write the whole project state to an INI-style .cfg file."""
    import configparser

    cfg = configparser.ConfigParser(interpolation=None)
    cfg.optionxform = str
    cfg.add_section("global")
    cfg.set("global", "bpm", str(bpm))
    if out_port:
        cfg.set("global", "port", out_port)
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
            if pdata.get("type") not in (None, "note"):
                cfg.set(section, "type", pdata["type"])
                cfg.set(section, "controller", str(pdata.get("controller", 1)))
            if pdata.get("division") != DEFAULT_DIVISIONS:
                cfg.set(section, "division",
                        division_to_text(pdata["division"]))
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
    if cfg.has_section("global") and cfg.has_option("global", "port"):
        port = cfg.get("global", "port").strip() or None

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
    return bpm, port, tracks, lfos


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

    def completedefault(self, text, line, begidx, endidx):
        # Route to complete_<cmd> for argument completion
        parts = line.strip().split()
        if parts:
            cmd_name = parts[0].replace("-", "_")
            func = getattr(self, "complete_" + cmd_name, None)
            if func:
                return func(text, line, begidx, endidx)
        return []

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
        start = line.rfind(" ", 0, cursor) + 1
        word = line[start:cursor]
        matches = self.completenames(word) if start == 0 else self.completedefault(word, line, start, cursor)
        if not matches:
            return cursor
        if len(matches) == 1:
            replacement = matches[0]
            if start == 0:
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
            written = save_project_file(filename, self.bpm, self.out_port,
                                        self.project["tracks"],
                                        self.project.get("lfos"))
        except OSError as e:
            print(f"Error writing {filename}: {e}")
            return
        print(f"Configuration saved to {filename} "
              f"({written} pattern section{'s' if written != 1 else ''}).")

    def do_load(self, arg):
        "Load the whole configuration from a .cfg file (default: seq.cfg)"
        filename = (arg or "").strip() or "seq.cfg"
        try:
            bpm, port, tracks, lfos = load_project_file(filename)
        except OSError as e:
            print(f"Error reading {filename}: {e}")
            return
        except Exception as e:
            print(f"Error loading {filename}: {e}")
            return
        self.bpm = bpm
        self.out_port = port
        self.project = {"tracks": tracks, "lfos": lfos}
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
        }

    def launch_phrase(self, track, pattern, phrase):
        """Start playback of phrase <phrase> of track/pattern. Returns Player or None.

        The pass data is re-read from the project store before every cycle, so
        edits to sequences/phrases (and scale/root/BPM/channel) are picked up
        at the start of the next division cycle while playing.
        """
        view0 = self._pattern_view(track, pattern)
        if not view0["port"]:
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
            plan, infinite = player.flatten_phrase(expr, lambda idx: view["seqs"].get(idx))
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
                if variation is None:
                    if degree >= len(intervals):
                        return None  # out of the current scale: silent step
                    return root_midi + intervals[degree] + 12 * shift
                # LFO-driven: whole scale steps, wrapping across octaves.
                index = degree + variation_offset(
                    variation, self.project.get("lfos", {}), view["bpm"], now)
                size = len(intervals)
                octave, pos = divmod(index, size)
                return root_midi + 12 * (octave + shift) + intervals[pos]

            return (plan, infinite, step_time, resolver, view["channel"],
                    view["type"], view["controller"])

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
            """Per-note microtiming as a signed share of the step (-0.5..0.5)."""
            view = self._pattern_view(track, pattern)
            expr = view["microtimes"].get(seq_index)
            if not expr:
                return 0.0
            try:
                return velocity.evaluate_span(
                    expr, self.project.get("lfos", {}), view["bpm"], now,
                    -0.5, 0.5)
            except ValueError:
                return 0.0

        def sustain_for(seq_index, now):
            """Per-note sustain as a share of the step (0..1)."""
            view = self._pattern_view(track, pattern)
            expr = view["sustains"].get(seq_index)
            if not expr:
                return player.SUSTAIN_FRACTION
            try:
                return velocity.evaluate_fraction(
                    expr, self.project.get("lfos", {}), view["bpm"], now)
            except ValueError:
                return player.SUSTAIN_FRACTION

        try:
            plan, infinite, step_time, resolver, channel, mode, controller = build()
        except (phrases.PhraseError, LookupError, ValueError) as e:
            print(f"Error: {e}")
            return None

        try:
            pl = player.Player(view0["port"], channel, step_time, plan, infinite,
                               resolver, provider=build,
                               velocity_resolver=velocity_for,
                               sustain_resolver=sustain_for,
                               microtime_resolver=microtime_for,
                               mode=mode, controller=controller)
        except (ValueError, OSError) as e:
            print(f"Error starting playback: {e}")
            return None
        self.playback[(track, pattern, phrase)] = pl
        kind = "infinite loop" if infinite else f"one pass ({len(plan)} steps)"
        what = (f"CC{controller}" if mode == "cc" else "notes")
        print(f"Playing phrase {phrase} of pattern {pattern}, track {track} "
              f"({self._pattern_view(track, pattern)['phrases'][phrase]}) -> "
              f"port '{view0['port']}', channel {channel} ({what}), "
              f"{view0['bpm']} BPM ({kind}, step {step_time * 1000:.1f} ms). "
              f"Type 'stop' to end.")
        return pl

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
        "Play a phrase: start t<track>p<pattern>f<phrase> (e.g. start t1p1f1)"
        try:
            path = parse_compact_path(arg, None, None)
            if path["kind"] != "f" or path["pattern"] is None:
                raise ValueError("use start t<n>p<m>f<k> (e.g. start t1p1f1)")
        except ValueError as e:
            print(f"Error: {e}")
            return
        self.launch_phrase(path["track"], path["pattern"], path["index"])

    def do_stop(self, arg):
        "Stop a phrase: stop t<track>p<pattern>f<phrase>"
        try:
            path = parse_compact_path(arg, None, None)
            if path["kind"] != "f" or path["pattern"] is None:
                raise ValueError("use stop t<n>p<m>f<k> (e.g. stop t1p1f1)")
        except ValueError as e:
            print(f"Error: {e}")
            return
        self.stop_phrase(path["track"], path["pattern"], path["index"])

    def _pattern_block(self, track, pattern, running):
        """Lines describing one pattern and its leaves (color marks running)."""
        view = self._pattern_view(track, pattern)
        seqs = view["seqs"]
        phrases = view["phrases"]
        velocities = view["velocities"]
        sustains = view["sustains"]
        microtimes = view["microtimes"]
        if not seqs and not phrases and not velocities and not sustains \
                and not microtimes:
            return None
        port_text = f"{view['port']}" if view["port"] else "(none selected)"
        what = ("type note" if view["type"] != "cc"
                else f"type CC{view['controller']}")
        block = [f"  Pattern {pattern}: {what}, scale {view['scale']}, root "
                 f"{view['root'].name()}, channel {view['channel']}, "
                 f"bpm {view['bpm']}, "
                 f"division {division_to_text(view['division'])}, "
                 f"port {port_text}"]
        for n in sorted(seqs):
            seq = seqs[n]
            if view["type"] == "cc":
                notes_text = describe_cc_values(
                    seq, self.project.get('lfos', {}), view['bpm'],
                    time.monotonic())
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
        color_used = False
        for n in sorted(phrases):
            line = f"    phrase-{n}: {phrases[n]}"
            if (track, pattern, n) in running:
                line = paint_green(line)
                color_used = True
            block.append(line)
        return block, color_used

    def do_show(self, arg):
        "Show the configuration tree, or a path: show [t<n>[p<m>[s<k>|f<k>]]]"
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
                print("       show t<n>p<m>s<k>|f<k>|v<k>|sus<k>|mt<k>  (one leaf)")
                return

        running = {(t, p, ph) for (t, p, ph), pl in self.playback.items() if pl.running}

        # Leaf views -----------------------------------------------------
        if selected and selected["kind"] in ("s", "f", "v", "u", "m"):
            track, pattern = selected["track"], selected["pattern"]
            if track not in tracks or pattern not in tracks[track]:
                print(f"\n(nothing configured for t{track}p{pattern})\n")
                return
            view = self._pattern_view(track, pattern)
            if view["type"] == "cc":
                desc = f"type CC{view['controller']}, values 0-127"
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
                        time.monotonic())
                else:
                    notes_text = resolve_seq_notes(
                        seq, view['root'], view['scale'],
                        self.project.get('lfos', {}), view['bpm'],
                        time.monotonic())
                print(f"  seq{n}: {seq_to_text(seq):<28} -> {notes_text}")
            else:
                if n not in view["phrases"]:
                    print(f"\nphrase-{n} of t{track}p{pattern} is not configured.\n")
                    return
                line = f"  phrase-{n}: {view['phrases'][n]}"
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
            built = self._pattern_block(track, pattern, running)
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
                built = self._pattern_block(track, pattern, running)
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
        print(f"  {'select-port':<20} - choose the MIDI output port for playback")
        print(f"  {'bpm [<value>]':<20} - show or set the global BPM (default 120)")
        print(f"  {'t<n>':<20} - enter a track menu (n = 1-16)")
        print(f"  {'t<n>p<m>':<20} - enter a pattern menu (e.g. t1p1)")
        print(f"  {'t<n>p<m>s<k> [deg...]':<20} - set a sequence (s0..s9) e.g. t1p1s1 0 0 O1")
        print(f"  {'t<n>p<m>f<k> <expr>':<20} - define a phrase (f1..f16) e.g. t1p1f1 inf*s0")
        print(f"  {'t<n>p<m> scale|root|bpm|channel':<20} - edit a pattern setting")
        print(f"  {'t<n>p<m> division 1/16':<20} - step note value (1, 1/2, 1/3, 1/16 ...)")
        print(f"  {'t<n>p<m> type note|CC':<20} - pattern kind (default note)")
        print(f"  {'show [t<n>[p<m>[s<k>|f<k>|v<k>]]]':<20} - config tree or one leaf")
        print(f"  {'<path> ...':<20} - edit by path, e.g. t1p1s0 0 0 5, "
              f"t1p1v0 64+0.4*lfo1, t1p1sus0 0.5 or t1p1mt0 -0.05")
        print(f"  {'start t<n>p<m>f<k>':<20} - play a phrase (e.g. start t1p1f1)")
        print(f"  {'stop t<n>p<m>f<k>':<20} - stop a phrase (e.g. stop t1p1f1)")
        print(f"  {'lfo <n>':<20} - enter an LFO definition menu (n = 1-8)")
        print(f"  {'lfo<n> start|stop':<20} - start or stop an LFO from here (e.g. lfo3 start)")
        print(f"  {'status-lfo':<20} - show the parameters of all started LFOs")
        print(f"  {'cp <src> <dst>':<20} - copy a track, pattern, sequence or phrase")
        print(f"  {'rm <path>|lfo<n>':<20} - delete a track/pattern/sequence/phrase, reset an LFO or a setting")
        print(f"  {'panic':<20} - stop all phrases and silence all MIDI outputs")
        print(f"  {'save [<file>]':<20} - save the whole configuration to a .cfg file")
        print(f"  {'load [<file>]':<20} - load the whole configuration from a .cfg file")
        print(f"  {'help':<20} - show this help (help <command> for details)")
        print(f"  {'exit':<20} - exit the shell")
        print()
        if self.out_port:
            print(f"  Output port: {self.out_port}")
            print()
        print("Context:\n  Tracks hold 16 patterns each; patterns hold sequences\n"
              "  (s0..s9) and phrases (f1..f16). Shortcuts: t=track, p=pattern,\n"
              "  s=sequence, f=phrase.")
        print()

    def _stop_all_playback(self):
        """Stop every running phrase (used when leaving the program)."""
        for key, pl in list(self.playback.items()):
            try:
                pl.stop()
            except Exception:
                pass
            self.playback.pop(key, None)

    def panic(self):
        """Stop all phrases and send MIDI All Sound Off / All Notes Off."""
        ports = {self.out_port} if self.out_port else set()
        ports.update(pl.port_name for pl in self.playback.values())
        count = len(self.playback)
        self._stop_all_playback()
        sent = player.panic_ports(ports)
        if count or sent:
            print(f"Panic: stopped {count} phrase{'s' if count != 1 else ''}; "
                  f"sent All Sound Off on {len(sent)} port"
                  f"{'s' if len(sent) != 1 else ''}.")
        else:
            print("Panic: nothing playing.")

    def do_panic(self, arg):
        "Stop all phrases and silence all MIDI outputs"
        self.panic()

    def do_cp(self, arg):
        "Copy a track, pattern, sequence or phrase: cp <source> <destination>"
        parts = (arg or "").split()
        if len(parts) != 2:
            print("Usage: cp <source> <destination>")
            print("       cp t1 t2             copy a whole track")
            print("       cp t1p1 t3p4         copy a pattern")
            print("       cp t1p1s1 t3p4s4     copy a sequence")
            print("       cp t1p1f1 t3p4f3     copy a phrase")
            print("       cp t1p1v1 t3p4v4     copy a velocity")
            print("       cp t1p1sus1 t3p4sus4 copy a sustain")
            print("       cp t1p1mt1 t3p4mt4     copy a microtiming")
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
            if src["track"] == dst["track"]:
                print("Error: source and destination are the same.")
                return
            source = tracks.get(src["track"])
            if not source:
                print(f"Error: track {src['track']} has no configuration to copy.")
                return
            tracks[dst["track"]] = copy.deepcopy(source)
            print(f"Copied track t{src['track']} -> t{dst['track']} "
                  f"({len(source)} pattern{'s' if len(source) != 1 else ''}).")
            return

        # --- pattern ----------------------------------------------------
        if src["kind"] is None and dst["kind"] is None:
            if src["pattern"] is None or dst["pattern"] is None:
                print("Error: source and destination must both be tracks, both "
                      "patterns, both sequences or both phrases.")
                return
            if (src["track"], src["pattern"]) == (dst["track"], dst["pattern"]):
                print("Error: source and destination are the same.")
                return
            entry = tracks.get(src["track"], {}).get(src["pattern"])
            if not entry:
                print(f"Error: pattern t{src['track']}p{src['pattern']} "
                      f"is not configured.")
                return
            tracks.setdefault(dst["track"], {})[dst["pattern"]] = \
                copy.deepcopy(entry)
            print(f"Copied pattern t{src['track']}p{src['pattern']} -> "
                  f"t{dst['track']}p{dst['pattern']}.")
            return

        # --- sequence / phrase ------------------------------------------
        if src["kind"] != dst["kind"] or src["kind"] not in ("s", "f", "v", "u", "m"):
            print("Error: source and destination must both be tracks, both "
                  "patterns, both sequences, both phrases, both velocities, "
                  "both sustains or both microtimings.")
            return
        if (src["track"], src["pattern"], src["kind"], src["index"]) == \
                (dst["track"], dst["pattern"], dst["kind"], dst["index"]):
            print("Error: source and destination are the same.")
            return
        bucket = {"s": "seqs", "f": "phrases", "v": "velocities",
                  "u": "sustains", "m": "microtimes"}[src["kind"]]
        label = f"{src['kind']}{src['index']}"
        source_entry = tracks.get(src["track"], {}).get(src["pattern"], {})
        if src["index"] not in source_entry.get(bucket, {}):
            print(f"Error: {label} of t{src['track']}p{src['pattern']} "
                  f"is not configured.")
            return
        dest_entry = tracks.setdefault(dst["track"], {}).setdefault(
            dst["pattern"], {})
        if not dest_entry.get("scale"):
            dest_entry.setdefault("scale", "chromatic")
            dest_entry.setdefault("root", "C4")
            dest_entry.setdefault("channel", DEFAULT_CHANNEL)
            dest_entry.setdefault("bpm", self.bpm)
            dest_entry.setdefault("bpm-inherited", True)
        dest_entry.setdefault(bucket, {})[dst["index"]] = \
            copy.deepcopy(source_entry[bucket][src["index"]])
        kind_name = {"s": "sequence", "f": "phrase", "v": "velocity",
                     "u": "sustain", "m": "microtiming"}[src["kind"]]
        shown = label if src["kind"] not in ("u", "m") else (
            f"sus{src['index']}" if src["kind"] == "u" else f"mt{src['index']}")
        dest_shown = (f"{dst['kind']}{dst['index']}" if dst["kind"] not in ("u", "m")
                      else (f"sus{dst['index']}" if dst["kind"] == "u"
                            else f"mt{dst['index']}"))
        print(f"Copied {kind_name} t{src['track']}p{src['pattern']}{shown} -> "
              f"t{dst['track']}p{dst['pattern']}{dest_shown}.")

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
                    "port": ["port"], "select-port": ["port"]}
            if verb not in keys:
                print(f"Error: unknown setting '{verb}'. Use scale, root, bpm, "
                      f"channel or port.")
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
                        "select-port": "inherits the global port"}
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
        else:
            del tracks[track][pattern]
            self._prune_pattern(tracks, track, pattern)
            print(f"Removed pattern t{track}p{pattern}.")

    def do_exit(self, arg):
        "Exit the shell"
        self._stop_all_playback()
        print("Goodbye!")
        return True

    def do_EOF(self, arg):
        self._stop_all_playback()
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
            m = re.fullmatch(r"(?i)([sfv])(\d+)", first_word)
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
            if not 1 <= index <= _PATH_MAX_PHRASE:
                raise ValueError(f"phrase must be f1..f{_PATH_MAX_PHRASE}")
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
        except ValueError:
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
        "Play a phrase of this track, or by full path: start p<m>f<k> / t<n>p<m>f<k>"
        try:
            path = parse_compact_path(arg, track=self.track_number)
            if path["kind"] != "f":
                raise ValueError("use start p<m>f<k> (e.g. start p1f1)")
        except ValueError as e:
            print(f"Error: {e}")
            return
        self._root_shell().launch_phrase(path["track"], path["pattern"],
                                         path["index"])

    def do_stop(self, arg):
        "Stop a phrase of this track, or by full path: stop p<m>f<k> / t<n>p<m>f<k>"
        try:
            path = parse_compact_path(arg, track=self.track_number)
            if path["kind"] != "f":
                raise ValueError("use stop p<m>f<k> (e.g. stop p1f1)")
        except ValueError as e:
            print(f"Error: {e}")
            return
        self._root_shell().stop_phrase(path["track"], path["pattern"],
                                       path["index"])

    def do_panic(self, arg):
        "Stop all phrases and silence all MIDI outputs"
        self._root_shell().panic()

    def do_help(self, arg):
        if arg:
            return super().do_help(arg.strip().replace("-", "_"))
        print(f"\nTrack {self.track_number} Commands:")
        print(f"  {'p<m>':<18} - enter the menu of a pattern (m = 1-16)")
        print(f"  {'p<m>s<k> [deg...]':<18} - set a sequence of that pattern (s0..s9)")
        print(f"  {'p<m>f<k> <expr>':<18} - define a phrase of that pattern (f1..f16)")
        print(f"  {'p<m> scale|root|bpm|channel':<18} - edit that pattern setting")
        print(f"  {'p<m> division 1/16':<18} - step note value (1, 1/2, 1/3, 1/16 ...)")
        print(f"  {'p<m> type note|CC':<18} - pattern kind (default note)")
        print(f"  {'p<m> controller <n>':<18} - CC number 0-127 for a CC pattern")
        print(f"  {'show p<m>[s<k>|f<k>]':<18} - show a pattern/sequence/phrase")
        print(f"  {'start p<m>f<k>':<18} - play a phrase of this track")
        print(f"  {'stop p<m>f<k>':<18} - stop a phrase of this track")
        print(f"  {'t<n>':<18} - switch to another track menu")
        print(f"  {'cp <src> <dst>':<18} - copy a track, pattern, sequence or phrase")
        print(f"  {'rm <path> [setting]':<18} - delete/zeroize a track, pattern, sequence, phrase or setting")
        print(f"  {'panic':<18} - stop all phrases and silence all MIDI outputs")
        print(f"  {'/':<18} - return to the main menu from anywhere")
        print(f"  {'/ <command>':<18} - run a main-menu command and return to the top menu")
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
            # '/ <root command>' runs at the root and returns to the top menu.
            self.run_root_command(command)
            self.request_root()
            return True
        token, rest = parts[0], text[len(parts[0]):].strip()
        if token.lower().startswith("lfo"):  # jump to an LFO menu from here
            self._root_shell().onecmd(text)
            return True if self.propagate_root_request() else None
        if token.lower() in ("cp", "rm"):  # copy / remove by path
            self._root_shell().onecmd(text)
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
        except ValueError:
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
    def _meter(self, v):
        """Single-line level meter around a fixed center point."""
        half = 12
        cells = ["\u00b7"] * (2 * half + 1)  # midpoint dots
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
        prefix = f"{self._meter(v)} {v:+.2f}  "
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
                frame = (f"\r\x1b[2K{self._meter(v)} {v:+.2f}  {self.prompt}")
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
        "Stop all phrases and silence all MIDI outputs"
        self.parent_shell.panic()

    def do_help(self, arg):
        if arg:
            return super().do_help(arg.strip().replace("-", "_"))
        print(f"\nLFO {self.lfo_number} Commands:")
        print(f"  {'frequency [<hz>|expr]':<22} - Hz or a tempo multiple, e.g. 0.5, 2bpm, freq 2bpm")
        print(f"  {'shape [<name>]':<20} - show or set the shape: sin, tri, saw, square")
        print(f"  {'phase [<-1..1>]':<20} - start point of the waveform (-1 to 1)")
        print(f"  {'show':<20} - show all parameters of this LFO")
        print(f"  {'rm [lfo<n>] [param]':<20} - reset this LFO (or another, or one parameter)")
        print(f"  {'start':<20} - start this LFO (stopped by default)")
        print(f"  {'stop':<20} - stop this LFO")
        print(f"  {'live':<20} - show the live instantaneous value next to the prompt")
        print(f"  {'live stop':<20} - stop the live view")
        print(f"  {'cp <src> <dst>':<20} - copy a track, pattern, sequence or phrase")
        print(f"  {'rm <path>|lfo<n>':<20} - delete a track/pattern/sequence/phrase, reset an LFO or a setting")
        print(f"  {'panic':<20} - stop all phrases and silence all MIDI outputs")
        print(f"  {'/':<20} - return to the main menu from anywhere")
        print(f"  {'/ <command>':<20} - run a main-menu command and return to the top menu")
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
            self.request_root()
            return True
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
        self.pattern_type = "note"  # "note" (default) or "cc"
        self.controller = 1  # CC number used by cc patterns
        self._live_block = []   # histogram lines drawn above the prompt
        self._hist_phrase = None
        self._hist_cached = None
        self._hist_last = 0.0
        self._hist_spin = 0
        self._hist_spin_last = 0.0
        self.division = DEFAULT_DIVISIONS  # step = 1/division note
        self.channel = DEFAULT_CHANNEL
        # BPM defaults to the global value; 'bpm' in this menu overrides it locally.
        self.bpm = getattr(self._root_shell(), "bpm", DEFAULT_BPM)
        self.bpm_inherited = True
        # MIDI output port: None means inherit the global selection.
        self.out_port = None

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
        })

    def _switch_pattern(self, n):
        # Navigating between patterns does not stop playback: any phrase that
        # was started keeps playing in the background.
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
                print(f"phrase-{index}: {self.phrases[index]}")
            else:
                print(f"phrase-{index}: not configured. "
                      f"Usage: f{index} <expression>  e.g. f{index} 4*(s0+2*s1)")
            return False
        try:
            canonical = phrases.parse_phrase_expr(expr_text)
        except phrases.PhraseError as e:
            print(f"Error: {e}")
            return False
        self.phrases[index] = canonical
        self._commit()
        print(f"Phrase {index} set: {canonical}{self._live_hint()}")
        return True

    def _hist_target(self, arg):
        """Pick what to render: (phrase_index, expr, label).

        Explicit 'f<k>' or 's<k>'; otherwise the phrase currently playing for
        this pattern, else the lowest configured phrase.
        """
        text = (arg or "").strip()
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
        return histogram_lines(self._hist_view(), root.project.get("lfos", {}),
                               self.bpm, time.monotonic(), phrase_index,
                               live=live, expr=expr, label=label,
                               playhead=self._hist_playhead(), spin=spin)

    def do_hist(self, arg):
        "Show a per-step histogram of a phrase: hist [f<k>|s<k>] [live|once|stop]"
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
        if text and not re.fullmatch(r"(?i)[fs]\d+", text):
            print("Usage: hist [f<k>|s<k>] [live|stop]   e.g. hist, hist f1 live")
            return
        lines = self._hist_lines(phrase, live=live, expr=expr, label=label)
        if live:
            if not sys.stdout.isatty():
                print("Live histogram requires an interactive terminal.")
                print("\n".join(lines))
                return
            _enable_ansi()
            self._hist_phrase = phrase
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

    def do_type(self, arg):
        "Show or set the pattern type: note (default) or CC"
        text = (arg or "").strip().lower()
        if not text:
            extra = (f" (controller {self.controller})"
                     if self.pattern_type == "cc" else "")
            print(f"Type: {'CC' if self.pattern_type == 'cc' else 'note'}{extra}")
            return
        if text in ("note", "notes"):
            self.pattern_type = "note"
        elif text in ("cc", "control", "controller-change"):
            self.pattern_type = "cc"
        else:
            print(f"Error: unknown type '{arg.strip()}'. Use 'note' or 'CC'.")
            return
        self._commit()
        if self.pattern_type == "cc":
            print(f"Type set to CC (controller {self.controller}); "
                  f"sequences hold raw values 0..127")
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
        elif verb in ("hist", "histogram"):
            self.do_hist(arg)
        else:
            raise ValueError(f"unknown pattern setting '{verb}' (use scale, "
                             f"root, bpm, channel, division, type, controller, "
                             f"hist, select-port, s<k> or f<k>)")

    def do_start(self, arg):
        "Play a phrase: start f<k>, or by path t<n>p<m>f<k> from anywhere"
        try:
            path = parse_compact_path(arg, track=self._track_number(),
                                      pattern=self.pattern_number)
            if path["kind"] != "f":
                raise ValueError("use start f<k> (e.g. start f1)")
        except ValueError as e:
            print(f"Error: {e}")
            return
        root = self._root_shell()
        self._player = root.launch_phrase(path["track"], path["pattern"],
                                          path["index"])

    def do_stop(self, arg):
        "Stop a phrase: stop [f<k>] or by path t<n>p<m>f<k>; no arg stops this pattern"
        text = (arg or "").strip()
        root = self._root_shell()
        if text:
            try:
                path = parse_compact_path(text, track=self._track_number(),
                                          pattern=self.pattern_number)
                if path["kind"] != "f":
                    raise ValueError("use stop f<k>")
            except ValueError as e:
                print(f"Error: {e}")
                return
            root.stop_phrase(path["track"], path["pattern"], path["index"])
        else:
            root.stop_pattern(self._track_number(), self.pattern_number)
        self._player = None

    def do_scale(self, arg):
        "Show or set the pattern scale (default: chromatic). 'scale ?' lists all known scales"
        name = (arg or "").strip()
        if name == "?":
            import textwrap
            print("Known scales (aliases accepted):")
            print(textwrap.fill(" ".join(scales.scale_choices()), width=80,
                                initial_indent="  ", subsequent_indent="  "))
            return
        if not name:
            print(f"Scale: {self._key_line()}")
            return
        key = scales.resolve_scale(name)
        if key is None:
            print(f"Error: unknown scale '{name}'.")
            print("Hint: type 'scale ?' to list all known scales.")
            return
        self.scale = key
        self._commit()
        print(f"Scale set: {self._key_line()}{self._live_hint()}")

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
            return describe_cc_values(seq, lfos, self.bpm, time.monotonic())
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
                print(f"  Degrees index the {self.scale} scale (0 = root); "
                      f"'r' is a rest.")
                print("  Prefix O = next octave up, o = next octave down (e.g. O0, o3).")
            print("  LFO variation: <degree>*<amp>*lfo<N>[*lfo<M>...] e.g. "
                  "0*5*lfo1 or 0*5*lfo1*lfo2")
            print("     moves round(amp x lfo1 x lfo2 x ...) scale steps "
                  "(octaves wrap; CC values clamp to 0..127).")
            return False
        parsed = []
        max_degree = 127 if cc else len(scales.SCALES[self.scale]) - 1
        for tok in tokens:
            try:
                entry = seq_token_entry(tok)
            except ValueError:
                what = ("a value 0..127" if cc else
                        "a scale degree like 0, an octave prefixed one like O0 / o3")
                print(f"Error: invalid token '{tok}'. Use {what}, 'r' for a rest, "
                      f"or an LFO variation like 0*5*lfo1*lfo2.")
                return False
            if entry is None:
                parsed.append(None)  # rest
                continue
            degree, shift, variation = entry
            if degree > max_degree:
                if cc:
                    print(f"Error: value {degree} is out of range for a CC "
                          f"pattern (values 0-127).")
                else:
                    print(f"Error: degree {degree} is out of range for the "
                          f"{self.scale} scale (degrees 0-{max_degree}). "
                          f"Use O/o to move octaves.")
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
        print(f"v{index} set: {canonical}   (now {preview})")
        return True

    def show_velocity(self, n):
        """Print one stored velocity expression."""
        if n in self.velocities:
            print(f"v{n}: {self.velocities[n]}")
        else:
            print(f"v{n}: not configured. Usage: v{n} <value|expr>  "
                  f"e.g. v{n} 45 or v{n} 64+0.4*lfo1")

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
            print(f"       Usage: sus{index} <value|expr>   e.g. sus{index} 0.5 "
                  f"or sus{index} 0.5+0.5*lfo1  (clamped to 0..1 when playing)")
            return False
        self.sustains[index] = canonical
        self._commit()
        now = velocity.evaluate_fraction(canonical,
                                         self._root_shell().project.get("lfos", {}),
                                         self.bpm, time.monotonic())
        print(f"sus{index} set: {canonical}   (now {now:g} of the step)")
        return True

    def show_sustain(self, n):
        """Print one stored sustain expression."""
        if n in self.sustains:
            print(f"sus{n}: {self.sustains[n]}")
        else:
            print(f"sus{n}: not configured. Usage: sus{n} <value|expr>  "
                  f"e.g. sus{n} 0.5 (default is {player.SUSTAIN_FRACTION:g})")

    def set_microtime(self, index, text, via_path=False):
        """Set microtiming mt<index>: signed share of the step (-0.5..0.5)."""
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
            print(f"       Usage: mt{index} <value|expr>   e.g. mt{index} -0.05 "
                  f"or mt{index} 0.01*lfo1  (clamped to -0.5..0.5 of the step)")
            return False
        self.microtimes[index] = canonical
        self._commit()
        now = velocity.evaluate_span(canonical,
                                     self._root_shell().project.get("lfos", {}),
                                     self.bpm, time.monotonic(), -0.5, 0.5)
        step = division_step_ms(self.bpm, self.division)
        print(f"mt{index} set: {canonical}   (now {now:+.4g} of the step = "
              f"{now * step:+.1f} ms)")
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
        print(f"\nMicrotiming (pattern {self.pattern_number}, division "
              f"{division_to_text(self.division)}, bpm {self.bpm}):")
        if not self.microtimes:
            print("  (none configured - notes land exactly on the grid)")
            print()
            return
        step = division_step_ms(self.bpm, self.division)
        for n in sorted(self.microtimes):
            expr = self.microtimes[n]
            now = velocity.evaluate_span(expr,
                                         self._root_shell().project.get("lfos", {}),
                                         self.bpm, time.monotonic(), -0.5, 0.5)
            print(f"  mt{n}: {expr}   (now {now:+.4g} = {now * step:+.1f} ms)")
        print()

    def do_list_sus(self, arg):
        "List all configured sustain sequences of this pattern (sus0..sus9)"
        print(f"\nSustains (pattern {self.pattern_number}, bpm {self.bpm}):")
        if not self.sustains:
            print(f"  (none configured - notes are held "
                  f"{player.SUSTAIN_FRACTION:g} of the step)")
            print()
            return
        for n in sorted(self.sustains):
            print(f"  sus{n}: {self.sustains[n]}")
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
        "List all configured phrases of this pattern (f1..f16)"
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
        names += [f"f{i}" for i in range(1, MAX_PHRASES + 1)
                  if f"f{i}".startswith(text)]
        names += [f"p{i}" for i in range(1, MAX_PATTERNS + 1)
                  if f"p{i}".startswith(text)]
        return names

    def do_panic(self, arg):
        "Stop all phrases and silence all MIDI outputs"
        self._root_shell().panic()

    def do_help(self, arg):
        if arg:
            return super().do_help(arg.strip().replace("-", "_"))
        print(f"\nPattern {self.pattern_number} Commands:")
        print(f"  {'s0..s9 [deg...]':<20} - set a sequence of scale degrees, e.g. s0 0 0 r 3")
        print(f"  {'':<20}   in a CC pattern the values are raw controller data 0..127")
        print(f"  {'':<20}   O/o prefixes shift octaves up/down: O0 = root +1 oct, o3 = degree 3 -1 oct")
        print(f"  {'':<20}   'r' inserts a rest")
        print(f"  {'s<k>':<20} - show one sequence of this pattern")
        print(f"  {'f1..f16 <expr>':<20} - define a phrase, e.g. f1 4*(s0+2*s1) or inf*s0")
        print(f"  {'f<k>':<20} - show one phrase of this pattern")
        print(f"  {'v<k> <expr>':<20} - velocity of sequence k (v0..v9): 45 or 64+0.4*lfo1")
        print(f"  {'':<20}   'v<k>=<expr>' is accepted too; values are clamped to 0..127")
        print(f"  {'sus<k> <expr>':<20} - sustain share of the step (sus0..sus9): 0.5 or 0.5+0.5*lfo1")
        print(f"  {'':<20}   'sus<k>=<expr>' works too; clamped to 0..1 of the step")
        print(f"  {'list-s':<20} - list all configured sequences")
        print(f"  {'list-f':<20} - list all configured phrases")
        print(f"  {'list-v':<20} - list all configured velocities")
        print(f"  {'mt<k> <expr>':<20} - microtiming of sequence k (mt0..mt9): -0.05 or 0.01*lfo1")
        print(f"  {'':<20}   shifts notes within the step; clamped to -0.5..0.5")
        print(f"  {'list-sus':<20} - list all configured sustains")
        print(f"  {'list-mt':<20} - list all configured microtimings")
        print(f"  {'hist [f<k>|s<k>] [live]':<20} - per-step histogram; 'live' refreshes it (hist stop ends)")
        print(f"  {'start f<k>':<20} - play phrase k to the MIDI output (velocity 100)")
        print(f"  {'stop [f<k>]':<20} - stop the running phrase playback")
        print(f"  {'scale [<name>]':<20} - show or set the pattern scale")
        print(f"  {'root [<note>]':<20} - show or set the root note (e.g. F#3)")
        print(f"  {'channel [<n>]':<20} - set this pattern's MIDI channel (default: 1)")
        print(f"  {'select-port':<20} - set this pattern's MIDI output port")
        print(f"  {'bpm [<value>]':<20} - set this pattern's BPM (default: inherits global)")
        print(f"  {'division [1/16]':<20} - step note value (1, 1/2, 1/3, 1/16 ...)")
        print(f"  {'type [note|CC]':<20} - pattern kind (default: note); CC sends controller data")
        print(f"  {'controller [<n>]':<20} - CC number 0-127 used by a CC pattern")
        print(f"  {'p<m>':<20} - switch to another pattern (m = 1-16)")
        print(f"  {'t<n>':<20} - switch to another track menu")
        print(f"  {'cp <src> <dst>':<20} - copy a track, pattern, sequence or phrase")
        print(f"  {'rm <path>|lfo<n>':<20} - delete a track/pattern/sequence/phrase, reset an LFO or a setting")
        print(f"  {'panic':<20} - stop all phrases and silence all MIDI outputs")
        print(f"  {'/':<20} - return to the main menu from anywhere")
        print(f"  {'/ <command>':<20} - run a main-menu command and return to the top menu")
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
            self.request_root()
            return True
        token, rest = parts[0], text[len(parts[0]):].strip()
        low = token.lower()
        if low.startswith("lfo"):  # jump to an LFO menu from here
            self._root_shell().onecmd(text)
            return True if self.propagate_root_request() else None
        if low in ("cp", "rm"):  # copy / remove by path
            self._root_shell().onecmd(text)
            return
        if low == "show":
            self._root_shell().do_show(f"t{self._track_number()}p"
                                       f"{self.pattern_number}{rest}")
            return
        # s<k> [degrees] / f<k> [expression] / v<k> [=][expression]
        name, rest = ((token.split("=", 1)[0],
                       (token.split("=", 1)[1] + " " + rest).strip())
                      if "=" in token else (token, rest))
        m = re.fullmatch(r"(?i)([sfv])(\d+)", name)
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
            if kind == "s":
                if not 0 <= index <= _PATH_MAX_SEQ:
                    print(f"Error: sequence must be s0..s{_PATH_MAX_SEQ}")
                    return
                if rest:
                    self.set_sequence(index, rest.split())
                else:
                    self.show_sequence(index)
            else:
                if not 1 <= index <= _PATH_MAX_PHRASE:
                    print(f"Error: phrase must be f1..f{_PATH_MAX_PHRASE}")
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
        except ValueError:
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
