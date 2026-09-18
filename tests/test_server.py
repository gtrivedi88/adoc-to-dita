import http.client
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import tempfile
import time
import unittest
import zipfile
import io

from adoc_dita.converter import ROOT


class ServerEndToEndTests(unittest.TestCase):
    def setUp(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            self.port = listener.getsockname()[1]
        env = dict(os.environ, ADOC_DITA_BIND_HOST='127.0.0.1',
                   ADOC_DITA_ALLOWED_HOSTS='converter.example.test')
        self.server = subprocess.Popen(
            [str(ROOT / 'adoc-dita'), 'serve', '--port', str(self.port)],
            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True,
        )
        for _ in range(80):
            try:
                status, _, body = self.request('GET', '/')
                if status == 200:
                    self.page = body.decode()
                    break
            except OSError:
                pass
            if self.server.poll() is not None:
                self.fail(self.server.stdout.read())
            time.sleep(0.05)
        else:
            self.fail('Server did not become ready')

    def tearDown(self):
        self.server.terminate()
        try:
            self.server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.server.kill()
            self.server.wait(timeout=5)
        self.server.stdout.close()

    def request(self, method, path, *, host=None, origin=None, token=None, data=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=30)
        headers = {}
        if host:
            headers['Host'] = host
        if origin:
            headers['Origin'] = origin
        if token:
            headers['X-App-Token'] = token
        body = None
        if data is not None:
            body = json.dumps(data).encode()
            headers['Content-Type'] = 'application/json'
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        content = response.read()
        connection.close()
        return response.status, response.headers, content

    def test_local_and_hosted_origins_and_conversion(self):
        self.assertIn('<title>AsciiDoc → DITA</title>', self.page)
        token = re.search(r'<script nonce="([^"]+)">', self.page).group(1)

        status, _, _ = self.request(
            'GET', '/', host='converter.example.test',
            origin='https://converter.example.test')
        self.assertEqual(status, 200)
        self.assertEqual(self.request(
            'GET', '/', host='converter.example.test',
            origin='https://untrusted.example')[0], 403)
        self.assertEqual(self.request('GET', '/', host='untrusted.example')[0], 403)

        source = ':_mod-docs-content-type: CONCEPT\n\n[id="hosted_{context}"]\n= Hosted\n\nRuns in a container.\n'
        payload = {'source': source, 'filename': 'con-hosted.adoc',
                   'attributes': '', 'type': 'auto'}
        self.assertEqual(self.request(
            'POST', '/api/convert', host='converter.example.test',
            origin='https://converter.example.test', data=payload)[0], 403)
        status, _, body = self.request(
            'POST', '/api/convert', host='converter.example.test',
            origin='https://converter.example.test', token=token, data=payload)
        result = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(result['status'], 'ok')
        self.assertIn('<concept id="hosted">', result['xml'])

    def test_hosted_comparison_accepts_an_uploaded_attributes_file(self):
        token = re.search(r'<script nonce="([^"]+)">', self.page).group(1)
        with tempfile.TemporaryDirectory() as folder:
            repository = Path(folder)
            def git(*arguments):
                subprocess.run(['git', '-C', str(repository), *arguments], check=True,
                               capture_output=True, text=True)
            git('init', '-b', 'main')
            git('config', 'user.name', 'Test Writer')
            git('config', 'user.email', 'test@example.invalid')
            topic = repository / 'modules/topic.adoc'
            topic.parent.mkdir()
            topic.write_text(':_mod-docs-content-type: CONCEPT\n\n= Topic\n\n{product}: before.\n')
            git('add', '.'); git('commit', '-m', 'baseline'); git('tag', 'v1')
            topic.write_text(':_mod-docs-content-type: CONCEPT\n\n= Topic\n\n{product}: after.\n')
            git('add', '.'); git('commit', '-m', 'target'); git('tag', 'v2')

            payload = {
                'repository': str(repository), 'base': 'v1', 'target': 'v2',
                'patterns': [], 'attributes': '',
                'attribute_files': None, 'attribute_text': ':product: Uploaded product\n',
                'attribute_filename': 'attributes.adoc', 'type': 'auto', 'guide': '',
            }
            def run_comparison(options):
                status, _, body = self.request(
                    'POST', '/api/compare', host='converter.example.test',
                    origin='https://converter.example.test', token=token, data=options)
                self.assertEqual(status, 202, body)
                job_id = json.loads(body)['id']
                for _ in range(200):
                    status, _, body = self.request(
                        'GET', '/api/job?id=' + job_id, host='converter.example.test',
                        origin='https://converter.example.test')
                    job = json.loads(body)
                    if job['status'] != 'running':
                        break
                    time.sleep(0.05)
                self.assertEqual(status, 200)
                self.assertEqual(job['status'], 'complete', job)
                return job_id, job

            job_id, job = run_comparison(payload)
            item = job['report']['files'][0]
            self.assertIn('Uploaded product', item['before']['xml'])
            self.assertIn('Uploaded product', item['after']['xml'])
            self.assertNotIn(payload['attribute_text'], json.dumps(job['report']))
            self.assertTrue(any('Converting' in event for event in job['timeline']))
            self.assertEqual(job['timeline'][-1], 'Comparison complete. ZIP ready to download.')

            status, headers, body = self.request(
                'GET', '/api/download?id=' + job_id, host='converter.example.test',
                origin='https://converter.example.test')
            self.assertEqual(status, 200)
            self.assertEqual(headers['Content-Type'], 'application/zip')
            with zipfile.ZipFile(io.BytesIO(body)) as archive:
                self.assertIn('after/modules/topic.xml', archive.namelist())

            local_attributes = repository / 'local-attributes.adoc'
            local_attributes.write_text(':product: Absolute path product\n')
            path_payload = dict(payload, attribute_files=[str(local_attributes)],
                                attribute_text='', attribute_filename='')
            _, path_job = run_comparison(path_payload)
            path_item = path_job['report']['files'][0]
            self.assertIn('Absolute path product', path_item['before']['xml'])
            self.assertIn('Absolute path product', path_item['after']['xml'])


if __name__ == '__main__':
    unittest.main()
