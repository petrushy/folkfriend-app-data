import importlib.util
import pathlib
import sys
import tempfile
import types
import unittest


def load_module(module_name, path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FolkwikiPipelineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        repo_root = pathlib.Path(__file__).resolve().parents[1]
        cls.repo_root = repo_root
        build_src = repo_root / 'build' / 'src'
        sys.path.insert(0, str(build_src))
        sys.modules.setdefault(
            'py_midicsv',
            types.SimpleNamespace(midi_to_csv=lambda *_args, **_kwargs: []),
        )
        # normalize_mode, split_abc_tunes, parse_abc_tune and
        # extract_primary_voice_body_lines moved to abc_common when the third
        # source arrived; they were never folkwiki-specific. The folkwiki
        # module re-exports nothing, so reach for them where they now live.
        cls.abc_common = load_module(
            'abc_common',
            build_src / 'abc_common.py',
        )
        cls.folkwiki = load_module(
            'build_folkwiki_data',
            build_src / 'build_folkwiki_data.py',
        )
        cls.validate_output = load_module(
            'validate_output',
            build_src / 'validate_output.py',
        )

    def test_normalize_mode_handles_common_special_values(self):
        normalize_mode = self.abc_common.normalize_mode
        self.assertEqual(normalize_mode('Am'), 'Aminor')
        self.assertEqual(normalize_mode('Ddor'), 'Ddorian')
        self.assertEqual(normalize_mode('HP'), 'Cmajor')
        self.assertEqual(normalize_mode('free'), 'Cmajor')

    def test_stable_folkwiki_id_is_deterministic_and_numeric(self):
        tune_id = self.folkwiki.stable_folkwiki_id('452219', 0, self.folkwiki.TUNE_ID_BASE)
        setting_id = self.folkwiki.stable_folkwiki_id('452219', 0, self.folkwiki.SETTING_ID_BASE)
        self.assertEqual(tune_id, self.folkwiki.stable_folkwiki_id('452219', 0, self.folkwiki.TUNE_ID_BASE))
        self.assertEqual(setting_id, self.folkwiki.stable_folkwiki_id('452219', 0, self.folkwiki.SETTING_ID_BASE))
        self.assertTrue(tune_id.isdigit())
        self.assertTrue(setting_id.isdigit())
        self.assertNotEqual(tune_id, setting_id)

    def test_stable_folkwiki_id_changes_with_tune_block_index(self):
        first = self.folkwiki.stable_folkwiki_id('452219', 0, self.folkwiki.TUNE_ID_BASE)
        second = self.folkwiki.stable_folkwiki_id('452219', 1, self.folkwiki.TUNE_ID_BASE)
        self.assertNotEqual(first, second)

    def test_extracts_multiple_titles_and_metadata(self):
        abc = '\n'.join([
            'X:1',
            'T:Primary Title',
            'T:Alias Title',
            'M:3/4',
            'L:1/16',
            'R:Polska',
            'O:Dalarna',
            'C:Trad.',
            'K:Am',
            'ABcd',
        ])
        parsed = self.abc_common.parse_abc_tune(abc)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed['titles'], ['Primary Title', 'Alias Title'])
        self.assertEqual(parsed['meter'], '3/4')
        self.assertEqual(parsed['note_len'], '1/16')
        self.assertEqual(parsed['dance'], 'polska')
        self.assertEqual(parsed['origin'], 'Dalarna')
        self.assertEqual(parsed['composer'], 'Trad.')
        self.assertEqual(parsed['mode'], 'Aminor')

    def test_header_directives_and_comments_do_not_end_the_header(self):
        # Klockarpolskan (folkwiki Musik/2964): `%%MIDI ratio` sat before M:,
        # was read as the first body line, and the tune was dropped.
        abc = '\n'.join([
            'X:1',
            '%%beginps',
            '/small1 { 0.8 dup scale }!',
            '%%endps',
            'T:Klockarpolskan efter Elsa Siljebo',
            '% a comment',
            '%%MIDI ratio 24 16',
            'M:3/4',
            'L:1/8',
            'K:Dm',
            'A2 G2 F2 |',
        ])
        parsed = self.abc_common.parse_abc_tune(abc)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed['titles'], ['Klockarpolskan efter Elsa Siljebo'])
        self.assertEqual(parsed['meter'], '3/4')
        self.assertEqual(parsed['mode'], 'Dminor')
        self.assertEqual(parsed['abc_body'], 'A2 G2 F2 |')

    MELODY = 'A2 G2 F2 | E2 D2 C2 | D2 E2 F2 | G2 A2 B2 | c2 B2 A2 | G6 |'

    def test_salvage_reads_headers_written_after_k(self):
        # Isbrytaren (2d26e2): K: first, then M:, L: and T: after it.
        abc = '\n'.join(['X:24', 'K:G', 'M:3/4', 'L:1/16',
                         'T:Isbrytaren', 'R:Slängpolska', self.MELODY])
        self.assertIsNone(self.abc_common.parse_abc_tune(abc))
        parsed = self.abc_common.parse_abc_tune(abc, lenient=True)
        self.assertEqual(parsed['titles'], ['Isbrytaren'])
        self.assertEqual(parsed['meter'], '3/4')
        self.assertEqual(parsed['note_len'], '1/16')
        self.assertEqual(parsed['dance'], 'slängpolska')
        self.assertEqual(parsed['abc_body'], self.MELODY)

    def test_salvage_skips_stray_text_before_k(self):
        # Wrapped N: continuation, `#Q:` and a T: typo, all seen in Folkwiki.
        abc = '\n'.join(['X:1', 'T:Han som haka opp se',
                         'N: Troligen en Knaft-Jonke bit,',
                         'och vandrat vidare till Burträsk.',
                         '#Q:1/4=150', 'T(SvL Värmland 177)',
                         'M:2/4', 'K:D', self.MELODY])
        self.assertIsNone(self.abc_common.parse_abc_tune(abc))
        parsed = self.abc_common.parse_abc_tune(abc, lenient=True)
        self.assertEqual(parsed['titles'], ['Han som haka opp se'])
        self.assertEqual(parsed['meter'], '2/4')
        self.assertEqual(parsed['abc_body'], self.MELODY)

    def test_salvage_meter_title_and_voice_fallbacks(self):
        abc = '\n'.join(['X:1', 'K:Gm', 'V:1', 'V:2',
                         '[M:6/8] ' + self.MELODY])
        parsed = self.abc_common.parse_abc_tune(
            abc, lenient=True, fallback_title='Från URL')
        self.assertEqual(parsed['titles'], ['Från URL'])
        self.assertEqual(parsed['meter'], '6/8')
        self.assertEqual(parsed['abc_body'], '[M:6/8] ' + self.MELODY)
        no_meter = abc.replace('[M:6/8] ', '')
        self.assertEqual(self.abc_common.parse_abc_tune(
            no_meter, lenient=True, fallback_title='x')['meter'], '4/4')

    def test_salvage_still_rejects_preambles_and_placeholders(self):
        lenient = dict(lenient=True, fallback_title='x')
        self.assertIsNone(self.abc_common.parse_abc_tune(
            'Copyright notice\nSome description', **lenient))
        # Kom till mig om kvälla (e55eb9): an unfilled template page.
        self.assertIsNone(self.abc_common.parse_abc_tune(
            'X:\nT:Kom till mig om kvälla\nR:Schottis\nS:\nB:\nEvelina Sand\n'
            'M:\nL:\nK:d\n|:abcd:|', **lenient))

    def test_salvage_never_changes_a_tune_that_parses(self):
        abc = '\n'.join(['X:1', 'T:Fine', 'M:3/4', 'K:D', 'L:1/16',
                         self.MELODY])
        self.assertEqual(self.abc_common.parse_abc_tune(abc),
                         self.abc_common.parse_abc_tune(abc, lenient=True))

    def test_title_from_manifest_url_decodes_latin1(self):
        self.assertEqual(
            self.folkwiki.title_from_manifest_url(
                'http://www.folkwiki.se/pub/cache/'
                '_Sl%E4ngpolska_efter_FutteriPetter_392126.abc', '392126'),
            'Slängpolska efter FutteriPetter')

    def test_parse_abc_tune_keeps_only_primary_voice(self):
        abc = '\n'.join([
            'X:1',
            'T:Two Voice Tune',
            'M:3/4',
            'L:1/8',
            'K:Dm',
            'V:1',
            'd2 e2 f2 |',
            'g2 a2 b2 |',
            'V:2',
            'D,2 A,2 D2 |',
            'A,2 D2 A,2 |',
        ])
        parsed = self.abc_common.parse_abc_tune(abc)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed['abc_body'], 'd2 e2 f2 |\ng2 a2 b2 |')

    def test_extract_primary_voice_body_lines_keeps_single_voice_input(self):
        lines = ['d2 e2 f2 |', 'g2 a2 b2 |']
        self.assertEqual(
            self.abc_common.extract_primary_voice_body_lines(lines),
            lines,
        )

    def test_validate_output_flags_missing_folkwiki_source_urls(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            data_dir = tmp_path / 'data'
            folkwiki_dir = data_dir / 'folkwiki'
            data_dir.mkdir()
            folkwiki_dir.mkdir()

            data = {
                'settings': {
                    '2000000': {
                        'tune_id': '1000000',
                        'meter': '4/4',
                        'mode': 'Gmajor',
                        'abc': 'ABcd',
                        'dance': 'polska',
                        'contour': 'abcd',
                        'origin': 'Dalarna',
                        'composer': 'Trad.',
                        'source_url': '',
                    },
                },
                'aliases': {
                    '1000000': ['test tune'],
                },
            }
            (data_dir / 'folkfriend-non-user-data.json').write_text(
                __import__('json').dumps(data),
                encoding='utf-8',
            )
            (data_dir / 'nud-meta.json').write_text(
                '{"v": 1, "date": "2026-04-17"}',
                encoding='utf-8',
            )
            (folkwiki_dir / 'manifest.json').write_text(
                '{"abc123": {"name": "test tune", "url": "http://example.com/test.abc"}}',
                encoding='utf-8',
            )
            (folkwiki_dir / 'hexhash_to_pageid.json').write_text(
                '{"abc123": 1}',
                encoding='utf-8',
            )
            (folkwiki_dir / 'abc123.abc').write_text('X:1\nT:test\nK:G\nABcd\n', encoding='utf-8')

            rc = self.validate_output.validate(
                str(tmp_path),
                manifest_path=str(folkwiki_dir / 'manifest.json'),
                pageid_path=str(folkwiki_dir / 'hexhash_to_pageid.json'),
                allow_missing_output=True,
            )
            self.assertEqual(rc, 1)


if __name__ == '__main__':
    unittest.main()
