import csv
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from magiczip.engine import ROOT,Runner,digest_file,inspect_source,scan_folder
from magiczip.office import binary_eligibility,package_fingerprints,recompress_excel,run_office


def package(path, extra=None):
    with zipfile.ZipFile(path,'w',zipfile.ZIP_STORED) as z:
        z.comment=b'Keep archive comment'
        z.writestr('[Content_Types].xml','<Types/>')
        z.writestr('xl/workbook.xml','<workbook/>')
        z.writestr('xl/worksheets/sheet1.xml','<worksheet><formula>A1*1.16</formula><data>'+('fiscalops '*20000)+'</data></worksheet>')
        for name,content in (extra or {}).items():z.writestr(name,content)


class OfficeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'work')
        self.folder=Path(self.temp.name)
    def tearDown(self):self.temp.cleanup()

    def test_excel_recompression_preserves_every_internal_byte_and_comment(self):
        source=self.folder/'original.xlsm';target=self.folder/'optimized.xlsm'
        package(source,{'xl/vbaProject.bin':b'opaque macro bytes - never executed','xl/charts/chart1.xml':b'<chart>do not remove</chart>'})
        before=digest_file(source);result=recompress_excel(source,target)
        self.assertTrue(result['accepted']);self.assertLess(target.stat().st_size,source.stat().st_size)
        self.assertEqual(package_fingerprints(source),package_fingerprints(target))
        self.assertEqual(digest_file(source),before)
        with zipfile.ZipFile(target) as z:self.assertEqual(z.comment,b'Keep archive comment')

    def test_signed_excel_is_not_reencoded(self):
        source=self.folder/'signed.xlsx';target=self.folder/'target.xlsx'
        package(source,{'_xmlsignatures/sig1.xml':b'opaque signature'})
        self.assertFalse(recompress_excel(source,target)['accepted']);self.assertFalse(target.exists())

    def test_binary_is_rejected_for_complex_features(self):
        source=self.folder/'complex.xlsx';package(source,{'xl/charts/chart1.xml':b'<chart/>'})
        self.assertFalse(binary_eligibility(source)[0])

    def test_output_has_exact_final_folder_and_no_recursion(self):
        selected=self.folder/'seleccionada';selected.mkdir();original=selected/'Anexo.txt';original.write_text('Demo')
        result=Runner([inspect_source(original)],{},selected).run('zip')
        self.assertEqual(Path(result['directory']).parent,selected/'ZIPS FINALES')
        files,_=scan_folder(selected)
        self.assertEqual([f.name for f in files],['Anexo.txt'])
        for key in ('html','csv','json'):self.assertTrue(Path(result['reports'][key]).is_file())
        report=json.loads(Path(result['report_path']).read_text('utf-8'))
        self.assertEqual(report['creator'],'LAVA');self.assertEqual(report['version'],'2.0.0')
        self.assertEqual(report['file_records'][0]['estado'],'Incluido y verificado')

    def test_report_escapes_filenames_and_csv_formulas(self):
        source=self.folder/'=1+1.txt';source.write_text('demo')
        result=Runner([inspect_source(source)],{},self.folder/'salida').run('zip')
        with Path(result['reports']['csv']).open(encoding='utf-8-sig',newline='') as f:
            records=list(csv.DictReader(f))
        self.assertTrue(records[0]['archivo'].startswith("'="))
        html=Path(result['reports']['html']).read_text('utf8')
        self.assertIn('MAGIC ZIP 2.0',html);self.assertNotIn('<script',html)

    def test_powerpoint_failure_is_reported_even_if_original_fits(self):
        source=self.folder/'presentation.pptx';source.write_bytes(b'DEMO original bytes')
        with patch('magiczip.engine.office_capabilities',return_value={'excel':False,'powerpoint':True}),patch('magiczip.engine.run_office',side_effect=ValueError('No se pudo exportar')):
            result=Runner([inspect_source(source)],{},self.folder/'salida').run('zip')
        self.assertEqual(result['status'],'review');self.assertTrue(result['outputs'])
        self.assertTrue(any('PowerPoint' in p['reason'] for p in result['pending']))
        with zipfile.ZipFile(result['outputs'][0]['path']) as z:self.assertEqual(z.read('presentation.pptx'),b'DEMO original bytes')

    def test_native_office_uses_short_private_paths_and_returns_both_verified_copies(self):
        source=self.folder/'Libro original con espacios y acentos ñ.xlsx'
        target=self.folder/'Libro convertido.xlsb'
        source.write_bytes(b'original bytes')
        captured=[]
        def helper(mode,local_source,local_target,check,progress,timeout):
            captured.append(local_source.parent)
            self.assertNotEqual(local_source.parent,self.folder)
            self.assertLess(len(str(local_source)),250)
            self.assertLess(len(str(local_target.with_suffix('.roundtrip.xlsx'))),250)
            self.assertEqual(local_source.read_bytes(),b'original bytes')
            local_target.write_bytes(b'binary copy')
            local_target.with_suffix('.roundtrip.xlsx').write_bytes(b'roundtrip copy')
            return {'ok':True}
        with patch('magiczip.office._run_office_helper',side_effect=helper):
            self.assertTrue(run_office('excel_binary',source,target)['ok'])
        self.assertEqual(source.read_bytes(),b'original bytes')
        self.assertEqual(target.read_bytes(),b'binary copy')
        self.assertEqual(target.with_suffix('.roundtrip.xlsx').read_bytes(),b'roundtrip copy')
        self.assertFalse(captured[0].exists())

    def test_failed_native_helper_cleans_its_short_workspace(self):
        source=self.folder/'Origen.pptx';source.write_bytes(b'original')
        target=self.folder/'Resultado.pdf';captured=[]
        def fail(mode,local_source,*args):
            captured.append(local_source.parent)
            raise ValueError('native failure')
        with patch('magiczip.office._run_office_helper',side_effect=fail):
            with self.assertRaises(ValueError):run_office('powerpoint_pdf',source,target)
        self.assertFalse(captured[0].exists())
        self.assertFalse(target.exists())
        self.assertEqual(source.read_bytes(),b'original')


if __name__=='__main__':unittest.main(verbosity=2)
