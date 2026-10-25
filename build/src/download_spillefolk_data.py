"""Download the Folkets Hus Spillefolk tune collection (spillefolk.dk).

PRIVATE DATASET — built for personal import into FolkFriend, never published.
spillefolk.dk is a small non-commercial club site; its rights page
(nodesamlingen/om-nodesamlingen/rettigheder-interlectual-properties/) shares
the tunes "from instrument to ear" and notes that some are by living
composers. Republishing the collection is not something it grants, so this
follows the Norbeck model: build locally, import by hand (Settings → "Add a
database"), and keep the file out of public/. assemble_datasets.py marks it
unpublished and validate_output.py / smoke_test.py check it stays that way.

What is fetched, and how:

- The catalogue comes from two of the site's own list pages —
  listnode.php?KnapTryk=medegn (tunes with a region) and ...=udenegn (without)
  — which together list every tune: key, title, country, regions, and the
  dance-type heading it is filed under. 3,339 tunes in October 2026. The live
  search (searchTxt.php) is NOT used: it only answers inside a browser session.
- Each tune's MIDI file, mid/<key>.mid. Every tune has one; only about a
  quarter also have MusicXML, so MIDI is the one format the whole collection
  shares. It is exact (exported from notation software), which is what the
  contour needs, and midi2abc turns it into displayable ABC.

Politeness: one request at a time, a pause between them, resumable (files
already on disk are kept unless --refresh), and a descriptive User-Agent. A
first full run is ~3,300 requests and takes about an hour.

Output (build/data/spillefolk/, git-ignored):
    catalogue.json   key -> {title, sub, country, regions, dance_heading}
    mid/<key>.mid
    manifest.json    key -> {status, bytes, sha256, fetched_at}

Usage:
    python src/download_spillefolk_data.py .            # resume
    python src/download_spillefolk_data.py . --refresh  # re-fetch every MIDI
"""

import argparse
import datetime
import hashlib
import html
import json
import logging
import os
import pathlib
import re
import time

import requests

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s %(levelname)s %(message)s',
                    datefmt='%H:%M:%S')
log = logging.getLogger('download_spillefolk')

SITE = 'https://spillefolk.dk/nodesamling'
LIST_PAGES = ('medegn', 'udenegn')
USER_AGENT = ('folkfriend-app-data (personal FolkFriend import; '
              '+https://folkfriend-data.web.app)')
PAUSE_SECONDS = 1.0
TIMEOUT = 45
TRIES = 4

# Prefix "bi-" is the biographies section: people, not tunes.
NOT_TUNES = ('bi',)

ROW_RE = re.compile(
    r'<a href=visnode\.php\?key=([^ >]+)[^>]*>\s*<font[^>]*>(.*?)</font></a>'
    r'\s*<font[^>]*>(.*?)</font>', re.S)
PLACE_RE = re.compile(r'\(([A-ZÆØÅ]{1,3}) - ([^)]*)\)\s*$')


def session():
    s = requests.Session()
    s.headers['User-Agent'] = USER_AGENT
    return s


def fetch(sess, url):
    """Body bytes, or raise after TRIES attempts. The site answers its PHP
    pages with HTTP 404 even when they render normally, so the status of a
    list page is not checked; its content is."""
    err = None
    for attempt in range(1, TRIES + 1):
        try:
            r = sess.get(url, timeout=TIMEOUT)
            return r
        except requests.RequestException as e:
            err = e
            time.sleep(3 * attempt)
    raise RuntimeError(f'{url}: {err}')


def text(fragment):
    return html.unescape(re.sub(r'\s+', ' ', fragment)).replace('\xa0', ' ').strip()


def parse_list_page(page_html):
    tunes = {}
    for section in re.split(r'<h1>', page_html)[1:]:
        heading = text(section.split('</h1>')[0])
        for key, title, rest in ROW_RE.findall(section):
            rest = text(rest)
            place = PLACE_RE.search(rest)
            tunes[key] = {
                'title': text(title),
                'sub': rest[:place.start()].strip() if place else rest,
                'country': place.group(1) if place else '',
                'regions': place.group(2).strip() if place else '',
                'dance_heading': heading,
            }
    return tunes


