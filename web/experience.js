'use strict';

let planTimer, planSequence = 0;
let processStarted = 0, elapsedTimer;
const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
const matrixScenes = [];
const sceneAnimations = new Set();
let introExit = null, folderPlanSignature = null;

function animateScene(element,keyframes,{duration=260,delay=0,name='scene'}={}){
 if(!element||!motionActive()||element.closest('[hidden]')||!element.getClientRects().length)return null;
 const animation=element.animate(keyframes,{duration,delay,easing:'cubic-bezier(.2,.75,.25,1)',fill:'backwards'});
 animation.id='mz-'+name;sceneAnimations.add(animation);
 animation.finished.then(()=>sceneAnimations.delete(animation),()=>sceneAnimations.delete(animation));return animation;
}
function revealScene(elements,name='scene',duration=280,step=45){
 [...elements].forEach((element,index)=>animateScene(element,[{opacity:0,transform:'translateY(10px)'},{opacity:1,transform:'none'}],{duration,delay:Math.min(index,7)*step,name}));
}

function motionActive() { return !reducedMotion.matches && !document.hidden; }

function createRain(canvas) {
  const context = canvas.getContext('2d');
  if (!context) return;
  let columns = [], width = 0, height = 0, frame, last = 0;
  const chars = '01010123456789ABCDEFｱｲｳｴｵｶｷｸ';
  function resize() {
    const bounds = canvas.getBoundingClientRect();
    const scale = Math.min(window.devicePixelRatio || 1, 1.5);
    width = Math.ceil(bounds.width); height = Math.ceil(bounds.height);
    canvas.width = width * scale; canvas.height = height * scale;
    context.setTransform(scale, 0, 0, scale, 0, 0);
    columns = Array.from({length: Math.ceil(width / 22)}, () => (Math.random()*1.2-.2)*height/20);
  }
  function visible() { return !canvas.closest('[hidden]') && width > 0 && height > 0; }
  function draw(time) {
    if (!motionActive() || !visible()) { frame = undefined; return; }
    frame = requestAnimationFrame(draw);
    if (time - last < 45) return;
    last = time;
    context.fillStyle = 'rgba(5, 13, 8, 0.11)';
    context.fillRect(0, 0, width, height);
    context.font = '14px Consolas, monospace';
    columns.forEach((y, i) => {
      const accent=Math.random();
      context.fillStyle = accent>.985?'#f18cce':accent>.95?'#73e8f1':'#8bdb63';
      context.fillText(chars[Math.floor(Math.random() * chars.length)], i * 22, y * 20);
      columns[i] = y * 20 > height && Math.random() > .97 ? -Math.random() * 12 : y + .6;
    });
  }
  function refresh() {
    if (frame !== undefined) { cancelAnimationFrame(frame); frame = undefined; }
    resize();
    if (motionActive() && visible()) frame = requestAnimationFrame(draw);
    else if (visible()) {
      context.fillStyle = '#224e29'; context.font = '13px Consolas, monospace';
      columns.forEach((y, i) => context.fillText('0101AF'[i % 6], i * 22, 25 + i % 5 * 20));
    }
  }
  new ResizeObserver(refresh).observe(canvas);
  matrixScenes.push(refresh);
  refresh();
}

function updateMotion() {
  document.body.classList.toggle('motion-off', reducedMotion.matches);
  document.body.classList.toggle('tab-hidden', document.hidden);
  if(!motionActive())for(const animation of sceneAnimations)animation.finish();
  matrixScenes.forEach(refresh => refresh());
}

