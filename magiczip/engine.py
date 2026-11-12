from __future__ import annotations

import hashlib
import io
import json
import math
import shutil
import time
import os
import re
import sys
import threading
import unicodedata
import uuid
import zipfile
import zlib
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from magiczip.runtime import RESOURCE_ROOT as ROOT, WORK_ROOT
sys.path.insert(0, str(ROOT / 'vendor'))
from PIL import Image, ImageOps
from pypdf import PdfReader, PdfWriter
from magiczip.office import (EXCEL_TYPES, POWERPOINT_TYPES, WORD_TYPES,
                             binary_eligibility, excel_semantics, office_capabilities,
                             recompress_excel, run_office, split_excel_values)
from magiczip.pdf_optimizer import PDFIUM_LOCK, adaptive_optimize, analyze_pdf, validate_pdf
from magiczip.pdf_book import is_book, split_book

MAX_PDF_BYTES = 512 * 1024 * 1024
IMAGE_TYPES = {'.jpg', '.jpeg', '.png', '.tif', '.tiff', '.bmp', '.webp'}
VIDEO_TYPES = {'.mp4', '.mov', '.avi', '.mkv', '.wmv', '.webm', '.m4v'}
OFFICE_TYPES = EXCEL_TYPES | POWERPOINT_TYPES | WORD_TYPES | {'.ods', '.odp'}
EXCEL_LARGE_BYTES = 4_000_000
PACKED_TYPES = VIDEO_TYPES | OFFICE_TYPES | {'.zip', '.7z', '.rar', '.gz', '.jpg', '.jpeg', '.png', '.mp3', '.aac'}


def source_collection(source) -> str:
    relative = source.relative.replace('\\', '/')
    return source.collection or (relative.split('/')[0] if '/' in relative else 'Archivos sueltos')


def pdf_zip_budget(limit: int, relative: str = '') -> int:
    # Include headers, UTF-8 names and worst-case DEFLATE overhead.
    reserve = max(1024, math.ceil(limit * .01), len(relative.encode('utf-8')) * 2 + 512)
    return max(1, limit - reserve)


