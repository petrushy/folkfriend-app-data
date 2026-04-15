import argparse
import json
import logging
import os
import pathlib
import re

import midi

from tqdm.contrib.concurrent import process_map

logging.basicConfig(level=logging.DEBUG,
                    format='[%(name)s:%(lineno)s] %(message)s')
log = logging.getLogger(os.path.basename(__file__))

# ---------------------------------------------------------------------------
# ID ranges for folkwiki data (must not overlap with thesession IDs)
# ---------------------------------------------------------------------------
TUNE_ID_BASE = 1_000_000
SETTING_ID_BASE = 2_000_000

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

NOTE_NAMES = {
    'C': 'C', 'D': 'D', 'E': 'E', 'F': 'F',
    'G': 'G', 'A': 'A', 'B': 'B',
    'Cb': 'Cb', 'Db': 'Db', 'Eb': 'Eb', 'Fb': 'Fb',
    'Gb': 'Gb', 'Ab': 'Ab', 'Bb': 'Bb',
    'C#': 'C#', 'D#': 'D#', 'E#': 'E#', 'F#': 'F#',
    'G#': 'G#', 'A#': 'A#', 'B#': 'B#',
}

# Regex: optional sharp/flat, then optional mode suffix
_KEY_RE = re.compile(
    r'^([A-G][#b]?)\s*(maj|min|m|dor|mix|lyd|phr|loc|aeo)?',
    re.IGNORECASE
)


def normalize_mode(k_value):
    """Convert a folkwiki K: value to a thesession-style mode string.

    Examples:
        'G'      -> 'Gmajor'
        'Am'     -> 'Aminor'
        'Ddor'   -> 'Ddorian'
        'Gmix'   -> 'Gmixolydian'
        'F#m'    -> 'F#minor'
        'none'   -> 'Cmajor'  (no key specified)
        'HP'     -> 'Cmajor'  (bagpipe/highland pipe — skip)
    """
    k_value = k_value.strip()
    # Special values with no meaningful key
    if k_value.lower() in ('none', 'hp', 'free'):
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
# ABC file parser
# ---------------------------------------------------------------------------

