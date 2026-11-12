"""Split extensive PDFs without recoding text, fonts or embedded images.

Every delivered page is checked against the original page object graph. This
avoids repeatedly extracting text and rendering two high-resolution copies of
an entire book for each optimization attempt. PDFium is already bundled.
"""
from __future__ import annotations

import hashlib
import io
import time

from pypdf import PdfReader, PdfWriter
from pypdf._page import PageObject
from pypdf.generic import ArrayObject, DictionaryObject, IndirectObject, NameObject, NullObject, NumberObject, StreamObject, TextStringObject
from magiczip.pdf_optimizer import PDFIUM_LOCK, adaptive_optimize, validate_pdf


def is_book(source, limit):
    return source.size >= limit and (source.pages >= 64 or source.pages >= 16 and source.size >= limit * 3)


class PageProof:
    """Reference-number independent hashes; shared resources are hashed once."""
    def __init__(self, check=lambda: None):
        self.memo, self.active, self.check = {}, set(), check

    def digest(self, obj):
        self.check()
        key = (id(obj.pdf), obj.idnum, obj.generation) if isinstance(obj, IndirectObject) else None
        if key in self.memo:
            return self.memo[key]
        if key in self.active:
            raise ValueError('Recurso PDF cíclico: requiere la validación convencional.')
        if key:
            self.active.add(key)
        try:
            value = obj.get_object() if isinstance(obj, IndirectObject) else obj
            h = hashlib.sha256()
            if isinstance(value, DictionaryObject):
                h.update(b'dict')
                decoded = isinstance(value, StreamObject) and value.get('/Filter') in (None, '/FlateDecode')
                for name in sorted(value):
                    if isinstance(value, StreamObject) and (name == '/Length' or decoded and name in ('/Filter', '/DecodeParms')):
                        continue
                    h.update(str(name).encode('utf-8'))
                    h.update(self.digest(value.raw_get(name)))
                if isinstance(value, StreamObject):
                    h.update(b'stream')
                    h.update(value.get_data() if decoded else value._data)
            elif isinstance(value, (ArrayObject, list, tuple)):
                h.update(b'array')
                for part in value:
                    h.update(self.digest(part))
            elif isinstance(value, NullObject) or value is None:
                h.update(b'null')
            elif isinstance(value, bytes):
                h.update(b'bytes' + value)
            elif isinstance(value, (int, float)):
                h.update(('number:' + format(float(value), '.12g')).encode())
            else:
                h.update((type(value).__name__ + ':' + str(value)).encode('utf-8'))
            result = h.digest()
            if key:
                self.memo[key] = result
            return result
        finally:
            self.active.discard(key)

    def page(self, page):
        boxes = (tuple(float(v) for v in page.mediabox), tuple(float(v) for v in page.cropbox), page.rotation,
                 float(page.get('/UserUnit', 1)))
        return boxes, tuple((name, self.digest(page.get(name))) for name in
                            ('/Contents', '/Resources', '/Group', '/BleedBox', '/TrimBox', '/ArtBox'))


