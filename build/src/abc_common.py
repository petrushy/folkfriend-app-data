"""Shared ABC parsing, alias cleaning and contour generation.

Extracted from build_non_user_data.py and build_folkwiki_data.py, which had
grown near-identical copies of all of this. A third source (Norbeck) would
have made three copies, at which point a fix applied to one and not the others is
inevitable — the passing-note and grace-note bugs of April 2026 both had to
be fixed twice for exactly this reason.

Behaviour here must remain byte-identical to what the two callers did before the
extraction. The regression gate is a full rebuild diffed against the previous
`folkfriend-non-user-data.json`: every contour must match.
"""

import collections
import logging
import os
import re
import unicodedata

import midi

log = logging.getLogger(os.path.basename(__file__))

# ---------------------------------------------------------------------------
# ABC accented-character escapes
#
# ABC files are historically 7-bit ASCII and spell accented letters with
# TeX-style escapes in header fields: `sl\"angpolska`, `G\aard`, `Jos\'e`.
# Norbeck's collection is pure ASCII and uses them 3,500+ times, so without
# this every Swedish and Irish accented title is stored as literal backslash
# noise and cannot be found by searching for the name a user would actually
# type. (folkwiki and thesession deliver real UTF-8 and are unaffected, but the
# decoding is a property of the ABC format, not of one source.)
#
# Applied to header-derived text only — titles, dance, origin, composer. The
# note body is left exactly as written, because that is handed to abc2midi and
# to ABCJS in the app, both of which understand ABC escapes themselves.
# ---------------------------------------------------------------------------

# Multi-character ligatures and standalone letters, checked before the
# combining-accent forms because `\aa` must not be read as `\a` + `a`.
_ABC_LITERAL_ESCAPES = {
    'AA': 'Å', 'aa': 'å', 'AE': 'Æ', 'ae': 'æ', 'OE': 'Œ', 'oe': 'œ',
    'ss': 'ß', 'DH': 'Ð', 'dh': 'ð', 'TH': 'Þ', 'th': 'þ',
    'O': 'Ø', 'o': 'ø', 'i': 'i',   # \i is dotless i; plain i is close enough
    'textquestiondown': '¿', 'textexclamdown': '¡',
}

# TeX brace form. `{\aa}` is how a ligature escape is separated from a
# following letter, and it is the dominant spelling in Norbeck's Swedish
# titles: 796 occurrences of `{\aa}`/`{\AA}` against none of the bare form.
# Missing this leaves `gärdebyl{å}ten` — accents fixed, braces left behind —
# which is arguably worse than not decoding at all, because it looks decoded.
_ABC_BRACED_RE = re.compile(
    r'\{\\(' + '|'.join(sorted(_ABC_LITERAL_ESCAPES, key=len, reverse=True))
    + r')\}'
)

# Accent character -> Unicode combining mark, composed with NFC afterwards.
_ABC_COMBINING = {
    '"': '̈',   # diaeresis
    "'": '́',   # acute
    '`': '̀',   # grave
    '^': '̂',   # circumflex
    '~': '̃',   # tilde
    '=': '̄',   # macron
    '.': '̇',   # dot above
    'c': '̧',   # cedilla
    'H': '̋',   # double acute
    'v': '̌',   # caron
    'u': '̆',   # breve
    'r': '̊',   # ring above
}

_ABC_ESCAPE_RE = re.compile(
    r'\\(AA|aa|AE|ae|OE|oe|ss|DH|dh|TH|th)'      # ligatures first
    r'|\\([\"\'`^~=.cHvur])([A-Za-z])'           # accent + letter
    r'|\\([Ooi])(?![A-Za-z])'                    # standalone \O \o \i
)


def decode_abc_escapes(text):
    """Turn ABC/TeX accent escapes into real Unicode characters.

    `sl\\"angpolska` -> `slängpolska`, `G\\aard` -> `Gård`.
    Text with no escapes is returned unchanged.
    """
    if not text or '\\' not in text:
        return text

    text = _ABC_BRACED_RE.sub(lambda m: _ABC_LITERAL_ESCAPES[m.group(1)], text)

    def repl(m):
        if m.group(1):
            return _ABC_LITERAL_ESCAPES[m.group(1)]
        if m.group(2):
            return unicodedata.normalize(
                'NFC', m.group(3) + _ABC_COMBINING[m.group(2)])
        return _ABC_LITERAL_ESCAPES[m.group(4)]

    return _ABC_ESCAPE_RE.sub(repl, text)


# ---------------------------------------------------------------------------
# Alias cleaning
# ---------------------------------------------------------------------------

