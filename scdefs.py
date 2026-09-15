"""Catalogue of built-in SuperCollider SynthDefs (like FoxDot's _SynthDefs.py).

Each definition is generated from a shared control prologue and a shared output
pipeline, so **every** one of them accepts the parameters an osc pattern sends:

    freq  amp  dur  pan  atk  rel  cutoff  resonance  drive  delay  reverb

plus a few per-definition extras (ratio, index, detune, numharm, decay...).
The envelope frees the node when it ends (doneAction: 2), so nothing has to be
cleaned up per note. Only core UGens are used (no sc3-plugins needed).

`synthdef all` writes every one of these to `synthdefs/` (keeping files you have
edited) and sends them to sclang as a single file, so one message installs the
whole set; then each pattern picks the one it plays with `synth <name>`.
"""

# Controls shared by every SynthDef (all of them are used by the pipeline below)
SHARED = ("out = 0, freq = 440, amp = 0.4, dur = 0.5, pan = 0, atk = 0.01, "
          "rel = 0.25, cutoff = 4000, resonance = 0.5, drive = 1, delay = 0, "
          "reverb = 0")

# entry: name -> (description, body, extras)
#   body   SuperCollider expression producing the raw signal (uses freq/amp and
#          any of its extras)
#   extras tuple of "name = default" controls that only this definition has
DEFS = {
    "seq": (
        "saw + pulse through a resonant lowpass (the default)",
        "(Saw.ar(freq) * 0.4) + (Pulse.ar(freq, 0.5) * 0.2)",
        {},
    ),
    "bass": (
        "round sub bass: triangle plus saw, driven",
        "(LFTri.ar(freq) * 0.6) + (VarSaw.ar(freq * 0.5, width: 0.3) * 0.4)",
        {},
    ),
    "pad": (
        "soft detuned saw pad with a slow filter",
        "(Mix(VarSaw.ar(freq * [1, 1 + detune])) * 0.5) + "
        "(SinOsc.ar(freq * 2) * 0.1)",
        ("detune = 0.006",),
    ),
    "pluck": (
        "plucked string: an impulse into a tuned comb filter",
        "CombC.ar(Impulse.ar(0.001) + (Saw.ar(freq) * 0.1), 0.5, "
        "(1 / freq).clip(0.0005, 0.4), decay.clip(0.05, 6)) * 2",
        ("decay = 1.2",),
    ),
    "bell": (
        "FM bell with inharmonic partial and a long tail",
        "SinOsc.ar(freq + (SinOsc.ar(freq * ratio) * index * "
        "EnvGen.kr(Env.perc(0.001, decay.clip(0.05, 8)))))",
        ("ratio = 2.7565", "index = 3", "decay = 2"),
    ),
    "fm": (
        "two-operator FM voice",
        "SinOsc.ar(freq + (SinOsc.ar(freq * ratio) * index * "
        "EnvGen.kr(Env.perc(0.001, dur.clip(0.02, 8)))))",
        ("ratio = 2", "index = 2"),
    ),
    "blip": (
        "band-limited pulse train (Blip), short and bright",
        "Blip.ar(freq, numharm.clip(1, 40)) * 0.4",
        ("numharm = 8",),
    ),
    "noise": (
        "filtered noise burst, percussive",
        "WhiteNoise.ar * 0.7",
        {},
    ),
    "perc": (
        "percussion: noise click plus a pitching sine",
        "(WhiteNoise.ar * 0.4) + SinOsc.ar(XLine.kr(freq * 6, freq, "
        "dur.clip(0.02, 2)) * 0.8)",
        {},
    ),
    "glass": (
        "klank with inharmonic partials excited by noise",
        "Klank.ar([[1, 2.4, 3.9, 5.1, 7.7], [1, 0.6, 0.4, 0.25, 0.15], "
        "[1, 0.8, 0.6, 0.4, 0.3]], PinkNoise.ar * 0.02, freq) * 0.5",
        {},
    ),
    "growl": (
        "growling bass: FM index wobbling with a slow LFO",
        "(VarSaw.ar(freq, width: 0.5) * 0.6) + SinOsc.ar(freq + "
        "(SinOsc.ar(freq * ratio) * index))",
        ("ratio = 1.5", "index = 4"),
    ),
    "organ": (
        "additive organ stack with a gentle detune",
        "Mix(SinOsc.ar(freq * [1, 2, 3, 4, 6]) * [0.5, 0.3, 0.2, 0.15, 0.1]) * "
        "(1 + detune)",
        ("detune = 0",),
    ),
    "stab": (
        "supersaw stab with a fast decay",
        "Mix(Saw.ar(freq * [1, 1 + detune, 1 - detune])) * 0.5",
        ("detune = 0.01",),
    ),
}

