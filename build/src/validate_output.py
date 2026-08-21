"""Sanity-check the built dataset files before deployment.

Exits with status 1 if any check fails, which causes build.sh (set -e) to
abort before anything is moved to public/ and deployed.

Restructured for the multi-dataset layout. Previously everything was checked
against the single merged file and "which source is this?" was decided by
numeric ID range (`tune_id >= 1_000_000` meant folkwiki). That could not
survive a third source — the folkwiki range runs to ~1.68e9, so any new base
either collides or makes the range test a growing chain of magic numbers.
Each dataset is now validated as its own file, and the cross-file check that
matters (no ID appears in two datasets) is done explicitly.
"""

import argparse
import glob
import json
import logging
import os
import sys

logging.basicConfig(
    level=logging.INFO, format='[%(name)s:%(lineno)s] %(message)s'
)
log = logging.getLogger(os.path.basename(__file__))

# ---- Per-dataset thresholds -------------------------------------------
# Conservative lower bounds. Real numbers as of August 2026:
#   thesession 54,293 settings / 23,017 tunes
#   folkwiki    8,484 settings /  8,484 tunes
#   norbeck     3,472 settings /  3,472 tunes
#
# `source_url_pct` is a floor on the share of settings carrying a source_url.
# thesession needs none — the app derives thesession.org URLs from the tune ID.
# folkwiki and norbeck cannot be derived and must carry one.
DATASETS = {
    'thesession': {
        'min_settings': 50_000, 'min_aliases': 20_000,
        'max_empty_contour_pct': 5.0, 'source_url_pct': 0.0,
    },
    'folkwiki': {
        'min_settings': 5_000, 'min_aliases': 5_000,
        'max_empty_contour_pct': 5.0, 'source_url_pct': 95.0,
    },
    'norbeck': {
        'min_settings': 3_000, 'min_aliases': 3_000,
        'max_empty_contour_pct': 5.0, 'source_url_pct': 99.0,
    },
}

# Datasets that are built but must never be served. See assemble_datasets.py.
UNPUBLISHED = ('norbeck',)

# The legacy merged bundle still shipped for clients predating dataset
# selection. It is thesession + folkwiki and must never contain norbeck.
LEGACY_MERGED_IDS = ('thesession', 'folkwiki')
MIN_LEGACY_SETTINGS = 55_000
MIN_LEGACY_ALIASES = 25_000

MIN_FOLKWIKI_PAGEID_COVERAGE_PCT = 65.0

REQUIRED_FIELDS = {
    'tune_id', 'meter', 'mode', 'abc', 'dance', 'contour', 'origin'
}
# -----------------------------------------------------------------------


def validate_dataset(ds_id, path, errors):
    """Check one dataset file. Returns its parsed payload, or None."""
    limits = DATASETS[ds_id]
    log.info(f'--- {ds_id} ({path})')

    with open(path, encoding='utf-8') as f:
        payload = json.load(f)

    settings = payload.get('settings') or {}
    aliases = payload.get('aliases') or {}

    n_settings, n_aliases = len(settings), len(aliases)
    log.info(f'  settings: {n_settings}   aliases: {n_aliases}')
    if n_settings < limits['min_settings']:
        errors.append(
            f'{ds_id}: too few settings: {n_settings} < '
            f'{limits["min_settings"]}.'
        )
    if n_aliases < limits['min_aliases']:
        errors.append(
            f'{ds_id}: too few alias entries: {n_aliases} < '
            f'{limits["min_aliases"]}.'
        )

    empty = sum(1 for s in settings.values() if not s.get('contour'))
    pct = 100 * empty / n_settings if n_settings else 100
    log.info(f'  empty contours: {empty}/{n_settings} ({pct:.2f}%)')
    if pct > limits['max_empty_contour_pct']:
        errors.append(
            f'{ds_id}: too many empty contours: {empty}/{n_settings} '
            f'({pct:.1f}%) > {limits["max_empty_contour_pct"]}%.'
        )

    if limits['source_url_pct'] > 0 and n_settings:
        with_url = sum(1 for s in settings.values() if s.get('source_url'))
        url_pct = 100 * with_url / n_settings
        log.info(
            f'  source_url coverage: {with_url}/{n_settings} ({url_pct:.2f}%)')
        if url_pct < limits['source_url_pct']:
            errors.append(
                f'{ds_id}: source_url coverage too low: {url_pct:.1f}% < '
                f'{limits["source_url_pct"]}%. The app cannot derive a URL '
                'for this source, so a missing one is a dead link.'
            )

    missing_fields = 0
    for sid, s in settings.items():
        missing = REQUIRED_FIELDS - set(s.keys())
        if missing:
            missing_fields += 1
            if missing_fields <= 3:
                log.warning(f'  setting {sid} missing fields: {missing}')
    if missing_fields:
        errors.append(
            f'{ds_id}: {missing_fields} settings are missing required fields.')

    # Every setting's tune must have a name, or it is unreachable by search.
    orphans = sum(1 for s in settings.values()
                  if s.get('tune_id') not in aliases)
    if orphans:
        errors.append(
            f'{ds_id}: {orphans} settings reference a tune_id with no aliases.')

    return payload