STOP_WORDS = {"a", "an", "the", "at", "by", "for", "in", "of", "on",
              "to", "up", "and", "as", "but", "or", "nor"}
NON_WORD_CHARS = re.compile('[^a-zA-Z ]')


def clean_alias(alias):
    # Remove redundancy from each string
    alias = alias.lower()
    alias = NON_WORD_CHARS.sub('', alias)
    alias = alias.split()
    alias = (w for w in alias if w and w not in STOP_WORDS)
    alias = ((w[:-1] if w.endswith('s') else w)
             for w in alias)  # Ignore plurals

    # This American spelling pops up a lot. Retain the British spelling
    #   for alias purposes.
    alias = ((w if not w == 'favorite' else 'favourite') for w in alias)
    return frozenset(sorted(alias))


def deduplicate_aliases(aliases):

    seen_aliases = set()
    deduped_aliases = []

    # Remove based on minor differences in punctuation / stopwords
    for alias in aliases:
        cleaned_alias = clean_alias(alias)
        if cleaned_alias not in seen_aliases:
            seen_aliases.add(cleaned_alias)
            deduped_aliases.append((cleaned_alias, alias))

    deduped_aliases = sorted(deduped_aliases, key=lambda c: len(c[0]))

    # Remove subsets. Requires sorting by length.
    deduped_aliases_no_subsets = []
    for i, (cleaned, alias) in enumerate(deduped_aliases):
        is_subset = (cleaned < c for (c, _) in deduped_aliases[i:])
        if not any(is_subset):
            deduped_aliases_no_subsets.append(alias)

    # Back to alphabetical at the end
    return sorted(deduped_aliases_no_subsets)


def gather_aliases(alias_records, tune_data):
    """Condense thesession's aliases.json and fold each tune's `name` in.

    The aliases.json file is inefficiently structured for network distribution.
    We also merge the "name" field of each tune into aliases, so all names are
    stored in one place and each name/alias is stored exactly once.
    """
    aliases = collections.defaultdict(list)

    # Add aliases from alias data proper
    for alias_record in sorted(alias_records, key=lambda r: int(r['tune_id'])):
        tid = alias_record['tune_id']
        alias = alias_record['alias'].lower()
        aliases[tid].append(alias)

    for tid in aliases:
        aliases[tid] = deduplicate_aliases(aliases[tid])

    # Add 'name' fields so that the first alias is always the 'name'
    #   on the session. This is usually the most common name for the
    #   tune, so we never de-dupe this and make sure it's always the
    #   first alias.
    for i, _ in enumerate(tune_data):
        tid = tune_data[i]['tune_id']
        alias = tune_data[i]['name'].lower()

        # Tune name is identical accross settings to only add once per setting.
        if not aliases[tid] or aliases[tid][0] != alias:
            aliases[tid].insert(0, alias)

    for alias_list in aliases.values():
        assert alias_list

    return aliases


def titles_to_aliases(titles):
    """Build an alias list from a tune's T: fields, canonical name first.

    Used by the file-based sources (folkwiki, norbeck) where there is no
    separate aliases dataset — every name comes from a T: header.
    """
    titles = [t.lower() for t in titles]
    if not titles:
        return []
    canonical = titles[0]
    rest = deduplicate_aliases(titles[1:]) if len(titles) > 1 else []
    return [canonical] + [a for a in rest if a != canonical]


# ---------------------------------------------------------------------------
# ABC mode normalization
# Map the suffix part of a K: value (after stripping the note letter) to the
# thesession-style suffix.
# ---------------------------------------------------------------------------

MODE_SUFFIX_MAP = {
    '':        'major',
    'maj':     'major',
    'm':       'minor',
    'min':     'minor',
    'dor':     'dorian',
    'mix':     'mixolydian',
    'lyd':     'lydian',
    'phr':     'phrygian',
    'loc':     'locrian',
    'aeo':     'minor',     # aeolian = natural minor
}

# Regex: optional sharp/flat, then optional mode suffix
_KEY_RE = re.compile(
    r'^([A-G][#b]?)\s*(maj|min|m|dor|mix|lyd|phr|loc|aeo)?',
    re.IGNORECASE
)

# K: values that carry no usable key. 'hp'/'highland' are bagpipe notations;
# abc2midi understands them but they do not map onto a thesession mode string.
_KEYLESS_VALUES = ('none', 'hp', 'h p', 'free', '')


