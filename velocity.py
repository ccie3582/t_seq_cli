"""Note parameter sequences (velocity 0..127, sustain fraction 0..1):
constant or LFO-linked expressions.

Syntax (per sequence): a plain number (``45``), an LFO reference (``lfo1``),
a scaled LFO reference (``0.4*lfo1``), or a sum of such terms
(``64+0.4*lfo1``, ``100-0.5*lfo2``).

Velocity values are clamped to the MIDI range (>127 sends 127, <0 sends 0);
sustain values are fractions of the step, clamped to 0..1; microtiming values
are signed fractions of the step, clamped to -0.5..0.5.
"""

import re

import lfo

_NUM = r"\d+(?:\.\d+)?"
_TERM_LFO = re.compile(rf"^([+-]?)(?:({_NUM})\*)?lfo(\d+)$", re.IGNORECASE)
_TERM_NUM = re.compile(rf"^([+-]?)({_NUM})$")


def _term_hint(term):
    """Friendly message for a bad term, catching common LFO typos."""
    low = term.lower()
    if re.search(r"l\s*[of]{1,2}\s*\d", low) or low.startswith("lf"):
        m = re.fullmatch(r"([+-]?(?:\d+(?:\.\d+)?\*)?)l[of]{1,2}\s*(\d+)", low)
        example = f"{m.group(1) if m and m.group(1) else ''}lfo{m.group(2)}"             if m else "1*lfo1"
        return (f"invalid expression term '{term}' - did you mean "
                f"'{example}'?")
    return (f"invalid expression term '{term}' - use a number, lfo<N> or "
            f"coef*lfo<N> (e.g. 64+0.4*lfo1)")


def parse(text):
    """Parse a velocity expression into [(coefficient, lfo_index|None), ...]."""
    text = (text or "").strip()
    if not text:
        raise ValueError("empty velocity expression")
    cleaned = re.sub(r"\s+", "", text)
    terms = re.findall(r"[+-]?[^+-]+", cleaned)
    if not terms or "".join(terms) != cleaned:
        raise ValueError(f"invalid expression '{text}'")
    out = []
    for term in terms:
        m = _TERM_LFO.match(term)
        if m:
            sign = -1.0 if m.group(1) == "-" else 1.0
            coef = float(m.group(2)) if m.group(2) else 1.0
            out.append((sign * coef, int(m.group(3))))
            continue
        m = _TERM_NUM.match(term)
        if m:
            sign = -1.0 if m.group(1) == "-" else 1.0
            out.append((sign * float(m.group(2)), None))
            continue
        raise ValueError(_term_hint(term))
    return out


def canonical(text):
    """Canonical text form of a velocity expression."""
    parts = []
    for i, (coef, ref) in enumerate(parse(text)):
        sign = "-" if coef < 0 else ("+" if i else "")
        mag = abs(coef)
        body = f"{mag:g}*lfo{ref}" if ref is not None else f"{mag:g}"
        parts.append(sign + body)
    return "".join(parts)


def raw_value(text, lfos, bpm, t):
    """Unclamped expression value at time t."""
    total = 0.0
    for coef, ref in parse(text):
        if ref is None:
            total += coef
        else:
            total += coef * lfo.evaluate(lfos, ref, t, bpm)
    return total


def evaluate_span(text, lfos, bpm, t, low, high):
    """Value clamped to [low, high] (used for sustain / microtiming shares)."""
    return max(low, min(high, raw_value(text, lfos, bpm, t)))


def evaluate(text, lfos, bpm, t):
    """Compute the velocity at time t, clamped to 0..127 (integer)."""
    return max(0, min(127, int(round(raw_value(text, lfos, bpm, t)))))


def evaluate_fraction(text, lfos, bpm, t):
    """Compute a 0..1 fraction (e.g. a sustain share of the step) at time t."""
    return evaluate_span(text, lfos, bpm, t, 0.0, 1.0)
