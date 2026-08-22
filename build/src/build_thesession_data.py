"""Build the thesession.org dataset file.

Was `build_non_user_data.py`, which also did the merge and wrote the single
combined output. Since the index is now published as one file per source, this
script owns thesession only and writes `data/thesession.json`; assembling the
published files is `assemble_datasets.py`.
"""

import argparse
import json
import logging
import os
import pathlib

from abc_common import contour_for_setting, gather_aliases

from tqdm.contrib.concurrent import process_map

logging.basicConfig(level=logging.DEBUG,
                    format='[%(name)s:%(lineno)s] %(message)s')
log = logging.getLogger(os.path.basename(__file__))


def build_thesession_data(parent_dir):

    # Set up paths and directories
    data_dir = os.path.join(parent_dir, 'data')
    tunes_path = os.path.join(data_dir, 'tunes.json')
    aliases_path = os.path.join(data_dir, 'aliases.json')
    output_path = os.path.join(data_dir, 'thesession.json')

    midis_dir = os.path.join(data_dir, 'midis')
    pathlib.Path(midis_dir).mkdir(parents=True, exist_ok=True)

    with open(tunes_path, 'r') as f:
        thesession_data = json.load(f)

    with open(aliases_path, 'r') as f:
        thesession_aliases = json.load(f)

    # We convert the ABC files to a form more directly usable by a search
    #   engine. ABC files contain non-trivial syntax that must be properly
    #   parsed and it is unsuitable to require this running on the edge
    #   before first use, so we do this once and distribute as part of the
    #   non-user data file.

    # But - we would still like the original ABC string as it is used to
    #   render the sheet music (and going back from our queryable
    #   representation into ABC is even harder than the reverse as that's
    #   a non-unique mapping). However there's some other information in the
    #   JSON file from thesession.org's GitHub that isn't useful for us,
    #   which we now get rid of.

    # One field we get rid of *for each setting* is the name, because it's
    #   identical for all settings of the same tune, so only needs stored
    #   once. We store this with the aliases, because that's where the names
    #   of tunes are kept.

    log.info('Gathering tune name aliases')
    gathered_aliases = gather_aliases(thesession_aliases, thesession_data)

    log.info('Creating cleaned version of input data file')
    cleaned_thesession_data = clean_thesession_data(thesession_data)

    multiprocessing_input = [(setting, midis_dir)
                             for setting in cleaned_thesession_data]

    # The heavy lifting is done here
    contours = process_map(
        generate_midi_contour,
        multiprocessing_input,
        desc='Converting ABC text to contour string',
        chunksize=8)

    # Convert settings/contours into one dictionary of setting_id: setting
    settings = {}

    for setting in cleaned_thesession_data:
        settings[setting['setting_id']] = setting

        # Key doesn't need to be also stored on value
        del settings[setting['setting_id']]['setting_id']

    # It's possible that a contour doesn't exist for some setting, but
    #   in that case we still want to keep the setting because it might
    #   be useful to have the sheet music even if it isn't queryable.
    empty_contours = 0
    for setting_id, contour in contours:
        settings[setting_id]['contour'] = contour
        if not contour:
            empty_contours += 1

    total = len(contours)
    log.info(
        f'thesession contours: {total - empty_contours}/{total} non-empty '
        f'({empty_contours} failed, '
        f'{100 * empty_contours / total:.1f}%)'
    )

    # Add empty origin field to all thesession settings for schema uniformity
    for s in settings.values():
        s['origin'] = ''

    output = {
        'settings': settings,
        'aliases': gathered_aliases,
    }

    log.info(f'Writing {output_path}')
    with open(output_path, 'w') as f:
        json.dump(output, f)

    log.info(f'Done. {len(settings)} thesession settings written.')


def clean_thesession_data(tune_data):
    # Convert types and discard redundant data
    for i, _ in enumerate(tune_data):
        del tune_data[i]['date']
        del tune_data[i]['username']
        del tune_data[i]['name']
        # "type" is a common programming keyword, causes issues later.
        tune_data[i]['dance'] = tune_data[i]['type']
        del tune_data[i]['type']

        # The keys are still stored as strings because that's all JSON can do.
        #   We don't bother converting the tune_id to an int so that it can be
        #   used as a key directly without worrying about parsing between int
        #   and string.

    return tune_data


def generate_midi_contour(args):
    """process_map worker. Module-level so it is picklable."""
    setting, midis_path = args
    midi_out_path = os.path.join(midis_path,
                                 f'{setting["setting_id"]}.midi')
    # thesession ABC is always in eighth notes; there is no L: field in the
    # upstream JSON to read.
    contour = contour_for_setting(
        abc_body=setting['abc'],
        meter=setting['meter'],
        mode=setting['mode'],
        note_len='1/8',
        midi_out_path=midi_out_path,
    )
    return setting['setting_id'], contour


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument(
        'dir', help='Parent directory for the `data` directory')
    args = parser.parse_args()
    build_thesession_data(args.dir)
