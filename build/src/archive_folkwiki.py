"""Take a complete, verified snapshot of folkwiki.se's Musik group.

Unlike download_folkwiki_data.py (Wayback CDX discovery, files never
re-fetched once cached), this works from the wiki itself:

  1. `Musik/RecentChanges?action=source` lists every page in the group, with
     its last-modified time. That is the page list; nothing is guessed from an
     ID range.
  2. For each page, `?action=source` is the raw wiki text - the authoritative
     copy of the tune, exactly as its author wrote it. It is stored verbatim.
  3. For each page with music, the rendered HTML gives the names of its
     `pub/cache/{name}_{hexhash}.abc` files (the hexhash cannot be derived from
     the source), and each of those is downloaded. The build keys stable tune
     IDs on that hexhash, so unchanged tunes keep their IDs and favourites.
  4. Every cache file is checked against the page source: it must equal one of
     the page's (:music:) blocks, ignoring blank lines and the charset line.

The site is slow and flaky, so a failed request is never silently counted as
"missing". Empty source is only accepted as "page does not exist" once the
HTML page confirms it with a 404. Anything that could not be verified is
retried in further passes, and is listed in the report if it never succeeds.

The snapshot is written to build/data/folkwiki-snapshots/<date>/ and is
resumable: re-running with the same --date skips pages already verified.

Usage:
    python src/archive_folkwiki.py . [--date YYYY-MM-DD] [--workers 4]
"""

import argparse
import datetime
import hashlib
import json
import logging
import os
import pathlib
import re
import tarfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote, unquote

import requests

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s %(levelname)s %(message)s',
                    datefmt='%H:%M:%S')
log = logging.getLogger('archive_folkwiki')

SITE = 'http://www.folkwiki.se'
TIMEOUT = 45
TRIES_PER_PASS = 4
PASSES = 6
PASS_COOLDOWN = 60  # seconds between passes over what is still failing

RC_LINE_RE = re.compile(
    r'^\*\s*\[\[Musik[./]([^\]]+)\]\]\s*\.\s*\.\s*\.\s*'
    r'(\d{4}-\d\d-\d\d \d\d:\d\d)', re.M)
# A titled block is cached as `{title}_{6 hex}.abc`; a block with no T: as
# just `{32 hex}.abc`. The old CDX pipeline only matched the first form, so
# untitled blocks (typically variations printed under a tune) were never
# fetched. Both are archived; the key is the hex part either way.
ABC_HREF_RE = re.compile(
    r"""href=['"](https?://www\.folkwiki\.se/pub/cache/"""
    r"""(?:[^'"/]*?_([0-9a-f]{6})|([0-9a-f]{32}))\.abc)['"]""",
    re.I)
MUSIC_BLOCK_RE = re.compile(r'\(:music:\)(.*?)\(:musicend:\)', re.S)

# Pages in the group that are wiki furniture, not tunes.
SKIP_PAGES = {'RecentChanges', 'GroupHeader', 'GroupFooter'}

_local = threading.local()


class Unverified(Exception):
    """A response that arrived but cannot be trusted (wrong shape)."""


def session():
    if not hasattr(_local, 's'):
        s = requests.Session()
        s.headers['User-Agent'] = (
            'folkfriend-app-data archiver (+https://folkfriend-data.web.app)')
        _local.s = s
    return _local.s


def get(url, check):
    """GET with retries. `check(response)` returns the value to keep, or
    raises Unverified to retry. Returns (value, None) or (None, error)."""
    err = None
    for attempt in range(1, TRIES_PER_PASS + 1):
        try:
            r = session().get(url, timeout=TIMEOUT)
            return check(r), None
        except (requests.RequestException, Unverified) as e:
            err = f'{type(e).__name__}: {e}'
            time.sleep(2 ** attempt)
    return None, err


def normalise_abc(text):
    lines = [l.rstrip() for l in text.replace('\r\n', '\n').split('\n')]
    return '\n'.join(l for l in lines
                     if l.strip() and l.strip() != '%%abc-charset utf-8')


# Free-text header fields. The renderer rewrites wiki links inside these when
# it writes the cache file (`[[url|text]]` -> `text`, somewhat greedily), so a
# difference confined to them is expected and says nothing about the music.
# `%` comment lines and R: carry rewritten wiki links too (Musik/2010,
# Musik/FriaValet), and neither is music.
FREE_TEXT_FIELD_RE = re.compile(r'^(?:[BDHNSZOCAGFR]:|%)')


def music_only(normalised):
    return '\n'.join(l for l in normalised.split('\n')
                     if not FREE_TEXT_FIELD_RE.match(l.strip()))


def safe_name(page):
    return quote(page, safe='')


