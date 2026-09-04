import json
from pathlib import Path
import subprocess
import tempfile
import unittest
import zipfile
import io
from lxml import etree
from adoc_dita.converter import ROOT, convert_files, convert_text, schema, PARSER
from adoc_dita.repository import compare, git, refs
from adoc_dita.report import file_diff, zip_report


class ConversionTests(unittest.TestCase):
    def test_three_types_and_explicit_selection(self):
        unknown = convert_text('= A title\n\nA paragraph.\n')
        self.assertEqual(unknown['status'], 'error')
        self.assertIn('Select Concept', unknown['diagnostics'][0]['message'])
        inferred = convert_text('= A title\n\nA paragraph.\n', filename='con-example.adoc')
        self.assertEqual(inferred['status'], 'ok', inferred)
        self.assertEqual(inferred['topic_type'], 'concept')

    def test_user_concept_and_ldap_task_structures(self):
        results = convert_files(ROOT / 'examples', ['con-role-based-access-control-in-rhdh.adoc', 'proc-share-a-secret-with-ldap.adoc'], attribute_files=['attributes.adoc'])
        for result in results:
            self.assertEqual(result['status'], 'ok', result['diagnostics'])
        concept = etree.fromstring(results[0]['xml'].encode(), PARSER())
        self.assertEqual(concept.tag, 'concept')
        self.assertIsNotNone(concept.find('shortdesc'))
        self.assertEqual(len(concept.findall('.//ol/li/ul/li')), 3)
        task = etree.fromstring(results[1]['xml'].encode(), PARSER())
        self.assertEqual(task.tag, 'task')
        self.assertEqual(len(task.findall('taskbody/steps/step')), 4)
        self.assertIsNotNone(task.find('.//step/info/dl'))
        self.assertIsNotNone(task.find('.//step/info/note[@type="warning"]'))
        self.assertIsNotNone(task.find('taskbody/result/ul'))
        self.assertIn('--from-file=./ldap_certs.pem', task.find('.//codeblock').text)
        link = task.find('.//prereq/ul/li/xref')
        self.assertEqual(link.get('scope'), 'external')
        self.assertTrue(link.get('href').startswith('https://'))

    def test_reference_matches_provided_vocabulary(self):
        sample = etree.parse(str(ROOT / 'examples/provided-reference.xml'), PARSER())
        self.assertTrue(schema('reference').validate(sample))
        result = convert_files(ROOT, ['examples/reference.adoc'])[0]
        self.assertEqual(result['status'], 'ok', result['diagnostics'])
        document = etree.fromstring(result['xml'].encode(), PARSER())
        for path in ['shortdesc', 'refbody/section', './/dl/dlentry/dt', './/dd', './/table/tgroup', './/codeblock', './/codeph', './/i']:
            self.assertTrue(document.findall(path), path)
        self.assertEqual(document.get('id'), sample.getroot().get('id'))
        code = document.find('.//codeblock').text
        self.assertEqual(json.loads(code)[0]['name'], 'role:default/guests')
        self.assertNotIn('<p>+</p>', result['xml'])

    def test_task_contains_steps_and_prerequisites(self):
        result = convert_files(ROOT, ['examples/procedure.adoc'])[0]
        self.assertEqual(result['status'], 'ok', result['diagnostics'])
        document = etree.fromstring(result['xml'].encode(), PARSER())
        self.assertEqual(document.tag, 'task')
        self.assertEqual(len(document.findall('.//steps/step')), 2)
        self.assertIsNotNone(document.find('.//prereq'))
        self.assertEqual(document.find('.//codeblock').text, 'npm install example-plugin')

    def test_stable_paste_ids_and_escaping(self):
        source = '= Stable\n\nA & B, <input>, *bold* and `code`.\n'
        one, two = convert_text(source, kind="concept"), convert_text(source, kind="concept")
        self.assertEqual(one['status'], 'ok')
        self.assertEqual(one['xml'], two['xml'])
        self.assertIn('&amp;', one['xml'])
        changed = convert_text(source.replace('A & B', 'B & C'), kind='concept')
        self.assertEqual(etree.fromstring(one['xml'].encode(), PARSER()).get('id'), etree.fromstring(changed['xml'].encode(), PARSER()).get('id'))

    def test_invalid_or_incomplete_input_is_not_success(self):
        for source in ['', 'No title\n', '= Broken\n\n{missing}\n', '= Broken\n\n++++\n<notdita/>\n++++\n']:
            result = convert_text(source, kind="concept")
            self.assertEqual(result['status'], 'error', result)
            self.assertIsNone(result['xml'])

    def test_local_and_remote_include_confinement(self):
        for include in ['/etc/passwd', '../outside.adoc', 'https://example.com/a.adoc']:
            result = convert_text('= Unsafe\n\ninclude::' + include + '[]\n', kind='concept')
            self.assertEqual(result['status'], 'error', result)

    def test_paste_with_local_attributes_file(self):
        with tempfile.TemporaryDirectory(prefix='attributes with spaces ') as folder:
            root = Path(folder)
            attributes = root / 'attributes.adoc'
            attributes.write_text(':_mod-docs-content-type: SNIPPET\n:product: File product\ninclude::extra.adoc[]\n')
            (root / 'extra.adoc').write_text('ifdef::enabled[]\n:config-file: app-config.yaml\nendif::[]\n')
            source = ':_mod-docs-content-type: CONCEPT\n\n[id="configuration_{context}"]\n= Configuration\n\n{product} uses `{config-file}`.\n'
            # A pasted filename may collide with a real file: no reads or writes to it.
            (root / 'document.adoc').write_text('Leave this file unchanged.\n')
            original = {p.name: p.read_bytes() for p in root.iterdir()}
            missing = convert_text(source, attribute_file=str(attributes), attributes={'enabled': ''})
            self.assertEqual(missing['status'], 'error')
            self.assertEqual(missing['missing_attributes'], ['context'])
            self.assertIsNone(missing['xml'])
            overrides = {'enabled': '', 'context': 'guide', 'product': 'Override product'}
            first = convert_text(source, attribute_file=str(attributes), attributes=overrides)
            self.assertEqual(first['status'], 'ok', first['diagnostics'])
            self.assertIn('id="configuration_guide"', first['xml'])
            self.assertIn('Override product uses <codeph>app-config.yaml</codeph>', first['xml'])
            self.assertEqual(first['dependencies'], ['attributes.adoc', 'extra.adoc'])
            self.assertEqual(first, convert_text(source, attribute_file=str(attributes), attributes=overrides))
            self.assertEqual(original, {p.name: p.read_bytes() for p in root.iterdir()})
            attributes.write_text(':product: Updated on disk\n')
            updated = convert_text('= Product\n\n{product}\n', attribute_file=str(attributes), kind='concept')
            self.assertIn('Updated on disk', updated['xml'])

    def test_local_attributes_path_and_include_errors(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for path, message in [('relative.adoc', 'absolute path'), (str(root / 'missing.adoc'), 'not found'), (str(root / 'file.txt'), 'ending in .adoc')]:
                with self.assertRaisesRegex(ValueError, message):
                    convert_text('= Test\n\nText.\n', attribute_file=path, kind='concept')
            attrs = root / 'attributes.adoc'
            for target in ['../outside.adoc', 'https://example.com/attributes.adoc']:
                attrs.write_text('include::' + target + '[]\n')
                result = convert_text('= Test\n\nText.\n', attribute_file=str(attrs), kind='concept')
                self.assertEqual(result['status'], 'error', result)
                self.assertIsNone(result['xml'])

    def test_native_includes_tags_lines_and_conditions(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root/'modules').mkdir(); (root/'snippets').mkdir()
            (root/'modules/main.adoc').write_text('= Includes\n\nifdef::enabled[]\ninclude::../snippets/test.adoc[tag=selected]\nendif::[]\n')
            (root/'snippets/test.adoc').write_text('Excluded.\n// tag::selected[]\nIncluded *text*.\n// end::selected[]\n')
            result = convert_files(root, ['modules/main.adoc'], attributes={'enabled': ''}, kind='concept')[0]
            self.assertEqual(result['status'], 'ok', result['diagnostics'])
            self.assertIn('Included <b>text</b>', result['xml']); self.assertNotIn('Excluded.', result['xml'])
            self.assertEqual(result['dependencies'], ['snippets/test.adoc'])
            (root/'modules/main.adoc').write_text('= Lines\n\ninclude::../snippets/test.adoc[lines=3]\n')
            result = convert_files(root, ['modules/main.adoc'], kind='concept')[0]
            self.assertEqual(result['status'], 'ok', result['diagnostics'])
            self.assertIn('Included <b>text</b>', result['xml'])
            (root/'modules/main.adoc').write_text('= Root relative\n\ninclude::snippets/test.adoc[tag=selected]\n')
            result = convert_files(root, ['modules/main.adoc'], kind='concept')[0]
            self.assertEqual(result['status'], 'ok', result['diagnostics'])
            self.assertIn('Included <b>text</b>', result['xml'])

    def test_code_whitespace_and_xml_characters(self):
        code = 'if (a < b && b > 0) {\n  return "hello";\n}'
        result = convert_text('= Code\n\n[source,javascript]\n----\n' + code + '\n----\n', kind='concept')
        self.assertEqual(result['status'], 'ok', result)
        node = etree.fromstring(result['xml'].encode(), PARSER()).find('.//codeblock')
        self.assertEqual(node.text, code)

    def test_continuation_artifact_warns_and_preserves(self):
        result = convert_text('= Plus\n\nA paragraph.\n\n+\n\nAnother paragraph.\n', kind='concept')
        self.assertEqual(result['status'], 'review', result)
        self.assertIn('<p>+</p>', result['xml'])

    def test_xref_xml_suffix(self):
        result = convert_text('= Links\n\nSee xref:other.adoc[Other topic].\n', kind='concept')
        self.assertIn('href="other.xml"', result['xml'])


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name)
        git(self.repo, 'init', '-b', 'main')
        git(self.repo, 'config', 'user.name', 'Test Writer')
        git(self.repo, 'config', 'user.email', 'test@example.invalid')
        self.write('artifacts/attributes.adoc', ':product: One\n')
        self.write('modules/changed.adoc', '= Changed\n\nOriginal text.\n')
        self.write('modules/dependent.adoc', '= Dependent\n\n{product}\n\ninclude::../snippets/shared.adoc[]\n')
        self.write('modules/deleted.adoc', '= Deleted\n\n' + '\n'.join('Remove this old procedure step '+str(i) for i in range(20)) + '\n')
        self.write('modules/rename.adoc', '[id="persistent"]\n= Renamed\n\nSame content.\n')
        self.write('modules/untouched.adoc', '= Untouched\n\nSame content.\n')
        self.write('snippets/shared.adoc', 'Snippet one.\n')
        self.commit('baseline'); git(self.repo, 'tag', 'v1')
        self.write('modules/changed.adoc', '= Changed\n\nUpdated text.\n')
        self.write('artifacts/attributes.adoc', ':product: Two\n')
        self.write('snippets/shared.adoc', 'Snippet two.\n')
        (self.repo/'modules/deleted.adoc').unlink()
        (self.repo/'modules/rename.adoc').rename(self.repo/'modules/renamed.adoc')
        self.write('modules/added.adoc', '= Added\n\n' + '\n'.join('Brand new reference entry '+str(i) for i in range(20)) + '\n')
        self.commit('target'); git(self.repo, 'tag', 'v2')

    def tearDown(self):
        self.temp.cleanup()

    def write(self, path, text):
        p = self.repo/path; p.parent.mkdir(parents=True, exist_ok=True)
        if path.startswith('modules/'):
            text = ':_mod-docs-content-type: CONCEPT\n\n' + text
        p.write_text(text)

    def commit(self, name):
        git(self.repo, 'add', '.'); git(self.repo, 'commit', '-m', name)

    def test_release_workflow_and_deterministic_bundle(self):
        before = git(self.repo, 'status', '--porcelain')
        report = compare(self.repo, 'v1', 'v2', patterns=['modules/*.adoc'])
        self.assertEqual(report['summary']['errors'], 0, report)
        by_change = {item['change']: item for item in report['files']}
        for change in ['added', 'deleted', 'renamed', 'modified', 'dependency']:
            self.assertIn(change, by_change)
        dependency = by_change['dependency']
        self.assertIn('Two', dependency['after']['xml'])
        self.assertIn('Snippet two.', dependency['after']['xml'])
        self.assertEqual(dependency['source_diff']['patch'], '')
        self.assertIn('snippets/shared.adoc', dependency['affected_dependencies'])
        self.assertNotIn('modules/untouched.adoc', [i['after_path'] for i in report['files']])
        self.assertEqual(by_change['renamed']['before']['xml'], by_change['renamed']['after']['xml'])
        self.assertEqual(before, git(self.repo, 'status', '--porcelain'))
        repeat = compare(self.repo, 'v1', 'v2', patterns=['modules/*.adoc'])
        self.assertEqual(report, repeat)
        self.assertEqual(zip_report(report), zip_report(repeat))
        with zipfile.ZipFile(io.BytesIO(zip_report(report))) as archive:
            self.assertIn('after/modules/added.xml', archive.namelist())
            self.assertIn('before/modules/deleted.xml', archive.namelist())
            self.assertIn('report.html', archive.namelist())

    def test_invalid_target_has_no_fake_deletion_diff(self):
        self.write('modules/changed.adoc', '= Broken\n\n{undefined}\n')
        self.commit('invalid')
        report = compare(self.repo, 'v2', 'HEAD', patterns=['modules/*.adoc'])
        item = next(i for i in report['files'] if i['after_path']=='modules/changed.adoc')
        self.assertEqual(item['after']['status'], 'error')
        self.assertTrue(item['xml_diff']['unavailable'])
        self.assertIsNone(item['xml_changed'])

    def test_empty_comparison_and_bad_refs(self):
        report = compare(self.repo, 'v1', 'v1')
        self.assertEqual(report['summary']['files'], 0)
        with self.assertRaises(ValueError):
            compare(self.repo, '--help', 'v2')
        self.assertIn('v1', refs(self.repo))

    def test_removed_title_is_an_error_not_a_deletion(self):
        self.write('modules/changed.adoc', 'The document title was removed.\n')
        self.commit('removed title')
        report = compare(self.repo, 'v2', 'HEAD', patterns=['modules/*.adoc'])
        item = next(i for i in report['files'] if i['after_path']=='modules/changed.adoc')
        self.assertEqual(item['change'], 'modified')
        self.assertEqual(item['after']['status'], 'error')
        self.assertTrue(item['xml_diff']['unavailable'])

    def test_source_and_xml_ranges(self):
        diff = file_diff('one\ntwo\n', 'one\nnew\ntwo\n', 'a.adoc', 'b.adoc')
        hunk = diff['hunks'][0]
        self.assertEqual(hunk['before']['count'], 0)
        self.assertIsNone(hunk['before']['start'])
        self.assertEqual(hunk['after']['start'], 2)
        self.assertEqual(hunk['after']['end'], 2)


if __name__ == '__main__':
    unittest.main()
