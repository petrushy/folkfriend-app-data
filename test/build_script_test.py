import pathlib
import unittest


class BuildScriptTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        repo_root = pathlib.Path(__file__).resolve().parents[1]
        cls.build_script = repo_root / 'build' / 'build.sh'
        cls.lines = cls.build_script.read_text(encoding='utf-8').splitlines()

    def test_hash_is_computed_after_download(self):
        download_idx = next(
            i for i, line in enumerate(self.lines)
            if 'python src/download_thesession_data.py $SCRIPTPATH' in line
        )
        hash_idx = next(
            i for i, line in enumerate(self.lines)
            if 'sha1sum data/tunes.json data/aliases.json &> $NEW_HASH' in line
        )
        self.assertGreater(hash_idx, download_idx)

    def test_hash_uses_downloaded_thesession_inputs(self):
        hash_lines = [
            line for line in self.lines
            if 'sha1sum ' in line and '$NEW_HASH' in line
        ]
        self.assertEqual(
            hash_lines,
            ['sha1sum data/tunes.json data/aliases.json &> $NEW_HASH']
        )

    def test_build_runs_folkwiki_validation(self):
        self.assertTrue(
            any('python src/validate_output.py $SCRIPTPATH --manifest-path data/folkwiki/manifest.json --pageid-path data/folkwiki/hexhash_to_pageid.json' in line
                for line in self.lines)
        )


if __name__ == '__main__':
    unittest.main()
