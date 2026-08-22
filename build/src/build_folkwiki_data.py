import argparse
import json
import logging
import os
import pathlib

from abc_common import (
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

def build_folkwiki_data(parent_dir):
    folkwiki_dir = os.path.join(parent_dir, 'data', 'folkwiki')
    manifest_path = os.path.join(folkwiki_dir, 'manifest.json')
    pageid_path = os.path.join(folkwiki_dir, 'hexhash_to_pageid.json')
    midis_dir = os.path.join(folkwiki_dir, 'midis')
    output_path = os.path.join(parent_dir, 'data', 'folkwiki.json')

    pathlib.Path(midis_dir).mkdir(parents=True, exist_ok=True)

    if not os.path.exists(manifest_path):
        log.error(f'No manifest found at {manifest_path}. '
                  'Run download_folkwiki_data.py first.')
        return

    with open(manifest_path, 'r', encoding='utf-8') as f:
        manifest = json.load(f)

    hexhash_to_pageid = {}
    if os.path.exists(pageid_path):
        with open(pageid_path, 'r', encoding='utf-8') as f:
            hexhash_to_pageid = json.load(f)
        log.info(f'Loaded {len(hexhash_to_pageid)} hexhash→pageID mappings')

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

        for tune_block_index, block in enumerate(tune_blocks):
            parsed = parse_abc_tune(block)
            if parsed is None:
                log.debug(f'Skipping unparseable tune block in {hexhash}')
                continue

            tune_id = stable_folkwiki_id(hexhash, tune_block_index, TUNE_ID_BASE)
            setting_id = stable_folkwiki_id(
                hexhash, tune_block_index, SETTING_ID_BASE
            )
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
                'composer': parsed['composer'],
                'source_url': (
                    f'http://www.folkwiki.se/Musik/{hexhash_to_pageid[hexhash]}'
                    if hexhash in hexhash_to_pageid
                    else manifest[hexhash]['url']
                ),
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
            'abc':        s['abc_body'],
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
        description='Build folkwiki contour index from downloaded ABC files')
    parser.add_argument(
        'dir', help='Parent directory for the `data` directory'
    )
    args = parser.parse_args()
    build_folkwiki_data(args.dir)