def split_abc_tunes(abc_text):
    """Split a potentially multi-tune ABC file into individual tune blocks.

    Returns a list of strings, each being one complete tune (headers + body).
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


def parse_abc_tune(abc_text):
    """Parse a single ABC tune block and return a dict of extracted fields.

    Returns None if the tune is missing required fields (T:, M:, K:).

    Returned dict keys:
        titles      - list of strings (first = canonical name)
        meter       - string, e.g. '3/4'
        note_len    - string, e.g. '1/8' or '1/16'
        mode        - thesession-style string, e.g. 'Aminor'
        dance       - lowercase dance/rhythm type, e.g. 'polska'
        origin      - origin string, e.g. 'Dalarna' (may be empty)
        abc_body    - the note body (lines after the last header line)
    """
    titles = []
    meter = None
    note_len = '1/8'
    mode = None
    dance = ''
    origin = ''
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
            elif field == 'K':
                # K: must be last header; body starts after this
                mode = normalize_mode(value)
                in_body = True
            elif field == 'R':
                dance = value.lower().strip()
            elif field == 'O':
                origin = value.strip()
            # Other fields (Z:, S:, B:, C:, D:, N:, X:) are ignored
        else:
            in_body = True
            body_lines.append(line)

    if not titles or meter is None or mode is None:
        return None

    abc_body = '\n'.join(body_lines).strip()
    if not abc_body:
        return None

    return {
        'titles': titles,
        'meter': meter,
        'note_len': note_len,
        'mode': mode,
        'dance': dance,
        'origin': origin,
        'abc_body': abc_body,
    }


# ---------------------------------------------------------------------------
# MIDI contour generation (mirrors build_non_user_data.py but with note_len)
# ---------------------------------------------------------------------------

def generate_midi_contour(args):
    """Generate a MIDI contour string for one folkwiki setting.

    args: (setting_dict, midis_dir)
    setting_dict must have: setting_id, meter, mode, note_len, abc_body
    """
    setting, midis_path = args

    abc_header = [
        'X:1',
        'T:',
        f'M:{setting["meter"].strip()}',
        f'L:{setting["note_len"].strip()}',
        f'K:{setting["mode"].strip()}',
    ]
    abc_body = (
        setting['abc_body'].replace('\\', '').replace('\r', '').split('\n')
    )
    abc = '\n'.join(abc_header + abc_body)

    midi_out_path = os.path.join(
        midis_path, f'fw_{setting["setting_id"]}.midi'
    )

    if not os.path.exists(midi_out_path):
        midi.abc_to_midi(abc, midi_out_path)

    if not os.path.exists(midi_out_path):
        # abc2midi failed to produce output (malformed ABC etc.)
        return setting['setting_id'], ''

    try:
        midi_events = midi.midi_as_csv(midi_out_path)
        note_contour = midi.CSVMidiNoteReader(midi_events).to_midi_contour()
    except Exception as e:
        log.warning(
            f'Contour failed for {setting["setting_id"]}: {e}'
        )
        return setting['setting_id'], ''

    return setting['setting_id'], note_contour


# ---------------------------------------------------------------------------
# Alias cleaning (mirrors build_non_user_data.py)
# ---------------------------------------------------------------------------

NON_WORD_CHARS = re.compile('[^a-zA-Z ]')
STOP_WORDS = {"a", "an", "the", "at", "by", "for", "in", "of", "on",
              "to", "up", "and", "as", "but", "or", "nor"}


def clean_alias(alias):
    alias = alias.lower()
    alias = NON_WORD_CHARS.sub('', alias)
    alias = alias.split()
    alias = (w for w in alias if w and w not in STOP_WORDS)
    alias = ((w[:-1] if w.endswith('s') else w) for w in alias)
    alias = ((w if w != 'favorite' else 'favourite') for w in alias)
    return frozenset(sorted(alias))


def deduplicate_aliases(aliases):
    seen = set()
    deduped = []
    for alias in aliases:
        cleaned = clean_alias(alias)
        if cleaned not in seen:
            seen.add(cleaned)
            deduped.append((cleaned, alias))

    deduped = sorted(deduped, key=lambda c: len(c[0]))

    result = []
    for i, (cleaned, alias) in enumerate(deduped):
        is_subset = (cleaned < c for (c, _) in deduped[i:])
        if not any(is_subset):
            result.append(alias)

    return sorted(result)


# ---------------------------------------------------------------------------
# Main build function
# ---------------------------------------------------------------------------

def build_folkwiki_data(parent_dir):
    folkwiki_dir = os.path.join(parent_dir, 'data', 'folkwiki')
    manifest_path = os.path.join(folkwiki_dir, 'manifest.json')
    midis_dir = os.path.join(folkwiki_dir, 'midis')
    output_path = os.path.join(parent_dir, 'data', 'folkwiki-processed.json')

    pathlib.Path(midis_dir).mkdir(parents=True, exist_ok=True)

    if not os.path.exists(manifest_path):
        log.error(f'No manifest found at {manifest_path}. '
                  'Run download_folkwiki_data.py first.')
        return

    with open(manifest_path, 'r', encoding='utf-8') as f:
        manifest = json.load(f)

    log.info(f'Processing {len(manifest)} folkwiki entries from manifest')

    # --- Parse all ABC files into flat setting dicts ---
    raw_settings = []   # list of dicts with all parsed fields + assigned IDs
    raw_aliases = {}    # tune_id -> [name strings]

    tune_counter = 0
    setting_counter = 0

    for hexhash in sorted(manifest.keys()):
        abc_path = os.path.join(folkwiki_dir, f'{hexhash}.abc')
        if not os.path.exists(abc_path):
            log.warning(f'Missing file for {hexhash}, skipping')
            continue

        with open(abc_path, 'r', encoding='utf-8', errors='replace') as f:
            abc_text = f.read()

        tune_blocks = split_abc_tunes(abc_text)

        for block in tune_blocks:
            parsed = parse_abc_tune(block)
            if parsed is None:
                log.debug(f'Skipping unparseable tune block in {hexhash}')
                continue

            tune_id = str(TUNE_ID_BASE + tune_counter)
            setting_id = str(SETTING_ID_BASE + setting_counter)
            tune_counter += 1
            setting_counter += 1

            raw_settings.append({
                'setting_id': setting_id,
                'tune_id': tune_id,
                'meter': parsed['meter'],
                'mode': parsed['mode'],
                'note_len': parsed['note_len'],
                'abc_body': parsed['abc_body'],
                'dance': parsed['dance'],
                'origin': parsed['origin'],
                'source_url': manifest[hexhash]['url'],
            })

            # Build aliases from all T: fields
            titles = [t.lower() for t in parsed['titles']]
            # canonical name first, then deduplicated rest
            if titles:
                canonical = titles[0]
                rest = (
                    deduplicate_aliases(titles[1:])
                    if len(titles) > 1 else []
                )
                raw_aliases[tune_id] = (
                    [canonical] + [a for a in rest if a != canonical]
                )
            else:
                raw_aliases[tune_id] = []

    log.info(f'Parsed {len(raw_settings)} settings from folkwiki ABC files')

    # --- Generate MIDI contours (parallel) ---
    mp_input = [(s, midis_dir) for s in raw_settings]
    contours = process_map(
        generate_midi_contour,
        mp_input,
        desc='Generating folkwiki MIDI contours',
        chunksize=8,
    )

    # --- Assemble output ---
    settings = {}
    for s in raw_settings:
        settings[s['setting_id']] = {
            'tune_id':    s['tune_id'],
            'meter':      s['meter'],
            'mode':       s['mode'],
            'abc':        s['abc_body'],
            'dance':      s['dance'],
            'origin':     s['origin'],
            'source_url': s['source_url'],
            # contour added below
        }

    for setting_id, contour in contours:
        if contour:
            settings[setting_id]['contour'] = contour
        else:
            settings[setting_id]['contour'] = ''

    output = {
        'settings': settings,
        'aliases':  raw_aliases,
    }

    log.info(f'Writing {output_path}')
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False)

    log.info(f'Done. {len(settings)} folkwiki settings written.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Build folkwiki contour index from downloaded ABC files')
    parser.add_argument(
        'dir', help='Parent directory for the `data` directory'
    )
    args = parser.parse_args()
    build_folkwiki_data(args.dir)
