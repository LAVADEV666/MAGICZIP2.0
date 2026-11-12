"""Conservative PDF decisions using existing pypdf, Pillow and PDFium runtimes.

Never render pages back into a PDF: only replace eligible embedded images.
All candidates retain text, page geometry, annotations and form values, and
must pass a page-by-page rendering comparison before they can be selected.
"""
from __future__ import annotations

import io
import math
import threading
import zlib

from PIL import Image, ImageChops, ImageFilter, ImageStat
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DictionaryObject, NameObject, NumberObject

PDFIUM_LOCK = threading.RLock()
MAX_IMAGE_PIXELS = 40_000_000


def serialize(writer):
    writer.compress_identical_objects(remove_duplicates=True, remove_unreferenced=True)
    stream = io.BytesIO()
    writer.write(stream)
    return stream.getvalue()


def image_characteristics(image):
    """Conservative image heuristics; ambiguous images retain their color/tone."""
    with image.convert('RGB') as rgb:
        channels = rgb.split()
        red_green = ImageChops.difference(channels[0], channels[1])
        red_blue = ImageChops.difference(channels[0], channels[2])
        green_blue = ImageChops.difference(channels[1], channels[2])
        # A small colored signature or seal matters even if a thumbnail misses it.
        relevant_color = any(d.getextrema()[1] > 18 for d in (red_green, red_blue, green_blue))
        for part in (*channels, red_green, red_blue, green_blue):
            part.close()
        rgb.thumbnail((256, 256))
        pixels = list(rgb.get_flattened_data() if hasattr(rgb, 'get_flattened_data') else rgb.getdata())
        colored = sum(max(p) - min(p) > 18 for p in pixels) / max(1, len(pixels))
        gray = rgb.convert('L')
        histogram = gray.histogram()
        gray.close()
    midtones = sum(histogram[40:216]) / max(1, sum(histogram))
    return {'color_fraction': round(colored, 5), 'midtones': round(midtones, 5),
            'relevant_color': relevant_color,
            'text_scan_candidate': not relevant_color and midtones < .12}


def analyze_pdf(reader, check=lambda: None):
    pages = []
    for index, page in enumerate(reader.pages):
        check()
        vector_count = [0]
        def operand(operator, arguments, matrix, text_matrix):
            if operator in {b'S', b's', b'f', b'F', b'f*', b'B', b'B*', b'b', b'b*'}:
                vector_count[0] += 1
        text = page.extract_text(visitor_operand_before=operand) or ''
        images, seen = [], set()

        def resources(objects):
            for obj in objects.values():
                obj = obj.get_object()
                identity = id(obj)
                if identity in seen:
                    continue
                seen.add(identity)
                if obj.get('/Subtype') == '/Form':
                    resources(obj.get('/Resources', DictionaryObject()).get_object().get('/XObject', DictionaryObject()).get_object())
                if obj.get('/Subtype') != '/Image':
                    continue
                width, height = int(obj.get('/Width', 0)), int(obj.get('/Height', 0))
                images.append({'width': width, 'height': height,
                               'bits': int(obj.get('/BitsPerComponent', 1)),
                               'colorspace': str(obj.get('/ColorSpace', 'unknown')),
                               'encoded_bytes': len(getattr(obj, '_data', b'')),
                               'mask': bool(obj.get('/SMask') or obj.get('/Mask') or obj.get('/ImageMask')),
                               'dpi_if_full_page': [round(width * 72 / max(1, float(page.mediabox.width))),
                                                    round(height * 72 / max(1, float(page.mediabox.height)))]})

        resources(page.get('/Resources', DictionaryObject()).get_object().get('/XObject', DictionaryObject()).get_object())
        vectors = vector_count[0]
        kind = 'mixed' if images and (text.strip() or vectors) else 'scanned' if images else 'digital'
        pages.append({'page': index + 1, 'type': kind, 'text_characters': len(text.strip()),
                      'vector_operations': vectors, 'images': images,
                      'image_bytes': sum(i['encoded_bytes'] for i in images),
                      'rotation': page.rotation,
                      'width': float(page.mediabox.width), 'height': float(page.mediabox.height)})
    kinds = {p['type'] for p in pages}
    return {'type': next(iter(kinds)) if len(kinds) == 1 else 'mixed', 'pages': pages,
            'image_count': sum(len(p['images']) for p in pages),
            'has_text': any(p['text_characters'] for p in pages),
            'has_vectors': any(p['vector_operations'] for p in pages),
            'metadata_bytes': sum(len(str(v).encode('utf-8')) for v in (reader.metadata or {}).values())}


