"""Regression coverage for the folder workflow requested in the review deck."""
import io
import random
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path

from app import Application
from magiczip.engine import ROOT, Runner, digest_file, inspect_source, scan_folder
from pypdf import PdfReader, PdfWriter
from PIL import Image


class FolderFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / 'work')
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_final_names_totals_structure_and_private_artifacts(self):
        source = self.root / 'MATRIZ'
        rng = random.Random(49)
        for folder in ('ANEXO 1', 'ANEXO 2'):
            path = source / folder / 'Interior'
            path.mkdir(parents=True)
            for index in range(3):
                (path / f'{index}.dat').write_bytes(rng.randbytes(3200))
        sources, _ = scan_folder(source)
        for style, suffix in [('numbers', '1-3'), ('part', 'PARTE 1-3'), ('part_of', 'PARTE 1 DE 3')]:
            private = self.root / ('private_' + style)
            events = []
            result = Runner(sources, {'limit_bytes': 5000, 'naming': style}, self.root / 'delivery',
                            telemetry=events.append, private_directory=private).run('zip')
            self.assertEqual(result['status'], 'completed')
            self.assertIn(f'ANEXO 1_{suffix}.zip', [o['name'] for o in result['outputs']])
            self.assertEqual(len(result['outputs']), 6)
            self.assertEqual([f['status'] for f in result['folders']], ['completed', 'completed'])
            self.assertTrue(all(len(f['outputs']) == 3 for f in result['folders']))
            self.assertTrue(all(f['input_bytes'] == 9600 for f in result['folders']))
            for folder in result['folders']:
                self.assertEqual(folder['output_bytes'], sum(o['bytes'] for o in result['outputs'] if o['collection'] == folder['name']))
            self.assertTrue(all(p.suffix == '.zip' for p in Path(result['directory']).iterdir()))
            self.assertTrue(Path(result['report_path']).is_relative_to(private))
            entries = {}
            for output in result['outputs']:
                self.assertLess(output['bytes'], 5000)
                with zipfile.ZipFile(output['path']) as archive:
                    entries.setdefault(output['collection'], []).extend(archive.namelist())
            for collection, names in entries.items():
                self.assertEqual(set(names), {s.zip_relative for s in sources if s.collection == collection})
                self.assertEqual(len(names), len(set(names)))
            self.assertTrue(any(e['active_folder'] == 'MATRIZ/ANEXO 1' for e in events))

    def test_pdf_over_ten_mb_inside_configured_limit_stays_unchanged(self):
        source = self.root / 'Color.pdf'
        image = Image.new('RGB', (120, 180), (210, 30, 90))
        stream = io.BytesIO()
        image.save(stream, format='PDF', resolution=72)
        image.close()
        writer = PdfWriter()
        writer.append(PdfReader(io.BytesIO(stream.getvalue())))
        writer.add_metadata({'/SyntheticPadding': 'x' * 10_000_000})
        with source.open('wb') as file:
            writer.write(file)
        fingerprint = digest_file(source)
        result = Runner([inspect_source(source)], {'smart_pdf': False, 'limit_bytes': 20_000_000},
                        self.root / 'out', private_directory=self.root / 'private').run('zip')
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['preparation'], [])
        self.assertEqual(result['pdf_decisions'][0]['decision'], 'accepted_unchanged')
        self.assertEqual(result['folders'][0]['changes']['grayscale'], 0)
        with zipfile.ZipFile(result['outputs'][0]['path']) as archive:
            reader = PdfReader(io.BytesIO(archive.read(archive.namelist()[0])))
        self.assertEqual(len(reader.pages), 1)
        self.assertAlmostEqual(float(reader.pages[0].mediabox.width), 120, places=2)
        self.assertAlmostEqual(float(reader.pages[0].mediabox.height), 180, places=2)
        objects = reader.pages[0]['/Resources']['/XObject']
        self.assertTrue(all(obj.get_object()['/ColorSpace'] == '/DeviceRGB' for obj in objects.values()))
        with zipfile.ZipFile(result['outputs'][0]['path']) as archive:
            self.assertEqual(archive.read('Color.pdf'), source.read_bytes())
        self.assertEqual(digest_file(source), fingerprint)

    def test_findings_stay_with_their_folder_when_filenames_match(self):
        source = self.root / 'MATRIZ'
        rng = random.Random(81)
        for name, count in [('ANEXO 1', 10_000), ('ANEXO 2', 1000)]:
            folder = source / name
            folder.mkdir(parents=True)
            (folder / 'Mismo_nombre.dat').write_bytes(rng.randbytes(count))
        result = Runner(scan_folder(source)[0], {'limit_bytes': 5000}, self.root / 'out',
                        private_directory=self.root / 'private').run('zip')
        first, second = result['folders']
        self.assertEqual(result['status'], 'review')
        self.assertEqual(first['pending'], 1)
        self.assertEqual(first['output_bytes'], 0)
        self.assertEqual(first['issues'][0]['source'], 'ANEXO 1/Mismo_nombre.dat')
        self.assertEqual(second['status'], 'completed')
        self.assertEqual(second['issues'], [])
        self.assertGreater(second['output_bytes'], 0)
        self.assertEqual(first['input_bytes'], 10_000)
        self.assertEqual(second['input_bytes'], 1000)

    def test_cancel_finishes_names_without_temporary_output_files(self):
        path = self.root / 'ANEXO' / 'a.txt'
        path.parent.mkdir()
        path.write_text('synthetic')
        cancel = threading.Event()
        def telemetry(event):
            if event['statistics']['zips_verified']:
                cancel.set()
        result = Runner(scan_folder(self.root / 'ANEXO')[0], {}, self.root / 'out',
                        cancel=cancel, telemetry=telemetry, private_directory=self.root / 'private').run('zip')
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(result['outputs'][0]['name'], 'ANEXO.zip')
        self.assertTrue(all(p.suffix == '.zip' for p in Path(result['directory']).iterdir()))

    def test_reset_cleans_owned_copies_and_keeps_originals_and_final_zips(self):
        app = Application()
        private = app.session / 'test_owned'
        private.mkdir()
        (private / 'temporary.txt').write_text('temporary')
        app.owned_directories.append(private)
        original = self.root / 'original.txt'
        original.write_text('original')
        app.sources['demo'] = inspect_source(original)
        final = self.root / 'final.zip'
        final.write_bytes(b'final')
        app.outputs['demo'] = final
        app.destination = self.root
        app.jobs['old'] = {'status': 'completed'}
        state = app.reset()
        self.assertFalse(private.exists())
        self.assertTrue(original.exists())
        self.assertTrue(final.exists())
        self.assertEqual(state['sources'], [])
        self.assertEqual(state['jobs'], [])
        self.assertEqual(state['destination'], '')
        app.jobs['active'] = {'status': 'running'}
        with self.assertRaises(ValueError):
            app.reset()
        app.session.rmdir()


if __name__ == '__main__':
    unittest.main()
