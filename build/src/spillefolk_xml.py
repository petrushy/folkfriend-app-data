"""spillefolk MusicXML -> displayable ABC, via Wim Vree's xml2abc.py.

The preferred notation source. MusicXML is the score as entered (in
MuseScore, for about a quarter of the collection), so xml2abc reproduces it
exactly: repeat signs and first/second endings, the upbeat, the bar lines,
chord symbols, and the score's own line breaks. MIDI cannot carry any of
that; see spillefolk_midi.abc for the fallback used when there is no
MusicXML.

xml2abc.py is LGPL and runs only in this build (build/xml2abc.py, git-ignored,
like abc2midi; see CLAUDE.md). Nothing from it ships in the app.
"""

import re
import subprocess
import sys

from abc_common import normalize_mode

XML2ABC_TIMEOUT_SECONDS = 60

FIELD_RE = re.compile(r'^([A-Za-z]):\s*(.*)$')


def run_xml2abc(path):
    out = subprocess.run(
        [sys.executable, './xml2abc.py', str(path)],
        capture_output=True, timeout=XML2ABC_TIMEOUT_SECONDS)
    return out.stdout.decode('utf-8', errors='replace')


def first_voice(lines):
    """Body lines of the first voice only. xml2abc declares voices in the
    header (`V:1 treble nm="Violin"`) and then opens each voice's music with
    a bare `V:<id>` line; a score with a second voice or accompaniment staff
    has more than one."""
    body, voice = [], None
    for line in lines:
        m = re.match(r'^V:\s*(\S+)\s*$', line)
        if m:
            if voice is None:
                voice = m.group(1)
                continue
            if m.group(1) != voice:
                break
            continue
        if voice is not None:
            body.append(line)
    return body


def tidy(body_lines):
    """Lyrics, %% directives and xml2abc's bar-count comments (`| %8`) are
    not music. `$` is xml2abc's mark for a line break in the original score
    (it writes `I:linebreak $`); the stored ABC has no header to declare
    that, so the breaks become real newlines."""
    kept = []
    for line in body_lines:
        if re.match(r'^(%|[wW]:)', line):
            continue
        kept.append(re.sub(r'\s*%\d*\s*$', '', line))
    music = ' '.join(l.strip() for l in kept if l.strip())
    return '\n'.join(p.strip() for p in music.split('$') if p.strip())


def abc(path):
    """{'meter', 'note_len', 'key', 'mode', 'body'} or None."""
    lines = run_xml2abc(path).splitlines()
    head = {}
    for line in lines:
        if line.startswith('V:') and not re.match(r'^V:\s*\S+\s*$', line):
            continue  # a voice declaration, not the start of its music
        m = FIELD_RE.match(line)
        if not m or m.group(1) == 'V':
            if head.get('K'):
                break
            continue
        head.setdefault(m.group(1), m.group(2).strip())
    if 'K' not in head:
        return None
    body = tidy(first_voice(lines))
    if not body:
        return None
    # K: may carry clef and other attributes after the key itself.
    key = head['K'].split()[0] if head['K'].split() else 'C'
    meter = head.get('M', '4/4')
    if not re.fullmatch(r'\d+/\d+|C\|?', meter):
        meter = '4/4'
    return {
        'meter': meter,
        'note_len': head.get('L', '1/8'),
        'key': key,
        'mode': normalize_mode(key),
        'body': body,
    }