def _semantics(reader, start=0, end=None):
    return [((tuple(float(v) for v in page.mediabox), tuple(float(v) for v in page.cropbox), page.rotation),
             page.extract_text() or '',
             [(str(a.get_object().get('/Subtype')), str(a.get_object().get('/Rect')),
               str(a.get_object().get('/Contents', '')), str(a.get_object().get('/V', '')))
              for a in page.get('/Annots', [])]) for page in list(reader.pages)[start:end]], {
                  key: (str(value.get('/FT')), str(value.get('/V')), str(value.get('/DV')))
                  for key, value in (reader.get_fields() or {}).items()}


def validate_pdf(original, candidate, check=lambda: None, *, lossy=False, page_range=None):
    """Check every page. Rendering metrics are a guard, not an OCR/QR guarantee."""
    import pypdfium2 as pdfium
    if original == candidate and page_range is None:
        # The bytes are identical: render once, rather than decoding and comparing
        # two copies of every page. This retains the original corruption check.
        reader = PdfReader(io.BytesIO(original))
        if reader.is_encrypted or not len(reader.pages):
            raise ValueError('PDF cifrado o sin páginas verificables.')
        with PDFIUM_LOCK, pdfium.PdfDocument(original) as doc:
            if len(doc) != len(reader.pages):
                raise ValueError('La cantidad de páginas no coincide entre lectores.')
            for index in range(len(doc)):
                check()
                page = doc[index]
                try:
                    bitmap = page.render(scale=min(2., 1800 / max(page.get_size())))
                    bitmap.close()
                finally:
                    page.close()
        return {'pages_verified': len(reader.pages), 'text_geometry_forms': True,
                'max_visual_error': 0., 'min_ink_retained': 1., 'requires_human_quality_review': False}
    before, after = PdfReader(io.BytesIO(original)), PdfReader(io.BytesIO(candidate))
    if before.is_encrypted or after.is_encrypted or not len(after.pages):
        raise ValueError('PDF cifrado o sin páginas verificables.')
    start, end = page_range or (0, len(before.pages))
    if len(after.pages) != end - start:
        raise ValueError('Cambió el número de páginas.')
    # Byte identity already proves semantic identity. Unsupported text extraction
    # must not reject a renderable original that will be delivered unchanged.
    if (original != candidate or page_range) and _semantics(before, start, end) != _semantics(after):
        raise ValueError('Cambió el texto, la geometría, las anotaciones o los formularios.')
    worst_error, lowest_ink = 0., 1.
    with PDFIUM_LOCK, pdfium.PdfDocument(original) as a, pdfium.PdfDocument(candidate) as b:
        if len(b) != end - start:
            raise ValueError('Cambió el número de páginas.')
        for index in range(len(b)):
            check()
            images = []
            try:
                for doc, number in ((a, start + index), (b, index)):
                    page = doc[number]
                    try:
                        scale = min(2., 1800 / max(page.get_size()))
                        bitmap = page.render(scale=scale)
                        try:
                            images.append(bitmap.to_pil().convert('RGB'))
                        finally:
                            bitmap.close()
                    finally:
                        page.close()
                if images[0].size != images[1].size:
                    raise ValueError('Cambió el tamaño visible de una página.')
                with ImageChops.difference(*images) as diff:
                    error = sum(ImageStat.Stat(diff).mean) / (3 * 255)
                    if not lossy and diff.getbbox():
                        raise ValueError(f'Cambió la apariencia de la página {index + 1} en una etapa sin pérdida.')
                ink_a = images[0].convert('L').point(lambda x: 255 if x < 160 else 0)
                ink_b = images[1].convert('L').point(lambda x: 255 if x < 190 else 0)
                # Allow one antialiasing pixel, while guarding small strokes and codes.
                expanded = ink_b.filter(ImageFilter.MaxFilter(3))
                overlap = ImageChops.multiply(ink_a, expanded)
                ink = sum(ink_a.histogram()[1:])
                recall = sum(overlap.histogram()[1:]) / max(1, ink) if ink else 1.
                for im in (ink_a, ink_b, expanded, overlap):
                    im.close()
                if error > .018 or recall < .985:
                    raise ValueError(f'La comparación visual de la página {index + 1} detectó pérdida de detalle o color.')
                worst_error, lowest_ink = max(worst_error, error), min(lowest_ink, recall)
            finally:
                for im in images:
                    im.close()
    return {'pages_verified': len(after.pages), 'text_geometry_forms': True,
            'max_visual_error': round(worst_error, 6), 'min_ink_retained': round(lowest_ink, 6),
            'requires_human_quality_review': lossy}


