import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from adoc_dita.converter import ROOT
from adoc_dita.repository import git


class CliEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name)
        git(self.repo, 'init', '-b', 'main')
        git(self.repo, 'config', 'user.name', 'CLI Test')
        git(self.repo, 'config', 'user.email', 'cli@example.invalid')
        git(self.repo, 'config', 'commit.gpgsign', 'false')
        (self.repo / 'artifacts').mkdir()
        (self.repo / 'modules').mkdir()
        (self.repo / 'artifacts/attributes.adoc').write_text(':product: Example product\n')
        self.source = ':_mod-docs-content-type: CONCEPT\n\n[id="overview_{context}"]\n= Overview\n\n[role="_abstract"]\nAbout {product}.\n'
        (self.repo / 'modules/con-overview.adoc').write_text(self.source)

    def tearDown(self):
        self.temp.cleanup()

    def run_cli(self, *args, input=None):
        return subprocess.run([str(ROOT / 'adoc-dita'), *map(str, args)], input=input,
                              text=True, capture_output=True, cwd=ROOT, timeout=90)

    def test_file_and_stdin_conversion_use_standalone_pipeline(self):
        attributes = self.repo / 'artifacts/attributes.adoc'
        output = self.repo / 'overview.xml'
        file_result = self.run_cli('convert', self.repo / 'modules/con-overview.adoc',
                                   '--attribute-file', attributes, '-o', output)
        self.assertEqual(file_result.returncode, 0, file_result.stderr)
        self.assertIn('<concept id="overview">', output.read_text())
        self.assertIn('About Example product.', output.read_text())

        stdin_result = self.run_cli('convert', '-', '--filename', 'con-overview.adoc',
                                    '--attribute-file', attributes, '--json', input=self.source)
        self.assertEqual(stdin_result.returncode, 0, stdin_result.stderr)
        payload = json.loads(stdin_result.stdout)
        self.assertEqual(payload['status'], 'ok')
        self.assertIn('<concept id="overview">', payload['xml'])

    def test_branch_comparison_writes_complete_report(self):
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-m', 'baseline')
        git(self.repo, 'tag', 'v1')
        module = self.repo / 'modules/con-overview.adoc'
        module.write_text(self.source.replace('About {product}.', 'Updated {product} overview.'))
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-m', 'updated')
        git(self.repo, 'tag', 'v2')
        output = self.repo / 'comparison-report'
        result = self.run_cli('compare', self.repo, '--base', 'v1', '--target', 'v2',
                              '--include', 'modules/*.adoc', '-a', 'context=standalone', '-o', output)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((output / 'report.html').is_file())
        report = json.loads((output / 'report.json').read_text())
        self.assertEqual(report['summary']['files'], 1)
        self.assertEqual(report['summary']['errors'], 0)
        self.assertTrue((output / 'after/modules/con-overview.xml').is_file())


if __name__ == '__main__':
    unittest.main()
