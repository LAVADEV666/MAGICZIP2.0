"""Synthetic diagnostics for an extracted portable; no personal documents are read."""
import io
import json
import platform
import random
import subprocess
import sys
import tempfile
from pathlib import Path

from magiczip.runtime import APP_ROOT, FROZEN, RESOURCE_ROOT, WORK_ROOT, picker_command


def main():
    import PIL
    import pypdf
    import pypdfium2 as pdfium
    from PIL import Image
    from pypdf import PdfReader, PdfWriter
    from magiczip.engine import Runner, digest_file, scan_folder
    from magiczip.office import office_capabilities

    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='diagnostico_', dir=WORK_ROOT) as temporary:
        root = Path(temporary)
        documents = root / 'Documentos de prueba' / 'ANEXO 1'
        documents.mkdir(parents=True)
        source = documents / 'Prueba ñ.pdf'
        writer = PdfWriter()
        rng = random.Random(5421)
        for _ in range(4):
            with Image.frombytes('RGB', (80, 100), rng.randbytes(80 * 100 * 3)) as image:
                stream = io.BytesIO()
                image.save(stream, format='PDF', resolution=72, quality=85)
                writer.append(PdfReader(io.BytesIO(stream.getvalue())))
        with source.open('wb') as stream:
            writer.write(stream)
        fingerprint = digest_file(source)
        with pdfium.PdfDocument(str(source)) as document:
            page = document[0]
            try:
                bitmap = page.render(scale=1)
                try:
                    preview = bitmap.to_pil()
                    try:
                        preview.save(root / 'preview.png')
                    finally:
                        preview.close()
                finally:
                    bitmap.close()
            finally:
                page.close()
        result = Runner(scan_folder(documents.parent)[0], {'limit_bytes': 12_000}, root / 'salida',
                        private_directory=root / 'temporal').run('zip')
        if result['status'] != 'completed' or not result['outputs']:
            raise RuntimeError('Falló la prueba de preparación ZIP: ' + json.dumps(result['pending']))
        if digest_file(source) != fingerprint:
            raise RuntimeError('El diagnóstico detectó cambios en el original.')
        if any(not output['verified'] or output['bytes'] >= 12_000 for output in result['outputs']):
            raise RuntimeError('Falló el límite o la integridad del ZIP.')
        picker = subprocess.run(picker_command('--self-test'), capture_output=True, text=True, encoding='utf-8',
                                timeout=30, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if picker.returncode:
            raise RuntimeError('No se pudo iniciar el selector incluido: ' + picker.stderr[-500:])
        selector = json.loads(picker.stdout)
        from app import Application
        application = Application()
        try:
            start = __import__('time').perf_counter()
            inventory = application.import_folder(str(documents.parent))
            inventory_seconds = __import__('time').perf_counter() - start
            if len(inventory['sources']) != 1 or not inventory['sources'][0]['pending_inspection'] or inventory['sources'][0]['sha256']:
                raise RuntimeError('El portable analizó documentos durante el inventario.')
            job_id = application.start_job({'action': 'zip', 'ids': list(application.sources),
                                            'options': {'limit_bytes': 12_000}})['id']
            application.jobs[job_id]['thread'].join(timeout=30)
            job = application.job(job_id)
            if job['status'] != 'completed' or not job['result']['outputs']:
                raise RuntimeError('Falló el procesamiento diferido del portable: ' + json.dumps(job))
        finally:
            application.cancel_active()
            if not application.has_active_work():
                application.reset()
                application.session.rmdir()
        for resource in ['web/index.html', 'web/app.js', 'web/experience.js', 'web/style.css', 'magiczip/office_worker.ps1']:
            if not (RESOURCE_ROOT / resource).is_file():
                raise RuntimeError('Falta un recurso: ' + resource)
        print(json.dumps({'ok': True, 'frozen': FROZEN, 'python': platform.python_version(),
                          'architecture': platform.machine(), 'executable': sys.executable,
                          'app_root': str(APP_ROOT), 'resources': str(RESOURCE_ROOT),
                          'libraries': {'Pillow': PIL.__file__, 'pypdf': pypdf.__file__, 'PDFium': pdfium.__file__},
                          'selector': selector, 'office': office_capabilities(),
                          'deferred_inventory_verified': True, 'inventory_seconds': round(inventory_seconds, 6),
                          'background_processing_verified': True,
                          'pdf_pages': 4, 'verified_zips': len(result['outputs']), 'original_preserved': True}, ensure_ascii=False), flush=True)
