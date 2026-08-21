"""Build the Norbeck dataset from the extracted ABC collection.

Emits `data/norbeck.json` in the same `{settings, aliases}` shape as the other
sources, so assemble_datasets.py can treat all three identically.

COPYRIGHT. This collection is (c) Henrik Norbeck and carries terms:

    - May not be used for commercial purposes.
    - The ABC files (or parts of them) may not be made available on a web page
      for download without permission from me.
    - This copyright notice must be kept.
    - Questions? E-mail: henrik@norbeck.nu

FolkFriend is free and non-commercial, Norbeck's `Z:id` is preserved on every
setting, the notice travels with the dataset (COPYRIGHT_NOTICE below is written
into norbeck.json and shown in the app), and every tune links back to
norbeck.nu. Publishing the dataset is nonetheless a redistribution and
permission has not been granted. If he objects, removing it is deleting
public/norbeck.json and its entry from datasets.json — clients then report it
unavailable and keep working. See CLAUDE.md.
"""

import argparse
import collections
import hashlib
import json
import logging
import os
import pathlib
import re
import urllib.parse

from abc_common import (
    contour_for_setting,
    decode_abc_escapes,
    parse_abc_tune,
    split_abc_tunes,
    titles_to_aliases,
)

from tqdm.contrib.concurrent import process_map

logging.basicConfig(level=logging.DEBUG,
                    format='[%(name)s:%(lineno)s] %(message)s')
log = logging.getLogger(os.path.basename(__file__))

COPYRIGHT_NOTICE = (
    'Tunes (c) Henrik Norbeck, https://www.norbeck.nu/abc/ — '
    'transcribed by Henrik Norbeck and used here with attribution. '
    'Not for commercial use.'
)

# ---------------------------------------------------------------------------
# ID ranges for norbeck data.
#
# These must clear folkwiki, which is NOT the small block its base suggests:
# `stable_folkwiki_id` reaches 1.68e9 in both namespaces (see
# build_folkwiki_data.py). Hence 3e9 / 8e9 rather than the obvious 3e6 / 4e6.
#
# Offsets are sha1(Z:id)[:8], i.e. 0..4.29e9, so:
#     tune_id    in [3.0e9,  7.3e9]
#     setting_id in [8.0e9, 12.3e9]
# Disjoint from each other, from thesession (<60k) and from folkwiki (<1.7e9),
# and far below 2**53 (JS parseInt) and u64 (the Rust sort key).
#
# Hash-derived rather than enumerated so IDs stay stable across releases as
# tunes are inserted; user favourites reference them. `Z:id` is Norbeck's own
# per-tune identifier, is stable across releases, and he requires it be kept —
# so it is the natural primary key.
# ---------------------------------------------------------------------------
NORBECK_TUNE_ID_BASE = 3_000_000_000
NORBECK_SETTING_ID_BASE = 8_000_000_000

# Z:id rhythm token -> the `rhythm` value display.asp expects.
#
# They agree for most families but not all, and the differences are not
# derivable: the Z:id uses a squashed token ('slipjig', 'sp', 'jp') while the
# site uses the human label with spaces ('slip jig', 'slängpolska', 'polska J').
# This table was read off the site's own category navigation
# (index2.asp?cat=i|s|m) and is verified at build time against the scraped
# (rhythm, ref) index — see discover_norbeck_refs.py. An entry that stops
# resolving shows up as a drop in deep-link coverage, not as a broken link.
ZID_TO_SITE_RHYTHM = {
    # Irish and Scottish
    'air': 'air', 'barndance': 'barndance', 'carolan': 'carolan',
    'countrydance': 'country dance', 'hf': 'highland', 'hornpipe': 'hornpipe',
    'jig': 'jig', 'march': 'march', 'mazurka': 'mazurka', 'polka': 'polka',
    'reel': 'reel', 'setdance': 'set dance', 'slide': 'slide',
    'slipjig': 'slip jig',
    'hp': 'slip jig',            # hop jig is listed as a slip-jig variant
    'slowair': 'slow air', 'song': 'song', 'strathspey': 'strathspey',
    'waltz': 'waltz',
    # Swedish and Scandinavian
    'ganglat': 'gånglåt', 'halling': 'halling', 'jp': 'polska J',
    'k1': 'polska K1', 'L1': 'polska L1', 'op': 'polska O', 'sang': 'sång',
    'schottis': 'schottis', 'sp': 'slängpolska', 'vals': 'vals',
    # Everything else
    'andro': 'an dro', 'bourree': 'bourree', 'buchimish': 'buchimish',
    'cadaneasca': 'cadaneasca', 'frailach': 'frailach', 'gavotte': 'gavotte',
    'geampara': 'geampara', 'hanterdro': 'hanter dro', 'hora': 'hora',
    'kolo': 'kolo', 'kopanitsa': 'kopanitsa', 'misc': 'misc',
    'miscbalkan': 'misc', 'muineira': 'muineira',
    'paidushkohoro': 'paidushko horo', 'rachenitsa': 'rachenitsa',
    'ridee': 'ridee', 'rond': 'rond', 'sandanskohoro': 'sandansko horo',
    'smesenohoro': 'smeseno horo', 'musette': 'valse musette',
    'waynu': 'waynu',
}

