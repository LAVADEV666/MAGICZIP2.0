import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from magiczip.engine import ROOT, Runner, inspect_source, plan_sources, range_bytes, scan_folder
from magiczip.pdf_optimizer import adaptive_optimize, analyze_pdf, image_characteristics, validate_pdf
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, ByteStringObject, DictionaryObject, NameObject, NumberObject
from pdf_fixtures import digital_pdf, image_pdf, padded_pdf, scan_image


class SmartPdfTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / 'work')
        self.root = Path(self.temp.name)
        self.counter = 0

    def tearDown(self):
        self.temp.cleanup()

    def run_zip(self, data, name='documento.pdf', **options):
        self.counter += 1
        folder = self.root / str(self.counter)
        folder.mkdir()
        path = folder / name
        path.write_bytes(data)
        source = inspect_source(path)
        result = Runner([source], options, folder / 'output', private_directory=folder / 'private').run('zip')
        self.assertEqual(path.read_bytes(), data)
        for output in result['outputs']:
            self.assertTrue(output['verified'])
            self.assertLess(output['bytes'], result['limit_bytes'])
            with zipfile.ZipFile(output['path']) as archive:
                self.assertIsNone(archive.testzip())
                for entry in archive.infolist():
                    if entry.filename.lower().endswith('.pdf'):
                        self.assertLess(entry.file_size, result['limit_bytes'])
                        reader = PdfReader(io.BytesIO(archive.read(entry)))
                        if not reader.is_encrypted:
                            self.assertGreater(len(reader.pages), 0)
        return result, source

    def pdf_payloads(self, result):
        payloads = []
        for output in result['outputs']:
            with zipfile.ZipFile(output['path']) as archive:
                payloads.extend((name, archive.read(name)) for name in archive.namelist() if name.endswith('.pdf'))
        return payloads

    def test_500kb_39mb_and_near_limit_are_accepted_byte_for_byte(self):
        for size in (500_000, 3_900_000, 3_960_001, 3_999_999):
            with self.subTest(size=size):
                data = padded_pdf(size)
                result, source = self.run_zip(data, name='ANEXO ñ.pdf')
                self.assertEqual(result['status'], 'completed')
                self.assertEqual(result['pending'], [])
                self.assertEqual(result['warnings'], [])
                self.assertEqual(result['preparation'], [])
                self.assertEqual(result['pdf_decisions'][0]['decision'], 'accepted_unchanged')
                self.assertEqual(self.pdf_payloads(result), [('ANEXO ñ.pdf', data)])
                assessment = plan_sources([source])['assessments'][0]
                self.assertFalse(assessment['automatic_pdf'])
                self.assertFalse(assessment['needs_attention'])

    def test_windows_kib_display_is_not_decimal_megabytes(self):
        data = digital_pdf(repetitions=1_010_000)
        self.assertGreater(len(data), 4_000_000)
        self.assertGreater(3945 * 1024, 4_000_000)
        result, _ = self.run_zip(data)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['pdf_decisions'][0]['original_bytes'], len(data))
        self.assertFalse(result['pending'])

    def test_exact_limit_and_slightly_above_are_measured_and_optimized(self):
        base = digital_pdf(repetitions=1_000_100)
        for size in (4_000_000, 4_000_001, 4_000_500):
            data = base + b' ' * max(0, size - len(base)) if len(base) <= size else digital_pdf(repetitions=999_000)
            data += b' ' * (size - len(data))
            result, _ = self.run_zip(data)
            self.assertEqual(result['status'], 'completed', result['pending'])
            decision = result['pdf_decisions'][0]
            self.assertEqual(decision['original_bytes'], size)
            self.assertEqual(decision['decision'], 'optimized')
            self.assertLess(decision['processed_bytes'], 4_000_000)
            self.assertTrue(decision['analysis']['has_text'])
            validate_pdf(data, self.pdf_payloads(result)[0][1])

    def test_8mb_images_and_12mb_scanned_have_real_lossless_reduction(self):
        for width, height in ((1500, 1800), (1800, 2250)):
            with self.subTest(dimensions=(width, height)):
                with scan_image(width, height) as image:
                    data = image_pdf([image])
                self.assertGreater(len(data), 8_000_000)
                result, _ = self.run_zip(data)
                self.assertEqual(result['status'], 'completed', result['pending'])
                record = result['pdf_decisions'][0]
                self.assertEqual(record['analysis']['type'], 'scanned')
                self.assertLess(record['processed_bytes'], len(data) // 2)
                self.assertTrue(any(a['stage'] == 'images_lossless' and a['status'] == 'accepted' for a in record['attempts']))
                self.assertFalse(record['grayscale'])
                validate_pdf(data, self.pdf_payloads(result)[0][1])

    def test_heavy_page_has_valid_page_measurement_and_specific_diagnostics(self):
        with scan_image(600, 700, photo=True) as heavy:
            first = image_pdf([heavy], jpeg=True)
        second = digital_pdf()
        writer = PdfWriter()
        writer.append(PdfReader(io.BytesIO(first)))
        writer.append(PdfReader(io.BytesIO(second)))
        stream = io.BytesIO()
        writer.write(stream)
        result, _ = self.run_zip(stream.getvalue(), limit_bytes=20_000)
        self.assertEqual(result['status'], 'review')
        self.assertEqual(len(result['pending']), 1)
        pending = result['pending'][0]
        self.assertEqual(pending['pages'], [1])
        for field in ('original_page_bytes', 'processed_page_bytes', 'limit_bytes', 'attempts', 'image_resources', 'recommendation'):
            self.assertIn(field, pending)
        self.assertGreater(pending['original_page_bytes'], 20_000)
        self.assertIn('PDF válido', pending['reason'])
        self.assertIn('decimales', pending['reason'])
        self.assertTrue(self.pdf_payloads(result))
        self.assertIn('PAGE 1', PdfReader(io.BytesIO(self.pdf_payloads(result)[0][1])).pages[0].extract_text())

    def test_digital_text_is_preserved_without_rasterization(self):
        data = digital_pdf(repetitions=1_050_000, pages=2)
        result, _ = self.run_zip(data)
        self.assertEqual(result['status'], 'completed')
        original = PdfReader(io.BytesIO(data))
        payload = self.pdf_payloads(result)[0][1]
        prepared = PdfReader(io.BytesIO(payload))
        self.assertEqual([p.extract_text() for p in original.pages], [p.extract_text() for p in prepared.pages])
        self.assertEqual(len(prepared.pages), 2)
        self.assertTrue(all(not p.images for p in prepared.pages))
        validate_pdf(data, payload)

    def test_mixed_document_keeps_color_marks_table_and_selectable_text(self):
        with scan_image(1500, 1800, color=True) as image:
            data = image_pdf([image], selectable=True)
        result, _ = self.run_zip(data)
        self.assertEqual(result['status'], 'completed')
        record = result['pdf_decisions'][0]
        self.assertEqual(record['analysis']['type'], 'mixed')
        payload = self.pdf_payloads(result)[0][1]
        reader = PdfReader(io.BytesIO(payload))
        self.assertIn('TEXTO SELECCIONABLE', reader.pages[0].extract_text())
        self.assertEqual(reader.pages[0].images[0].image.mode, 'RGB')
        validate_pdf(data, payload)

    def test_signed_pdf_is_kept_or_reported_without_invalidating_signature(self):
        writer = PdfWriter(clone_from=PdfReader(io.BytesIO(digital_pdf())))
        signature = DictionaryObject({NameObject('/Type'): NameObject('/Sig'), NameObject('/ByteRange'): ArrayObject([NumberObject(0), NumberObject(10)]),
                                      NameObject('/Contents'): ByteStringObject(b'synthetic signature')})
        field = DictionaryObject({NameObject('/FT'): NameObject('/Sig'), NameObject('/T'): NameObject('/Firma'),
                                  NameObject('/V'): writer._add_object(signature)})
        writer._root_object[NameObject('/AcroForm')] = writer._add_object(DictionaryObject({NameObject('/Fields'): ArrayObject([writer._add_object(field)])}))
        stream = io.BytesIO()
        writer.write(stream)
        data = stream.getvalue()
        result, source = self.run_zip(data)
        self.assertTrue(source.signed)
        self.assertEqual(self.pdf_payloads(result), [('documento.pdf', data)])
        self.assertFalse(result['pending'])
        large = data + b' ' * 4_000_000
        result, _ = self.run_zip(large)
        self.assertEqual(result['status'], 'review')
        self.assertFalse(result['outputs'])
        self.assertIn('firmado', result['pending'][0]['reason'])

    def test_zip_compression_cannot_hide_an_oversized_pdf_when_auto_is_disabled(self):
        data = digital_pdf(repetitions=1_100_000)
        result, _ = self.run_zip(data, smart_pdf=False)
        self.assertEqual(result['status'], 'review')
        self.assertFalse(result['outputs'])
        self.assertIn('límite individual', result['pending'][0]['reason'])

    def test_encrypted_pdf_under_limit_is_preserved_without_a_false_corruption_warning(self):
        writer = PdfWriter(clone_from=PdfReader(io.BytesIO(digital_pdf())))
        writer.encrypt('synthetic password')
        stream = io.BytesIO()
        writer.write(stream)
        result, source = self.run_zip(stream.getvalue())
        self.assertTrue(source.encrypted)
        self.assertFalse(source.error)
        self.assertEqual(result['status'], 'completed')
        self.assertFalse(result['pending'])
        self.assertEqual(self.pdf_payloads(result), [('documento.pdf', stream.getvalue())])

    def test_small_colored_marks_are_not_classified_as_disposable_color(self):
        with scan_image(1500, 1800) as image:
            image.putpixel((501, 503), (0, 0, 255))
            traits = image_characteristics(image)
            self.assertTrue(traits['relevant_color'])
            self.assertFalse(traits['text_scan_candidate'])

    def test_bilevel_is_adaptive_below_ten_mb_and_requires_visual_validation(self):
        with scan_image(1500, 1800, noise=True) as image:
            # Near-white paper noise, with text and fine marks kept unchanged.
            clean_paper = image.point(lambda value: 248 + (value - 224) // 4 if value >= 224 else value)
            data = image_pdf([clean_paper], jpeg=True)
            clean_paper.close()
        self.assertLess(len(data), 10_000_000)
        result, _ = self.run_zip(data, limit_bytes=150_000)
        self.assertEqual(result['status'], 'completed', result['pending'])
        decision = result['pdf_decisions'][0]
        self.assertTrue(any(a['stage'] == 'bilevel' and a['status'] == 'accepted' for a in decision['attempts']))
        self.assertTrue(any(a['stage'] == 'grayscale' for a in decision['attempts']))
        self.assertTrue(decision['quality_review'])
        validate_pdf(data, self.pdf_payloads(result)[0][1], lossy=True)

    def test_corrupt_pdf_is_not_reported_as_success(self):
        result, _ = self.run_zip(b'%PDF-1.7\nnot a valid document')
        self.assertEqual(result['status'], 'review')
        self.assertFalse(result['outputs'])
        self.assertEqual(len(result['pending']), 1)

    def test_validation_rejects_missing_text_pages_rotation_and_graphics(self):
        original = digital_pdf(pages=2)
        for candidate in (digital_pdf(pages=1), digital_pdf(text='CHANGED', pages=2)):
            with self.assertRaises(ValueError):
                validate_pdf(original, candidate)
        writer = PdfWriter(clone_from=PdfReader(io.BytesIO(original)))
        writer.pages[0].rotate(90)
        stream = io.BytesIO()
        writer.write(stream)
        with self.assertRaises(ValueError):
            validate_pdf(original, stream.getvalue())
        with scan_image(400, 500, color=True) as image:
            scanned = image_pdf([image])
        writer = PdfWriter(clone_from=PdfReader(io.BytesIO(scanned)))
        writer.pages[0]['/Resources']['/XObject']['/Scan'].set_data(b'\xff' * (400 * 500 * 3))
        stream = io.BytesIO()
        writer.write(stream)
        with self.assertRaises(ValueError):
            validate_pdf(scanned, stream.getvalue(), lossy=True)

    def test_larger_or_invalid_candidates_are_discarded(self):
        original = digital_pdf()
        with patch('magiczip.pdf_optimizer._candidate', return_value=(original + b' ' * 100, 1, [])):
            best, diagnosis = adaptive_optimize(original, lambda payload: False)
        self.assertEqual(best, original)
        self.assertTrue(all(a['status'] in ('discarded', 'skipped') for a in diagnosis['attempts']))

    def test_renderable_original_is_not_rejected_for_unsupported_text_extraction(self):
        data = digital_pdf()
        with patch('pypdf._page.PageObject.extract_text', side_effect=ValueError('Fuente no compatible con extracción')):
            result, source = self.run_zip(data)
        self.assertFalse(source.error)
        self.assertIn('analysis_error', source.pdf_analysis)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(self.pdf_payloads(result), [('documento.pdf', data)])

    def test_split_validation_compares_with_the_actual_original_page(self):
        data = digital_pdf(pages=2)
        reader = PdfReader(io.BytesIO(data))
        first = range_bytes(reader, 0, 1)
        second = range_bytes(reader, 1, 2)
        validate_pdf(data, first, page_range=(0, 1))
        validate_pdf(data, second, page_range=(1, 2))
        with self.assertRaises(ValueError):
            validate_pdf(data, second, page_range=(0, 1))

    def test_multiple_pdfs_and_subfolders_keep_original_internal_structure(self):
        matrix = self.root / 'MATRIZ'
        folder = matrix / 'ANEXO 1'
        nested = folder / 'Interior' / 'Nivel 2'
        nested.mkdir(parents=True)
        small = digital_pdf()
        (folder / 'original.pdf').write_bytes(small)
        (nested / 'grande.pdf').write_bytes(digital_pdf(repetitions=1_050_000))
        result = Runner(scan_folder(matrix)[0], {}, self.root / 'out', private_directory=self.root / 'private').run('zip')
        self.assertEqual(result['status'], 'completed')
        self.assertEqual([o['name'] for o in result['outputs']], ['ANEXO 1.zip'])
        self.assertEqual([name for name, _ in self.pdf_payloads(result)], ['original.pdf', 'Interior/Nivel 2/grande_optimizado.pdf'])
        self.assertEqual(self.pdf_payloads(result)[0][1], small)


if __name__ == '__main__':
    unittest.main()
