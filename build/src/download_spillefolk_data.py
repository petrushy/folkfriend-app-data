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
- Each tune's MusicXML, xml/<key>.xml, where the site has one (about a
  quarter — the tunes entered in MuseScore; not linked from the tune page).
  It is the score as written, so the notation keeps repeats, endings, bar
  lines and upbeats. Preferred for display.
- Each tune's MIDI file, mid/<key>.mid. Every tune has one, and it is exact
  (exported from notation software), so it is what the contour is made from,
  and the notation fallback (midi2abc) when there is no MusicXML.

A 404 means the site has no such file for that tune; it is recorded as
'none' and not asked again unless --refresh.

Politeness: one request at a time, a pause between them, resumable (files
already on disk are kept unless --refresh), and a descriptive User-Agent. A
first full run is ~6,700 requests (both formats) and takes about two hours.

Output (build/data/spillefolk/, git-ignored):
    catalogue.json      key -> {title, sub, country, regions, dance_heading}
    mid/<key>.mid       manifest.json      key -> {status, bytes, sha256, ...}
    xml/<key>.xml       xml_manifest.json  (same shape)

Usage:
    python src/download_spillefolk_data.py .            # resume
    python src/download_spillefolk_data.py . --refresh  # re-fetch every MIDI
"""

import argparse
import datetime
import hashlib
import html
import io
import json
import logging
import os
import pathlib
import re
import time
import zipfile

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


def _is_musicxml(body):
    return (b'<score-partwise' in body[:4000]
            or b'<score-timewise' in body[:4000])


def unpack_musicxml(body):
    """The body to store for a tune's xml/<key>.xml, or None if it is not
    MusicXML. A few are COMPRESSED MusicXML (.mxl — a zip holding the score)
    and are unpacked; at least one is a zipped MuseScore .mscx served under
    the .xml name, which is not MusicXML and is left to the MIDI fallback."""
    if body[:2] == b'PK':
        try:
            with zipfile.ZipFile(io.BytesIO(body)) as z:
                for name in z.namelist():
                    if (name.lower().endswith(('.xml', '.musicxml'))
                            and not name.startswith('META-INF/')):
                        inner = z.read(name)
                        if _is_musicxml(inner):
                            return inner
        except zipfile.BadZipFile:
            return None
        return None
    return body if _is_musicxml(body) else None


# Each format the site serves per tune: (directory, URL path, extension,
# the body to store or None if it is not that format, manifest file).
FORMATS = {
    'midi': ('mid', 'mid', '.mid',
             lambda body: body if body[:4] == b'MThd' else None,
             'manifest.json'),
    # Notation as written — repeats, endings, bar lines, upbeats — which MIDI
    # cannot carry. About a quarter of the tunes have it (the ones entered in
    # MuseScore); xml2abc turns it into exact ABC.
    'musicxml': ('xml', 'xml', '.xml', unpack_musicxml, 'xml_manifest.json'),
}


def fetch_all(sess, out, keys, fmt, refresh):
    """Fetch one format for every key. 404 means the site has no such file
    for that tune and is recorded as 'none' (re-checked only with --refresh);
    anything else that is not the expected format is an error, retried on the
    next run. Returns (have, none, failed)."""
    folder, url_dir, ext, accept, manifest_name = FORMATS[fmt]
    (out / folder).mkdir(parents=True, exist_ok=True)
    manifest_path = out / manifest_name
    manifest = (json.loads(manifest_path.read_text())
                if manifest_path.exists() else {})

    def save():
        tmp = manifest_path.with_suffix('.tmp')
        tmp.write_text(json.dumps(manifest, indent=1))
        tmp.replace(manifest_path)

    failed = []
    for i, key in enumerate(keys, 1):
        path = out / folder / f'{key}{ext}'
        status = manifest.get(key, {}).get('status')
        # Older manifests wrote 'no-midi' for a 404.
        if status in ('none', 'no-midi', 'unsupported') and not refresh:
            continue
        if path.exists() and not refresh and accept(path.read_bytes()):
            if status != 'ok':
                body = path.read_bytes()
                manifest[key] = {'status': 'ok', 'bytes': len(body),
                                 'sha256': hashlib.sha256(body).hexdigest(),
                                 'fetched_at': None}
            continue
        try:
            r = fetch(sess, f'{SITE}/{url_dir}/{key}{ext}')
            body = r.content
            stored = accept(body) if r.status_code == 200 else None
            if r.status_code == 404:
                manifest[key] = {'status': 'none'}
            elif r.status_code == 200 and stored is None and body[:2] == b'PK':
                # A zip that holds no MusicXML (a MuseScore file served
                # under the .xml name): not ours to convert, and asking
                # again will not change it.
                manifest[key] = {'status': 'unsupported'}
            elif stored is None:
                raise RuntimeError(f'HTTP {r.status_code}, not a {fmt} file')
            else:
                body = stored
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
            log.warning(f'{fmt} {key}: {e}')
        if i % 100 == 0:
            save()
            log.info(f'{fmt}: {i}/{len(keys)}')
        time.sleep(PAUSE_SECONDS)

    # A tune that left the site is reported, not deleted: the file on disk is
    # still the only copy, and the build only uses what the catalogue lists.
    for key in set(manifest) - set(keys):
        manifest[key]['in_catalogue'] = False
    save()
    have = sum(1 for k in keys if (out / folder / f'{k}{ext}').exists())
    none = sum(1 for k in keys
               if manifest.get(k, {}).get('status') in ('none', 'no-midi'))
    log.info(f'{fmt}: {have}/{len(keys)} tunes have one; {none} have none on '
             f'the site; {len(failed)} failed this run (re-run to retry).')
    if failed:
        log.warning(f'{fmt} failed: {failed[:20]}')
    return have, none, failed


def download(parent_dir, refresh=False, formats=tuple(FORMATS)):
    out = pathlib.Path(parent_dir, 'data', 'spillefolk')
    out.mkdir(parents=True, exist_ok=True)
    sess = session()

    catalogue = fetch_catalogue(sess)
    (out / 'catalogue.json').write_text(
        json.dumps(catalogue, ensure_ascii=False, indent=1), encoding='utf-8')
    log.info(f'Catalogue: {len(catalogue)} tunes')

    keys = sorted(catalogue)
    failed = []
    for fmt in formats:
        failed += fetch_all(sess, out, keys, fmt, refresh)[2]
    return 1 if failed else 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('dir', help='Parent directory for the `data` directory')
    parser.add_argument('--refresh', action='store_true',
                        help='Re-fetch files already on disk, and re-check '
                             'tunes the site had no file for')
    parser.add_argument('--formats', default=','.join(FORMATS),
                        help=f'Comma-separated subset of {", ".join(FORMATS)}')
    args = parser.parse_args()
    raise SystemExit(download(args.dir, args.refresh,
                              tuple(args.formats.split(','))))
