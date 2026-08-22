"""
midi.py — MIDI intermediary for the ABC → contour pipeline.

Pipeline position:
  ABC text  →  [abc2midi]  →  MIDI file  →  [py-midicsv]  →  CSV event list
                                                            →  CSVMidiNoteReader
                                                            →  to_midi_contour()
                                                            →  contour string

The contour string is a sequence of ASCII characters where each character
represents one quaver (eighth note) of the melody, encoded by MIDI pitch.
See ff_config.py for the pitch-to-character mapping (MIDI 48–95 → ascii_letters).
"""

import csv
import logging
import math
import os
import subprocess
import sys

import py_midicsv
import ff_config

logging.basicConfig(level=logging.DEBUG,
                    format='[%(name)s:%(lineno)s] %(message)s')
log = logging.getLogger(os.path.basename(__file__))
ABC_TO_MIDI_TIMEOUT_SECONDS = 30


class CSVMidiNoteReader(csv.DictReader):
    """Parse a py-midicsv event list into a list of Notes, then a contour string.

    Only reads notes from all MIDI channels combined.  Callers are responsible
    for stripping ABC chord symbols ("Am") and bracket chords ([CEG]) from the
    ABC *before* calling abc2midi, so that only melody notes reach this reader.
    """

    def __init__(self, *posargs, **kwargs):
        kwargs['fieldnames'] = ['track', 'time', 'type', 'channel',
                                'note', 'velocity']
        super().__init__(*posargs, **kwargs)
        self._notes = self.to_notes()

    def to_notes(self):
        """Convert MIDI event records to a time-ordered list of Note objects."""

        active_notes = {}
        notes = []

        for record in self:
            if not record['note'] or not record['note'].isdigit():
                continue
            note = int(record['note'])
            time = int(record['time'])

            if record['type'] == 'Note_on_c':
                if note not in active_notes:
                    active_notes[note] = time
            elif record['type'] == 'Note_off_c':
                if note not in active_notes:
                    continue

                note_end = time
                note_start = active_notes.pop(note)

                notes.append(Note(start=note_start,
                                  end=note_end,
                                  pitch=note))

        return notes

    def to_midi_contour(self, tempo=125, start_seconds=None, end_seconds=None):
        """Quantise a sequence of MIDI notes into a quaver-resolution contour string.

        Every note becomes one or more quaver-duration slots.  A dotted quarter
        note (1.5 quavers) rounds to 2 slots; a semiquaver rounds to 1.  The
        resulting string has one character per quaver slot, so its length is
        proportional to the tune's total duration in eighth notes.

        Two clocks are maintained to track quantisation drift:
          music_time  — actual elapsed real time of the note sequence
          output_time — elapsed time as written into the contour so far

        When output_time is ahead of music_time (e.g. the previous dotted note
        was rounded *up*, overshooting the short note that follows), the short
        note is still emitted as one quaver.  Without this, passing notes in
        patterns like A>B would be silently dropped, causing stored contours to
        diverge from audio-transcribed contours that always retain such notes.
        """

        midi_contour = []

        music_time = 0
        output_time = 0

        # Compute the real-time duration of one quaver at this tempo.
        # Default abc2midi MIDI times: 240 raw units = 1 quaver at 125 bpm.
        n = Note(0, 240, None)
        n.set_tempo(tempo)
        quaver_duration = n.duration

        for _, note in enumerate(self._notes):
            note.set_tempo(tempo)

            # Skip notes outside the requested time window.
            if start_seconds and note.end < 1000 * start_seconds:
                music_time = note.end
                output_time = note.end
                continue
            if end_seconds and note.start > 1000 * end_seconds:
                break
            else:
                music_time += note.duration

            # Output is ahead of music (previous note was rounded up):
            # still include this note as exactly 1 quaver so passing notes
            # in dotted-rhythm patterns (A>B, A>G) are never silently dropped.
            if music_time <= output_time:
                output_time += quaver_duration
                midi_contour.append(note.rel_pitch())
                continue

            rel_duration = (note.duration / quaver_duration)
            if rel_duration.is_integer():
                output_time += note.duration
                midi_contour.extend([note.rel_pitch()] * int(rel_duration))
            elif rel_duration < 1.0:
                if rel_duration < 0.35:
                    # Grace-note ornament that slipped through stripping: skip.
                    # Don't advance output_time — these are not melody rhythm.
                    continue
                # Sub-quaver note: emit as one quaver (minimum resolution).
                output_time += quaver_duration
                midi_contour.append(note.rel_pitch())
            else:
                # Non-integer multiple: round toward the direction that keeps
                # output_time in sync with music_time.
                round_f = math.ceil if music_time > output_time else math.floor
                rounded_int = round_f(rel_duration)
                output_time += rounded_int * quaver_duration
                midi_contour.extend([note.rel_pitch()] * rounded_int)

        return ''.join(ff_config.MIDI_MAP[n] for n in midi_contour)



class Note:
    """A single MIDI note with start time, end time, and pitch.

    Times are in raw MIDI units until set_tempo() is called, after which
    they are in milliseconds scaled to the actual tempo.
    """

    def __init__(self, start, end, pitch):
        self._midi_start = start
        self._midi_end = end
        self.pitch = pitch

        self.start = self._midi_start
        self.end = self._midi_end

    def set_tempo(self, tempo):
        """Scale raw MIDI times to milliseconds for the given tempo (BPM).

        abc2midi encodes note times using 240 raw units = 1 quaver at 125 bpm
        (i.e. 480 000 µs = 1 crotchet).  The tempo command in the MIDI file
        specifies µs-per-crotchet; this method applies that scale factor so
        that self.start/end are in real milliseconds regardless of tempo.
        """
        us_per_crotchet = 60000000. / tempo
        # 480 000 = default µs/crotchet at 125 bpm (see above).
        ms_scale_factor = us_per_crotchet / 480000

        self.start = ms_scale_factor * self._midi_start
        self.end = ms_scale_factor * self._midi_end

    @property
    def duration(self):
        return self.end - self.start

    def rel_pitch(self):
        """Return the pitch as an index into ff_config.MIDI_MAP (0-based from MIDI_LOW).

        Notes outside the supported range [MIDI_LOW+1, MIDI_HIGH-1] are
        octave-shifted into it.  The ≤/≥ bounds (not strict < >) ensure a
        small frequency margin on both sides of the boundary pitch bins,
        matching the behaviour of the audio feature extractor.
        """
        pitch = self.pitch

        while pitch <= ff_config.MIDI_LOW:
            pitch += 12

        while pitch >= ff_config.MIDI_HIGH:
            pitch -= 12

        return pitch - ff_config.MIDI_LOW


def abc_to_midi(abc, midi_path, clean=True):
    """Convert ABC text into a midi file."""

    # Generate MIDI file with chords and actual instruments
    try:
        captured = subprocess.run([
            './abc2midi', '-',
            '-quiet', '-silent',
            '-NGUI' if clean else '',
            '-o', midi_path
        ],
            input=abc.encode('utf-8'),
            capture_output=True,
            timeout=ABC_TO_MIDI_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        log.warning(
            f'abc2midi timed out after {ABC_TO_MIDI_TIMEOUT_SECONDS}s for '
            f'{midi_path}'
        )
        return
    stderr = captured.stderr.decode('utf-8')
    if stderr:
        log.warning(stderr, file=sys.stderr)


def midi_as_csv(midi_path):
    midi_lines = py_midicsv.midi_to_csv(midi_path)
    return [line.strip().replace(', ', ',') for line in midi_lines]