def sample_compression(path: Path, size: int) -> float | None:
    if size < 262144:
        return None
    block_size = 65536
    positions = sorted({0, max(0, (size - block_size) // 2), max(0, size - block_size)})
    raw = compressed = 0
    with path.open('rb') as stream:
        for position in positions:
            stream.seek(position)
            block = stream.read(block_size)
            raw += len(block)
            compressed += len(zlib.compress(block, 6))
    return compressed / raw if raw else None


def manual_recommendation(source) -> str:
    suffix = Path(source.name).suffix.lower()
    if source.kind == 'pdf':
        if source.signed:
            return 'PDF firmado: conserva sus bytes. Solicita una versión o un medio de entrega admitido; no se corta ni recomprime automáticamente.'
        if source.encrypted:
            return 'PDF cifrado: prepara una copia desbloqueada autorizada y vuelve a agregarla.'
        return 'Revisa la página indicada en Magic PDF. Si permites reducir imágenes, revisa legibilidad antes del envío.'
    if suffix in VIDEO_TYPES:
        return 'Reduce resolución o bitrate en un editor local, o divide el video en segmentos reproducibles si el trámite lo permite. Vuelve a agregar las copias.'
    if suffix in OFFICE_TYPES:
        return 'Trabaja en una copia: comprime imágenes o separa hojas y anexos por secciones. Exporta a PDF solo si el trámite admite ese formato y verifica fórmulas y contenido.'
    if suffix in IMAGE_TYPES:
        return 'Redimensiona una copia o usa Imágenes a PDF y compresión de imágenes. Revisa sellos, folios y códigos.'
    if suffix in PACKED_TYPES:
        return 'El formato ya utiliza compresión. Reorganiza su contenido o prepara archivos independientes admitidos; recomprimir puede aportar poco.'
    return 'Prepara una copia más pequeña o separa su contenido con la herramienta del formato. No cortes bytes arbitrarios de un documento.'


def assess_source(source, limit: int, smart_pdf: bool = True) -> dict:
    budget = pdf_zip_budget(limit, source.relative)
    ratio = source.compression_ratio
    estimated = math.ceil(source.size * (ratio if ratio is not None else 1)) + 512
    can_edit = source.kind == 'pdf' and not (source.signed or source.encrypted or source.error)
    automatic = smart_pdf and can_edit and source.size >= limit
    manual_risk = not automatic and (source.size >= limit if source.kind == 'pdf' else estimated >= limit)
    if source.kind == 'pdf' and source.size < limit:
        estimated = min(estimated, source.size + 512)
    if not source.sha256:
        if source.pending_inspection:
            label, manual_risk = 'Inventario listo · se verificará al procesar', False
        else:
            label, manual_risk = 'No se pudo leer · revisión manual', True
    elif source.kind == 'pdf' and source.size < limit and not source.error:
        label = 'Aceptado sin cambios · PDF dentro del límite'
    elif automatic:
        label = 'Automático · optimizar PDF y cortar si aún no cabe'
    elif source.signed:
        label = 'Firma detectada · empaquetar intacto'
    elif source.encrypted:
        label = 'PDF cifrado · empaquetar intacto'
    elif source.kind == 'pdf' and source.error:
        label = 'PDF con incidencia · solo se intentará empaquetar intacto'
    elif manual_risk:
        label = 'Revisión manual probable · se comprobará el ZIP real'
    elif source.size >= limit:
        label = 'La muestra sugiere que puede caber al comprimir'
    else:
        label = 'Listo para empaquetar'
    pages_per_part = max(1, int(budget / max(1, source.size / max(1, source.pages))))
    parts = math.ceil(max(1, source.pages) / pages_per_part) if automatic else 1
    return {'id': source.id, 'label': label, 'automatic_pdf': automatic,
            'manual_risk': manual_risk, 'estimated_zip_bytes': estimated,
            'estimated_pdf_parts': parts, 'pdf_pages_per_part': pages_per_part,
            'recommendation': manual_recommendation(source) if manual_risk else '',
            'sample_ratio': ratio, 'needs_attention': bool(manual_risk or source.error)}


def plan_sources(sources, limit: int = 4_000_000, smart_pdf: bool = True) -> dict:
    if not 1000 <= limit <= 2_000_000_000:
        raise ValueError('Configura un límite entre 1 KB y 2 GB.')
    folders, assessments = {}, []
    for source in sources:
        key = source_collection(source)
        folder = folders.setdefault(key, {'name': key, 'files': 0, 'bytes': 0, 'pdfs': 0,
                                         'automatic_pdfs': 0, 'manual_risks': 0, 'estimated_zips': 0, '_sizes': []})
        folder['files'] += 1
        folder['bytes'] += source.size
        folder['pdfs'] += source.kind == 'pdf'
        assessment = assess_source(source, limit, smart_pdf)
        assessments.append(assessment)
        folder['automatic_pdfs'] += assessment['automatic_pdf']
        folder['manual_risks'] += assessment['manual_risk']
        if assessment['automatic_pdf']:
            remaining = max(1, source.pages)
            per_page = source.size / remaining
            while remaining:
                pages = min(assessment['pdf_pages_per_part'], remaining)
                folder['_sizes'].append(math.ceil(pages * per_page) + 512)
                remaining -= pages
        elif not assessment['manual_risk'] and (source.sha256 or source.pending_inspection):
            folder['_sizes'].append(assessment['estimated_zip_bytes'])
    for folder in folders.values():
        accumulated = 0
        for size in folder.pop('_sizes'):
            if accumulated and accumulated + size >= limit * .94:
                folder['estimated_zips'] += 1
                accumulated = 0
            accumulated += size
        if accumulated:
            folder['estimated_zips'] += 1
    return {'folders': list(folders.values()), 'assessments': assessments,
            'files': len(sources), 'bytes': sum(s.size for s in sources),
            'estimated_zips': sum(f['estimated_zips'] for f in folders.values()),
            'automatic_pdfs': sum(f['automatic_pdfs'] for f in folders.values()),
            'manual_risks': sum(f['manual_risks'] for f in folders.values()),
            'estimate_only': True}


class Cancelled(Exception):
    pass


def digest_file(path: Path, check: Callable = lambda: None) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        while block := f.read(1024 * 1024):
            check()
            h.update(block)
    return h.hexdigest()


def safe_name(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', unicodedata.normalize('NFC', value))
    value = value.strip(' .') or 'archivo'
    if value.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}:
        value = '_' + value
    return value[:160]


def zip_part_name(folder: str, index: int, total: int, style: str = 'numbers') -> str:
    base = safe_name(folder.replace('\\', '/').rsplit('/', 1)[-1])[:100]
    if total == 1:
        return f'{base}.zip'
    suffix = {'numbers': f'{index}-{total}', 'part': f'PARTE {index}-{total}',
              'part_of': f'PARTE {index} DE {total}'}[style]
    return f'{base}_{suffix}.zip'


@dataclass
class Source:
    id: str
    name: str
    relative: str
    path: str
    size: int
    sha256: str = ''
    kind: str = 'file'
    pages: int = 0
    signed: bool = False
    encrypted: bool = False
    forms: bool = False
    error: str = ''
    collection: str = ''
    compression_ratio: float | None = None
    zip_relative: str | None = None
    pdf_analysis: dict = field(default_factory=dict)
    pending_inspection: bool = False
    stat_signature: tuple = ()

    def public(self):
        d = asdict(self)
        del d['path']
        del d['zip_relative']
        del d['stat_signature']
        return d


def file_signature(path: Path) -> tuple:
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def inventory_source(path: Path, relative: str | None = None) -> Source:
    """Metadata only. Opening document contents belongs to the processing job."""
    suffix = path.suffix.lower()
    item = Source(uuid.uuid4().hex, path.name, relative or path.name, str(path), 0,
                  kind='pdf' if suffix == '.pdf' else 'image' if suffix in IMAGE_TYPES else 'file',
                  pending_inspection=True)
    try:
        item.stat_signature = file_signature(path)
        item.size = item.stat_signature[0]
    except OSError as exc:
        item.error, item.pending_inspection = str(exc), False
    return item


def inspect_source(path: Path, relative: str | None = None, *, deep=True, check=lambda: None) -> Source:
    item = Source(uuid.uuid4().hex, path.name, relative or path.name, str(path), 0)
    try:
        check()
        before = path.stat()
        item.size = before.st_size
        item.sha256 = digest_file(path, check)
        item.compression_ratio = sample_compression(path, item.size)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError('El archivo cambió durante el análisis. Vuelve a agregarlo.')
        suffix = path.suffix.lower()
        if suffix == '.pdf':
            item.kind = 'pdf'
            if item.size > MAX_PDF_BYTES:
                raise ValueError('PDF mayor a 512 MiB: esta versión permite empaquetarlo, pero no editarlo.')
            with path.open('rb') as f:
                reader = PdfReader(f)
                item.encrypted = reader.is_encrypted
                if item.encrypted:
                    # Encryption is a preservation constraint, not a corrupt file.
                    item.pages = 0
                else:
                    item.pages = len(reader.pages)
                    fields = reader.get_fields() or {}
                    item.forms = bool(fields)
                    item.signed = bool(reader.trailer['/Root'].get('/Perms')) or any(
                        field.get('/FT') == '/Sig' and field.get('/V') for field in fields.values()
                    )
                    if deep:
                        try:
                            item.pdf_analysis = analyze_pdf(reader, check)
                        except Cancelled:
                            raise
                        except Exception as exc:
                            item.pdf_analysis = {'type': 'unknown', 'analysis_error': str(exc)}
        elif suffix in IMAGE_TYPES:
            item.kind = 'image'
            with Image.open(path) as image:
                item.pages = getattr(image, 'n_frames', 1)
        check()
        item.stat_signature = file_signature(path)
        if item.stat_signature[:2] != (before.st_size, before.st_mtime_ns):
            raise ValueError('El archivo cambió durante el análisis. Vuelve a agregarlo.')
    except Cancelled:
        raise
    except Exception as exc:
        item.error = str(exc)
    return item


def scan_folder(folder: Path, excluded: list[Path] | None = None, *, inventory_only=False,
                check=lambda: None, progress=lambda *args: None):
    sources, errors = [], []
    excluded = [p.resolve() for p in (excluded or [])]
    def error(exc):
        errors.append({'path': str(exc.filename), 'reason': str(exc)})
    for parent, dirs, files in os.walk(folder, followlinks=False, onerror=error):
        check()
        empty = not dirs and not files
        allowed = []
        for name in dirs:
            p = Path(parent) / name
            try:
                if p.is_symlink() or getattr(p.lstat(), 'st_file_attributes', 0) & 0x400:
                    errors.append({'path': str(p), 'reason': 'Enlace o junction omitido para evitar ciclos.'})
                elif p.resolve() not in excluded and name not in {'.git', '.venv', '__pycache__', 'vendor'} and name.casefold() != 'zips finales':
                    allowed.append(name)
            except OSError as exc:
                error(exc)
        dirs[:] = sorted(allowed, key=str.casefold)
        for name in sorted(files, key=str.casefold):
            check()
            p = Path(parent) / name
            if p.is_symlink():
                errors.append({'path': str(p), 'reason': 'Enlace omitido.'})
            else:
                relative = p.relative_to(folder).as_posix()
                source = inventory_source(p, relative) if inventory_only else inspect_source(p, relative, check=check)
                child = source.relative.split('/')[0] if '/' in source.relative else ''
                source.collection = folder.name + ('/' + child if child else '')
                source.zip_relative = p.relative_to(folder / child if child else folder).as_posix()
                sources.append(source)
                progress(len(sources), source.size, str(p))
        if empty:
            p = Path(parent)
            relative = p.relative_to(folder)
            child = relative.parts[0] if relative.parts else ''
            zip_root = folder / child if child else folder
            sources.append(Source(uuid.uuid4().hex, p.name, relative.as_posix(), str(p), 0,
                                  sha256=hashlib.sha256(b'').hexdigest(), kind='directory',
                                  collection=folder.name + ('/' + child if child else ''),
                                  zip_relative=p.relative_to(zip_root).as_posix() if p != zip_root else ''))
    return sources, errors


def parse_pages(text: str, count: int) -> list[int]:
    if not text.strip():
        return list(range(count))
    result = []
    for segment in text.split(','):
        m = re.fullmatch(r'\s*(\d+)\s*(?:-\s*(\d+)\s*)?', segment)
        if not m:
            raise ValueError('Usa páginas como 1-3,5,8. El orden indicado se conserva.')
        first, last = int(m[1]), int(m[2] or m[1])
        if not 1 <= first <= count or not 1 <= last <= count:
            raise ValueError(f'Las páginas deben estar entre 1 y {count}.')
        step = 1 if last >= first else -1
        result.extend(range(first - 1, last - 1 + step, step))
    if len(result) > count * 10:
        raise ValueError('La selección repite demasiadas páginas.')
    return result


def reader_bytes(source: Source) -> bytes:
    if source.size > MAX_PDF_BYTES:
        raise ValueError('Máximo de edición por documento: 512 MiB.')
    data = Path(source.path).read_bytes()
    if hashlib.sha256(data).hexdigest() != source.sha256:
        raise ValueError('El original cambió desde el análisis. Vuelve a agregarlo.')
    if source.signed:
        raise ValueError('Firma digital detectada: se preserva el original; usa Magic ZIP para empaquetarlo.')
    if source.encrypted:
        raise ValueError('PDF cifrado: no se modifica.')
    if source.error:
        raise ValueError(source.error)
    return data


def writer_bytes(writer: PdfWriter) -> bytes:
    writer.compress_identical_objects(remove_duplicates=True, remove_unreferenced=True)
    stream = io.BytesIO()
    writer.write(stream)
    return stream.getvalue()


def optimize_pdf(data: bytes, lossy: bool, quality: int, edge: int, check: Callable, progress: Callable) -> tuple[bytes, list[str]]:
    reader = PdfReader(io.BytesIO(data))
    profile = analyze_pdf(reader, check)
    writer = PdfWriter()
    writer.clone_document_from_reader(reader)
    warnings, seen = [], set()
    for i, page in enumerate(writer.pages):
        check()
        progress('Optimizando páginas', i, len(writer.pages), f'Página {i + 1}')
        page.compress_content_streams(level=9)
        if lossy:
            for image in list(page.images):
                check()
                reference = image.indirect_reference
                if reference is None or reference.idnum in seen:
                    continue
                seen.add(reference.idnum)
                obj = reference.get_object()
                # Preserve transparency, masks and unusual color spaces.
                if obj.get('/SMask') or obj.get('/Mask'):
                    continue
                try:
                    original = image.image
                    if original.mode not in ('RGB', 'L'):
                        continue
                    page_profile = profile.get('pages', [])[i] if i < len(profile.get('pages', [])) else {}
                    page_kind = page_profile.get('type', 'mixed')
                    target_dpi = 300 if page_kind == 'scanned' else 260 if page_kind == 'mixed' else 220
                    target_box = (max(1, math.ceil(float(page.mediabox.width) * target_dpi / 72)),
                                  max(1, math.ceil(float(page.mediabox.height) * target_dpi / 72)))
                    target_box = (min(edge, target_box[0]), min(edge, target_box[1]))
                    if original.width <= target_box[0] and original.height <= target_box[1]:
                        continue
                    resized = original.copy()
                    resized.thumbnail(target_box, Image.Resampling.LANCZOS)
                    image.replace(resized, quality=quality, optimize=True)
                except Exception as exc:
                    warnings.append(f'Imagen de página {i + 1} conservada: {exc}')
    result = writer_bytes(writer)
    # Never replace a source with a larger "compressed" result.
    if len(result) < len(data):
        try:
            validate_pdf(data, result, check, lossy=lossy)
            return result, warnings
        except Cancelled:
            raise
        except Exception as exc:
            warnings.append('Copia optimizada descartada por verificación: ' + str(exc))
    return data, warnings


def range_bytes(reader: PdfReader, start: int, end: int) -> bytes:
    writer = PdfWriter()
    writer.append(reader, pages=(start, end), import_outline=False)
    if reader.metadata:
        writer.add_metadata({str(k): str(v) for k, v in reader.metadata.items() if v is not None})
    return writer_bytes(writer)


def strategic_parts(data: bytes, limit: int, check: Callable, progress: Callable, *, fits=None, optimize_page=None):
    """Contiguous, measured ranges; bookmark boundaries preferred above 85% fill.

    All outputs are remeasured. A single oversize page is reported explicitly;
    subsequent pages are still processed and the manifest records the gap.
    """
    reader = PdfReader(io.BytesIO(data))
    total, start, parts, pending = len(reader.pages), 0, [], []
    fits = fits or (lambda payload: len(payload) < limit)
    if fits(data):
        validate_pdf(data, data, check)
        return [(1, total, data)], []
    boundaries = set()
    def outlines(items):
        for item in items:
            if isinstance(item, list):
                outlines(item)
            else:
                try:
                    n = reader.get_destination_page_number(item)
                    if n is not None and 0 < n < total:
                        boundaries.add(n)
                except (ValueError, KeyError, AttributeError):
                    pass
    outlines(reader.outline)
    while start < total:
        check()
        progress('Midiendo cortes por páginas', start, total, f'Desde página {start + 1}')
        best = range_bytes(reader, start, start + 1)
        original_page_bytes, diagnosis = len(best), {}
        if not fits(best) and optimize_page:
            best, diagnosis = optimize_page(best, start + 1)
        if not fits(best):
            pending.append({'pages': [start + 1], 'original_page_bytes': original_page_bytes,
                            'processed_page_bytes': len(best), 'limit_bytes': limit,
                            'unit': 'MB decimal (1 MB = 1,000,000 bytes)', 'attempts': diagnosis.get('attempts', []),
                            'image_resources': analyze_pdf(PdfReader(io.BytesIO(best)), check),
                            'reason': f'La página {start + 1}, aislada como PDF válido, mide {original_page_bytes:,} bytes; '
                                      f'tras los intentos seguros mide {len(best):,} bytes. El PDF y su ZIP deben medir '
                                      f'menos de {limit:,} bytes ({limit / 1_000_000:g} MB decimales). '
                                      'No se encontró una versión que cumpla sin pérdida de detalle detectada.',
                            'recommendation': 'Conserva el original. Solicita una copia de mayor calidad de origen o un medio de entrega con mayor límite. '
                                              'No se segmentó visualmente la página; requiere autorización explícita y revisión documental.'})
            start += 1
            continue
        best_end, low, high = start + 1, start + 2, total
        while low <= high:
            check()
            end = (low + high) // 2
            candidate = range_bytes(reader, start, end)
            if fits(candidate):
                best, best_end, low = candidate, end, end + 1
            else:
                high = end - 1
        for boundary in sorted((b for b in boundaries if start < b < best_end), reverse=True):
            check()
            candidate = range_bytes(reader, start, boundary)
            if limit * .85 <= len(candidate) and fits(candidate):
                best, best_end = candidate, boundary
                break
        assert len(best) < limit
        validate_pdf(data, best, check, page_range=(start, best_end),
                     lossy=bool(diagnosis.get('quality_review')))
        parts.append((start + 1, best_end, best))
        start = best_end
    assert sum(end - begin + 1 for begin, end, _ in parts) + len(pending) == total
    return parts, pending


class Runner:
    def __init__(self, sources: list[Source], options: dict, destination: Path,
                 progress: Callable = lambda *args: None, cancel: threading.Event | None = None,
                 telemetry: Callable = lambda event: None, private_directory: Path | None = None,
                 source_cache=None, performance=None):
        self.sources, self.options = sources, options
        self.limit = int(options.get('limit_bytes', 4_000_000))
        if not 1_000 <= self.limit <= 2_000_000_000:
            raise ValueError('Configura un límite entre 1 KB y 2 GB.')
        self.naming = options.get('naming', 'numbers')
        if self.naming not in {'numbers', 'part', 'part_of'}:
            raise ValueError('Elige un formato válido para los nombres de los ZIP.')
        self.progress = progress
        self.cancel = cancel or threading.Event()
        self.started = time.perf_counter()
        self.source_cache = source_cache
        self.performance = performance or {}
        self.phase_seconds, self.phase_started, self.last_phase = {}, self.started, 'analyze'
        final_root = destination if destination.name.casefold() == 'zips finales' else destination / 'ZIPS FINALES'
        self.directory = final_root / (datetime.now().strftime('Envio_%Y%m%d_%H%M%S_') + uuid.uuid4().hex[:6])
        self.directory.mkdir(parents=True, exist_ok=False)
        self.private_directory = private_directory or WORK_ROOT / 'processing' / uuid.uuid4().hex
        self.private_directory.mkdir(parents=True, exist_ok=False)
        self.folders, self.active_folder = [], None
        self.outputs, self.pending, self.warnings = [], [], []
        self.preparation, self.provenance = [], {}
        self.pdf_decisions = []
        self.office_preparation, self.timeline = [], []
        self.telemetry = telemetry
        self.statistics = {'input_files': len(sources), 'input_bytes': sum(s.size for s in sources),
                           'pdfs_optimized': 0, 'pdf_parts': 0, 'zips_verified': 0, 'pending': 0,
                           'excel_optimized': 0, 'excel_split': 0,
                           'powerpoints_converted': 0, 'words_converted': 0,
                           'packing_strategy': 'best_fit_decreasing'}

    def report(self, phase, stage, current=0, total=1, detail='', event=None):
        self.check()
        if phase != self.last_phase:
            now = time.perf_counter()
            self.phase_seconds[self.last_phase] = self.phase_seconds.get(self.last_phase, 0.) + now - self.phase_started
            self.phase_started, self.last_phase = now, phase
        self.progress(stage, current, total, detail)
        self.statistics['pending'] = len(self.pending)
        if event:
            self.timeline.append({'time':datetime.now().isoformat(timespec='seconds'),'phase':phase,'text':event})
        if self.active_folder:
            self.active_folder.update(phase=phase, current=current, total=total, stage=stage)
        self.telemetry({'phase': phase, 'statistics': dict(self.statistics), 'event': event,
                        'folders': self.folders, 'active_folder': self.active_folder['name'] if self.active_folder else None})

    def check(self):
        if self.cancel.is_set():
            raise Cancelled('Proceso cancelado. Los originales no se modificaron.')

    def store(self, data: bytes, name: str, **extra):
        self.check()
        path = self.directory / safe_name(name)
        counter = 1
        while path.exists():
            counter += 1
            path = self.directory / f'{safe_name(Path(name).stem)}_{counter}{Path(name).suffix}'
        temp = path.with_name(path.name + '.partial')
        try:
            with temp.open('xb') as stream:
                stream.write(data)
            if path.suffix.lower() == '.pdf':
                with temp.open('rb') as stream:
                    reader = PdfReader(stream)
                    page_count = len(reader.pages)
                if 'pages' in extra and extra['pages'] != page_count:
                    raise ValueError('La cantidad de páginas resultante no coincide.')
                extra['pages'] = page_count
            fingerprint = digest_file(temp, self.check)
            self.check()
            temp.rename(path)
            entry = {'name': path.name, 'path': str(path), 'bytes': path.stat().st_size,
                     'sha256': fingerprint, 'within_limit': path.stat().st_size < self.limit,
                     'verified': True, **extra}
            self.outputs.append(entry)
            return entry
        finally:
            temp.unlink(missing_ok=True)

    def emit_pdf(self, data: bytes, name: str, origin: list[str], force_split=False):
        if len(data) < self.limit:
            self.store(data, f'{name}.pdf', sources=origin, kind='pdf')
        elif force_split or self.options.get('auto_split', True):
            parts = None
            pages = len(PdfReader(io.BytesIO(data)).pages)
            if is_book(Source('', name, name, '', len(data), pages=pages), self.limit):
                try:
                    parts, pending, _ = split_book(data, self.limit, lambda payload: len(payload) < self.limit,
                                                   self.check, self.progress, stem=name)
                except Cancelled:
                    raise
                except Exception:
                    parts = None
            if parts is None:
                parts, pending = strategic_parts(data, self.limit, self.check, self.progress)
            for index, (begin, end, payload) in enumerate(parts, 1):
                self.store(payload, f'{name}_{index}-{len(parts)}.pdf', sources=origin,
                           kind='pdf', pages=end - begin + 1, page_range=[begin, end])
            self.pending.extend({'sources': origin, **p} for p in pending)
            if parts:
                self.warnings.append('Las partes conservan páginas completas; revisa vínculos internos, marcadores y formularios antes del envío.')
        else:
            self.store(data, f'{name}_REVISAR_LIMITE.pdf', sources=origin, kind='pdf')
            self.pending.append({'sources': origin, 'reason': 'El resultado supera el límite. Usa Dividir por tamaño.'})

    def run(self, action: str):
        if not self.sources:
            raise ValueError('Selecciona al menos un archivo.')
        status = 'completed'
        try:
            if action == 'zip':
                self.make_zip()
            elif action == 'merge':
                self.merge()
            elif action == 'images_pdf':
                self.images_pdf()
            else:
                allowed = {'compress', 'split', 'extract', 'rotate', 'pdf_images'}
                if action not in allowed:
                    raise ValueError('Herramienta desconocida.')
                for index, source in enumerate(self.sources):
                    self.check()
                    self.progress('Procesando documentos', index, len(self.sources), source.name)
                    try:
                        self.process_one(action, source)
                    except Cancelled:
                        raise
                    except Exception as exc:
                        self.pending.append({'source': source.relative, 'reason': str(exc)})
        except Cancelled:
            status = 'cancelled'
        except Exception as exc:
            status = 'error'
            self.pending.append({'reason': str(exc)})
        if self.active_folder and self.active_folder['status'] == 'running':
            self.active_folder['status'] = status
        for output in self.outputs:
            if not output['within_limit'] and not any(p.get('output') == output['name'] for p in self.pending):
                self.pending.append({'output': output['name'], 'reason': f"{output['name']} supera el límite configurado; no está listo como adjunto."})
        if status == 'completed' and self.pending:
            status = 'review'
        self.statistics['pending'] = len(self.pending)
        self.phase_seconds[self.last_phase] = self.phase_seconds.get(self.last_phase, 0.) + time.perf_counter() - self.phase_started
        result = {'status': status, 'action': action, 'limit_bytes': self.limit,
                  'performance': {**self.performance, 'phase_seconds': {k: round(v, 6) for k, v in self.phase_seconds.items()}},
                  'directory': str(self.directory), 'outputs': self.outputs, 'pending': self.pending,
                  'warnings': list(dict.fromkeys(self.warnings)),
                  'preparation': self.preparation, 'statistics': self.statistics, 'office_preparation':self.office_preparation,
                  'pdf_decisions': self.pdf_decisions,
                  'folders': self.folders,
                  'timeline':self.timeline,'elapsed_seconds':round(time.perf_counter()-self.started,2),'version':'2.0.0','creator':'LAVA',
                  'sources': [s.public() for s in self.sources], 'originals_modified': False}
        report = self.private_directory / 'reporte_magiczip.json'
        report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        result['report_path'] = str(report)
        from magiczip.reporting import write_reports
        result['reports'] = write_reports(result, self.private_directory)
        report.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        return result

    def process_one(self, action: str, source: Source):
        if source.kind != 'pdf':
            raise ValueError('Esta herramienta requiere PDF. Usa Imágenes a PDF o Magic ZIP.')
        data = reader_bytes(source)
        name = safe_name(Path(source.name).stem)
        if action == 'compress':
            quality = int(self.options.get('quality', 80))
            edge = int(self.options.get('max_edge', 2000))
            if not 25 <= quality <= 95 or not 600 <= edge <= 4000:
                raise ValueError('Parámetros de calidad fuera de rango.')
            if is_book(source, self.limit) and self.options.get('auto_split', True):
                self.emit_pdf(data, name, [source.relative], force_split=True)
                return
            optimized, warnings = optimize_pdf(data, bool(self.options.get('lossy')), quality, edge, self.check, self.progress)
            self.warnings.extend(warnings)
            if optimized == data:
                self.warnings.append(f'{source.name}: no se obtuvo una reducción adicional; se conserva el contenido de entrada.')
            self.emit_pdf(optimized, name + '_optimizado', [source.relative])
        elif action == 'split':
            self.emit_pdf(data, name, [source.relative], force_split=True)
        elif action in {'extract', 'rotate'}:
            reader = PdfReader(io.BytesIO(data))
            pages = parse_pages(str(self.options.get('pages', '')), len(reader.pages))
            writer = PdfWriter()
            if action == 'extract':
                writer.append(reader, pages=pages, import_outline=False)
            else:
                writer.clone_document_from_reader(reader)
                angle = int(self.options.get('angle', 90))
                if angle not in (90, 180, 270):
                    raise ValueError('Rotación permitida: 90, 180 o 270 grados.')
                for n in set(pages):
                    self.check()
                    writer.pages[n].rotate(angle)
            self.emit_pdf(writer_bytes(writer), name + ('_paginas' if action == 'extract' else '_rotado'), [source.relative])
        elif action == 'pdf_images':
            import pypdfium2 as pdfium
            reader = PdfReader(io.BytesIO(data))
            pages = parse_pages(str(self.options.get('pages', '')), len(reader.pages))
            document = pdfium.PdfDocument(data)
            try:
                for index, n in enumerate(pages):
                    self.check()
                    self.progress('Exportando páginas a PNG', index, len(pages), f'Página {n + 1}')
                    page = document[n]
                    try:
                        scale = min(2, 2400 / max(page.get_size()))
                        bitmap = page.render(scale=scale)
                        image = bitmap.to_pil()
                        try:
                            stream = io.BytesIO()
                            image.save(stream, format='PNG', optimize=True)
                            self.store(stream.getvalue(), f'{name}_p{n + 1:03d}.png', kind='image', sources=[source.relative])
                        finally:
                            image.close()
                            bitmap.close()
                    finally:
                        page.close()
            finally:
                document.close()

    def merge(self):
        writer = PdfWriter()
        keep_alive = []
        for index, source in enumerate(self.sources):
            self.check()
            self.progress('Uniendo documentos', index, len(self.sources), source.name)
            if source.kind != 'pdf':
                raise ValueError('Selecciona solamente PDF para unir.')
            reader = PdfReader(io.BytesIO(reader_bytes(source)))
            keep_alive.append(reader)
            writer.append(reader, outline_item=source.name)
        self.emit_pdf(writer_bytes(writer), 'Documentos_unidos', [s.relative for s in self.sources])

    def images_pdf(self):
        images = []
        try:
            for index, source in enumerate(self.sources):
                self.check()
                self.progress('Convirtiendo imágenes', index, len(self.sources), source.name)
                if source.kind != 'image':
                    raise ValueError('Selecciona solamente imágenes para esta herramienta.')
                data = reader_bytes(source)
                with Image.open(io.BytesIO(data)) as original:
                    if original.width * original.height > 60_000_000:
                        raise ValueError('Imagen mayor a 60 megapíxeles: redimensiónala primero.')
                    for n in range(getattr(original, 'n_frames', 1)):
                        self.check()
                        original.seek(n)
                        image = ImageOps.exif_transpose(original).convert('RGBA')
                        image.thumbnail((2400, 2400), Image.Resampling.LANCZOS)
                        background = Image.new('RGB', image.size, 'white')
                        background.paste(image, mask=image.getchannel('A'))
                        images.append(background)
            stream = io.BytesIO()
            images[0].save(stream, format='PDF', save_all=True, append_images=images[1:], resolution=150, quality=88)
            self.emit_pdf(stream.getvalue(), 'Imagenes_convertidas', [s.relative for s in self.sources])
        finally:
            for image in images:
                image.close()

    def prepare_office_sources(self, sources=None):
        prepared = []
        capabilities = office_capabilities()
        for source in (self.sources if sources is None else sources):
            self.check()
            if source.kind == 'directory':
                prepared.append(source)
                continue
            suffix = Path(source.name).suffix.lower()
            excel = suffix in EXCEL_TYPES and self.options.get('optimize_excel',True)
            powerpoint = suffix in POWERPOINT_TYPES and self.options.get('convert_powerpoint',True)
            word = suffix in WORD_TYPES and self.options.get('convert_word', True)
            if not (excel or powerpoint or word):
                prepared.append(source); continue
            office_kind = 'excel' if excel else 'powerpoint' if powerpoint else 'word'
            record = {'source':source.relative,'source_id':source.id,'original_bytes':source.size,
                      'source_sha256':source.sha256,'kind':office_kind,'status':'unchanged','notes':[]}
            self.office_preparation.append(record)
            folder = self.private_directory / 'COPIAS OPTIMIZADAS' / source.id[:12]
            folder.mkdir(parents=True,exist_ok=False)
            snapshot = folder / ('_entrada' + suffix)
            try:
                self.report('office', 'Preparando Excel' if excel else 'Convirtiendo PowerPoint' if powerpoint else 'Convirtiendo Word', 0, 1, source.name,
                            f'Office: preparando copia de {source.relative}')
                with Path(source.path).open('rb') as src, snapshot.open('xb') as dst:
                    while block := src.read(1024*1024):
                        self.check(); dst.write(block)
                if digest_file(snapshot,self.check) != source.sha256:
                    raise ValueError('El archivo cambió desde el análisis. Vuelve a seleccionarlo.')
                stem = safe_name(Path(source.name).stem)[:90]
                chosen = None
                if excel:
                    split_items = []
                    large_split = (source.size > EXCEL_LARGE_BYTES and
                                   self.options.get('excel_split_large', True) and
                                   suffix == '.xlsx' and capabilities['excel'])
                    if large_split:
                        split_dir = folder / 'POR_PESTANAS'
                        split_result = split_excel_values(snapshot, split_dir, self.check,
                            lambda detail: self.report('office', 'Separando Excel por pestañas', 0, 1, source.name, detail))
                        total = len(split_result['files'])
                        record['strategy'] = 'values_per_worksheet'
                        record['verification'] = split_result.get('verification', 'native_values_one_workbook_per_worksheet')
                        record['notes'].append('Libro mayor de 4 MB convertido a valores actuales y separado por pestañas; las fórmulas no viajan a estas copias.')
                        record['worksheets'] = []
                        for index, part in enumerate(split_result['files'], 1):
                            self.check()
                            output_name = f'{stem}_{index}-{total}.xlsx'
                            part_path = Path(part['path'])
                            final = folder / output_name
                            part_path.rename(final)
                            relative = (Path(source.relative).parent / output_name).as_posix()
                            item = inspect_source(final, relative)
                            if item.error:
                                raise ValueError(item.error)
                            item.collection = source_collection(source)
                            if source.zip_relative is not None:
                                item.zip_relative = (Path(source.zip_relative).parent / output_name).as_posix()
                            self.provenance[item.id] = {'source': source.relative, 'source_id': source.id,
                                'source_sha256': source.sha256, 'office_conversion': 'excel_values_per_worksheet',
                                'worksheet': part.get('worksheet', index)}
                            split_items.append(item)
                            record['worksheets'].append({'worksheet': part.get('worksheet', index),
                                'output_name': output_name, 'bytes': item.size, 'sha256': item.sha256})
                        prepared.extend(split_items)
                        record.update(status='prepared', output_name=', '.join(i.name for i in split_items),
                                      bytes=sum(i.size for i in split_items), sha256=[i.sha256 for i in split_items])
                        self.statistics['excel_split'] += 1
                        self.report('office', 'Excel separado y verificado', total, total, source.name,
                                    f'{source.name}: {total} pestaña(s) en archivos {stem}_1-{total}.xlsx')
                        continue
                    if zipfile.is_zipfile(snapshot):
                        candidate = folder / (stem + '_optimizado' + suffix)
                        compression = recompress_excel(snapshot,candidate,self.check)
                        record['notes'].append(compression['reason'])
                        if compression['accepted']:
                            chosen = candidate
                            record['verification'] = compression['verification']
                        eligible, reason = binary_eligibility(snapshot if suffix=='.xlsx' else Path(source.path))
                        if self.options.get('excel_binary',True) and eligible and capabilities['excel']:
                            binary = folder / (stem + '.xlsb')
                            roundtrip = binary.with_suffix('.roundtrip.xlsx')
                            try:
                                native = run_office('excel_binary',snapshot,binary,self.check,
                                    lambda detail:self.report('office','Validando copia binaria de Excel',0,1,source.name))
                                if excel_semantics(snapshot) != excel_semantics(roundtrip):
                                    raise ValueError('La comprobación de retorno detectó diferencias en contenido, formato o estructura. Se descarta XLSB.')
                                if binary.stat().st_size < (chosen.stat().st_size if chosen else source.size):
                                    if chosen: chosen.unlink()
                                    chosen = binary
                                    record['verification'] = 'native_values_formulas_and_ooxml_roundtrip'
                                    record['native_checks'] = native.get('snapshot',{})
                                    record['notes'].append('XLSB reabierto y contrastado con datos, fórmulas y estructura del XLSX original.')
                                else:
                                    binary.unlink(missing_ok=True)
                                    record['notes'].append('XLSB no mejoró el tamaño de la copia sin pérdida; no se utiliza.')
                            except Cancelled:
                                raise
                            except Exception as exc:
                                binary.unlink(missing_ok=True)
                                record['notes'].append('XLSB descartado: '+str(exc))
                            finally:
                                roundtrip.unlink(missing_ok=True)
                        elif self.options.get('excel_binary',True):
                            record['notes'].append(reason or 'Excel nativo no está disponible para validar una copia XLSB.')
                    else:
                        record['notes'].append('Excel binario antiguo, cifrado o no reconocido: se conserva íntegro para no arriesgar contenido.')
                    if chosen:
                        self.statistics['excel_optimized'] += 1
                elif powerpoint:
                    if not capabilities['powerpoint']:
                        raise ValueError('Se requiere PowerPoint instalado para convertir esta presentación con fidelidad.')
                    if zipfile.is_zipfile(snapshot):
                        with zipfile.ZipFile(snapshot) as archive:
                            for name in archive.namelist():
                                if name.endswith('.rels'):
                                    import xml.etree.ElementTree as ET
                                    for relation in ET.fromstring(archive.read(name)):
                                        if relation.get('TargetMode')=='External' and not relation.get('Type','').endswith('/hyperlink'):
                                            raise ValueError('La presentación contiene contenido externo vinculado. Revísalo antes de convertir para evitar una copia incompleta.')
                    chosen = folder / (stem + '.pdf')
                    native = run_office('powerpoint_pdf',snapshot,chosen,self.check,
                        lambda detail:self.report('office','Exportando PowerPoint a PDF',0,1,source.name))
                    with chosen.open('rb') as stream:
                        if len(PdfReader(stream).pages) != native['slides']:
                            raise ValueError('La cantidad de páginas del PDF no coincide con las diapositivas de la presentación.')
                    record['verification']='native_pdf_slide_count'
                    record['slides']=native['slides']
                    record['notes'].append('PDF estático con todas las diapositivas, incluidas las ocultas. Notas, animaciones y contenido interactivo permanecen en el PPT original.')
                    self.statistics['powerpoints_converted'] += 1
                else:
                    if not capabilities['word']:
                        raise ValueError('Se requiere Word instalado para convertir este documento a PDF con fidelidad.')
                    chosen = folder / (stem + '.pdf')
                    native = run_office('word_pdf', snapshot, chosen, self.check,
                        lambda detail: self.report('office', 'Exportando Word a PDF', 0, 1, source.name, detail))
                    with chosen.open('rb') as stream:
                        page_count = len(PdfReader(stream).pages)
                    if page_count < 1:
                        raise ValueError('Word no generó un PDF con páginas verificables.')
                    record['verification'] = 'native_word_export'
                    record['pages'] = page_count
                    record['notes'].append('PDF estático generado desde Word; el documento original permanece intacto.')
                    self.statistics['words_converted'] += 1
                if chosen:
                    item = inspect_source(chosen,(Path(source.relative).parent/chosen.name).as_posix())
                    if item.error: raise ValueError(item.error)
                    item.collection=source_collection(source)
                    if source.zip_relative is not None:
                        item.zip_relative = (Path(source.zip_relative).parent / chosen.name).as_posix()
                    self.provenance[item.id]={'source':source.relative,'source_id':source.id,'source_sha256':source.sha256,
                                              'office_conversion':'excel' if excel else 'powerpoint_pdf' if powerpoint else 'word_pdf'}
                    prepared.append(item)
                    record.update(status='prepared',output_name=chosen.name,path=str(chosen),bytes=item.size,sha256=item.sha256)
                else:
                    prepared.append(source)
                    record['notes'].append('El original es la mejor opción verificada disponible y se empaqueta sin modificarlo.')
                self.report('office','Copia Office verificada',1,1,source.name,f'{source.name}: '+('copia preparada' if chosen else 'se conserva el original'))
            except Cancelled:
                raise
            except Exception as exc:
                record.update(status='unchanged',reason=str(exc))
                self.warnings.append(f'{source.name}: '+str(exc)+' Se intenta empaquetar el original íntegro.')
                if powerpoint or word:
                    kind_name = 'PowerPoint' if powerpoint else 'Word'
                    self.pending.append({'source':source.relative,'reason':f'No se completó la conversión de {kind_name} a PDF: '+str(exc),
                                         'recommendation':f'Conserva el {kind_name} original. Revísalo en Office, exporta una copia a PDF y vuelve a procesarlo.'})
                prepared.append(source)
            finally:
                try: snapshot.unlink(missing_ok=True)
                except PermissionError: record['notes'].append('Una copia temporal sigue bloqueada por Office; no se modificó el original.')
        return prepared

    def _pdf_fits(self, payload, name):
        if len(payload) >= self.limit:
            return False
        # With ample space for DEFLATE/ZIP headers, defer actual compression to
        # the final writer. Near the boundary we still measure a real ZIP.
        if len(payload) < pdf_zip_budget(self.limit, name):
            return True
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            with archive.open(name, 'w', force_zip64=True) as entry:
                entry.write(payload)
        return stream.tell() < self.limit

    def prepare_zip_sources(self, sources=None):
        prepared = []
        sources = self.sources if sources is None else sources
        for index, source in enumerate(sources):
            self.report('analyze', 'Analizando el expediente', index, len(sources), source.relative)
            if source.kind != 'pdf':
                prepared.append(source)
                continue
            record = {'source': source.relative, 'source_id': source.id, 'source_sha256': source.sha256,
                      'original_bytes': source.size, 'processed_bytes': source.size,
                      'limit_bytes': self.limit, 'unit': 'MB decimal (1 MB = 1,000,000 bytes)',
                      'analysis': source.pdf_analysis, 'attempts': [], 'parts': [],
                      'status': 'accepted', 'decision': 'accepted_unchanged',
                      'collection': source_collection(source)}
            self.pdf_decisions.append(record)
            protected = source.signed or source.encrypted or source.forms
            if source.error or not source.sha256:
                record.update(status='review', decision='review', reason=source.error or 'No se pudo leer el PDF.')
                self.pending.append({'source': source.relative, 'source_id': source.id, 'reason': record['reason'],
                                     'original_bytes': source.size, 'limit_bytes': self.limit,
                                     'recommendation': manual_recommendation(source)})
                # An unreadable/corrupt PDF is never declared successfully processed.
                continue
            if source.size < self.limit:
                try:
                    if not source.encrypted:
                        data = Path(source.path).read_bytes()
                        if hashlib.sha256(data).hexdigest() != source.sha256:
                            raise ValueError('El original cambió desde el análisis. Vuelve a agregarlo.')
                        verification = self.source_cache.validation(source.sha256) if self.source_cache else None
                        record['validation_cache_hit'] = verification is not None
                        if verification is None:
                            verification = validate_pdf(data, data, self.check)
                            if self.source_cache:
                                self.source_cache.remember_validation(source.sha256, verification)
                        record['verification'] = verification
                    prepared.append(source)
                except Cancelled:
                    raise
                except Exception as exc:
                    record.update(status='review', decision='review', reason=str(exc))
                    self.pending.append({'source': source.relative, 'source_id': source.id,
                                         'reason': 'No se pudo validar el PDF original: ' + str(exc),
                                         'original_bytes': source.size, 'limit_bytes': self.limit,
                                         'recommendation': manual_recommendation(source)})
                continue
            if protected or not self.options.get('smart_pdf', True):
                reason = ('PDF firmado, cifrado o con formularios: se conserva el original sin transformaciones.' if protected
                          else 'La optimización automática está desactivada.')
                record.update(status='review', decision='review', reason=reason)
                self.pending.append({'source': source.relative, 'source_id': source.id,
                                     'original_bytes': source.size, 'processed_bytes': source.size,
                                     'limit_bytes': self.limit, 'unit': record['unit'],
                                     'reason': f'{reason} Mide {source.size:,} bytes; el límite individual es menor de {self.limit:,} bytes '
                                               f'({self.limit / 1_000_000:g} MB decimales). La compresión ZIP no sustituye este requisito.',
                                     'recommendation': manual_recommendation(source)})
                continue
            record.update(status='preparing', pdf_budget_bytes=self.limit)
            self.preparation.append(record)
            origin = self.provenance.get(source.id, {})
            stem = safe_name(Path(source.name).stem)[:100]
            output_name = f'{stem}_optimizado.pdf'
            zip_parent = Path(source.zip_relative if source.zip_relative is not None else source.relative).parent
            fits = lambda payload: self._pdf_fits(payload, (zip_parent / output_name).as_posix())
            stage_labels = {'lossless': 'Comprimiendo PDF sin pérdida', 'images_lossless': 'Comprimiendo imágenes sin pérdida',
                            'images': 'Optimizando imágenes del PDF', 'resolution': 'Ajustando resolución del PDF',
                            'grayscale': 'Evaluando escala de grises', 'bilevel': 'Evaluando blanco y negro',
                            'aggressive': 'Aplicando reducción adicional'}
            progress = lambda stage, current, total, detail: self.report('pdf', stage_labels.get(stage, stage), current, total, source.name + ' · ' + detail)
            try:
                data = reader_bytes(source)
                cache_key = (source.sha256, self.limit, bool(self.options.get('zip_lossy', False)),
                             source.name, source.zip_relative or source.relative)
                cached = self.source_cache.optimized(cache_key) if self.source_cache else None
                cached_parts = []
                if cached:
                    try:
                        for part in cached['parts']:
                            self.check()
                            payload = Path(part['path']).read_bytes()
                            if hashlib.sha256(payload).hexdigest() != part['sha256'] or not fits(payload):
                                cached = None
                                break
                            cached_parts.append((*part['page_range'], payload))
                    except OSError:
                        cached = None
                if cached:
                    record.update({k: v for k, v in cached.items() if k not in
                                   {'source', 'source_id', 'source_sha256', 'collection', 'parts', 'status', 'decision'}})
                    record.update(optimization_cache_hit=True, parts=[])
                    parts, pending = cached_parts, []
                    self.report('pdf', 'Reutilizando PDF verificado', 1, 1, source.name)
                    if record.get('reduced'):
                        self.statistics['pdfs_optimized'] += 1
                else:
                    book_result = None
                    if is_book(source, self.limit):
                        self.report('split', 'Dividiendo el libro sin recodificar sus páginas', 0, source.pages, source.name)
                        try:
                            book_result = split_book(data, self.limit, fits, self.check,
                                lambda stage, current, total, detail: self.report('pdf' if stage in stage_labels else 'verify' if stage == 'Verificando páginas intactas' else 'split', stage_labels.get(stage, stage), current, total, source.name + ' · ' + detail),
                                allow_aggressive=bool(self.options.get('zip_lossy', False)), stem=stem)
                        except Cancelled:
                            raise
                        except Exception as exc:
                            record['fast_route_fallback'] = str(exc)
                    if book_result is not None:
                        parts, pending, diagnosis = book_result
                        record.update(diagnosis)
                        record['optimized_bytes'] = record['processed_bytes']
                        self.pending.extend({'source':source.relative, 'source_id':source.id, **item} for item in pending)
                    else:
                        optimized, diagnosis = adaptive_optimize(data, fits, self.check, progress,
                            allow_aggressive=bool(self.options.get('zip_lossy', False)))
                        record.update(diagnosis)
                        record.update(optimized_bytes=len(optimized), reduced=len(optimized) < len(data),
                                      grayscale=any(a['stage'] in ('grayscale', 'bilevel') and a.get('status') == 'accepted'
                                                    for a in record['attempts']))
                        if record['reduced']:
                            self.statistics['pdfs_optimized'] += 1
                        if not fits(optimized):
                            def optimize_page(payload, page):
                                result, diagnostics = adaptive_optimize(payload, fits, self.check, progress,
                                    allow_aggressive=bool(self.options.get('zip_lossy', False)))
                                record.setdefault('page_attempts', []).append({'page': page, **diagnostics})
                                return result, diagnostics
                            parts, pending = strategic_parts(optimized, self.limit, self.check,
                                lambda stage, current, total, detail: self.report('split', stage, current, total, source.name + ' · ' + detail),
                                fits=fits, optimize_page=optimize_page)
                            self.pending.extend({'source': source.relative, 'source_id': source.id,
                                                 'original_bytes': source.size, 'processed_bytes': len(optimized), **p} for p in pending)
                            record['split'] = len(parts) > 1 or bool(pending) or any(begin != 1 or end != source.pages for begin, end, _ in parts)
                        else:
                            parts, pending = [(1, source.pages, optimized)], []
                            record['split'] = False
                folder = self.private_directory / 'PDF_preparados' / source.id[:12]
                folder.mkdir(parents=True, exist_ok=False)
                new_items = []
                for part_index, (begin, end, payload) in enumerate(parts, 1):
                    self.check()
                    filename = f'{stem}_{part_index}-{len(parts)}.pdf' if record['split'] else output_name
                    path = folder / filename
                    partial = path.with_suffix('.partial')
                    try:
                        with partial.open('xb') as stream:
                            stream.write(payload)
                        if len(PdfReader(io.BytesIO(payload)).pages) != end - begin + 1 or len(payload) >= self.limit:
                            raise ValueError('Falló la comprobación de páginas o del límite individual del PDF.')
                        fingerprint = digest_file(partial, self.check)
                        self.check()
                        partial.rename(path)
                    finally:
                        partial.unlink(missing_ok=True)
                    relative = (Path(source.relative).parent / filename).as_posix()
                    item = Source(uuid.uuid4().hex, filename, relative, str(path), len(payload),
                                  sha256=fingerprint, kind='pdf', pages=end - begin + 1,
                                  collection=source_collection(source),
                                  zip_relative=(zip_parent / filename).as_posix() if source.zip_relative is not None else None)
                    new_items.append(item)
                    self.provenance[item.id] = {'source': source.relative, 'source_id': source.id,
                                               'source_sha256': source.sha256, **origin, 'page_range': [begin, end]}
                    record['parts'].append({'name': filename, 'path': str(path), 'bytes': len(payload),
                                            'pages': end - begin + 1, 'page_range': [begin, end],
                                            'sha256': fingerprint, 'kind': 'pdf', 'verified': True,
                                            'within_limit': True, 'sources': [source.relative]})
                    self.statistics['pdf_parts'] += 1
                prepared.extend(new_items)
                record.update(status='review' if pending else 'prepared',
                              decision='review' if pending else 'split' if record['split'] else 'optimized')
                if self.source_cache and not pending:
                    self.source_cache.remember_optimized(cache_key, record)
                self.report('split' if record['split'] else 'pdf', 'PDF preparado', source.pages, max(1, source.pages),
                            source.name, f'{source.name}: {len(parts)} PDF verificados; {len(pending)} páginas pendientes')
            except Cancelled:
                raise
            except Exception as exc:
                record.update(status='review', decision='review', reason=str(exc))
                self.pending.append({'source': source.relative, 'source_id': source.id,
                                     'original_bytes': source.size, 'processed_bytes': record.get('processed_bytes', source.size),
                                     'limit_bytes': self.limit, 'attempts': record['attempts'],
                                     'reason': 'No se encontró una copia PDF segura bajo el límite: ' + str(exc),
                                     'recommendation': manual_recommendation(source)})
        return prepared

    def make_zip(self):
        collections = {}
        for source in self.sources:
            collections.setdefault(source_collection(source), []).append(source)
        self.folders = [{'name': name, 'label': name.replace('\\', '/').rsplit('/', 1)[-1],
                         'status': 'waiting', 'current': 0, 'total': 0, 'outputs': [],
                         'input_bytes': sum(s.size for s in items), 'output_bytes': 0,
                         'files': len(items), 'issues': [], 'notes': [], 'changes': {}}
                        for name, items in collections.items()]
        used = set()
        for folder in self.folders:
            self.check()
            self.active_folder = folder
            folder['status'] = 'running'
            start, pending_start, warnings_start = len(self.outputs), len(self.pending), len(self.warnings)
            pdf_start, office_start = len(self.preparation), len(self.office_preparation)
            base = safe_name(folder['label'])[:100]
            original, number = base, 1
            while base.casefold() in used:
                number += 1
                base = f'{original} ({number})'
            used.add(base.casefold())
            try:
                self.report('analyze', 'Analizando carpeta', 0, 0, folder['label'], f"Carpeta: {folder['name']}")
                sources = self.prepare_zip_sources(self.prepare_office_sources(collections[folder['name']]))
                self._pack_sources(sources)
                folder['status'] = 'review' if len(self.pending) > pending_start else 'completed'
            except Cancelled:
                folder['status'] = 'cancelled'
                raise
            except Exception:
                folder['status'] = 'error'
                raise
            finally:
                outputs = self.outputs[start:]
                for index, output in enumerate(outputs, 1):
                    final = self.directory / zip_part_name(base, index, len(outputs), self.naming)
                    Path(output['path']).rename(final)
                    output.update(name=final.name, path=str(final), collection=folder['name'])
                folder['outputs'] = [{'name': o['name'], 'bytes': o['bytes']} for o in outputs]
                folder['output_bytes'] = sum(o['bytes'] for o in outputs)
                folder['pending'] = len(self.pending) - pending_start
                folder['warnings'] = len(self.warnings) - warnings_start
                folder['issues'] = self.pending[pending_start:]
                folder['notes'] = list(dict.fromkeys(self.warnings[warnings_start:]))
                pdfs = self.preparation[pdf_start:]
                office = self.office_preparation[office_start:]
                folder['changes'] = {
                    'pdfs_split': sum(bool(r.get('split') and r['parts']) for r in pdfs),
                    'grayscale': sum(bool(r.get('grayscale') and r['parts']) for r in pdfs),
                    'excel': sum(r['status'] == 'prepared' and r['kind'] == 'excel' for r in office),
                    'excel_split': sum(r['status'] == 'prepared' and r.get('strategy') == 'values_per_worksheet' for r in office),
                    'powerpoint': sum(r['status'] == 'prepared' and r['kind'] == 'powerpoint' for r in office),
                    'word': sum(r['status'] == 'prepared' and r['kind'] == 'word' for r in office)}
            self.report('verify', 'Carpeta terminada', 1, 1, folder['label'],
                        f"{folder['label']}: {len(folder['outputs'])} ZIP verificados")
        self.active_folder = None

    def _pack_sources(self, sources):
        grouped = {}
        for source in sources:
            grouped.setdefault(source_collection(source), []).append(source)
        groups = []

        def estimate(source, keep_structure=True):
            """Estimate the real DEFLATE entry, including ZIP-name overhead.

            This is only a placement hint. The final ZIP is always written and
            measured, so an optimistic estimate can never produce an invalid
            delivery.
            """
            relative = source.zip_relative if source.zip_relative is not None else source.relative
            name = relative if keep_structure else source.name
            name_bytes = len(str(name).replace('\\', '/').encode('utf-8'))
            if source.kind == 'directory':
                return name_bytes + 96
            ratio = source.compression_ratio if source.compression_ratio is not None else 1.0
            ratio = max(.02, min(1.0, float(ratio)))
            return max(256, math.ceil(source.size * ratio) + name_bytes + 96)

        # Best-fit decreasing fills the available space much better than the
        # former sequential 94% cutoff. Files are sorted only for placement;
        # entries inside each resulting ZIP are restored to source order.
        for collection, items in grouped.items():
            usable = []
            for order, source in enumerate(items):
                self.check()
                if source.kind == 'pdf' and source.size >= self.limit:
                    self.pending.append({'source': source.relative, 'original_bytes': source.size,
                                         'limit_bytes': self.limit,
                                         'reason': f'El PDF mide {source.size:,} bytes y debe medir menos de {self.limit:,} bytes. '
                                                   'Comprimirlo dentro de un ZIP no cumple el límite individual del PDF.',
                                         'recommendation': manual_recommendation(source)})
                    continue
                if not source.sha256:
                    self.pending.append({'source': source.relative, 'reason': source.error or 'No fue posible leer el archivo.',
                                         'recommendation': manual_recommendation(source)})
                    continue
                usable.append((estimate(source, self.options.get('structure', 'keep') == 'keep'), order, source))

            bins = []
            for estimated, order, source in sorted(usable, key=lambda item: (-item[0], item[1])):
                best = None
                best_remaining = None
                for candidate in bins:
                    remaining = self.limit - 1024 - candidate['estimated'] - estimated
                    if remaining >= 0 and (best_remaining is None or remaining < best_remaining):
                        best, best_remaining = candidate, remaining
                if best is None:
                    best = {'estimated': 0, 'items': []}
                    bins.append(best)
                best['items'].append((order, source))
                best['estimated'] += estimated

            for candidate in bins:
                candidate['items'].sort(key=lambda item: item[0])
                groups.append((collection, [source for _, source in candidate['items']]))
        index = 0
        counters = {}
        while groups:
            self.check()
            collection, group = groups.pop(0)
            total_bytes = max(1, sum(s.size for s in group))
            written = 0
            self.report('zip', 'Comprimiendo ZIP', 0, total_bytes, collection,
                        f'Empaquetando {collection}: {len(group)} archivo(s)')
            temp = self.directory / ('_' + uuid.uuid4().hex + '.partial')
            names, used = [], set()
            try:
                with zipfile.ZipFile(temp, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as archive:
                    for source in group:
                        self.check()
                        keep_structure = self.options.get('structure', 'keep') == 'keep'
                        relative = source.zip_relative if source.zip_relative is not None else source.relative
                        if source.kind == 'directory':
                            if not Path(source.path).is_dir() or any(Path(source.path).iterdir()):
                                raise ValueError(f'{source.name} cambió desde el análisis. Vuelve a agregarlo.')
                            if not keep_structure or not relative:
                                continue
                        if keep_structure and source.zip_relative is not None:
                            # Already relative to the selected collection, including prepared copies.
                            # Preserve the original Unicode spelling and full component names.
                            components = relative.replace('\\', '/').split('/')
                            if Path(relative).drive or relative.startswith(('/', '\\')) or any(
                                    p in ('', '.', '..') or ':' in p or '\x00' in p for p in components):
                                raise ValueError('Ruta ZIP inválida: debe ser relativa a la carpeta seleccionada.')
                        else:
                            components = source.relative.replace('\\', '/').split('/') if keep_structure else [source.name]
                            components = [safe_name(p) for p in components if p not in ('', '.', '..')]
                        name = '/'.join(components)
                        if source.kind == 'directory':
                            name += '/'
                        n, original = 1, name
                        while name.casefold() in used:
                            n += 1
                            p = Path(original)
                            name = (p.parent / f'{p.stem}_{n}{p.suffix}').as_posix()
                        used.add(name.casefold())
                        h = hashlib.sha256()
                        if source.kind == 'directory':
                            archive.writestr(name, b'')
                        else:
                            with Path(source.path).open('rb') as src, archive.open(name, 'w', force_zip64=True) as dst:
                                while chunk := src.read(1024 * 1024):
                                    self.check()
                                    h.update(chunk)
                                    dst.write(chunk)
                                    written += len(chunk)
                                    self.report('zip', 'Comprimiendo ZIP', written, total_bytes, source.name)
                        if h.hexdigest() != source.sha256:
                            raise ValueError(f'{source.name} cambió desde el análisis. Vuelve a agregarlo.')
                        provenance = self.provenance.get(source.id, {})
                        names.append({'entry': name, 'source': provenance.get('source', source.relative), 'source_id':provenance.get('source_id',source.id),
                                      'sha256': source.sha256, **({'prepared': source.relative, **provenance} if provenance else {})})
                if temp.stat().st_size >= self.limit:
                    if len(group) > 1:
                        middle = len(group) // 2
                        groups[0:0] = [(collection, group[:middle]), (collection, group[middle:])]
                    else:
                        self.pending.append({'source': group[0].relative, 'reason': 'El ZIP real también supera el límite. Se requiere procesamiento manual.',
                                             'recommendation': manual_recommendation(group[0]), 'measured_zip_bytes': temp.stat().st_size,
                                             'source_bytes': group[0].size})
                        self.report('zip', 'Archivo para revisión manual', written, total_bytes, group[0].name,
                                    f'Pendiente: {group[0].relative} no cabe comprimido')
                    continue
                verified = 0
                self.report('verify', 'Verificando integridad del ZIP', 0, total_bytes, collection)
                with zipfile.ZipFile(temp) as archive:
                    for entry in names:
                        self.check()
                        h = hashlib.sha256()
                        with archive.open(entry['entry']) as f:
                            while chunk := f.read(1024 * 1024):
                                self.check()
                                h.update(chunk)
                                verified += len(chunk)
                                self.report('verify', 'Verificando integridad del ZIP', verified, total_bytes, entry['entry'])
                        if h.hexdigest() != entry['sha256']:
                            raise ValueError('Falló la verificación de integridad del ZIP.')
                index += 1
                final = self.directory / (uuid.uuid4().hex + '.zip')
                fingerprint = digest_file(temp, self.check)
                self.check()
                temp.rename(final)
                self.outputs.append({'name': final.name, 'path': str(final), 'bytes': final.stat().st_size,
                                     'sha256': fingerprint, 'within_limit': True, 'verified': True,
                                     'kind': 'zip', 'entries': names, 'sources': [s.relative for s in group]})
                self.statistics['zips_verified'] += 1
                self.report('verify', 'ZIP verificado', total_bytes, total_bytes, final.name,
                            f'Verificado: {final.name} · {final.stat().st_size:,} bytes')
            except Cancelled:
                raise
            except Exception as exc:
                self.pending.extend({'source': s.relative, 'reason': str(exc)} for s in group)
            finally:
                temp.unlink(missing_ok=True)
