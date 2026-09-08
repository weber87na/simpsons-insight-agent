/* All model/source content is rendered as text, never executable HTML. */
(() => {
  const $ = id => document.getElementById(id);
  const reportId = document.body.dataset.reportId;
  let planId, stream;
  const el = (tag, text, cls) => { const n = document.createElement(tag); if (text !== undefined) n.textContent = text; if (cls) n.className = cls; return n; };
  const message = error => { $('insight-error').classList.add('error'); $('insight-error').textContent = error.message || String(error); };
  async function api(url, method = 'GET', body) {
    const res = await fetch(url, {method, headers: {'Content-Type':'application/json'}, ...(body === undefined ? {} : {body:JSON.stringify(body)})});
    const data = await res.json(); if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail)); return data;
  }
  const list = values => { const ul = el('ul'); for (const v of values || []) ul.append(el('li',v)); return ul; };
  const ratio = value => value === null ? '無資料' : `${(value*100).toFixed(1)}%`;
  function chart(points, field, title, percent=false) {
    const figure = el('figure',undefined,'insight-chart'); figure.append(el('figcaption',title));
    const ns = 'http://www.w3.org/2000/svg'; const svg = document.createElementNS(ns,'svg'); svg.setAttribute('viewBox','0 0 560 230'); svg.setAttribute('role','img'); svg.setAttribute('aria-label',title); svg.classList.add('trend-svg');
    const draw = (tag,attrs,text) => {const n=document.createElementNS(ns,tag);for(const [k,v] of Object.entries(attrs)) n.setAttribute(k,v);if(text!==undefined)n.textContent=text;svg.append(n);return n;};
    const max = percent ? 1 : Math.max(1,...points.map(p=>p[field]||0));
    for(let i=0;i<5;i++){const y=20+i*40;draw('line',{x1:50,x2:540,y1:y,y2:y,stroke:'#d8d1c0'});draw('text',{x:2,y:y+4},percent?`${100-i*25}%`:String(Math.round(max*(1-i/4))));}
    let last=null;
    points.forEach((p,i)=>{ const x=50+i*490/Math.max(1,points.length-1), value=p[field]; if(value===null){last=null;return;}const y=180-value/max*160;
      if(last)draw('line',{x1:last.x,y1:last.y,x2:x,y2:y,stroke:percent?'#a23435':'#267b77','stroke-width':2});
      const c=draw('circle',{cx:x,cy:y,r:3,fill:percent?'#a23435':'#267b77'});const tip=document.createElementNS(ns,'title');tip.textContent=`${p.period}: ${percent?ratio(value):value}`;c.append(tip);last={x,y};
    });
    if(points.length){draw('text',{x:50,y:215},points[0].period);draw('text',{x:540,y:215,'text-anchor':'end'},points.at(-1).period);}else draw('text',{x:200,y:100},'沒有可分析的日期資料');
    figure.append(svg);return figure;
  }
  async function loadTrend(){return window.operationalAnalytics.load();}
  async function evidence(ids){const dialog=el('dialog',undefined,'evidence-dialog');const close=el('button','關閉','secondary');close.onclick=()=>dialog.close();dialog.append(close);document.body.append(dialog);dialog.addEventListener('close',()=>dialog.remove());dialog.showModal();
    for(const id of ids){try{const data=await api(`/api/reports/${reportId}/evidence/${encodeURIComponent(id)}`);const items=[data];for(const r of items){dialog.append(el('h3',r.source),el('p',r.text));}}catch(e){dialog.append(el('p',e.message));}}
  }
  const evidenceButton = ids => {const b=el('button','查看引用內容','secondary');b.type='button';b.onclick=()=>evidence(ids).catch(message);return b;};
  window.openReportEvidence = evidence;
  async function loadTopics(){const data=await api(`/api/reports/${reportId}/topics`);$('topic-cards').replaceChildren();for(const t of data.topics){const o=el('option',`${t.name} (${t.count})`);o.value=t.topic_key;$('trend-topic').append(o);if(new URLSearchParams(location.search).get('topic_key')===t.topic_key)o.selected=true;const card=el('article',undefined,'insight-card');card.append(el('h3',t.name),el('p',`${t.count} 筆 · ${t.mapping==='matched'?'對應既有主題':'新主題'}${t.provisional?' · 單筆暫定':''}`),list(t.keywords),evidenceButton(t.item_ids.slice(0,10)));$('topic-cards').append(card);}if(!data.topics.length)$('topic-cards').append(el('p',data.analysis.status==='legacy'?'舊報告未保存主題快照。':'目前沒有可呈現的抱怨分群。'));if(data.analysis.missing_embeddings)$('topic-cards').append(el('p',`${data.analysis.missing_embeddings} 筆缺少向量，未納入語意分群。`));}
  function field(form,label,key,value,type='text'){const l=el('label',label);const input=el(type==='textarea'?'textarea':'input');input.name=key;if(type!=='textarea')input.type=type;input.value=value||'';l.append(input);form.append(l);return input;}
  function renderPlan(data){
    document.dispatchEvent(new CustomEvent('decision-loaded', {detail:data}));
    planId=data.id;const running=['PENDING','RUNNING'].includes(data.status);$('decision-status').textContent=`${data.status}${data.error?'：'+data.error:''} ${data.runs?.map(r=>`${r.stage}: ${r.status}`).join(' · ')||''}`;
    $('decision-form').hidden=!!planId;$('cancel-decision').hidden=!running;$('retry-decision').hidden=!['PARTIAL','CANCELED'].includes(data.status);$('decision-audit').hidden=!planId;$('decision-audit').href=`/api/decisions/${planId}/audit`;
    if(running)return;
    $('decision-problems').replaceChildren();for(const p of data.diagnosis?.problems||[]){const c=el('article',undefined,'insight-card');c.append(el('h3',p.problem),el('p',`根因假設：${p.root_cause_hypothesis}`),list(p.unknowns),evidenceButton(p.evidence_ids));$('decision-problems').append(c);}
    $('decision-tasks').replaceChildren();for(const t of data.tasks||[]){const c=el('article',undefined,'insight-card');c.id='task-'+t.id;c.append(el('h3',`${t.priority}. ${t.title}`),list(t.steps),el('p',`預估 ${t.hours} 小時 · ${t.cost_estimate}`),el('p',`指標：${t.metric}／目標：${t.target}／驗證：${t.verification}`),list([...t.prerequisites,...t.assumptions,...t.risks]),el('p',`第 ${t.start_week}–${t.due_week} 週。${t.schedule_assumption||''}`),evidenceButton(t.evidence_ids));
      const f=el('form');field(f,'負責角色','owner_role',t.owner_role);field(f,'開始日','start_date',t.start_date,'date');field(f,'到期日','due_date',t.due_date,'date');const l=el('label','工作狀態');const select=el('select');select.name='status';for(const [v,label] of [['TODO','待處理'],['IN_PROGRESS','執行中'],['DONE','已完成'],['BLOCKED','受阻']]){const o=el('option',label);o.value=v;select.append(o);}select.value=t.status;l.append(select);f.append(l);field(f,'實際成果','actual_results',t.actual_results,'textarea');field(f,'備註','notes',t.notes,'textarea');f.append(el('button','保存工作','secondary'));f.onsubmit=async e=>{e.preventDefault();try{const body=Object.fromEntries(new FormData(f));body.start_date||=null;body.due_date||=null;await api(`/api/improvement-tasks/${t.id}`,'PATCH',body);$('insight-error').classList.remove('error');$('insight-error').textContent='已保存工作';}catch(error){message(error);}};c.append(f);$('decision-tasks').append(c);
    }
    $('decision-evaluations').replaceChildren();for(const e of data.evaluations||[]){const box=el('details');box.append(el('summary',e.kind==='human'?`真人評估：${e.payload.reviewer}`:`AI／規則評估：第 ${e.payload.revision+1} 輪 · ${e.payload.passed?'通過':'待確認'}`));const scores=e.kind==='human'?e.payload:e.payload.expert;box.append(el('p',`證據 ${scores.evidence}／實用性 ${scores.feasibility}／資源 ${scores.resources}／衡量 ${scores.measurability}／風險 ${scores.risk}`),list(e.payload.reasons),list(scores.revisions));if(e.payload.corrections)box.append(el('p',e.payload.corrections));$('decision-evaluations').append(box);}
    if(location.hash.startsWith('#task-'))document.getElementById(decodeURIComponent(location.hash.slice(1)))?.scrollIntoView();
    $('expert-form').hidden=!planId;$('outcome-form').hidden=!(data.tasks||[]).length;
  }
  function watch(id){stream?.close();stream=new EventSource(`/api/decisions/${id}/events`);stream.onmessage=e=>{const data=JSON.parse(e.data);renderPlan(data);if(!['RUNNING','PENDING'].includes(data.status))stream.close();};stream.onerror=()=>{$('decision-status').textContent='進度連線中斷，正在重新連線…';};}
  async function loadPlan(){const data=await api(`/api/reports/${reportId}/decisions`);renderPlan(data);if(data.id&&['RUNNING','PENDING'].includes(data.status))watch(data.id);}
  $('trend-form').onsubmit=e=>{e.preventDefault();loadTrend().catch(message);};
  $('decision-form').onsubmit=async e=>{e.preventDefault();$('start-decision').disabled=true;try{const data=await api(`/api/reports/${reportId}/decisions`,'POST',{start_date:$('plan-start').value||null,weekly_hours:$('plan-hours').value?Number($('plan-hours').value):null,constraints:$('plan-constraints').value});watch(data.plan_id);}catch(error){message(error);}finally{$('start-decision').disabled=false;}};
  $('cancel-decision').onclick=()=>api(`/api/decisions/${planId}/cancel`,'POST').catch(message);
  $('retry-decision').onclick=async()=>{try{await api(`/api/decisions/${planId}/retry`,'POST');watch(planId);}catch(error){message(error);}};
  $('expert-form').onsubmit=async e=>{e.preventDefault();try{const body=Object.fromEntries([...new FormData(e.target)].map(([k,v])=>[k,Number(v)]));Object.assign(body,{reviewer:$('expert-name').value,reasons:[$('expert-reasons').value],revisions:[],corrections:$('expert-corrections').value});await api(`/api/decisions/${planId}/evaluations`,'POST',body);await loadPlan();}catch(error){message(error);}};
  $('outcome-form').onsubmit=async e=>{e.preventDefault();try{const q=new URLSearchParams({after_report_id:$('outcome-report').value,intervention_date:$('outcome-date').value});const data=await api(`/api/decisions/${planId}/outcomes?${q}`);const target=$('outcome-result');target.replaceChildren(el('p',data.note));for(const [label,window] of [['改善前',data.before],['改善後',data.after]]){const n=window.points.reduce((s,p)=>s+p.classified_count,0),neg=window.points.reduce((s,p)=>s+p.negative_count,0);target.append(el('p',`${label}：${window.included_count} 筆，負評比例 ${ratio(n?neg/n:null)}，日期不足排除 ${window.excluded_count} 筆`));}}catch(error){message(error);}};
  async function init(){window.reportTopicsReady=loadTopics();await window.reportTopicsReady;await Promise.all([loadTrend(),loadPlan()]);const reports=await api('/api/insights/reports');const current=reports.find(r=>r.id===reportId);for(const r of reports.filter(r=>r.business_id===current?.business_id)){const o=el('option',`${r.name} · ${r.created_at}`);o.value=r.id;$('outcome-report').append(o);}}
  init().catch(message);
})();
