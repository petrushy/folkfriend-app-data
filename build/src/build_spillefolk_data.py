"""Build data/spillefolk.json — a PRIVATE dataset, never published.

Input is what download_spillefolk_data.py fetched into data/spillefolk/
(catalogue.json + mid/<key>.mid). See that module for why this dataset is
built for personal import only, like Norbeck.

Each tune becomes one setting of its own tune:
- contour: straight from the MIDI's melody track (spillefolk_midi.contour);
           from the notation via abc2midi only when there is no MIDI;
- abc:     the score's MusicXML through xml2abc when the site has it (about a
           quarter: exact repeats, endings, upbeats, bar lines); otherwise
           midi2abc's rendering of the MIDI, which cannot carry those. Either
           way with its own L: on top (the app assumes L:1/8 otherwise);
- meter / mode: from that rendering (mode named from the tune's last note);
- dance:   from the heading the site files the tune under;
- origin:  the site's regions and country;
- source_url: the tune's page on spillefolk.dk.

The file is stamped with id/label/description/v/date here, so it can be
imported as soon as it is built without running assemble_datasets.py (which
would also re-stamp the published datasets).

Usage:
    python src/build_spillefolk_data.py .
"""

import argparse
import datetime
import hashlib
import json
import logging
import os
import pathlib
import re
import sys

from abc_common import contour_for_setting, titles_to_aliases
from tqdm.contrib.concurrent import process_map

import spillefolk_midi
import spillefolk_xml

logging.basicConfig(level=logging.INFO, format='[%(name)s] %(message)s')
log = logging.getLogger(os.path.basename(__file__))

# ---------------------------------------------------------------------------
# ID ranges — see "ID ranges" in CLAUDE.md before changing these.
#
# Norbeck's setting IDs reach ~4.4e11 (8e9 + an 8-hex offset * 100), so this
# source starts at 1e12 / 2e12. Offsets come from the site's own tune key, so
# an ID survives a rebuild and a re-download, and favourites keep working.
# Highest possible: 2e12 + 0xffffffff * 100 ≈ 2.43e12, far below 2**53.
# ---------------------------------------------------------------------------
SPILLEFOLK_TUNE_ID_BASE = 1_000_000_000_000
SPILLEFOLK_SETTING_ID_BASE = 2_000_000_000_000
SETTING_MULTIPLIER = 100

DATASET_ID = 'spillefolk'
LABEL = 'Spillefolk'
DESCRIPTION = ('Danish and Nordic tunes from Folkets Hus Spillefolk '
               '(spillefolk.dk) — personal use, not for redistribution')

COUNTRY = {'DK': 'Danmark', 'S': 'Sverige', 'N': 'Norge', 'SF': 'Finland'}

# The site files every tune under one heading; the key prefix names it.
DANCE = {
    'en': 'engelska', 'fa': 'sønderhoning', 'ha': 'hambo', 'ho': 'hopsa',
    'kr': 'kredsdans', 'mg': 'marsch', 'mn': 'menuett', 'mz': 'mazurka',
    'pk': 'polka', 'po': 'polska', 're': 'reel', 'sc': 'schottis',
    'ta': 'tango', 'tr': 'trekant', 'tu': 'turdans', 'va': 'vals', 'xx': '',
}

# "Dalajärna, Skänklåt från" / "Halv kæde, Den": the site sorts by the
# distinctive word, so the title is often inverted. The natural order is
# added as an alias, so a search for what people actually call it works.
INVERSION_TAIL_RE = re.compile(
    r'^(?:.*\s)?(?:från|fra|fr\.|efter|e\.|af|av|i|til|till)$'
    r'|^(?:den|det|de|en|et|ett|the)$', re.I)


def stable_id(key, base, multiplier=1):
    offset = int(hashlib.sha1(key.encode('utf-8')).hexdigest()[:8], 16)
    return str(base + offset * multiplier)


def titles_for(entry):
    title = entry['title']
    titles = [title]
    head, sep, tail = title.rpartition(', ')
    if sep and head and tail and INVERSION_TAIL_RE.match(tail.strip()):
        titles.append(f'{tail.strip()} {head.strip()}')
    return titles


def origin_for(entry):
    country = COUNTRY.get(entry['country'], entry['country'])
    parts = [p for p in (entry['regions'], country) if p]
    return ', '.join(parts)


