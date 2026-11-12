"""ZIP paths must start at the contents of each selected collection."""
import io
import random
import tempfile
import unittest
import zipfile
from pathlib import Path, PurePosixPath
from unittest.mock import patch

from magiczip.engine import ROOT, Runner, inspect_source, scan_folder
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject


class ZipStructureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / 'work')
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def snapshot(self, folder):
        return {p.relative_to(folder).as_posix(): None if p.is_dir() else p.read_bytes()
                for p in folder.rglob('*')}

    def pack(self, sources, **options):
        originals = {Path(s.path): None if s.kind == 'directory' else Path(s.path).read_bytes()
                     for s in sources}
        result = Runner(sources, {'smart_pdf': False, 'optimize_excel': False,
                                 'convert_powerpoint': False, **options}, self.root / 'out',
                        private_directory=self.root / ('private_' + str(len(list(self.root.glob('private_*')))))).run('zip')
        self.assertEqual(result['status'], 'completed', result['pending'])
        for path, content in originals.items():
            if content is None:
                self.assertTrue(path.is_dir())
                self.assertFalse(list(path.iterdir()))
            else:
                self.assertEqual(path.read_bytes(), content)
        return result

    def assert_roundtrip(self, folder, result):
        before = self.snapshot(folder)
        destination = self.root / 'extracted'
        destination.mkdir()
        names = []
        for output in result['outputs']:
            self.assertTrue(output['verified'])
            self.assertLess(output['bytes'], result['limit_bytes'])
            with zipfile.ZipFile(output['path']) as archive:
                self.assertIsNone(archive.testzip())
                for name in archive.namelist():
                    self.assertFalse(PurePosixPath(name).is_absolute())
                    self.assertNotIn('..', PurePosixPath(name).parts)
                    self.assertNotIn('\\', name)
                    self.assertNotIn(':', name)
                    self.assertFalse(name.startswith(folder.name + '/'))
                names.extend(archive.namelist())
                archive.extractall(destination)
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(self.snapshot(destination), before)
        self.assertEqual(self.snapshot(folder), before)
        return names

    def test_selected_folder_contents_and_empty_directories_roundtrip(self):
        matrix = self.root / 'MATRIZ'
        folder = matrix / 'ANEXO 1'
        files = {'raíz ñ & datos.txt': b'root contents', 'sin extension': b'',
                 'Datos [2026]/datos.xml': b'<datos/>',
                 'Datos [2026]/Nivel 2/imagen.bin': bytes(range(256)),
                 'Datos [2026]/Nivel 2/cafe\u0301.csv': b'a,b\n1,2'}
        for relative, data in files.items():
            path = folder / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        (folder / 'Vacía ñ').mkdir()
        (folder / 'Datos [2026]' / 'Sin archivos' / 'Interior vacío').mkdir(parents=True)
        sources, errors = scan_folder(matrix)
        self.assertFalse(errors)
        result = self.pack(sources)
        self.assertEqual([o['name'] for o in result['outputs']], ['ANEXO 1.zip'])
        names = self.assert_roundtrip(folder, result)
        self.assertTrue(set(files) <= set(names))
        self.assertIn('Vacía ñ/', names)
        self.assertIn('Datos [2026]/Sin archivos/Interior vacío/', names)

    def test_matrix_root_files_keep_their_names(self):
        folder = self.root / 'ANEXO 1'
        folder.mkdir()
        (folder / 'archivo raíz.txt').write_bytes(b'unchanged')
        result = self.pack(scan_folder(folder)[0])
        self.assertEqual(self.assert_roundtrip(folder, result), ['archivo raíz.txt'])

    def test_empty_selected_folder_has_no_container_entry(self):
        matrix = self.root / 'MATRIZ'
        folder = matrix / 'ANEXO 1'
        folder.mkdir(parents=True)
        result = self.pack(scan_folder(matrix)[0])
        self.assertEqual([o['name'] for o in result['outputs']], ['ANEXO 1.zip'])
        self.assertEqual(self.assert_roundtrip(folder, result), [])
        self.assertEqual(result['file_records'][0]['estado'], 'Incluido y verificado')

    def test_empty_scan_root_creates_empty_zip(self):
        folder = self.root / 'ANEXO 1'
        folder.mkdir()
        result = self.pack(scan_folder(folder)[0])
        self.assertEqual(self.assert_roundtrip(folder, result), [])

    def test_multipart_archives_preserve_nested_paths_without_duplicates(self):
        folder = self.root / 'MATRIZ' / 'ANEXO 1'
        nested = folder / 'Interior' / 'Nivel 2'
        nested.mkdir(parents=True)
        rng = random.Random(218)
        for index in range(4):
            (nested / f'{index}.dat').write_bytes(rng.randbytes(2200))
        (folder / 'Vacía').mkdir()
        result = self.pack(scan_folder(folder.parent)[0], limit_bytes=3500)
        self.assertGreater(len(result['outputs']), 1)
        self.assert_roundtrip(folder, result)

    def test_best_fit_decreasing_uses_space_left_by_previous_archive(self):
        folder = self.root / 'ANEXO 1'
        folder.mkdir()
        rng = random.Random(901)
        (folder / 'grande.dat').write_bytes(rng.randbytes(3000))
        (folder / 'mediano.dat').write_bytes(rng.randbytes(1800))
        (folder / 'pequeno.dat').write_bytes(rng.randbytes(1700))
        result = self.pack(scan_folder(folder)[0], limit_bytes=5000)
        self.assertEqual(len(result['outputs']), 2)
        self.assert_roundtrip(folder, result)

    def test_prepared_pdf_keeps_its_internal_subfolder(self):
        folder = self.root / 'MATRIZ' / 'ANEXO 1'
        nested = folder / 'Interior'
        nested.mkdir(parents=True)
        writer = PdfWriter()
        page = writer.add_blank_page(width=200, height=200)
        content = DecodedStreamObject()
        content.set_data(b'q Q\n' * 50_000)
        page[NameObject('/Contents')] = writer._add_object(content)
        stream = io.BytesIO()
        writer.write(stream)
        path = nested / 'documento.pdf'
        path.write_bytes(stream.getvalue())
        result = self.pack(scan_folder(folder.parent)[0], smart_pdf=True, limit_bytes=40_000)
        self.assertEqual(result['preparation'][0]['status'], 'prepared')
        with zipfile.ZipFile(result['outputs'][0]['path']) as archive:
            self.assertEqual(archive.namelist(), ['Interior/documento_optimizado.pdf'])
        self.assertEqual(path.read_bytes(), stream.getvalue())

    def test_individual_file_keeps_existing_entry_name(self):
        folder = self.root / 'not selected'
        folder.mkdir()
        path = folder / 'archivo ñ.bin'
        path.write_bytes(bytes(range(256)))
        result = self.pack([inspect_source(path)])
        with zipfile.ZipFile(result['outputs'][0]['path']) as archive:
            self.assertEqual(archive.namelist(), [path.name])
            self.assertEqual(archive.read(path.name), path.read_bytes())

    def test_same_named_internal_folder_is_preserved(self):
        matrix = self.root / 'ANEXO 1'
        folder = matrix / 'ANEXO 1'
        nested = folder / 'ANEXO 1'
        nested.mkdir(parents=True)
        (nested / 'datos.txt').write_bytes(b'nested')
        result = self.pack(scan_folder(matrix)[0])
        with zipfile.ZipFile(result['outputs'][0]['path']) as archive:
            self.assertEqual(archive.namelist(), ['ANEXO 1/datos.txt'])

    def test_prepared_excel_keeps_its_internal_subfolder(self):
        folder = self.root / 'MATRIZ' / 'ANEXO 1'
        nested = folder / 'Interior'
        nested.mkdir(parents=True)
        path = nested / 'libro.xlsm'
        contents = {'[Content_Types].xml': b'<Types/>', 'xl/workbook.xml': b'<workbook/>',
                    'xl/worksheets/sheet1.xml': b'<worksheet>' + b'data ' * 20_000 + b'</worksheet>'}
        with zipfile.ZipFile(path, 'w', zipfile.ZIP_STORED) as archive:
            for name, data in contents.items():
                archive.writestr(name, data)
        before = path.read_bytes()
        result = self.pack(scan_folder(folder.parent)[0], optimize_excel=True, excel_binary=False)
        self.assertEqual(result['office_preparation'][0]['status'], 'prepared')
        with zipfile.ZipFile(result['outputs'][0]['path']) as archive:
            self.assertEqual(archive.namelist(), ['Interior/libro_optimizado.xlsm'])
            with zipfile.ZipFile(io.BytesIO(archive.read(archive.namelist()[0]))) as workbook:
                self.assertEqual({name: workbook.read(name) for name in workbook.namelist()}, contents)
        self.assertEqual(path.read_bytes(), before)

    def test_empty_directory_with_office_extension_is_not_converted(self):
        folder = self.root / 'MATRIZ' / 'ANEXO 1'
        (folder / 'Vacía.xlsx').mkdir(parents=True)
        with patch('magiczip.engine.recompress_excel') as recompress:
            result = self.pack(scan_folder(folder.parent)[0], optimize_excel=True)
            recompress.assert_not_called()
        self.assertEqual(self.assert_roundtrip(folder, result), ['Vacía.xlsx/'])


if __name__ == '__main__':
    unittest.main()