function openMatrixIntro() {
  if(introExit){introExit.cancel();introExit=null;}
  const intro=document.getElementById('matrix-intro');intro.hidden=false;intro.inert=false;intro.classList.remove('intro-closing');
  document.querySelector('.shell').inert=true;document.body.classList.add('intro-open');
  matrixScenes.forEach(refresh=>refresh());
  revealScene(document.querySelector('.intro-content').children,'intro-enter',400,35);
  document.getElementById('enter-app').focus({preventScroll:true});
}
function closeMatrixIntro() {
  const intro=document.getElementById('matrix-intro');if(intro.hidden||intro.inert)return;
  intro.inert=true;intro.classList.add('intro-closing');document.querySelector('.shell').inert=state.busy;document.body.classList.remove('intro-open');
  try{sessionStorage.setItem('magiczip-intro-seen','yes');}catch{}
  const finish=()=>{intro.hidden=true;intro.classList.remove('intro-closing');introExit=null;matrixScenes.forEach(refresh=>refresh());};
  const animation=animateScene(intro,[{opacity:1},{opacity:0}],{duration:180,name:'intro-exit'});
  introExit=animation;if(animation)animation.finished.then(()=>{if(introExit===animation)finish();},()=>{});else finish();
  revealScene([document.querySelector('.app-header'),document.querySelector('.page-heading'),document.querySelector('#workspace-view')],'workspace-enter',330,45);
  (state.busy?document.getElementById('progress-panel'):(document.querySelector('#review-step:not([hidden]) input')||document.getElementById('choose-folder'))).focus({preventScroll:true});
}

function schedulePlan() {
  clearTimeout(planTimer);
  const sequence = ++planSequence;
  planTimer = setTimeout(async () => {
    if (state.busy) return;
    try {
      const plan = await api('/api/plan', {ids: state.files.map(f => f.id), options: {limit_bytes: limitBytes(), smart_pdf: settings.smartPdf}});
      if (sequence !== planSequence) return;
      state.assessments = Object.fromEntries(plan.assessments.map(a => [a.id, a]));
      renderStrategy(plan);
      renderFiles(false);
    } catch (exc) {
      if (sequence === planSequence) {
        document.getElementById('plan-status').textContent = 'NO SE PUDO ESTIMAR';
        document.getElementById('plan-pdfs').textContent = 'Recarga la aplicación para actualizar el diagnóstico: ' + exc.message;
      }
    }
  }, 180);
}

function renderStrategy(plan) {
  document.getElementById('plan-status').textContent = plan.files ? plan.folders.length+' CARPETA(S)' : 'ESPERANDO ARCHIVOS';
  document.getElementById('plan-bytes').textContent = size(plan.bytes);
  document.getElementById('plan-count').textContent = `${plan.files} / ${plan.folders.length}`;
  document.getElementById('plan-zips').textContent = plan.files ? '≈ ' + plan.estimated_zips : '—';
  document.getElementById('plan-manual').textContent = plan.manual_risks;
  document.getElementById('plan-manual').classList.toggle('attention-text', plan.manual_risks > 0);
  document.getElementById('plan-pdfs').textContent = plan.files ? `${plan.automatic_pdfs} PDF con preparación automática · cada carpeta mantiene sus propios ZIP.` : 'Agrega una carpeta para conocer el plan de procesamiento.';
  const folders = document.getElementById('folder-plan');
  const focusedFolder=folders.contains(document.activeElement)?document.activeElement.closest('.folder-plan-row')?.dataset.folder:null;
  folders.replaceChildren();
  for (const folder of plan.folders) {
    const files=state.files.filter(f=>(f.collection||(f.relative.includes('/')?f.relative.split('/')[0]:'Archivos sueltos'))===folder.name);
    const included=files.filter(f=>state.selected.has(f.id)).length;
    const row = node('label', 'folder-plan-row'+(!included?' excluded':''));
    row.dataset.folder=folder.name;
    const check=node('input');check.type='checkbox';check.checked=included===files.length;check.indeterminate=included>0&&included<files.length;check.disabled=state.busy;check.setAttribute('aria-label','Comprimir '+folder.name.split('/').pop());
    check.addEventListener('change',()=>{row.classList.toggle('excluded',!check.checked);if(check.checked)animateScene(row.querySelector('.folder-glyph'),[{transform:'scale(.9)'},{transform:'scale(1)'}],{duration:170,name:'folder-select'});for(const f of files)check.checked?state.selected.add(f.id):state.selected.delete(f.id);renderFiles();});
    const title = node('div', 'folder-plan-name');
    title.append(icon('folder', 'folder-glyph type-folder'), node('strong', '', folder.name.split('/').pop()));
    const metrics = node('div', 'folder-plan-values');
    metrics.append(node('span', '', `${folder.files} archivos · ${mb(folder.bytes)}`), node('span', 'folder-plan-zips', `≈ ${folder.estimated_zips} ZIP`));
    row.append(check,title,metrics);folders.append(row);
  }
  const signature=plan.assessments.map(a=>a.id).join(',');
  if(signature!==folderPlanSignature){
    folderPlanSignature=signature;
    revealScene(folders.children,'folder-arrive',340,45);
    if(state.sourceRevealPending){
      state.sourceRevealPending=false;
      revealScene([document.querySelector('#review-step .strategy-heading'),document.querySelector('#review-step .run-card')],'review-arrive',300,70);
      if(document.activeElement===document.body||document.activeElement===document.getElementById('progress-panel'))folders.querySelector('input')?.focus({preventScroll:true});
    }
  }
  if(focusedFolder)[...folders.children].find(row=>row.dataset.folder===focusedFolder)?.querySelector('input')?.focus({preventScroll:true});
  const manual = plan.assessments.filter(a => a.manual_risk);
  document.getElementById('manual-plan').hidden = true;
  document.getElementById('manual-plan-title').textContent = `Procesamiento manual probable · ${manual.length} archivo(s)`;
  const recommendations = document.getElementById('manual-plan-items'); recommendations.replaceChildren();
  for (const assessment of manual) {
    const source = state.files.find(f => f.id === assessment.id);
    const row = node('div', 'manual-recommendation');
    row.append(node('h3', '', source?.relative || 'Archivo'), node('p', '', assessment.recommendation));
    if (assessment.sample_ratio !== null) row.append(node('small', '', `Muestra comprimida: ${Math.round(assessment.sample_ratio * 100)}% del peso original. Orientativo; el ZIP completo se comprobará.`));
    recommendations.append(row);
  }
}

