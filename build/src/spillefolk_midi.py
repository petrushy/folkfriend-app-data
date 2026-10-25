"""spillefolk MIDI -> contour and displayable ABC.

spillefolk.dk serves every tune as a MIDI file exported from notation
software, often with more than one track: the melody first, then a second
voice ("andenstemme") and sometimes a chord track. Only the melody may reach
the contour, so both halves here work from the FIRST track that has notes.

The contour goes straight from the MIDI through midi.py's quantiser — no ABC
round trip — because the MIDI is the exact source. The ABC is only for the
app to draw, and comes from abcmidi's midi2abc (build/midi2abc, 5.02-era like
abc2midi; see CLAUDE.md).
"""

import os
import re
import subprocess

import py_midicsv

from abc_common import normalize_mode
from midi import CSVMidiNoteReader

MIDI2ABC_TIMEOUT_SECONDS = 30

# Ticks per quarter the quantiser assumes (abc2midi's 480: 240 = a quaver).
QUANTISER_DIVISION = 480


# --- contour --------------------------------------------------------------

def melody_notes(path):
    """(notes, division) for the melody track. Double stops keep the top note;
    a note-on with velocity 0 is a note-off (midi.py's reader would otherwise
    leave such notes hanging)."""
    fields = [r.strip().split(', ') for r in py_midicsv.midi_to_csv(str(path))]
    header = next(f for f in fields if f[2] == 'Header')
    division = int(header[5])
    note_tracks = sorted({int(f[0]) for f in fields
                          if f[2] == 'Note_on_c' and int(f[5]) > 0})
    if not note_tracks:
        return [], division
    melody = str(note_tracks[0])
    rows = []
    for f in fields:
        if f[0] != melody or f[2] not in ('Note_on_c', 'Note_off_c'):
            continue
        kind = 'Note_off_c' if f[2] == 'Note_on_c' and int(f[5]) == 0 else f[2]
        tick = int(f[1]) * QUANTISER_DIVISION // division
        # midi.py's reader wants bare commas, as midi_as_csv produces.
        rows.append(','.join([f[0], str(tick), kind, f[3], f[4], f[5]]))
    reader = CSVMidiNoteReader(rows)
    top = {}
    for note in reader._notes:
        if note.start not in top or note.pitch > top[note.start].pitch:
            top[note.start] = note
    return sorted(top.values(), key=lambda n: n.start), division


def contour(path):
    notes, _ = melody_notes(path)
    reader = CSVMidiNoteReader([])
    reader._notes = notes
    return reader.to_midi_contour()


# --- ABC ----------------------------------------------------------------------

MAJOR_ROOT_BY_SHARPS = {0: 0, 1: 7, 2: 2, 3: 9, 4: 4, 5: 11, 6: 6, 7: 1,
                        -1: 5, -2: 10, -3: 3, -4: 8, -5: 1, -6: 6, -7: 11}
SHARP_NAMES = ['C', '^C', 'D', '^D', 'E', 'F', '^F', 'G', '^G', 'A', '^A', 'B']
SHARP_KEY = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']
FLAT_KEY = ['C', 'Db', 'D', 'Eb', 'E', 'F', 'Gb', 'G', 'Ab', 'A', 'Bb', 'B']
MODE_BY_INTERVAL = {0: '', 2: 'dor', 4: 'phr', 5: 'lyd', 7: 'mix', 9: 'm',
                    11: 'loc'}
KEY_COMMENT_RE = re.compile(r'%\s*(\d+)\s+(sharps?|flats?)')


def key_name(sharps, last_pitch):
    """A K: value with midi2abc's signature and the tonic the melody ends on.

    midi2abc's -gk only counts sharps or flats and always names the MAJOR key,
    so a Dorian polska ending on D comes out as K:C. Same signature, so every
    accidental midi2abc wrote stays valid; only the name changes. A last note
    outside the scale (rare) keeps the major name.
    """
    root = MAJOR_ROOT_BY_SHARPS.get(sharps, 0)
    names = SHARP_KEY if sharps >= 0 else FLAT_KEY
    if last_pitch is None:
        return names[root]
    tonic = last_pitch % 12
    mode = MODE_BY_INTERVAL.get((tonic - root) % 12)
    if mode is None:
        return names[root]
    return names[tonic] + mode


# About a third of the files carry a meaningless time signature — 1/8, 1/4
# or 1/16, i.e. a numerator of 1 — so midi2abc drew one-beat bars (47 of 98
# waltzes in the first sample). Those get their dance type's usual meter.
# Display only: the contour never depends on bar lines.
METER_BY_DANCE = {
    'po': '3/4', 'va': '3/4', 'ha': '3/4', 'mz': '3/4', 'mn': '3/4',
    'pk': '2/4', 'sc': '2/4', 'en': '2/4', 'ho': '2/4', 'tu': '2/4',
    'mg': '2/4', 'fa': '2/4', 'kr': '2/4', 'tr': '2/4', 're': '2/4',
    'ta': '2/4',
}


