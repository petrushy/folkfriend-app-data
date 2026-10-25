"""Provide the folkwiki input for a build: the newest committed snapshot.

Folkwiki used to be "downloaded" by discovering .abc cache URLs through the
Wayback Machine's CDX index and fetching each one live, never re-fetching a
file once cached. That missed pages the crawler had not seen (everything
past Musik/6412), kept ~1,600 superseded versions of edited tunes as if they
were separate tunes, never fetched untitled blocks, and could not tell a
deleted page from a failed request.

The site is now captured by archive_folkwiki.py into a verified snapshot,
committed as archive/folkwiki/<date>.tar.gz. A routine build only unpacks
the newest of those — it never touches the network, so a slow or flaky
folkwiki.se cannot break or silently shrink a build. To take a new snapshot:

    python src/archive_folkwiki.py .

Usage:
    python src/download_folkwiki_data.py .
"""

import argparse
import sys

from archive_folkwiki import unpack

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('dir', help='Parent directory for the `data` directory')
    args = parser.parse_args()
    sys.exit(0 if unpack(args.dir) else 1)
