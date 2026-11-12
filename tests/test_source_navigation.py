import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import Application
from magiczip.engine import ROOT, inspect_source, scan_folder, source_collection


class SourceNavigationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / 'work')
        self.root = Path(self.temp.name)
        self.app = Application()

    def tearDown(self):
        self.app.reset()
        self.app.session.rmdir()
        self.temp.cleanup()

    def register(self, sources):
        self.app.jobs['test-navigation'] = {
            'status': 'completed', 'source_paths': {
                s.id: {'path': str(Path(s.path).resolve()), 'relative': s.relative,
                       'collection': source_collection(s), 'kind': s.kind} for s in sources}}

    def test_empty_directories_keep_the_original_folder_location(self):
        matrix = self.root / 'MATRIZ'
        folder = matrix / 'ANEXO 1'
        empty = folder / 'Vacía' / 'Interior'
        empty.mkdir(parents=True)
        (folder / 'Documento.txt').write_text('original')
        empty_collection = matrix / 'ANEXO 2'
        empty_collection.mkdir()
        sources = scan_folder(matrix)[0]
        self.register(sources)
        access = self.app.source_access('test-navigation')
        for selected in (folder, empty_collection):
            group = access['folders']['MATRIZ/' + selected.name]
            self.assertTrue(group['available'])
            self.assertEqual(group['directory'], str(selected.resolve()))
        directory_source = next(s for s in sources if Path(s.path) == empty)
        self.assertEqual(access['files'][directory_source.id]['directory'], str(empty.resolve()))

    def test_original_folder_and_file_location_survive_new_selection(self):
        matrix = self.root / 'MATRIZ'
        folder = matrix / 'ANEXO 1'
        nested = folder / 'Documentos'
        nested.mkdir(parents=True)
        source = nested / 'Pendiente.txt'
        source.write_text('original')
        sources = scan_folder(matrix)[0]
        self.register(sources)
        other = self.root / 'OTRA MATRIZ'
        other.mkdir()
        (other / 'Otro.txt').write_text('other')
        self.app.import_folder(str(other))
        access = self.app.source_access('test-navigation')
        self.assertTrue(access['folders']['MATRIZ/ANEXO 1']['available'])
        self.assertEqual(access['folders']['MATRIZ/ANEXO 1']['directory'], str(folder.resolve()))
        with patch('app.os.startfile', create=True) as opened:
            self.app.open_source('test-navigation', collection='MATRIZ/ANEXO 1')
            opened.assert_called_once_with(str(folder.resolve()))
            opened.reset_mock()
            self.app.open_source('test-navigation', source_id=sources[0].id)
            opened.assert_called_once_with(str(nested.resolve()))
        self.assertNotIn('source_paths', self.app.job('test-navigation'))

    def test_browser_copy_is_not_exposed_as_original_location(self):
        copied = self.app.session / 'uploaded.txt'
        copied.write_text('browser copy')
        self.register([inspect_source(copied)])
        access = self.app.source_access('test-navigation')
        self.assertFalse(access['folders']['Archivos sueltos']['available'])
        self.assertTrue(access['folders']['Archivos sueltos']['browser_upload'])
        with patch('app.os.startfile', create=True) as opened:
            with self.assertRaises(ValueError):
                self.app.open_source('test-navigation', collection='Archivos sueltos')
            opened.assert_not_called()
        copied.unlink()

    def test_unknown_missing_and_multiple_locations_are_handled(self):
        sources = []
        for name in ('Uno', 'Dos'):
            folder = self.root / name
            folder.mkdir()
            file = folder / 'Documento.txt'
            file.write_text('original')
            sources.append(inspect_source(file))
        self.register(sources)
        access = self.app.source_access('test-navigation')
        self.assertFalse(access['folders']['Archivos sueltos']['available'])
        self.assertTrue(access['folders']['Archivos sueltos']['multiple'])
        with patch('app.os.startfile', create=True) as opened:
            with self.assertRaises(ValueError):
                self.app.open_source('missing-job', source_id=sources[0].id)
            with self.assertRaises(ValueError):
                self.app.open_source('test-navigation', source_id='not-a-source')
            with self.assertRaises(ValueError):
                self.app.open_source('test-navigation', collection='Archivos sueltos')
            Path(sources[0].path).unlink()
            Path(sources[0].path).parent.rmdir()
            with self.assertRaises(ValueError):
                self.app.open_source('test-navigation', source_id=sources[0].id)
            opened.assert_not_called()


if __name__ == '__main__':
    unittest.main()
