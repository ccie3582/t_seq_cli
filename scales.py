"""Note and scale data for the sequencer CLI.

Curated catalog of heptatonic (7-note) scales inspired by the listing at
https://www.allthescales.org/scales.php?n=7 (Ionian..Locrian, the harmonic /
melodic minor families and other common 7-note scales), plus a 'chromatic'
pseudo-scale (all 12 semitones) used as the default.

Notes use the 'Nm' convention: a note letter (A-G), an optional accidental
(# or b) and an octave number, e.g. C4, F#3, Bb2.
"""

import re

# Pitch classes in semitones from C.
CHROMATIC_SHARPS = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
LETTERS = ("C", "D", "E", "F", "G", "A", "B")
NATURALS = (0, 2, 4, 5, 7, 9, 11)  # semitone offset of each natural letter

# Canonical scale name -> semitone offsets from the root.
SCALES = {
    "chromatic": tuple(range(12)),
    # Major modes
    "ionian": (0, 2, 4, 5, 7, 9, 11),
    "dorian": (0, 2, 3, 5, 7, 9, 10),
    "phrygian": (0, 1, 3, 5, 7, 8, 10),
    "lydian": (0, 2, 4, 6, 7, 9, 11),
    "mixolydian": (0, 2, 4, 5, 7, 9, 10),
    "aeolian": (0, 2, 3, 5, 7, 8, 10),
    "locrian": (0, 1, 3, 5, 6, 8, 10),
    # Harmonic minor family
    "harmonic-minor": (0, 2, 3, 5, 7, 8, 11),
    "locrian-natural-6": (0, 1, 3, 5, 6, 9, 10),
    "ionian-sharp-5": (0, 2, 4, 5, 8, 9, 11),
    "dorian-sharp-4": (0, 2, 3, 6, 7, 9, 10),
    "phrygian-dominant": (0, 1, 4, 5, 7, 8, 10),
    "lydian-sharp-2": (0, 3, 4, 6, 7, 9, 11),
    # Melodic minor family
    "melodic-minor": (0, 2, 3, 5, 7, 9, 11),
    "dorian-b2": (0, 1, 3, 5, 7, 9, 10),
    "lydian-augmented": (0, 2, 4, 6, 8, 9, 11),
    "lydian-dominant": (0, 2, 4, 6, 7, 9, 10),
    "mixolydian-b6": (0, 2, 4, 5, 7, 8, 10),
    "locrian-natural-2": (0, 2, 3, 5, 6, 8, 10),
    "altered": (0, 1, 3, 4, 6, 8, 10),
    # Other common 7-note scales
    "harmonic-major": (0, 2, 4, 5, 7, 8, 11),
    "double-harmonic": (0, 1, 4, 5, 7, 8, 11),
    "hungarian-minor": (0, 2, 3, 6, 7, 8, 11),
    "neapolitan-major": (0, 1, 3, 5, 7, 9, 11),
    "neapolitan-minor": (0, 1, 3, 5, 7, 8, 11),
    "enigmatic": (0, 1, 4, 6, 8, 10, 11),
}

# Common synonyms -> canonical name.
ALIASES = {
    "major": "ionian",
    "minor": "aeolian",
    "natural-minor": "aeolian",
    "melodic-minor-ascending": "melodic-minor",
    "super-locrian": "altered",
    "ultralocrian": "altered",
    "byzantine": "double-harmonic",
    "double-harmonic-major": "double-harmonic",
}


class Note:
    """A parsed note: letter index (0-6), accidental (-1=b, 0, 1=#), octave, MIDI."""

    __slots__ = ("letter", "acc", "octave", "midi")

    def __init__(self, letter, acc, octave, midi):
        self.letter = letter
        self.acc = acc
        self.octave = octave
        self.midi = midi

    def name(self):
        acc = "#" if self.acc == 1 else ("b" if self.acc == -1 else "")
        return f"{LETTERS[self.letter]}{acc}{self.octave}"

    def __repr__(self):
        return f"Note({self.name()})"


def normalize_scale_name(name):
    """'Harmonic  Minor' / 'harmonic_minor' -> 'harmonic-minor'."""
    return (name or "").strip().lower().replace(" ", "-").replace("_", "-")


def resolve_scale(name):
    """Return the canonical scale key for a (possibly aliased) name or None."""
    key = normalize_scale_name(name)
    if key in SCALES:
        return key
    return ALIASES.get(key)


def scale_choices():
    """All names accepted by the scale command (canonical + aliases)."""
    return sorted(set(SCALES) | set(ALIASES))


_NOTE_RE = re.compile(r"^([A-Ga-g])([#b]?)(-?\d+)$")


def parse_note(text):
    """Parse 'Nm' notation (e.g. C4, F#3, Bb2) into a Note or None."""
    m = _NOTE_RE.fullmatch((text or "").strip())
    if not m:
        return None
    letter_idx = LETTERS.index(m.group(1).upper())
    acc = {"#": 1, "b": -1}.get(m.group(2), 0)
    octave = int(m.group(3))
    # Raw offset may be -1 (Cb) or 12 (B#), keeping octave boundaries correct.
    midi = (octave + 1) * 12 + NATURALS[letter_idx] + acc
    return Note(letter_idx, acc, octave, midi)


def scale_notes(root, intervals):
    """Return the spelled note names of a scale built on a root Note.

    One note per letter is assumed for 7-note scales (all catalog scales have
    seven distinct degrees). The 'chromatic' scale is spelled with sharps.
    """
    if len(intervals) == 12:  # chromatic
        names = []
        octave = root.octave
        prev_pc = None
        for step in intervals:
            pc = (root.midi % 12 + step) % 12
            if prev_pc is not None and pc <= prev_pc:
                octave += 1
            names.append(f"{CHROMATIC_SHARPS[pc]}{octave}")
            prev_pc = pc
        return names

    names = []
    for i, iv in enumerate(intervals):
        pc = (root.midi % 12 + iv) % 12
        letter_idx = (root.letter + i) % 7
        natural = NATURALS[letter_idx]
        diff = (pc - natural) % 12
        if diff == 0:
            acc = ""
        elif diff <= 6:
            acc = "#"
        else:
            acc = "b"
        octave = root.octave + (root.letter + i) // 7
        names.append(f"{LETTERS[letter_idx]}{acc}{octave}")
    return names
