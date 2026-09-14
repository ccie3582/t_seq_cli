# seq — a MIDI CLI sequencer

An interactive, hierarchical MIDI sequencer that runs in a terminal. You describe
music as **tracks → patterns → sequences → phrases**, play it to a MIDI output
port, and modulate parameters with up to **8 global LFOs**. Patterns can send
**notes** or **control-change (CC) data**.

Everything is edited with short commands; there is no file format to learn beyond
`save`/`load`.

```
seq> t1p1s0 0 2 4 r
seq> t1p1s1 5*lfo1 7
seq> t1p1f1 inf*(s0+s1)
seq> t1p1v0 90
seq> t1p1sus0 0.5+0.5*lfo2
seq> t1p1 division 1/8
seq> start t1p1f1
```

---

## 1. Running it

Requirements: Python 3.12+ on Windows (MIDI output uses `winmm` through `ctypes`;
no third-party packages needed).

```bash
python seq.py            # start the interactive shell
python seq.py help       # print the root command list and exit
```

Useful one-shot command-line helpers (see `COMMAND_SCRIPTS`): if additional
script wrappers are present in the folder (e.g. `list-ports.py`) they can be run
as `python seq.py list-ports`.

If no MIDI port is selected yet, start playing will tell you:
`No MIDI output port selected. Use 'select-port'…`.

### Terminal notes

* On Windows without `readline`, the shell uses a built-in line editor: **Tab**
  completes commands, **Up/Down** recall history, **Left/Right/Home/End/Del**
  edit, **Ctrl+C** cancels the line, **Ctrl+D / Ctrl+Z** send EOF.
* **Tab completion works across menus and paths**, not just for the current
  shell's commands:

  | typed | Tab completes to |
  |---|---|
  | `/ t1p2 chan` | `/ t1p2 channel` |
  | `/t1p2 cont` | `/t1p2 controller` |
  | `t1p2 div` (from any menu) | `t1p2 division` |
  | `/lfo1 frequ` | `/lfo1 frequency` |
  | `cp lfo1 lfo` | `cp lfo1 lfo2` … `lfo8` |
  | `rm lfo` | `rm lfo1` … `lfo8` |
  | `hist ` / `hist f` | `live once stop static` / `f1 f2 …` |
  | `scale ` (pattern menu) | scale names |
  | `sh` (LFO menu) | `shape show` |
* ANSI colours are enabled automatically; the histograms use Unicode block
  glyphs when the output can carry them, ASCII otherwise.
* Use a reasonably wide window: the histogram adapts its column count to the
  terminal width.

---

## 2. Menus at a glance

| Prompt | Menu | What lives here |
|---|---|---|
| `seq>` | main | global BPM, port, LFO entry, save/load, panic, `show`, play/stop by path |
| `seq:t1>` | track | 16 patterns |
| `seq:t1p1>` | pattern | sequences, phrases, note/CC settings, LFO value sequences, `hist` |
| `seq:lfo:1>` | LFO | one oscillator's frequency, shape, phase, start/stop, live view |

### Navigation and prompts

| Input | Effect |
|---|---|
| `t1` | enter track 1 (from the main menu) |
| `t1p2` | enter pattern 2 of track 1 — from **any** menu |
| `p3` | inside a track menu: switch to pattern 3 in place |
| `p3` | inside a pattern menu: switch to pattern 3 in place |
| `s2`, `f1`, `v0`, `sus0`, `mt0` | touch a leaf of the current pattern |
| `/` | return to the main menu (`seq>`) from anywhere — unwinds every level |
| `/ <command>` | run a **main-menu** command **without leaving this menu**, e.g. `/ lfo1 frequency 10`, `/ bpm 100` |
| `exit` | go one level up (playback keeps running) |
| `help` / `help <command>` | command list / detail for one command |

`/` and `/ <command>` differ deliberately, like a shell: a lone `/` changes
directory to the root, while `/ <command>` is a command run with the full path
and **no change of directory**:

```
seq:t1p1> /lfo1 frequency 10
Frequency set to 10 Hz (5bpm)
seq:t1p1>                    ← still here
seq:lfo:2> / bpm 99          ← from an LFO menu too
Global BPM set to: 99
seq:lfo:2>
seq:t1p1> /                  ← lone slash: go to the top
seq>
```

### Path syntax

Every leaf and setting is addressable by a compact path:

```
t<track>            t2
t<track>p<pattern>  t2p4
...s<seq>           t2p4s0     (sequences s0..s9)
...f<phrase>        t2p4f3     (phrases f0..f7)
...v<seq>           t2p4v0     (velocity of a sequence)
...sus<seq>         t2p4sus0   (sustain of a sequence)
...mt<seq>          t2p4mt0    (microtiming of a sequence)
```

At the main menu a path followed by a value **edits** the leaf:

