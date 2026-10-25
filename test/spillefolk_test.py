"""The private spillefolk dataset: catalogue parsing, MIDI -> contour/ABC, IDs.

Every case here comes from a real tune met while building it (October 2026).
Nothing touches the network.
"""
import importlib.util
import os
import pathlib
import sys
import tempfile
import unittest


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


BUILD_SRC = pathlib.Path(__file__).resolve().parents[1] / 'build' / 'src'
sys.path.insert(0, str(BUILD_SRC))
import py_midicsv  # noqa: E402  (real, not stubbed: the MIDI tests need it)

dl = load('download_spillefolk_data', BUILD_SRC / 'download_spillefolk_data.py')
sm = load('spillefolk_midi', BUILD_SRC / 'spillefolk_midi.py')
build = load('build_spillefolk_data', BUILD_SRC / 'build_spillefolk_data.py')


LIST_PAGE = """<h1> Biografier </h1><a href=visnode.php?key=bi-bast title="x"  target="_blank"  ><font size=1>Brødrene Bast
</font></a>
 <font size=1>&nbsp;  (DK -  Lolland Amager) </font><br><h1> Polska, Pols </h1><a href=visnode.php?key=po-nr49 title="x" target="_blank" ><font size=1>Polska nr 49</font></a>
 <font size=1>&nbsp;e. Anders Leonard Carlsson  (S -  Bohuslän) </font><br><a href=visnode.php?key=po-utan title="x" target="_blank" ><font size=1>Polska utan land</font></a>
 <font size=1>&nbsp;   </font><br>"""


class CatalogueTest(unittest.TestCase):
    def test_list_page_rows_carry_country_regions_and_heading(self):
        tunes = dl.parse_list_page(LIST_PAGE)
        self.assertEqual(tunes['po-nr49'], {
            'title': 'Polska nr 49', 'sub': 'e. Anders Leonard Carlsson',
            'country': 'S', 'regions': 'Bohuslän',
            'dance_heading': 'Polska, Pols'})
        self.assertEqual(tunes['po-utan']['country'], '')
        self.assertIn('bi-bast', tunes)  # filtered later, by fetch_catalogue

    def test_biographies_are_not_tunes(self):
        self.assertIn('bi', dl.NOT_TUNES)


class AbcTest(unittest.TestCase):
    def test_key_is_named_from_the_last_note(self):
        # midi2abc -gk says "0 sharps"; a polska ending on D is D Dorian.
        self.assertEqual(sm.key_name(0, 62), 'Ddor')
        self.assertEqual(sm.key_name(1, 64), 'Em')     # 1# ending on E
        self.assertEqual(sm.key_name(2, 62), 'D')      # 2# ending on D
        self.assertEqual(sm.key_name(-1, 67), 'Gdor')  # 1b ending on G
        self.assertEqual(sm.key_name(0, 61), 'C')      # off-scale: major name
        self.assertEqual(sm.key_name(3, None), 'A')

    def test_chords_reduce_to_their_top_note(self):
        # Gånglåt efter Troskari Erik: five-note stacks on every beat.
        self.assertEqual(
            sm.top_notes_only('[G-GE-C-C,,-]2 [cG-E-C-C,,-] [dGECC,,]2|'),
            'G2- c d2|')
        self.assertEqual(sm.top_notes_only("[c'c]/2 [^F=c]"), "c'/2 =c")
        self.assertEqual(sm.top_notes_only('A2 B |: c :|'), 'A2 B |: c :|')

    def test_rest_only_bar_detection(self):
        self.assertTrue(sm._is_rest_only('z8'))
        self.assertTrue(sm._is_rest_only(' z2 z/2 '))
        self.assertFalse(sm._is_rest_only('z4 Ge-'))

    def test_bogus_meters_fall_back_by_dance(self):
        self.assertEqual(sm.METER_BY_DANCE['va'], '3/4')
        self.assertEqual(sm.METER_BY_DANCE['pk'], '2/4')


