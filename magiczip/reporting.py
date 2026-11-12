"""Human-readable, complete local processing reports, without external resources."""
import csv
import html
import json
from pathlib import Path


def amount(value):
    return f'{value / 1_000_000:,.2f} MB ({value:,} bytes)'


def file_records(result):
    rows = []
    for source in result['sources']:
        entries = [(output,entry) for output in result['outputs'] if output.get('kind')=='zip'
                   for entry in output.get('entries',[]) if entry.get('source_id')==source['id']]
        if source.get('kind') == 'directory' and not entries:
            # An empty selected root produces an empty ZIP, without a container entry.
            entries = [(output, {}) for output in result['outputs'] if output.get('kind') == 'zip'
                       and source['relative'] in output.get('sources', [])]
        office = [r for r in result.get('office_preparation',[]) if r.get('source_id')==source['id']]
        direct_pending = [p for p in result['pending'] if p.get('source')==source['relative'] or source['relative'] in p.get('sources',[])]
        prepared_names = {r.get('output_name') for r in office if r.get('output_name')}
        for record in result.get('preparation',[]):
            if record.get('source_id')==source['id'] or Path(record.get('source','')).name in prepared_names:
                prepared_names.update(p['name'] for p in record['parts'])
        direct_pending += [p for p in result['pending'] if any(Path(name).name in prepared_names for name in [p.get('source',''),*p.get('sources',[])])]
        actions = []
        for record in office:
            if record['status']=='prepared':
                label = {'excel': 'Excel optimizado', 'powerpoint': 'PowerPoint a PDF', 'word': 'Word a PDF'}.get(record['kind'], 'Office preparado')
                actions.append(label + ': ' + record.get('output_name',''))
            else:
                actions.append('Formato original conservado')
            actions.extend(record.get('notes',[]))
            if record.get('reason'): actions.append(record['reason'])
        transformed = any(entry.get('prepared') for _,entry in entries)
        if transformed and any(entry.get('page_range') for _,entry in entries): actions.append('PDF optimizado/dividido por páginas; rangos registrados en el informe técnico.')
        status = 'Parcial / requiere atención' if entries and direct_pending else 'Incluido y verificado' if entries else 'Requiere atención'
        decision = next((d for d in result.get('pdf_decisions', []) if d['source_id'] == source['id']), None)
        if decision:
            if entries and not direct_pending:
                status = {'accepted_unchanged': 'Aceptado sin cambios', 'optimized': 'Optimizado satisfactoriamente',
                          'split': 'Dividido satisfactoriamente'}.get(decision['decision'], status)
            actions.append(f"PDF: {decision['original_bytes']:,} → {decision.get('processed_bytes', decision['original_bytes']):,} bytes; "
                           f"límite {decision['limit_bytes']:,} bytes (MB decimales)")
            actions.extend(f"{a['stage']}: {a['status']}" for a in decision.get('attempts', []))
        if result['action']!='zip': status='Procesado / revisar resultados' if result['outputs'] else 'Requiere atención'
        rows.append({'archivo':source['relative'],'formato':Path(source['name']).suffix.lower(),'bytes_originales':source['size'],
                     'estado':status,'acciones':' | '.join(actions) or 'Empaquetado sin cambiar contenido',
                     'zips':' | '.join(dict.fromkeys(o['name'] for o,_ in entries)),
                     'copias':' | '.join(sorted(prepared_names)),
                     'pendientes':' | '.join(p['reason'] for p in direct_pending),
                     'recomendaciones':' | '.join(dict.fromkeys(p.get('recommendation','') for p in direct_pending if p.get('recommendation'))),
                     'sha256_original':source.get('sha256','')})
    return rows


def csv_safe(value):
    if isinstance(value,str) and value.lstrip().startswith(('=','+','-','@')):
        return "'"+value
    return value