```
t1p1s0 0 0 5            # set sequence 0
t1p1f1 inf*s0           # define phrase 1
t1p1v0 64+0.4*lfo1      # velocity expression
t1p1sus0 0.5+0.5*lfo1   # sustain expression
t1p1mt0 -0.05           # microtiming expression
t1p1 scale dorian       # pattern setting
t1p1 root E4
t1p1 bpm 95
t1p1 channel 10
t1p1 division 1/8
t1p1 type CC
t1p1 controller 74
t1p1 hist f1 live       # view
t1p1s0                  # show one leaf
```

An `=` is accepted anywhere a value follows a leaf name:
`t1p1v0=64+0.4*lfo1`, `v3=-5`, `sus1=0.5`.

---

## 3. The timing model

* **BPM** — global (`bpm 120`) or per pattern (`bpm 95`). Range 1–300.
* **division** — the note value of one step: `1`, `1/2`, `1/4`, `1/8`, `1/16`
  (default), `1/32`, `1/64`, or **any** `1/<integer>` (`1/3`, `1/5`, `1/7` …).
  A bare integer is accepted as the denominator (`division 8` = `1/8`).

  ```
  step_ms = 240000 / (bpm × division_denominator)
  ```

  At 120 BPM: `1/16` → 125 ms, `1/8` → 250 ms, `1/3` → 666.67 ms, `1` → 2000 ms.
* **sustain** — how long a note holds, expressed in **steps** (not a MIDI
  value: MIDI has no note-length byte, a note lasts from Note On to Note Off).
  `0.5` = half a step (default), `2` = two steps, `1/4` = a quarter step, or an
  expression (`0.5+0.5*lfo1`); clamped to `0..8` steps. The real length in ms
  follows the division and BPM.

  Playback is **polyphonic**: every Note On carries its own Note Off, so a
  sustain longer than one step rings *over* the following notes — the Note Off
  is sent as soon as its length has elapsed, which is after the next Note On
  when it overlaps. The sequencer never waits for a note to end, so long sustains
  neither block the grid nor delay the following steps, and `stop`/`panic`
  release everything immediately.
* **microtiming** — an earlier/later offset **in steps**, signed like sustain
  is unsigned: `1/4` = a quarter step late, `-1/8` = an eighth early, `0.5`,
  `-1`, clamped to `-8..8` steps, default `0` (on the grid). Negative values are
  allowed; the real ms follow the division and BPM.
* **LFO start alignment** — starting an LFO latches it to the **next beat** of
  the main BPM grid and restarts it from its configured phase, so several LFOs
  started within the same beat stay phase-locked. A stopped LFO reads `0`.

Changing division, BPM, scale, root, channel, sequences or phrases while playing
takes effect at the **start of the next cycle** (the player re-reads the pattern
before every pass).

---

## 4. Pattern menu

```
seq:t1p1>
```

### 4.1 Sequences — `s0 … s9`

A sequence is a list of steps. In a **note pattern** each step is a scale degree
(**relative to the pattern's current scale**, wrapping across octaves — changing
the scale later re-reads the same degrees in the new scale);
in a **CC pattern** each step is a raw controller value.

```
s0 0 2 4 r          # degrees: root, 3rd, 5th, rest
s0 O0 o3            # octave shifts: O = +1 octave, o = -1 octave
s0 4+2*lfo1 7       # LFO term (see below): degree 4 +/- 2 scale steps
s0                  # show the sequence with resolved values
list-s              # list all configured sequences
```

| Token | Meaning |
|---|---|
| `<int>` | scale degree 0..127 (note pattern) or raw value 0..127 (CC pattern) |

Degrees are **scale-relative**: `s0 1 2 3 4` in `chromatic` gives `C#4 D4 D#4 E4`,
and after `scale dorian` the very same sequence plays `D4 Eb4 F4 G4`. Degrees at
or beyond the scale size wrap into the next octave (`s0 7` in a 7-note scale =
root one octave up), so no step is ever silently dropped.
| `O…`, `o…` | octave up/down prefixes (`O0`, `o3`); in CC mode they add ±12 |
| `r` | rest — the step is skipped (no note, no CC) |
| `A*lfo1` | multiplication: value = `round(A × lfo1)` |
| `A*B*lfo1` | multiplication: value = `round(A × B × lfo1)` |
| `A+B*lfo1` | sum: value = `A + round(B × lfo1)` — how you write an offset |
| `A*lfo1*lfo2` | several LFOs multiply together |
| `A-B*lfo1` | subtraction (also `lfo1*30+30`, any sum of products) |

The value of a step is a small **algebraic expression**: products bind first,
then sums, exactly like normal maths. Numbers may be fractional or negative.