def fetch_catalogue(sess):
    catalogue = {}
    for kind in LIST_PAGES:
        r = fetch(sess, f'{SITE}/listnode.php?KnapTryk={kind}&T1=Special')
        found = parse_list_page(r.content.decode('utf-8', errors='replace'))
        if not found:
            raise SystemExit(
                f'The {kind} list page parsed to nothing — the site layout has '
                'probably changed. Not touching the existing catalogue.')
        log.info(f'{kind}: {len(found)} entries')
        catalogue.update(found)
        time.sleep(PAUSE_SECONDS)
    return {k: v for k, v in catalogue.items()
            if k.split('-')[0] not in NOT_TUNES}


def download(parent_dir, refresh=False):
    out = pathlib.Path(parent_dir, 'data', 'spillefolk')
    (out / 'mid').mkdir(parents=True, exist_ok=True)
    manifest_path = out / 'manifest.json'
    manifest = (json.loads(manifest_path.read_text())
                if manifest_path.exists() else {})
    sess = session()

    catalogue = fetch_catalogue(sess)
    (out / 'catalogue.json').write_text(
        json.dumps(catalogue, ensure_ascii=False, indent=1), encoding='utf-8')
    log.info(f'Catalogue: {len(catalogue)} tunes')

    def save():
        tmp = manifest_path.with_suffix('.tmp')
        tmp.write_text(json.dumps(manifest, indent=1))
        tmp.replace(manifest_path)

    failed, no_midi = [], []
    keys = sorted(catalogue)
    for i, key in enumerate(keys, 1):
        path = out / 'mid' / f'{key}.mid'
        # A tune the site has no MIDI for (43 in October 2026, mostly tunes
        # published only as PDF) answers 404 every time; asking again on each
        # run is pointless. --refresh re-checks them.
        if manifest.get(key, {}).get('status') == 'no-midi' and not refresh:
            continue
        if path.exists() and not refresh and path.read_bytes()[:4] == b'MThd':
            if manifest.get(key, {}).get('status') != 'ok':
                body = path.read_bytes()
                manifest[key] = {'status': 'ok', 'bytes': len(body),
                                 'sha256': hashlib.sha256(body).hexdigest(),
                                 'fetched_at': None}
            continue
        try:
            r = fetch(sess, f'{SITE}/mid/{key}.mid')
            body = r.content
            if r.status_code == 404:
                manifest[key] = {'status': 'no-midi'}
                no_midi.append(key)
                time.sleep(PAUSE_SECONDS)
                continue
            if r.status_code != 200 or body[:4] != b'MThd':
                raise RuntimeError(f'HTTP {r.status_code}, not a MIDI file')
            path.write_bytes(body)
            manifest[key] = {
                'status': 'ok', 'bytes': len(body),
                'sha256': hashlib.sha256(body).hexdigest(),
                'fetched_at': datetime.datetime.now(
                    datetime.timezone.utc).isoformat(timespec='seconds'),
            }
        except RuntimeError as e:
            manifest[key] = {'status': 'error', 'error': str(e)}
            failed.append(key)
            log.warning(f'{key}: {e}')
        if i % 100 == 0:
            save()
            log.info(f'{i}/{len(keys)}')
        time.sleep(PAUSE_SECONDS)

    # A tune that left the site is reported, not deleted: the MIDI on disk is
    # still the only copy, and the build only uses what the catalogue lists.
    gone = sorted(set(manifest) - set(catalogue))
    for key in gone:
        manifest[key]['in_catalogue'] = False
    save()

    have = sum(1 for k in catalogue if (out / 'mid' / f'{k}.mid').exists())
    absent = sum(1 for k in catalogue
                 if manifest.get(k, {}).get('status') == 'no-midi')
    log.info(f'Done: {have}/{len(catalogue)} tunes have a MIDI file; '
             f'{absent} have none on the site; {len(failed)} failed this run '
             f'(network or bad file — re-run to retry); '
             f'{len(gone)} no longer listed.')
    if failed:
        log.warning(f'Failed (re-run to retry): {failed[:20]}')
    return 1 if failed else 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('dir', help='Parent directory for the `data` directory')
    parser.add_argument('--refresh', action='store_true',
                        help='Re-fetch MIDI files already on disk')
    args = parser.parse_args()
    raise SystemExit(download(args.dir, args.refresh))