def normalize_mode(k_value):
    """Convert an ABC K: value to a thesession-style mode string.

    Examples:
        'G'          -> 'Gmajor'
        'Am'         -> 'Aminor'
        'Ddor'       -> 'Ddorian'
        'Gmix'       -> 'Gmixolydian'
        'F#m'        -> 'F#minor'
        'D exp _b'   -> 'Dmajor'   (explicit-accidental tail dropped)
        'none'/'HP'  -> 'Cmajor'   (no meaningful key)
    """
    k_value = k_value.strip()
    # Special values with no meaningful key
    if k_value.lower() in _KEYLESS_VALUES:
        return 'Cmajor'
    m = _KEY_RE.match(k_value)
    if not m:
        log.warning(
            f'Cannot parse K: value {k_value!r}, defaulting to Cmajor'
        )
        return 'Cmajor'

    note = m.group(1)
    # Normalise note capitalisation: first char upper, rest lower
    if len(note) > 1:
        note = note[0].upper() + note[1:].lower()
    else:
        note = note.upper()

    suffix_raw = (m.group(2) or '').lower()
    suffix = MODE_SUFFIX_MAP.get(suffix_raw, 'major')

    return f'{note}{suffix}'


# ---------------------------------------------------------------------------
# ABC file structure
# ---------------------------------------------------------------------------

def split_abc_tunes(abc_text):
    """Split a potentially multi-tune ABC file into individual tune blocks.

    Returns a list of strings, each being one complete tune (headers + body).
    Splits on any line starting `X:`, which is the layout used by both
    folkwiki's multi-tune files and Norbeck's per-rhythm files.
    """
    tunes = []
    current = []
    for line in abc_text.splitlines():
        stripped = line.strip()
        if stripped.startswith('X:') and current:
            tunes.append('\n'.join(current))
            current = []
        current.append(line)
    if current:
        tunes.append('\n'.join(current))
    return [t for t in tunes if t.strip()]


def extract_primary_voice_body_lines(body_lines):
    """Keep only the primary melody voice from a body with multiple voices.

    Sources often store a melody on the upper staff (`V:1`) and an
    accompaniment/harmony on a lower staff (`V:2`). For contour matching we
    want the melody only; mixing both voices into one MIDI contour makes the
    search representation much noisier than TheSession's mostly single-line
    tunes.

    Strategy:
      - If there are no `V:` body markers, keep the body unchanged.
      - If there are multiple voices, keep only the first voice encountered.
      - Ignore subsequent voice sections entirely.
    """
    voice_re = re.compile(r'^\s*V:\s*([^\s]+)')

    primary_voice = None
    active_voice = None
    has_voice_markers = False
    kept = []

    for line in body_lines:
        match = voice_re.match(line)
        if match:
            has_voice_markers = True
            active_voice = match.group(1)
            if primary_voice is None:
                primary_voice = active_voice
            continue

        if not has_voice_markers:
            kept.append(line)
            continue

        if active_voice == primary_voice:
            kept.append(line)

    return kept if has_voice_markers else body_lines


def parse_abc_tune(abc_text, extra_fields=()):
    """Parse a single ABC tune block and return a dict of extracted fields.

    Returns None if the tune is missing required fields (T:, M:, K:) or has an
    empty body.

    `extra_fields` names additional single-letter header fields to capture
    verbatim into the result under `extra[<letter>]` — a list, since ABC allows
    a field to repeat (Norbeck uses several `N:` and `D:` lines per tune).

    Returned dict keys:
        titles      - list of strings (first = canonical name)
        meter       - string, e.g. '3/4'
        note_len    - string, e.g. '1/8' or '1/16'
        has_note_len- True only if the source actually carried an L: field;
                      `note_len` is the ABC default otherwise, and callers who
                      care (see contour_for_setting) should pass None instead
        mode        - thesession-style string, e.g. 'Aminor'
        dance       - lowercase dance/rhythm type, e.g. 'polska'
        origin      - origin string, e.g. 'Dalarna' (may be empty)
        composer    - composer string (may be empty)
        abc_body    - the note body (lines after the last header line)
        extra       - dict of letter -> [values], for `extra_fields`
    """
    titles = []
    meter = None
    note_len = '1/8'
    has_note_len = False
    mode = None
    dance = ''
    origin = ''
    composer = ''
    extra = {f.upper(): [] for f in extra_fields}
    body_lines = []
    in_body = False

    for line in abc_text.splitlines():
        stripped = line.strip()
        if not stripped:
            if in_body:
                body_lines.append(line)
            continue

        # Once we hit a non-header line (doesn't match 'X:', 'T:', etc.)
        # we're in the body.  A line with a colon in position 1 is a header.
        if not in_body and len(stripped) >= 2 and stripped[1] == ':':
            field = stripped[0].upper()
            value = stripped[2:].strip()

            if field == 'T':
                titles.append(value)
            elif field == 'M':
                meter = value
            elif field == 'L':
                # e.g. '1/8', '1/16'
                note_len = value.strip()
                has_note_len = True
            elif field == 'K':
                # K: must be last header; body starts after this
                mode = normalize_mode(value)
                in_body = True
            elif field == 'R':
                dance = value.lower().strip()
            elif field == 'O':
                origin = value.strip()
            elif field == 'C':
                composer = value.strip()

            if field in extra:
                extra[field].append(value)
            # Other fields (Z:, S:, B:, D:, N:, X:) are ignored unless named
            # in extra_fields.
        else:
            in_body = True
            body_lines.append(line)

    if not titles or meter is None or mode is None:
        return None

    abc_body = '\n'.join(extract_primary_voice_body_lines(body_lines)).strip()
    if not abc_body:
        return None

    return {
        'titles': titles,
        'meter': meter,
        'note_len': note_len,
        'has_note_len': has_note_len,
        'mode': mode,
        'dance': dance,
        'origin': origin,
        'composer': composer,
        'abc_body': abc_body,
        'extra': extra,
    }