```
s0 127*lfo1         # 127 x lfo1      -> 0..127 with a ramp LFO
s0 63+63*lfo1       # 63 + 63 x lfo1  -> 0..126 with a bipolar LFO
s0 64+63*lfo1       # centred sweep   -> 1..127
s0 4+2*lfo1         # a note degree +/- 2 scale steps
s0 2*lfo1*lfo2      # two LFOs multiplied
```

Two things to keep in mind:

* **Bipolar vs unipolar** — `sin`, `tri`, `saw`, `square`, `random` run
  `-1 … 1`, so `127*lfo1` reaches `-127…127` and a CC value clamps to `0…127`
  (use `shape ramp` for a clean `0…127`); `ramp` and any `shape ramp` factor run
  `0 … 1`.
* `0*5*lfo1` now means `(0 × 5) × lfo1 = 0`. For an offset write the sum:
  `0+5*lfo1`, or just `5*lfo1` (the setter warns when an LFO term multiplies
  out to 0).

In a note pattern the result is a **scale step index** and octaves wrap, so
`5*lfo1` walks ±5 notes of the selected scale (in C major: `+5` → A4, `-5` →
E3). In a CC pattern it is clamped to `0..127`.

```
seq:t1p1> s1 0*5*lfo1 0 0        # a note that moves with LFO 1
seq:t1p1> s1                     # s1: 0*5*lfo1 0 0   -> A4 C4 C4
seq:t1p1> s2 0*5*lfo1*lfo2       # amplitude shaped by two LFOs
seq:t1p1> s2                     # s2: 0*5*lfo1*lfo2 -> C4
seq:t1p1> type CC
seq:t1p1> controller 1
seq:t1p1> s3 63*30*lfo1*lfo2     # CC1 = 63 + round(30 x lfo1 x lfo2)
```

A stopped (or not-yet-latched) LFO counts as 0, so any chain containing one
currently reads 0 and the value falls back to the base number.

### 4.2 Phrases — `f0 … f7`

A phrase is a looping or one-shot arrangement of sequences (8 phrases per
pattern, numbered **0 to 7**, like sequences):

```
f0 inf*s0               # loop sequence 0 forever
f1 4*s0                 # play s0 four times, once
f2 inf*(s0+2*s1)        # loop [s0, s1, s1]
f7 2*(3*s0+s1)+s2       # weighted groups
f0                      # show the phrase
list-f                  # list all phrases
```

Grammar:

```
expr  := 'inf*' unit | sum
sum   := term ( '+' term )*
term  := count '*' unit | unit
unit  := 's' DIGIT | '(' sum ')'
count := positive integer
```

Weights repeat, `+` concatenates, parentheses group, `inf` loops until stopped.
`seq0` is accepted as a synonym of `s0`. A phrase with no `inf` plays **one pass**
and stops by itself.

### 4.3 Value sequences — velocity `v0…v9`, sustain `sus0…sus9`, microtiming `mt0…mt9`

These attach to the **sequence with the same index**: notes coming from `s2` use
`v2`, `sus2` and `mt2`. Each accepts a number **or an LFO expression**:

| Sequence | Meaning | Range (clamped) | Default |
|---|---|---|---|
| `v<k>` | MIDI velocity | `0..127` (integer) | `100` |
| `sus<k>` | note length, **in steps** (`0.5`, `2`, `1/4`) | `0..8` steps | `0.5` |

Fractions also work inside expressions, for both values and LFO coefficients —
`sus0 1/8+1/16*lfo1` sweeps the length from `1/16` to `3/16` of a step, and
`mt0 1/8-1/16*lfo1` swings the timing between `+1/16` and `+3/16` of a step.
| `mt<k>` | timing offset, **in steps**, signed (`1/4`, `-1/8`) | `-8..8` steps | `0` |

Expression grammar (all three):

```
expr := term ( ('+'|'-') term )*
term := number | lfo<N> | <coef>*lfo<N>
```

Examples:

```
v0 45                    # constant velocity
v1 =64+0.4*lfo1          # 64 plus 40% of LFO 1
sus0 0.5                 # hold half the step
sus1 1/4                 # a quarter of a step
sus2 2                   # two steps long (e.g. 250 ms at 1/16, 120 BPM)
sus3 0.5+0.5*lfo1        # vary the length
mt0 -0.05                # 5% of a step early (same as -1/20)
mt1 1/4                  # a quarter step late
mt2 -1/8                 # an eighth of a step early
mt3 0.01*lfo1            # swing with LFO 1
v0 / sus0 / mt0          # show one entry
list-v, list-sus, list-mt
```

`mt` values are shown in milliseconds wherever the step length is known
(`mt0 0.1` at `1/16`, 120 BPM → `+25.0 ms`).

### 4.4 Pattern type: note, CC or loop

```
type                     # "Type: note", "Type: CC (controller 74)" or "Type: loop"
type CC                  # this pattern sends control-change data (also: type note)
type loop                # this pattern plays positions of a .wav sample (see 4.5)
controller 74            # CC number 0..127 used by a CC pattern
```

