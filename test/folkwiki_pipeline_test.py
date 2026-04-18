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
        cls.folkwiki = load_module(
            'build_folkwiki_data',
            build_src / 'build_folkwiki_data.py',
        )
        cls.validate_output = load_module(
            'validate_output',
            build_src / 'validate_output.py',
        )

    def test_normalize_mode_handles_common_special_values(self):
        normalize_mode = self.folkwiki.normalize_mode
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
        parsed = self.folkwiki.parse_abc_tune(abc)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed['titles'], ['Primary Title', 'Alias Title'])
        self.assertEqual(parsed['meter'], '3/4')
        self.assertEqual(parsed['note_len'], '1/16')
        self.assertEqual(parsed['dance'], 'polska')
        self.assertEqual(parsed['origin'], 'Dalarna')
        self.assertEqual(parsed['composer'], 'Trad.')
        self.assertEqual(parsed['mode'], 'Aminor')

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
        parsed = self.folkwiki.parse_abc_tune(abc)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed['abc_body'], 'd2 e2 f2 |\ng2 a2 b2 |')

    def test_extract_primary_voice_body_lines_keeps_single_voice_input(self):
        lines = ['d2 e2 f2 |', 'g2 a2 b2 |']
        self.assertEqual(
            self.folkwiki.extract_primary_voice_body_lines(lines),
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
