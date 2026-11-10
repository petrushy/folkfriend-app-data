"""Which tunes are commonly played together — "set partners" for thesession.

thesession.org members save the SETS they play (two to four tunes, one after
the other), and list the tracks of commercial recordings, many of which are
sets too. Both are in the TheSession-data export (`sets.json`,
`recordings.json`). From them this builds, for each tune, the tunes it is most
often played with, which the app shows as "Played in sets with…" — the same
list thesession.org shows on a tune's page ("This tune gets played in sets
together with…"). Measured against that page for Farewell To Whalley Range
(tune 2410), the top four agree in order. The site's fifth, Elizabeth
Kelly's Delight, comes seventh here: the site also counts saved lists longer
than a real set, which this leaves out (see MAX_SET_SIZE).

Output, embedded as a top-level key of thesession.json:

    "sets": { "<tune_id>": [[partner_id, in_sets, on_recordings,
                             followed_by, preceded_by], ...] }

- `in_sets`      — member sets containing both tunes (what thesession.org counts)
- `on_recordings`— recording tracks containing both tunes
- `followed_by`  — how often the partner came DIRECTLY AFTER this tune, in a
                   set or on a track. It is what "what do we play next" wants.
- `preceded_by`  — how often it came directly BEFORE this tune.

Both directions are carried on the PAIR, counted before the list is cut to
MAX_PARTNERS. They used to be one number here with the reverse read from the
partner's own entry, but each tune keeps only its own top 12, so that entry is
often not there — and an absent entry read as zero claimed a direction the
data does not support (The Cock and the Hen -> The Kid on the Mountain showed
"usually played after" on 6 transitions each way).

Adjacency is only claimed between positions that are unambiguous. A set whose
`settingorder` ties (two tunes both at position 2 — 1,618 sets in the
2026-11-10 export) or is missing still counts for co-occurrence, but no
transition is inferred through the tie: sorting would otherwise make tune id
the ordering rule and publish transitions nobody played.

Partner ids are strings like every other tune id in the file. Ranked by
`in_sets + on_recordings`, ties broken by id so a rebuild is deterministic,
capped at MAX_PARTNERS. Only tunes that are in the published index appear on
either side — a partner the app cannot open is no use as a suggestion.

`sets.json` is stored in Git LFS (~150 MB): the raw.githubusercontent.com URL
answers with a 134-byte LFS pointer, so the download goes to
media.githubusercontent.com. See download_thesession_data.py.

Also usable on its own, to add partners to an already-built thesession.json
without regenerating every contour:

    python src/thesession_sets.py . --into data/thesession.json
"""
import argparse
import collections
import json
import logging
import os

log = logging.getLogger(os.path.basename(__file__))

MAX_PARTNERS = 12

# A set as played is normally three to six tunes, never more than eight. A
# saved "set" longer than that is a "my favourite reels" list (some run to 130
# tunes), and counting every pair in a 40-tune list adds 780 pairs, mostly of
# tunes nobody plays together. Such lists are left out entirely.
MAX_SET_SIZE = 8


def _slots(entries):
    """Ordered (position, tune) pairs -> a list of SLOTS, one per position.

    A slot holding more than one tune is a tie: those tunes are in the group,
    but which came first is unknown. A position of None (missing or not a
    number) makes the whole group's order unknown: the lot goes into one
    slot, which counts for co-occurrence and claims no adjacency at all.
    """
    if any(pos is None for pos, _ in entries):
        return [[t for _, t in entries]]
    by_pos = collections.defaultdict(list)
    for pos, tune in entries:
        by_pos[pos].append(tune)
    return [by_pos[pos] for pos in sorted(by_pos)]