**CC patterns** behave exactly like note patterns — same sequences, phrases,
phrases weights, division, BPM, microtiming, channel, port, `cp`/`rm`,
`save`/`load` — with these differences:

* a sequence value is the **CC data byte** (`0..127`, scale-independent),
* every step sends `CC(controller, value)` on the pattern's channel/port,
* **no note on/off** is sent, velocity and sustain sequences are not used,
* the number of controllers is unlimited: use several CC patterns.

```
seq:t1p1> type CC
seq:t1p1> controller 1
seq:t1p1> channel 3
seq:t1p1> s0 63+30*lfo1 r        # CC1 sweeps 33..93 (offset form)
seq:t1p1> f1 inf*s0
seq:t1p1> division 1/8
seq> lfo1 shape sin
seq> lfo1 start
seq> start t1p1f1
```

Because the LFO term is an **offset** (`base*amp*lfo` = `base ± amp`), the base
decides how much room there is. `show` and `list-s` annotate the reachable
range and warn you when it cannot move:

```
s0: 127*lfo1      -> 127   [LFO range 0..127]          # multiply (ramp: 0..127)
s0: 63+30*lfo1    -> 63    [LFO range 33..93]          # add an offset
s0: 64+63*lfo1    -> 64    [LFO range 1..127]          # full centred sweep
s0: 127*lfo1      -> 0     [lfo1 stopped -> contributes 0 (value stays 0)]
```

### 4.5 Loop patterns (sample positions)

A **loop pattern** plays a .wav file. The sample's length is normalised to **1**,
so an order entry is a **position** in it: `0.2` means "start at 20 %", `0.5` the
middle, `1`/`0` the beginning. Positions **wrap modulo 1**, so negatives work too
(`-0.2` -> `0.8`) - an LFO can therefore sweep the whole file.

```
type loop                # this pattern plays the sample
sample Drums.wav         # pick a file from the 'samples' folder next to seq.py
sample                   # show the current file and its length
division 1/8             # step length (250 ms at 120 BPM)
o0 0.2 0.3 0.6           # order list 0: play from 20 %, 30 %, 60 % of the sample
o1 0 0.5 r 0.75          # order list 1 (o0..o9); 'r' is a rest (silent step)
o2 0.2 0.5*lfo1          # an entry may be an LFO: position = round-free 0.5 x lfo1
o3 lfo2                  # with shape ramp, lfo2 sweeps 0 -> 1 across the sample
o0                       # show one order list (with the values it resolves to now)
list-o                   # list the sample and every order list
f0 inf*o0                # phrase: arrangement of order lists (like inf*s0)
f1 2*o0+o1               #   ... 0.2 0.3 0.6 | 0.2 0.3 0.6 | 0 0.5 r 0.75, then repeat
start f0                 # play the arrangement (loops forever)
stop f0                  # stop it
start o0                 # shortcut: play one order list (same as inf*o0)
```

* `division` sets the **step length**, so a pass through an order list follows
  the BPM grid (a rest also takes one step); messages show where the order list
  sits in the bar at that division (`4 passes per 4/4 bar`, `5/16 of a 4/4 bar`,
  `1 bar (4/4)`).
* Every step **starts the sample at its position** and the next step cuts it off
  (single audio channel, like a sampler). A position of `0.8` on a 3.10 s sample
  therefore has 0.62 s left to ring before the next step.
* An order entry may be an **expression with LFOs** (same algebra as sequences:
  `lfo1`, `0.5*lfo1`, `0.3+0.1*lfo1`, `2*lfo1*lfo2`). It is evaluated at every
  step; the value is used as a position and wrapped modulo 1, so a `ramp` LFO
  sweeps the sample and a bipolar one crosses its whole length. `show`, `o<k>`
  and `list-o` print the values it resolves to at that moment
  (`o1: 0.75 0.2 r 0.5*lfo1   -> now 0.75 0.20 r 0.25`) and note a stopped LFO.
* Playback is **audio**, not MIDI: positions are played by feeding the audio
  device the sample's frames from that offset through winmm's `waveOut` API
  (ctypes, no extra packages, Windows only). The file must be a plain **PCM
  .wav**. `panic` and `exit` silence it.
* Live editing works like the rest of the sequencer: changing the order list,
  the sample, the division or the BPM takes effect on the **next pass**.
* Order lists are the loop-pattern equivalent of sequences, and **phrases are
  still the arrangement layer** — just like `s0` -> `f0 inf*s0` in a note
  pattern: `f0 inf*o0` loops order list 0, `f1 2*o0+o1` chains repeats and
  groups (`4*(o0+2*o1)` works too). A loop phrase must use `o<k>` units (a
  note/CC phrase must use `s<k>`) and the setter tells you when they are mixed.
  `start f0` plays it and the running phrase shows in green, like any phrase.
