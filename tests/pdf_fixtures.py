"""Synthetic document fixtures: actual page content, not estimated page weights."""
import io
import random

from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, NameObject, NumberObject


def digital_pdf(repetitions=1, text='RFC DEMO010101AAA - TOTAL 1234.56', pages=1):
    writer = PdfWriter()
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'),
                             NameObject('/BaseFont'): NameObject('/Helvetica')})
    font_ref = writer._add_object(font)
    for index in range(pages):
        page = writer.add_blank_page(width=612, height=792)
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): font_ref})})
        content = DecodedStreamObject()
        content.set_data(f'BT /F1 12 Tf 50 740 Td ({text} - PAGE {index + 1}) Tj ET\n'.encode('ascii') + b'%' + b'x' * (repetitions * 4) + b'\n')
        page[NameObject('/Contents')] = writer._add_object(content)
    stream = io.BytesIO()
    writer.write(stream)
    return stream.getvalue()


def padded_pdf(size):
    base = digital_pdf()
    padding = max(0, size - len(base))
    for _ in range(4):
        writer = PdfWriter(clone_from=PdfReader(io.BytesIO(base)))
        writer.add_metadata({'/FixturePadding': 'x' * padding})
        stream = io.BytesIO()
        writer.write(stream)
        data = stream.getvalue()
        if len(data) == size:
            return data
        padding += size - len(data)
    raise AssertionError((len(data), size))


def scan_image(width=1500, height=1800, *, noise=False, color=False, photo=False):
    rng = random.Random(91)
    if photo:
        image = Image.frombytes('RGB', (width, height), rng.randbytes(width * height * 3))
    elif noise:
        gray = Image.frombytes('L', (width, height), bytes(rng.randrange(224, 256) for _ in range(width * height)))
        image = gray.convert('RGB')
        gray.close()
    else:
        image = Image.new('RGB', (width, height), 'white')
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=max(16, width // 38))
    for index, text in enumerate(('ANEXO 1 - DOCUMENTO SINTETICO', 'RFC DEMO010101AAA', 'IMPORTE 1,234.56', 'FOLIO 1234567890')):
        draw.text((width // 14, height // 20 + index * height // 19), text, font=font, fill='black')
    for row in range(7):
        y = height // 3 + row * height // 22
        draw.line((width // 14, y, width * 13 // 14, y), fill='black', width=3)
    for column in range(4):
        x = width // 14 + column * width * 2 // 7
        draw.line((x, height // 3, x, height // 3 + 6 * height // 22), fill='black', width=3)
    draw.line([(width // 5, height * 4 // 5), (width // 3, height * 3 // 4),
               (width // 4, height * 5 // 6), (width // 2, height * 4 // 5)], fill='blue' if color else 'black', width=5)
    draw.ellipse((width * 3 // 5, height * 3 // 4, width * 4 // 5, height * 9 // 10),
                 outline='red' if color else 'black', width=5)
    # Fine marks and a deterministic code-like grid to guard against lost detail.
    for row in range(25):
        for column in range(25):
            if rng.randrange(2):
                x, y = width * 3 // 4 + column * 4, height // 12 + row * 4
                draw.rectangle((x, y, x + 3, y + 3), fill='black')
    return image


def image_pdf(images, *, selectable=False, jpeg=False):
    writer = PdfWriter()
    for index, image in enumerate(images):
        page = writer.add_blank_page(width=612, height=792)
        obj = DecodedStreamObject()
        obj.update({NameObject('/Type'): NameObject('/XObject'), NameObject('/Subtype'): NameObject('/Image'),
                    NameObject('/Width'): NumberObject(image.width), NameObject('/Height'): NumberObject(image.height),
                    NameObject('/ColorSpace'): NameObject('/DeviceRGB' if image.mode == 'RGB' else '/DeviceGray'),
                    NameObject('/BitsPerComponent'): NumberObject(8)})
        if jpeg:
            encoded = io.BytesIO()
            image.save(encoded, format='JPEG', quality=98)
            obj.set_data(encoded.getvalue())
            obj[NameObject('/Filter')] = NameObject('/DCTDecode')
        else:
            obj.set_data(image.tobytes())
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/XObject'): DictionaryObject({NameObject('/Scan'): writer._add_object(obj)})})
        content = DecodedStreamObject()
        commands = b'q 612 0 0 792 0 0 cm /Scan Do Q\n'
        if selectable:
            font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'),
                                     NameObject('/BaseFont'): NameObject('/Helvetica')})
            page['/Resources'][NameObject('/Font')] = DictionaryObject({NameObject('/F1'): writer._add_object(font)})
            commands += f'BT /F1 11 Tf 40 30 Td (TEXTO SELECCIONABLE - PAGINA {index + 1}) Tj ET\n'.encode('ascii')
        content.set_data(commands)
        page[NameObject('/Contents')] = writer._add_object(content)
    stream = io.BytesIO()
    writer.write(stream)
    return stream.getvalue()