def check_disjoint_ids(loaded, errors):
    """No setting or tune ID may appear in two datasets.

    A collision does not raise anywhere downstream: the app merges the
    datasets into one map and one setting silently shadows the other, so the
    tune just stops being findable. It has to be caught here.
    """
    setting_owner = {}
    tune_owner = {}
    collisions = []

    for ds_id, payload in loaded.items():
        for setting_id, setting in payload['settings'].items():
            prev = setting_owner.get(setting_id)
            if prev and prev != ds_id:
                collisions.append(
                    f'setting_id {setting_id}: {prev} vs {ds_id}')
            setting_owner[setting_id] = ds_id

            tune_id = setting.get('tune_id')
            prev = tune_owner.get(tune_id)
            if prev and prev != ds_id:
                collisions.append(f'tune_id {tune_id}: {prev} vs {ds_id}')
            tune_owner[tune_id] = ds_id

        for tune_id in payload['aliases']:
            prev = tune_owner.get(tune_id)
            if prev and prev != ds_id:
                collisions.append(f'tune_id {tune_id}: {prev} vs {ds_id}')
            tune_owner[tune_id] = ds_id

    log.info(
        f'--- cross-dataset: {len(setting_owner)} setting IDs, '
        f'{len(tune_owner)} tune IDs'
    )
    if collisions:
        for c in collisions[:10]:
            log.error(f'  {c}')
        errors.append(
            f'{len(collisions)} ID collisions across datasets. The ID bases in '
            'the builders must not overlap.'
        )
    else:
        log.info('  all disjoint')


def validate_manifest(path, loaded, errors):
    log.info(f'--- datasets.json ({path})')
    with open(path, encoding='utf-8') as f:
        manifest = json.load(f)

    if 'manifestVersion' not in manifest:
        errors.append('datasets.json is missing "manifestVersion".')
    entries = manifest.get('datasets')
    if not isinstance(entries, list) or not entries:
        errors.append('datasets.json has no "datasets" array.')
        return

    for entry in entries:
        for field in ('id', 'filename', 'v', 'date', 'size', 'settings'):
            if field not in entry:
                errors.append(
                    f'datasets.json entry {entry.get("id")!r} missing '
                    f'"{field}".')
        ds_id = entry.get('id')
        if ds_id in UNPUBLISHED:
            errors.append(
                f'{ds_id} is listed in datasets.json but must not be '
                'published. See assemble_datasets.py.')
        payload = loaded.get(ds_id)
        if payload is not None and entry.get('settings') != len(
                payload['settings']):
            errors.append(
                f'datasets.json says {ds_id} has {entry.get("settings")} '
                f'settings but the file has {len(payload["settings"])}.'
            )
        # The app uses `size` for the download progress bar rather than
        # Content-Length (which is the gzipped length), so a wrong value makes
        # the bar visibly wrong.
        directory = os.path.dirname(path)
        file_path = os.path.join(directory, entry.get('filename', ''))
        if os.path.exists(file_path):
            actual = os.path.getsize(file_path)
            if entry.get('size') != actual:
                errors.append(
                    f'datasets.json says {ds_id} is {entry.get("size")} bytes '
                    f'but {file_path} is {actual}.'
                )
        log.info(f'  {ds_id}: v{entry.get("v")} '
                 f'{entry.get("size", 0) / 1e6:.1f} MB '
                 f'{entry.get("settings")} settings')


def validate_legacy_bundle(data_dir, loaded, errors):
    merged_path = os.path.join(data_dir, 'folkfriend-non-user-data.json')
    meta_path = os.path.join(data_dir, 'nud-meta.json')
    log.info(f'--- legacy bundle ({merged_path})')

    if not os.path.exists(merged_path):
        errors.append(
            'The legacy merged bundle is missing. Installed apps that predate '
            'dataset selection fetch it and will break without it.'
        )
        return

    with open(merged_path) as f:
        merged = json.load(f)
    settings = merged.get('settings', {})
    log.info(f'  settings: {len(settings)}  aliases: {len(merged.get("aliases", {}))}')

    if len(settings) < MIN_LEGACY_SETTINGS:
        errors.append(
            f'Legacy bundle has too few settings: {len(settings)} < '
            f'{MIN_LEGACY_SETTINGS}.')
    if len(merged.get('aliases', {})) < MIN_LEGACY_ALIASES:
        errors.append('Legacy bundle has too few alias entries.')

    # norbeck must NOT be here. Clients reading this file have no way to turn a
    # dataset off, and the collection carries redistribution terms.
    norbeck = loaded.get('norbeck')
    if norbeck:
        leaked = set(norbeck['settings']) & set(settings)
        if leaked:
            errors.append(
                f'{len(leaked)} norbeck settings leaked into the legacy '
                'bundle. Clients reading it cannot opt out; see '
                'assemble_datasets.py.'
            )
        else:
            log.info('  norbeck correctly absent')

    if os.path.exists(meta_path):
        with open(meta_path) as f:
            meta = json.load(f)
        for field in ('v', 'date', 'size'):
            if field not in meta:
                errors.append(f'nud-meta.json is missing "{field}".')
        if meta.get('size') != os.path.getsize(merged_path):
            errors.append(
                f'nud-meta.json says {meta.get("size")} bytes but the bundle '
                f'is {os.path.getsize(merged_path)}.')
        log.info(f'  nud-meta: {meta}')
    else:
        errors.append(f'nud-meta.json not found at {meta_path}')