* `start o0` still works as a shortcut (it is `inf*o0`), from the pattern menu
  (`start o0`), the track menu (`start p1o0`) or the main menu (`start t1p1o0`),
  and `panic` stops loops.
* Saving stores `type = loop`, `sample = Drums.wav`, `order-<k> = 0.2 0.3 0.6`
  and the arrangements (`phrase-<k> = inf*o0`) in the pattern section; `cp o0 o1`
  copies an order list (and `cp t1p1o0 t3p4o1` across patterns), `rm t1p1o0`
  deletes one.
* `hist` works here too, on the same horizontal layout: one row per step with the
  sample's length as the axis (0 at the left, 1 at the right). The marker is the
  position the step plays from and the short line after it is what is actually
  heard - the sample runs only until the next step (or the end of the file):

  ```
  seq:t1p3> hist o0 live
  o0  step 250 ms  bpm 120  division 1/8  Motorway.wav   (live)   step 3

          0               1/4             1/2            3/4             1
          |---------------|---------------|--------------|---------------|
    s1    ......*-----....................................................  0.094
    s2    ................................*----...........................    0.5
    s3    .........................................................*----..    0.9
  ```

  With a step longer than the sample the line runs to the right edge (the whole
  rest of the file is heard); `hist live` refreshes the view and the row of the
  step being played is marked `>` (a rest row shows `>` too, with no marker).

```
seq> t1p1 type loop
seq> t1p1 sample Drums.wav
Sample set to Drums.wav (C:\CLI_1\samples)
  Length 4.04 s.
seq> t1p1 division 1/8
seq> t1p1o0 0 0.25 0.5 r 0.9
o0 set: 0 0.25 0.5 r 0.9   (5 steps x 250 ms = 1250 ms per pass, 5/16 of a 4/4 bar; positions in the sample)
seq> t1p1 list-o

Loop (pattern 1, step 250 ms at division 1/8, bpm 120):
  sample: Drums.wav  (4.04 s, C:\CLI_1\samples)
  o0: 0 0.25 0.5 r 0.9     -> 5 steps = 1250 ms per pass, 5/16 of a 4/4 bar

seq> start t1p1o0
Playing o0 of pattern 1, track 1 ('Drums.wav', 4.04 s): 5 steps
(0 0.25 0.5 r 0.9) = positions in the sample, 120 BPM, step 250.0 ms
= 5/16 of a 4/4 bar, looped. Type 'stop o0' to end.
seq> stop t1p1o0
Loop stopped.
```

### 4.6 Other pattern settings

```
scale [<name>]        # chromatic (default); 'scale ?' lists all scales
root [<note>]         # C4 default; e.g. F#3, Bb2
bpm [<value>]         # per-pattern BPM (default: inherits the global BPM)
channel [<n>]         # 1..16, default 1
division [1/16]       # step note value
select-port           # choose this pattern's output port (default: global)
```

### 4.7 Playback from the pattern menu

```
start f1              # start phrase 1
stop                  # stop this pattern's playback
stop f1               # stop phrase 1 of this pattern
```

### 4.8 Histogram view — `hist`

A per-step view of what the phrase plays, one column per division step:

```
phrase f1  division 1/8  step 250 ms  bpm 120  chromatic on C4
        0    1    2    3    4    5    6    7
        --------------------|-------------------
  note  C4   D4   E4   ·    F4   E4   C4   D4
  vel   ▆ 90 ▆ 90 ▆ 90      ▃ 40 ▃ 40 ▆ 90 ▆ 90
  sus   ▋ 50 ▋ 50 ▋ 50      ▍ 25 ▍ 25 ▋ 50 ▋ 50
  mt    > 25 > 25 > 25      < 50 < 50 > 25 > 25
```

| Command | Effect |
|---|---|
| `hist` | snapshot of the phrase (the one that is playing, else the lowest) |
| `hist f2` / `hist s3` | a specific phrase or sequence |
| `hist live` | keep refreshing while you work (panel above the prompt) |
| `hist once` | force a one-shot snapshot |
| `hist stop` / `hist live stop` | end the live view |

Reading it:

* **note** — resolved note name (octave-wrapped), `·` for a rest;
* **vel** — velocity glyph (height ∝ value) and number;
* **sus** — note length in steps, shown as the fraction you would type
  (`▏ 1/8`, `▎3/10`, `█   2` = two steps; decimals never exceed two, so
  `1.3333` reads `1.33`); glyph height scales up to 2 steps;
* **mt** — microtiming in steps (two decimals at most, fractions when exact):
  `<1/4` = a quarter step early, `>0.33` late, `|` on the grid; a bare `|` means
  "no microtiming defined", `|  0` means defined but currently zero;
* the header marks the **currently sounding step** with `>`;
* in a CC pattern the rows are `val` (the CC values) and `mt`, and the title
  reads `type CC<controller> … values 0-127`;
