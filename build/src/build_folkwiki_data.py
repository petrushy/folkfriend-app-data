import argparse
import json
import logging
import os
import pathlib
import re
import urllib.parse

from abc_common import (
    abc_for_display,
    contour_for_setting,
    parse_abc_tune,
    split_abc_tunes,
    titles_to_aliases,
)

from tqdm.contrib.concurrent import process_map

logging.basicConfig(level=logging.DEBUG,
                    format='[%(name)s:%(lineno)s] %(message)s')
log = logging.getLogger(os.path.basename(__file__))

# ---------------------------------------------------------------------------
# ID ranges for folkwiki data (must not overlap with thesession or norbeck).
#
# These are NOT small blocks. `stable_folkwiki_id` is
# `base + int(hexhash,16)*100 + block`, and hexhash is six hex digits, so the
# offset reaches 1.68e9: observed tune IDs run to 1,678,715,901 and setting IDs
# to 1,679,715,901. Folkwiki therefore effectively owns everything from 1e6 up
# to ~1.68e9 in BOTH namespaces. Any new source must start above that — see
# NORBECK_TUNE_ID_BASE in build_norbeck_data.py.
#
# The IDs are hash-derived rather than enumerated so that they stay stable when
# new files are added earlier in sort order; user favourites reference them.
# ---------------------------------------------------------------------------
TUNE_ID_BASE = 1_000_000
SETTING_ID_BASE = 2_000_000
ID_BLOCK_MULTIPLIER = 100


def stable_folkwiki_id(hexhash, tune_block_index, base):
    """Derive a stable numeric ID from file hash + tune-block index.

    We keep Folkwiki IDs numeric because other parts of the stack parse them as
    integers, but they must also be stable across rebuilds so user favourites
    and links do not drift when new files are added earlier in sort order.

    `hexhash` is a six-hex-digit Folkwiki source identity and
    `tune_block_index` is the zero-based tune number within that source file.
    """
    if tune_block_index >= ID_BLOCK_MULTIPLIER:
        raise ValueError(
            f'Folkwiki file {hexhash} has {tune_block_index + 1} tune blocks; '
            f'ID_BLOCK_MULTIPLIER={ID_BLOCK_MULTIPLIER} is too small.'
        )

    offset = int(hexhash, 16) * ID_BLOCK_MULTIPLIER + tune_block_index
    return str(base + offset)


def title_from_manifest_url(url, hexhash):
    """A title for a file whose ABC has no T:, from its cache URL.

    The manifest's `name` field lost its non-ASCII letters to mojibake, but
    the URL still carries them, percent-encoded as Latin-1:
    `_Sl%E4ngpolska_efter_FutteriPetter_392126.abc` -> 'Slängpolska efter
    FutteriPetter'.
    """
    stem = urllib.parse.unquote(url.rsplit('/', 1)[-1], encoding='latin-1')
    stem = re.sub(rf'_?{re.escape(hexhash)}\.abc$', '', stem)
    return ' '.join(stem.replace('_', ' ').split()) or None


# ---------------------------------------------------------------------------
# Contour generation
# ---------------------------------------------------------------------------

def generate_midi_contour(args):
    """process_map worker. Module-level so it is picklable."""
    setting, midis_path = args
    # `fw_` prefix keeps this cache disjoint from thesession's, which is keyed
    # on setting_id alone in a different directory.
    midi_out_path = os.path.join(
        midis_path, f'fw_{setting["setting_id"]}.midi'
    )
    contour = contour_for_setting(
        abc_body=setting['abc_body'],
        meter=setting['meter'],
        mode=setting['mode'],
        note_len=setting['note_len'],
        midi_out_path=midi_out_path,
    )
    return setting['setting_id'], contour


# ---------------------------------------------------------------------------
# Main build function
# ---------------------------------------------------------------------------

# A six-hex hash can be shared by two different tunes (eb995b is both
# "Polonesse Comp a Linblad", Musik/3837, and "Vals efter Kopparslagaren",
# Musik/6060). The file on the lowest-numbered page keeps the plain IDs —
# which is the one a hash-keyed build already published — and each further
# file gets its block indices shifted by this much, so the IDs stay distinct
# and stable. Source files hold one or two blocks, far below the offset.
COLLISION_BLOCK_OFFSET = 50


def _page_sort_key(page):
    return (0, int(page), '') if page.isdigit() else (1, 0, page)