class MidiContourTest(unittest.TestCase):
    """A synthetic two-track file: melody first, a second voice below it."""

    def write_midi(self, rows):
        tmp = tempfile.NamedTemporaryFile(suffix='.mid', delete=False)
        tmp.close()
        with open(tmp.name, 'wb') as f:
            py_midicsv.FileWriter(f).write(py_midicsv.csv_to_midi(rows))
        self.addCleanup(os.unlink, tmp.name)
        return tmp.name

    def test_melody_track_only_top_note_and_velocity_zero_offs(self):
        q = 240  # a quaver at 480 ticks per quarter
        rows = ['0, 0, Header, 1, 3, 480',
                '1, 0, Start_track', '1, 0, End_track',
                '2, 0, Start_track',
                # melody: D E (double stop E+G on the second) F, with the
                # note-offs written as velocity-0 note-ons
                f'2, 0, Note_on_c, 0, 62, 80', f'2, {q}, Note_on_c, 0, 62, 0',
                f'2, {q}, Note_on_c, 0, 64, 80', f'2, {q}, Note_on_c, 0, 67, 80',
                f'2, {2*q}, Note_on_c, 0, 64, 0', f'2, {2*q}, Note_on_c, 0, 67, 0',
                f'2, {2*q}, Note_on_c, 0, 66, 80', f'2, {3*q}, Note_off_c, 0, 66, 0',
                f'2, {3*q}, End_track',
                '3, 0, Start_track',
                # second voice, lower and different — must be ignored
                f'3, 0, Note_on_c, 1, 50, 80', f'3, {3*q}, Note_off_c, 1, 50, 0',
                f'3, {3*q}, End_track',
                '0, 0, End_of_file']
        notes, division = sm.melody_notes(self.write_midi(rows))
        self.assertEqual(division, 480)
        self.assertEqual([n.pitch for n in notes], [62, 67, 66])
        self.assertEqual(len(sm.contour(self.write_midi(rows))), 3)


class DatasetTest(unittest.TestCase):
    def test_ids_are_stable_and_above_norbeck(self):
        norbeck_max_setting = 8_000_000_000 + 0xFFFFFFFF * 100 + 99
        tid = int(build.stable_id('po-nr49', build.SPILLEFOLK_TUNE_ID_BASE))
        sid = int(build.stable_id('po-nr49', build.SPILLEFOLK_SETTING_ID_BASE,
                                  build.SETTING_MULTIPLIER))
        self.assertEqual(str(tid), build.stable_id(
            'po-nr49', build.SPILLEFOLK_TUNE_ID_BASE))
        self.assertGreater(tid, norbeck_max_setting)
        self.assertGreater(sid, tid)
        self.assertLess(build.SPILLEFOLK_SETTING_ID_BASE + 0xFFFFFFFF * 100,
                        2 ** 53)
        # tune and setting ranges must not overlap each other
        self.assertLess(build.SPILLEFOLK_TUNE_ID_BASE + 0xFFFFFFFF,
                        build.SPILLEFOLK_SETTING_ID_BASE)

    def test_inverted_titles_gain_their_natural_order(self):
        self.assertEqual(build.titles_for({'title': 'Dalajärna, Skänklåt från'}),
                         ['Dalajärna, Skänklåt från', 'Skänklåt från Dalajärna'])
        self.assertEqual(build.titles_for({'title': 'Halv kæde, Den'}),
                         ['Halv kæde, Den', 'Den Halv kæde'])
        self.assertEqual(build.titles_for({'title': 'Polska, e. Pelle'}),
                         ['Polska, e. Pelle'])

    def test_origin_names_the_country(self):
        self.assertEqual(build.origin_for({'regions': 'Skåne', 'country': 'S'}),
                         'Skåne, Sverige')
        self.assertEqual(build.origin_for({'regions': '', 'country': ''}), '')

    def test_the_dataset_is_never_published(self):
        assemble = load('assemble_datasets', BUILD_SRC / 'assemble_datasets.py')
        spec = next(d for d in assemble.DATASETS if d['id'] == 'spillefolk')
        self.assertFalse(spec['published'])
        validate = load('validate_output', BUILD_SRC / 'validate_output.py')
        self.assertIn('spillefolk', validate.UNPUBLISHED)
        self.assertIn('spillefolk', validate.OPTIONAL)


if __name__ == '__main__':
    unittest.main()