* the title also explains zero values: `(lfo1 stopped)`,
  `(lfo1 starts at the next beat)`.

---

## 5. Track and main menus

### 5.1 Track menu

```
seq:t1> p3                # enter pattern 3
seq:t1> p3s0 0 2 4        # edit a sequence of pattern 3 directly
seq:t1> t2                # switch track in place
seq:t1> start p3f1        # play a phrase of this track
seq:t1> stop p3f1
seq:t1> show p2           # show a subtree of this track
seq:t1> show              # show this track
```

### 5.2 Main menu

```
seq> bpm [<value>]        # global tempo
seq> list-ports           # MIDI ports (uses the helper script)
seq> select-port          # choose the global MIDI output port

seq> lfo 3                # enter the LFO 3 menu
seq> lfo3 start           # start/stop an LFO without entering its menu
seq> lfo1 shape square    # any LFO command can be run this way
seq> status-lfo           # parameters of all running LFOs

seq> start t1p1f1         # play a phrase
seq> stop t1p1f1          # stop a phrase
seq> panic                # stop everything and silence MIDI

seq> show                 # whole configuration tree
seq> show t1p1            # one pattern
seq> show t1p1s0          # one sequence
seq> show t1p1f1          # one phrase
seq> show t1p1v0          # one velocity entry

seq> save [<file>]        # write seq.cfg
seq> load [<file>]        # read seq.cfg
seq> help / help start
seq> exit
```

`show` prints only branches that lead to configured leaves, and running phrases
are highlighted in green. In a note pattern a sequence is resolved to note
names; in a CC pattern the values are printed as numbers.

---

## 6. LFOs (8 global oscillators)

Enter a menu with `lfo <n>` (1–8) or `lfo<n>`; from anywhere you can use
`/ lfo<n> <command>` or `lfo<n> <command>`.

### 6.1 Parameters

| Command | Syntax | Default |
|---|---|---|
| `frequency [<value>]` | Hertz number (`2`), tempo multiple (`8bpm`), submultiple (`1/8bpm`), or modulation by another LFO (`0.5*lfo2`) | `1` Hz |
| `shape [<name>]` | `sin`, `tri`, `saw`, `square`, `random` (bipolar `-1..1`) or `ramp` (`0..1`) (`shape ?` lists them) | `sin` |
| `phase [<v>]` | start point of the waveform, in cycles, `-1..1` | `0` |

`<k>bpm` means *k cycles per beat*, i.e. `k × BPM / 60` Hz; `1/8bpm` means one
cycle every 8 beats. Both display as e.g. `8bpm (16 Hz)` / `2 Hz (1bpm)`, and
tempo-relative values **follow the global BPM**. `phase 0.25` starts a sine at
its positive peak (`0.5` is the old "inverted amplitude" case); `square` is a
50 % duty cycle with phase moving its edges.

`ramp` is **unipolar**: it rises linearly from `0` to `1` across the cycle and
jumps back to `0`, so modulated values only ever move upwards from the base —
`63*30*lfo1` with `lfo1 shape ramp` sweeps a CC from 63 to 93, and
`40+80*lfo1` sweeps velocity from 40 to 120. Its live meter is drawn as a
left-anchored progress bar instead of a centered needle.

`random` is **sample-and-hold**: every cycle it draws a new value in `-1..1` and
holds it for the whole cycle, so `frequency` is simply **how fast the values
change** (`frequency 4` = four new values per second; `1/4bpm` = a new value
every 4 beats). The draw is deterministic for a given LFO and cycle, so the
histogram, the live meter and the playback all agree on the same value, and the
same phase always reproduces the same sequence — `phase` shifts which values are
drawn (`lfo1 shape random`, then `s0 0*12*lfo1` steps randomly through the
scale). Its meter stays centered, like the other bipolar shapes.

### 6.2 Running an LFO

```
seq:lfo:1> start          # latch to the next main-BPM beat, start from `phase`
seq:lfo:1> stop           # a later start begins fresh again (never resumes)
seq:lfo:1> live           # single-line meter next to the prompt
seq:lfo:1> live stop
seq:lfo:1> show           # all parameters
seq> status-lfo           # all running LFOs
```

The `live` meter draws a normalised level (`[-···|###···] +0.42`) and stays at
`0.00` while the LFO is stopped. Updating it is flicker-free: only the meter
field is repainted.

### 6.3 Using LFOs

`lfo<N>` can be referenced by:

* sequence variations — `s0 0*5*lfo1`
* velocity / sustain / microtiming expressions — `v1 64+0.4*lfo1`,
  `sus0 0.5+0.5*lfo2`, `mt0 0.01*lfo1`
* LFO frequency modulation — `frequency 2*lfo3` (cycles and self-references are
  rejected).

---

## 7. Copy, remove and reset

