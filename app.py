from __future__ import annotations

import argparse
import copy
import hmac
import io
import json
import mimetypes
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from magiczip.engine import ROOT, PDFIUM_LOCK, Cancelled, Runner, Source, inspect_source, inventory_source, plan_sources, safe_name, scan_folder, source_collection
from magiczip.source_cache import SourceCache
from magiczip.office import office_capabilities
from magiczip.runtime import WORK_ROOT, installation_id, picker_command

VERSION = '2.0.0'
STATIC = ROOT / 'web'


class Application:
    def __init__(self):
        self.token = secrets.token_urlsafe(32)
        self.sources: dict[str, Source] = {}
        self.outputs: dict[str, Path] = {}
        self.jobs: dict[str, dict] = {}
        self.lock = threading.RLock()
        self.dialog_lock = threading.Lock()
        self.destination = None
        self.source_folder = None
        self.office = office_capabilities()
        self.html_reports = set()
        self.session = WORK_ROOT / 'sessions' / uuid.uuid4().hex
        self.session.mkdir(parents=True, exist_ok=False)
        self.scan_errors = []
        self.owned_directories = []
        self.clients = {}
        self.clients_seen = False
        self.last_client_left = None
        self.source_cache = SourceCache()
        self.selection_task = None

    def reset(self):
        with self.lock:
            if self.selection_task and self.selection_task['status'] == 'running':
                raise ValueError('Cancela la selección y espera a que termine antes de limpiar la sesión.')
            if any(j['status'] == 'running' for j in self.jobs.values()):
                raise ValueError('Cancela el procesamiento y espera a que termine antes de limpiar la sesión.')
            # Only directories explicitly created by this session may be removed.
            for directory in self.owned_directories[:]:
                resolved = directory.resolve()
                if resolved == self.session.resolve() or not resolved.is_relative_to(self.session.resolve()):
                    raise ValueError('La carpeta temporal está fuera de la sesión.')
                shutil.rmtree(resolved, ignore_errors=False) if resolved.exists() else None
                self.owned_directories.remove(directory)
            self.sources.clear()
            self.outputs.clear()
            self.jobs.clear()
            self.html_reports.clear()
            self.scan_errors.clear()
            self.source_folder = self.destination = None
            self.source_cache.clear()
            self.selection_task = None
        return self.state()

    def client_presence(self, ident, closed=False):
        if not isinstance(ident, str) or not 1 <= len(ident) <= 80:
            raise ValueError('Identificador de ventana inválido.')
        with self.lock:
            self.clients_seen = True
            if closed:
                self.clients.pop(ident, None)
                if not self.clients:
                    self.last_client_left = time.monotonic()
            else:
                self.clients[ident] = time.monotonic()
                self.last_client_left = None

    def state(self):
        with self.lock:
            return {'version': VERSION, 'sources': [s.public() for s in self.sources.values()],
                    'destination': str(self.destination) if self.destination else '',
                    'source_folder':str(self.source_folder) if self.source_folder else '',
                    'final_directory':str(self.destination if self.destination.name.casefold()=='zips finales' else self.destination / 'ZIPS FINALES') if self.destination else '',
                    'office':self.office,'scan_errors': self.scan_errors,
                    'jobs': [self.job(k) for k in self.jobs],
                    'selection': self._selection_public() if self.selection_task else None}

    def _selection_public(self):
        return copy.deepcopy({k: v for k, v in self.selection_task.items() if k not in {'cancel', 'thread', 'committed'}})

    @staticmethod
    def _check_cancel(cancel):
        if cancel.is_set():
            raise Cancelled('Proceso cancelado. Los originales no se modificaron.')

    def selection(self, ident):
        with self.lock:
            if not self.selection_task or self.selection_task['id'] != ident:
                raise ValueError('Selección no encontrada.')
            result = self._selection_public()
            if result['status'] == 'completed':
                result['state'] = self.state()
            return result

    def cancel_selection(self, ident=None):
        with self.lock:
            if self.selection_task and self.selection_task['status'] == 'running' and not self.selection_task.get('committed') and (ident is None or self.selection_task['id'] == ident):
                self.selection_task['cancel'].set()
                self.selection_task['stage'] = 'Cancelando selección'

    def begin_selection(self, *, kind='folder', path=None):
        if kind not in {'folder', 'files', 'destination'}:
            raise ValueError('Selector desconocido.')
        with self.lock:
            if self.selection_task and self.selection_task['status'] == 'running':
                raise ValueError('Espera o cancela la selección actual.')
            if any(j['status'] == 'running' for j in self.jobs.values()):
                raise ValueError('Espera o cancela el procesamiento actual.')
            task = {'id': uuid.uuid4().hex, 'kind': kind, 'status': 'running', 'phase': 'select',
                    'stage': 'Seleccionando carpeta' if kind == 'folder' else 'Seleccionando en Windows',
                    'detail': '', 'selected_path': '', 'current': 0, 'total': 0,
                    'bytes': 0, 'cancel': threading.Event(), 'metrics': {}}
            self.selection_task = task
        def work():
            started, cpu = time.perf_counter(), time.process_time()
            outcome, stage, detail = 'completed', 'Inventario completado', None
            try:
                if path is None:
                    self.choose(kind, selection=task)
                else:
                    self.import_folder(path, selection=task)
                self._check_cancel(task['cancel'])
            except Cancelled:
                outcome, stage = 'cancelled', 'Selección cancelada'
            except Exception as exc:
                outcome, stage, detail = 'error', 'No se pudo leer la carpeta', str(exc)
            finally:
                with self.lock:
                    task['metrics'].update(total_seconds=round(time.perf_counter() - started, 6),
                                           cpu_seconds=round(time.process_time() - cpu, 6))
                    task.update(status=outcome, stage=stage)
                    if detail is not None:
                        task['detail'] = detail
        thread = threading.Thread(target=work, name='MagicZip-inventory', daemon=True)
        task['thread'] = thread
        thread.start()
        return {'id': task['id'], 'status': 'running'}

    def _native_choice(self, kind, task):
        started = time.perf_counter()
        process = subprocess.Popen(picker_command(kind), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding='utf-8',
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        with self.lock:
            task['metrics']['picker_pid'] = process.pid
        try:
            while True:
                self._check_cancel(task['cancel'])
                try:
                    stdout, stderr = process.communicate(timeout=.1)
                    break
                except subprocess.TimeoutExpired:
                    if time.perf_counter() - started > 600:
                        raise ValueError('El selector no respondió. Vuelve a intentarlo.')
            if process.returncode:
                raise ValueError('No se pudo abrir el selector nativo. ' + stderr[-300:])
            return json.loads(stdout)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            for stream in (process.stdout, process.stderr):
                if stream:
                    stream.close()
            with self.lock:
                task['metrics']['picker_seconds'] = round(time.perf_counter() - started, 6)
                task['metrics']['picker_exit_code'] = process.returncode

    def choose(self, kind, selection=None):
        if kind not in {'files', 'folder', 'destination'}:
            raise ValueError('Selector desconocido.')
        if not self.dialog_lock.acquire(blocking=False):
            raise ValueError('Ya hay un selector abierto.')
        try:
            if selection:
                chosen = self._native_choice(kind, selection)
                self._check_cancel(selection['cancel'])
                if not chosen:
                    raise Cancelled('Selección cancelada.')
            else:
                process = subprocess.run(picker_command(kind),
                                     capture_output=True, text=True, encoding='utf-8',
                                     creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), timeout=600)
                if process.returncode:
                    raise ValueError('No se pudo abrir el selector nativo. Usa Elegir carpeta o Agregar archivos. ' + process.stderr[-300:])
                chosen = json.loads(process.stdout)
            if kind == 'files':
                items = []
                for p in chosen:
                    if selection:
                        self._check_cancel(selection['cancel'])
                    items.append(inventory_source(Path(p)))
                with self.lock:
                    if selection:
                        self._check_cancel(selection['cancel'])
                    self.sources.update({s.id: s for s in items})
                    if selection:
                        selection.update(committed=True, current=len(items), total=len(items), stage='Inventario completado')
            elif chosen:
                if kind == 'destination':
                    with self.lock:
                        if selection:
                            self._check_cancel(selection['cancel'])
                        self.destination = Path(chosen).resolve()
                        if selection:
                            selection.update(committed=True, selected_path=str(self.destination))
                else:
                    return self.import_folder(chosen, selection=selection)
        finally:
            self.dialog_lock.release()
        return self.state()

    def import_folder(self, path, selection=None):
        folder=Path(str(path)).expanduser()
        with self.lock:
            if self.selection_task and self.selection_task['status'] == 'running' and selection is not self.selection_task:
                raise ValueError('Espera o cancela el inventario actual antes de cambiar la carpeta.')
            if any(j['status'] == 'running' for j in self.jobs.values()):
                raise ValueError('Espera o cancela el proceso actual antes de cambiar la carpeta.')
            if selection:
                selection.update(phase='inventory', stage='Leyendo archivos', selected_path=str(folder), detail=str(folder))
        if not folder.is_absolute() or not folder.is_dir():
            raise ValueError('Selecciona una carpeta existente o escribe su ruta completa.')
        folder=folder.resolve()
        if folder.name.casefold()=='zips finales':
            raise ValueError('Selecciona la carpeta de documentos originales, no ZIPS FINALES.')
        started, accumulated = time.perf_counter(), 0
        last_update = [0.]
        def check():
            if selection:
                self._check_cancel(selection['cancel'])
        def progress(count, size, detail):
            nonlocal accumulated
            accumulated += size
            now = time.perf_counter()
            if selection and now - last_update[0] >= .05:
                with self.lock:
                    selection.update(current=count, bytes=accumulated, detail=detail)
                last_update[0] = now
        items, errors=scan_folder(folder,[WORK_ROOT], inventory_only=True, check=check, progress=progress)
        check()
        with self.lock:
            check()
            self.sources={s.id:s for s in items}
            self.source_folder=folder
            self.destination=folder
            self.scan_errors=errors
            if selection:
                selection.update(current=len(items), total=len(items), bytes=sum(s.size for s in items))
                selection.update(committed=True, stage='Inventario completado')
                selection['metrics']['inventory_seconds'] = round(time.perf_counter() - started, 6)
        return self.state()

    def hydrate_sources(self, selected, cancel, progress):
        started, cpu = time.perf_counter(), time.process_time()
        before_hits, before_misses = self.source_cache.hits, self.source_cache.misses
        check = lambda: self._check_cancel(cancel)
        def inspect(source):
            check()
            try:
                return self.source_cache.inspect(source, check)
            except Cancelled:
                raise
            except (OSError, ValueError) as exc:
                return replace(source, error=str(exc), sha256='', pending_inspection=False)
        progress('Analizando documentos', 0, len(selected), 'Leyendo y verificando los documentos seleccionados')
        executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='MagicZip-analysis')
        try:
            futures = {executor.submit(inspect, source): source.id for source in selected}
            ready = {}
            for future in as_completed(futures):
                check()
                item = future.result()
                ready[item.id] = item
                progress('Analizando documentos', len(ready), len(selected), item.relative)
            check()
        finally:
            executor.shutdown(wait=True, cancel_futures=True)
        ordered = [ready[s.id] for s in selected]
        with self.lock:
            check()
            for item in ordered:
                if item.id in self.sources:
                    self.sources[item.id] = item
        return ordered, {'inspection_seconds': round(time.perf_counter() - started, 6),
                         'inspection_cpu_seconds': round(time.process_time() - cpu, 6),
                         'inspection_workers': 2, 'inspection_cache_hits': self.source_cache.hits - before_hits,
                         'inspection_cache_misses': self.source_cache.misses - before_misses}

    def start_job(self, body):
        action = body.get('action')
        options = body.get('options') or {}
        ids = body.get('ids') or []
        if not isinstance(ids, list) or not isinstance(options, dict):
            raise ValueError('Solicitud inválida.')
        with self.lock:
            if self.selection_task and self.selection_task['status'] == 'running':
                raise ValueError('Espera a que termine el inventario o cancélalo antes de procesar.')
            if self.destination is None:
                raise ValueError('Elige la carpeta de salida. Se creará ZIPS FINALES dentro de ella.')
            if any(j['status'] == 'running' for j in self.jobs.values()):
                raise ValueError('Espera o cancela el proceso actual antes de iniciar otro.')
            selected = [self.sources[k] for k in ids if k in self.sources]
            if not selected or len(selected) != len(ids) or len(ids) != len(set(ids)):
                raise ValueError('Selecciona archivos válidos, sin repetir identificadores.')
            ident = uuid.uuid4().hex
            job = {'id': ident, 'status': 'running', 'stage': 'Preparando', 'current': 0,
                   'total': len(selected), 'detail': '', 'cancel': threading.Event(), 'result': None,
                   'phase': 'analyze', 'statistics': {}, 'events': [], 'folders': [],
                   'source_paths': {s.id: {'path': str(Path(s.path).resolve()), 'relative': s.relative,
                                         'collection': source_collection(s), 'kind': s.kind} for s in selected}}
            self.jobs[ident] = job
            destination = self.destination
            scan_errors = list(self.scan_errors)

        def progress(stage, current, total, detail):
            with self.lock:
                job.update(stage=stage, current=current, total=total, detail=detail)

        last_telemetry = [0.]
        def telemetry(event):
            now = time.monotonic()
            if not event.get('event') and now - last_telemetry[0] < .1:
                return
            last_telemetry[0] = now
            with self.lock:
                job['phase'] = event['phase']
                job['statistics'] = event['statistics']
                job['folders'] = copy.deepcopy(event.get('folders', []))
                job['active_folder'] = event.get('active_folder')
                if event.get('event'):
                    job['events'].append({'time': datetime.now().strftime('%H:%M:%S'),
                                          'phase': event['phase'], 'text': event['event']})
                    job['events'] = job['events'][-60:]

        def work():
            try:
                private_directory = self.session / ('job_' + ident)
                with self.lock:
                    self.owned_directories.append(private_directory)
                ready, metrics = self.hydrate_sources(selected, job['cancel'], progress)
                runner = Runner(ready, options, destination, progress, job['cancel'], telemetry, private_directory,
                                source_cache=self.source_cache, performance=metrics)
                runner.pending.extend({'source': e['path'], 'reason': e['reason']} for e in scan_errors)
                # PDFium is process-global and must not render concurrently.
                if action == 'pdf_images':
                    with PDFIUM_LOCK:
                        result = runner.run(action)
                else:
                    result = runner.run(action)
                result.setdefault('performance', {}).update(metrics)
                result['source_access'] = self.source_access(ident)
                with self.lock:
                    for output in result['outputs']:
                        out_id = uuid.uuid4().hex
                        self.outputs[out_id] = Path(output['path'])
                        output['id'] = out_id
                    for preparation in result.get('preparation', []):
                        for part in preparation['parts']:
                            out_id = uuid.uuid4().hex
                            self.outputs[out_id] = Path(part['path'])
                            part['id'] = out_id
                    report_id = uuid.uuid4().hex
                    self.outputs[report_id] = Path(result['report_path'])
                    result['report_id'] = report_id
                    result['report_ids']={}
                    for report_kind,report_path in result.get('reports',{}).items():
                        report_key=uuid.uuid4().hex
                        self.outputs[report_key]=Path(report_path)
                        result['report_ids'][report_kind]=report_key
                        if report_kind=='html': self.html_reports.add(report_key)
                    job['result'] = result
                    job['status'] = result['status']
                    job['stage'] = 'Finalizado'
                    job['statistics'] = result.get('statistics', {})
                    job['folders'] = result.get('folders', [])
            except Cancelled:
                with self.lock:
                    job.update(status='cancelled', stage='Proceso cancelado', detail='Se cerró el análisis; originales intactos.')
            except Exception as exc:
                with self.lock:
                    job.update(status='error', stage='No se completó', detail=str(exc))
        thread = threading.Thread(target=work, name='MagicZip-processing', daemon=True)
        job['thread'] = thread
        thread.start()
        return {'id': ident}

    def inspect_requested_source(self, ident):
        with self.lock:
            source = self.sources.get(ident)
        if source is None:
            raise ValueError('Archivo no encontrado.')
        item = self.source_cache.inspect(source)
        with self.lock:
            if ident in self.sources:
                self.sources[ident] = item
        return item.public()

    def job(self, ident):
        with self.lock:
            if ident not in self.jobs:
                raise ValueError('Proceso no encontrado.')
            return {k: v for k, v in self.jobs[ident].items() if k not in {'cancel', 'source_paths', 'thread'}}

    def cancel_active(self):
        self.cancel_selection()
        with self.lock:
            for job in self.jobs.values():
                if job['status'] == 'running':
                    job['cancel'].set()

    def has_active_work(self):
        with self.lock:
            return bool(self.selection_task and self.selection_task['status'] == 'running') or any(
                job['status'] == 'running' for job in self.jobs.values())

    def source_access(self, ident):
        """Keep navigation tied to the sources of this job, even after a new import."""
        with self.lock:
            job = self.jobs.get(ident)
            if not job:
                raise ValueError('Proceso no encontrado.')
            records = dict(job.get('source_paths', {}))
        files, groups = {}, {}
        for source_id, record in records.items():
            path = Path(record['path'])
            native = not path.is_relative_to(self.session.resolve())
            source_directory = path if record.get('kind') == 'directory' else path.parent
            directory = source_directory
            pieces = record['relative'].replace('\\', '/').split('/')
            if native and len(pieces) > 1:
                try:
                    root = path.parents[len(pieces) - 1]
                    if root.joinpath(*pieces).resolve() == path:
                        directory = root / pieces[0]
                except IndexError:
                    pass
            files[source_id] = {'available': native, 'directory': str(source_directory) if native else ''}
            group = groups.setdefault(record['collection'], {'directories': set(), 'all_native': True})
            group['all_native'] &= native
            if native:
                group['directories'].add(str(directory))
        folders = {}
        for collection, group in groups.items():
            available = group['all_native'] and len(group['directories']) == 1
            folders[collection] = {'available': available,
                                   'directory': next(iter(group['directories'])) if available else '',
                                   'multiple': len(group['directories']) > 1,
                                   'browser_upload': not group['all_native']}
        return {'files': files, 'folders': folders}

    def open_source(self, ident, collection=None, source_id=None):
        access = self.source_access(ident)
        record = access['files'].get(source_id) if source_id else access['folders'].get(collection)
        if not record or not record['available']:
            raise ValueError('No hay una ubicación original disponible. Elige la carpeta con Examinar en Windows o abre la ubicación de un archivo individual.')
        directory = Path(record['directory'])
        if not directory.is_dir():
            raise ValueError('La carpeta de origen ya no está disponible. Comprueba si se movió o si la unidad está conectada.')
        os.startfile(str(directory))
        return {'ok': True}


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    @property
    def app(self) -> Application:
        return self.server.app

    def log_message(self, fmt, *args):
        pass

    def send(self, data: bytes, content_type='application/json; charset=utf-8', status=200, extra=None):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob:; frame-src 'self' blob:; object-src 'none'; connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'self'")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def json(self, value, status=200):
        self.send(json.dumps(value, ensure_ascii=False).encode('utf-8'), status=status)

    def authorized(self):
        expected = f'127.0.0.1:{self.server.server_port}'
        if self.headers.get('Host') != expected:
            self.json({'error': 'Host no permitido.'}, 403)
            return False
        origin = self.headers.get('Origin')
        if origin and origin != 'http://' + expected:
            self.json({'error': 'Origen no permitido.'}, 403)
            return False
        supplied = self.headers.get('X-Magic-Token') or parse_qs(urlparse(self.path).query).get('token', [''])[0]
        if not hmac.compare_digest(supplied, self.app.token):
            self.json({'error': 'Sesión no autorizada. Recarga la aplicación.'}, 403)
            return False
        return True

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if self.headers.get('Host') != f'127.0.0.1:{self.server.server_port}':
                return self.json({'error': 'Host no permitido.'}, 403)
            if path == '/health':
                return self.json({'application': 'FiscalOps Magic ZIP', 'version': VERSION, 'installation': installation_id()})
            if path == '/':
                html = (STATIC / 'index.html').read_text(encoding='utf-8').replace('__SESSION_TOKEN__', self.app.token)
                return self.send(html.encode('utf-8'), 'text/html; charset=utf-8')
            if path in {'/app.js', '/experience.js', '/style.css', '/favicon.svg'}:
                file = STATIC / path[1:]
                return self.send(file.read_bytes(), mimetypes.guess_type(file.name)[0] or 'text/plain')
            if not self.authorized():
                return
            if path == '/api/session/watch':
                ident = parse_qs(parsed.query).get('client', [''])[0]
                self.app.client_presence(ident)
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Connection', 'close')
                self.end_headers()
                self.close_connection = True
                try:
                    while True:
                        with self.app.lock:
                            if ident not in self.app.clients:
                                break
                            self.app.clients[ident] = time.monotonic()
                        self.wfile.write(b'data: {}\n\n')
                        self.wfile.flush()
                        time.sleep(2)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                    pass
                finally:
                    self.app.client_presence(ident, closed=True)
                return
            if path == '/api/state':
                return self.json(self.app.state())
            if path.startswith('/api/job/'):
                return self.json(self.app.job(path.rsplit('/', 1)[1]))
            if path.startswith('/api/selection/'):
                return self.json(self.app.selection(path.rsplit('/', 1)[1]))
            if path.startswith('/api/file/'):
                ident = path.rsplit('/', 1)[1]
                with self.app.lock:
                    file = self.app.outputs.get(ident)
                    source = self.app.sources.get(ident)
                    file = file or (Path(source.path) if source else None)
                if not file or not file.is_file():
                    return self.json({'error': 'Archivo no encontrado.'}, 404)
                content_type = mimetypes.guess_type(file.name)[0] or 'application/octet-stream'
                safe_inline = file.suffix.lower() in {'.pdf', '.png', '.jpg', '.jpeg', '.webp', '.bmp', '.gif'} or ident in self.app.html_reports
                disposition = 'attachment' if parse_qs(parsed.query).get('download') or not safe_inline else 'inline'
                # Stream full files; thumbnails provide lightweight PDF previews.
                self.send_response(200)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(file.stat().st_size))
                self.send_header('Content-Disposition', f"{disposition}; filename*=UTF-8''{quote(file.name)}")
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Referrer-Policy', 'no-referrer')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.send_header('Content-Security-Policy', "sandbox; default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'" if ident in self.app.html_reports else "sandbox; default-src 'none'; base-uri 'none'")
                self.end_headers()
                with file.open('rb') as stream:
                    while block := stream.read(1024 * 1024):
                        self.wfile.write(block)
                return
            if path.startswith('/api/thumbnail/'):
                ident = path.rsplit('/', 1)[1]
                with self.app.lock:
                    source = self.app.sources.get(ident)
                    file = Path(source.path) if source else self.app.outputs.get(ident)
                if not file:
                    return self.json({'error': 'Archivo no encontrado.'}, 404)
                page_number = max(0, int(parse_qs(parsed.query).get('page', ['0'])[0]))
                from PIL import Image, ImageOps
                stream = io.BytesIO()
                if file.suffix.lower() == '.pdf':
                    import pypdfium2 as pdfium
                    with PDFIUM_LOCK:
                        doc = pdfium.PdfDocument(str(file))
                        try:
                            if not 0 <= page_number < len(doc):
                                raise ValueError('Página fuera de rango.')
                            page = doc[page_number]
                            try:
                                scale = min(1.4, 1000 / max(page.get_size()))
                                bitmap = page.render(scale=scale)
                                image = bitmap.to_pil()
                                try:
                                    image.save(stream, format='PNG')
                                finally:
                                    image.close()
                                    bitmap.close()
                            finally:
                                page.close()
                        finally:
                            doc.close()
                else:
                    with Image.open(file) as image:
                        image = ImageOps.exif_transpose(image)
                        image.thumbnail((900, 900))
                        image.convert('RGB').save(stream, format='PNG')
                return self.send(stream.getvalue(), 'image/png')
            self.json({'error': 'Ruta no encontrada.'}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            self.json({'error': str(exc)}, 400)

    def do_POST(self):
        if not self.authorized():
            self.close_connection = True
            return
        parsed = urlparse(self.path)
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 <= length <= 512 * 1024 * 1024:
                self.close_connection = True
                return self.json({'error': 'Máximo por archivo agregado desde el navegador: 512 MiB. Para ZIP mayores usa Agregar carpeta.'}, 413)
            if parsed.path == '/api/upload':
                original = parse_qs(parsed.query).get('name', ['archivo'])[0]
                relative = '/'.join(safe_name(p) for p in original.replace('\\', '/').split('/') if p not in {'', '.', '..'})
                folder = self.app.session / uuid.uuid4().hex
                folder.mkdir()
                with self.app.lock:
                    self.app.owned_directories.append(folder)
                file = folder / safe_name(relative.split('/')[-1])
                remaining = length
                with file.open('xb') as stream:
                    while remaining:
                        block = self.rfile.read(min(1024 * 1024, remaining))
                        if not block:
                            raise ValueError('La carga fue interrumpida.')
                        stream.write(block)
                        remaining -= len(block)
                source = inspect_source(file, relative, deep=False)
                pieces = relative.split('/')
                if len(pieces) > 1:
                    source.collection = '/'.join(pieces[:min(2, len(pieces) - 1)])
                    zip_pieces = [p for p in original.replace('\\', '/').split('/') if p not in {'', '.', '..'}]
                    source.zip_relative = '/'.join(zip_pieces[min(2, len(pieces) - 1):])
                with self.app.lock:
                    self.app.sources[source.id] = source
                return self.json(source.public())
            if length > 1024 * 1024:
                self.close_connection = True
                return self.json({'error': 'Solicitud demasiado grande.'}, 413)
            body = json.loads(self.rfile.read(length) or b'{}')
            if parsed.path in {'/api/session/ping', '/api/session/close'}:
                self.app.client_presence(body.get('client'), parsed.path.endswith('/close'))
                return self.json({'ok': True})
            if parsed.path == '/api/shutdown':
                self.app.cancel_active()
                def shutdown():
                    while True:
                        if not self.app.has_active_work():
                            break
                        time.sleep(.1)
                    self.server.shutdown()
                threading.Thread(target=shutdown, daemon=True).start()
                return self.json({'ok': True})
            if parsed.path == '/api/choose':
                if body.get('background'):
                    return self.json(self.app.begin_selection(kind=body.get('kind')))
                return self.json(self.app.choose(body.get('kind')))
            if parsed.path == '/api/source-folder':
                if body.get('background'):
                    return self.json(self.app.begin_selection(path=body.get('path', '')))
                return self.json(self.app.import_folder(body.get('path')))
            if parsed.path == '/api/cancel-selection':
                self.app.cancel_selection(body.get('id'))
                return self.json({'ok': True})
            if parsed.path == '/api/inspect':
                return self.json(self.app.inspect_requested_source(body.get('id')))
            if parsed.path == '/api/plan':
                with self.app.lock:
                    ids = body.get('ids', [])
                    if not isinstance(ids, list) or len(ids) != len(set(ids)) or any(k not in self.app.sources for k in ids):
                        raise ValueError('La selección contiene archivos no disponibles.')
                    sources = [self.app.sources[k] for k in ids]
                options = body.get('options') or {}
                return self.json(plan_sources(sources, int(options.get('limit_bytes', 4_000_000)), options.get('smart_pdf', True)))
            if parsed.path == '/api/destination':
                destination = Path(str(body.get('path', ''))).expanduser()
                if not destination.is_absolute():
                    raise ValueError('Escribe una ruta completa de Windows, por ejemplo C:\\Expedientes\\Salida.')
                if not destination.is_dir():
                    raise ValueError('La carpeta debe existir. Puedes crearla desde el Explorador y volver a seleccionarla.')
                self.app.destination = destination.resolve()
                return self.json(self.app.state())
            if parsed.path == '/api/remove':
                with self.app.lock:
                    for ident in body.get('ids', []):
                        self.app.sources.pop(ident, None)
                return self.json(self.app.state())
            if parsed.path == '/api/clear':
                return self.json(self.app.reset())
            if parsed.path == '/api/jobs':
                return self.json(self.app.start_job(body))
            if parsed.path == '/api/use-result':
                with self.app.lock:
                    job = self.app.jobs.get(body.get('job'))
                    if not job or not job.get('result'):
                        raise ValueError('Resultado no encontrado.')
                    outputs = [o for o in job['result']['outputs'] if o.get('kind') in {'pdf', 'image'}]
                added = [inventory_source(Path(o['path']), 'MagicPDF/' + o['name']) for o in outputs]
                with self.app.lock:
                    self.app.sources.update({s.id: s for s in added})
                return self.json({'state': self.app.state(), 'ids': [s.id for s in added]})
            if parsed.path == '/api/cancel':
                with self.app.lock:
                    job = self.app.jobs.get(body.get('id'))
                    if job:
                        job['cancel'].set()
                return self.json({'ok': True})
            if parsed.path == '/api/open-output':
                ident = body.get('job')
                with self.app.lock:
                    job = self.app.jobs.get(ident)
                    directory = Path(job['result']['directory']) if job and job.get('result') else self.app.destination
                if directory is None or not directory.exists():
                    raise ValueError('Aún no se ha creado la carpeta de salida.')
                os.startfile(str(directory))
                return self.json({'ok': True})
            if parsed.path == '/api/open-source':
                return self.json(self.app.open_source(body.get('job'), body.get('collection'), body.get('source_id')))
            return self.json({'error': 'Ruta no encontrada.'}, 404)
        except Exception as exc:
            self.json({'error': str(exc)}, 400)


def main():
    parser = argparse.ArgumentParser(description='Magic ZIP — powered by FiscalOps')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--exit-on-idle', action='store_true')
    args = parser.parse_args()
    for port in ([0] if args.port == 0 else range(args.port, args.port + 20)):
        try:
            server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
            break
        except OSError:
            continue
    else:
        raise SystemExit('No hay un puerto local disponible.')
    server.app = Application()
    server.daemon_threads = True
    stop_monitor = threading.Event()

    def monitor():
        while not stop_monitor.wait(2):
            with server.app.lock:
                now = time.monotonic()
                server.app.clients = {key: stamp for key, stamp in server.app.clients.items() if now - stamp < 180}
                absent = server.app.clients_seen and not server.app.clients
                if absent and server.app.last_client_left is None:
                    server.app.last_client_left = now
                cleanup = absent and now - (server.app.last_client_left or now) >= 6
                if cleanup:
                    server.app.cancel_active()
                    running = server.app.has_active_work()
                else:
                    running = True
            if cleanup and not running:
                try:
                    server.app.reset()
                except OSError:
                    continue
                if args.exit_on_idle:
                    server.shutdown()
                    return
                with server.app.lock:
                    server.app.clients_seen = False
                    server.app.last_client_left = None
    threading.Thread(target=monitor, daemon=True).start()
    url = f'http://127.0.0.1:{server.server_port}'
    print(f'Magic ZIP {VERSION} · Local: {url}', flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_monitor.set()
        server.app.cancel_active()
        workers = [j['thread'] for j in server.app.jobs.values() if j.get('thread')]
        if server.app.selection_task:
            workers.append(server.app.selection_task['thread'])
        for worker in workers:
            worker.join(timeout=3)
        server.server_close()
        try:
            server.app.reset()
            server.app.session.rmdir()
        except (OSError, ValueError):
            pass


if __name__ == '__main__':
    main()
