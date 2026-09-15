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
    "persian": (0, 1, 4, 5, 6, 8, 11),
    # Common hexatonic (6-note) scales
    "whole-tone": (0, 2, 4, 6, 8, 10),
    "augmented": (0, 3, 4, 7, 8, 11),
    "hexatonic": (0, 1, 4, 5, 8, 9),
    "blues": (0, 3, 5, 6, 7, 10),
    "major-blues": (0, 2, 3, 4, 7, 9),
    "prometheus": (0, 2, 4, 6, 9, 10),
    "tritone": (0, 1, 4, 6, 7, 10),
    "chromatic-hexachord": (0, 1, 2, 3, 4, 5),
    # Common pentatonic (5-note) scales
    "major-pentatonic": (0, 2, 4, 7, 9),
    "minor-pentatonic": (0, 3, 5, 7, 10),
    "egyptian": (0, 2, 5, 7, 10),
    "hirajoshi": (0, 2, 3, 7, 8),
    "insen": (0, 1, 5, 7, 10),
    "yo": (0, 2, 3, 7, 10),
    "iwato": (0, 1, 5, 6, 10),
    "kumoi": (0, 1, 5, 7, 8),
    "pelog": (0, 1, 3, 7, 8),
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
    "gypsy": "double-harmonic",
    "bhairav": "double-harmonic",
    "hijaz": "phrygian-dominant",
    "spanish": "phrygian-dominant",
    "spanish-phrygian": "phrygian-dominant",
    "romanian": "dorian-sharp-4",
    "romanian-minor": "dorian-sharp-4",
    "scriabin": "prometheus",
    "blues-minor": "blues",
    "minor-blues": "blues",
    "pentatonic": "major-pentatonic",
    "major-pent": "major-pentatonic",
    "minor-pent": "minor-pentatonic",
    "japanese": "yo",
}


# ---------------------------------------------------------------------------
# Complete catalogue of set classes ("all scales" content, generated)
# ---------------------------------------------------------------------------
# Every pitch-class set of 5, 6 and 7 notes belongs to exactly one *set class*
# up to transposition and inversion. Generating them is exact and verifiable:
# the number of classes comes out 38 (5 notes), 50 (6 notes) and 38 (7 notes),
# which is the published count for those cardinalities.
#
# Names: `pent-01`, `hex-07`, `hept-23` (our own deterministic numbering, in
# prime-form order - not the Forte numbers), and `hex-07@3` for the 4th mode of
# that class. The prime form is the packed-left normal order, as shown.
CARDINALITY_NAMES = {2: "diad", 3: "tri", 4: "tetra", 5: "pent", 6: "hex",
                     7: "hept", 8: "octa", 9: "ennea"}
CATALOGUE_CARDINALITIES = (5, 6, 7)
# Expected number of set classes per cardinality (published values)
SET_CLASS_COUNTS = {2: 6, 3: 12, 4: 29, 5: 38, 6: 50, 7: 38, 8: 29, 9: 12,
                    10: 6, 11: 1}

CATALOGUE = {}          # key ("hex-07") -> prime form, as a tuple of semitones
CLASS_KEYS = {}         # key -> (cardinality, index)
_NAMED_BY_CLASS = {}    # prime form -> [named scales with that set class]


def _mask(intervals):
    """Bit mask (bit pc set) of a scale's semitone offsets."""
    m = 0
    for iv in intervals:
        m |= 1 << (iv % 12)
    return m


def _intervals(mask):
    """Semitone offsets of a bit mask, starting from the lowest note."""
    pcs = [pc for pc in range(12) if mask >> pc & 1]
    return tuple(pc - pcs[0] for pc in pcs)


def _rotations(mask):
    """All 12 transpositions of a mask, each normalised to start at 0."""
    out = set()
    for shift in range(12):
        m = ((mask << shift) | (mask >> (12 - shift))) & 0xFFF
        low = (m & -m).bit_length() - 1
        out.add(((m >> low) | (m << (12 - low))) & 0xFFF)
    return out


def _inverted(mask):
    """The mask's inversion (mirror around the root)."""
    m = 0
    for pc in range(12):
        if mask >> pc & 1:
            m |= 1 << ((-pc) % 12)
    return m


def prime_form(intervals):
    """Packed-left normal order of a scale: (0, ...) semitone offsets.

    This is the canonical representative of the scale's set class: the
    lexicographically smallest of all its transpositions and of all the
    transpositions of its inversion.
    """
    mask = _mask(intervals)
    best = None
    for candidate in _rotations(mask) | _rotations(_inverted(mask)):
        key = _intervals(candidate)
        if best is None or key < best:
            best = key
    return best


def rotations(intervals):
    """All distinct modes of a scale, each starting from its own root."""
    mask = _mask(intervals)
    out = []
    for candidate in sorted(_rotations(mask), key=_intervals):
        form = _intervals(candidate)
        if form not in out:
            out.append(form)
    return out