```
cp t1 t2                     # whole track (all its patterns)
cp t1p1 t3p4                 # a pattern (settings + all leaves)
cp t1p1s1 t3p4s4             # a sequence (+ its v/sus/mt, see below)
cp t1p1f1 t3p4f3             # a phrase
cp t1p1v1 t3p4v4             # velocity / sus / mt entries
cp t1p1sus1 t3p4sus2
cp t1p1mt1 t3p4mt0
cp lfo1 lfo2                 # an LFO (see below)
```

Copying a **sequence** also copies the velocity, sustain and microtiming that
belong to it (they are indexed by the sequence number), so a copied sequence
keeps its dynamics and timing — and the destination's old companions are
cleared, so the copy is exact:

```
seq:t1p1> cp s0 s1
Copied sequence t1p1s0 -> t1p1s1 (v1, sus1, mt1 too).
```

`cp` is **total**, like a Unix `cp`: a source that was never configured counts as
its default/empty state, so the copy still succeeds.

| situation | result |
|---|---|
| `cp t1p1 t1p1` | succeeds, reports `unchanged` |
| source empty, destination configured | destination is **reset to defaults** (cleared) |
| source configured, destination empty | destination gets a full copy |
| overwriting a playing pattern/track | its phrases are stopped first |

```
cp t1p1 t1p2        → Copied pattern t1p1 -> t1p2; t1p2 reset to defaults (t1p1 is empty).
cp t3 t2            → Copied track t3 -> t2; t2 is empty (t3 has nothing configured).
cp t1p1s1 t1p2s2    → Copied sequence t1p1s1 -> t1p2s2; t1p2s2 cleared (t1p1s1 is not configured).
cp t1p1s0 t3p4s4    → Copied sequence t1p1s0 -> t3p4s4.
```

Mixing kinds is still refused (`cp t1p1s0 t1p1f1`), since that is a type error.

**Operands can be relative to the current menu**, exactly like the other
commands:

```
seq:t1> cp p1 p2          → Copied pattern t1p1 -> t1p2.
seq:t1> rm p2             → Removed pattern t1p2.
seq:t1p1> cp s0 s1        → Copied sequence t1p1s0 -> t1p1s1.
seq:t1p1> cp s1 p3s2      → Copied sequence t1p1s1 -> t1p3s2.
seq:t1p1> rm division     → Reset division of t1p1 (1/16).
seq:t1p1> rm s0           → Removed sequence t1p1s0.
```

Absolute (`t2`, `t3p4`) and LFO (`lfo2`) operands are still accepted as-is, and
`/cp t1p1 t4p4` from a submenu runs the copy at the root (with absolute paths)
without leaving your menu.

### Copying LFOs

```
cp lfo1 lfo2                 # LFO 2 gets LFO 1's frequency, shape and phase
cp /lfo1 /lfo2               # same thing, from inside any menu
```

The leading `/` is optional and may be used on either or both operands
(`cp /lfo1 lfo2`). `cp` is total, like in a Unix shell: an LFO that was never
configured simply has its default values (1 Hz, sin, phase 0, stopped), so

* `cp lfo1 lfo1` succeeds (reports `unchanged`) and leaves the LFO alone, even
  while it is running;
* copying identical settings reports `no change` instead of restarting the
  destination, so a running LFO keeps its phase;
* copying a default LFO over a configured one resets the destination to the
  defaults (`Copied LFO 5 -> LFO 1 (frequency 1, shape sin, phase 0); LFO 1 stopped.`).

The destination's running state follows the source: if LFO 1 is running, LFO 2
is started too (fresh, beat-aligned); otherwise the destination is left stopped.

```
rm t1                        # delete a track
rm t1p1                      # delete a pattern (back to defaults)
rm t1p1s1  rm t1p1f1         # delete one sequence / phrase
rm t1p1v1  rm t1p1sus1  rm t1p1mt1
rm t1p1 scale                # reset a setting to its default
rm t1p1 root  |  bpm  |  channel  |  division  |  type  |  controller  |  port
```

### Resetting LFOs

```
rm lfo1                      # LFO 1 back to defaults: 1 Hz, sin, phase 0, stopped
rm lfo2 frequency            # reset one parameter (frequency | shape | phase | running)
rm lfo                       # reset all 8 LFOs (also: 'rm lfo all')
seq:lfo:4> rm                # reset the LFO you are inside
seq:lfo:4> rm shape          # reset one parameter of that LFO
seq:lfo:4> rm lfo2           # reset another LFO without leaving the menu
```