def split_book(data, limit, fits, check=lambda: None, progress=lambda *args: None, *, allow_aggressive=False, stem="Documento"):
    import pypdfium2 as pdfium
    started = time.perf_counter()
    reader = PdfReader(io.BytesIO(data))
    total = len(reader.pages)
    # Catalog-wide rendering state or interactive forms must use the existing path.
    if reader.is_encrypted or reader.get_fields() or any(key in reader.root_object for key in ('/OCProperties', '/OutputIntents')):
        raise ValueError('El catálogo requiere el flujo PDF convencional.')
    source_proof = PageProof(check)
    parts, pending, attempts, verifications = [], [], [], []
    measured_ranges = 0
    start, hint = 0, max(1, min(total, int(total * limit * .88 / max(1, len(data)))))
    metadata = {str(k): str(v) for k, v in (reader.metadata or {}).items() if v is not None}
    with PDFIUM_LOCK:
        page_indices = {page.indirect_reference.idnum: index for index, page in enumerate(reader.pages)}
        final_ranges = []

        def annotations(page):
            result = page.get('/Annots', [])
            return result.get_object() if hasattr(result, 'get_object') else result

        def destination(annotation):
            action = annotation.get('/A', {})
            action = action.get_object() if hasattr(action, 'get_object') else action
            value = annotation.get('/Dest') or (action.get('/D') if action.get('/S') == '/GoTo' else None)
            value = value.get_object() if hasattr(value, 'get_object') else value
            if value is None:
                return None
            if not isinstance(value, (ArrayObject, list)) or not value or not isinstance(value[0], IndirectObject):
                raise ValueError('Destino de enlace no compatible con la división rápida.')
            return page_indices[value[0].idnum], value[1:]

        def copy_range(begin, end):
            nonlocal measured_ranges
            check()
            progress('Midiendo partes del libro', begin, total, f'Páginas {begin + 1}–{end}')
            writer = PdfWriter()
            writer.pdf_header = reader.pdf_header
            for number in range(begin, end):
                check()
                # We remap links explicitly after all ranges are known. A
                # detached page avoids pypdf's automatic partial-link filtering.
                detached = PageObject(indirect_reference=reader.pages[number].indirect_reference)
                writer.add_page(detached, excluded_keys=['/Annots'])
            for offset, original in enumerate(reader.pages[begin:end]):
                copied = ArrayObject()
                for annotation in annotations(original):
                    original_annotation = annotation.get_object()
                    item = original_annotation.clone(writer, force_duplicate=True, ignore_fields=('/P', '/A', '/Dest'))
                    target = destination(original_annotation)
                    if target:
                        number, view = target
                        tail = [part.clone(writer) if hasattr(part, 'clone') else part for part in view]
                        if begin <= number < end:
                            action = DictionaryObject({NameObject('/S'): NameObject('/GoTo'),
                                                       NameObject('/D'): ArrayObject([writer.pages[number-begin].indirect_reference, *tail])})
                        else:
                            selected = next(((i, bounds) for i, bounds in enumerate(final_ranges) if bounds[0] <= number < bounds[1]), None)
                            filename = f'{stem}_{selected[0]+1}-{len(final_ranges)}.pdf' if selected else f'{stem}_999999-999999.pdf'
                            page_number = number - selected[1][0] if selected else number
                            action = DictionaryObject({NameObject('/S'): NameObject('/GoToR'), NameObject('/F'): TextStringObject(filename),
                                                       NameObject('/D'): ArrayObject([NumberObject(page_number), *tail])})
                        item[NameObject('/A')] = action
                    elif '/A' in original_annotation:
                        item[NameObject('/A')] = original_annotation.raw_get('/A').clone(writer)
                    copied.append(item.indirect_reference or writer._add_object(item))
                if copied:
                    writer.pages[offset][NameObject('/Annots')] = copied
            if metadata:
                writer.add_metadata(metadata)
            if '/Metadata' in reader.root_object:
                writer.root_object[NameObject('/Metadata')] = reader.root_object.raw_get('/Metadata').clone(writer)
            output = io.BytesIO()
            writer.write(output)
            measured_ranges += 1
            return output.getvalue()

        while start < total:
            check()
            end = min(total, start + hint)
            payload = copy_range(start, end)
            if not fits(payload):
                while not fits(payload) and end > start + 1:
                    # A measured proportional backoff avoids serializing many
                    # overlapping ranges just to fill the last few kilobytes.
                    count = max(1, min(end-start-1, int((end-start)*limit*.88/max(1,len(payload)))))
                    end = start + count
                    payload = copy_range(start, end)
            if not fits(payload):
                # Only this genuinely oversized page receives intensive optimization.
                original_page_bytes = len(payload)
                payload, diagnosis = adaptive_optimize(payload, fits, check, progress, allow_aggressive=allow_aggressive)
                attempts.append({'page': start + 1, **diagnosis})
                if not fits(payload):
                    pending.append({'pages': [start + 1], 'original_page_bytes': original_page_bytes,
                                    'processed_page_bytes': len(payload), 'limit_bytes': limit,
                                    'attempts': diagnosis['attempts'],
                                    'reason': f'La página {start + 1}, aislada como PDF válido, mide {len(payload):,} bytes '
                                              f'y debe medir menos de {limit:,} bytes. No se obtuvo una copia segura que cumpla.',
                                    'recommendation': 'Conserva el original y solicita una copia de origen o un medio con mayor límite.'})
                    start += 1
                    continue
                validate_pdf(data, payload, check, page_range=(start, end), lossy=diagnosis['quality_review'])
                verification = {'pages_verified': 1, 'method': 'adaptive_page_and_visual_comparison'}
            else:
                candidate = PdfReader(io.BytesIO(payload))
                if len(candidate.pages) != end - start:
                    raise ValueError('La parte no conserva su cantidad de páginas.')
                proof = PageProof(check)
                with pdfium.PdfDocument(payload) as target:
                    for offset, page in enumerate(candidate.pages):
                        check()
                        original = reader.pages[start + offset]
                        progress('Verificando páginas intactas', start + offset, total, f'Página {start + offset + 1}')
                        if source_proof.page(original) != proof.page(page):
                            raise ValueError(f'La copia de la página {start + offset + 1} cambió sus recursos o contenido.')
                        old_ann, new_ann = annotations(original), annotations(page)
                        if len(old_ann) != len(new_ann):
                            raise ValueError('Cambió la cantidad de anotaciones del original.')
                        for old, new in zip(old_ann, new_ann):
                            left, right = old.get_object(), new.get_object()
                            for key in set(left) | set(right):
                                if key not in ('/P', '/A', '/Dest') and source_proof.digest(left.get(key)) != proof.digest(right.get(key)):
                                    raise ValueError('Cambió una anotación visible del original.')
                            if destination(left) is None and source_proof.digest(left.get('/A')) != proof.digest(right.get('/A')):
                                raise ValueError('Cambió una acción o enlace externo del original.')
                        opened = target[offset]
                        try:
                            # Parse/render every delivered page once at preview scale;
                            # exact content/resource hashes prove full-resolution identity.
                            bitmap = opened.render(scale=min(.5, 450 / max(opened.get_size())))
                            bitmap.close()
                        finally:
                            opened.close()
                verification = {'pages_verified': end - start, 'method': 'sha256_content_fonts_images_geometry_and_page_render'}
            parts.append((start + 1, end, payload))
            verifications.append(verification)
            previous_count = end - start
            start = end
            hint = max(1, min(total - start or 1, int(previous_count * limit * .88 / max(1, len(payload)))))
        # Now that all real ranges are known, resolve cross-part links without
        # copying destination pages from the rest of the original book.
        final_ranges[:] = [(begin-1, end) for begin, end, _ in parts]
        for index, (begin, end, payload) in enumerate(parts):
            if any(annotations(page) for page in reader.pages[begin-1:end]):
                payload = copy_range(begin-1, end)
                if not fits(payload):
                    raise ValueError('Los enlaces de la parte excedieron su límite medido.')
                final_reader = PdfReader(io.BytesIO(payload))
                final_proof = PageProof(check)
                local_indices = {page.indirect_reference.idnum: i for i, page in enumerate(final_reader.pages)}
                for offset, copied_page in enumerate(final_reader.pages):
                    original_page = reader.pages[begin-1+offset]
                    if source_proof.page(original_page) != final_proof.page(copied_page):
                        raise ValueError('Cambió una página al resolver los enlaces.')
                    old_ann, new_ann = annotations(original_page), annotations(copied_page)
                    if len(old_ann) != len(new_ann):
                        raise ValueError('Cambió la cantidad de enlaces al resolver los destinos.')
                    for old, new in zip(old_ann, new_ann):
                        target = destination(old.get_object())
                        if not target:
                            continue
                        number, view = target
                        action = new.get_object()['/A']
                        actual = action['/D']
                        if begin-1 <= number < end:
                            valid = action['/S'] == '/GoTo' and local_indices[actual[0].idnum] == number-(begin-1)
                        else:
                            selected = next(((i, bounds) for i, bounds in enumerate(final_ranges) if bounds[0] <= number < bounds[1]), None)
                            valid = selected is not None and action['/S'] == '/GoToR' and str(action['/F']) == f'{stem}_{selected[0]+1}-{len(parts)}.pdf' and int(actual[0]) == number-selected[1][0]
                        if not valid or source_proof.digest(ArrayObject(view)) != final_proof.digest(ArrayObject(actual[1:])):
                            raise ValueError('Un enlace del índice no conserva su página de destino.')
                parts[index] = begin, end, payload
    assert sum(end - begin + 1 for begin, end, _ in parts) + len(pending) == total
    return parts, pending, {'strategy': 'split_first', 'original_bytes': len(data),
                             'processed_bytes': sum(len(p) for _, _, p in parts),
                             'analysis': {'type': 'book', 'pages_count': total, 'analysis_scope': 'page_objects_without_text_extraction'},
                             'attempts': [], 'page_attempts': attempts, 'verification': verifications,
                             'split': True, 'reduced': False, 'grayscale': False,
                             'quality_review': any(a['quality_review'] for a in attempts),
                             'measured_ranges': measured_ranges, 'book_seconds': round(time.perf_counter() - started, 6)}
