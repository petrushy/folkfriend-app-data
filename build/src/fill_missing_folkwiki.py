"""Fill gaps in the folkwiki manifest by fetching tunes that the CDX API
missed due to Latin-1-encoded filenames.

For each hexhash present in hexhash_to_pageid.json but absent from manifest.json:
  1. Fetch http://www.folkwiki.se/Musik/{pageID}
  2. Extract all .abc hrefs from the page HTML
  3. Match the one whose URL contains the target hexhash
  4. Download the ABC file
  5. Save to data/folkwiki/{hexhash}.abc and update the manifest

Usage:
    cd build
    . env.sh
    python src/fill_missing_folkwiki.py .
"""

import argparse
import json
import logging
import os
import pathlib
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import unquote

import requests

logging.basicConfig(level=logging.INFO,
                    format='[%(name)s:%(lineno)s] %(message)s')
log = logging.getLogger(os.path.basename(__file__))

MAX_RETRIES = 3
RETRY_SLEEP = 3  # seconds
MAX_WORKERS = 5   # parallel page fetches (lower to avoid rate limiting)
HEXHASH_RE = re.compile(r'_([0-9a-f]{6})\.abc', re.IGNORECASE)

# Match <a href='..._{hexhash}.abc'> or <a href="..._{hexhash}.abc">
ABC_HREF_RE = re.compile(
    r"""href=['"]([^'"]*_[0-9a-f]{6}\.abc)['"]""",
    re.IGNORECASE,
)


def fetch_with_retries(url, timeout=30, **kwargs):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(url, timeout=timeout, **kwargs)
            r.raise_for_status()
            return r
        except Exception as e:
            log.warning(f'Attempt {attempt}/{MAX_RETRIES} failed for {url}: {e}')
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_SLEEP)
    raise RuntimeError(f'Failed to fetch {url} after {MAX_RETRIES} attempts')


def extract_abc_urls_from_page(html):
    """Return list of (hexhash, full_url) found in the page HTML."""
    results = []
    for m in ABC_HREF_RE.finditer(html):
        href = m.group(1)
        # Ensure absolute URL
        if href.startswith('/'):
            href = 'http://www.folkwiki.se' + href
        # Decode to find the hexhash in the filename
        decoded = unquote(href, encoding='latin-1')
        hm = HEXHASH_RE.search(decoded)
        if hm:
            hexhash = hm.group(1).lower()
            results.append((hexhash, href))
    return results


def process_page(page_id, target_hashes, folkwiki_dir, manifest):
    """Fetch a single wiki page, extract ABC URLs, download matching files.

    Returns list of (hexhash, name, url) for newly downloaded tunes,
    or empty list on failure.
    """
    url = f'http://www.folkwiki.se/Musik/{page_id}'
    try:
        r = fetch_with_retries(url, timeout=30)
    except RuntimeError as e:
        log.warning(f'Page {page_id}: {e}')
        return []

    html = r.text
    abc_entries = extract_abc_urls_from_page(html)
    if not abc_entries:
        log.debug(f'Page {page_id}: no .abc links found')
        return []

    downloaded = []
    for hexhash, abc_url in abc_entries:
        if hexhash not in target_hashes:
            continue  # Not one we're looking for

        abc_path = os.path.join(folkwiki_dir, f'{hexhash}.abc')
        if os.path.exists(abc_path):
            # File already on disk (perhaps downloaded by a parallel worker
            # processing another page_id that shares this tune).
            # Derive tune name from URL anyway for manifest.
            decoded = unquote(abc_url, encoding='latin-1')
            filename = decoded.split('/')[-1]
            raw_name = filename[:-4].rsplit('_', 1)[0]
            tune_name = raw_name.replace('_', ' ').strip()
            downloaded.append((hexhash, tune_name, abc_url))
            continue

        try:
            log.debug(f'Downloading {abc_url}')
            r2 = fetch_with_retries(abc_url, timeout=30)
            content = r2.content.decode('utf-8', errors='replace')
            if 'X:' not in content and 'T:' not in content:
                log.warning(f'Unexpected content at {abc_url}, skipping')
                continue
            with open(abc_path, 'w', encoding='utf-8') as f:
                f.write(content)

            # Derive tune name from URL
            decoded = unquote(abc_url, encoding='latin-1')
            filename = decoded.split('/')[-1]
            raw_name = filename[:-4].rsplit('_', 1)[0]
            tune_name = raw_name.replace('_', ' ').strip()
            downloaded.append((hexhash, tune_name, abc_url))
            log.info(f'  Saved {hexhash}: {tune_name}')
        except RuntimeError as e:
            log.warning(f'Failed to download {abc_url}: {e}')

    return downloaded


def fill_missing(parent_dir):
    folkwiki_dir = os.path.join(parent_dir, 'data', 'folkwiki')
    pathlib.Path(folkwiki_dir).mkdir(parents=True, exist_ok=True)

    manifest_path = os.path.join(folkwiki_dir, 'manifest.json')
    pageid_path = os.path.join(folkwiki_dir, 'hexhash_to_pageid.json')

    if not os.path.exists(pageid_path):
        log.error(f'hexhash_to_pageid.json not found at {pageid_path}')
        return

    with open(manifest_path, 'r', encoding='utf-8') as f:
        manifest = json.load(f)
    with open(pageid_path, 'r', encoding='utf-8') as f:
        hexhash_to_pageid = json.load(f)

    # Build the set of missing hashes and group by page_id
    missing = {h: pid for h, pid in hexhash_to_pageid.items()
               if h not in manifest}
    log.info(f'Found {len(missing)} missing hashes to fill')

    if not missing:
        log.info('Nothing to do.')
        return

    # Group missing hashes by page_id (one page fetch may yield multiple tunes)
    page_to_hashes = {}
    for hexhash, page_id in missing.items():
        page_to_hashes.setdefault(page_id, set()).add(hexhash)

    log.info(f'Need to fetch {len(page_to_hashes)} unique wiki pages')

    newly_downloaded = 0
    failed_pages = 0
    save_interval = 100  # Save manifest every N pages

    page_items = list(page_to_hashes.items())

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(
                process_page, page_id, hashes, folkwiki_dir, manifest
            ): page_id
            for page_id, hashes in page_items
        }

        done_count = 0
        for future in as_completed(futures):
            page_id = futures[future]
            done_count += 1
            try:
                downloaded = future.result()
                for hexhash, tune_name, abc_url in downloaded:
                    if hexhash not in manifest:
                        manifest[hexhash] = {'name': tune_name, 'url': abc_url}
                        newly_downloaded += 1
            except Exception as e:
                log.warning(f'Page {page_id} raised: {e}')
                failed_pages += 1

            if done_count % save_interval == 0:
                log.info(f'Progress: {done_count}/{len(page_items)} pages, '
                         f'{newly_downloaded} new files so far...')
                with open(manifest_path, 'w', encoding='utf-8') as f:
                    json.dump(manifest, f, ensure_ascii=False, indent=2)

    # Final save
    with open(manifest_path, 'w', encoding='utf-8') as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    log.info(f'Done. {newly_downloaded} new files added, '
             f'{failed_pages} pages failed. '
             f'Manifest now has {len(manifest)} entries.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Fill missing folkwiki ABC files from wiki pages')
    parser.add_argument(
        'dir', help='Parent directory for the `data` directory'
    )
    args = parser.parse_args()
    fill_missing(args.dir)