def _position(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _groups_from_sets(rows):
    """Member sets as lists of position slots (see _slots)."""
    sets = collections.defaultdict(list)
    for r in rows:
        tune = str(r.get('tune_id') or '').strip()
        key = r.get('tuneset')
        if not tune or key in (None, ''):
            continue
        sets[key].append((_position(r.get('settingorder')), tune))
    return [_slots(v) for _, v in sorted(sets.items(), key=lambda kv: str(kv[0]))]


def _groups_from_recordings(rows):
    """Recording tracks as lists of position slots.

    A track whose tunes are not all linked to thesession tunes still counts
    for the linked ones, but adjacency is only claimed between tunes that were
    adjacent on the track: an unlinked tune between two linked ones means they
    were not played one after the other. Unlinked tunes are kept as '' so
    adjacency sees the gap and the size limit counts them.
    """
    tracks = collections.defaultdict(list)
    for r in rows:
        tracks[(r.get('id'), r.get('track'))].append(
            (_position(r.get('number')), str(r.get('tune_id') or '').strip()))
    return [_slots(v) for _, v in sorted(tracks.items(), key=lambda kv: str(kv[0]))]


def _count(groups, together, follows):
    for slots in groups:
        size = sum(len(slot) for slot in slots)
        members = {t for slot in slots for t in slot if t}
        if len(members) < 2 or size > MAX_SET_SIZE:
            continue
        for a in members:
            for b in members:
                if a != b:
                    together[(a, b)] += 1
        for here, nxt in zip(slots, slots[1:]):
            if len(here) != 1 or len(nxt) != 1:
                continue        # a tie on either side: order unknown
            a, b = here[0], nxt[0]
            if a and b and a != b:
                follows[(a, b)] += 1


def build_set_partners(set_rows, recording_rows, known_tunes,
                       max_partners=MAX_PARTNERS):
    known = {str(t) for t in known_tunes}
    in_sets = collections.Counter()
    on_recordings = collections.Counter()
    follows = collections.Counter()
    _count(_groups_from_sets(set_rows), in_sets, follows)
    _count(_groups_from_recordings(recording_rows), on_recordings, follows)

    by_tune = collections.defaultdict(dict)
    for (a, b) in set(in_sets) | set(on_recordings):
        if a in known and b in known:
            by_tune[a][b] = [in_sets[(a, b)], on_recordings[(a, b)],
                             follows[(a, b)], follows[(b, a)]]

    # Emitted in tune-id order: the pair set above iterates in hash order, and
    # json.dump writes keys in insertion order, so without this two rebuilds of
    # the same inputs produce different bytes (and different file hashes).
    out = {}
    for tune in sorted(by_tune, key=int):
        partners = by_tune[tune]
        ranked = sorted(partners.items(),
                        key=lambda kv: (-(kv[1][0] + kv[1][1]), int(kv[0])))
        out[tune] = [[p, *counts] for p, counts in ranked[:max_partners]]
    return out


def load_rows(path):
    with open(path, encoding='utf-8') as f:
        head = f.read(64)
        if head.startswith('version https://git-lfs'):
            raise SystemExit(
                f'FATAL: {path} is a Git LFS pointer, not the data. '
                'download_thesession_data.py fetches it from '
                'media.githubusercontent.com; re-run it.')
        f.seek(0)
        return json.load(f)


def attach_set_partners(payload, data_dir):
    """Add `sets` to a thesession payload in place. Returns the partner map.

    Missing inputs are not fatal: a thesession.json without partners is still
    a complete tune index, and the app simply offers no suggestions.
    validate_output.py reports the absence.
    """
    sets_path = os.path.join(data_dir, 'sets.json')
    recordings_path = os.path.join(data_dir, 'recordings.json')
    if not os.path.exists(sets_path):
        log.warning(f'No {sets_path}; thesession.json will carry no set partners')
        payload.pop('sets', None)
        return {}
    set_rows = load_rows(sets_path)
    recording_rows = (load_rows(recordings_path)
                      if os.path.exists(recordings_path) else [])
    partners = build_set_partners(set_rows, recording_rows,
                                  payload.get('aliases', {}).keys())
    payload['sets'] = partners
    log.info(f'Set partners for {len(partners)} tunes '
             f'(from {len(set_rows)} set rows, {len(recording_rows)} '
             'recording rows)')
    return partners


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO,
                        format='[%(name)s:%(lineno)s] %(message)s')
    parser = argparse.ArgumentParser()
    parser.add_argument('dir', help='Parent directory for the `data` directory')
    parser.add_argument('--into', required=True,
                        help='thesession.json to add the partners to, in place')
    args = parser.parse_args()
    with open(args.into, encoding='utf-8') as f:
        payload = json.load(f)
    attach_set_partners(payload, os.path.join(args.dir, 'data'))
    with open(args.into, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False)