# Which site category each rhythm belongs to, for the index-page fallback URL.
SITE_CATEGORY = {}
for _r in ('air', 'barndance', 'carolan', 'country dance', 'highland',
           'hornpipe', 'jig', 'march', 'mazurka', 'polka', 'reel', 'set dance',
           'slide', 'slip jig', 'slow air', 'song', 'strathspey', 'waltz'):
    SITE_CATEGORY[_r] = 'i'
for _r in ('gånglåt', 'halling', 'polska J', 'polska K1', 'polska L1',
           'polska O', 'sång', 'schottis', 'slängpolska', 'vals'):
    SITE_CATEGORY[_r] = 's'
for _r in ('an dro', 'bourree', 'buchimish', 'cadaneasca', 'frailach',
           'gavotte', 'geampara', 'hanter dro', 'hora', 'kolo', 'kopanitsa',
           'misc', 'muineira', 'paidushko horo', 'rachenitsa', 'ridee', 'rond',
           'sandansko horo', 'smeseno horo', 'valse musette', 'waynu'):
    SITE_CATEGORY[_r] = 'm'

SITE_BASE = 'https://www.norbeck.nu/abc/'

# Z:id values in the collection are sometimes unsubstituted templates
# ('hn-%R-%X'), which are not identifiers at all.
_ZID_RE = re.compile(r'^hn-(.+)-(\d+)$')

# A P: line in the BODY starts an alternative rendering of the whole tune —
# 'variations', 'Version 2', song verses. 1,062 of them across the collection.
#
# THESE ARE COMPLETE SETTINGS, NOT FRAGMENTS. Measured against the head of the
# same tune, the median section is 1.01x its length; only 2 of 836 'variations'
# sections are under a quarter. So Norbeck's variations are the same thing
# thesession calls a setting: another way the tune is played, end to end.
#
# They are therefore split into separate settings sharing one tune_id, exactly
# as thesession does. The previous behaviour — compute the contour from the head
# and discard the rest — left 925 tunes with material the app could display but
# never match, and left the 12 tunes whose body STARTS with a P: (songs, where
# every verse is a part) with an empty contour and no way to find them at all.
_BODY_PART_RE = re.compile(r'(?m)^P:(.*)$')

# Sections per tune are packed into the setting id, so they sort after their
# head and stay adjacent. The real maximum is 5.
SECTION_MULTIPLIER = 100


def parse_zid(zid):
    """'hn-reel-1' -> ('reel', '1').

    None when absent or an unsubstituted template ('hn-%R-%X').
    """
    if not zid:
        return None
    m = _ZID_RE.match(zid.strip())
    if not m:
        return None
    rhythm, number = m.group(1), m.group(2)
    if '%' in rhythm:            # unsubstituted 'hn-%R-%X'
        return None
    return rhythm, number


def content_key(parsed):
    """A short hash of what makes this tune this tune.

    Title, meter, mode and the note body — everything that would have to change
    for it to be a different tune. Deliberately NOT the file it came from or its
    position in that file, both of which move between releases.
    """
    material = '|'.join([
        (parsed['titles'][0] if parsed['titles'] else ''),
        parsed['meter'] or '',
        parsed['mode'] or '',
        parsed['abc_body'] or '',
    ])
    return hashlib.sha1(material.encode('utf-8')).hexdigest()[:12]


