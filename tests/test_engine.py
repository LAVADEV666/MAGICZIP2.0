import io
import json
import os
import random
import sys
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from magiczip.engine import Runner, digest_file, inspect_source, parse_pages, plan_sources, pdf_zip_budget, range_bytes, scan_folder, strategic_parts
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, ByteStringObject, DecodedStreamObject, DictionaryObject, NameObject, NumberObject, TextStringObject
from PIL import Image, ImageDraw


HERE = Path(__file__).resolve().parents[1]


def sample_pdf(path, pages=5, noisy=True, bookmarks=False):
    # Each page contains a different image, so real compressed size drives cuts.
    writer = PdfWriter()
    rng = random.Random(5421)
    for i in range(pages):
        raw = rng.randbytes(220 * 280 * 3) if noisy else bytes([240, 245, 240]) * (220 * 280)
        image = Image.frombytes('RGB', (220, 280), raw)
        ImageDraw.Draw(image).text((10, 10), f'MAGIC PDF / PAGE {i+1}', fill='black', stroke_width=1, stroke_fill='white')
        stream = io.BytesIO()
        image.save(stream, format='PDF', resolution=72, quality=85)
        writer.append(PdfReader(io.BytesIO(stream.getvalue())))
    if bookmarks:
        writer.add_outline_item('Segunda sección', 2)
    with path.open('wb') as f:
        writer.write(f)
    return path


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=HERE / 'work')
        self.root = Path(self.temp.name)
        self.out = self.root / 'out'

    def tearDown(self):
        self.temp.cleanup()

    def run_tool(self, action, paths, **options):
        sources = [inspect_source(p) for p in paths]
        before = [digest_file(p) for p in paths]
        result = Runner(sources, {'limit_bytes': 4_000_000, **options}, self.out).run(action)
        self.assertEqual(before, [digest_file(p) for p in paths])
        return result

    def test_strategic_split_exact_limit_complete_pages(self):
        path = sample_pdf(self.root / 'áé ñ.pdf', pages=7, bookmarks=True)
        result = self.run_tool('split', [path], limit_bytes=120_000)
        self.assertEqual(result['status'], 'completed')
        self.assertGreater(len(result['outputs']), 1)
        self.assertEqual([o['name'] for o in result['outputs']],
                         [f'áé ñ_{i}-{len(result["outputs"])}.pdf' for i in range(1, len(result['outputs']) + 1)])
        seen = []
        for output in result['outputs']:
            self.assertLess(output['bytes'], 120_000)
            first, last = output['page_range']
            seen.extend(range(first, last + 1))
            self.assertEqual(output['pages'], len(PdfReader(output['path']).pages))
        self.assertEqual(seen, list(range(1, 8)))
        import hashlib
        import pypdfium2 as pdfium
        def render_hashes(file):
            hashes = []
            document = pdfium.PdfDocument(str(file))
            try:
                for n in range(len(document)):
                    page = document[n]
                    bitmap = page.render(scale=.6)
                    image = bitmap.to_pil()
                    hashes.append(hashlib.sha256(image.tobytes()).hexdigest())
                    image.close(); bitmap.close(); page.close()
            finally:
                document.close()
            return hashes
        self.assertEqual(render_hashes(path), [h for o in result['outputs'] for h in render_hashes(o['path'])])

    def test_prefers_bookmark_when_nearly_full(self):
        path = sample_pdf(self.root / 'sections.pdf', pages=12)
        writer = PdfWriter(clone_from=path)
        writer.add_outline_item('New section', 9)
        with path.open('wb') as stream:
            writer.write(stream)
        data = path.read_bytes()
        reader = PdfReader(io.BytesIO(data))
        limit = int(len(range_bytes(reader, 0, 10)) / .95)
        parts, pending = strategic_parts(data, limit, lambda:None, lambda *a:None)
        self.assertEqual(parts[0][1], 9)
        self.assertFalse(pending)

    def test_single_page_too_heavy_is_explicit(self):
        path = sample_pdf(self.root / 'heavy.pdf', pages=2)
        result = self.run_tool('split', [path], limit_bytes=10_000)
        self.assertEqual(result['status'], 'review')
        self.assertEqual(len(result['pending']), 2)
        self.assertEqual(len(result['outputs']), 0)
        self.assertEqual([p['pages'] for p in result['pending']], [[1], [2]])

    def test_fit_exactly_is_not_under_limit(self):
        path = sample_pdf(self.root / 'exact.pdf', pages=1)
        data = path.read_bytes()
        parts, pending = strategic_parts(data, len(data), lambda: None, lambda *a: None)
        self.assertTrue(all(len(payload) < len(data) for _, _, payload in parts))

    def test_lossless_compress_never_grows(self):
        path = sample_pdf(self.root / 'compress.pdf', pages=3)
        result = self.run_tool('compress', [path], auto_split=False)
        self.assertEqual(result['status'], 'completed')
        self.assertLessEqual(result['outputs'][0]['bytes'], path.stat().st_size)
        self.assertEqual(result['outputs'][0]['pages'], 3)

    def test_lossy_compression_and_split(self):
        path = sample_pdf(self.root / 'lossy.pdf', pages=4)
        result = self.run_tool('compress', [path], lossy=True, quality=45, max_edge=600, limit_bytes=80_000)
        self.assertEqual(result['status'], 'completed')
        self.assertTrue(all(p['bytes'] < 80_000 for p in result['outputs']))
        self.assertEqual(sum(p['pages'] for p in result['outputs']), 4)

    def test_extract_reorder_and_rotation(self):
        path = sample_pdf(self.root / 'pages.pdf', pages=3, noisy=False)
        writer = PdfWriter(clone_from=path)
        for index, page in enumerate(writer.pages):
            page[NameObject('/MagicPage')] = NumberObject(index + 1)
        with path.open('wb') as stream:
            writer.write(stream)
        result = self.run_tool('extract', [path], pages='3,1,2')
        self.assertEqual(result['outputs'][0]['pages'], 3)
        self.assertEqual([p['/MagicPage'] for p in PdfReader(result['outputs'][0]['path']).pages], [3, 1, 2])
        rotated = self.run_tool('rotate', [path], pages='2', angle=90)
        pages = PdfReader(rotated['outputs'][0]['path']).pages
        self.assertEqual([p.rotation for p in pages], [0, 90, 0])

    def test_merge_preserves_document_order(self):
        first = sample_pdf(self.root / 'first.pdf', pages=2, noisy=False)
        second = sample_pdf(self.root / 'second.pdf', pages=3, noisy=False)
        result = self.run_tool('merge', [second, first])
        self.assertEqual(result['outputs'][0]['pages'], 5)
        self.assertEqual(result['outputs'][0]['sources'], ['second.pdf', 'first.pdf'])

    def test_image_pdf_and_png_export(self):
        path = self.root / 'foto.png'
        Image.new('RGB', (500, 600), '#f1f5ed').save(path)
        result = self.run_tool('images_pdf', [path])
        self.assertEqual(result['outputs'][0]['pages'], 1)
        png = self.run_tool('pdf_images', [Path(result['outputs'][0]['path'])])
        self.assertEqual(len(png['outputs']), 1)
        with Image.open(png['outputs'][0]['path']) as image:
            self.assertEqual(image.format, 'PNG')

    def test_signature_is_blocked_but_zip_preserves_it(self):
        path = sample_pdf(self.root / 'signed.pdf', pages=1, noisy=False)
        writer = PdfWriter(clone_from=path)
        value = DictionaryObject({NameObject('/Type'): NameObject('/Sig'), NameObject('/ByteRange'): ArrayObject([NumberObject(0), NumberObject(10), NumberObject(20), NumberObject(30)]), NameObject('/Contents'): ByteStringObject(b'fake-signature-for-protection-test')})
        field = DictionaryObject({NameObject('/FT'): NameObject('/Sig'), NameObject('/T'): TextStringObject('Firma'), NameObject('/V'): writer._add_object(value)})
        writer._root_object[NameObject('/AcroForm')] = writer._add_object(DictionaryObject({NameObject('/Fields'): ArrayObject([writer._add_object(field)])}))
        with path.open('wb') as f:
            writer.write(f)
        self.assertTrue(inspect_source(path).signed)
        result = self.run_tool('compress', [path])
        self.assertEqual(result['status'], 'review')
        self.assertFalse(result['outputs'])
        archive = self.run_tool('zip', [path])
        self.assertEqual(archive['status'], 'completed')
        with zipfile.ZipFile(archive['outputs'][0]['path']) as z:
            self.assertEqual(z.read('signed.pdf'), path.read_bytes())

    def test_encrypted_pdf_is_pending(self):
        path = sample_pdf(self.root / 'encrypted.pdf', pages=1, noisy=False)
        writer = PdfWriter(clone_from=path)
        writer.encrypt('password')
        with path.open('wb') as f:
            writer.write(f)
        result = self.run_tool('split', [path])
        self.assertEqual(result['status'], 'review')
        self.assertFalse(result['outputs'])

    def test_zip_incompressible_and_empty_and_duplicates(self):
        a, b = self.root / 'one', self.root / 'two'
        a.mkdir(); b.mkdir()
        first, second, empty = a / 'igual.bin', b / 'igual.bin', a / 'empty.txt'
        first.write_bytes(random.Random(10).randbytes(40_000))
        second.write_bytes(random.Random(11).randbytes(40_000))
        empty.write_bytes(b'')
        result = self.run_tool('zip', [first, second, empty], limit_bytes=50_000, structure='flat')
        self.assertEqual(result['status'], 'completed')
        self.assertTrue(all(o['bytes'] < 50_000 for o in result['outputs']))
        self.assertEqual(sum(len(o['entries']) for o in result['outputs']), 3)
        combined = self.run_tool('zip', [first, second], limit_bytes=100_000, structure='flat')
        with zipfile.ZipFile(combined['outputs'][0]['path']) as z:
            self.assertEqual(len(set(z.namelist())), 2)
        oversized = self.run_tool('zip', [first], limit_bytes=10_000)
        self.assertEqual(oversized['status'], 'review')
        self.assertFalse(oversized['outputs'])

    def test_large_compressible_input_is_not_skipped(self):
        path = self.root / 'large.xml'
        path.write_bytes(b'<cfdi>test</cfdi>' * 10_000)
        result = self.run_tool('zip', [path], limit_bytes=10_000)
        self.assertEqual(result['status'], 'completed')
        self.assertLess(result['outputs'][0]['bytes'], 10_000)

    def test_source_changes_are_detected(self):
        path = self.root / 'changed.txt'
        path.write_bytes(b'original')
        source = inspect_source(path)
        path.write_bytes(b'changed')
        result = Runner([source], {'limit_bytes': 10_000}, self.out).run('zip')
        self.assertEqual(result['status'], 'review')
        self.assertFalse(result['outputs'])

    def test_cancel_and_non_overwrite(self):
        path = sample_pdf(self.root / 'cancel.pdf', pages=2)
        event = threading.Event(); event.set()
        result = Runner([inspect_source(path)], {}, self.out, cancel=event).run('compress')
        self.assertEqual(result['status'], 'cancelled')
        self.assertFalse(list(Path(result['directory']).glob('*.partial')))
        first = self.run_tool('split', [path])
        second = self.run_tool('split', [path])
        self.assertNotEqual(first['directory'], second['directory'])

    def test_scan_hidden_and_nested(self):
        nested = self.root / 'nested'
        nested.mkdir()
        (nested / '.hidden.xml').write_text('hidden')
        (self.root / 'empty.txt').write_bytes(b'')
        files, errors = scan_folder(self.root)
        self.assertEqual(len(files), 2)
        self.assertFalse(errors)
        self.assertTrue(all(s.sha256 for s in files))

    def test_page_parser_rejects_bad_values(self):
        self.assertEqual(parse_pages('3,1-2', 3), [2,0,1])
        for text in ('0', '1-4', 'hello', '1,,2'):
            with self.assertRaises(ValueError): parse_pages(text, 3)

    def test_smart_zip_optimizes_splits_and_covers_all_pages(self):
        path = sample_pdf(self.root / 'Expediente.pdf', pages=7, bookmarks=True)
        original = digest_file(path)
        events = []
        result = Runner([inspect_source(path)], {'limit_bytes':120_000}, self.out, telemetry=events.append).run('zip')
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(digest_file(path), original)
        self.assertTrue(all(o['bytes'] < 120_000 for o in result['outputs']))
        preparation = result['preparation'][0]
        self.assertTrue(preparation['split'])
        self.assertGreater(len(preparation['parts']), 1)
        self.assertEqual([p['name'] for p in preparation['parts']],
                         [f'Expediente_{i}-{len(preparation["parts"])}.pdf' for i in range(1, len(preparation['parts']) + 1)])
        ranges = [n for p in preparation['parts'] for n in range(p['page_range'][0], p['page_range'][1] + 1)]
        self.assertEqual(ranges, list(range(1,8)))
        self.assertEqual(sum(len(o['entries']) for o in result['outputs']), len(preparation['parts']))
        hashes = {p['sha256'] for p in preparation['parts']}
        for output in result['outputs']:
            with zipfile.ZipFile(output['path']) as z:
                for entry in output['entries']:
                    import hashlib
                    self.assertIn(hashlib.sha256(z.read(entry['entry'])).hexdigest(), hashes)
                    self.assertEqual(entry['source'], 'Expediente.pdf')
                    self.assertEqual(entry['source_sha256'], original)
        self.assertTrue({'pdf','split','zip','verify'} <= {e['phase'] for e in events})
        self.assertEqual(events[-1]['statistics']['zips_verified'], len(result['outputs']))

    def test_smart_zip_compression_can_avoid_cutting(self):
        writer = PdfWriter(); page = writer.add_blank_page(width=200,height=200)
        content = DecodedStreamObject(); content.set_data(b'q Q\n' * 80_000)
        page[NameObject('/Contents')] = writer._add_object(content)
        path = self.root / 'textual.pdf'
        with path.open('wb') as stream: writer.write(stream)
        result = self.run_tool('zip', [path], limit_bytes=40_000)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(len(result['outputs']), 1)
        prep = result['preparation'][0]
        self.assertTrue(prep['reduced'])
        self.assertFalse(prep['split'])
        self.assertLess(prep['optimized_bytes'], prep['pdf_budget_bytes'])

    def test_smart_zip_protected_pdf_stays_intact(self):
        path = sample_pdf(self.root / 'protected.pdf', pages=4)
        source = inspect_source(path); source.signed = True
        result = Runner([source], {'limit_bytes':20_000}, self.out).run('zip')
        self.assertEqual(result['status'], 'review')
        self.assertFalse(result['preparation'])
        self.assertIn('firmado', result['pending'][0]['recommendation'])

    def test_smart_zip_can_be_disabled(self):
        path = sample_pdf(self.root / 'manual.pdf', pages=4)
        result = self.run_tool('zip', [path], limit_bytes=20_000, smart_pdf=False)
        self.assertFalse(result['preparation'])
        self.assertEqual(result['status'], 'review')

    def test_video_and_excel_have_measured_manual_recommendations(self):
        paths = [self.root / 'video.mp4', self.root / 'libro.xlsx']
        for index, path in enumerate(paths): path.write_bytes(random.Random(index).randbytes(300_000))
        sources = [inspect_source(p) for p in paths]
        plan = plan_sources(sources, 50_000)
        self.assertEqual(plan['manual_risks'], 2)
        self.assertTrue(all(a['sample_ratio'] > .9 for a in plan['assessments']))
        result = self.run_tool('zip', paths, limit_bytes=50_000)
        self.assertEqual(result['status'], 'review')
        self.assertEqual(len(result['pending']), 2)
        self.assertTrue(all(p['measured_zip_bytes'] >= 50_000 and p['recommendation'] for p in result['pending']))

    def test_plan_does_not_reject_compressible_large_files(self):
        path = self.root / 'large.xml'; path.write_bytes(b'<FiscalOps>test</FiscalOps>' * 40_000)
        source = inspect_source(path)
        plan = plan_sources([source], 50_000)
        self.assertEqual(plan['manual_risks'], 0)
        self.assertEqual(plan['estimated_zips'], 1)
        self.assertLess(plan['assessments'][0]['sample_ratio'], .1)

    def test_collections_remain_separate_in_plan_and_zip(self):
        sources = []
        for name in ('Carpeta A', 'Carpeta B'):
            folder = self.root / name; folder.mkdir()
            (folder / 'Anexo.xml').write_bytes(b'example')
            sources.extend(scan_folder(folder)[0])
        plan = plan_sources(sources, 50_000)
        self.assertEqual(len(plan['folders']), 2)
        self.assertEqual(plan['estimated_zips'], 2)
        result = Runner(sources, {'limit_bytes':50_000}, self.out).run('zip')
        self.assertEqual(len(result['outputs']), 2)
        self.assertTrue(any(o['name'].startswith('Carpeta A') for o in result['outputs']))
        self.assertTrue(any(o['name'].startswith('Carpeta B') for o in result['outputs']))

    def test_parent_folder_is_planned_by_first_level_subfolder(self):
        parent = self.root / 'Expediente'
        for child in ('Bancos', 'CFDI'):
            folder = parent / child; folder.mkdir(parents=True)
            (folder / 'Anexo.txt').write_text(child)
        sources, errors = scan_folder(parent)
        self.assertFalse(errors)
        self.assertEqual({f['name'] for f in plan_sources(sources)['folders']}, {'Expediente/Bancos','Expediente/CFDI'})

    def test_pdf_zip_margin_and_cancel_during_optimization(self):
        self.assertLess(pdf_zip_budget(4_000_000), 4_000_000)
        path = sample_pdf(self.root / 'cancel_auto.pdf', pages=5)
        event = threading.Event()
        def telemetry(update):
            if update['phase'] == 'pdf': event.set()
        result = Runner([inspect_source(path)], {'limit_bytes':50_000}, self.out, cancel=event, telemetry=telemetry).run('zip')
        self.assertEqual(result['status'], 'cancelled')
        self.assertFalse(result['outputs'])
        self.assertFalse(list(Path(result['directory']).rglob('*.partial')))


if __name__ == '__main__':
    unittest.main(verbosity=2)