def write_reports(result, directory:Path):
    records=file_records(result)
    csv_path=directory/'INFORME_ARCHIVOS.csv'
    with csv_path.open('w',encoding='utf-8-sig',newline='') as stream:
        fields=list(records[0]) if records else ['archivo','estado']
        writer=csv.DictWriter(stream,fieldnames=fields);writer.writeheader()
        writer.writerows({k:csv_safe(v) for k,v in record.items()} for record in records)
    escape=lambda value:html.escape(str(value),quote=True)
    status={'completed':'Completado','review':'Completado con pendientes','cancelled':'Cancelado','error':'No completado'}.get(result['status'],result['status'])
    output_rows=''.join(f'<tr><td>{escape(o["name"])}</td><td>{amount(o["bytes"])}</td><td>{"Sí" if o["within_limit"] else "No: revisar"}</td><td><code>{escape(o["sha256"])}</code></td></tr>' for o in result['outputs'])
    input_rows=''.join('<tr>'+''.join(f'<td>{escape(record[k])}</td>' for k in ['archivo','estado','acciones','zips','pendientes','recomendaciones'])+'</tr>' for record in records)
    pending=''.join(f'<li><strong>{escape(p.get("source") or ", ".join(p.get("sources",[])) or "Ejecución")}</strong>{escape(" · páginas "+str(p["pages"]) if p.get("pages") else "")}: {escape(p["reason"])}<p>{escape(p.get("recommendation",""))}</p></li>' for p in result['pending'])
    warnings=''.join(f'<li>{escape(w)}</li>' for w in result['warnings'])
    timeline=''.join(f'<tr><td>{escape(e["time"])}</td><td>{escape(e["phase"])}</td><td>{escape(e["text"])}</td></tr>' for e in result.get('timeline',[]))
    office=''.join(f'<details><summary>{escape(r["source"])} · {escape(r["status"])}</summary><p>{amount(r["original_bytes"])} → {amount(r.get("bytes",r["original_bytes"]))}</p><p>Comprobación: {escape(r.get("verification","original conservado"))}</p><ul>'+''.join(f'<li>{escape(note)}</li>' for note in r.get('notes',[]))+f'</ul><p>{escape(r.get("reason",""))}</p></details>' for r in result.get('office_preparation',[]))
    content=f'''<!doctype html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Informe · Magic ZIP 2.0 by LAVA</title><style>
    :root{{font:14px/1.65 system-ui,sans-serif;color:#e1f0ed;background:#0a1116}}body{{max-width:1200px;margin:40px auto;padding:0 24px}}h1{{font-size:32px;margin-bottom:4px}}h2{{font-size:20px;margin-top:32px;color:#a6f68a}}.brand{{letter-spacing:3px;color:#8ceaf5}}.summary{{display:flex;gap:30px;flex-wrap:wrap;padding:22px;background:#14232a;border:1px solid #355560;border-radius:12px;margin:24px 0}}.summary strong{{display:block;font-size:22px;color:#b0fa84}}.scroll{{overflow:auto}}table{{width:100%;border-collapse:collapse;font-size:12px}}th,td{{padding:11px;text-align:left;vertical-align:top;border-bottom:1px solid #30424a;overflow-wrap:anywhere}}th{{color:#9bdce7}}code{{font-size:10px;word-break:break-all}}details{{padding:14px;background:#14232a;margin:10px 0;border-radius:8px}}summary{{cursor:pointer}}li{{margin-bottom:8px}}.muted{{color:#acc0c4}}@media print{{:root{{background:white;color:black}}h2,th,.brand{{color:#175361}}.summary,details{{background:#f2f6f6;color:black}}.summary strong{{color:#175361}}details{{break-inside:avoid}}}}
    </style></head><body><div class="brand">MAGIC ZIP 2.0 · POWERED BY FISCALOPS · BY LAVA</div><h1>Informe de procesamiento</h1><p class="muted">{escape(status)} · Duración: {result.get('elapsed_seconds',0)} s · Tamaño máximo por salida: {amount(result['limit_bytes'])}</p><div class="summary"><div><strong>{len(result['sources'])}</strong>archivos de entrada</div><div><strong>{sum(o.get('kind')=='zip' for o in result['outputs'])}</strong>ZIP generados</div><div><strong>{len(result['pending'])}</strong>incidencias / pendientes</div><div><strong>Conservados</strong>originales</div></div><p>Las copias preparadas se guardaron en esta ejecución. No se borraron ni sobrescribieron documentos originales. La validación de tamaño no acredita aceptación del contenido por el trámite.</p><h2>Archivos generados</h2><div class="scroll"><table><thead><tr><th>Archivo</th><th>Tamaño final</th><th>Bajo el límite</th><th>SHA-256</th></tr></thead><tbody>{output_rows}</tbody></table></div><h2>Qué ocurrió con cada archivo</h2><div class="scroll"><table><thead><tr><th>Original</th><th>Estado</th><th>Acciones y verificaciones</th><th>ZIP</th><th>Pendiente</th><th>Recomendación</th></tr></thead><tbody>{input_rows}</tbody></table></div><h2>Office: Excel, PowerPoint y Word</h2>{office or '<p>No hubo conversiones Office en esta ejecución.</p>'}<h2>Pendientes</h2><ul>{pending or '<li>Ninguno.</li>'}</ul><h2>Notas de revisión</h2><ul>{warnings or '<li>Sin notas adicionales.</li>'}</ul><h2>Bitácora</h2><div class="scroll"><table><thead><tr><th>Hora</th><th>Etapa</th><th>Acción</th></tr></thead><tbody>{timeline}</tbody></table></div><p class="muted">También se incluyen INFORME_ARCHIVOS.csv y reporte_magiczip.json para revisión detallada. Carpeta: {escape(directory)}</p></body></html>'''
    human=directory/'INFORME_MAGIC_ZIP.html';human.write_text(content,encoding='utf-8')
    result['file_records']=records
    return {'html':str(human),'csv':str(csv_path),'json':result['report_path']}
