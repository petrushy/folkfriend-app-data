"""
Crawls folkwiki.se /Musik/{id} pages (id=1..MAX_ID) and builds a mapping
    hexhash -> page_id
saved to build/data/folkwiki/hexhash_to_pageid.json.

Each wiki page embeds its ABC file as a pub/cache/{name}_{hexhash}.abc link,
so we can reverse-lookup the page ID from any hexhash we downloaded.

Usage:
    python src/discover_folkwiki_pageids.py .
"""

import argparse
import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

logging.basicConfig(level=logging.INFO, format='[%(name)s:%(lineno)s] %(message)s')
log = logging.getLogger(os.path.basename(__file__))

MAX_ID = 6500          # 404 seen at 7000; scan a little past the last known hit
WORKERS = 20
TIMEOUT = 8
HEXHASH_RE = re.compile(r'pub/cache/[^"\']+_([0-9a-f]{6})\.abc', re.IGNORECASE)


def fetch_page(page_id):
    """Return (page_id, [hexhashes]) or (page_id, None) on 404/error."""
    url = f'http://www.folkwiki.se/Musik/{page_id}'
    try:
        r = requests.get(url, timeout=TIMEOUT)
        if r.status_code == 404:
            return page_id, None
        if r.status_code != 200:
            log.debug(f'{page_id}: status {r.status_code}')
            return page_id, None
        hashes = list(set(HEXHASH_RE.findall(r.text)))
        return page_id, hashes
    except Exception as e:
        log.debug(f'{page_id}: {e}')
        return page_id, None


def build_mapping(parent_dir):
    out_path = os.path.join(parent_dir, 'data', 'folkwiki', 'hexhash_to_pageid.json')

    # Load existing mapping if present (resume support)
    if os.path.exists(out_path):
        with open(out_path, 'r') as f:
            mapping = json.load(f)
        log.info(f'Loaded existing mapping with {len(mapping)} entries')
    else:
        mapping = {}

    ids_to_fetch = list(range(1, MAX_ID + 1))
    # Skip IDs whose pages we know are already in the mapping
    already_seen_pages = set()
    for page_id in mapping.values():
        already_seen_pages.add(page_id)
    ids_to_fetch = [i for i in ids_to_fetch if i not in already_seen_pages]
    log.info(f'Fetching {len(ids_to_fetch)} pages (already have {len(already_seen_pages)})')

    found = 0
    errors = 0
    batch_size = 500

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for batch_start in range(0, len(ids_to_fetch), batch_size):
            batch = ids_to_fetch[batch_start:batch_start + batch_size]
            futures = {pool.submit(fetch_page, pid): pid for pid in batch}

            for future in as_completed(futures):
                page_id, hashes = future.result()
                if hashes is None:
                    errors += 1
                    continue
                for h in hashes:
                    if h not in mapping:
                        mapping[h] = page_id
                        found += 1

            log.info(
                f'Progress: {batch_start + len(batch)}/{len(ids_to_fetch)} '
                f'| new mappings: {found} | skipped/404: {errors}'
            )
            # Save incrementally
            with open(out_path, 'w') as f:
                json.dump(mapping, f)

    log.info(f'Done. {len(mapping)} total hexhash→pageID mappings saved to {out_path}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Build hexhash→pageID mapping by crawling folkwiki /Musik/ pages')
    parser.add_argument('dir', help='Parent directory for the data directory')
    args = parser.parse_args()
    build_mapping(args.dir)
