"""Ordering invariants of build.sh, checked at the text level.

These are cheap guards on a script that is only ever run by hand against live
data, where getting the order wrong deploys a broken dataset rather than
failing a test. The assertions are deliberately about *relative order* and
*presence*, not exact command strings, so ordinary edits to the script do not
break them — the previous version asserted on three literal lines and broke the
moment the script was touched.
"""

import pathlib
import re
import unittest

DATASETS = ('thesession', 'folkwiki', 'norbeck')


class BuildScriptTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        repo_root = pathlib.Path(__file__).resolve().parents[1]
        cls.script = (repo_root / 'build' / 'build.sh').read_text(
            encoding='utf-8')
        cls.lines = cls.script.splitlines()

    def index_of(self, pattern):
        rx = re.compile(pattern)
        for i, line in enumerate(self.lines):
            if rx.search(line):
                return i
        self.fail(f'build.sh has no line matching {pattern!r}')

    def test_every_dataset_is_downloaded_then_hashed(self):
        # Change detection must run on freshly downloaded inputs, or a build
        # skips itself on the strength of last run's files.
        for ds in DATASETS:
            download = self.index_of(rf'download_{ds}_data\.py')
            check = self.index_of(rf'check_changed {ds}\b')
            self.assertGreater(
                check, download,
                f'{ds}: change detection must come after its download')

    def test_change_detection_covers_every_dataset(self):
        # The original build.sh hashed only thesession's inputs, so a folkwiki
        # or norbeck update could never trigger a rebuild.
        for ds in DATASETS:
            self.index_of(rf'check_changed {ds}\b')

    def test_each_builder_is_gated_on_its_own_dataset(self):
        # Rebuilding an unchanged dataset bumps its version and forces every
        # client to re-download it — 35 MB in thesession's case.
        for ds in DATASETS:
            line = self.lines[self.index_of(rf'build_{ds}_data\.py')]
            self.assertIn(
                f'CHANGED[{ds}]', line,
                f'{ds}: builder must be gated on CHANGED[{ds}]')

    def test_assemble_runs_after_all_builders(self):
        assemble = self.index_of(r'assemble_datasets\.py')
        for ds in DATASETS:
            self.assertGreater(
                assemble, self.index_of(rf'build_{ds}_data\.py'),
                f'assemble_datasets must run after the {ds} builder')

    def test_validation_runs_before_publishing(self):
        # set -e plus this ordering is what stops a bad dataset reaching
        # public/ and then Firebase.
        validate = max(
            i for i, line in enumerate(self.lines)
            if 'validate_output.py' in line
        )
        publish = self.index_of(r'mv "data/\$f" \.\./public/')
        self.assertGreater(
            publish, validate,
            'output must not reach public/ before it is validated')

    def test_deploy_runs_last(self):
        publish = self.index_of(r'mv "data/\$f" \.\./public/')
        deploy = self.index_of(r'^firebase deploy')
        self.assertGreater(deploy, publish)

    def test_publishes_only_what_the_build_declared(self):
        # build.sh used to name each output file literally, which meant the
        # list could drift from what assemble_datasets actually produced — and
        # the way it would drift is by publishing something that must not be.
        # It now reads PUBLISHED_FILES.txt, which the build writes.
        self.index_of(r'PUBLISHED_FILES\.txt')
        publish = self.index_of(r'mv "data/\$f" \.\./public/')
        reads_list = self.index_of(r'< data/PUBLISHED_FILES\.txt')
        self.assertGreater(
            reads_list, publish - 3,
            'the publish loop must be driven by PUBLISHED_FILES.txt')

    def test_refuses_to_publish_an_unpublished_dataset(self):
        # Norbeck may not be made available for download. A guard in the script
        # is the last line of defence if the list is ever wrong.
        self.assertIn('norbeck.json', self.script,
                      'build.sh has no guard against publishing norbeck')
        guard = self.index_of(r'is not publishable')
        publish = self.index_of(r'mv "data/\$f" \.\./public/')
        self.assertGreater(guard, publish,
                           'the guard must run after files are moved')
        deploy = self.index_of(r'^firebase deploy')
        self.assertLess(guard, deploy,
                        'the guard must run before the deploy')


if __name__ == '__main__':
    unittest.main()
