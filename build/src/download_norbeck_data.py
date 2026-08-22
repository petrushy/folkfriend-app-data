"""Download Henrik Norbeck's ABC collection.

The collection ships as a single small zip (~640 KB) of multi-tune .abc files,
grouped by rhythm into three category directories: `i/` (Irish and Scottish),
`s/` (Swedish and Scandinavian), `m/` (everything else), plus `henrikc.abc`
(his own compositions) at the root.

Unlike folkwiki this needs no discovery crawl — one file, one request.

The zip name encodes its release (`hn` + YYYYMM), so it changes whenever he
publishes. Resolve it from the download page rather than hardcoding, or a
pinned name silently 404s the day he updates.

Licensing: the collection is his copyright and carries redistribution terms —
see the header comment in build_norbeck_data.py and the note in CLAUDE.md.
"""

import argparse
import hashlib
import io
import json
import logging
import os
import pathlib
import re
import zipfile
from datetime import datetime, timezone

import requests

logging.basicConfig(level=logging.DEBUG,
                    format='[%(name)s:%(lineno)s] %(message)s')
log = logging.getLogger(os.path.basename(__file__))

BASE_URL = 'https://www.norbeck.nu/abc/'
DOWNLOAD_PAGE = BASE_URL + 'download.asp'
ZIP_NAME_RE = re.compile(r'\b(hn\d{6}\.zip)\b', re.IGNORECASE)
REQUEST_TIMEOUT = 60


def resolve_zip_name(session):
    """Find the current zip filename from the download page.

    Returns e.g. 'hn202601.zip'. Raises if the page cannot be read or names no
    zip — guessing a name here would mean silently building from a stale cache.
    """
    log.info(f'Resolving current zip name from {DOWNLOAD_PAGE}')
    r = session.get(DOWNLOAD_PAGE, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    names = ZIP_NAME_RE.findall(r.text)
    if not names:
        raise RuntimeError(
            f'No hnYYYYMM.zip link found on {DOWNLOAD_PAGE}. The page layout '
            'may have changed; check it by hand before overriding.'
        )
    # Newest by name — the YYYYMM ordering is lexicographic.
    return sorted(set(n.lower() for n in names))[-1]


def download_norbeck_data(parent_dir, offline=False, zip_name=None):
    norbeck_dir = os.path.join(parent_dir, 'data', 'norbeck')
    abc_dir = os.path.join(norbeck_dir, 'abc')
    manifest_path = os.path.join(norbeck_dir, 'manifest.json')
    pathlib.Path(abc_dir).mkdir(parents=True, exist_ok=True)

    if offline:
        if not os.path.exists(manifest_path):
            log.error(
                f'--offline given but no manifest at {manifest_path}. '
                'Run without --offline once to fetch the collection.'
            )
            return 1
        with open(manifest_path, encoding='utf-8') as f:
            manifest = json.load(f)
        log.info(
            f'Offline: using cached release {manifest["release"]} '
            f'({len(manifest["files"])} ABC files)'
        )
        return 0

    session = requests.Session()
    session.headers['User-Agent'] = (
        'folkfriend-app-data build (https://github.com/petrush/folkfriend)'
    )

    if zip_name is None:
        zip_name = resolve_zip_name(session)
    url = BASE_URL + zip_name
    log.info(f'Downloading {url}')

    r = session.get(url, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    payload = r.content
    digest = hashlib.sha1(payload).hexdigest()
    log.info(f'Got {len(payload)} bytes, sha1={digest}')

    # Keep the zip itself so a rebuild can be reproduced from the exact bytes
    # that produced a given dataset version.
    with open(os.path.join(norbeck_dir, zip_name), 'wb') as f:
        f.write(payload)

    files = []
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        for info in zf.infolist():
            if info.is_dir() or not info.filename.lower().endswith('.abc'):
                continue
            # Zip entries are 'i/hnr0.abc'. Refuse anything that would escape
            # the extraction directory.
            rel = info.filename.replace('\\', '/')
            if rel.startswith('/') or '..' in rel.split('/'):
                log.warning(f'Skipping suspicious zip entry {rel!r}')
                continue
            out_path = os.path.join(abc_dir, rel)
            pathlib.Path(os.path.dirname(out_path)).mkdir(
                parents=True, exist_ok=True)
            with open(out_path, 'wb') as f:
                f.write(zf.read(info))
            files.append(rel)

    files.sort()
    manifest = {
        'release': zip_name,
        'url': url,
        'sha1': digest,
        'bytes': len(payload),
        'downloaded_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'files': files,
    }
    with open(manifest_path, 'w', encoding='utf-8') as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)

    log.info(f'Extracted {len(files)} ABC files to {abc_dir}')
    return 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description="Download Henrik Norbeck's ABC tune collection")
    parser.add_argument(
        'dir', help='Parent directory for the `data` directory')
    parser.add_argument(
        '--offline', action='store_true',
        help='Use the cached extraction; make no network requests')
    parser.add_argument(
        '--zip-name',
        help='Override the zip filename (e.g. hn202601.zip) instead of '
             'resolving it from the download page')
    args = parser.parse_args()
    raise SystemExit(
        download_norbeck_data(args.dir, args.offline, args.zip_name))
