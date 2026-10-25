"""The folkwiki snapshot: archiving the site, and building the index from it.

Every case here is something the first full snapshot (2026-10-25) actually
ran into. A synthetic snapshot is written to a temp directory, so nothing
touches the network.
"""
import importlib.util
import json
import os
import pathlib
import stat
import sys
import tempfile
import types
import unittest


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


MELODY = 'A2 G2 F2 | E2 D2 C2 | D2 E2 F2 | G2 A2 B2 | c2 B2 A2 | G6 |'


def cache_text(title, extra=''):
    # Exactly how the site writes a cache file: charset line, blank, tune.
    return (f'%%abc-charset utf-8\n\nX:1\nT:{title}\nM:3/4\nL:1/8\nK:G\n'
            f'{extra}{MELODY}\n')


class SnapshotTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        build_src = pathlib.Path(__file__).resolve().parents[1] / 'build' / 'src'
        sys.path.insert(0, str(build_src))
        sys.modules.setdefault(
            'py_midicsv',
            types.SimpleNamespace(midi_to_csv=lambda *_a, **_k: []))
        cls.archive = load_module('archive_folkwiki',
                                  build_src / 'archive_folkwiki.py')
        cls.build = load_module('build_folkwiki_data',
                                build_src / 'build_folkwiki_data.py')
        cls.validate = load_module('validate_output',
                                   build_src / 'validate_output.py')
        cls.midi = load_module('midi', build_src / 'midi.py')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    # --- a synthetic snapshot ----------------------------------------------

    def make_snapshot(self, pages):
        """pages: {page: (source_text, [(url, cache_text)])}"""
        snap = self.root / 'build' / 'data' / 'folkwiki-snapshots' / '2026-10-25'
        (snap / 'pages').mkdir(parents=True)
        (snap / 'cache').mkdir()
        index = {}
        for page, (source, files) in pages.items():
            (snap / 'pages' / f'{self.archive.safe_name(page)}.txt').write_text(
                source, encoding='utf-8')
            blocks = [self.archive.normalise_abc(b)
                      for b in self.archive.MUSIC_BLOCK_RE.findall(source)]
            links = []
            for url, text in files:
                m = self.archive.ABC_HREF_RE.search(f"href='{url}'")
                hexhash = (m.group(2) or m.group(3)).lower()
                file = url.rsplit('/', 1)[-1]
                (snap / 'cache' / file).write_text(text, encoding='utf-8')
                links.append({
                    'hexhash': hexhash, 'url': url, 'file': file,
                    'titled': len(hexhash) == 6,
                    'matches_source': self.archive.classify_match(text, blocks),
                })
            index[page] = {'page': page, 'status': 'ok', 'abc': links,
                           'music_blocks': len(blocks)}
        (snap / 'index.json').write_text(json.dumps(index), encoding='utf-8')
        return snap

    @staticmethod
    def page(*blocks):
        return ''.join(f'(:music:)\n{b}\n(:musicend:)\n\nText.\n'
                       for b in blocks)

    @staticmethod
    def block(cache):
        return cache.split('\n\n', 1)[1].strip()

    # --- archiving -----------------------------------------------------------

    def test_cache_links_match_both_titled_and_untitled_names(self):
        html = ("href='http://www.folkwiki.se/pub/cache/Polska_6bf360.abc' "
                "href='http://www.folkwiki.se/pub/cache/"
                "9b2f0b7a5be812193f0e949a855974c7.abc'")
        found = self.archive.ABC_HREF_RE.findall(html)
        self.assertEqual([(s or f) for _, s, f in found],
                         ['6bf360', '9b2f0b7a5be812193f0e949a855974c7'])

    def test_rewritten_links_in_text_lines_are_not_a_music_difference(self):
        source = 'X:1\nT:a\nB:[[https://x.se/1|FMK M189]]\n% see [[https://x|y]]\nK:D\nAB|'
        cache = 'X:1\nT:a\nB:FMK M189\n% see y\nK:D\nAB|'
        blocks = [self.archive.normalise_abc(source)]
        self.assertEqual(self.archive.classify_match(cache, blocks),
                         'text-fields-only')
        self.assertFalse(self.archive.classify_match(
            cache.replace('AB|', 'AC|'), blocks))

    def test_a_blank_page_is_recognised_only_from_an_empty_body(self):
        empty = ("<div id='wikitext'>\n<div class='vspace'></div>"
                 "<div class='fpltemplate'></div>\n</div><!--PageFooterFmt-->")
        full = "<div id='wikitext'><p>Polska</p></div><!--PageFooterFmt-->"
        self.assertTrue(self.archive.page_body_is_empty(empty))
        self.assertFalse(self.archive.page_body_is_empty(full))
        self.assertFalse(self.archive.page_body_is_empty('<html>cut off'))

    def test_double_encoded_names_in_the_page_list_are_the_same_page(self):
        snap = self.root / 'snap'
        snap.mkdir()
        (snap / 'RecentChanges.txt').write_text(
            '* [[Musik/Gärdebylåten]]  . . . 2026-10-01 10:00 av ?: [==]\n'
            '* [[Musik/GÃ¤rdebylÃ¥ten]]  . . . 2020-01-01 10:00 av ?: [==]\n'
            '* [[Musik/RecentChanges]]  . . . 2020-01-01 10:00 av ?: [==]\n'
            '* [[Musik/2964]]  . . . 2019-01-01 10:00 av ?: [==]\n',
            encoding='utf-8')
        pages = self.archive.load_page_list(snap)
        self.assertEqual(pages, {'Gärdebylåten': '2026-10-01 10:00',
                                 '2964': '2019-01-01 10:00'})

    def test_pack_and_unpack_round_trip(self):
        snap = self.make_snapshot({'1': (self.page(self.block(cache_text('a'))),
            [('http://www.folkwiki.se/pub/cache/a_000001.abc', cache_text('a'))])})
        build_dir = self.root / 'build'
        out = self.archive.pack(build_dir, '2026-10-25')
        self.assertEqual(out.resolve(), (self.root / 'archive' / 'folkwiki'
                                         / '2026-10-25.tar.gz').resolve())
        original = {p.relative_to(snap): p.read_bytes()
                    for p in snap.rglob('*') if p.is_file()}
        for p in sorted(snap.rglob('*'), reverse=True):
            p.unlink() if p.is_file() else p.rmdir()
        restored = self.archive.unpack(build_dir)
        self.assertEqual(restored.resolve(), snap.resolve())
        self.assertEqual({p.relative_to(snap): p.read_bytes()
                          for p in snap.rglob('*') if p.is_file()}, original)

    # --- building from a snapshot -------------------------------------------

    def build_entries(self, snap):
        return list(self.build.iter_snapshot_files(snap))

    def test_a_shared_hash_gives_two_distinct_stable_ids(self):
        # eb995b is both a polonaise (Musik/3837) and a waltz (Musik/6060).
        pol, vals = cache_text('Polonesse'), cache_text('Vals')
        snap = self.make_snapshot({
            '6060': (self.page(self.block(vals)), [(
                'http://www.folkwiki.se/pub/cache/Vals_eb995b.abc', vals)]),
            '3837': (self.page(self.block(pol)), [(
                'http://www.folkwiki.se/pub/cache/Polonesse_eb995b.abc', pol)]),
        })
        entries = self.build_entries(snap)
        by_url = {e['source_url']: e for e in entries}
        first = by_url['http://www.folkwiki.se/Musik/3837']
        second = by_url['http://www.folkwiki.se/Musik/6060']
        # The lower page keeps the plain IDs a hash-keyed build published.
        self.assertEqual(first['block_offset'], 0)
        self.assertEqual(second['block_offset'],
                         self.build.COLLISION_BLOCK_OFFSET)
        self.assertIn('Polonesse', first['text'])
        self.assertIn('Vals', second['text'])

    def test_a_cache_file_damaged_by_wiki_markup_is_replaced_by_the_source(self):
        # PmWiki rewrites `$[K:F]` when it writes the cache (Musik/6448).
        source_block = self.block(cache_text('Brita', extra='AB $[K:F] cd |\n'))
        damaged = cache_text('Brita', extra='AB K:F cd |\n')
        snap = self.make_snapshot({'6448': (self.page(source_block), [(
            'http://www.folkwiki.se/pub/cache/Brita_e755df.abc', damaged)])})
        [entry] = self.build_entries(snap)
        self.assertIn('$[K:F]', entry['text'])
        # Wrapped as the cache wraps it, so the block index (and ID) holds.
        self.assertEqual(
            [b.splitlines()[0] for b in self.build.split_abc_tunes(entry['text'])],
            ['%%abc-charset utf-8', 'X:1'])

    def test_untitled_blocks_are_archived_but_not_indexed(self):
        titled = cache_text('Polska')
        untitled = '%%abc-charset utf-8\n\nX:1\nM:3/4\nK:G\nAB|\n'
        snap = self.make_snapshot({'1': (
            self.page(self.block(titled), self.block(untitled)), [
                ('http://www.folkwiki.se/pub/cache/Polska_6bf360.abc', titled),
                ('http://www.folkwiki.se/pub/cache/'
                 '9b2f0b7a5be812193f0e949a855974c7.abc', untitled)])})
        self.assertEqual([e['hexhash'] for e in self.build_entries(snap)],
                         ['6bf360'])

    def test_a_named_page_links_to_its_wiki_page(self):
        text = cache_text('Gärdebylåten')
        snap = self.make_snapshot({'Gärdebylåten': (self.page(self.block(text)), [
            ('http://www.folkwiki.se/pub/cache/G%E4rdebyl%E5ten_fe0a17.abc', text)])})
        [entry] = self.build_entries(snap)
        self.assertEqual(entry['source_url'],
                         'http://www.folkwiki.se/Musik/G%C3%A4rdebyl%C3%A5ten')
        self.assertEqual(entry['fallback_title'], 'Gärdebylåten')

    def test_the_newest_snapshot_is_used(self):
        root = self.root / 'build' / 'data' / 'folkwiki-snapshots'
        for d in ('2026-01-01', '2026-10-25', 'not-a-date'):
            (root / d).mkdir(parents=True)
        self.assertEqual(self.build.latest_snapshot(self.root / 'build'),
                         root / '2026-10-25')

    # --- validation ------------------------------------------------------------

    def test_validation_rejects_an_incomplete_snapshot(self):
        text = cache_text('a')
        snap = self.make_snapshot({'1': (self.page(self.block(text)), [
            ('http://www.folkwiki.se/pub/cache/a_000001.abc', text)])})
        index = json.loads((snap / 'index.json').read_text())
        index['2'] = {'page': '2', 'status': 'error', 'error': 'timeout'}
        (snap / 'index.json').write_text(json.dumps(index))
        (snap / 'cache' / 'a_000001.abc').unlink()
        errors = []
        self.validate.validate_folkwiki_snapshot(str(snap), errors)
        joined = ' '.join(errors)
        self.assertIn('unverified', joined)
        self.assertIn('missing 1 cache files', joined)
        self.assertIn('truncated', joined)

    # --- abc2midi --------------------------------------------------------------

    def test_abc2midi_is_retried_when_it_crashes_without_output(self):
        # abc2midi segfaults on some tunes at random (Musik/2164: about one
        # run in ten succeeds) and writes nothing when it does.
        work = self.root / 'work'
        work.mkdir()
        counter = work / 'runs'
        fake = work / 'abc2midi'
        fake.write_text(
            '#!/bin/sh\n'
            f'n=$(cat {counter} 2>/dev/null || echo 0); n=$((n+1)); echo $n > {counter}\n'
            'out=""; while [ $# -gt 0 ]; do [ "$1" = "-o" ] && out="$2"; shift; done\n'
            '[ $n -ge 3 ] && echo MIDI > "$out"\n'
            '[ $n -ge 3 ] || exit 139\n')
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        cwd = os.getcwd()
        os.chdir(work)
        try:
            self.midi.abc_to_midi('X:1\nK:D\nAB|', str(work / 'out.mid'))
        finally:
            os.chdir(cwd)
        self.assertTrue((work / 'out.mid').exists())
        self.assertEqual(counter.read_text().strip(), '3')


if __name__ == '__main__':
    unittest.main()
