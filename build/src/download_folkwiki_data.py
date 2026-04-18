import argparse
import json
import logging
import os
import pathlib
import re
import time
from urllib.parse import unquote

import requests

logging.basicConfig(level=logging.DEBUG,
                    format='[%(name)s:%(lineno)s] %(message)s')
log = logging.getLogger(os.path.basename(__file__))

# The folkwiki.se/pub/cache/ directory listing is intercepted by WordPress,
# but individual .abc files are still served directly by Apache.
# We use the Wayback Machine CDX API to discover all .abc URLs, then fetch
# them live from folkwiki.se.
FOLKWIKI_BASE = 'http://www.folkwiki.se/pub/cache/'
CDX_API = 'http://web.archive.org/cdx/search/cdx'

HEXHASH_RE = re.compile(r'_([0-9a-f]{6})\.abc$', re.IGNORECASE)

MAX_RETRIES = 3
RETRY_SLEEP = 2  # seconds


def make_session():
    session = requests.Session()
    session.headers.update({
        'User-Agent': 'folkfriend-app-data/1.0 (+https://folkfriend-data.web.app)'
    })
    return session


def fetch_with_retries(session, url, timeout=30, **kwargs):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = session.get(url, timeout=timeout, **kwargs)
            if r.status_code == 404:
                return r
            r.raise_for_status()
            return r
        except Exception as e:
            log.warning(
                f'Attempt {attempt}/{MAX_RETRIES} failed for {url}: {e}'
            )
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_SLEEP * attempt)
    raise RuntimeError(
        f'Failed to fetch {url} after {MAX_RETRIES} attempts'
    )


def discover_via_cdx():
    """Use Wayback Machine CDX API to enumerate all folkwiki .abc URLs.

    Returns dict: hexhash -> {'name': str, 'url': str}
    """
    log.info(
        'Querying Wayback Machine CDX for folkwiki .abc file list...'
    )
    session = make_session()
    r = fetch_with_retries(
        session,
        CDX_API,
        timeout=120,
        params={
            'url': 'folkwiki.se/pub/cache/',
            'matchType': 'prefix',
            'output': 'text',
            'fl': 'original',
            'collapse': 'urlkey',
            'limit': 100000,
            'filter': ['original:.*\\.abc'],
        },
    )

    tunes = {}
    for raw_url in r.text.strip().splitlines():
        raw_url = raw_url.strip()
        if not raw_url:
            continue

        # Normalise: strip port 80 variant, ensure http
        url = raw_url.replace(':80/', '/')
        if not url.startswith('http'):
            url = 'http://' + url

        # Decode percent-encoding to get the filename
        decoded = unquote(url)
        m = HEXHASH_RE.search(decoded)
        if not m:
            continue
        hexhash = m.group(1).lower()

        # Derive tune name: strip path prefix and hash+extension suffix
        filename = decoded.split('/')[-1]       # "Pellikvalsen_12b397.abc"
        raw_name = filename[:-4].rsplit('_', 1)[0]  # strip ".abc" + hash
        tune_name = raw_name.replace('_', ' ').strip()

        # Reconstruct a live URL using the original percent-encoded path
        path_part = url.split('/pub/cache/')[-1]
        live_url = FOLKWIKI_BASE + path_part

        if hexhash not in tunes:
            tunes[hexhash] = {'name': tune_name, 'url': live_url}

    log.info(f'CDX discovery found {len(tunes)} unique .abc hashes')
    return tunes


def download_folkwiki_data(parent_dir, offline=False):
    folkwiki_dir = os.path.join(parent_dir, 'data', 'folkwiki')
    pathlib.Path(folkwiki_dir).mkdir(parents=True, exist_ok=True)

    manifest_path = os.path.join(folkwiki_dir, 'manifest.json')

    # Load existing manifest if present
    if os.path.exists(manifest_path):
        with open(manifest_path, 'r', encoding='utf-8') as f:
            manifest = json.load(f)
        log.info(
            f'Loaded existing manifest with {len(manifest)} entries'
        )
    else:
        manifest = {}

    if offline:
        log.info(
            '--offline: skipping discovery/download, using cached files'
        )
        if not manifest:
            log.error(
                'No manifest found and --offline set. '
                'Run without --offline first.'
            )
        return

    session = make_session()

    # --- Discover available tunes via CDX ---
    try:
        discovered = discover_via_cdx()
    except RuntimeError as e:
        log.error(f'CDX discovery failed: {e}')
        if manifest:
            log.warning(
                'Falling back to previously cached manifest and files.'
            )
            return
        raise

    # --- Download each ABC file live from folkwiki.se ---
    newly_downloaded = 0
    failed = []

    for hexhash, info in discovered.items():
        abc_path = os.path.join(folkwiki_dir, f'{hexhash}.abc')

        if os.path.exists(abc_path):
            if hexhash not in manifest:
                manifest[hexhash] = info
            continue

        try:
            log.debug(f'Downloading {info["url"]}')
            r = fetch_with_retries(session, info['url'])
            if r.status_code == 404:
                log.warning(
                    f'Skipping {hexhash} ({info["name"]}): upstream returned 404'
                )
                failed.append(hexhash)
                continue
            # Force UTF-8: server returns text/plain without charset
            # declaration, so requests defaults to ISO-8859-1 and mangles
            # Swedish characters. Use raw bytes + explicit UTF-8 decode.
            content = r.content.decode('utf-8', errors='replace')
            if 'X:' not in content and 'T:' not in content:
                log.warning(
                    f'Unexpected content at {info["url"]}, skipping'
                )
                failed.append(hexhash)
                continue
            with open(abc_path, 'w', encoding='utf-8') as f:
                f.write(content)
            manifest[hexhash] = info
            newly_downloaded += 1
        except RuntimeError as e:
            log.warning(
                f'Skipping {hexhash} ({info["name"]}): {e}'
            )
            failed.append(hexhash)

    # Persist updated manifest
    with open(manifest_path, 'w', encoding='utf-8') as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    tail = '...' if len(failed) > 20 else ''
    log.info(
        f'Done. Downloaded {newly_downloaded} new files, '
        f'{len(failed)} failed, '
        f'{len(manifest)} total in manifest.'
    )
    if failed:
        log.warning(
            f'Failed hashes ({len(failed)}): {failed[:20]}{tail}'
        )


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Download ABC tune files from folkwiki.se cache')
    parser.add_argument(
        'dir', help='Parent directory for the `data` directory'
    )
    parser.add_argument(
        '--offline', action='store_true',
        help='Skip download; use only previously cached files'
    )
    args = parser.parse_args()
    download_folkwiki_data(args.dir, offline=args.offline)
