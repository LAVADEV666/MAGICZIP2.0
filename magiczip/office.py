"""Local Office processing. Originals are never opened for editing or replaced."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
import zipfile
from decimal import Decimal, InvalidOperation
from pathlib import Path

EXCEL_TYPES = {'.xlsx', '.xlsm', '.xlsb', '.xls'}
POWERPOINT_TYPES = {'.pptx', '.pptm', '.ppt', '.ppsx', '.ppsm', '.pps'}
WORD_TYPES = {'.docx', '.docm', '.doc', '.dotx', '.dotm', '.dot', '.odt', '.rtf'}
NS = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
REL = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'
from magiczip.runtime import RESOURCE_ROOT as ROOT


def close_owned_office(owner_path: Path):
    """Cleanup on cancellation/timeout, only with matching PID, creation time and exe."""
    if os.name != 'nt' or not owner_path.exists():
        return
    import ctypes
    from ctypes import wintypes
    try:
        owner = json.loads(owner_path.read_text(encoding='utf-8-sig'))
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        handle = kernel.OpenProcess(0x101001, False, int(owner['pid']))
        if not handle: return
        try:
            created, exited, cpu_kernel, cpu_user = (wintypes.FILETIME() for _ in range(4))
            kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
            if not kernel.GetProcessTimes(handle,ctypes.byref(created),ctypes.byref(exited),ctypes.byref(cpu_kernel),ctypes.byref(cpu_user)): return
            stamp = (created.dwHighDateTime << 32) | created.dwLowDateTime
            if stamp != int(owner['created']): return
            buffer = ctypes.create_unicode_buffer(32768); size = wintypes.DWORD(len(buffer))
            kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE,wintypes.DWORD,wintypes.LPWSTR,ctypes.POINTER(wintypes.DWORD)]
            if not kernel.QueryFullProcessImageNameW(handle,0,buffer,ctypes.byref(size)): return
            if Path(buffer.value).name.upper() != owner['name']: return
            kernel.TerminateProcess.argtypes = [wintypes.HANDLE,wintypes.UINT]
            if kernel.TerminateProcess(handle, 1):
                kernel.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD]
                kernel.WaitForSingleObject(handle,5000)
        finally:
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]; kernel.CloseHandle(handle)
    except (OSError,ValueError,KeyError):
        pass


def office_capabilities():
    result = {'excel': False, 'powerpoint': False, 'word': False}
    if os.name != 'nt':
        return result
    import winreg
    for name, executable in [('excel', 'excel.exe'), ('powerpoint', 'powerpnt.exe'), ('word', 'winword.exe')]:
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, rf'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{executable}') as key:
                path = winreg.QueryValue(key, None)
                result[name] = Path(path.strip('"')).is_file()
        except OSError:
            pass
    return result


def package_fingerprints(path: Path, check=lambda: None):
    result = []
    with zipfile.ZipFile(path) as archive:
        if len(archive.infolist()) > 100_000:
            raise ValueError('El paquete de Excel contiene demasiados elementos para esta verificación.')
        for entry in archive.infolist():
            check()
            h = hashlib.sha256()
            with archive.open(entry) as stream:
                while block := stream.read(1024 * 1024):
                    check(); h.update(block)
            result.append((entry.filename, entry.file_size, h.hexdigest()))
    return result


def recompress_excel(source: Path, destination: Path, check=lambda: None):
    """Re-encode ZIP compression; every uncompressed part must stay byte-identical."""
    with zipfile.ZipFile(source) as original:
        if any('_xmlsignatures/' in n.lower() or 'vbaprojectsignature' in n.lower() for n in original.namelist()):
            return {'accepted': False, 'reason': 'Firma de documento detectada: se conserva el archivo íntegro.'}
        with zipfile.ZipFile(destination, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=9, allowZip64=True) as target:
            target.comment=original.comment
            for entry in original.infolist():
                check()
                import copy
                info = copy.copy(entry)
                info.compress_type = zipfile.ZIP_DEFLATED
                info._compresslevel = 9
                with original.open(entry) as src, target.open(info, 'w', force_zip64=entry.file_size >= 2**31) as dst:
                    while block := src.read(1024 * 1024):
                        check(); dst.write(block)
    if package_fingerprints(source, check) != package_fingerprints(destination, check):
        destination.unlink(missing_ok=True)
        raise ValueError('La copia no conservó todos los elementos internos; se descarta.')
    if destination.stat().st_size >= source.stat().st_size:
        destination.unlink()
        return {'accepted': False, 'reason': 'No hubo ahorro con recompresión sin pérdida; se mantiene el formato original.'}
    return {'accepted': True, 'reason': 'Todos los archivos internos son idénticos byte a byte.', 'verification': 'package_sha256'}


def binary_eligibility(path: Path):
    if path.suffix.lower() != '.xlsx':
        return False, 'La conversión binaria automática se limita a XLSX verificables; se conserva el formato de este libro.'
    try:
        with zipfile.ZipFile(path) as archive:
            if sum(i.file_size for i in archive.infolist()) > 128*1024*1024:
                return False, 'El libro supera el límite de verificación binaria de esta versión; se conserva su formato.'
            if 'xl/sharedStrings.xml' in archive.namelist():
                strings=ET.fromstring(archive.read('xl/sharedStrings.xml'))
                if strings.find(f'.//{NS}r') is not None or strings.find(f'.//{NS}rPh') is not None:
                    return False, 'Texto enriquecido o fonético: se conserva el formato para no arriesgar detalles del contenido.'
            for name in archive.namelist():
                low = name.lower()
                if low.startswith(('xl/worksheets/sheet', 'xl/theme/')) and (low.endswith('.xml') and '/_rels/' not in low):
                    continue
                if name in {'[Content_Types].xml', '_rels/.rels', 'docProps/app.xml', 'docProps/core.xml',
                            'xl/workbook.xml', 'xl/_rels/workbook.xml.rels', 'xl/styles.xml',
                            'xl/sharedStrings.xml', 'xl/calcChain.xml'}:
                    continue
                return False, f'No se convierte a XLSB automáticamente: el libro contiene funciones o elementos complejos ({name}).'
            for name in archive.namelist():
                if name.startswith('xl/worksheets/') and name.endswith('.xml'):
                    root = ET.fromstring(archive.read(name))
                    if root.find(f'{NS}extLst') is not None or root.find(f'{NS}legacyDrawing') is not None:
                        return False, 'El libro contiene características extendidas que no se pueden verificar por completo.'
        return True, ''
    except (zipfile.BadZipFile, ET.ParseError):
        return False, 'Libro cifrado, protegido o no reconocido: se conserva el archivo original.'


def canonical(node):
    # Namespace prefixes / XML indentation are not workbook information.
    attrs = sorted((k, v) for k, v in node.attrib.items() if 'markup-compatibility' not in k and 'revision' not in k.lower())
    text = node.text or ''
    return (node.tag, attrs, text if text.strip() else '', [canonical(child) for child in node])


def excel_semantics(path: Path):
    """Strict round-trip comparison: cell coordinates, data, styles and sheet layout.

    Formula expressions are additionally compared by native Excel before/after
    and after reopening XLSB. Shared XML formula encoding may legitimately differ.
    """
    result = {'sheets': [], 'names': [], 'styles': None}
    with zipfile.ZipFile(path) as archive:
        strings = []
        if 'xl/sharedStrings.xml' in archive.namelist():
            strings = [''.join(item.itertext()) for item in ET.fromstring(archive.read('xl/sharedStrings.xml'))]
        workbook = ET.fromstring(archive.read('xl/workbook.xml'))
        result['names'] = [(n.get('name'), n.get('localSheetId'), n.get('hidden'), n.text) for n in workbook.findall(f'{NS}definedNames/{NS}definedName')]
        result['date1904'] = workbook.find(f'{NS}workbookPr').get('date1904', '0') if workbook.find(f'{NS}workbookPr') is not None else '0'
        relationships = {item.get('Id'): item.get('Target') for item in ET.fromstring(archive.read('xl/_rels/workbook.xml.rels'))}
        if 'xl/styles.xml' in archive.namelist():
            result['styles'] = canonical(ET.fromstring(archive.read('xl/styles.xml')))
        for sheet in workbook.findall(f'{NS}sheets/{NS}sheet'):
            target = relationships[sheet.get(f'{REL}id')]
            target = target.lstrip('/') if target.startswith('/') else 'xl/' + target
            root = ET.fromstring(archive.read(target))
            cells, rows = [], []
            for row in root.findall(f'{NS}sheetData/{NS}row'):
                rows.append(sorted((k,v) for k,v in row.attrib.items() if k not in {'spans', 'dyDescent'} and 'revision' not in k.lower()))
                for cell in row.findall(f'{NS}c'):
                    kind = cell.get('t', 'n')
                    value = cell.findtext(f'{NS}v', '')
                    if kind == 's':
                        value, kind = strings[int(value)], 'text'
                    elif kind == 'inlineStr':
                        value, kind = ''.join(cell.find(f'{NS}is').itertext()), 'text'
                    elif kind == 'n' and value:
                        try: value = str(Decimal(value).normalize())
                        except InvalidOperation: pass
                    cells.append((cell.get('r'), kind, value, cell.get('s', '0'), cell.find(f'{NS}f') is not None))
            layout = [canonical(child) for child in root if child.tag not in {f'{NS}sheetData', f'{NS}dimension', f'{NS}sheetViews'}]
            result['sheets'].append({'name':sheet.get('name'), 'state':sheet.get('state','visible'), 'cells':cells, 'rows':rows, 'layout':layout})
    return result


def run_office(mode: str, source: Path, target: Path, check=lambda: None, progress=lambda detail: None, timeout=240):
    # Office has its own path limits, even when Python supports longer paths.
    # Keep the native input, output, owner record and roundtrip under a short,
    # private Windows temporary directory. Only verified copies return to the job.
    with tempfile.TemporaryDirectory(prefix='MZ_') as temporary:
        folder = Path(temporary)
        local_source = folder / ('entrada' + source.suffix)
        local_target = folder / ('salida' + target.suffix)
        if max(len(str(local_source)), len(str(local_target.with_suffix('.roundtrip.xlsx')))) >= 250:
            raise ValueError('La ruta temporal de Windows es demasiado larga para Office. Configura una ubicación temporal local más corta.')

        def copy_checked(origin, destination):
            partial = destination.with_name(destination.name + '.partial')
            try:
                with origin.open('rb') as src, partial.open('xb') as dst:
                    while block := src.read(1024 * 1024):
                        check()
                        dst.write(block)
                check()
                partial.replace(destination)
            finally:
                partial.unlink(missing_ok=True)

        copy_checked(source, local_source)
        response = _run_office_helper(mode, local_source, local_target, check, progress, timeout)
        copy_checked(local_target, target)
        roundtrip = local_target.with_suffix('.roundtrip.xlsx')
        if mode == 'excel_binary':
            copy_checked(roundtrip, target.with_suffix('.roundtrip.xlsx'))
        return response


def split_excel_values(source: Path, destination: Path, check=lambda: None,
                       progress=lambda detail: None, timeout=600):
    """Create one verified XLSX copy per worksheet, replacing formulas by values.

    The original is copied to a short private path and never opened for editing.
    This is intentionally a separate operation from XLSB conversion because the
    result is a deliberate, user-enabled reduction that changes formulas into
    their current displayed values.
    """
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='MZ_') as temporary:
        folder = Path(temporary)
        local_source = folder / ('entrada' + source.suffix)
        local_target = folder / 'salidas'
        local_target.mkdir()

        def copy_checked(origin, target):
            partial = target.with_name(target.name + '.partial')
            try:
                with origin.open('rb') as src, partial.open('xb') as dst:
                    while block := src.read(1024 * 1024):
                        check(); dst.write(block)
                check(); partial.replace(target)
            finally:
                partial.unlink(missing_ok=True)

        copy_checked(source, local_source)
        response = _run_office_helper('excel_split_values', local_source, local_target,
                                      check, progress, timeout)
        copied = []
        for item in response.get('files', []):
            check()
            name = Path(item['name']).name
            origin = local_target / name
            target = destination / name
            copy_checked(origin, target)
            copied.append({**item, 'path': str(target), 'bytes': target.stat().st_size})
        if not copied:
            raise ValueError('Excel no devolvió pestañas exportables.')
        return {**response, 'files': copied}


def _run_office_helper(mode: str, source: Path, target: Path, check, progress, timeout):
    request_path = target.parent / ('office_request_' + target.stem + '.json')
    response_path = request_path.with_name(request_path.stem + '_result.json')
    request = {'mode': mode, 'source':str(source.resolve()), 'target':str(target.resolve()),
               'response':str(response_path.resolve()), 'roundtrip':str(target.with_suffix('.roundtrip.xlsx').resolve())}
    request_path.write_text(json.dumps(request, ensure_ascii=False), encoding='utf-8')
    powershell = Path(os.environ.get('WINDIR','C:/Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    command = [str(powershell), '-NoProfile', '-STA', '-NonInteractive', '-File', str(ROOT/'magiczip/office_worker.ps1'), '-RequestFile', str(request_path)]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    started = time.monotonic()
    try:
        while process.poll() is None:
            check()
            if time.monotonic() - started > timeout:
                raise TimeoutError('Office superó el tiempo de espera. Se conserva el original.')
            progress('Office está procesando una copia local…')
            time.sleep(.2)
        stdout, stderr = process.communicate()
        if not response_path.exists():
            raise ValueError('No se pudo completar la conversión local de Office. ' + stderr.decode('utf-8',errors='replace')[-250:])
        response = json.loads(response_path.read_text(encoding='utf-8-sig'))
        if not response.get('ok'):
            raise ValueError(response.get('error','Office no pudo generar una copia verificable.'))
        return response
    finally:
        if process.poll() is None:
            # Kill only this owned helper; its finally block normally closes Office.
            process.terminate()
            process.wait(timeout=10)
        close_owned_office(Path(str(target) + '.owner.json'))
        Path(str(target) + '.owner.json').unlink(missing_ok=True)
        request_path.unlink(missing_ok=True)
        response_path.unlink(missing_ok=True)
