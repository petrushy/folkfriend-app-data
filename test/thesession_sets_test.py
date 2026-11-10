"""Set partners: which thesession tunes are played together."""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'build' / 'src'))
import thesession_sets as ts  # noqa: E402


def set_rows(*sets):
    rows = []
    for n, tunes in enumerate(sets):
        for order, tune in enumerate(tunes, 1):
            rows.append({'tuneset': str(n), 'settingorder': str(order),
                         'tune_id': str(tune)})
    return rows


def track_rows(*tracks):
    rows = []
    for n, tunes in enumerate(tracks):
        for number, tune in enumerate(tunes, 1):
            rows.append({'id': 'r', 'track': str(n), 'number': str(number),
                         'tune_id': '' if tune is None else str(tune)})
    return rows


KNOWN = [str(n) for n in range(1, 50)]


class SetPartnersTest(unittest.TestCase):
    def test_counts_sets_recordings_and_direction(self):
        out = ts.build_set_partners(
            set_rows([1, 2, 3], [2, 1], [1, 2]),
            track_rows([1, 2]),
            KNOWN)
        self.assertEqual(out['1'][0], ['2', 3, 1, 3])   # 1→2 in two sets + a track
        self.assertEqual(out['2'][0], ['1', 3, 1, 1])   # 2→1 once
        self.assertEqual(out['1'][1], ['3', 1, 0, 0])   # 3 not adjacent to 1
        self.assertEqual(out['2'][1], ['3', 1, 0, 1])

    def test_set_order_comes_from_settingorder_not_row_order(self):
        rows = set_rows([1, 2])
        rows.reverse()
        out = ts.build_set_partners(rows, [], KNOWN)
        self.assertEqual(out['1'][0][3], 1)
        self.assertEqual(out['2'][0][3], 0)

    def test_unlinked_tune_on_a_track_breaks_adjacency(self):
        out = ts.build_set_partners([], track_rows([1, None, 2]), KNOWN)
        self.assertEqual(out['1'], [['2', 0, 1, 0]])

    def test_only_published_tunes_on_either_side(self):
        out = ts.build_set_partners(set_rows([1, 999]), [], KNOWN)
        self.assertNotIn('999', out)
        self.assertNotIn('1', out)

    def test_long_lists_are_not_sets(self):
        out = ts.build_set_partners(set_rows(list(range(1, 20))), [], KNOWN)
        self.assertEqual(out, {})

    def test_eight_tunes_is_a_set_nine_is_not(self):
        self.assertIn('1', ts.build_set_partners(set_rows(list(range(1, 9))), [], KNOWN))
        self.assertEqual(ts.build_set_partners(set_rows(list(range(1, 10))), [], KNOWN), {})
        self.assertEqual(
            ts.build_set_partners([], track_rows(list(range(1, 10))), KNOWN), {},
            'the same limit applies to recording tracks')

    def test_ranked_capped_and_deterministic(self):
        sets = [[1, p] for p in range(2, 30)] + [[1, 7]] * 3
        out = ts.build_set_partners(set_rows(*sets), [], KNOWN, max_partners=5)
        self.assertEqual([e[0] for e in out['1']], ['7', '2', '3', '4', '5'])
        again = ts.build_set_partners(set_rows(*reversed(sets)), [], KNOWN,
                                      max_partners=5)
        self.assertEqual(out, again)

    def test_missing_sets_file_leaves_the_index_alone(self):
        payload = {'aliases': {'1': ['x']}, 'sets': {'stale': []}}
        self.assertEqual(ts.attach_set_partners(payload, '/nonexistent'), {})
        self.assertNotIn('sets', payload)


if __name__ == '__main__':
    unittest.main()