def derive_key(parsed, zid_parts, seen_keys, duplicate_zids=(), stats=None):
    """The source-unique key a tune's IDs are hashed from.

    IDs must be stable across RELEASES, not merely across rebuilds of the same
    release: a user's favourites reference them, and Norbeck publishes a new zip
    every few months. Both fallbacks here are therefore derived from tune
    CONTENT, never from position.

    They used to be `<filename>#<block index>` and a `#2` suffix by encounter
    order. Both are stable only while nothing moves — insert one tune near the
    top of hnr0.abc, or swap two tunes that share a Z:id, and every later ID
    shifts onto a different tune. A favourite would silently start pointing at
    the wrong one.

    `duplicate_zids` is the set of Z:ids that occur MORE THAN ONCE anywhere in
    the collection, computed in a pre-pass. It has to be known up front: if only
    the second occurrence were suffixed, the first would keep the bare key by
    virtue of being encountered first, and the two tunes would swap IDs the
    moment they swapped places. Both are suffixed, so neither depends on order.

    `seen_keys` is a Counter carried across the whole build; this function
    updates it.
    """
    def bump(name):
        if stats is not None:
            stats[name] += 1

    if zid_parts:
        key = f'hn-{zid_parts[0]}-{zid_parts[1]}'
        if key in duplicate_zids:
            # Eight Z:ids are duplicated in the collection.
            bump('duplicate_zid')
            key = f'{key}#{content_key(parsed)}'
    else:
        # ~28 blocks carry an unsubstituted template or no Z: at all.
        key = f'hn-content-{content_key(parsed)}'
        bump('no_usable_zid')

    # Byte-identical blocks collide even on content. They are the same tune, so
    # which one wins does not matter — but IDs must still be unique, and this is
    # the one place encounter order can legitimately decide.
    if key in seen_keys:
        bump('identical_duplicate')
        key = f'{key}#{seen_keys[key] + 1}'
    seen_keys[key] += 1
    return key


def find_duplicate_zids(zid_keys):
    """Z:id keys occurring more than once. See derive_key."""
    counts = collections.Counter(k for k in zid_keys if k)
    return {k for k, n in counts.items() if n > 1}


def stable_norbeck_id(key, base, section=None):
    """Derive a stable numeric ID from a source-unique key.

    `key` is the Z:id where there is one, else a content hash.

    `section` packs a setting's position within its tune into the id, so a
    tune's settings sort together and the head (section 0) comes first — the
    Rust side orders settings by numeric id, so without this a variation could
    be listed above the tune it varies.
    """
    offset = int(hashlib.sha1(key.encode('utf-8')).hexdigest()[:8], 16)
    if section is None:
        return str(base + offset)
    if section >= SECTION_MULTIPLIER:
        raise ValueError(
            f'{key} has more than {SECTION_MULTIPLIER} sections')
    return str(base + offset * SECTION_MULTIPLIER + section)


def source_url_for(zid_parts, valid_refs):
    """Per-tune deep link, falling back to the rhythm's index page.

    `valid_refs` is the scraped set of (site_rhythm, ref) pairs that actually
    exist on the site; when the pair is absent the deep link would 500, so we
    return the listing page for that rhythm instead. Never returns ''. The app
    cannot derive a Norbeck URL from a tune ID, so every setting must carry one.
    """
    site_rhythm = None
    if zid_parts:
        site_rhythm = ZID_TO_SITE_RHYTHM.get(zid_parts[0])

    if site_rhythm is None:
        return SITE_BASE

    if valid_refs is None or (site_rhythm, zid_parts[1]) in valid_refs:
        return SITE_BASE + 'display.asp?' + urllib.parse.urlencode(
            {'rhythm': site_rhythm, 'ref': zid_parts[1]})

    cat = SITE_CATEGORY.get(site_rhythm)
    query = {'rhythm': site_rhythm}
    if cat:
        query = {'cat': cat, 'rhythm': site_rhythm}
    return SITE_BASE + 'index2.asp?' + urllib.parse.urlencode(query)


def split_body_sections(abc_body):
    """Split a body into settings: [(label, text), ...], the head first.

    `label` is None for the head and the P: text for each section. Sections
    with no notes are dropped — a body that STARTS with a P: has an empty head,
    which is not a setting.
    """
    marks = list(_BODY_PART_RE.finditer(abc_body))
    if not marks:
        return [(None, abc_body.strip())] if has_notes(abc_body) else []

    out = [(None, abc_body[:marks[0].start()])]
    for i, mark in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(abc_body)
        out.append((mark.group(1).strip(), abc_body[mark.end():end]))

    return [(label, text.strip()) for label, text in out if has_notes(text)]


_NOTE_RE = re.compile(r'[A-Ga-g]')


def has_notes(text):
    return bool(_NOTE_RE.search(text or ''))