def iter_snapshot_files(snapshot_dir):
    """Yield one dict per distinct titled cache file in a folkwiki snapshot
    (see archive_folkwiki.py), in a deterministic order.

    Untitled blocks (32-hex cache names: variations printed under a tune) are
    archived but not indexed, as before. Where the site's cache file was
    damaged by wiki markup — `$[K:F]` and `{(B/c/)}` are PmWiki syntax, and
    the renderer rewrites them (Musik/6448, Musik/2386) — the tune is taken
    from the page source instead, which is what the author wrote. It is
    wrapped exactly as the cache wraps it, so the block index, and with it
    the ID, is unchanged.
    """
    snapshot_dir = pathlib.Path(snapshot_dir)
    index = json.loads((snapshot_dir / 'index.json').read_text(encoding='utf-8'))

    owners = {}  # file -> (page, link)
    for page in sorted(index, key=_page_sort_key):
        entry = index[page]
        if entry.get('status') != 'ok':
            continue
        for link in entry.get('abc', []):
            if link.get('titled'):
                owners.setdefault(link['file'], (page, link))

    files_by_hash = {}
    for file, (page, link) in owners.items():
        files_by_hash.setdefault(link['hexhash'], []).append((page, file))

    for hexhash in sorted(files_by_hash):
        ranked = sorted(files_by_hash[hexhash],
                        key=lambda pf: (_page_sort_key(pf[0]), pf[1]))
        for rank, (page, file) in enumerate(ranked):
            link = owners[file][1]
            text = (snapshot_dir / 'cache' / file).read_text(
                encoding='utf-8', errors='replace')
            if link.get('matches_source') is False:
                text = _source_block_for(snapshot_dir, page, text)
            yield {
                'hexhash': hexhash,
                'block_offset': rank * COLLISION_BLOCK_OFFSET,
                'text': text,
                'source_url': 'http://www.folkwiki.se/Musik/'
                              + urllib.parse.quote(page),
                'fallback_title': title_from_manifest_url(link['url'], hexhash),
            }


def _source_block_for(snapshot_dir, page, cache_text):
    import difflib
    from archive_folkwiki import MUSIC_BLOCK_RE, safe_name
    source = (snapshot_dir / 'pages' / f'{safe_name(page)}.txt').read_text(
        encoding='utf-8', errors='replace')
    blocks = [b.strip('\n') for b in MUSIC_BLOCK_RE.findall(source)]
    best = max(blocks, key=lambda b: difflib.SequenceMatcher(
        None, b, cache_text).ratio())
    log.info(f'{page}: cache file damaged by wiki markup; using page source')
    return f'%%abc-charset utf-8\n\n{best}\n'


def latest_snapshot(parent_dir):
    root = pathlib.Path(parent_dir, 'data', 'folkwiki-snapshots')
    dated = sorted(d for d in root.glob('????-??-??') if d.is_dir()) \
        if root.exists() else []
    return dated[-1] if dated else None


def build_folkwiki_data(parent_dir, snapshot_dir=None):
    folkwiki_dir = os.path.join(parent_dir, 'data', 'folkwiki')
    midis_dir = os.path.join(folkwiki_dir, 'midis')
    output_path = os.path.join(parent_dir, 'data', 'folkwiki.json')

    pathlib.Path(midis_dir).mkdir(parents=True, exist_ok=True)

    snapshot_dir = snapshot_dir or latest_snapshot(parent_dir)
    if not snapshot_dir or not os.path.exists(
            os.path.join(snapshot_dir, 'index.json')):
        log.error('No folkwiki snapshot found. Run '
                  '`python src/archive_folkwiki.py . --unpack` first.')
        raise SystemExit(1)
    log.info(f'Building from snapshot {snapshot_dir}')

    # --- Parse all ABC files into flat setting dicts ---
    raw_settings = []   # list of dicts with all parsed fields + assigned IDs
    raw_aliases = {}    # tune_id -> [name strings]

    for source in iter_snapshot_files(snapshot_dir):
        hexhash = source['hexhash']
        tune_blocks = split_abc_tunes(source['text'])

        for tune_block_index, block in enumerate(tune_blocks):
            parsed = parse_abc_tune(
                block,
                lenient=True,
                fallback_title=source['fallback_title'],
            )
            if parsed is None:
                log.debug(f'Skipping unparseable tune block in {hexhash}')
                continue

            block_id = tune_block_index + source['block_offset']
            tune_id = stable_folkwiki_id(hexhash, block_id, TUNE_ID_BASE)
            setting_id = stable_folkwiki_id(hexhash, block_id, SETTING_ID_BASE)

            raw_settings.append({
                'setting_id': setting_id,
                'tune_id': tune_id,
                'meter': parsed['meter'],
                'mode': parsed['mode'],
                'note_len': parsed['note_len'],
                'abc_body': parsed['abc_body'],
                'abc_display': abc_for_display(parsed),
                'dance': parsed['dance'],
                'origin': parsed['origin'],
                'composer': parsed['composer'],
                'source_url': source['source_url'],
            })

            # Build aliases from all T: fields, canonical name first
            raw_aliases[tune_id] = titles_to_aliases(parsed['titles'])

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
            'abc':        s['abc_display'],
            'dance':      s['dance'],
            'origin':     s['origin'],
            'composer':   s['composer'],
            'source_url': s['source_url'],
            # contour added below
        }

    empty_contours = 0
    for setting_id, contour in contours:
        settings[setting_id]['contour'] = contour
        if not contour:
            empty_contours += 1

    total = len(contours)
    log.info(
        f'folkwiki contours: {total - empty_contours}/{total} non-empty '
        f'({empty_contours} failed, '
        f'{100 * empty_contours / total:.1f}%)'
    )

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
        description='Build folkwiki contour index from a folkwiki snapshot')
    parser.add_argument(
        'dir', help='Parent directory for the `data` directory'
    )
    parser.add_argument(
        '--snapshot',
        help='Snapshot directory to build from (default: the newest under '
             'data/folkwiki-snapshots/)')
    args = parser.parse_args()
    build_folkwiki_data(args.dir, args.snapshot)