def validate_folkwiki_cache(folkwiki_dir, manifest_path, pageid_path, errors):
    if not manifest_path:
        return
    if not os.path.exists(manifest_path):
        errors.append(f'Folkwiki manifest not found at {manifest_path}')
        return

    log.info('--- folkwiki cache')
    with open(manifest_path, encoding='utf-8') as f:
        manifest = json.load(f)
    abc_files = glob.glob(os.path.join(folkwiki_dir, '*.abc'))
    log.info(f'  manifest entries: {len(manifest)}  abc files: {len(abc_files)}')
    if len(abc_files) < len(manifest):
        errors.append(
            f'Folkwiki file cache incomplete: {len(abc_files)} .abc files for '
            f'{len(manifest)} manifest entries.'
        )

    if pageid_path and os.path.exists(pageid_path) and manifest:
        with open(pageid_path, encoding='utf-8') as f:
            pageids = json.load(f)
        pct = 100 * len(pageids) / len(manifest)
        log.info(f'  page-id coverage: {len(pageids)}/{len(manifest)} ({pct:.2f}%)')
        if pct < MIN_FOLKWIKI_PAGEID_COVERAGE_PCT:
            errors.append(
                f'Folkwiki page-id coverage too low: {pct:.1f}% < '
                f'{MIN_FOLKWIKI_PAGEID_COVERAGE_PCT}%.')
        stale = set(pageids) - set(manifest)
        if stale:
            log.warning(
                f'{len(stale)} page-id hashes are missing from the manifest. '
                'Treated as a warning: the crawl retains small numbers of '
                'stale entries.')


def check_unpublished_absent(out_dir, errors):
    """An unpublished dataset must not be sitting in the deploy directory."""
    for ds_id in UNPUBLISHED:
        path = os.path.join(out_dir, f'{ds_id}.json')
        if os.path.exists(path):
            errors.append(
                f'{path} exists but {ds_id} must not be published. Nothing may '
                'serve it; it is imported by hand in the app.')


def validate(parent_dir, manifest_path=None, pageid_path=None,
             allow_missing_output=False, only_dataset=None):
    data_dir = os.path.join(parent_dir, 'data')
    folkwiki_dir = os.path.join(data_dir, 'folkwiki')
    errors = []
    loaded = {}

    wanted = [only_dataset] if only_dataset else list(DATASETS)

    missing = []
    for ds_id in wanted:
        path = os.path.join(data_dir, f'{ds_id}.json')
        if not os.path.exists(path):
            missing.append(ds_id)
            continue
        loaded[ds_id] = validate_dataset(ds_id, path, errors)

    if missing:
        message = f'Dataset files not found: {", ".join(missing)}'
        if allow_missing_output:
            log.warning(message)
            if not loaded:
                return 1
        else:
            log.error(message)
            sys.exit(1)

    if only_dataset == 'folkwiki' or not only_dataset:
        validate_folkwiki_cache(
            folkwiki_dir, manifest_path, pageid_path, errors)

    if not only_dataset:
        if len(loaded) > 1:
            check_disjoint_ids(loaded, errors)
        manifest = os.path.join(data_dir, 'datasets.json')
        if os.path.exists(manifest):
            validate_manifest(manifest, loaded, errors)
        elif not allow_missing_output:
            errors.append(f'datasets.json not found at {manifest}')
        validate_legacy_bundle(data_dir, loaded, errors)
        check_unpublished_absent(
            os.path.join(parent_dir, '..', 'public'), errors)

    if errors:
        log.error('VALIDATION FAILED:')
        for e in errors:
            log.error(f'  * {e}')
        if allow_missing_output:
            return 1
        sys.exit(1)

    log.info('Validation passed.')
    return 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Validate the built dataset files before deployment'
    )
    parser.add_argument(
        'dir', help='Parent directory for the `data` directory'
    )
    parser.add_argument('--manifest-path')
    parser.add_argument('--pageid-path')
    parser.add_argument(
        '--dataset', choices=sorted(DATASETS),
        help='Validate only this dataset, skipping the cross-file and legacy '
             'checks. Used by build.sh as a rebuild trigger.'
    )
    parser.add_argument(
        '--allow-missing-output', action='store_true',
        help='Return a non-zero status instead of exiting immediately when '
             'output is missing'
    )
    args = parser.parse_args()
    sys.exit(validate(
        args.dir,
        manifest_path=args.manifest_path,
        pageid_path=args.pageid_path,
        allow_missing_output=args.allow_missing_output,
        only_dataset=args.dataset,
    ))
