"""Unit tests for the shared ABC helpers and the Norbeck-specific pipeline.

The escape-decoding tests carry the most weight here. Norbeck's collection is
7-bit ASCII and spells every accented letter as a TeX-style escape, so a
regression there does not crash anything — it silently stores `sl\\"angpolska`
and `g{\\aa}rdebyl{\\aa}ten` as tune names, which no user will ever type. That
failure is invisible unless something asserts on it.
"""

import importlib.util
import pathlib
import sys
import types
import unittest


def load_module(module_name, path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class AbcCommonTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        build_src = pathlib.Path(__file__).resolve().parents[1] / 'build' / 'src'
        sys.path.insert(0, str(build_src))
        sys.modules.setdefault(
            'py_midicsv',
            types.SimpleNamespace(midi_to_csv=lambda *a, **k: []),
        )
        cls.abc = load_module('abc_common', build_src / 'abc_common.py')

    # -- escapes ---------------------------------------------------------

    def test_decodes_accent_escapes(self):
        d = self.abc.decode_abc_escapes
        self.assertEqual(d(r'sl\"angpolska'), 'slängpolska')
        self.assertEqual(d(r'Jos\'e'), 'José')
        self.assertEqual(d(r'Se\'an \'O Riada'), 'Seán Ó Riada'.replace('Ó', 'Ó'))
        self.assertEqual(d(r'ma\~nana'), 'mañana')
        self.assertEqual(d(r'H\^otel'), 'Hôtel')

    def test_decodes_brace_ligature_form(self):
        # `{\aa}` is the dominant spelling in the Swedish titles — 796
        # occurrences. Handling `\aa` but not the braces leaves
        # `gärdebyl{å}ten`, which looks decoded and is not.
        d = self.abc.decode_abc_escapes
        self.assertEqual(d(r'G{\aa}rdebyl{\aa}ten'), 'Gårdebylåten')
        self.assertEqual(d(r'{\AA}kermans polska'), 'Åkermans polska')
        self.assertEqual(d(r'polska fr{\aa}n Sm{\aa}land'),
                         'polska från Småland')

    def test_leaves_unescaped_text_alone(self):
        d = self.abc.decode_abc_escapes
        for text in ('The Flogging Reel', '', 'already ä decoded'):
            self.assertEqual(d(text), text)

    def test_leaves_no_residue_on_real_titles(self):
        d = self.abc.decode_abc_escapes
        for raw in (r'sl\"angpolska efter Byss-Calle',
                    r'N\"ackens polska',
                    r'{\AA}r 1718',
                    r'Tabhair dom do l\'amh'):
            out = d(raw)
            self.assertNotIn('\\', out, raw)
            self.assertNotIn('{', out, raw)

    # -- unit note length ------------------------------------------------

    def test_reports_whether_note_length_was_explicit(self):
        # 45% of Norbeck tunes have no L:. Defaulting them to 1/8 is wrong for
        # every tune in 2/4 or 3/8 (290 of them), so callers must be able to
        # tell "the source said 1/8" from "we guessed 1/8".
        with_l = self.abc.parse_abc_tune(
            'X:1\nT:A\nM:2/4\nL:1/8\nK:D\nabc\n')
        without_l = self.abc.parse_abc_tune('X:1\nT:A\nM:2/4\nK:D\nabc\n')
        self.assertTrue(with_l['has_note_len'])
        self.assertEqual(with_l['note_len'], '1/8')
        self.assertFalse(without_l['has_note_len'])

    def test_contour_omits_l_when_note_length_is_unknown(self):
        # Passing None must leave L: out of the synthetic header entirely, so
        # abc2midi applies the ABC standard rule rather than our guess.
        captured = {}

        def fake_abc_to_midi(abc, path):
            captured['abc'] = abc

        original = self.abc.midi.abc_to_midi
        self.abc.midi.abc_to_midi = fake_abc_to_midi
        try:
            self.abc.contour_for_setting('abc', '2/4', 'Dmajor', None,
                                         '/nonexistent/x.midi')
            self.assertNotIn('L:', captured['abc'])
            self.abc.contour_for_setting('abc', '2/4', 'Dmajor', '1/16',
                                         '/nonexistent/x.midi')
            self.assertIn('L:1/16', captured['abc'])
        finally:
            self.abc.midi.abc_to_midi = original

    # -- ornament stripping ----------------------------------------------

    def test_strips_chords_annotations_and_grace_notes(self):
        strip = self.abc.strip_abc_ornaments
        self.assertEqual(strip('"Am"ABC'), 'ABC')
        self.assertEqual(strip('{g}e2'), 'e2')
        self.assertEqual(strip('[CEG]2'), 'C2')
        # Bar and repeat markers must survive.
        self.assertIn('[|', strip('AB[|CD'))
        self.assertIn('[1', strip('AB[1CD'))


class NorbeckPipelineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        build_src = pathlib.Path(__file__).resolve().parents[1] / 'build' / 'src'
        sys.path.insert(0, str(build_src))
        sys.modules.setdefault(
            'py_midicsv',
            types.SimpleNamespace(midi_to_csv=lambda *a, **k: []),
        )
        cls.nb = load_module('build_norbeck_data',
                             build_src / 'build_norbeck_data.py')
        cls.abc = load_module('abc_common', build_src / 'abc_common.py')

    def test_parses_zid(self):
        self.assertEqual(self.nb.parse_zid('hn-reel-1'), ('reel', '1'))
        self.assertEqual(self.nb.parse_zid('hn-slip jig-12'), ('slip jig', '12'))
        self.assertIsNone(self.nb.parse_zid(''))
        self.assertIsNone(self.nb.parse_zid(None))
        # Unsubstituted templates are not identifiers.
        self.assertIsNone(self.nb.parse_zid('hn-%R-%X'))

    def test_ids_are_stable_and_in_range(self):
        tune = self.nb.stable_norbeck_id('hn-reel-1',
                                         self.nb.NORBECK_TUNE_ID_BASE)
        setting = self.nb.stable_norbeck_id('hn-reel-1',
                                            self.nb.NORBECK_SETTING_ID_BASE)
        self.assertEqual(tune, self.nb.stable_norbeck_id(
            'hn-reel-1', self.nb.NORBECK_TUNE_ID_BASE))
        self.assertNotEqual(tune, setting)
        # Must clear folkwiki, whose IDs reach ~1.68e9 in both namespaces, and
        # stay under 2**53 so JS parseInt is exact.
        self.assertGreater(int(tune), 1_700_000_000)
        self.assertLess(int(setting), 2 ** 53)

    def test_different_keys_give_different_ids(self):
        a = self.nb.stable_norbeck_id('hn-reel-1',
                                      self.nb.NORBECK_TUNE_ID_BASE)
        b = self.nb.stable_norbeck_id('hn-reel-2',
                                      self.nb.NORBECK_TUNE_ID_BASE)
        c = self.nb.stable_norbeck_id('hn-reel-1#2',
                                      self.nb.NORBECK_TUNE_ID_BASE)
        self.assertEqual(len({a, b, c}), 3)

    def test_body_without_parts_is_one_setting(self):
        plain = 'ABCD|EFGA|'
        self.assertEqual(self.nb.split_body_sections(plain), [(None, plain)])

    def test_each_part_becomes_its_own_setting(self):
        # A P: section is a complete alternative rendering of the tune —
        # measured at 1.01x the length of the head — so it is a setting in
        # exactly the sense thesession uses, not a fragment to discard.
        body = 'ABCD|EFGA|\nP:variations\ndcba|gfed|\nP:more variations\nGGGG|AAAA|'
        self.assertEqual(
            self.nb.split_body_sections(body),
            [(None, 'ABCD|EFGA|'),
             ('variations', 'dcba|gfed|'),
             ('more variations', 'GGGG|AAAA|')])

    def test_a_body_starting_with_a_part_has_no_empty_head(self):
        # 12 tunes (songs, where every verse is a part) start with a P:. The
        # old truncate-at-first-P: gave them an EMPTY contour, so they could
        # not be found at all.
        body = 'P:first verse\nABCD|EFGA|\nP:remaining verses\ndcba|gfed|'
        sections = self.nb.split_body_sections(body)
        self.assertEqual(len(sections), 2)
        self.assertEqual(sections[0], ('first verse', 'ABCD|EFGA|'))
        for _, text in sections:
            self.assertTrue(self.nb.has_notes(text))

    def test_sections_with_no_notes_are_dropped(self):
        body = 'ABCD|EFGA|\nP:see the other version\n\nP:variations\ndcba|'
        self.assertEqual(
            self.nb.split_body_sections(body),
            [(None, 'ABCD|EFGA|'), ('variations', 'dcba|')])

    def test_section_ids_sort_after_their_head_and_stay_adjacent(self):
        # The Rust side orders a tune's settings by numeric id, so without the
        # packing a variation could be listed above the tune it varies.
        base = self.nb.NORBECK_SETTING_ID_BASE
        head = int(self.nb.stable_norbeck_id('hn-reel-1', base, 0))
        first = int(self.nb.stable_norbeck_id('hn-reel-1', base, 1))
        second = int(self.nb.stable_norbeck_id('hn-reel-1', base, 2))
        self.assertLess(head, first)
        self.assertLess(first, second)
        self.assertEqual(second - head, 2)
        # ...and another tune's settings never interleave with them.
        other = int(self.nb.stable_norbeck_id('hn-reel-2', base, 0))
        self.assertFalse(head < other < second)

    def test_too_many_sections_is_a_build_failure(self):
        with self.assertRaises(ValueError):
            self.nb.stable_norbeck_id(
                'hn-reel-1', self.nb.NORBECK_SETTING_ID_BASE,
                self.nb.SECTION_MULTIPLIER)

    def test_source_url_is_a_deep_link_when_the_ref_exists(self):
        url = self.nb.source_url_for(('reel', '1'), {('reel', '1')})
        self.assertEqual(
            url, 'https://www.norbeck.nu/abc/display.asp?rhythm=reel&ref=1')

    def test_source_url_falls_back_when_the_ref_is_unknown(self):
        # A wrong ref returns HTTP 500, not 404 — linking users at an error
        # page is worse than linking them at the right listing.
        url = self.nb.source_url_for(('reel', '9999'), {('reel', '1')})
        self.assertIn('index2.asp', url)
        self.assertIn('rhythm=reel', url)
        self.assertNotIn('9999', url)

    def test_source_url_maps_squashed_rhythm_tokens(self):
        # The Z:id token and the site's rhythm parameter disagree for several
        # families and the difference is not derivable.
        for zid_token, expected in (('sp', 'sl%C3%A4ngpolska'),
                                    ('slipjig', 'slip+jig'),
                                    ('hf', 'highland'),
                                    ('setdance', 'set+dance'),
                                    ('jp', 'polska+J')):
            url = self.nb.source_url_for((zid_token, '1'), None)
            self.assertIn(f'rhythm={expected}', url, zid_token)

    def test_source_url_is_never_empty(self):
        # The app cannot derive a Norbeck URL from a tune ID, so a missing one
        # is a dead chip in the UI.
        self.assertTrue(self.nb.source_url_for(None, None))
        self.assertTrue(self.nb.source_url_for(('nonsense', '1'), None))

    # --- ID stability across releases -----------------------------------

    def _keys_for(self, blocks):
        """Derive the ID key for each parsed block, in the given order.

        Mirrors the real build: a parse pass to find duplicated Z:ids, then an
        ID pass. The pre-pass is the whole point — without it the first of two
        duplicates keeps the bare key just for being first.
        """
        import collections

        def zid_of(parsed):
            raw = (parsed['extra']['Z'] or [''])[0]
            if raw.lower().startswith('id:'):
                raw = raw[3:]
            return self.nb.parse_zid(raw)

        pairs = [(parsed, zid_of(parsed)) for parsed in blocks]
        duplicates = self.nb.find_duplicate_zids(
            f'hn-{z[0]}-{z[1]}' if z else None for _, z in pairs)

        seen = collections.Counter()
        out = {}
        for parsed, zid_parts in pairs:
            key = self.nb.derive_key(parsed, zid_parts, seen, duplicates)
            # Index by content so the two runs can be compared regardless of
            # the order they were produced in.
            out[self.nb.content_key(parsed)] = key
        return out

    def _blocks(self, texts):
        parsed = []
        for text in texts:
            p = self.abc.parse_abc_tune(text, extra_fields=('Z', 'S'))
            if p is not None:
                parsed.append(p)
        return parsed

    def test_ids_survive_tunes_being_reordered(self):
        # Norbeck publishes a new zip every few months. If an ID depends on a
        # tune's POSITION, inserting one tune near the top of a file silently
        # repoints every later ID onto a different tune — and a user's
        # favourites follow it. This is the property that stops that.
        head = 'X:1\nT:%s\nR:reel\nZ:id:hn-reel-%d\nM:C|\nK:G\n%s\n'
        texts = [
            head % ('Alpha', 1, 'GABc defg|'),
            head % ('Beta', 2, 'ABcd efga|'),
            # No Z:id at all — the position-derived fallback case.
            'X:3\nT:Gamma\nR:reel\nM:C|\nK:D\nDEFG ABcd|\n',
            # An unsubstituted template, which is not an identifier either.
            'X:4\nT:Delta\nR:reel\nZ:id:hn-%R-%X\nM:C|\nK:D\ncdef gabc|\n',
        ]
        forward = self._keys_for(self._blocks(texts))
        reversed_ = self._keys_for(self._blocks(list(reversed(texts))))
        self.assertEqual(forward, reversed_,
                         'tune IDs changed when the file order changed')

        # ...and inserting a new tune must not disturb the existing ones.
        inserted = ['X:0\nT:Newcomer\nR:reel\nM:C|\nK:A\naaaa bbbb|\n'] + texts
        after = self._keys_for(self._blocks(inserted))
        for content, key in forward.items():
            self.assertEqual(after.get(content), key,
                             'an existing tune ID moved when a tune was '
                             'inserted before it')

    def test_duplicate_zids_are_separated_by_content_not_order(self):
        # Eight Z:ids are duplicated in the real collection. Disambiguating by
        # encounter order means the two tunes swap IDs if they ever swap places.
        dup = 'X:%d\nT:%s\nR:reel\nZ:id:hn-reel-9\nM:C|\nK:G\n%s\n'
        texts = [dup % (1, 'First', 'GABc defg|'),
                 dup % (2, 'Second', 'ABcd efga|')]
        forward = self._keys_for(self._blocks(texts))
        reversed_ = self._keys_for(self._blocks(list(reversed(texts))))
        self.assertEqual(len(set(forward.values())), 2, 'IDs must stay unique')
        self.assertEqual(forward, reversed_,
                         'duplicate-Z:id tunes swapped IDs when reordered')

    def test_byte_identical_tunes_still_get_unique_ids(self):
        # Content hashing cannot separate two identical blocks. They are the
        # same tune so it does not matter which wins, but the IDs must not
        # collide — the build asserts uniqueness and would fail.
        same = 'X:1\nT:Twin\nR:reel\nZ:id:hn-reel-9\nM:C|\nK:G\nGABc defg|\n'
        import collections
        seen = collections.Counter()
        keys = []
        for parsed in self._blocks([same, same]):
            keys.append(self.nb.derive_key(
                parsed, ('reel', '9'), seen, {'hn-reel-9'}))
        self.assertEqual(len(set(keys)), 2, f'IDs collided: {keys}')

    def test_content_key_ignores_where_the_tune_came_from(self):
        a = self.abc.parse_abc_tune(
            'X:1\nT:Same\nR:reel\nM:C|\nK:G\nGABc defg|\n')
        b = self.abc.parse_abc_tune(
            'X:57\nT:Same\nR:jig\nM:C|\nK:G\nGABc defg|\n')
        # X: number and R: differ; title, meter, mode and body do not.
        self.assertEqual(self.nb.content_key(a), self.nb.content_key(b))
        c = self.abc.parse_abc_tune(
            'X:1\nT:Same\nR:reel\nM:C|\nK:G\ncccc dddd|\n')
        self.assertNotEqual(self.nb.content_key(a), self.nb.content_key(c))

    def test_every_mapped_rhythm_has_a_category(self):
        # The index-page fallback needs cat=; without it the listing 404s.
        for zid_token, rhythm in self.nb.ZID_TO_SITE_RHYTHM.items():
            self.assertIn(rhythm, self.nb.SITE_CATEGORY, zid_token)


if __name__ == '__main__':
    unittest.main()
