import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from app import Application
from magiczip import engine
from magiczip.engine import ROOT, Cancelled
from pdf_fixtures import digital_pdf


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / 'work')
        self.root = Path(self.temp.name)
        self.app = Application()

    def tearDown(self):
        self.app.cancel_active()
        if self.app.selection_task:
            self.app.selection_task['thread'].join(timeout=5)
        for job in self.app.jobs.values():
            if job.get('thread'):
                job['thread'].join(timeout=10)
        self.assertFalse(self.app.has_active_work())
        self.app.reset()
        self.app.session.rmdir()
        self.temp.cleanup()

    def folder(self, count=1, payload=None):
        matrix = self.root / 'MATRIZ'
        folder = matrix / 'ANEXO 1'
        folder.mkdir(parents=True, exist_ok=True)
        for index in range(count):
            (folder / f'{index}.pdf').write_bytes(payload or digital_pdf())
        return matrix

    def job(self):
        ids = list(self.app.sources)
        ident = self.app.start_job({'action': 'zip', 'ids': ids, 'options': {}})['id']
        self.app.jobs[ident]['thread'].join(timeout=15)
        self.assertFalse(self.app.jobs[ident]['thread'].is_alive())
        return self.app.job(ident)

    def test_inventory_500_documents_never_opens_hashes_or_analyzes_pdf(self):
        matrix = self.folder(500)
        with patch('magiczip.engine.inspect_source', side_effect=AssertionError('premature inspection')), \
             patch('magiczip.engine.digest_file', side_effect=AssertionError('premature hash')), \
             patch('magiczip.engine.PdfReader', side_effect=AssertionError('premature PDF open')):
            state = self.app.import_folder(str(matrix))
        self.assertEqual(len(state['sources']), 500)
        self.assertTrue(all(s['kind'] == 'pdf' and s['pending_inspection'] and not s['sha256'] for s in state['sources']))
        self.assertEqual(sum(s['size'] for s in state['sources']), sum(p.stat().st_size for p in matrix.rglob('*.pdf')))

    def test_background_ack_path_progress_and_cancel_without_partial_publication(self):
        old = self.folder()
        self.app.import_folder(str(old))
        original_ids = set(self.app.sources)
        entered = threading.Event()
        def slow(folder, excluded, *, inventory_only, check, progress):
            entered.set()
            progress(1, 123, str(folder / 'reading.pdf'))
            while True:
                check()
                time.sleep(.005)
        with patch('app.scan_folder', side_effect=slow):
            started = time.perf_counter()
            ident = self.app.begin_selection(path=str(old))['id']
            self.assertLess(time.perf_counter() - started, .5)
            self.assertTrue(entered.wait(timeout=2))
            current = self.app.selection(ident)
            self.assertEqual(current['phase'], 'inventory')
            self.assertEqual(current['selected_path'], str(old))
            self.assertEqual(self.app.state()['selection']['status'], 'running')
            with self.assertRaises(ValueError):
                self.app.begin_selection(path=str(old))
            self.app.cancel_selection(ident)
            self.app.selection_task['thread'].join(timeout=3)
        self.assertEqual(self.app.selection(ident)['status'], 'cancelled')
        self.assertEqual(set(self.app.sources), original_ids)
        self.assertFalse(self.app.selection_task['thread'].is_alive())

    def test_native_picker_process_is_terminated_and_lock_released_on_cancel(self):
        import subprocess
        children = []
        real_popen = subprocess.Popen
        launched = threading.Event()
        def popen(*args, **kwargs):
            child = real_popen(*args, **kwargs)
            children.append(child)
            launched.set()
            return child
        with patch('app.picker_command', return_value=[sys.executable, '-c', 'import time; time.sleep(60)']), \
             patch('app.subprocess.Popen', side_effect=popen):
            ident = self.app.begin_selection()['id']
            self.assertTrue(launched.wait(timeout=3))
            self.app.cancel_selection(ident)
            self.app.selection_task['thread'].join(timeout=4)
        self.assertEqual(self.app.selection(ident)['status'], 'cancelled')
        self.assertIsNotNone(children[0].poll())
        self.assertTrue(children[0].stdout.closed)
        self.assertTrue(self.app.dialog_lock.acquire(blocking=False))
        self.app.dialog_lock.release()

    def test_small_pdf_analysis_is_deferred_and_repeated_processing_reuses_validation(self):
        matrix = self.folder(payload=digital_pdf(pages=2))
        self.app.import_folder(str(matrix))
        real_inspect, real_validate = engine.inspect_source, engine.validate_pdf
        with patch('magiczip.engine.inspect_source', wraps=real_inspect) as inspect, \
             patch('magiczip.engine.analyze_pdf', side_effect=AssertionError('deep analysis on small PDF')), \
             patch('magiczip.engine.validate_pdf', wraps=real_validate) as validate:
            first, second = self.job(), self.job()
        self.assertEqual(first['status'], 'completed', first['detail'])
        self.assertEqual(second['status'], 'completed', second['detail'])
        self.assertEqual(inspect.call_count, 1)
        self.assertEqual(validate.call_count, 1)
        self.assertTrue(second['result']['pdf_decisions'][0]['validation_cache_hit'])
        self.assertEqual(second['result']['performance']['inspection_cache_hits'], 1)
        for job in (first, second):
            with zipfile.ZipFile(job['result']['outputs'][0]['path']) as archive:
                self.assertEqual(archive.namelist(), ['0.pdf'])
                self.assertEqual(archive.read('0.pdf'), (matrix / 'ANEXO 1' / '0.pdf').read_bytes())

    def test_optimized_pdf_is_reused_and_tampered_cache_is_not_delivered(self):
        matrix = self.folder(payload=digital_pdf(repetitions=1_050_000))
        self.app.import_folder(str(matrix))
        real = engine.adaptive_optimize
        with patch('magiczip.engine.adaptive_optimize', wraps=real) as optimize:
            first, second = self.job(), self.job()
            self.assertEqual(first['status'], 'completed')
            self.assertEqual(second['status'], 'completed')
            self.assertEqual(optimize.call_count, 1)
            self.assertTrue(second['result']['preparation'][0]['optimization_cache_hit'])
            cached = next(iter(self.app.source_cache.prepared.values()))
            Path(cached['parts'][0]['path']).write_bytes(b'corrupted cache')
            third = self.job()
            self.assertEqual(third['status'], 'completed')
            self.assertEqual(optimize.call_count, 2)

    def test_changed_original_invalidates_inventory_and_cache(self):
        matrix = self.folder()
        self.app.import_folder(str(matrix))
        self.assertEqual(self.job()['status'], 'completed')
        path = matrix / 'ANEXO 1' / '0.pdf'
        modified = digital_pdf(text='NUEVO DOCUMENTO')
        path.write_bytes(modified)
        failed = self.job()
        self.assertEqual(failed['status'], 'review')
        self.assertFalse(failed['result']['outputs'])
        self.assertIn('cambió', failed['result']['pending'][0]['reason'])
        self.app.import_folder(str(matrix))
        self.assertEqual(self.job()['status'], 'completed')
        self.assertEqual(path.read_bytes(), modified)

    def test_cancel_during_inspection_stops_workers_before_reporting_cancelled(self):
        matrix = self.folder(10)
        self.app.import_folder(str(matrix))
        entered = threading.Event()
        def slow(path, *, deep, check):
            entered.set()
            while True:
                check()
                time.sleep(.005)
        with patch('magiczip.engine.inspect_source', side_effect=slow):
            ident = self.app.start_job({'action': 'zip', 'ids': list(self.app.sources), 'options': {}})['id']
            self.assertTrue(entered.wait(timeout=3))
            self.app.jobs[ident]['cancel'].set()
            self.app.jobs[ident]['thread'].join(timeout=4)
        self.assertEqual(self.app.job(ident)['status'], 'cancelled')
        self.assertFalse(self.app.jobs[ident]['thread'].is_alive())
        self.assertFalse(any(t.name.startswith('MagicZip-analysis') for t in threading.enumerate()))
        self.assertTrue(all(s.pending_inspection for s in self.app.sources.values()))
        self.assertFalse(list(matrix.rglob('*.zip')))

    def test_inspection_concurrency_is_bounded_and_errors_do_not_stop_other_files(self):
        matrix = self.folder(20)
        self.app.import_folder(str(matrix))
        active, maximum, lock = 0, 0, threading.Lock()
        real = engine.inspect_source
        def inspect(path, **kwargs):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            try:
                time.sleep(.002)
                if path.name == '0.pdf':
                    raise OSError('Acceso denegado de prueba')
                return real(path, **kwargs)
            finally:
                with lock:
                    active -= 1
        with patch('magiczip.engine.inspect_source', side_effect=inspect):
            job = self.job()
        self.assertGreaterEqual(maximum, 1)
        self.assertLessEqual(maximum, 2)
        self.assertEqual(job['status'], 'review')
        self.assertEqual(len(job['result']['pending']), 1)
        self.assertEqual(sum(len(o['entries']) for o in job['result']['outputs']), 19)

    def test_on_demand_inspection_keeps_multipage_preview_metadata(self):
        self.app.import_folder(str(self.folder(payload=digital_pdf(pages=3))))
        source = next(iter(self.app.sources.values()))
        self.assertEqual(source.pages, 0)
        inspected = self.app.inspect_requested_source(source.id)
        self.assertEqual(inspected['pages'], 3)
        self.assertFalse(inspected['pending_inspection'])


if __name__ == '__main__':
    unittest.main()