function processExperience(active) {
  clearInterval(elapsedTimer);
  if (active) {
    document.getElementById('progress-panel').dataset.phase='analyze';
    processStarted = Date.now();
    document.getElementById('process-folders-done').textContent='0 / 0';
    document.getElementById('process-output-mb').textContent=mb(0);
    const naming={numbers:'1-X',part:'PARTE 1-X',part_of:'PARTE 1 DE X'}[settings.naming];
    document.getElementById('processing-settings').textContent=document.getElementById('progress-panel').dataset.mode==='source'?'Lectura local · Originales intactos':`Máximo ${mb(limitBytes())} · Nombres ${naming} · Subcarpetas conservadas`;
    document.getElementById('process-elapsed').textContent = '00:00';
    document.getElementById('process-log').replaceChildren();
    document.getElementById('processing-folders').replaceChildren();
    document.getElementById('processing-folders').dataset.snapshot='';
    document.getElementById('progress-percent').textContent = '…';
    ['processed-pdfs','processed-parts','processed-zips','processed-pending'].forEach(id => document.getElementById(id).textContent = '0');
    elapsedTimer = setInterval(() => {
      const elapsed = Math.floor((Date.now() - processStarted) / 1000);
      document.getElementById('process-elapsed').textContent = `${String(Math.floor(elapsed / 60)).padStart(2,'0')}:${String(elapsed % 60).padStart(2,'0')}`;
    }, 1000);
  }
  matrixScenes.forEach(refresh => refresh());
}

function updateProcessExperience(job) {
  renderProcessingFolders(job);
  const panel = document.getElementById('progress-panel');
  panel.dataset.phase = job.phase || 'analyze';
  document.querySelectorAll('[data-phase]').forEach(element => element.classList.toggle('active', element.dataset.phase === (job.phase || 'analyze')));
  document.getElementById('progress-percent').textContent = job.total ? Math.min(100, Math.floor(job.current / job.total * 100)) + '%' : '…';
  const folders=job.folders||[];
  document.getElementById('process-folders-done').textContent=`${folders.filter(f=>['completed','review'].includes(f.status)).length} / ${folders.length}`;
  document.getElementById('process-output-mb').textContent=mb(folders.reduce((n,f)=>n+(f.output_bytes??(f.outputs||[]).reduce((sum,o)=>sum+o.bytes,0)),0));
  const statistics = job.statistics || {};
  for (const [id, key] of [['processed-pdfs','pdfs_optimized'],['processed-parts','pdf_parts'],['processed-zips','zips_verified'],['processed-pending','pending']]) document.getElementById(id).textContent = statistics[key] || 0;
  const log = document.getElementById('process-log');
  log.replaceChildren();
  for (const event of (job.events || []).slice(-1)) {
    const row = node('div'); row.append(node('time', '', event.time), node('span', '', event.text)); log.append(row);
  }
}