# The output stage is deliberately cheap: a note can be one of dozens sounding at
# once, so no FreeVerb (8 delay lines) and no 1-second DelayC per note. The echo
# is a short feedback comb and the space is a two-comb + one-allpass tail, both
# scaled by their control (and inaudible when it is 0).
PIPELINE = """    env = EnvGen.kr(
        Env([0, 1, 1, 0], [atk, (dur - atk - rel).max(0.01), rel]),
        doneAction: 2);
    sig = {body};
    sig = RLPF.ar(sig, cutoff.clip(30, 18000), resonance.clip(0.05, 1));
    sig = (sig * drive.clip(0.05, 8)).tanh;
    echo = CombC.ar(sig, 0.25, delay.clip(0.001, 0.25), 1.5);
    sig = sig + (echo * delay.clip(0, 1) * 0.5);
    space = CombC.ar(sig, 0.1, 0.031, 2.5) + CombC.ar(sig, 0.1, 0.043, 2.1);
    space = AllpassC.ar(space * 0.4, 0.05, 0.007, 0.5);
    sig = sig + (space * reverb.clip(0, 1));
    Out.ar(out, Pan2.ar(sig * env * amp, pan.clip(-1, 1)));"""


def names():
    """Catalogue names, in definition order."""
    return list(DEFS)


def shared_controls():
    """Control names every catalogue SynthDef declares ('out' excluded)."""
    out = []
    for part in SHARED.split(","):
        name = part.split("=")[0].strip()
        if name and name != "out":
            out.append(name)
    return out


def controls(name):
    """(shared, extras) control names of a catalogue SynthDef.

    `shared` is what every definition accepts, `extras` the ones only this one
    has ([] for an unknown name).
    """
    entry = DEFS.get((name or "").strip())
    extras = []
    if entry is not None:
        for text in entry[2] or ():
            extras.append(text.split("=")[0].strip())
    return shared_controls(), extras


def describe(name):
    """'saw + pulse through ...' for the help/list views, or ''."""
    entry = DEFS.get((name or "").lower())
    return entry[0] if entry else ""


def source(name):
    """SuperCollider source of a catalogue SynthDef.

    Raises KeyError when the name is not in the catalogue (callers fall back to
    the generic template or to a file the user wrote).
    """
    key = (name or "").strip()
    entry = DEFS.get(key)
    if entry is None:
        raise KeyError(name)
    description, body, extras = entry
    extra_controls = "".join(f", {text}" for text in extras) if extras else ""
    controls = SHARED + extra_controls
    pipeline = PIPELINE.format(body=body)
    return (f"// '{key}' SynthDef - {description}.\n"
            f"// Generated by seq.py ('synthdef {key}'); edit freely and re-send.\n"
            f"SynthDef(\\{key}, {{ |{controls}|\n"
            f"    var sig, env, echo, space;\n"
            f"{pipeline}\n"
            f"}}).add;\n")


def all_sources():
    """{name: source} for the whole catalogue."""
    return {name: source(name) for name in DEFS}
