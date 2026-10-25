"""The thesession build's MIDI cache.

The cache was keyed on setting id alone. A setting edited on thesession keeps
its id, so the build reused the MIDI of its old notes and the edited tune kept
its old contour for good (285 settings in the October 2026 update).
"""
import importlib.util
import pathlib
import sys
import types
import unittest


class MidiCacheNameTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        build_src = pathlib.Path(__file__).resolve().parents[1] / 'build' / 'src'
        sys.path.insert(0, str(build_src))
        sys.modules.setdefault(
            'py_midicsv', types.SimpleNamespace(midi_to_csv=lambda *a, **k: []))
        spec = importlib.util.spec_from_file_location(
            'build_thesession_data', build_src / 'build_thesession_data.py')
        cls.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.mod)

    SETTING = {'setting_id': 51419, 'abc': '|:A2A2 AAAA|', 'meter': '4/4',
               'mode': 'Amixolydian'}

    def test_same_content_reuses_the_cached_midi(self):
        name = self.mod.midi_cache_name(self.SETTING)
        self.assertEqual(name, self.mod.midi_cache_name(dict(self.SETTING)))
        self.assertTrue(name.startswith('51419-') and name.endswith('.midi'))

    def test_an_edit_to_anything_the_contour_reads_misses_the_cache(self):
        base = self.mod.midi_cache_name(self.SETTING)
        for field, value in (('abc', '|:A2A2 AAAB|'), ('meter', '2/2'),
                             ('mode', 'Adorian')):
            self.assertNotEqual(
                base, self.mod.midi_cache_name({**self.SETTING, field: value}),
                f'editing {field} must not reuse the old MIDI')

    def test_unrelated_fields_do_not_invalidate(self):
        self.assertEqual(
            self.mod.midi_cache_name(self.SETTING),
            self.mod.midi_cache_name({**self.SETTING, 'name': 'Renamed',
                                      'date': '2026-10-25'}))


if __name__ == '__main__':
    unittest.main()