function renderPreparation(result, container) {
  const records = result.pdf_decisions || result.preparation || [];
  if (!records.length) return;
  const details = node('details', 'preparation-details');
  details.append(node('summary', '', `Decisiones PDF · ${records.length} documento(s)`));
  const outcomes = {accepted_unchanged:'Aceptado sin cambios',optimized:'Optimizado satisfactoriamente',split:'Dividido satisfactoriamente',review:'Requiere revisión'};
  const stages = {lossless:'Compresión sin pérdida',images_lossless:'Imágenes sin pérdida',images:'Optimización de imágenes',resolution:'Resolución adaptativa',grayscale:'Escala de grises selectiva',bilevel:'Blanco y negro adaptativo',aggressive:'Reducción adicional'};
  for (const record of records) {
    const row = node('div', 'preparation-row');
    row.append(node('h3', '', record.source), node('p', '', outcomes[record.decision] || (record.split ? 'Dividido por páginas' : 'Optimizado')));
    const diagnosis = node('details', 'report-details');
    diagnosis.append(node('summary', '', 'Diagnóstico y tamaños'), node('p', 'micro', `${Number(record.original_bytes).toLocaleString('es-MX')} bytes de origen · ${Number(record.processed_bytes ?? record.optimized_bytes ?? record.original_bytes).toLocaleString('es-MX')} bytes procesados · límite ${Number(record.limit_bytes || result.limit_bytes).toLocaleString('es-MX')} bytes (MB decimales)`));
    if(record.analysis?.type==='book')diagnosis.append(node('p','micro',`${record.analysis.pages_count} páginas · División directa del libro. Se contrastan contenido, fuentes e imágenes con el original; no se recodifican las páginas. El tamaño procesado es la suma de las partes, cada una bajo el límite.`));
    else if(record.analysis)diagnosis.append(node('p','micro',`Tipo: ${{scanned:'escaneado',digital:'digital',mixed:'mixto'}[record.analysis.type]||'sin clasificar'} · ${record.analysis.pages?.length||0} páginas · ${record.analysis.image_count||0} imágenes · texto seleccionable: ${record.analysis.has_text?'sí':'no'}`));
    for(const attempt of record.attempts||[])diagnosis.append(node('p','micro',`${stages[attempt.stage]||attempt.stage}: ${{accepted:'conservada',discarded:'descartada',skipped:'omitida'}[attempt.status]||attempt.status}${attempt.bytes!=null?' · '+Number(attempt.bytes).toLocaleString('es-MX')+' bytes':''}${attempt.reason?' · '+attempt.reason:''}`));
    if(record.quality_review)diagnosis.append(node('p','micro','Se modificaron imágenes de una copia. La comparación automática no sustituye revisar folios, firmas visibles, sellos y códigos.'));
    for(const page of record.page_attempts||[])diagnosis.append(node('p','micro',`Página ${page.page}: ${page.original_bytes.toLocaleString('es-MX')} → ${page.processed_bytes.toLocaleString('es-MX')} bytes · ${page.attempts.length} etapas evaluadas`));
    row.append(diagnosis);
    if (record.reason) row.append(node('p', 'warnings', record.reason));
    for (const part of record.parts || []) {
      const line = node('div', 'prepared-part');
      line.append(node('span', '', `${part.name} · ${size(part.bytes)}`));
      if (part.id) line.append(button('Ver PDF', () => preview(part), 'quiet small'));
      row.append(line);
    }
    details.append(row);
  }
  container.append(details);
}

