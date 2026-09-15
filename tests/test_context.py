from pathlib import Path
import tempfile
import unittest

from adoc_dita.context import convert_in_repository, convert_standalone, infer_repository
from adoc_dita.repository import compare, git, snapshot


class RepositoryContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / '.git').mkdir()
        self.source = ':_mod-docs-content-type: CONCEPT\n\n[id="main_{context}"]\n= Main topic\n\n{product} and xref:target_{context}[Target].\n'
        self.write('artifacts/attributes.adoc', ':product: Shared product\n:enabled:\n')
        self.write('modules/main.adoc', self.source)
        self.write('modules/target.adoc', ':_mod-docs-content-type: CONCEPT\n\n[id="target_{context}"]\n= Target\n\nText.\n')
        self.write('assemblies/nested.adoc', ':saved-context: {context}\n:context: nested\n:product: Assembly product\n\nifdef::enabled[]\ninclude::modules/main.adoc[leveloffset=+1]\ninclude::modules/target.adoc[leveloffset=+1]\nendif::[]\n:context: {saved-context}\n')
        self.write('titles/first/master.adoc', 'include::artifacts/attributes.adoc[]\n:context: first\n\n= First guide\n\ninclude::assemblies/nested.adoc[]\n')

    def tearDown(self):
        self.temp.cleanup()

    def write(self, path, text):
        file = self.root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(text)

    def convert(self, source=None, **kwargs):
        return convert_in_repository(source or self.source, repository=self.root, **kwargs)

    def test_native_inherited_context_and_link_registry(self):
        before = {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob('*.adoc')}
        result = self.convert()
        self.assertEqual(result['status'], 'ok', result)
        self.assertEqual(result['diagnostics'], [])
        self.assertIn('id="main_nested"', result['xml'])
        self.assertIn('Assembly product', result['xml'])
        self.assertIn('href="target.xml#target_nested"', result['xml'])
        self.assertEqual(result['source_path'], 'modules/main.adoc')
        self.assertEqual(result['resolution']['parent'], 'assemblies/nested.adoc')
        self.assertEqual(result['resolution']['line'], 6)
        self.assertEqual(result, self.convert())
        self.assertEqual(before, {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob('*.adoc')})
        self.assertEqual(infer_repository(self.root / 'artifacts/attributes.adoc'), self.root)

    def test_multiple_guides_require_selection_and_use_real_values(self):
        self.write('titles/second/master.adoc', 'include::artifacts/attributes.adoc[]\n:context: second\n\n= Second guide\n\ninclude::modules/main.adoc[leveloffset=+1]\ninclude::modules/target.adoc[leveloffset=+1]\n')
        choices = self.convert()
        self.assertEqual(choices['status'], 'selection')
        self.assertIsNone(choices['xml'])
        self.assertEqual([c['context'] for c in choices['guide_choices']], ['nested', 'second'])
        selected = self.convert(profile=choices['guide_choices'][1]['key'], attribute_file=self.root / 'artifacts/attributes.adoc')
        self.assertEqual(selected['status'], 'ok', selected)
        self.assertEqual(selected['resolution']['line'], 6)
        self.assertIn('id="main_second"', selected['xml'])
        self.assertIn('Shared product', selected['xml'])
        self.assertIn('target.xml#target_second', selected['xml'])
        self.assertEqual(self.convert(profile='stale-choice')['status'], 'selection')

    def test_topic_attributes_and_explicit_overrides(self):
        source = self.source.replace('= Main topic\n', '= Main topic\n:product: Topic product\n')
        local = self.convert(source)
        self.assertIn('Topic product', local['xml'])
        explicit = self.convert(source, attributes={'product': 'Explicit product'})
        self.assertIn('Explicit product', explicit['xml'])
        self.assertNotIn('Topic product', explicit['xml'])
        # A shared file is loaded before the guide, so the assembly can override it.
        shared = self.convert(attribute_file=self.root / 'artifacts/attributes.adoc')
        self.assertIn('Assembly product', shared['xml'])

    def test_standalone_uses_unique_source_only_for_local_includes(self):
        source = ':_mod-docs-content-type: REFERENCE\n\n[id="preview_{context}"]\n= Preview\n\n[role="_abstract"]\n{product}.\n\ninclude::{docdir}/artifacts/preview.adoc[]\n'
        self.write('modules/preview.adoc', source)
        self.write('artifacts/preview.adoc', 'IMPORTANT: Included preview text.\n')
        result = convert_standalone(source, filename='document.adoc',
                                    attribute_file=self.root / 'artifacts/attributes.adoc')
        self.assertEqual(result['status'], 'ok', result)
        self.assertEqual(result['source_path'], 'modules/preview.adoc')
        self.assertEqual(result['source_match'], 'automatic')
        self.assertIn('id="preview"', result['xml'])
        self.assertIn('Included preview text.', result['xml'])
        self.assertIn('artifacts/preview.adoc', result['dependencies'])
        linked = convert_standalone(self.source, attribute_file=self.root / 'artifacts/attributes.adoc')
        self.assertEqual(linked['status'], 'ok', linked)
        self.assertIn('href="target.xml#target"', linked['xml'])
        self.assertEqual(linked['resolved_links'][0]['topic_id'], 'target')

    def test_basic_browser_has_no_project_or_guide_selector(self):
        html = (Path(__file__).parents[1] / 'adoc_dita/index.html').read_text()
        for removed in ['local-repository', 'repository-choices', 'source-choice', 'guide-choice',
                        'Repository source settings']:
            self.assertNotIn(removed, html)
        self.assertIn('No project or guide selection is required', html)

    def test_dynamic_include_discovers_an_additional_guide(self):
        self.write('guides/custom-name.adoc', 'include::artifacts/attributes.adoc[]\n:topic-directory: ../modules\n:context: dynamic\n\n= Dynamic guide\n\ninclude::{topic-directory}/main.adoc[leveloffset=+1]\ninclude::{topic-directory}/target.adoc[leveloffset=+1]\n')
        result = self.convert()
        self.assertEqual(result['status'], 'selection')
        choice = next(c for c in result['guide_choices'] if c['guide'] == 'guides/custom-name.adoc')
        self.assertEqual(choice['context'], 'dynamic')
        selected = self.convert(profile=choice['key'])
        self.assertEqual(selected['status'], 'ok', selected)
        self.assertIn('target.xml#target_dynamic', selected['xml'])

    def test_ambiguous_link_target_is_not_rewritten(self):
        self.write('modules/other.adoc', ':_mod-docs-content-type: CONCEPT\n\n[id="target_{context}"]\n= Other target\n\nOther text.\n')
        assembly = self.root / 'assemblies/nested.adoc'
        assembly.write_text(assembly.read_text().replace('endif::[]', 'include::modules/other.adoc[leveloffset=+1]\nendif::[]'))
        result = self.convert()
        self.assertEqual(result['status'], 'review', result)
        self.assertIn('href="#target_nested"', result['xml'])
        self.assertEqual(result['resolved_links'], [])
        assembly.write_text(assembly.read_text().replace('include::modules/other.adoc', 'include::modules/target.adoc'))
        repeated = self.convert()
        self.assertEqual(repeated['status'], 'review')
        self.assertEqual(repeated['resolved_links'], [])

    def test_inactive_include_and_unresolved_target_are_not_invented(self):
        self.write('artifacts/attributes.adoc', ':product: Shared product\n')
        inactive = self.convert()
        self.assertEqual(inactive['status'], 'selection')
        self.assertEqual(inactive['guide_choices'], [])
        self.write('artifacts/attributes.adoc', ':product: Shared product\n:enabled:\n')
        broken = self.convert(self.source.replace('target_{context}', 'nonexistent'))
        self.assertEqual(broken['status'], 'review')
        self.assertEqual(broken['resolved_links'], [])
        self.assertIn('href="#nonexistent"', broken['xml'])

    def test_duplicate_source_identity_and_confined_paths(self):
        self.write('modules/duplicate.adoc', self.source)
        result = self.convert()
        self.assertEqual(result['status'], 'selection')
        self.assertEqual(len(result['source_choices']), 2)
        self.assertEqual(self.convert(source_path='modules/main.adoc')['status'], 'ok')
        with self.assertRaisesRegex(ValueError, 'inside the repository'):
            self.convert(source_path='../outside.adoc')
        with self.assertRaisesRegex(ValueError, 'inside the repository'):
            self.convert(source_path='modules/main.adoc', guide='/etc/passwd')
        with self.assertRaisesRegex(ValueError, 'not found'):
            infer_repository(self.root / 'artifacts/missing.adoc')

    def test_generic_directory_layout_and_conditional_guide_patterns(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'books').mkdir()
            (root / 'chapters').mkdir()
            (root / 'settings').mkdir()
            source = '[id="intro_{context}"]\n= Introduction\n\n{guide-name}: {platform}.\n'
            (root / 'chapters/intro.adoc').write_text(source)
            (root / 'settings/common.adoc').write_text(':platform: local\n:title: Custom handbook title\n:guide-name: Custom content\n')
            (root / 'books/index.adoc').write_text('include::../settings/common.adoc[]\n\n= Custom handbook\n\nifeval::["{platform}" == "cloud"]\n:context: cloud-guide\nendif::[]\nifeval::["{platform}" == "local"]\n:context: local-guide\nendif::[]\ninclude::../chapters/intro.adoc[leveloffset=+1]\n')
            local = convert_in_repository(source, repository=root, kind='concept')
            self.assertEqual(local['status'], 'ok', local)
            self.assertEqual(local['resolution']['guide'], 'books/index.adoc')
            self.assertIn('id="intro_local-guide"', local['xml'])
            self.assertIn('<title>Introduction</title>', local['xml'])
            self.assertIn('Custom content: local.', local['xml'])
            cloud = convert_in_repository(source, repository=root, kind='concept', attributes={'platform': 'cloud'})
            self.assertEqual(cloud['status'], 'ok', cloud)
            self.assertIn('id="intro_cloud-guide"', cloud['xml'])

    def test_guide_comparison_uses_each_snapshot_and_preserves_include_aliases(self):
        git(self.root, 'init', '-b', 'main')
        git(self.root, 'config', 'user.name', 'Test Writer')
        git(self.root, 'config', 'user.email', 'test@example.invalid')
        git(self.root, 'config', 'commit.gpgsign', 'false')
        (self.root / 'content-alias').symlink_to('modules', target_is_directory=True)
        (self.root / 'external-alias').symlink_to('/etc', target_is_directory=True)
        assembly = self.root / 'assemblies/nested.adoc'
        assembly.write_text(assembly.read_text().replace('include::modules/', 'include::content-alias/'))
        git(self.root, 'add', '.'); git(self.root, 'commit', '-m', 'baseline')
        git(self.root, 'tag', 'before')
        assembly.write_text(assembly.read_text().replace(':context: nested', ':context: updated'))
        git(self.root, 'add', '.'); git(self.root, 'commit', '-m', 'updated guide')
        with tempfile.TemporaryDirectory() as folder:
            extracted = Path(folder)
            snapshot(self.root, 'HEAD', extracted)
            self.assertTrue((extracted / 'content-alias').is_symlink())
            self.assertFalse((extracted / 'external-alias').is_symlink())
        report = compare(self.root, 'before', 'HEAD', guide='titles/first/master.adoc', patterns=['modules/*.adoc'])
        self.assertEqual(report['summary']['errors'], 0, report)
        self.assertEqual(report['summary']['files'], 2)
        main = next(item for item in report['files'] if item['after_path'] == 'modules/main.adoc')
        self.assertEqual(main['source_diff']['patch'], '')
        self.assertIn('id="main_nested"', main['before']['xml'])
        self.assertIn('id="main_updated"', main['after']['xml'])
        self.assertIn('target.xml#target_updated', main['after']['xml'])
        self.assertEqual(report, compare(self.root, 'before', 'HEAD', guide='titles/first/master.adoc', patterns=['modules/*.adoc']))


if __name__ == '__main__':
    unittest.main()