def convert(args):
    """process_map worker: (key, midi path | None, xml path | None, midis dir)
    -> (key, result | error string)."""
    key, midi, xml, midis_dir = args
    try:
        rendered, notation = None, None
        if xml:
            rendered, notation = spillefolk_xml.abc(xml), 'musicxml'
        if not rendered and midi:
            rendered = spillefolk_midi.abc(midi, key.split('-')[0])
            notation = 'midi'
        if not rendered:
            return key, 'no usable notation'
        contour = spillefolk_midi.contour(midi) if midi else ''
        if not contour:
            # No MIDI, or one with no notes in it (sc-sulhusgubben): make the
            # contour from the notation, as the other datasets do.
            contour = contour_for_setting(
                abc_body=rendered['body'], meter=rendered['meter'],
                mode=rendered['mode'], note_len=rendered['note_len'],
                midi_out_path=os.path.join(midis_dir, f'sf_{key}.midi'))
    except Exception as e:  # one malformed file must not stop the build
        return key, f'{type(e).__name__}: {e}'
    return key, {**rendered, 'contour': contour, 'notation': notation}


def build(parent_dir):
    data = pathlib.Path(parent_dir, 'data')
    src = data / 'spillefolk'
    catalogue_path = src / 'catalogue.json'
    if not catalogue_path.exists():
        sys.exit('No data/spillefolk/catalogue.json — run '
                 'download_spillefolk_data.py first.')
    for tool in ('./midi2abc', './xml2abc.py'):
        if not os.path.exists(tool):
            sys.exit(f'Missing {tool}. See "Spillefolk" in CLAUDE.md.')
    catalogue = json.loads(catalogue_path.read_text(encoding='utf-8'))

    midis_dir = src / 'midis'
    midis_dir.mkdir(exist_ok=True)
    jobs = []
    for k in sorted(catalogue):
        midi, xml = src / 'mid' / f'{k}.mid', src / 'xml' / f'{k}.xml'
        if midi.exists() or xml.exists():
            jobs.append((k, str(midi) if midi.exists() else None,
                         str(xml) if xml.exists() else None, str(midis_dir)))
    missing = len(catalogue) - len(jobs)
    log.info(f'{len(jobs)} tunes with MIDI or MusicXML ({missing} with '
             'neither)')

    results = process_map(convert, jobs, chunksize=8,
                          desc='Converting spillefolk MIDI')

    settings, aliases, failed = {}, {}, []
    by_notation = {'musicxml': 0, 'midi': 0}
    for key, result in results:
        if isinstance(result, str):
            failed.append((key, result))
            continue
        entry = catalogue[key]
        by_notation[result['notation']] += 1
        tune_id = stable_id(key, SPILLEFOLK_TUNE_ID_BASE)
        setting_id = stable_id(key, SPILLEFOLK_SETTING_ID_BASE,
                               SETTING_MULTIPLIER)
        settings[setting_id] = {
            'tune_id': tune_id,
            'meter': result['meter'],
            'mode': result['mode'],
            'abc': f"L:{result['note_len']}\n{result['body']}",
            'dance': DANCE.get(key.split('-')[0], ''),
            'origin': origin_for(entry),
            'composer': '',
            'source_url': ('https://spillefolk.dk/nodesamling/visnode.php'
                           f'?key={key}&lan=dk'),
            'contour': result['contour'],
        }
        aliases[tune_id] = titles_to_aliases(titles_for(entry))

    empty = sum(1 for s in settings.values() if not s['contour'])
    log.info(f'{len(settings)} settings; notation from MusicXML '
             f'{by_notation["musicxml"]}, from MIDI {by_notation["midi"]}; '
             f'{empty} empty contours; {len(failed)} failed: {failed[:10]}')

    today = datetime.date.today()
    payload = {
        'settings': settings,
        'aliases': aliases,
        'id': DATASET_ID,
        'label': LABEL,
        'description': DESCRIPTION,
        'v': (today - datetime.date(2020, 1, 1)).days,
        'date': today.isoformat(),
        'copyright': ('Folkets Hus Spillefolk, spillefolk.dk. Built for '
                      'personal use; do not redistribute.'),
    }
    out = data / 'spillefolk.json'
    out.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    log.info(f'Wrote {out} ({out.stat().st_size / 1e6:.1f} MB). '
             'Import it in the app via Settings → "Add a database". '
             'Never copy it to public/.')
    return 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('dir', help='Parent directory for the `data` directory')
    args = parser.parse_args()
    sys.exit(build(args.dir))
