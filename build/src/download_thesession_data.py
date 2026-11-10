import argparse
import os
import requests
import logging

logging.basicConfig(level=logging.DEBUG,
                    format='[%(name)s:%(lineno)s] %(message)s')
log = logging.getLogger(os.path.basename(__file__))


DATA_URL = 'https://raw.githubusercontent.com/adactio/TheSession-data/main/json/{}.json'
# Files too big for plain Git (sets.json, ~150 MB) are stored in Git LFS, and
# the raw URL answers with a 134-byte pointer instead of the data. The media
# host serves the real file for both LFS and ordinary files.
LFS_URL = 'https://media.githubusercontent.com/media/adactio/TheSession-data/main/json/{}.json'
LFS_POINTER = b'version https://git-lfs'


def download_data(parent_dir, data_type):
    data_file_path = os.path.join(parent_dir, 'data', data_type + '.json')
    tmp_path = data_file_path + '.part'

    for url in (DATA_URL.format(data_type), LFS_URL.format(data_type)):
        with requests.get(url, stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(tmp_path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    f.write(chunk)
        with open(tmp_path, 'rb') as f:
            if not f.read(len(LFS_POINTER)).startswith(LFS_POINTER):
                break
        log.info(f'{data_type}.json is in Git LFS; fetching the media copy')
    else:
        raise SystemExit(f'FATAL: could not fetch {data_type}.json')

    # Written to a temporary name and moved into place, so an interrupted
    # download never leaves a truncated file for the build to read.
    log.info(f'Writing {data_file_path}')
    os.replace(tmp_path, data_file_path)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument(
        'dir', help='Parent directory for the `data` directory')
    args = parser.parse_args()
    download_data(args.dir, 'tunes')
    download_data(args.dir, 'aliases')
    # Tune sets and recording tracks: which tunes are played together.
    download_data(args.dir, 'sets')
    download_data(args.dir, 'recordings')