def _binary(image):
    gray = image.convert('L')
    mean = gray.filter(ImageFilter.BoxBlur(12))
    local = ImageChops.subtract(mean, gray).point(lambda v: 0 if v > 12 else 255)
    dark = gray.point(lambda v: 0 if v < 100 else 255)
    result = ImageChops.darker(local, dark).convert('1')
    for im in (gray, mean, local, dark):
        im.close()
    return result


def _replace_lossless_image(image_file, image):
    """Use Flate for bilevel images; Pillow's PDF exporter otherwise needs libtiff."""
    obj = image_file.indirect_reference.get_object()
    obj._data = zlib.compress(image.tobytes(), 9)
    obj[NameObject('/Filter')] = NameObject('/FlateDecode')
    obj[NameObject('/ColorSpace')] = NameObject('/DeviceGray' if image.mode in ('1', 'L') else '/DeviceRGB')
    obj[NameObject('/BitsPerComponent')] = NumberObject(1 if image.mode == '1' else 8)
    obj[NameObject('/Width')], obj[NameObject('/Height')] = NumberObject(image.width), NumberObject(image.height)
    for key in ('/DecodeParms', '/Decode'):
        obj.pop(NameObject(key), None)


def _page_target(profile_page, stage):
    """Choose a conservative target from the page's visual role.

    Scanned pages keep more sampling resolution because their text is pixels;
    digital pages can go lower because their text and vectors remain untouched.
    These are targets, not forced resizes: an image is only reduced when its
    effective full-page DPI is materially above the target.
    """
    kind = profile_page.get('type', 'mixed') if profile_page else 'mixed'
    if kind == 'scanned':
        return 300 if profile_page.get('text_characters', 0) else 260
    if kind == 'digital':
        return 180 if stage == 'aggressive' else 220
    return 220 if stage == 'aggressive' else 260