# ---------------------------------------------------------------------------
# MIDI contour generation
# ---------------------------------------------------------------------------

def strip_abc_ornaments(abc_body):
    """Remove everything abc2midi would sound that is not the melody.

    Three distinct ABC constructs must go before abc2midi, because it renders
    all of them as real MIDI notes that contaminate the contour read back by
    CSVMidiNoteReader.

    1. String chord symbols  "D", "Am", "A7", …
       abc2midi plays these on a separate MIDI channel as accompaniment.
       Strip entirely. (This also eats "^text" / "_text" annotations, which is
       what we want.)

    2. Bracket chord notes  [CEG], [A,E]2, …
       abc2midi plays all voices simultaneously on the same channel.
       ~32% of folkwiki settings use these, vs ~5% for TheSession.
       Keep only the first note (e.g. [A,E] → A,   [CEG]2 → C2).
       Guard: [|  [1  [2  are bar/repeat markers — skip. Inline headers such
       as [K:G] are left alone because K/M/L/V are outside the [A-Ga-g] note
       class the pattern requires.

    3. Grace notes  {g}, {fg}, …
       These produce ~59 ms MIDI events, which land in the "sub-quaver → emit
       as one quaver" path and insert spurious characters between melody
       notes. Real-audio queries have no ornaments, so these are pure noise.
       midi.py drops anything under 0.35 quaver as a second line of defence.
    """
    abc_body = re.sub(r'"[^"]*"', '', abc_body)
    abc_body = re.sub(
        r'\[(?![|:\d])([=_^]?[A-Ga-g][,\']*\d*)[^\]]*\]',
        r'\1', abc_body)
    abc_body = re.sub(r'\{[^}]*\}', '', abc_body)  # strip grace notes
    return abc_body


def contour_for_setting(abc_body, meter, mode, note_len, midi_out_path):
    """ABC body -> quaver-quantised contour string. '' on any failure.

    A synthetic header is used rather than the source file's, so that meter,
    unit note length and key are exactly what the caller resolved. `note_len`
    matters: TheSession is always L:1/8, folkwiki is frequently L:1/16, and
    Norbeck varies by rhythm. Getting it wrong halves or doubles every duration
    and silently destroys matching.

    `note_len` of None means the source had no L: field. In that case the L:
    line is OMITTED rather than defaulted to 1/8, so abc2midi applies the ABC
    standard rule (unit note length is 1/16 when the meter is below 0.75,
    otherwise 1/8) — exactly as it would when rendering the original file.
    Substituting 1/8 here would be wrong for every tune in 2/4 or 3/8, which is
    290 of Norbeck's 3,473, and would halve every one of their note durations.

    `midi_out_path` doubles as the cache: an existing file is reused, which is
    what makes incremental rebuilds fast. Each source must use its own
    directory or filename prefix, or the caches alias across sources.
    """
    abc_header = [
        'X:1',
        'T:',
        f'M:{meter.strip()}',
    ]
    if note_len is not None:
        abc_header.append(f'L:{note_len.strip()}')
    abc_header.append(f'K:{mode.strip()}')
    cleaned = strip_abc_ornaments(abc_body)
    body = cleaned.replace('\\', '').replace('\r', '').split('\n')
    abc = '\n'.join(abc_header + body)

    if not os.path.exists(midi_out_path):
        midi.abc_to_midi(abc, midi_out_path)

    if not os.path.exists(midi_out_path):
        # abc2midi failed to produce output (malformed ABC, unsupported syntax)
        return ''

    try:
        midi_events = midi.midi_as_csv(midi_out_path)
        return midi.CSVMidiNoteReader(midi_events).to_midi_contour()
    except Exception:
        return ''
