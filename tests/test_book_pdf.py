import io
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from magiczip.engine import ROOT, Cancelled, Runner, inspect_source
from magiczip.pdf_book import PageProof, split_book
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, NameObject, NumberObject
from pdf_fixtures import digital_pdf


class BookPdfTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT/'work')
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def book(self):
        return digital_pdf(repetitions=250, pages=80)

    def test_extensive_pdf_is_split_without_global_optimization_or_text_extraction(self):
        data = self.book()
        path = self.root/'Libro.pdf'
        path.write_bytes(data)
        source = inspect_source(path,deep=False)
        with patch('magiczip.engine.adaptive_optimize',side_effect=AssertionError('global optimization')), \
             patch('pypdf._page.PageObject.extract_text',side_effect=AssertionError('global text extraction')):
            result = Runner([source],{'limit_bytes':25_000},self.root/'out',private_directory=self.root/'private').run('zip')
        self.assertEqual(result['status'],'completed',result['pending'])
        self.assertEqual(result['pdf_decisions'][0]['strategy'],'split_first')
        self.assertGreater(len(result['outputs']),1)
        self.assertEqual(sum(p['pages']for p in result['preparation'][0]['parts']),80)
        self.assertEqual(path.read_bytes(),data)
        actual=[]
        for item in result['preparation'][0]['parts']:
            reader=PdfReader(item['path'])
            self.assertLess(item['bytes'],25_000)
            actual.extend(page.extract_text()for page in reader.pages)
        expected=[page.extract_text()for page in PdfReader(io.BytesIO(data)).pages]
        self.assertEqual(actual,expected)

    def test_cross_part_and_local_index_links_are_preserved(self):
        writer=PdfWriter(clone_from=PdfReader(io.BytesIO(self.book())))
        links=ArrayObject()
        for target in (1,70):
            action=DictionaryObject({NameObject('/S'):NameObject('/GoTo'),
                                     NameObject('/D'):ArrayObject([writer.pages[target].indirect_reference,NameObject('/Fit')])})
            link=DictionaryObject({NameObject('/Type'):NameObject('/Annot'),NameObject('/Subtype'):NameObject('/Link'),
                                   NameObject('/Rect'):ArrayObject([NumberObject(v)for v in (10,10,80,30)]),NameObject('/A'):action})
            links.append(writer._add_object(link))
        writer.pages[0][NameObject('/Annots')]=writer._add_object(links)
        stream=io.BytesIO();writer.write(stream)
        parts,pending,info=split_book(stream.getvalue(),25_000,lambda p:len(p)<25_000,stem='Libro')
        self.assertFalse(pending)
        self.assertGreater(len(parts),1)
        first=PdfReader(io.BytesIO(parts[0][2]))
        annotations=first.pages[0]['/Annots']
        self.assertEqual(len(annotations),2)
        self.assertEqual(annotations[0].get_object()['/A']['/S'],'/GoTo')
        external=annotations[1].get_object()['/A']
        index=next(i for i,(begin,end,_)in enumerate(parts)if begin<=71<=end)
        self.assertEqual(external['/S'],'/GoToR')
        self.assertEqual(external['/F'],f'Libro_{index+1}-{len(parts)}.pdf')
        self.assertEqual(int(external['/D'][0]),71-parts[index][0])

    def test_page_proof_rejects_changed_text_resources_and_geometry(self):
        a=PdfReader(io.BytesIO(digital_pdf(text='ORIGINAL')))
        b=PdfReader(io.BytesIO(digital_pdf(text='CHANGED')))
        self.assertNotEqual(PageProof().page(a.pages[0]),PageProof().page(b.pages[0]))
        b=PdfReader(io.BytesIO(digital_pdf(text='ORIGINAL')))
        b.pages[0].rotate(90)
        self.assertNotEqual(PageProof().page(a.pages[0]),PageProof().page(b.pages[0]))
        b=PdfReader(io.BytesIO(digital_pdf(text='ORIGINAL')))
        b.pages[0]['/Resources']['/Font']['/F1'][NameObject('/BaseFont')]=NameObject('/Courier')
        self.assertNotEqual(PageProof().page(a.pages[0]),PageProof().page(b.pages[0]))

    def test_book_cancellation_is_checked_during_copy_and_verification(self):
        cancel=threading.Event()
        def check():
            if cancel.is_set():raise Cancelled('cancelled')
        def progress(*args):cancel.set()
        with self.assertRaises(Cancelled):
            split_book(self.book(),25_000,lambda p:len(p)<25_000,check,progress)

    def test_only_an_actual_oversized_page_is_optimized(self):
        from magiczip.pdf_optimizer import adaptive_optimize
        writer=PdfWriter(clone_from=PdfReader(io.BytesIO(self.book())))
        stream=DecodedStreamObject()
        stream.set_data(writer.pages[0].get_contents().get_data()+b'%' + b'x'*40_000+b'\n')
        writer.pages[0][NameObject('/Contents')]=writer._add_object(stream)
        data=io.BytesIO();writer.write(data)
        with patch('magiczip.pdf_book.adaptive_optimize',wraps=adaptive_optimize)as optimize:
            parts,pending,info=split_book(data.getvalue(),25_000,lambda p:len(p)<25_000)
        self.assertFalse(pending)
        self.assertEqual(optimize.call_count,1)
        self.assertEqual(info['page_attempts'][0]['page'],1)
        self.assertEqual(sum(end-begin+1 for begin,end,_ in parts),80)

    def test_split_tool_and_compress_tool_use_the_same_fast_route(self):
        path=self.root/'Libro.pdf';path.write_bytes(self.book())
        source=inspect_source(path,deep=False)
        for action in ('split','compress'):
            with patch('magiczip.engine.optimize_pdf',side_effect=AssertionError('global recompression')), \
                 patch('magiczip.engine.strategic_parts',side_effect=AssertionError('legacy repeated search')):
                result=Runner([source],{'limit_bytes':25_000},self.root/action,private_directory=self.root/('private_'+action)).run(action)
            self.assertEqual(result['status'],'completed',result['pending'])
            self.assertEqual(sum(o['pages']for o in result['outputs']),80)


if __name__=='__main__':unittest.main()