def _candidate(data, stage, check, progress, profile=None, quality=92):
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(data)))
    seen, changed, decisions = set(), 0, []
    profile_pages = (profile or {}).get('pages', [])
    for index, page in enumerate(writer.pages):
        check()
        progress(stage, index, len(writer.pages), f'Página {index + 1}')
        page.compress_content_streams(level=9)
        if stage == 'lossless':
            continue
        for image_key in page.images.keys():
            check()
            # Inspect the stream before Pillow decodes it; bound memory for large scans.
            resources = page.get('/Resources', DictionaryObject()).get_object()
            keys = image_key if isinstance(image_key, list) else [image_key]
            for key in keys:
                obj = resources['/XObject'][key].get_object()
                resources = obj.get('/Resources', DictionaryObject()).get_object()
            ref = obj.indirect_reference
            if ref is None or ref.idnum in seen:
                continue
            seen.add(ref.idnum)
            width, height = int(obj.get('/Width', 0)), int(obj.get('/Height', 0))
            if obj.get('/SMask') or obj.get('/Mask') or obj.get('/ImageMask') or width * height > MAX_IMAGE_PIXELS:
                continue
            if stage == 'images_lossless':
                if obj.get('/Filter') in (None, '/FlateDecode'):
                    raw = obj.get_data()
                    compressed = zlib.compress(raw, 9)
                    if len(compressed) < len(obj._data):
                        obj._data = compressed
                        obj[NameObject('/Filter')] = NameObject('/FlateDecode')
                        obj.pop(NameObject('/DecodeParms'), None)
                        changed += 1
                continue
            if obj.get('/ColorSpace') not in ('/DeviceRGB', '/DeviceGray') or obj.get('/Decode'):
                continue
            image_file = page.images[image_key]
            original = image_file.image
            if original is None or original.mode not in ('RGB', 'L', '1'):
                continue
            traits = image_characteristics(original)
            decisions.append({'page': index + 1, 'image': image_file.name, **traits})
            if stage in ('grayscale', 'bilevel') and traits['relevant_color']:
                continue
            if stage == 'bilevel' and (not traits['text_scan_candidate'] or original.mode == '1'):
                continue
            if stage == 'grayscale' and original.mode != 'RGB':
                continue
            image = original.copy()
            try:
                if stage in ('resolution', 'aggressive'):
                    dpi = _page_target(profile_pages[index] if index < len(profile_pages) else None, stage)
                    full_page_dpi = min(width * 72 / max(1., float(page.mediabox.width)),
                                        height * 72 / max(1., float(page.mediabox.height)))
                    # Avoid touching images that are already close to the
                    # selected target; this prevents needless resampling and
                    # preserves small text and seals at their native detail.
                    if full_page_dpi <= dpi * 1.15:
                        image.close()
                        continue
                    box = (max(1, math.ceil(float(page.mediabox.width) * dpi / 72)),
                           max(1, math.ceil(float(page.mediabox.height) * dpi / 72)))
                    image.thumbnail(box, Image.Resampling.LANCZOS)
                    if stage == 'resolution' and image.size == original.size:
                        continue
                if stage == 'grayscale':
                    image.close()
                    image = original.convert('L')
                elif stage == 'bilevel':
                    image.close()
                    image = _binary(original)
                if image.mode == '1':
                    _replace_lossless_image(image_file, image)
                else:
                    image_file.replace(image, quality=quality if stage == 'aggressive' else max(92, quality), optimize=True)
                changed += 1
            finally:
                image.close()
    return serialize(writer), changed, decisions


def adaptive_optimize(data, fits, check=lambda: None, progress=lambda *args: None, *, allow_aggressive=False, quality=82):
    best, attempts = data, []
    profile = analyze_pdf(PdfReader(io.BytesIO(data)), check)
    validate_pdf(data, data, check)
    for stage in ('lossless', 'images_lossless', 'images', 'resolution', 'grayscale', 'bilevel', 'aggressive'):
        check()
        if fits(best):
            break
        if stage == 'aggressive' and not allow_aggressive:
            attempts.append({'stage': stage, 'status': 'skipped', 'reason': 'Reducción agresiva no autorizada en los ajustes.'})
            continue
        if stage not in ('lossless',) and not profile['image_count']:
            continue
        attempt = {'stage': stage, 'input_bytes': len(best)}
        attempts.append(attempt)
        try:
            candidate, changed, decisions = _candidate(best, stage, check, progress, profile=profile, quality=quality)
            attempt.update(bytes=len(candidate), images_changed=changed, image_decisions=decisions)
            reduction = len(best) - len(candidate)
            minimum = max(1024, len(best) * (.02 if stage in ('grayscale', 'bilevel') else .002))
            significant = reduction >= minimum or reduction > 0 and fits(candidate)
            if not significant:
                attempt.update(status='discarded', reason='No ofrece una reducción suficiente.')
                continue
            verification = validate_pdf(data, candidate, check, lossy=stage not in ('lossless', 'images_lossless') or best != data and any(
                a.get('status') == 'accepted' and a['stage'] not in ('lossless', 'images_lossless') for a in attempts))
            best = candidate
            attempt.update(status='accepted', verification=verification)
        except Exception as exc:
            check()  # Cancellation is never downgraded to a failed optimization.
            attempt.update(status='discarded', reason=str(exc))
    return best, {'analysis': profile, 'attempts': attempts, 'original_bytes': len(data),
                  'processed_bytes': len(best), 'fits': fits(best),
                  'quality_review': any(a.get('status') == 'accepted' and a['stage'] not in ('lossless', 'images_lossless') for a in attempts)}