def check_source(r):
    if r.status_code != 200:
        raise Unverified(f'source HTTP {r.status_code}')
    head = r.content[:200].lstrip().lower()
    if head.startswith(b'<!doctype') or head.startswith(b'<html'):
        raise Unverified('source came back as HTML')
    return r.content


def check_html(r):
    if r.status_code == 404:
        return None  # confirmed: page does not exist
    if r.status_code != 200:
        raise Unverified(f'HTML HTTP {r.status_code}')
    text = r.content.decode('latin-1')
    if '<!--PageText-->' not in text:
        raise Unverified('HTML has no PmWiki page body')
    return text


def check_abc(r):
    if r.status_code != 200:
        raise Unverified(f'cache HTTP {r.status_code}')
    if r.content[:200].lstrip().lower().startswith((b'<!doctype', b'<html')):
        raise Unverified('cache file came back as HTML')
    # No X:/K: requirement: a (:music:) block quoting one bar inside a
    # discussion (Musik/77) is cached exactly as written. Whether the file is
    # right is decided by comparing it to the page source, not by its shape.
    if not r.content.strip():
        raise Unverified('cache file is empty')
    return r.content


def classify_match(cache_text, blocks):
    cached = normalise_abc(cache_text)
    if cached in blocks:
        return 'exact'
    if music_only(cached) in [music_only(b) for b in blocks]:
        return 'text-fields-only'
    return False


def reverify(snap, index):
    """Re-check every stored cache file against its page source on disk, so
    the report reflects the current rules rather than the ones in force when
    each file happened to be downloaded."""
    for page, entry in index.items():
        if entry.get('status') != 'ok' or not entry.get('abc'):
            continue
        source = (snap / 'pages' / f'{safe_name(page)}.txt').read_bytes()
        blocks = [normalise_abc(b) for b in
                  MUSIC_BLOCK_RE.findall(source.decode('utf-8', errors='replace'))]
        for link in entry['abc']:
            link.setdefault('file', link['url'].rsplit('/', 1)[-1])
            path = snap / 'cache' / link['file']
            text = path.read_bytes().decode('utf-8', errors='replace')
            link['matches_source'] = classify_match(text, blocks)


def page_body_is_empty(html):
    start = html.find("<div id='wikitext'>")
    end = html.find('<!--PageFooterFmt-->', start)
    if start < 0 or end < 0:
        return False
    body = re.sub(r'<[^>]*>', '', html[start:end])
    return not body.strip()


def archive_page(page, snap):
    """Fetch and verify one page. Returns its index entry."""
    entry = {'page': page, 'fetched_at': datetime.datetime.now(
        datetime.timezone.utc).isoformat(timespec='seconds')}
    url = f'{SITE}/Musik/{quote(page)}'

    source, err = get(f'{url}?action=source', check_source)
    if err:
        return {**entry, 'status': 'error', 'error': f'source: {err}'}

    has_music = b'(:music' in source
    html = None
    if has_music or not source.strip():
        html, err = get(url, check_html)
        if err:
            return {**entry, 'status': 'error', 'error': f'html: {err}'}

    if not source.strip():
        if html is None:
            return {**entry, 'status': 'absent'}
        # A page that exists with no text (Musik/1535, blanked in 2013) is
        # recorded as blank — but only when the rendered page agrees there is
        # nothing in it, so a source request that flaked is still retried.
        if page_body_is_empty(html):
            return {**entry, 'status': 'blank'}
        return {**entry, 'status': 'error',
                'error': 'empty source for a page that has content'}

    src_path = snap / 'pages' / f'{safe_name(page)}.txt'
    src_path.write_bytes(source)
    entry.update(status='ok', source_sha256=hashlib.sha256(source).hexdigest(),
                 source_bytes=len(source))

    try:
        source_text = source.decode('utf-8')
        entry['source_utf8'] = True
    except UnicodeDecodeError:
        source_text = source.decode('utf-8', errors='replace')
        entry['source_utf8'] = False
    blocks = [normalise_abc(b) for b in MUSIC_BLOCK_RE.findall(source_text)]
    entry['music_blocks'] = len(blocks)

    links = []
    seen = set()
    for href, short, full in ABC_HREF_RE.findall(html or ''):
        hexhash = (short or full).lower()
        if hexhash in seen:
            continue
        seen.add(hexhash)
        links.append({'hexhash': hexhash, 'url': href})
    entry['abc'] = links

    # The renderer writes one cache file per DISTINCT block — two identical
    # blocks share a file (Musik/1634 prints the same part twice). Fewer links
    # than that means the HTML was cut off or a file was not generated.
    distinct = len(set(blocks))
    if len(links) != distinct:
        return {**entry, 'status': 'error',
                'error': f'{distinct} distinct music blocks but '
                         f'{len(links)} cache links'}

    for link in links:
        # Stored under the site's own file name, NOT the hexhash: the six-hex
        # hash collides across different tunes (eb995b is both "Polonesse
        # Comp a Linblad", Musik/3837, and "Vals efter Kopparslagaren",
        # Musik/6060), and keying by hash handed one page the other's tune.
        link['file'] = link['url'].rsplit('/', 1)[-1]
        abc_path = snap / 'cache' / link['file']
        if abc_path.exists():
            data = abc_path.read_bytes()
        else:
            data, err = get(link['url'], check_abc)
            if err:
                return {**entry, 'status': 'error',
                        'error': f'cache {link["hexhash"]}: {err}'}
        try:
            text = data.decode('utf-8')
            link['utf8'] = True
        except UnicodeDecodeError:
            text = data.decode('utf-8', errors='replace')
            link['utf8'] = False
        link['matches_source'] = classify_match(text, blocks)
        link['sha256'] = hashlib.sha256(data).hexdigest()
        link['titled'] = len(link['hexhash']) == 6
        link['name'] = (unquote(link['url'].rsplit('/', 1)[-1],
                                encoding='latin-1')[:-4].rsplit('_', 1)[0]
                        if link['titled'] else '')
        abc_path.write_bytes(data)
    return entry