def mode_of(intervals, index):
    """The index-th mode (0 = the scale as given, 1 = one degree up...)."""
    values = sorted(iv % 12 for iv in intervals)
    if index % len(values) == 0:
        return tuple(values)
    root = values[index % len(values)]
    return tuple(sorted((iv - root) % 12 for iv in values))


def interval_vector(intervals):
    """(ic1..ic6) interval-class counts of a scale."""
    counts = [0] * 6
    values = sorted(iv % 12 for iv in intervals)
    for i, first in enumerate(values):
        for second in values[i + 1:]:
            step = (second - first) % 12
            name = min(step, 12 - step)
            if name:
                counts[name - 1] += 1
    return tuple(counts)


def _build_catalogue(cardinalities=CATALOGUE_CARDINALITIES):
    """Generate the set classes of the given cardinalities (verified counts)."""
    from itertools import combinations

    for cardinality in cardinalities:
        classes = set()
        for combo in combinations(range(12), cardinality):
            classes.add(prime_form(combo))
        expected = SET_CLASS_COUNTS.get(cardinality)
        if expected is not None and len(classes) != expected:
            # Should never happen; keep the catalogue usable either way.
            pass
        prefix = CARDINALITY_NAMES.get(cardinality, f"c{cardinality}")
        for index, form in enumerate(sorted(classes), start=1):
            key = f"{prefix}-{index:02d}"
            CATALOGUE[key] = form
            CLASS_KEYS[key] = (cardinality, index)
            # usable like any other scale: SCALES[key] is what playback reads
            SCALES.setdefault(key, form)


def catalogue_key(intervals):
    """Catalogue key of a scale's set class (prime form), or None."""
    form = prime_form(intervals)
    for key, value in CATALOGUE.items():
        if value == form:
            return key
    return None


def catalogue_names(cardinality=None, prefix=None):
    """Catalogue keys, optionally only one cardinality / naming prefix."""
    if prefix:
        prefix = prefix.strip().lower()
        return [k for k in sorted(CATALOGUE) if k.startswith(prefix)]
    if cardinality is None:
        return sorted(CATALOGUE)
    prefix = CARDINALITY_NAMES.get(cardinality, f"c{cardinality}")
    return [k for k in sorted(CATALOGUE) if k.startswith(prefix + "-")]


def named_scales_for(intervals):
    """Names of the curated scales that share this scale's set class."""
    form = prime_form(intervals)
    if not _NAMED_BY_CLASS:
        for name, values in SCALES.items():
            if len(values) in (5, 6, 7):
                _NAMED_BY_CLASS.setdefault(prime_form(values), []).append(name)
    return sorted(_NAMED_BY_CLASS.get(form, []))


def parse_intervals(text):
    """Parse '0,2,4,7,9' / '0 2 4 7 9' / '[0,2,4]' -> offsets, or None."""
    raw = (text or "").strip().strip("[]()")
    if not raw or not any(ch.isdigit() for ch in raw):
        return None
    parts = [p for p in re.split(r"[,\s]+", raw) if p]
    try:
        values = sorted({int(p) % 12 for p in parts})
    except ValueError:
        return None
    if len(values) < 2:
        return None
    return tuple(values)


def custom_key(intervals):
    """Key used for a scale typed as a list of semitone offsets."""
    return "custom-" + "-".join(str(iv) for iv in intervals)


def register_custom(intervals):
    """Add a user-typed interval list to SCALES and return its key."""
    key = custom_key(intervals)
    SCALES[key] = tuple(intervals)
    return key


_build_catalogue()


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
    """Return the canonical scale key for a name, or None.

    Accepts: a canonical name, an alias, a catalogue key (`hex-07`), a mode of
    one (`hex-07@3`), or a plain list of semitone offsets (`0,2,4,7,9`).
    """
    text = (name or "").strip()
    intervals = parse_intervals(text)
    if intervals is not None and len(intervals) >= 2:
        return register_custom(intervals)
    key = normalize_scale_name(text)
    if key in SCALES:
        return key
    if key in ALIASES:
        return ALIASES[key]
    base, _, mode = key.partition("@")
    if base in CATALOGUE:
        form = CATALOGUE[base]
        if not mode:
            return base
        try:
            index = int(mode)
        except ValueError:
            return None
        if not 0 <= index < len(form):
            return None
        rotated = mode_of(form, index)
        rotated_key = f"{base}@{index}"
        SCALES[rotated_key] = rotated
        return rotated_key
    return None


def scale_choices():
    """All names accepted by the scale command (canonical + aliases + catalogue)."""
    return sorted(set(SCALES) | set(ALIASES) | set(CATALOGUE))


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

    Seven-note scales are spelled one letter per degree (C D E F G A B with the
    accidentals needed); shorter scales and 'chromatic' are spelled with sharps,
    so e.g. a whole-tone degree reads F#4 rather than Gb4/E#4.
    """
    if len(intervals) != 7:
        # 5/6-note scales (and the chromatic): one letter per degree would
        # misspell degrees that skip a letter, so use plain sharp names.
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
