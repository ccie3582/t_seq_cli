# seq — a MIDI CLI sequencer

An interactive, hierarchical MIDI sequencer that runs in a terminal. You describe
music as **tracks → patterns → sequences → phrases**, play it to a MIDI output
port, and modulate parameters with up to **8 global LFOs**. Patterns can send
**notes** or **control-change (CC) data**.

Everything is edited with short commands; there is no file format to learn beyond
`save`/`load`.

```
seq> t1p1s0 0 2 4 r
seq> t1p1s1 0*5*lfo1 7
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
| `/` | return to the main menu (`seq>`) from anywhere |
| `/ <command>` | run a **main-menu** command without leaving the current menu, e.g. `/ lfo1 start`, `/ bpm 100` |
| `exit` | go one level up (playback keeps running) |
| `help` / `help <command>` | command list / detail for one command |

### Path syntax

Every leaf and setting is addressable by a compact path:

```
t<track>            t2
t<track>p<pattern>  t2p4
...s<seq>           t2p4s0     (sequences s0..s9)
...f<phrase>        t2p4f3     (phrases f1..f16)
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
* **sustain** — how long a note (or a CC gate, if you use one) holds, as a
  **share of the step**: `0..1`, default `0.5`.
* **microtiming** — an earlier/later offset, as a **share of the step**:
  `-0.5..0.5` (`-` = early, `+` = late), default `0` (on the grid).
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

A sequence is a list of steps. In a **note pattern** each step is a scale degree;
in a **CC pattern** each step is a raw controller value.

```
s0 0 2 4 r          # degrees: root, 3rd, 5th, rest
s0 O0 o3            # octave shifts: O = +1 octave, o = -1 octave
s0 0*5*lfo1 7       # LFO variation (see below)
s0                  # show the sequence with resolved values
list-s              # list all configured sequences
```

| Token | Meaning |
|---|---|
| `<int>` | scale degree (note pattern) or raw value 0..127 (CC pattern) |
| `O…`, `o…` | octave up/down prefixes (`O0`, `o3`); in CC mode they add ±12 |
| `r` | rest — the step is skipped (no note, no CC) |
| `<base>*<amp>*lfo<N>` | LFO variation: value becomes `base + round(lfo<N> × amp)` |
| `<base>*lfo<N>` | same with amp = 1 |

`amp` may be negative or fractional (`0*-2.5*lfo3`). In a note pattern the result
is a **scale step index** and octaves wrap, so `0*5*lfo1` walks ±5 notes of the
selected scale (e.g. in C major: `+5` → A4, `-5` → E3). In a CC pattern the
result is clamped to `0..127`.

```
seq:t1p1> s1 0*5*lfo1 0 0     # a note that moves with LFO 1
seq:t1p1> s1                  # s1: 0*5*lfo1 0 0   -> A4 C4 C4
```

### 4.2 Phrases — `f1 … f16`

A phrase is a looping or one-shot arrangement of sequences:

```
f1 inf*s0               # loop sequence 0 forever
f1 4*s0                 # play s0 four times, once
f1 inf*(s0+2*s1)        # loop [s0, s1, s1]
f1 2*(3*s0+s1)+s2       # weighted groups
f1                      # show the phrase
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
| `sus<k>` | note length, share of the step | `0..1` | `0.5` |
| `mt<k>` | timing offset, share of the step | `-0.5..0.5` | `0` |

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
sus1 0.25+0.25*lfo2      # vary the length
mt0 -0.05                # 5% of a step early
mt1 0.01*lfo1            # swing with LFO 1
v0 / sus0 / mt0          # show one entry
list-v, list-sus, list-mt
```

`mt` values are shown in milliseconds wherever the step length is known
(`mt0 0.1` at `1/16`, 120 BPM → `+25.0 ms`).

### 4.4 Pattern type: note or CC

```
type                     # "Type: note" or "Type: CC (controller 74)"
type CC                  # this pattern sends control-change data (also: type note)
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
seq:t1p1> s0 63*30*lfo1 r        # CC1 sweeps 33..93
seq:t1p1> f1 inf*s0
seq:t1p1> division 1/8
seq> lfo1 shape sin
seq> lfo1 start
seq> start t1p1f1
```

### 4.5 Other pattern settings

```
scale [<name>]        # chromatic (default); 'scale ?' lists all scales
root [<note>]         # C4 default; e.g. F#3, Bb2
bpm [<value>]         # per-pattern BPM (default: inherits the global BPM)
channel [<n>]         # 1..16, default 1
division [1/16]       # step note value
select-port           # choose this pattern's output port (default: global)
```

### 4.6 Playback from the pattern menu

```
start f1              # start phrase 1
stop                  # stop this pattern's playback
stop f1               # stop phrase 1 of this pattern
```

### 4.7 Histogram view — `hist`

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
* **sus** — sustain as a fraction glyph and percent;
* **mt** — microtiming in ms: `<` early, `>` late, `|` on the grid; a bare `|`
  means "no microtiming defined", `|  0` means defined but currently zero;
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
| `shape [<name>]` | `sin`, `tri`, `saw`, `square` (`shape ?` lists them) | `sin` |
| `phase [<v>]` | start point of the waveform, in cycles, `-1..1` | `0` |

`<k>bpm` means *k cycles per beat*, i.e. `k × BPM / 60` Hz; `1/8bpm` means one
cycle every 8 beats. Both display as e.g. `8bpm (16 Hz)` / `2 Hz (1bpm)`, and
tempo-relative values **follow the global BPM**. `phase 0.25` starts a sine at
its positive peak (`0.5` is the old "inverted amplitude" case); `square` is a
50 % duty cycle with phase moving its edges.

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
cp t1p1s1 t3p4s4             # a sequence
cp t1p1f1 t3p4f3             # a phrase
cp t1p1v1 t3p4v4             # velocity / sus / mt entries
cp t1p1sus1 t3p4sus2
cp t1p1mt1 t3p4mt0
```

```
rm t1                        # delete a track
rm t1p1                      # delete a pattern (back to defaults)
rm t1p1s1  rm t1p1f1         # delete one sequence / phrase
rm t1p1v1  rm t1p1sus1  rm t1p1mt1
rm t1p1 scale                # reset a setting to its default
rm t1p1 root  |  bpm  |  channel  |  division  |  type  |  controller  |  port
```

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
sustain-0 = 0.5+0.5*lfo2
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
`hist [f<k>|s<k>] [live|once|stop]`, `start f<k>`, `stop [f<k>]`, `p<m>`, `t<n>`,
`cp`, `rm`, `panic`, `help`, `/`, `/ <command>`, `exit`.

**LFO (`seq:lfo:1>`)**
`frequency`, `shape`, `phase`, `start`, `stop`, `live [stop]`, `show`,
`start <path>`/`stop <path>` (phrase playback), `cp`, `rm`, `panic`, `help`,
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
| `lfo.py` | waveform evaluation, frequency/phase parsing, LFO graph |
| `velocity.py` | expression grammar for velocity / sustain / microtiming |
| `phrases.py` | phrase expression parser and canonicaliser |
| `scales.py` | scales, note names and MIDI numbers |
| `midi_ports.py`, `list-ports.py` | MIDI port discovery helpers |