def generate_midi_contour(args):
    """process_map worker. Module-level so it is picklable."""
    setting, midis_path = args
    # `hn_` prefix keeps this cache disjoint from thesession's and folkwiki's.
    midi_out_path = os.path.join(
        midis_path, f'hn_{setting["setting_id"]}.midi')
    contour = contour_for_setting(
        abc_body=setting['contour_body'],
        meter=setting['meter'],
        mode=setting['mode'],
        note_len=setting['note_len'],
        midi_out_path=midi_out_path,
    )
    return setting['setting_id'], contour


def build_norbeck_data(parent_dir):
    norbeck_dir = os.path.join(parent_dir, 'data', 'norbeck')
    abc_dir = os.path.join(norbeck_dir, 'abc')
    manifest_path = os.path.join(norbeck_dir, 'manifest.json')
    refs_path = os.path.join(norbeck_dir, 'site_refs.json')
    midis_dir = os.path.join(norbeck_dir, 'midis')
    output_path = os.path.join(parent_dir, 'data', 'norbeck.json')

    pathlib.Path(midis_dir).mkdir(parents=True, exist_ok=True)

    if not os.path.exists(manifest_path):
        log.error(f'No manifest at {manifest_path}. '
                  'Run download_norbeck_data.py first.')
        return 1

    with open(manifest_path, encoding='utf-8') as f:
        manifest = json.load(f)
    log.info(f'Building from release {manifest["release"]} '
             f'({len(manifest["files"])} ABC files)')

    valid_refs = None
    if os.path.exists(refs_path):
        with open(refs_path, encoding='utf-8') as f:
            valid_refs = {(r, str(n)) for r, ns in json.load(f).items()
                          for n in ns}
        log.info(f'Loaded {len(valid_refs)} verified site (rhythm, ref) pairs')
    else:
        log.warning(
            f'No {refs_path}; emitting deep links unverified. '
            'Run discover_norbeck_refs.py to check them.')

    raw_settings = []
    raw_aliases = {}
    seen_keys = collections.Counter()
    stats = collections.Counter()

    # PARSE PASS. Every block is parsed before any ID is assigned, because
    # derive_key needs to know which Z:ids are duplicated ANYWHERE in the
    # collection — see its docstring. Parsing is cheap; the expensive step is
    # abc2midi, which happens once, later.
    parsed_blocks = []

    for rel in manifest['files']:
        abc_path = os.path.join(abc_dir, rel)
        if not os.path.exists(abc_path):
            log.warning(f'Missing file {rel}, skipping')
            continue

        # The collection is ASCII/UTF-8 throughout, but decode explicitly and
        # tolerantly rather than trusting the locale — folkwiki's mojibake came
        # from exactly that assumption going unstated.
        with open(abc_path, 'rb') as f:
            data = f.read()
        try:
            abc_text = data.decode('utf-8')
        except UnicodeDecodeError:
            log.warning(f'{rel} is not UTF-8; falling back to cp1252')
            abc_text = data.decode('cp1252', errors='replace')
            stats['non_utf8_files'] += 1

        for block_index, block in enumerate(split_abc_tunes(abc_text)):
            # Each file opens with a preamble (the copyright notice and a
            # description) before the first X:. It becomes block 0 and has no
            # T:/M:/K:, so parse_abc_tune rejects it.
            parsed = parse_abc_tune(block, extra_fields=('Z', 'S'))
            if parsed is None:
                stats['unparseable_blocks'] += 1
                continue

            zid_raw = (parsed['extra']['Z'] or [''])[0]
            # Z: is 'id:hn-reel-1'; parse_abc_tune strips the field letter.
            if zid_raw.lower().startswith('id:'):
                zid_raw = zid_raw[3:]
            zid_parts = parse_zid(zid_raw)
            parsed_blocks.append((parsed, zid_parts))

    duplicate_zids = find_duplicate_zids(
        f'hn-{z[0]}-{z[1]}' if z else None for _, z in parsed_blocks)
    log.info(f'Parsed {len(parsed_blocks)} tune blocks; '
             f'{len(duplicate_zids)} Z:ids are duplicated')

    # ID PASS.
    for parsed, zid_parts in parsed_blocks:
        key = derive_key(parsed, zid_parts, seen_keys,
                         duplicate_zids, stats)

        tune_id = stable_norbeck_id(key, NORBECK_TUNE_ID_BASE)

        origin = parsed['origin'] or (parsed['extra']['S'] or [''])[0]

        # The collection is 7-bit ASCII and spells every accented letter as
        # an ABC escape (`sl\"angpolska`). Decode header-derived text or
        # every Swedish and Irish title is stored as backslash noise and
        # cannot be found by searching for the name a user would type.
        note_len = parsed['note_len'] if parsed['has_note_len'] else None

        # One setting per section, all sharing this tune_id — the same shape
        # thesession has, where a tune carries several settings.
        sections = split_body_sections(parsed['abc_body'])
        if not sections:
            stats['no_notes'] += 1
            continue
        if len(sections) > 1:
            stats['tunes_with_variations'] += 1
            stats['extra_settings'] += len(sections) - 1

        for index, (label, section_body) in enumerate(sections):
            # The label is kept in the STORED abc so ABCJS renders it above
            # the score ("Variations"), and stripped from what abc2midi sees,
            # where a P: would trigger part expansion.
            stored = f'P:{label}\n{section_body}' if label else section_body
            raw_settings.append({
                'setting_id': stable_norbeck_id(
                    key, NORBECK_SETTING_ID_BASE, index),
                'tune_id': tune_id,
                'meter': parsed['meter'],
                'mode': parsed['mode'],
                # None when the source has no L:, so abc2midi applies the ABC
                # standard default. 45% of the collection relies on this.
                'note_len': note_len,
                'abc_body': stored,
                'contour_body': section_body,
                'dance': decode_abc_escapes(parsed['dance']),
                'origin': decode_abc_escapes(origin),
                'composer': decode_abc_escapes(parsed['composer']),
                'source_url': source_url_for(zid_parts, valid_refs),
                # Norbeck's own per-tune identifier. His terms require the
                # Z:id line be kept when a tune is passed on; the stored `abc`
                # is body-only by schema, so it is carried as a field instead.
                'zid': key,
            })

        raw_aliases[tune_id] = titles_to_aliases(
            [decode_abc_escapes(t) for t in parsed['titles']])

    log.info(f'Parsed {len(raw_settings)} settings across {len(raw_aliases)} '
             'tunes from Norbeck ABC files')
    for k, v in sorted(stats.items()):
        log.info(f'  {k}: {v}')

    # IDs must be globally unique or one record silently shadows another in the
    # merged index. Collision chance is ~1e-3 across 3.5k tunes, so this is a
    # real if unlikely event and must fail the build rather than warn.
    #
    # Setting ids must be unique outright. Tune ids are SHARED by the settings
    # of one tune — that is the point of splitting — so what matters there is
    # that two different source keys never landed on the same tune id.
    dupes = [i for i, n in collections.Counter(
        s['setting_id'] for s in raw_settings).items() if n > 1]
    if dupes:
        raise SystemExit(
            f'FATAL: {len(dupes)} duplicate norbeck setting_ids '
            f'(e.g. {dupes[:5]}). Two source keys hashed to the same offset; '
            'widen the hash slice in stable_norbeck_id.')

    key_by_tune = {}
    for setting in raw_settings:
        previous = key_by_tune.setdefault(setting['tune_id'], setting['zid'])
        if previous != setting['zid']:
            raise SystemExit(
                f'FATAL: tune_id {setting["tune_id"]} is claimed by two '
                f'different tunes ({previous!r} and {setting["zid"]!r}). '
                'Widen the hash slice in stable_norbeck_id.')

    contours = process_map(
        generate_midi_contour,
        [(s, midis_dir) for s in raw_settings],
        desc='Generating norbeck MIDI contours',
        chunksize=8,
    )

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
            'zid':        s['zid'],
        }

    empty_contours = 0
    for setting_id, contour in contours:
        settings[setting_id]['contour'] = contour
        if not contour:
            empty_contours += 1

    total = len(contours)
    log.info(
        f'norbeck contours: {total - empty_contours}/{total} non-empty '
        f'({empty_contours} failed, {100 * empty_contours / total:.1f}%)'
    )

    deep = sum(1 for s in raw_settings if 'display.asp' in s['source_url'])
    log.info(f'source_url: {deep}/{total} per-tune deep links '
             f'({100 * deep / total:.1f}%), remainder are index pages')

    output = {
        'settings': settings,
        'aliases': raw_aliases,
        'copyright': COPYRIGHT_NOTICE,
        'release': manifest['release'],
    }

    log.info(f'Writing {output_path}')
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False)

    log.info(f'Done. {len(settings)} norbeck settings written.')
    return 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description="Build the Norbeck dataset from downloaded ABC files")
    parser.add_argument(
        'dir', help='Parent directory for the `data` directory')
    args = parser.parse_args()
    raise SystemExit(build_norbeck_data(args.dir))