document.addEventListener('DOMContentLoaded', () => {
  createRain(document.getElementById('intro-rain'));
  createRain(document.getElementById('processing-rain'));
  createMatrixCursor();
  document.getElementById('enter-app').addEventListener('click', closeMatrixIntro);
  document.getElementById('skip-intro').addEventListener('click', closeMatrixIntro);
  document.getElementById('replay-intro').addEventListener('click', openMatrixIntro);
  document.addEventListener('keydown', event => {
    if (!document.getElementById('matrix-intro').hidden && !document.getElementById('matrix-intro').inert && ['Enter','Escape'].includes(event.key)) { event.preventDefault(); closeMatrixIntro(); }
  });
  document.addEventListener('visibilitychange', updateMotion);
  reducedMotion.addEventListener('change', updateMotion);
  let seen = false;
  try { seen = sessionStorage.getItem('magiczip-intro-seen') === 'yes'; } catch {}
  if (seen) { document.getElementById('matrix-intro').hidden = true; document.querySelector('.shell').inert = false; }
  else openMatrixIntro();
  document.addEventListener('toggle',event=>{
    if(event.target.matches?.('.folder-findings')&&event.target.open)animateScene(event.target.querySelector('.findings-content'),[{opacity:0,transform:'translateY(-5px)'},{opacity:1,transform:'none'}],{duration:180,name:'findings-open'});
  },true);
  updateMotion();
});

function renderProcessingFolders(job){
 const root=document.getElementById('processing-folders'),folders=job.folders||[];
 const snapshot=JSON.stringify(folders);
 if(root.dataset.snapshot===snapshot)return;root.dataset.snapshot=snapshot;
 const previousActive=root.dataset.active;root.dataset.active=job.active_folder||'';
 const existing=new Map([...root.children].map(row=>[row.dataset.folder,row]));
 for(const [index,folder] of folders.entries()){
  let row=existing.get(folder.name);if(!row){row=node('section','processing-folder');row.dataset.folder=folder.name;root.append(row);}existing.delete(folder.name);
  const signature=JSON.stringify(folder);if(row.dataset.signature===signature)continue;row.dataset.signature=signature;
  const previousStatus=row.dataset.status;row.dataset.status=folder.status;
  row.className='processing-folder '+folder.status;row.replaceChildren();
  const heading=node('div','folder-process-heading'),copy=node('div','folder-process-copy');
  const title=node('div','folder-process-title');title.append(node('strong','',folder.label),node('span','folder-process-index',`${index+1} / ${folders.length}`));
  copy.append(title);heading.append(icon('folder','folder-process-icon type-folder'),copy);
  const terminal=['completed','review','cancelled','error'].includes(folder.status);
  if(terminal){const weight=node('div','folder-process-weight');weight.append(node('strong','',mb(folder.output_bytes??(folder.outputs||[]).reduce((n,o)=>n+o.bytes,0))),node('small','',countText(folder.outputs.length,'ZIP generado','ZIP generados')));heading.append(weight);}
  row.append(heading);
  const track=node('div','folder-process-track'),beam=node('i');track.append(beam);
  track.setAttribute('aria-label','Avance de la etapa de '+folder.label);
  if(['completed','review'].includes(folder.status))beam.style.width='100%';
  else if(folder.status==='running'){
   if(folder.total>0&&folder.current>0)beam.style.width=Math.min(100,folder.current/folder.total*100)+'%';
   else track.classList.add('indeterminate');
  }
  row.append(track);
  const text=folder.status==='running'?folder.stage:folder.status==='waiting'?'En espera':folder.status==='cancelled'?'Proceso cancelado':folder.status==='error'?'No completada':`${folder.pending?'⚠ '+countText(folder.pending,'pendiente','pendientes'):'✓ Carpeta procesada'}`;
  row.append(node('p','micro folder-stage'+(folder.pending?' warnings':''),text));
  for(const output of folder.outputs||[]){const line=node('div','folder-zip-name');line.append(icon('zip','type-zip'),node('span','',output.name),node('strong','',mb(output.bytes)));row.append(line);}
  if(previousStatus!==folder.status&&['completed','review'].includes(folder.status))animateScene(row,[{opacity:.55,transform:'translateX(-7px)'},{opacity:1,transform:'none'}],{duration:260,name:'folder-complete'});
 }
 for(const row of existing.values())row.remove();
 if(previousActive!==job.active_folder&&job.active_folder){const active=[...root.children].find(row=>row.dataset.folder===job.active_folder);active?.scrollIntoView({block:'nearest',behavior:'instant'});}
}