def load_page_list(snap):
    rc_path = snap / 'RecentChanges.txt'
    if not rc_path.exists():
        data, err = get(f'{SITE}/Musik/RecentChanges?action=source',
                        check_source)
        if err:
            raise SystemExit(f'Cannot fetch the page list: {err}')
        rc_path.write_bytes(data)
    text = rc_path.read_bytes().decode('utf-8', errors='replace')
    pages = {}
    for name, when in RC_LINE_RE.findall(text):
        name = name.strip()
        # Some RecentChanges lines carry the name double-encoded
        # (Ã¤ for ä); those are the same page as the correct spelling.
        try:
            fixed = name.encode('latin-1').decode('utf-8')
        except (UnicodeEncodeError, UnicodeDecodeError):
            fixed = name
        if fixed in SKIP_PAGES:
            continue
        pages.setdefault(fixed, when)  # first line is the newest
    return pages


def archive(parent_dir, date, workers):
    snap = pathlib.Path(parent_dir, 'data', 'folkwiki-snapshots', date)
    (snap / 'pages').mkdir(parents=True, exist_ok=True)
    (snap / 'cache').mkdir(parents=True, exist_ok=True)
    index_path = snap / 'index.json'
    index = json.loads(index_path.read_text()) if index_path.exists() else {}

    pages = load_page_list(snap)
    log.info(f'{len(pages)} pages listed in Musik/RecentChanges')

    lock = threading.Lock()

    def save():
        tmp = index_path.with_suffix('.tmp')
        tmp.write_text(json.dumps(index, ensure_ascii=False, indent=1))
        tmp.replace(index_path)

    for pass_no in range(1, PASSES + 1):
        todo = [p for p in pages
                if index.get(p, {}).get('status') not in ('ok', 'absent', 'blank')]
        if not todo:
            break
        if pass_no > 1:
            log.info(f'Pass {pass_no}: {len(todo)} pages still unverified; '
                     f'cooling down {PASS_COOLDOWN}s')
            time.sleep(PASS_COOLDOWN)
        else:
            log.info(f'Pass 1: {len(todo)} pages to fetch')
        done = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(archive_page, p, snap): p for p in todo}
            for fut in as_completed(futures):
                page = futures[fut]
                try:
                    entry = fut.result()
                except Exception as e:  # a bug, not the network
                    entry = {'page': page, 'status': 'error',
                             'error': f'crash: {type(e).__name__}: {e}'}
                entry['last_modified'] = pages[page]
                with lock:
                    index[page] = entry
                    done += 1
                    if entry['status'] == 'error':
                        log.warning(f'{page}: {entry["error"]}')
                    if done % 200 == 0 or done == len(todo):
                        save()
                        n_ok = sum(e['status'] == 'ok' for e in index.values())
                        log.info(f'pass {pass_no}: {done}/{len(todo)} '
                                 f'(verified ok overall: {n_ok})')
        save()

    reverify(snap, index)
    save()
    failed = report(snap, pages, index)
    if failed:
        log.error(f'{failed} pages could not be verified; NOT packing. '
                  'Re-run with the same --date to retry them.')
        raise SystemExit(1)
    pack(parent_dir, date)


