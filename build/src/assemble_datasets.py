"""Assemble the per-dataset build products into the files served to the app.

Inputs  (data/):  thesession.json, folkwiki.json, norbeck.json
Outputs (data/):  datasets.json
                  thesession.json, folkwiki.json, norbeck.json  (passed through)
                  folkfriend-non-user-data.json                 (legacy merged)
                  nud-meta.json                                 (legacy meta)

Two things here are deliberate and easy to get wrong later.

**The legacy merged file is still published, and excludes norbeck.** Installed
PWAs that have not updated fetch `folkfriend-non-user-data.json` and know
nothing about dataset selection. They must keep working, so the file stays. But
they also cannot turn a dataset OFF, and Norbeck's collection carries
redistribution terms — pushing it to clients that have no say is the one place
the exposure would be involuntary. So the merged file remains thesession +
folkwiki, exactly as it is today.

**`nud-meta.json` is generated FROM `datasets.json`.** Two manifests describing
the same pipeline will drift the moment they are produced independently, and
the failure mode ("updates stopped happening") is silent. Deriving one from the
other makes that impossible.
"""

import argparse
import json
import logging
import os
from datetime import date

logging.basicConfig(level=logging.DEBUG,
                    format='[%(name)s:%(lineno)s] %(message)s')
log = logging.getLogger(os.path.basename(__file__))

# The manifest is a contract with the app: `id` is what userSettings stores and
# what IndexedDB keys are namespaced by, so these strings must never change
# once shipped. `filename` may.
DATASETS = (
    {'id': 'thesession', 'filename': 'thesession.json', 'source': 'thesession.json'},
    {'id': 'folkwiki',   'filename': 'folkwiki.json',   'source': 'folkwiki.json'},
    {'id': 'norbeck',    'filename': 'norbeck.json',    'source': 'norbeck.json'},
)

# Which datasets the pre-multi-dataset app expects to find in one blob.
LEGACY_MERGED_IDS = ('thesession', 'folkwiki')

LEGACY_MERGED_NAME = 'folkfriend-non-user-data.json'
LEGACY_META_NAME = 'nud-meta.json'
MANIFEST_NAME = 'datasets.json'

MANIFEST_VERSION = 1


def days_since_2020(day=None):
    """The dataset version scheme: whole days since 2020-01-01.

    Kept from the single-file era so the app's existing `remote.v > local.v`
    comparison and its Help/About date conversion are unchanged. Each dataset
    now carries its OWN v, bumped only when that dataset is rebuilt — which is
    the point: refreshing folkwiki must not force a 35 MB thesession download.

    Note two builds of the same dataset on the same day produce the same v and
    clients will not update. That has bitten this repo before; bump by hand if
    you need to force one out.
    """
    return ((day or date.today()) - date(2020, 1, 1)).days


def assemble(parent_dir, out_dir=None, version_overrides=None):
    data_dir = os.path.join(parent_dir, 'data')
    out_dir = out_dir or data_dir
    version_overrides = version_overrides or {}
    today = date.today()

    entries = []
    loaded = {}

    for spec in DATASETS:
        path = os.path.join(data_dir, spec['source'])
        if not os.path.exists(path):
            log.warning(
                f'{spec["id"]}: no {spec["source"]}, omitting from the manifest'
            )
            continue

        with open(path, encoding='utf-8') as f:
            payload = json.load(f)

        settings = payload.get('settings') or {}
        aliases = payload.get('aliases') or {}
        if not settings or not aliases:
            raise SystemExit(
                f'FATAL: {path} has {len(settings)} settings and '
                f'{len(aliases)} aliases; refusing to publish it.'
            )

        loaded[spec['id']] = payload

        out_path = os.path.join(out_dir, spec['filename'])
        if os.path.abspath(out_path) != os.path.abspath(path):
            with open(out_path, 'w', encoding='utf-8') as f:
                json.dump(payload, f, ensure_ascii=False)

        # `size` is the UNCOMPRESSED byte count and the app prefers it over
        # Content-Length for the download progress bar — Firebase gzips JSON,
        # so Content-Length is the compressed length while the streaming
        # reader counts decoded bytes, and the bar overshoots.
        entry = {
            'id': spec['id'],
            'filename': spec['filename'],
            'v': version_overrides.get(spec['id'], days_since_2020(today)),
            'date': today.strftime('%Y-%m-%d'),
            'size': os.path.getsize(out_path),
            'settings': len(settings),
            'tunes': len(aliases),
        }
        if payload.get('copyright'):
            entry['copyright'] = payload['copyright']
        entries.append(entry)
        log.info(
            f'{spec["id"]}: {entry["settings"]} settings, {entry["tunes"]} '
            f'tunes, {entry["size"] / 1e6:.1f} MB, v{entry["v"]}'
        )

    if not entries:
        raise SystemExit('FATAL: no dataset files found; nothing to assemble.')

    # An ID appearing in two datasets means one setting silently shadows
    # another once the app merges them. Disjointness is guaranteed by the ID
    # bases in each builder, so a collision here is a bug in one of them and
    # must stop the build rather than ship a corrupted index.
    check_disjoint(loaded)

    manifest = {
        'manifestVersion': MANIFEST_VERSION,
        'generated': today.strftime('%Y-%m-%d'),
        'datasets': entries,
    }
    manifest_path = os.path.join(out_dir, MANIFEST_NAME)
    with open(manifest_path, 'w', encoding='utf-8') as f:
        json.dump(manifest, f, ensure_ascii=False)
    log.info(f'Wrote {manifest_path}')

    write_legacy_bundle(loaded, entries, out_dir)
    return 0


