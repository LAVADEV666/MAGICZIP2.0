'use strict';
const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];
const token=$('meta[name="magic-token"]').content;
const state={files:[],selected:new Set(),toolSelected:new Set(),toolSignature:'',order:[],tool:'compress',view:'workspace',results:[],job:null,selection:null,busy:false,destination:'',sourceFolder:'',assessments:{},preview:null,sourceRevealPending:false};
const settings={sizeMB:4,smartPdf:true,optimizeExcel:true,excelBinary:true,excelSplitLarge:true,convertPowerpoint:true,convertWord:true,zipLossy:false,structure:'keep',naming:'numbers'};
try{const saved=JSON.parse(localStorage.getItem('magiczip2-settings')||'null');if(saved){if(Number.isFinite(saved.sizeMB)&&saved.sizeMB>=.001&&saved.sizeMB<=2000)settings.sizeMB=saved.sizeMB;for(const k of ['smartPdf','optimizeExcel','excelBinary','excelSplitLarge','convertPowerpoint','convertWord','zipLossy'])if(typeof saved[k]==='boolean')settings[k]=saved[k];settings.structure='keep';if(['numbers','part','part_of'].includes(saved.naming))settings.naming=saved.naming;}}catch{}
const tools=[
 {id:'compress',icon:'↧',title:'Comprimir PDF',short:'Optimizar y ajustar al tamaño.',description:'Optimiza sin pérdida. Si autorizas reducción de imágenes, se usan copias. Divide por páginas si todavía no cabe.',cta:'Comprimir PDF'},
 {id:'split',icon:'↔',title:'Dividir por tamaño',short:'Bloques de páginas completos.',description:'Crea rangos consecutivos bajo tu límite. Se mide cada parte y se conservan las páginas completas.',cta:'Dividir PDF'},
 {id:'merge',icon:'⊕',title:'Unir PDF',short:'Varios documentos en uno.',description:'Ordena los documentos y crea una copia unida. Si supera el tamaño configurado, puede dividirse.',cta:'Unir PDF'},
 {id:'extract',icon:'▤',title:'Extraer y ordenar',short:'Las páginas que necesitas.',description:'Escribe las páginas y su orden: 1-3,5 o 3,1,2. Para eliminar páginas, omítelas de la lista.',cta:'Crear copia de páginas'},
 {id:'rotate',icon:'↻',title:'Rotar páginas',short:'Corrige su orientación.',description:'Gira todas las páginas o solamente una selección. El original se conserva.',cta:'Rotar PDF'},
 {id:'images_pdf',icon:'▧',title:'Imágenes a PDF',short:'JPG, PNG, TIFF y más.',description:'Ordena las imágenes y genera un PDF. Se corrige orientación EXIF y se incluyen los cuadros de TIFF.',cta:'Crear PDF'},
 {id:'pdf_images',icon:'▦',title:'PDF a imágenes',short:'Exportar páginas a PNG.',description:'Genera imágenes de las páginas elegidas y comprueba su tamaño.',cta:'Exportar PNG'}
];
function node(tag,cls,text){const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n;}
function button(text,action,cls='quiet small'){const b=node('button',cls,text);b.type='button';b.addEventListener('click',action);return b;}
function size(n){return n>=1e6?(n/1e6).toLocaleString('es-MX',{maximumFractionDigits:2})+' MB':n>=1000?(n/1000).toLocaleString('es-MX',{maximumFractionDigits:1})+' KB':n+' B';}
function countText(n,singular,plural){return n+' '+(n===1?singular:plural);}
function mb(bytes){const value=Number(bytes||0)/1e6;return value.toLocaleString('es-MX',{minimumFractionDigits:2,maximumFractionDigits:value>0&&value<.01?6:2})+' MB';}
function icon(type,cls=''){
 const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('class','ui-icon '+cls);svg.setAttribute('viewBox',type==='logo'?'0 0 96 96':'0 0 24 24');svg.setAttribute('aria-hidden','true');
 const use=document.createElementNS('http://www.w3.org/2000/svg','use');use.setAttribute('href','#icon-'+type);svg.append(use);return svg;
}
function fileIcon(file){
 const ext=(file.name||'').split('.').pop().toLowerCase();
 const type=file.kind==='zip'||['zip','7z','rar','gz'].includes(ext)?'zip':file.kind==='pdf'||ext==='pdf'?'pdf':['xlsx','xls','xlsm','xlsb','csv'].includes(ext)?'excel':['pptx','ppt','pptm','ppsx'].includes(ext)?'ppt':file.kind==='image'||['png','jpg','jpeg','tiff','tif','webp','bmp','gif'].includes(ext)?'image':'file';
 return icon(type,'type-'+type);
}
function limitBytes(){return Math.round(settings.sizeMB*1e6);}
function selected(){return state.files.filter(f=>state.selected.has(f.id));}
function fileUrl(id,download=false){return `/api/file/${encodeURIComponent(id)}?token=${encodeURIComponent(token)}${download?'&download=1':''}`;}
async function api(path,body){const r=await fetch(path,{method:body===undefined?'GET':'POST',headers:{'X-Magic-Token':token,...(body===undefined?{}:{'Content-Type':'application/json'})},body:body===undefined?undefined:JSON.stringify(body)});const data=await r.json();if(!r.ok){const error=new Error(data.error||'No se completó la operación.');error.status=r.status;throw error;}return data;}
let toastTimer;
function toast(message,error=false){$('#toast').textContent=message;$('#toast').classList.toggle('error',error);$('#toast').hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('#toast').hidden=true,error?10000:5000);}
function fail(e){toast(e.message||String(e),true);}
function saveSettings(){try{localStorage.setItem('magiczip2-settings',JSON.stringify(settings));}catch{}}
function syncSettings(){
 $('#zip-size').value=settings.sizeMB;$('#pdf-size').value=settings.sizeMB;$('#exact-limit').textContent=limitBytes().toLocaleString('es-MX')+' bytes · escribe el tamaño que necesites';$('#structure').value='keep';$('#naming').value=settings.naming;updateNamingPreview();
 for(const [id,k] of [['smart-pdf','smartPdf'],['optimize-excel','optimizeExcel'],['excel-binary','excelBinary'],['excel-split-large','excelSplitLarge'],['convert-powerpoint','convertPowerpoint'],['convert-word','convertWord'],['zip-lossy','zipLossy']])$('#'+id).checked=settings[k];
 const zipLabel=$('#zip-lossy')?.closest('label')?.querySelector('span');if(zipLabel){if(zipLabel.firstChild)zipLabel.firstChild.textContent='Compresión inteligente de imágenes PDF';const note=zipLabel.querySelector('small');if(note)note.textContent='Prueba DPI, gris y blanco/negro por página; solo conserva candidatos con validación visual. Revisa letra pequeña, sellos y códigos.';}
 $('#zip-lossy').disabled=!settings.smartPdf;$('#excel-binary').disabled=!settings.optimizeExcel;
}
function applyState(data){
 const changed=state.files.map(f=>f.id).join(',')!==data.sources.map(f=>f.id).join(',');
 if(changed&&data.sources.length){state.sourceRevealPending=true;$('#folder-plan').replaceChildren();$('#plan-status').textContent='LEYENDO CARPETAS';}
 state.files=data.sources;state.destination=data.destination||'';state.sourceFolder=data.source_folder||'';
 state.selected=new Set([...state.selected].filter(id=>state.files.some(f=>f.id===id)));
 $('#source-label').textContent=state.files.length?`${state.files.length} archivos listos para analizar. PDF, Excel, PowerPoint y Word se preparan automáticamente.`:'PDF, Excel, PowerPoint, Word y otros archivos. Se analizan y preparan en este equipo.';
 $('#selected-folder-caption').textContent=state.sourceFolder||'';
 $('#destination-label').textContent=state.destination||'Elige dónde guardar tus resultados';$('#destination-label').title=state.destination||'';$('#destination').textContent=state.destination?'Cambiar':'Elegir';
 $('#output-label').textContent=state.destination?'SALIDA / '+data.final_directory:'Los resultados se guardan en la carpeta que elijas.';
 $('#office-status').textContent=`Excel nativo: ${data.office?.excel?'disponible':'no disponible'} · PowerPoint: ${data.office?.powerpoint?'disponible':'no disponible'} · Word: ${data.office?.word?'disponible':'no disponible'}`;
 $('#scan-errors').hidden=!(data.scan_errors||[]).length;$('#scan-errors').textContent=(data.scan_errors||[]).map(e=>e.path+' — '+e.reason).join(' / ');
 $('#source-step').hidden=state.files.length>0;$('#review-step').hidden=!state.files.length;
 renderFiles();renderToolSelection();
}
function setView(view){
 const previous=state.view;state.view=view;$$('.view').forEach(e=>e.hidden=e.id!==view+'-view');$$('[data-view]').forEach(b=>{b.classList.toggle('active',b.dataset.view===view);b.setAttribute('aria-current',b.dataset.view===view?'page':'false');});
 const headings={workspace:[state.files.length?'TU EXPEDIENTE':'MAGIC ZIP 2.0',state.files.length?'Tus carpetas. Listas para comprimir.':'Elige tu carpeta. La magia sigue.'],toolbox:['MAGIC PDF / TOOLBOX','Tus PDF, a tu manera.'],results:['RESULTADOS DEL ENVÍO',state.results[0]?.status==='completed'?'Tus ZIP, listos.':'Tu envío, en detalle.'],details:['DETALLE DE LA SESIÓN','Todo lo que hizo Magic ZIP.']};
 $('#section-tag').textContent=headings[view][0];$('#section-title').textContent=headings[view][1];
 if(view==='details'){renderResults();renderFiles(false,true);}if(view==='toolbox')renderToolSelection();
 if(previous!==view)animateScene($('#'+view+'-view'),[{opacity:0,transform:'translateY(8px)'},{opacity:1,transform:'none'}],{duration:240,name:'view-enter'});
}
function updateNamingPreview(){
 const folder=selected()[0]?.collection?.split('/').pop()||'ANEXO 1';
 const suffix={numbers:'1-3',part:'PARTE 1-3',part_of:'PARTE 1 DE 3'}[settings.naming];
 $('#naming-preview').textContent=`Ejemplo: ${folder}_${suffix}.zip`;
}
function needsAttention(f){return !!(f.error||f.signed||f.size>=limitBytes());}
function risk(f){if(f.error)return f.error;if(f.signed)return 'Firma detectada · se conserva sin editar';const ext=f.name.split('.').pop().toLowerCase();if(['xlsx','xlsm','xlsb','xls'].includes(ext)&&settings.optimizeExcel)return f.size>4e6&&settings.excelSplitLarge?'Excel > 4 MB: valores y una pestaña por archivo':'Excel: aligerar y validar; conservar original';if(['pptx','pptm','ppt','ppsx','ppsm','pps'].includes(ext)&&settings.convertPowerpoint)return 'PowerPoint → PDF → bloques si hace falta';if(['docx','docm','doc','dotx','dotm','dot','odt','rtf'].includes(ext)&&settings.convertWord)return 'Word → PDF → bloques si hace falta';return state.assessments[f.id]?.label||(f.kind==='pdf'&&f.size>=limitBytes()?'PDF automático · optimizar y dividir':f.pages?f.pages+' página(s)':'Empaquetar y verificar');}
function renderFiles(refreshPlan=true,force=false){
 $('#file-count').textContent=state.files.length;$('#inventory-summary').textContent=`${state.selected.size} seleccionados · ${size(selected().reduce((n,f)=>n+f.size,0))}`;$('#empty-state').hidden=state.files.length>0;const list=$('#file-list');list.replaceChildren();
 if(force||state.view==='details')for(const f of state.files){if($('#only-risk').checked&&!needsAttention(f))continue;const row=node('div','file-row');const check=node('input');check.type='checkbox';check.checked=state.selected.has(f.id);check.setAttribute('aria-label','Incluir '+f.relative);check.addEventListener('change',()=>{check.checked?state.selected.add(f.id):state.selected.delete(f.id);renderFiles();});const copy=node('div','file-copy');copy.append(node('span','file-name',f.relative),node('span','file-meta',risk(f)));row.append(check,fileIcon(f),copy,node('span','file-weight',size(f.size)));if(['pdf','image'].includes(f.kind)&&!f.error)row.append(button('Ver',()=>preview(f)));const remove=button('×',async()=>{try{applyState(await api('/api/remove',{ids:[f.id]}));}catch(e){fail(e);}});remove.setAttribute('aria-label','Quitar '+f.name);row.append(remove);list.append(row);}
 $('#run-zip').disabled=state.busy||!state.selected.size;updateNamingPreview();if(refreshPlan)schedulePlan();
}
function busy(active,title='Procesando',detail='',mode='process'){
 const panel=$('#progress-panel'),previousMode=panel.dataset.mode;
 if(active){panel.dataset.mode=mode;$('#processing-eyebrow').textContent=mode==='source'?'CONECTANDO TUS DOCUMENTOS':'PREPARANDO TU ENVÍO';$('#progress-panel .process-note').textContent=mode==='source'?'Lectura local. Los originales permanecen intactos.':'Originales intactos. ZIP independientes y verificados.';}
 state.busy=active;document.body.classList.toggle('busy',active);$('#progress-panel').hidden=!active;$('#progress-title').textContent=title;$('#progress-detail').textContent=detail;$('#progress').removeAttribute('value');$('#cancel-job').hidden=!(state.job||state.selection);$('#cancel-job').disabled=false;
 $('.shell').inert=active||(!$('#matrix-intro').hidden&&!$('#matrix-intro').inert);
 if(active){clearTimeout(toastTimer);$('#toast').hidden=true;$('#progress-panel').focus();}
 for(const id of ['choose-folder','choose-files','tool-add','destination','clear-files','zip-size','pdf-size','smart-pdf','optimize-excel','excel-binary','excel-split-large','convert-powerpoint','convert-word','zip-lossy','structure','naming','new-session','change-source'])$('#'+id).disabled=active;
 if(!active)syncSettings();processExperience(active);renderFiles();renderToolSelection();
 if(active)animateScene($('.processing-frame'),[{opacity:0,transform:'translateY(10px)'},{opacity:1,transform:'none'}],{duration:220,name:'process-enter'});
 else if(previousMode==='source'&&state.sourceRevealPending)animateScene($('#review-step'),[{opacity:0,transform:'translateY(14px)'},{opacity:1,transform:'none'}],{duration:360,name:'source-arrive'});
}
async function finishSelection(id,kind='folder'){
 state.selection=id;$('#cancel-job').hidden=false;
 try{for(;;){
  let task;try{task=await api('/api/selection/'+id);}catch(error){if(error.status&&error.status<500)throw error;$('#progress-detail').textContent='Reconectando al motor local…';await new Promise(resolve=>setTimeout(resolve,400));continue;}if(state.selection!==id)return;
  $('#progress-title').textContent=task.stage;
  $('#progress-detail').textContent=[task.selected_path,task.phase==='inventory'?`${task.current} archivos encontrados · ${size(task.bytes)}${task.detail&&task.detail!==task.selected_path?' · '+task.detail:''}`:task.detail].filter(Boolean).join(' · ');
  $('#progress').removeAttribute('value');$('#progress-percent').textContent=task.total?`${task.current} / ${task.total}`:'…';
  if(task.status==='completed'){applyState(task.state);state.selected=new Set(state.files.map(f=>f.id));renderFiles();if(kind==='folder')setView('workspace');return;}
  if(task.status==='cancelled'){toast('Selección cancelada.');return;}
  if(task.status==='error')throw new Error(task.detail||task.stage);
  await new Promise(resolve=>setTimeout(resolve,100));
 }}finally{if(state.selection===id){state.selection=null;busy(false);}}
}
async function importFolder(){if(state.busy)return;const path=$('#source-folder-path').value;$('#source-folder-dialog').close();busy(true,'Leyendo archivos',path,'source');try{const task=await api('/api/source-folder',{path,background:true});await finishSelection(task.id);}catch(e){if(!state.selection&&state.busy)busy(false);fail(e);}}
async function nativeChoose(kind){if(state.busy)return;$('#source-folder-dialog').close();$('#destination-dialog').close();busy(true,kind==='destination'?'Elige el destino en Windows':kind==='files'?'Agrega tus archivos en Windows':'Elige la carpeta en Windows','','source');try{const task=await api('/api/choose',{kind,background:true});await finishSelection(task.id,kind);}catch(e){if(!state.selection&&state.busy)busy(false);fail(e);}}
async function upload(files){
 if(state.busy||!files.length)return;
 files=files.filter(f=>!(f.webkitRelativePath||f.name).split('/').some(p=>p.toUpperCase()==='ZIPS FINALES'));
 busy(true,'Agregando archivos locales','','source');
 try{for(let i=0;i<files.length;i++){const f=files[i];$('#progress-detail').textContent=`${i+1} / ${files.length} · ${f.name}`;$('#progress').max=files.length;$('#progress').value=i;const r=await fetch('/api/upload?name='+encodeURIComponent(f.webkitRelativePath||f.name),{method:'POST',headers:{'X-Magic-Token':token},body:f});const d=await r.json();if(!r.ok)throw new Error(d.error);state.selected.add(d.id);}applyState(await api('/api/state'));}
 catch(e){try{applyState(await api('/api/state'));}catch{}fail(e);}finally{busy(false);$('#file-input').value='';$('#folder-input').value='';}
 if(!state.destination){$('#destination-path').value='';$('#destination-dialog').showModal();}
}
function renderTools(){const root=$('#tool-grid');for(const tool of tools){const b=button('',()=>setTool(tool.id),'tool-card');b.dataset.tool=tool.id;b.append(icon(tool.id,'tool-icon'),node('h3','',tool.title),node('p','',tool.short));root.append(b);}setTool(state.tool);}
function setTool(id){state.tool=id;state.toolSignature='';const t=tools.find(t=>t.id===id);$$('[data-tool]').forEach(b=>{b.classList.toggle('selected',b.dataset.tool===id);b.setAttribute('aria-pressed',String(b.dataset.tool===id));});$('#tool-number').textContent=String(tools.indexOf(t)+1).padStart(2,'0');$('#tool-title').textContent=t.title;$('#tool-description').textContent=t.description;$('#run-tool').textContent=t.cta+' →';$('#compression-settings').hidden=id!=='compress';$('#auto-split-field').hidden=['split','pdf_images'].includes(id);$('#pages-field').hidden=!['extract','rotate','pdf_images'].includes(id);$('#angle-field').hidden=id!=='rotate';renderToolSelection();}
function renderToolSelection(){
 if(state.view!=='toolbox')return;
 const eligible=state.files.filter(f=>state.tool==='images_pdf'?f.kind==='image':f.kind==='pdf');const signature=state.tool+eligible.map(f=>f.id).join(',');if(signature!==state.toolSignature){state.toolSelected=new Set(eligible.map(f=>f.id));state.toolSignature=signature;}
 const root=$('#tool-selection');root.replaceChildren();for(const f of eligible){const b=button(f.name,()=>{state.toolSelected.has(f.id)?state.toolSelected.delete(f.id):state.toolSelected.add(f.id);renderToolSelection();},'selection-chip');b.prepend(fileIcon(f));b.setAttribute('aria-pressed',String(state.toolSelected.has(f.id)));root.append(b);}if(!eligible.length)root.append(node('p','micro','Agrega '+(state.tool==='images_pdf'?'imágenes':'PDF')+' para usar esta herramienta.'));
 const files=eligible.filter(f=>state.toolSelected.has(f.id));state.order=[...state.order.filter(id=>state.toolSelected.has(id)),...files.map(f=>f.id).filter(id=>!state.order.includes(id))];const order=$('#tool-order');order.replaceChildren();order.hidden=!['merge','images_pdf'].includes(state.tool)||files.length<2;
 if(!order.hidden)state.order.forEach((id,index)=>{const f=files.find(f=>f.id===id);const row=node('div','order-row');row.append(node('span','',`${index+1}. ${f.name}`));for(const delta of [-1,1]){const b=button(delta<0?'↑':'↓',()=>{const j=index+delta;[state.order[index],state.order[j]]=[state.order[j],state.order[index]];renderToolSelection();},'small');b.disabled=index+delta<0||index+delta>=state.order.length;row.append(b);}order.append(row);});
 $('#tool-warning').hidden=!files.some(f=>f.signed||f.encrypted||f.error);$('#tool-warning').textContent='Los documentos firmados, cifrados o con incidencias quedarán pendientes sin editar.';$('#run-tool').disabled=state.busy||!files.length;
}
async function start(action){
 if(state.busy)return;const sizeField=$(action==='zip'?'#zip-size':'#pdf-size');const entered=Number(sizeField.value);if(!Number.isFinite(entered)||entered<.001||entered>2000){toast('Escribe un tamaño válido entre 0.001 y 2000 MB.',true);sizeField.focus();return;}settings.sizeMB=entered;saveSettings();syncSettings();const ids=action==='zip'?selected().map(f=>f.id):['merge','images_pdf'].includes(action)?state.order:[...state.toolSelected];if(!ids.length)return toast('Selecciona documentos para comenzar.',true);if(!state.destination){$('#destination-path').value='';$('#destination-dialog').showModal();return;}
 if((action==='compress'&&$('#lossy').checked||action==='zip'&&settings.zipLossy)&&!window.confirm('Reducir imágenes puede perder detalle. Se conservarán los originales. ¿Crear copias con esta reducción?'))return;
 const [quality,max_edge]=$('#quality').value.split(':').map(Number);const options={limit_bytes:limitBytes(),smart_pdf:settings.smartPdf,optimize_excel:settings.optimizeExcel,excel_binary:settings.excelBinary,excel_split_large:settings.excelSplitLarge,convert_powerpoint:settings.convertPowerpoint,convert_word:settings.convertWord,zip_lossy:settings.zipLossy,structure:'keep',naming:settings.naming,lossy:$('#lossy').checked,quality,max_edge,auto_split:$('#auto-split').checked,pages:$('#pages').value,angle:Number($('#angle').value)};
 busy(true,'Preparando tus documentos');try{state.job=(await api('/api/jobs',{action,ids,options})).id;$('#cancel-job').hidden=false;pollJob(state.job);}catch(e){state.job=null;busy(false);fail(e);}
}
async function pollJob(id){try{const job=await api('/api/job/'+id);$('#progress-title').textContent=job.stage;$('#progress-detail').textContent=job.detail;$('#progress').max=job.total||1;$('#progress').value=job.current||0;updateProcessExperience(job);if(job.phase==='office'&&!job.current){$('#progress').removeAttribute('value');$('#progress-percent').textContent='…';}if(job.status==='running'){setTimeout(()=>pollJob(id),400);return;}if(job.result){state.results.unshift({...job.result,jobId:id});renderResults();setView('results');}else toast(job.detail||'No se completó el proceso.',true);state.job=null;busy(false);}catch(e){$('#progress-detail').textContent='Reconectando al motor local…';setTimeout(()=>pollJob(id),1800);}}
function reportLink(result,kind,text,download=false){const id=result.report_ids?.[kind]||(kind==='json'?result.report_id:null);if(!id)return null;const a=node('a',download?'text-link':'button primary',text);a.href=fileUrl(id,download);if(!download){a.target='_blank';a.rel='noopener';}return a;}
function outputRow(output){
 const row=node('div','result-row');row.append(fileIcon(output));
 const copy=node('div','result-file-copy');copy.append(node('h3','',output.name));
 const weight=node('strong','result-file-weight',mb(output.bytes));weight.title=output.bytes.toLocaleString('es-MX')+' bytes';
 const controls=node('div','result-file-actions');if(['pdf','image'].includes(output.kind))controls.append(button('Ver',()=>preview(output),'quiet small'));
 const download=node('a','zip-download');download.href=fileUrl(output.id,true);download.setAttribute('aria-label','Descargar '+output.name);download.title='Descargar';download.append(icon('download'));controls.append(download);
 row.append(copy,weight,controls);return row;
}
function folderData(result,folder){
 const sources=result.sources.filter(s=>(s.collection||(s.relative.includes('/')?s.relative.split('/')[0]:'Archivos sueltos'))===folder.name);
 const relatives=new Set(sources.map(s=>s.relative));
 const issues=folder.issues||result.pending.filter(p=>relatives.has(p.source)||(p.sources||[]).some(s=>relatives.has(s)));
 const outputs=result.outputs.filter(o=>o.collection===folder.name);
 return {sources,outputs,issues,notes:folder.notes||[],changes:folder.changes||{},inputBytes:folder.input_bytes??sources.reduce((n,s)=>n+s.size,0),outputBytes:outputs.reduce((n,o)=>n+o.bytes,0)};
}
function findingKey(issue){return JSON.stringify([issue.source,issue.sources,issue.reason,issue.pages]);}
function sourceLocationButton(result,request,label,directory,cls='source-location-link'){
 const control=button('',async()=>{control.disabled=true;try{await api('/api/open-source',{job:result.jobId,...request});}catch(e){fail(e);}finally{control.disabled=false;}},'quiet '+cls);
 control.append(icon('folder'),node('span','',label+' ↗'));control.title=directory;return control;
}
function folderFindings(result,folder,data){
 const pending=Math.max(folder.pending||0,data.issues.length);
 if(!pending)return null;
 const box=node('details','folder-findings'),content=node('div','findings-content');
 for(const issue of data.issues){
  const line=node('div','finding-issue'),text=node('div');let source=issue.source||issue.sources?.join(', ')||folder.label;
  if(source.startsWith(folder.label+'/'))source=source.slice(folder.label.length+1);
  text.append(node('strong','',source+(issue.pages?.length?' · página(s) '+issue.pages.join(', '):'')),node('p','',issue.reason));line.append(icon('alert'),text);
  const original=data.sources.find(s=>s.id===issue.source_id||s.relative===issue.source||(issue.sources||[]).includes(s.relative));
  const location=original&&result.source_access?.files?.[original.id];
  if(location?.available)text.append(sourceLocationButton(result,{source_id:original.id},'Ver ubicación',location.directory));
  if(issue.recommendation)text.append(node('p','finding-recommendation',issue.recommendation));content.append(line);
 }
 if(!data.issues.length)content.append(node('p','warnings','Hay archivos que requieren revisión. Consulta Detalle para ver cada caso.'));
 const access=result.source_access?.folders?.[folder.name];
 if(access?.available)content.append(sourceLocationButton(result,{collection:folder.name},'Abrir carpeta fuente',access.directory));
 else if(access?.browser_upload)content.append(node('p','source-location-note','El navegador no comparte la ruta original. Usa Examinar en Windows al elegir la carpeta para activar el acceso a su origen.'));
 else if(access?.multiple)content.append(node('p','source-location-note','Esta selección tiene varias ubicaciones. Usa Ver ubicación junto al archivo pendiente.'));
 const heading=node('summary','has-findings');heading.append(icon('alert'),node('span','',countText(pending,'pendiente','pendientes')));box.append(heading,content);return box;
}
function renderResults(){
 const root=$('#results'),technical=$('#technical-results');root.replaceChildren();technical.replaceChildren();$('#result-empty').hidden=state.results.length>0;$('#results-count').textContent=state.results.reduce((n,r)=>n+r.outputs.filter(o=>o.kind==='zip').length,0);
 for(const result of state.results){
  const group=node('article','result-group native-result');const zip=result.action==='zip';const totalBytes=result.outputs.reduce((n,o)=>n+o.bytes,0);
  const header=node('div','result-header');const copy=node('div');
  copy.append(node('span','eyebrow',zip?'MAGIC ZIP / ENVÍO':'MAGIC PDF / RESULTADO'),node('h2','',countText(result.outputs.length,zip?'ZIP generado':'archivo generado',zip?'ZIP generados':'archivos generados')));
  const completed=result.status==='completed';const statusText={completed:'✓ Verificado',review:'⚠ Con pendientes',cancelled:'Proceso cancelado',error:'No completado'}[result.status]||result.status;
  header.append(copy,node('span','result-status'+(completed?'':' review'),statusText));group.append(header);
  const stats=node('div','result-total-line');stats.append(icon(zip?'zip':'file'),node('strong','',mb(totalBytes)),node('span','','finales'),node('span','result-total-separator','/'),icon('folder'),node('span','',countText(result.folders?.length||1,zip?'carpeta':'selección',zip?'carpetas':'selecciones')));group.append(stats);
  const actions=node('div','report-actions');const open=button('',async()=>{try{await api('/api/open-output',{job:result.jobId});}catch(e){fail(e);}},'primary');open.append(icon('folder'),node('span','','Abrir resultados ↗'));actions.append(open,button('Nuevo ZIP',resetSession));group.append(actions);
  const summary=node('div','result-summary');const matchedIssues=new Set();
  if(result.folders?.length){
   for(const folder of result.folders){
    const data=folderData(result,folder);data.issues.forEach(p=>matchedIssues.add(findingKey(p)));
    const block=node('section','folder-result');block.setAttribute('aria-label',folder.label);
    const heading=node('div','folder-result-heading');const title=node('div','folder-result-title');const titleCopy=node('div');titleCopy.append(node('h3','',folder.label));const origin=node('div','folder-origin-row');origin.append(node('span','folder-origin',`${mb(data.inputBytes)} de origen`));const access=result.source_access?.folders?.[folder.name];if(access?.available)origin.append(sourceLocationButton(result,{collection:folder.name},'Carpeta fuente',access.directory,'source-folder-link'));titleCopy.append(origin);title.append(icon('folder','type-folder'),titleCopy);
    const totals=node('div','folder-final-totals');totals.append(node('strong','',mb(data.outputBytes)),node('span','',countText(data.outputs.length,'ZIP generado','ZIP generados')));heading.append(title,totals);block.append(heading);
    const status=folder.pending?'⚠ '+countText(folder.pending,'pendiente','pendientes'):folder.status==='completed'?'✓ Todo incluido':folder.status==='review'?'⚠ Requiere revisión':folder.status==='cancelled'?'Proceso cancelado':folder.status==='error'?'No completada':'Sin procesar';
    if(!folder.pending)block.append(node('p','folder-outcome'+(['review','error','cancelled'].includes(folder.status)?' warnings':''),status));
    const findings=folderFindings(result,folder,data);if(findings)block.append(findings);
    for(const output of data.outputs)block.append(outputRow(output));
    if(!data.outputs.length)block.append(node('p','micro no-folder-output','No se generaron ZIP de esta carpeta.'));
    summary.append(block);
   }
  }else for(const output of result.outputs)summary.append(outputRow(output));
  const unassigned=result.pending.filter(p=>!matchedIssues.has(findingKey(p)));
  if(unassigned.length){const box=node('details','general-findings');box.append(node('summary','','⚠ Pendientes del envío · '+unassigned.length));for(const issue of unassigned)box.append(node('p','',`${issue.source||issue.sources?.join(', ')||'Proceso'}: ${issue.reason}`));summary.append(box);}
  summary.append(button('Detalle de cambios →',()=>setView('details'),'quiet small'),node('p','result-location',result.directory));group.append(summary);root.append(group);
  if(state.view==='details')renderTechnicalResult(result,technical);
 }
}
function renderTechnicalResult(result,technical){
 const detail=node('article','result-group technical-group');detail.append(node('h2','',result.action==='zip'?'Detalle del envío':'Detalle de Magic PDF'),node('p','micro',`${result.sources.length} archivos · máximo ${mb(result.limit_bytes)} · ${result.elapsed_seconds} s`));
 const report=reportLink(result,'html','Abrir informe de la sesión ↗');if(report)detail.append(report);
 for(const office of result.office_preparation||[]){const block=node('details','report-details');block.append(node('summary','',office.source));for(const note of office.notes||[])block.append(node('p','micro',note));if(office.reason)block.append(node('p','warnings',office.reason));detail.append(block);}
 renderPreparation(result,detail);
 if(result.warnings?.length){const notes=node('details','report-details processing-notes');notes.append(node('summary','','Cambios y notas del proceso'));for(const note of result.warnings)notes.append(node('p','micro',note));detail.append(notes);}
 for(const record of result.file_records||[]){const block=node('details','report-details');block.append(node('summary','',record.archivo+' · '+record.estado),node('p','micro',record.acciones));if(record.pendientes)block.append(node('p','warnings',record.pendientes));if(record.recomendaciones)block.append(node('p','micro',record.recomendaciones));detail.append(block);}
 const timeline=node('details','report-details');timeline.append(node('summary','','Bitácora'));for(const event of result.timeline||[])timeline.append(node('p','micro',event.time+' · '+event.text));detail.append(timeline);technical.append(detail);
}
async function resetSession(){
 if(state.busy)return;
 try{const data=await api('/api/clear',{});state.selected.clear();state.toolSelected.clear();state.order=[];state.results=[];state.assessments={};state.preview=null;state.job=null;$('#file-input').value='';$('#folder-input').value='';$('#preview-image').removeAttribute('src');$('#processing-folders').replaceChildren();$$('dialog[open]').forEach(d=>d.close());applyState(data);renderResults();setView('workspace');toast('Sesión limpia. Elige una nueva carpeta.');}catch(e){fail(e);}
}
async function preview(file){
 const opened={file,page:0,total:file.pages||1};state.preview=opened;$('#preview-title').textContent=file.name;$('#preview-download').href=fileUrl(file.id);$('#preview-message').textContent=file.pending_inspection?'Leyendo el documento seleccionado…':'Revisa textos, folios, sellos y códigos. El tamaño correcto no sustituye la revisión del contenido.';$('#preview-dialog').showModal();
 try{if(file.pending_inspection){$('#preview-image').removeAttribute('src');$('#preview-counter').textContent='Leyendo páginas…';const inspected=await api('/api/inspect',{id:file.id});if(inspected.error)throw new Error(inspected.error);opened.file=inspected;opened.total=inspected.pages||1;state.files=state.files.map(item=>item.id===file.id?inspected:item);}
 if(state.preview!==opened||!$('#preview-dialog').open)return;$('#preview-message').textContent='Revisa textos, folios, sellos y códigos. El tamaño correcto no sustituye la revisión del contenido.';renderPreview();
 }catch(error){if(state.preview===opened)$('#preview-message').textContent=error.message;fail(error);}
}
function renderPreview(){const p=state.preview;$('#preview-counter').textContent=`Página ${p.page+1} / ${p.total}`;$('#prev-page').disabled=p.page===0;$('#next-page').disabled=p.page>=p.total-1;$('#preview-image').classList.add('loading');$('#preview-image').src=`/api/thumbnail/${p.file.id}?token=${encodeURIComponent(token)}&page=${p.page}`;}
$$('[data-view]').forEach(b=>b.addEventListener('click',()=>setView(b.dataset.view)));$('.brand').addEventListener('click',e=>{e.preventDefault();setView('workspace');});
$('#choose-folder').addEventListener('click',()=>{$('#source-folder-path').value=state.sourceFolder;$('#source-folder-dialog').showModal();});$('#close-source').addEventListener('click',()=>$('#source-folder-dialog').close());$('#save-source').addEventListener('click',importFolder);$('#source-folder-path').addEventListener('keydown',e=>{if(e.key==='Enter')importFolder();});$('#browse-source').addEventListener('click',()=>nativeChoose('folder'));$('#browser-folder').addEventListener('click',()=>{$('#source-folder-dialog').close();$('#folder-input').click();});
$('#choose-files').addEventListener('click',()=>$('#file-input').click());$('#tool-add').addEventListener('click',()=>$('#file-input').click());$('#file-input').addEventListener('change',e=>upload([...e.target.files]));$('#folder-input').addEventListener('change',e=>upload([...e.target.files]));$('#destination').addEventListener('click',()=>{$('#destination-path').value=state.destination;$('#destination-dialog').showModal();});$('#close-destination').addEventListener('click',()=>$('#destination-dialog').close());$('#browse-destination').addEventListener('click',()=>nativeChoose('destination'));$('#save-destination').addEventListener('click',async()=>{try{applyState(await api('/api/destination',{path:$('#destination-path').value}));$('#destination-dialog').close();toast('Destino elegido. Se creará ZIPS FINALES dentro.');}catch(e){fail(e);}});
for(const id of ['zip-size','pdf-size']){
 const update=e=>{const value=Number(e.target.value);if(!Number.isFinite(value)||value<.001||value>2000){$('#exact-limit').textContent='Escribe un tamaño entre 0.001 y 2000 MB';return;}settings.sizeMB=value;state.assessments={};saveSettings();$(id==='zip-size'?'#pdf-size':'#zip-size').value=value;$('#exact-limit').textContent=limitBytes().toLocaleString('es-MX')+' bytes · escribe el tamaño que necesites';renderFiles();};
 $('#'+id).addEventListener('input',update);$('#'+id).addEventListener('change',update);
}
for(const [id,key] of [['smart-pdf','smartPdf'],['optimize-excel','optimizeExcel'],['excel-binary','excelBinary'],['excel-split-large','excelSplitLarge'],['convert-powerpoint','convertPowerpoint'],['convert-word','convertWord'],['zip-lossy','zipLossy']])$('#'+id).addEventListener('change',e=>{settings[key]=e.target.checked;saveSettings();syncSettings();renderFiles();});$('#structure').addEventListener('change',e=>{settings.structure=e.target.value;saveSettings();});
$('#select-all').addEventListener('click',()=>{state.selected=state.selected.size===state.files.length?new Set():new Set(state.files.map(f=>f.id));renderFiles();});$('#clear-files').addEventListener('click',resetSession);$('#only-risk').addEventListener('change',()=>renderFiles(false));$('#run-zip').addEventListener('click',()=>start('zip'));$('#run-tool').addEventListener('click',()=>start(state.tool));$('#cancel-job').addEventListener('click',async()=>{try{await api(state.selection?'/api/cancel-selection':'/api/cancel',{id:state.selection||state.job});$('#cancel-job').disabled=true;$('#progress-detail').textContent='Cancelando y cerrando las copias de trabajo…';}catch(e){fail(e);}});
$('#dropzone').addEventListener('dragover',e=>{e.preventDefault();$('#dropzone').classList.add('dragover');});$('#dropzone').addEventListener('dragleave',()=>$('#dropzone').classList.remove('dragover'));$('#dropzone').addEventListener('drop',e=>{e.preventDefault();$('#dropzone').classList.remove('dragover');upload([...e.dataTransfer.files]);});$('#close-preview').addEventListener('click',()=>$('#preview-dialog').close());$('#prev-page').addEventListener('click',()=>{state.preview.page--;renderPreview();});$('#next-page').addEventListener('click',()=>{state.preview.page++;renderPreview();});$('#preview-image').addEventListener('load',()=>$('#preview-image').classList.remove('loading'));$('#preview-image').addEventListener('error',()=>{toast('No se pudo generar esta vista previa.',true);$('#preview-image').classList.remove('loading');});
syncSettings();renderTools();api('/api/state').then(data=>{applyState(data);state.selected=new Set(state.files.map(f=>f.id));state.results=(data.jobs||[]).filter(j=>j.result).reverse().map(j=>({...j.result,jobId:j.id}));renderResults();const active=(data.jobs||[]).find(j=>j.status==='running');if(active){state.job=active.id;busy(true,'Recuperando proceso');pollJob(active.id);}else if(data.selection?.status==='running'){state.selection=data.selection.id;busy(true,'Recuperando selección','','source');finishSelection(data.selection.id,data.selection.kind).catch(fail);}renderFiles();setView(state.view);}).catch(fail);

$('#naming').addEventListener('change',e=>{settings.naming=e.target.value;saveSettings();updateNamingPreview();});
$('#new-session').addEventListener('click',resetSession);
$('#change-source').addEventListener('click',()=>$('#choose-folder').click());
const windowClient=crypto.randomUUID();
const sessionWatch=new EventSource('/api/session/watch?token='+encodeURIComponent(token)+'&client='+encodeURIComponent(windowClient));
window.addEventListener('pagehide',()=>{sessionWatch.close();fetch('/api/session/close',{method:'POST',keepalive:true,headers:{'X-Magic-Token':token,'Content-Type':'application/json'},body:JSON.stringify({client:windowClient})}).catch(()=>{});});
window.addEventListener('pageshow',event=>{if(event.persisted)location.reload();});