def report(snap, pages, index):
    statuses = {}
    for p in pages:
        statuses.setdefault(index.get(p, {}).get('status', 'missing'), []).append(p)
    links = [l for e in index.values() for l in e.get('abc', [])]
    files = {l.get('file') or l['hexhash'] for l in links}
    lines = [
        f'Snapshot: {snap}',
        f'Pages listed:        {len(pages)}',
        f'  verified ok:       {len(statuses.get("ok", []))}',
        f'  confirmed absent:  {len(statuses.get("absent", []))}',
        f'  blank (no text):   {len(statuses.get("blank", []))}',
        f'  FAILED:            {len(statuses.get("error", [])) + len(statuses.get("missing", []))}',
        f'Pages with music:    {sum(1 for e in index.values() if e.get("music_blocks"))}',
        f'Music blocks:        {sum(e.get("music_blocks", 0) for e in index.values())}',
        f'Cache files:         {len(files)}',
        f'  identical to page source:        '
        f'{sum(1 for l in links if l.get("matches_source") == "exact")}',
        f'  differ only in free-text fields: '
        f'{sum(1 for l in links if l.get("matches_source") == "text-fields-only")}',
        f'  MUSIC DIFFERS from page source:  '
        f'{sum(1 for l in links if not l.get("matches_source"))}',
        f'  untitled blocks (32-hex names):  '
        f'{sum(1 for l in links if not l.get("titled"))}',
        f'  not valid UTF-8:   {sum(1 for l in links if not l.get("utf8"))}',
        f'Sources not UTF-8:   {sum(1 for e in index.values() if e.get("source_utf8") is False)}',
    ]
    for p in statuses.get('error', []) + statuses.get('missing', []):
        lines.append(f'  FAILED {p}: {index.get(p, {}).get("error", "never fetched")}')
    for l in links:
        if not l.get('matches_source'):
            lines.append(f'  MISMATCH {l.get("file", l["hexhash"])} {l["url"]}')
    text = '\n'.join(lines)
    (snap / 'REPORT.txt').write_text(text + '\n')
    print(text)
    return len(statuses.get('error', [])) + len(statuses.get('missing', []))


# ---------------------------------------------------------------------------
# The committed archive
#
# build/data/ is git-ignored, so a snapshot that only lives there exists on one
# machine. Each verified snapshot is packed into archive/folkwiki/<date>.tar.gz
# at the repo root (about 7 MB) and committed; the build unpacks the newest one,
# so a folkwiki.json can be rebuilt from the repository alone, and every past
# state of the site stays recoverable.
# ---------------------------------------------------------------------------

def archive_dir(parent_dir):
    return pathlib.Path(parent_dir).resolve().parent / 'archive' / 'folkwiki'


def snapshots_dir(parent_dir):
    return pathlib.Path(parent_dir, 'data', 'folkwiki-snapshots')


def pack(parent_dir, date):
    snap = snapshots_dir(parent_dir) / date
    out = archive_dir(parent_dir) / f'{date}.tar.gz'
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix('.tmp')
    with tarfile.open(tmp, 'w:gz') as tar:
        # Sorted, so the same snapshot always packs in the same order.
        for path in sorted(snap.rglob('*')):
            if path.is_file():
                tar.add(path, arcname=str(path.relative_to(snap.parent)))
    tmp.replace(out)
    log.info(f'Packed {snap} -> {out} ({out.stat().st_size / 1e6:.1f} MB)')
    return out


def unpack(parent_dir):
    """Make sure the newest committed snapshot is extracted. Returns its
    directory, or None when nothing has been archived yet."""
    archives = sorted(archive_dir(parent_dir).glob('????-??-??.tar.gz'))
    if not archives:
        log.error(f'No archived snapshot in {archive_dir(parent_dir)}')
        return None
    latest = archives[-1]
    date = latest.name[:-len('.tar.gz')]
    target = snapshots_dir(parent_dir) / date
    if (target / 'index.json').exists():
        log.info(f'Snapshot {date} already unpacked')
        return target
    snapshots_dir(parent_dir).mkdir(parents=True, exist_ok=True)
    with tarfile.open(latest, 'r:gz') as tar:
        tar.extractall(snapshots_dir(parent_dir), filter='data')
    log.info(f'Unpacked {latest} -> {target}')
    return target


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('dir', help='Parent directory for the `data` directory')
    parser.add_argument('--date', default=datetime.date.today().isoformat())
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument(
        '--unpack', action='store_true',
        help='Only extract the newest committed archive (no network)')
    parser.add_argument(
        '--pack', action='store_true',
        help='Only pack the snapshot for --date into archive/folkwiki/')
    args = parser.parse_args()
    if args.unpack:
        raise SystemExit(0 if unpack(args.dir) else 1)
    if args.pack:
        pack(args.dir, args.date)
    else:
        archive(args.dir, args.date, args.workers)