function createMatrixCursor(){
 const layer=document.getElementById('cursor-effects'),canvas=document.getElementById('cursor-matrix'),pointer=document.getElementById('matrix-pointer');
 const context=canvas.getContext('2d');if(!context)return;
 const finePointer=window.matchMedia('(hover: hover) and (pointer: fine)');
 let particles=[],frame=null,lastTime=0,lastMove=0,lastPoint=null,width=0,height=0,scale=1;
 const glyphs='0123456789010101';
 function fit(){
  width=innerWidth;height=innerHeight;scale=Math.min(1,1600/width);
  canvas.width=Math.ceil(width*scale);canvas.height=Math.ceil(height*scale);context.setTransform(scale,0,0,scale,0,0);
 }
 function show(){if(layer.showPopover&&!layer.matches(':popover-open'))layer.showPopover();}
 function stop(){
  if(frame!==null)cancelAnimationFrame(frame);frame=null;particles=[];lastPoint=null;lastTime=0;
  canvas.width=canvas.height=1;if(layer.hidePopover&&layer.matches(':popover-open'))layer.hidePopover();
 }
 function draw(time){
  if(!motionActive()||!finePointer.matches){stop();return;}
  const dt=lastTime?Math.min(.05,(time-lastTime)/1000):.016;lastTime=time;
  context.clearRect(0,0,width,height);context.font='11px Consolas, monospace';
  particles=particles.filter(p=>(p.life-=dt)>0);
  for(const p of particles){
   p.y+=p.speed*dt;p.x+=p.drift*dt;context.globalAlpha=Math.min(1,p.life/p.duration)*.65;
   context.fillStyle=p.accent?'#baff9a':'#64cfa4';context.fillText(p.glyph,p.x,p.y);
  }
  context.globalAlpha=1;
  if(!particles.length){stop();return;}
  frame=requestAnimationFrame(draw);
 }
 document.addEventListener('pointermove',event=>{
  if(event.pointerType==='touch'||!finePointer.matches||!motionActive())return;
  const now=performance.now();if(now-lastMove<18)return;lastMove=now;
  if(!particles.length){fit();show();}
  pointer.style.transform=`translate3d(${event.clientX}px,${event.clientY}px,0)`;
  const from=lastPoint||{x:event.clientX,y:event.clientY};const distance=Math.hypot(event.clientX-from.x,event.clientY-from.y);const count=Math.min(5,Math.max(2,Math.ceil(distance/14)));
  for(let i=0;i<count;i++){
   const fraction=(i+1)/count,duration=.4+Math.random()*.35;
   particles.push({x:from.x+(event.clientX-from.x)*fraction+(Math.random()-.5)*15,y:from.y+(event.clientY-from.y)*fraction+6,glyph:glyphs[Math.floor(Math.random()*glyphs.length)],speed:28+Math.random()*55,drift:(Math.random()-.5)*9,life:duration,duration,accent:Math.random()>.8});
  }
  particles=particles.slice(-84);lastPoint={x:event.clientX,y:event.clientY};
  if(frame===null)frame=requestAnimationFrame(draw);
 },{passive:true});
 // Raise the decorative layer above newly opened dialogs without intercepting input.
 document.addEventListener('toggle',event=>{if(event.target instanceof HTMLDialogElement&&event.target.open&&particles.length){layer.hidePopover?.();show();}},true);
 document.documentElement.addEventListener('pointerleave',()=>{lastPoint=null;});
 document.addEventListener('visibilitychange',()=>{if(document.hidden)stop();});
 reducedMotion.addEventListener('change',()=>{if(reducedMotion.matches)stop();});
 finePointer.addEventListener('change',()=>{if(!finePointer.matches)stop();});
 window.addEventListener('resize',()=>{if(particles.length)fit();},{passive:true});
 window.addEventListener('pagehide',stop);
}
