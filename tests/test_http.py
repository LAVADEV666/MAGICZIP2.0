import io
import json
import re
import sys
import threading
import time
import unittest
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from urllib.parse import quote
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import Application, Handler
from pypdf import PdfWriter


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.server.app = Application()
        cls.server.app.destination = cls.server.app.session / 'results'
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = 'http://127.0.0.1:' + str(cls.server.server_port)
        cls.token = cls.server.app.token

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def request(self, path, data=None, token=True, headers=None):
        hdr = {'X-Magic-Token': self.token} if token else {}
        hdr.update(headers or {})
        if isinstance(data, dict):
            data = json.dumps(data).encode()
            hdr['Content-Type'] = 'application/json'
        return urlopen(Request(self.url + path, data=data, headers=hdr), timeout=15)

    def test_auth_origin_and_path_boundaries(self):
        for path in ['/api/state', '/api/file/missing', '/api/thumbnail/missing']:
            with self.assertRaises(HTTPError) as caught:
                self.request(path, token=False)
            self.assertEqual(caught.exception.code, 403)
        with self.assertRaises(HTTPError) as caught:
            self.request('/api/clear', {}, headers={'Origin': 'https://example.com'})
        self.assertEqual(caught.exception.code, 403)
        with self.assertRaises(HTTPError):
            self.request('/../app.py')
        html = self.request('/').read().decode()
        self.assertIn(self.token, html)
        self.assertNotIn('__SESSION_TOKEN__', html)

    def test_pdf_upload_process_preview_report_and_reuse(self):
        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        stream = io.BytesIO(); writer.write(stream)
        source = json.load(self.request('/api/upload?name=Prueba%20%C3%B1.pdf', stream.getvalue()))
        self.assertEqual(source['pages'], 1)
        job = json.load(self.request('/api/jobs', {'action':'compress', 'ids':[source['id']], 'options':{'limit_bytes':4_000_000}}))
        for _ in range(150):
            result = json.load(self.request('/api/job/' + job['id']))
            if result['status'] != 'running': break
            time.sleep(.02)
        self.assertEqual(result['status'], 'completed')
        output = result['result']['outputs'][0]
        thumbnail = self.request('/api/thumbnail/' + output['id']).read()
        self.assertTrue(thumbnail.startswith(b'\x89PNG'))
        downloaded = self.request('/api/file/' + output['id'] + '?download=1')
        self.assertEqual(len(downloaded.read()), output['bytes'])
        self.assertIn('attachment', downloaded.headers['Content-Disposition'])
        report = json.load(self.request('/api/file/' + result['result']['report_id']))
        self.assertFalse(report['originals_modified'])
        reused = json.load(self.request('/api/use-result', {'job':job['id']}))
        self.assertEqual(len(reused['ids']), 1)
        self.assertTrue(any(f['relative'].startswith('MagicPDF/') for f in reused['state']['sources']))
        state = json.load(self.request('/api/state'))
        self.assertTrue(any(j.get('result') for j in state['jobs']))

    def test_active_content_downloaded_not_executed(self):
        source = json.load(self.request('/api/upload?name=prueba.html', b'<script>alert(1)</script>'))
        response = self.request('/api/file/' + source['id'])
        self.assertIn('attachment', response.headers['Content-Disposition'])
        self.assertIn('sandbox', response.headers['Content-Security-Policy'])

    def test_output_destination_requires_existing_absolute_path(self):
        for bad in ('relative-folder', 'C:\\MagicZIP-folder-does-not-exist-7ec5678c'):
            with self.assertRaises(HTTPError):
                self.request('/api/destination', {'path':bad})
        folder = self.server.app.session / 'chosen-output'
        folder.mkdir(exist_ok=True)
        state = json.load(self.request('/api/destination', {'path':str(folder)}))
        self.assertEqual(state['destination'], str(folder.resolve()))

    def test_smart_plan_and_prepared_pdf_are_available_through_api(self):
        from pypdf.generic import DecodedStreamObject, NameObject
        writer=PdfWriter(); page=writer.add_blank_page(width=200,height=200)
        content=DecodedStreamObject(); content.set_data(b'q Q\n'*50_000)
        page[NameObject('/Contents')]=writer._add_object(content)
        stream=io.BytesIO(); writer.write(stream)
        source=json.load(self.request('/api/upload?name=Carpeta/automatico.pdf',stream.getvalue()))
        plan=json.load(self.request('/api/plan',{'ids':[source['id']],'options':{'limit_bytes':40_000}}))
        self.assertTrue(plan['estimate_only'])
        self.assertEqual(plan['automatic_pdfs'],1)
        self.assertEqual(plan['folders'][0]['name'],'Carpeta')
        job=json.load(self.request('/api/jobs',{'action':'zip','ids':[source['id']],'options':{'limit_bytes':40_000}}))
        for _ in range(150):
            result=json.load(self.request('/api/job/'+job['id']))
            if result['status']!='running': break
            time.sleep(.02)
        self.assertEqual(result['status'],'completed')
        prepared=result['result']['preparation'][0]['parts'][0]
        self.assertTrue(self.request('/api/thumbnail/'+prepared['id']).read().startswith(b'\x89PNG'))
        self.assertEqual(result['statistics']['zips_verified'],1)
        self.assertTrue(any('Verificado:' in e['text'] for e in result['events']))

    def test_browser_folder_groups_match_native_first_level_folders(self):
        source=json.load(self.request('/api/upload?name=Expediente/Bancos/Anexo.txt',b'demo'))
        self.assertEqual(source['collection'],'Expediente/Bancos')
        plan=json.load(self.request('/api/plan',{'ids':[source['id']]}))
        self.assertEqual(plan['folders'][0]['name'],'Expediente/Bancos')

    def test_background_inventory_api_remains_responsive_and_cancellable(self):
        entered = threading.Event()
        original_ids = set(self.server.app.sources)
        folder = self.server.app.session / 'background-input'
        folder.mkdir(exist_ok=True)
        def scan(path, excluded, *, inventory_only, check, progress):
            entered.set()
            progress(1, 42, str(path / 'archivo.pdf'))
            while True:
                check()
                time.sleep(.005)
        with patch('app.scan_folder', side_effect=scan):
            started = time.perf_counter()
            task = json.load(self.request('/api/source-folder', {'path': str(folder), 'background': True}))
            self.assertLess(time.perf_counter() - started, .5)
            self.assertTrue(entered.wait(timeout=2))
            current = json.load(self.request('/api/selection/' + task['id']))
            self.assertEqual(current['status'], 'running')
            self.assertEqual(current['phase'], 'inventory')
            self.assertEqual(current['selected_path'], str(folder))
            state = json.load(self.request('/api/state'))
            self.assertEqual(state['selection']['id'], task['id'])
            self.request('/api/cancel-selection', {'id': task['id']}).close()
            self.server.app.selection_task['thread'].join(timeout=3)
        self.assertEqual(self.server.app.selection(task['id'])['status'], 'cancelled')
        self.assertEqual(set(self.server.app.sources), original_ids)

    def test_browser_folder_zip_starts_at_selected_collection_contents(self):
        files = {'MATRIZ/ANEXO 1/raíz ñ.txt': b'root',
                 'MATRIZ/ANEXO 1/Interior/Nivel 2/cafe\u0301.xml': b'<datos/>'}
        ids = [json.load(self.request('/api/upload?name=' + quote(name), data))['id']
               for name, data in files.items()]
        job = json.load(self.request('/api/jobs', {'action': 'zip', 'ids': ids, 'options': {}}))
        for _ in range(150):
            state = json.load(self.request('/api/job/' + job['id']))
            if state['status'] != 'running':
                break
            time.sleep(.02)
        self.assertEqual(state['status'], 'completed')
        outputs = state['result']['outputs']
        self.assertEqual([o['name'] for o in outputs], ['ANEXO 1.zip'])
        payload = self.request('/api/file/' + outputs[0]['id']).read()
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            expected = {name.removeprefix('MATRIZ/ANEXO 1/'): data for name, data in files.items()}
            self.assertEqual(archive.namelist(), list(expected))
            self.assertEqual({name: archive.read(name) for name in archive.namelist()}, expected)

    def test_open_source_route_requires_session_and_dispatches_only_known_identifiers(self):
        with self.assertRaises(HTTPError) as caught:
            self.request('/api/open-source', {'job': 'demo'}, token=False)
        self.assertEqual(caught.exception.code, 403)
        with self.assertRaises(HTTPError) as caught:
            self.request('/api/open-source', {'job': 'missing', 'path': 'C:\\Windows'})
        self.assertEqual(caught.exception.code, 400)
        with patch.object(self.server.app, 'open_source', return_value={'ok': True}) as opened:
            response = json.load(self.request('/api/open-source', {'job': 'known-job', 'collection': 'MATRIZ/ANEXO 1', 'source_id': 'known-source'}))
            self.assertTrue(response['ok'])
            opened.assert_called_once_with('known-job', 'MATRIZ/ANEXO 1', 'known-source')

    def test_frozen_picker_uses_the_engine_dispatch_instead_of_a_python_script(self):
        from magiczip import runtime
        with patch.object(runtime, 'FROZEN', True), patch('app.subprocess.run') as run:
            run.return_value.returncode = 0
            run.return_value.stdout = '[]'
            self.server.app.choose('files')
            command = run.call_args.args[0]
            self.assertEqual(command, [sys.executable, '--picker', 'files'])

    def test_health_identifies_the_installation_for_portable_launcher_reuse(self):
        from magiczip.runtime import installation_id
        health = json.load(self.request('/health', token=False))
        self.assertEqual(health['installation'], installation_id())
        self.assertEqual(len(health['installation']), 64)

    def test_selected_destination_required_and_source_folder_sets_it(self):
        app=Application()
        self.assertEqual(app.state()['destination'],'')
        with self.assertRaises(ValueError):app.start_job({'action':'zip','ids':[]})
        source_folder=app.session/'input';source_folder.mkdir();(source_folder/'Anexo.txt').write_text('demo')
        data=app.import_folder(str(source_folder))
        self.assertEqual(data['destination'],str(source_folder.resolve()))
        self.assertTrue(data['final_directory'].endswith('ZIPS FINALES'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