A running LFO is stopped by the reset (it reports "was running and has been
stopped"), and a fully default LFO is no longer written to the configuration
file. Resets work from every menu, because `rm` is routed to the main shell.

Removing a leaf/setting prunes empty patterns and tracks automatically. Copying
and removing always targets the same kind on both sides (mismatches are
refused), and a running phrase of a touched pattern is stopped first.

---

## 8. Save / load format

`save [<file>]` (default `seq.cfg`) writes a plain INI file; `load [<file>]`
reads it back. Only non-default settings are written.

```ini
[global]
bpm = 120

[lfo-1]
frequency = 2bpm
shape = square
phase = 0.25
running = true

[track-1:pattern-1]
seq-0 = 0 2 4 r
seq-1 = 0*5*lfo1 7
phrase-1 = inf*(s0+1*s1)
velocity-0 = 90
velocity-1 = 64+0.4*lfo1
sustain-0 = 1/4
microtime-0 = -0.02
division = 1/8
type = cc
controller = 74
```

Notes:
* `bpm`, `scale`, `root`, `channel`, `port`, `type`, `division` are omitted when
  they equal their defaults; the global `bpm` is always written.
* The selected output port is stored as `port = <name>` in `[global]` (omitted
  while no port is selected).
* LFOs saved as `running = true` are re-armed at load: they start at the next
  beat from their stored phase.

---

## 9. Panic and playback control

```
seq> panic          # stop every phrase and send All Sound Off (CC120) +
                    # All Notes Off (CC123) on all 16 channels of every
                    # reachable port
```

`panic` is available in **every** menu (main, track, pattern, LFO). Playback
otherwise survives navigation: leaving a pattern or track keeps phrases running
until you `stop`, `panic`, or exit the program.

Multiple phrases (and multiple CC patterns) can play **simultaneously** on the
same port and channel — the sequencer shares one device handle per port and each
player is independent.

---

## 10. Command reference (by menu)

**Main (`seq>`)**
`bpm`, `list-ports`, `select-port`, `lfo <n>`, `lfo<n> start|stop`,
`lfo<n> <param> …`, `status-lfo`, `start <path>`, `stop <path>`, `show [<path>]`,
`cp`, `rm`, `save`, `load`, `panic`, `help`, `exit`, `<path> …` (edit by path).

**Track (`seq:t1>`)**
`p<m>`, `p<m>s<k> …`, `p<m>f<k> …`, `p<m> division/type/controller/scale/root/bpm/channel …`,
`show [p<m>[s<k>|f<k>]]`, `start p<m>f<k>`, `stop p<m>f<k>`, `t<n>`, `cp`, `rm`,
`panic`, `help`, `/`, `/ <command>`, `exit`.

**Pattern (`seq:t1p1>`)**
`type`, `controller`, `division`, `scale`, `root`, `bpm`, `channel`,
`select-port`, `s<k> [tokens…]`, `f<k> <expr>`, `v<k> <expr>`, `sus<k> <expr>`,
`mt<k> <expr>`, `list-s`, `list-f`, `list-v`, `list-sus`, `list-mt`,
`hist [f<k>|s<k>|o<k>] [live|once|stop]`, `start f<k>`, `stop [f<k>]`, `p<m>`,
`t<n>`,
`cp`, `rm`, `panic`, `help`, `/`, `/ <command>`, `exit`.

**LFO (`seq:lfo:1>`)**
`frequency`, `shape`, `phase`, `start`, `stop`, `live [stop]`, `show`,
`rm [lfo<n>] [param]` (reset this/another LFO or one parameter),
`start <path>`/`stop <path>` (phrase playback), `cp`, `panic`, `help`,
`/`, `/ <command>`, `exit`.

---

## 11. Tips and troubleshooting

| Symptom | Cause / fix |
|---|---|
| Values read `0` and never move | The referenced LFO is stopped (`lfo1 start`), or it is still waiting for its beat-aligned start — the title of `hist` says which. |
| `hist live` does not appear to move | Check the title: `(live)` plus a small spinner and a moving `>` playhead mean it is refreshing. If the terminal is narrower than the panel, the column count is reduced automatically. |
| `invalid expression term '…'` | A typo in an LFO reference — the message suggests the corrected token (`0.01*lof1` → `0.01*lfo1`). |
| Nothing sounds | No output port selected (`select-port`), or the phrase is a rest-only sequence. |
| Notes stick | `panic` (sends All Sound Off / All Notes Off on all channels). |
| A pattern sends no notes | Its `type` is `CC` — check with `type` and switch back with `type note`. |

---

## 12. Files

| File | Role |
|---|---|
| `seq.py` | shells (main/track/pattern/LFO), commands, views, save/load |
| `player.py` | playback threads, step grid, note/CC output, `panic_ports` |
| `sampler.py` | wav loading and position playback (`winmm waveOut`) for loop patterns |
| `lfo.py` | waveform evaluation, frequency/phase parsing, LFO graph |
| `velocity.py` | expression grammar for velocity / sustain / microtiming |
| `phrases.py` | phrase expression parser and canonicaliser |
| `scales.py` | scales, note names and MIDI numbers |
| `midi_ports.py`, `list-ports.py` | MIDI port discovery helpers |
