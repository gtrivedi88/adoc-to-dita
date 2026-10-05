import io
import json
from pathlib import Path
import tempfile
import unittest

from adoc_dita.usage import UsageLogger, anonymous_user, summarize_usage, summarize_usage_file


class UsageTests(unittest.TestCase):
    def test_structured_events_and_summary(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'nested/usage.jsonl'
            stream = io.StringIO()
            logger = UsageLogger(path, stream)
            logger.record('convert', 'ok', 'browser-one', 12.9, xml_files=1, ignored='private')
            logger.record('convert', 'error', 'browser-one', -1, xml_files=0, diagnostics=2)
            logger.record('compare', 'complete', 'browser-two', 140, topics=3)

            events = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(events[0]['anonymous_user'], anonymous_user('browser-one'))
            self.assertNotIn('browser-one', path.read_text())
            self.assertNotIn('ignored', events[0])
            self.assertEqual(events[1]['duration_ms'], 0)
            self.assertEqual(stream.getvalue(), path.read_text())

            summary = summarize_usage_file(path)
            self.assertEqual(summary['uses'], 3)
            self.assertEqual(summary['anonymous_users'], 2)
            self.assertEqual(summary['workflows']['convert']['uses'], 2)
            self.assertEqual(summary['workflows']['convert']['anonymous_users'], 1)
            self.assertEqual(summary['workflows']['convert']['outcomes'], {'error': 1, 'ok': 1})
            self.assertEqual(summary['workflows']['pull_request']['uses'], 0)

    def test_summary_ignores_non_usage_log_lines(self):
        lines = ['AsciiDoc server started\n', '{bad json\n',
                 json.dumps({'event': 'something_else', 'workflow': 'convert'}) + '\n']
        self.assertEqual(summarize_usage(lines)['uses'], 0)

    def test_unwritable_optional_file_never_breaks_conversion_logging(self):
        with tempfile.TemporaryDirectory() as folder:
            stream = io.StringIO()
            logger = UsageLogger(folder, stream)  # A directory cannot be opened for append.
            event = logger.record('convert', 'ok', 'browser', 1, xml_files=1)
            self.assertEqual(event['outcome'], 'ok')
            self.assertEqual(json.loads(stream.getvalue())['workflow'], 'convert')


if __name__ == '__main__':
    unittest.main()
