"""Scrape norbeck.nu's tune listings for the (rhythm, ref) pairs that exist.

Why this is needed. Norbeck's per-tune pages are
`display.asp?rhythm=<rhythm>&ref=<n>`, and `<n>` is *usually* the number in the
tune's own `Z:id:hn-<rhythm>-<n>` line — measured at 90.6% agreement, confirmed
on both the ref and the tune title. But not always, and a wrong ref does not
404, it returns **HTTP 500**. Shipping ~9% of Norbeck tunes with a link to an
error page is worse than shipping a link to the right listing page.

So this builds the set of pairs the site actually serves, and
build_norbeck_data.py emits a deep link only when the pair is in it, falling
back to the rhythm's index page otherwise. Same role as
discover_folkwiki_pageids.py: a cached, occasionally-refreshed discovery
artifact, not part of every build.

Output: `data/norbeck/site_refs.json` — `{"<rhythm>": [ref, ref, ...]}`.

Note the listing pages require a `rhythm` parameter; `index2.asp?cat=i` with no
rhythm returns no tune links at all. Hence one request per rhythm.
"""

import argparse
import collections
import html
import json
import logging
import os
import pathlib
import re
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

import requests

logging.basicConfig(level=logging.DEBUG,
                    format='[%(name)s:%(lineno)s] %(message)s')
log = logging.getLogger(os.path.basename(__file__))

BASE_URL = 'https://www.norbeck.nu/abc/'
CATEGORIES = ('i', 's', 'm')
REQUEST_TIMEOUT = 40
WORKERS = 5

# Links look like: <a href="display.asp?rhythm=slip+jig&ref=12">Title</a>
_DISPLAY_RE = re.compile(
    r'href="display\.asp\?rhythm=([^&"]*)&(?:amp;)?ref=(\d+)"')
# The category page's own navigation names every rhythm it holds.
_RHYTHM_RE = re.compile(r'index2\.asp\?([^"\']*rhythm=[^"\']*)')


def rhythms_for_category(session, cat):
    url = f'{BASE_URL}index2.asp?cat={cat}'
    r = session.get(url, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    found = []
    for m in _RHYTHM_RE.finditer(r.text):
        params = urllib.parse.parse_qs(html.unescape(m.group(1)),
                                       keep_blank_values=True)
        rhythm = (params.get('rhythm') or [''])[0]
        if rhythm and rhythm not in found:
            found.append(rhythm)
    return found


def discover_norbeck_refs(parent_dir):
    norbeck_dir = os.path.join(parent_dir, 'data', 'norbeck')
    pathlib.Path(norbeck_dir).mkdir(parents=True, exist_ok=True)
    out_path = os.path.join(norbeck_dir, 'site_refs.json')

    session = requests.Session()
    session.headers['User-Agent'] = (
        'folkfriend-app-data build (https://github.com/petrush/folkfriend)'
    )

    jobs = []
    for cat in CATEGORIES:
        for rhythm in rhythms_for_category(session, cat):
            jobs.append((cat, rhythm))
    log.info(f'{len(jobs)} listing pages to fetch')

    refs = collections.defaultdict(set)

    def fetch(job):
        cat, rhythm = job
        url = (f'{BASE_URL}index2.asp?cat={cat}&rhythm='
               f'{urllib.parse.quote_plus(rhythm)}')
        try:
            r = session.get(url, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
        except Exception as e:
            log.warning(f'{cat}/{rhythm}: {e}')
            return rhythm, []
        # A listing page carries links for the whole category, not just its own
        # rhythm, so the rhythm token is taken from each href rather than from
        # the request.
        return rhythm, _DISPLAY_RE.findall(r.text)

    with ThreadPoolExecutor(WORKERS) as ex:
        for rhythm, rows in ex.map(fetch, jobs):
            if not rows:
                log.warning(f'No tune links found for rhythm {rhythm!r}')
            for href_rhythm, ref in rows:
                refs[urllib.parse.unquote_plus(href_rhythm)].add(ref)

    out = {k: sorted(v, key=int) for k, v in sorted(refs.items())}
    total = sum(len(v) for v in out.values())
    log.info(f'Discovered {total} (rhythm, ref) pairs across '
             f'{len(out)} rhythms')

    if total < 2000:
        log.error(
            f'Only {total} pairs found; the site layout has probably changed. '
            f'Refusing to overwrite {out_path} with a bad snapshot.'
        )
        return 1

    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    log.info(f'Wrote {out_path}')
    return 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Discover valid norbeck.nu display.asp (rhythm, ref) pairs')
    parser.add_argument(
        'dir', help='Parent directory for the `data` directory')
    args = parser.parse_args()
    raise SystemExit(discover_norbeck_refs(args.dir))