def check_disjoint(loaded):
    seen_settings = {}
    seen_tunes = {}
    problems = []

    for ds_id, payload in loaded.items():
        for setting_id, setting in payload['settings'].items():
            owner = seen_settings.get(setting_id)
            if owner is not None and owner != ds_id:
                problems.append(
                    f'setting_id {setting_id} in both {owner} and {ds_id}')
            seen_settings[setting_id] = ds_id

            tune_id = setting.get('tune_id')
            owner = seen_tunes.get(tune_id)
            if owner is not None and owner != ds_id:
                problems.append(
                    f'tune_id {tune_id} in both {owner} and {ds_id}')
            seen_tunes[tune_id] = ds_id

        for tune_id in payload['aliases']:
            owner = seen_tunes.get(tune_id)
            if owner is not None and owner != ds_id:
                problems.append(
                    f'alias tune_id {tune_id} in both {owner} and {ds_id}')
            seen_tunes[tune_id] = ds_id

    if problems:
        for p in problems[:20]:
            log.error(p)
        raise SystemExit(
            f'FATAL: {len(problems)} ID collisions across datasets. '
            'Check the ID bases in the builders — the ranges must not overlap.'
        )
    log.info(
        f'ID check: {len(seen_settings)} setting IDs and {len(seen_tunes)} '
        'tune IDs, all disjoint across datasets'
    )


def write_legacy_bundle(loaded, entries, out_dir):
    """The single merged file + nud-meta.json, for clients predating datasets."""
    settings = {}
    aliases = {}
    included = []
    for ds_id in LEGACY_MERGED_IDS:
        payload = loaded.get(ds_id)
        if payload is None:
            log.warning(f'{ds_id} missing; legacy bundle will not contain it')
            continue
        settings.update(payload['settings'])
        aliases.update(payload['aliases'])
        included.append(ds_id)

    merged_path = os.path.join(out_dir, LEGACY_MERGED_NAME)
    with open(merged_path, 'w') as f:
        json.dump({'settings': settings, 'aliases': aliases}, f)

    # The legacy `v` must move whenever any dataset in the bundle moves, or an
    # old client never picks up new tunes. Take the newest of its members.
    by_id = {e['id']: e for e in entries}
    legacy_v = max((by_id[i]['v'] for i in included if i in by_id), default=0)
    legacy_date = max((by_id[i]['date'] for i in included if i in by_id),
                      default=date.today().strftime('%Y-%m-%d'))

    meta = {
        'v': legacy_v,
        'size': os.path.getsize(merged_path),
        'date': legacy_date,
    }
    with open(os.path.join(out_dir, LEGACY_META_NAME), 'w') as f:
        json.dump(meta, f)

    log.info(
        f'Legacy bundle ({" + ".join(included)}): {len(settings)} settings, '
        f'{meta["size"] / 1e6:.1f} MB, v{legacy_v}'
    )
    if 'norbeck' in included:
        raise SystemExit(
            'FATAL: norbeck must not be in the legacy merged bundle — clients '
            'reading it cannot opt out. See the module docstring.'
        )


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Assemble per-dataset files, datasets.json and the '
                    'legacy merged bundle')
    parser.add_argument(
        'dir', help='Parent directory for the `data` directory')
    parser.add_argument(
        '--set-version', action='append', default=[], metavar='ID=V',
        help='Force a dataset version, e.g. --set-version norbeck=2500. '
             'Use when two builds land on the same day.')
    args = parser.parse_args()
    overrides = {}
    for item in args.set_version:
        key, _, value = item.partition('=')
        overrides[key] = int(value)
    raise SystemExit(assemble(args.dir, version_overrides=overrides))