def run_midi2abc(path, meter=None):
    cmd = ['./midi2abc', str(path), '-gk', '-noly']
    if meter:
        cmd += ['-m', meter]
    out = subprocess.run(cmd, capture_output=True,
                         timeout=MIDI2ABC_TIMEOUT_SECONDS)
    return out.stdout.decode('utf-8', errors='replace')


NOTE_IN_CHORD_RE = re.compile(r"([=_^]*)([A-Ga-g])([,']*)([\d/]*)(-?)")
CHORD_RE = re.compile(r'\[([^\]|:\[]+)\]([\d/]*)(-?)')
LETTER_PC = {'C': 0, 'D': 2, 'E': 4, 'F': 5, 'G': 7, 'A': 9, 'B': 11}


def _height(acc, letter, octave):
    h = LETTER_PC[letter.upper()] + (12 if letter.islower() else 0)
    h += 12 * octave.count("'") - 12 * octave.count(',')
    return h + acc.count('^') - acc.count('_')


def top_notes_only(body):
    """Reduce every chord to its highest note, as the contour does.

    Some melody tracks are written as full chords (Gånglåt efter Troskari
    Erik: five notes on every beat), which midi2abc renders as stacks no one
    can read. The tie and duration are kept: `[cG-E-C-]2` -> `c2`, and a tie
    on the top note survives.
    """
    def reduce(m):
        notes = NOTE_IN_CHORD_RE.findall(m.group(1))
        if not notes:
            return m.group(0)
        acc, letter, octave, inner, tie = max(
            notes, key=lambda n: _height(n[0], n[1], n[2]))
        return f'{acc}{letter}{octave}{m.group(2) or inner}{tie or m.group(3)}'
    return CHORD_RE.sub(reduce, body)


def _is_rest_only(bar):
    return bool(bar.strip()) and not re.sub(r'[zx]\d*(/\d*)?|\s', '', bar)


def abc(path, dance=None):
    """{'meter', 'note_len', 'mode', 'body'} for display, or None.

    `dance` is the key prefix ('po', 'va', ...), used to replace a bogus
    time signature.

    The body is the first voice only, with no header, lyrics or %%MIDI lines,
    and without the empty bar midi2abc writes when the first note does not
    start at zero.
    """
    text = run_midi2abc(path)
    declared = re.search(r'^M:\s*(\d+)/(\d+)', text, re.M)
    if declared and declared.group(1) == '1' and dance in METER_BY_DANCE:
        text = run_midi2abc(path, METER_BY_DANCE[dance])
    lines = text.splitlines()
    try:
        start = next(i for i, l in enumerate(lines) if l.startswith('X:'))
    except StopIteration:
        return None
    lines = lines[start:]
    head = {}
    sharps = 0
    body = []
    voice = None
    for line in lines:
        m = re.match(r'^([A-Za-z]):\s*(.*)$', line)
        if m and m.group(1) == 'V':
            voice = voice or m.group(2).split()[0]
            if m.group(2).split()[0] != voice:
                break  # a second voice: done with the melody
            continue
        if voice is None:
            if m:
                head[m.group(1)] = m.group(2)
                if m.group(1) == 'K':
                    k = KEY_COMMENT_RE.search(line)
                    if k:
                        n = int(k.group(1))
                        sharps = n if k.group(2).startswith('sharp') else -n
            continue
        if line.startswith('%') or re.match(r'^[wW]:', line):
            continue
        body.append(line)
    if not body:
        return None
    # Drop leading bars that are nothing but rests (one per beat of silence
    # before the first note, sometimes several), and blank lines among them.
    while body:
        if not body[0].strip(' \\'):
            body.pop(0)
            continue
        bar, sep, rest = body[0].partition('|')
        if not (sep and _is_rest_only(bar)):
            break
        body[0] = rest.lstrip(' \\')
    if not body:
        return None
    # A first bar of rest-then-pickup (`z4 Ge-|`) is an upbeat: keep only the
    # notes, which is how a pickup is written.
    bar, sep, rest = body[0].partition('|')
    if sep and not _is_rest_only(bar):
        body[0] = re.sub(r'^\s*(?:[zx]\d*(?:/\d*)?\s*)+', '', bar) + sep + rest
    body = [top_notes_only(line) for line in body]
    notes, _ = melody_notes(path)
    last = notes[-1].pitch if notes else None
    meter = head.get('M', '').strip()
    if not re.fullmatch(r'\d+/\d+|C\|?', meter):
        meter = '4/4'
    k = key_name(sharps, last)
    return {
        'meter': meter,
        'note_len': head.get('L', '1/8').strip(),
        'key': k,
        'mode': normalize_mode(k),
        'body': '\n'.join(body).strip(),
    }
