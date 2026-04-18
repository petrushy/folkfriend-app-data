"""Sanity-check the built folkfriend-non-user-data.json before deployment.

Exits with status 1 if any check fails, which causes build.sh (set -e) to
abort before the file is moved to public/ and deployed.
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

# ---- Thresholds -------------------------------------------------------
# Conservative lower bounds; real numbers as of April 2026:
#   ~54 300 thesession + ~7 600 folkwiki = ~61 900 settings total
#   ~23 000 thesession + ~7 600 folkwiki = ~30 600 alias entries
#     (aliases are per-tune, not per-setting)
#   <0.1 % empty contours across the full set
MIN_SETTINGS = 55_000
MIN_ALIASES = 25_000
MAX_EMPTY_CONTOUR_PCT = 5.0   # alert if more than 5 % have no contour
MIN_FOLKWIKI_SETTINGS = 5_000  # folkwiki tune_ids start at 1_000_000
MIN_FOLKWIKI_PAGEID_COVERAGE_PCT = 65.0
MIN_FOLKWIKI_SOURCE_URL_COVERAGE_PCT = 95.0
# -----------------------------------------------------------------------


def validate(parent_dir, manifest_path=None, pageid_path=None,
             allow_missing_output=False):
    data_path = os.path.join(
        parent_dir, 'data', 'folkfriend-non-user-data.json'
    )
    meta_path = os.path.join(parent_dir, 'data', 'nud-meta.json')
    folkwiki_dir = os.path.join(parent_dir, 'data', 'folkwiki')

    if not os.path.exists(data_path):
        if allow_missing_output:
            log.warning(f'Output file not found: {data_path}')
            return 1
        log.error(f'Output file not found: {data_path}')
        sys.exit(1)

    log.info(f'Loading {data_path} ...')
    with open(data_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    settings = data.get('settings', {})
    aliases = data.get('aliases', {})
    errors = []

    # 1. Minimum settings count
    n_settings = len(settings)
    log.info(f'  settings: {n_settings}')
    if n_settings < MIN_SETTINGS:
        errors.append(
            f'Too few settings: {n_settings} < {MIN_SETTINGS}. '
            'Pipeline may have failed to merge folkwiki data.'
        )

    # 2. Minimum alias count
    n_aliases = len(aliases)
    log.info(f'  aliases:  {n_aliases}')
    if n_aliases < MIN_ALIASES:
        errors.append(
            f'Too few alias entries: {n_aliases} < {MIN_ALIASES}.'
        )

    # 3. Empty contour rate
    empty = sum(1 for s in settings.values() if not s.get('contour'))
    pct = 100 * empty / n_settings if n_settings else 100
    log.info(f'  empty contours: {empty}/{n_settings} ({pct:.2f}%)')
    if pct > MAX_EMPTY_CONTOUR_PCT:
        errors.append(
            f'Too many empty contours: {empty}/{n_settings} ({pct:.1f}%)'
            f' > {MAX_EMPTY_CONTOUR_PCT}%.'
        )

    # 4. Folkwiki settings present (tune_id >= 1_000_000)
    n_folkwiki = sum(
        1 for s in settings.values()
        if int(s.get('tune_id', 0)) >= 1_000_000
    )
    log.info(f'  folkwiki settings: {n_folkwiki}')
    if n_folkwiki < MIN_FOLKWIKI_SETTINGS:
        errors.append(
            f'Too few folkwiki settings: {n_folkwiki} < '
            f'{MIN_FOLKWIKI_SETTINGS}. '
            'Folkwiki merge may have been skipped or failed.'
        )

    # 4b. Folkwiki source URL coverage
    fw_settings = [
        s for s in settings.values()
        if int(s.get('tune_id', 0)) >= 1_000_000
    ]
    if fw_settings:
        fw_with_source_url = sum(1 for s in fw_settings if s.get('source_url'))
        source_url_pct = 100 * fw_with_source_url / len(fw_settings)
        log.info(
            f'  folkwiki source_url coverage: {fw_with_source_url}/'
            f'{len(fw_settings)} ({source_url_pct:.2f}%)'
        )
        if source_url_pct < MIN_FOLKWIKI_SOURCE_URL_COVERAGE_PCT:
            errors.append(
                f'Folkwiki source_url coverage too low: {source_url_pct:.1f}% '
                f'< {MIN_FOLKWIKI_SOURCE_URL_COVERAGE_PCT}%.'
            )

    # 5. Required fields present on all settings
    required_fields = {
        'tune_id', 'meter', 'mode', 'abc', 'dance', 'contour', 'origin'
    }
    missing_fields_count = 0
    for sid, s in settings.items():
        missing = required_fields - set(s.keys())
        if missing:
            missing_fields_count += 1
            if missing_fields_count <= 3:
                log.warning(f'  setting {sid} missing fields: {missing}')
    if missing_fields_count:
        errors.append(
            f'{missing_fields_count} settings are missing required fields.'
        )

    missing_fw_source_url = sum(
        1 for s in fw_settings
        if not s.get('source_url')
    )
    if missing_fw_source_url:
        errors.append(
            f'{missing_fw_source_url} Folkwiki settings are missing source_url.'
        )

    # 6. nud-meta.json has required fields
    if os.path.exists(meta_path):
        with open(meta_path, 'r') as f:
            meta = json.load(f)
        if 'date' not in meta:
            errors.append('nud-meta.json is missing the "date" field.')
        if 'v' not in meta:
            errors.append('nud-meta.json is missing the "v" field.')
        log.info(f'  nud-meta: {meta}')
    else:
        errors.append(f'nud-meta.json not found at {meta_path}')

    # 7. Folkwiki manifest/file parity
    if manifest_path and os.path.exists(manifest_path):
        with open(manifest_path, 'r', encoding='utf-8') as f:
            manifest = json.load(f)
        abc_files = glob.glob(os.path.join(folkwiki_dir, '*.abc'))
        log.info(f'  folkwiki manifest entries: {len(manifest)}')
        log.info(f'  folkwiki abc files: {len(abc_files)}')
        if len(abc_files) < len(manifest):
            errors.append(
                f'Folkwiki file cache incomplete: {len(abc_files)} .abc files '
                f'for {len(manifest)} manifest entries.'
            )
    elif manifest_path:
        errors.append(f'Folkwiki manifest not found at {manifest_path}')

    # 8. Page-id coverage
    if manifest_path and pageid_path and os.path.exists(manifest_path) and os.path.exists(pageid_path):
        with open(manifest_path, 'r', encoding='utf-8') as f:
            manifest = json.load(f)
        with open(pageid_path, 'r', encoding='utf-8') as f:
            pageids = json.load(f)
        if manifest:
            pageid_coverage_pct = 100 * len(pageids) / len(manifest)
            log.info(
                f'  folkwiki page-id coverage: {len(pageids)}/{len(manifest)} '
                f'({pageid_coverage_pct:.2f}%)'
            )
            if pageid_coverage_pct < MIN_FOLKWIKI_PAGEID_COVERAGE_PCT:
                errors.append(
                    f'Folkwiki page-id coverage too low: {pageid_coverage_pct:.1f}% '
                    f'< {MIN_FOLKWIKI_PAGEID_COVERAGE_PCT}%.'
                )
            missing_manifest_hashes = set(pageids) - set(manifest)
            log.info(
                f'  page-id hashes missing from manifest: {len(missing_manifest_hashes)}'
            )
            if missing_manifest_hashes:
                log.warning(
                    f'{len(missing_manifest_hashes)} page-id hashes are missing from the '
                    'Folkwiki manifest. This is treated as a warning because '
                    'the page-id crawl can retain small numbers of stale entries.'
                )

    # ---- Result --------------------------------------------------------
    if errors:
        log.error('VALIDATION FAILED:')
        for e in errors:
            log.error(f'  * {e}')
        if allow_missing_output:
            return 1
        sys.exit(1)
    else:
        log.info('Validation passed.')
        return 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Validate folkfriend-non-user-data.json before deployment'
    )
    parser.add_argument(
        'dir', help='Parent directory for the `data` directory'
    )
    parser.add_argument('--manifest-path')
    parser.add_argument('--pageid-path')
    parser.add_argument(
        '--allow-missing-output', action='store_true',
        help='Return a non-zero status instead of exiting immediately when the output is missing'
    )
    args = parser.parse_args()
    sys.exit(validate(
        args.dir,
        manifest_path=args.manifest_path,
        pageid_path=args.pageid_path,
        allow_missing_output=args.allow_missing_output,
    ))
