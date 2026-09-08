/* No model text is inserted as HTML. Measurement specs lock when execution begins. */
(() => {
  const $ = id => document.getElementById(id);
  if (!$('validation-panel')) return;
  let planId, runId, timer, fetching = false, alive = true, loadedPlan;
  const active = status => ['PENDING', 'RUNNING'].includes(status);
  const el = (tag, text, cls) => {const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;};
  const labels = {PENDING:'排隊中', RUNNING:'執行中', COMPLETED:'已完成', PARTIAL:'部分完成', CANCELED:'已取消', NEEDS_REVIEW:'需要確認', DRAFT:'草稿', STOPPED:'已停止'};
  const stageLabels = {design_0:'驗證設計', review_0:'獨立審查', design_1:'修訂設計', review_1:'修訂審查'};
  const error = e => {$('validation-error').textContent=e.message||String(e);};
  async function api(url, method='GET', body) {
    const response=await fetch(url,{method,headers:{'Content-Type':'application/json'},...(body===undefined?{}:{body:JSON.stringify(body)})});
    const data=await response.json();if(!response.ok)throw new Error(typeof data.detail==='string'?data.detail:JSON.stringify(data.detail));return data;
  }
  function list(values){const n=el('ul');for(const v of values||[])n.append(el('li',v));return n;}
  function field(form,label,name,type='text',value='',required=false){
    const wrap=el('label',label),input=el(type==='textarea'?'textarea':'input');input.name=name;if(type!=='textarea')input.type=type;input.value=value??'';input.required=required;
    if(type==='number')input.step='any';wrap.append(input);form.append(wrap);return input;
  }
  function select(form,label,name,choices,value){const wrap=el('label',label),input=el('select');input.name=name;for(const [key,title] of choices){const o=el('option',title);o.value=key;input.append(o);}input.value=value;wrap.append(input);form.append(wrap);return input;}
  function button(text,action){const b=el('button',text,'secondary');b.type='button';b.onclick=async()=>{b.disabled=true;try{await action();}catch(e){error(e);}finally{b.disabled=false;}};return b;}
  function references(title,ids){return button(`${title}（${ids.length}）`,()=>window.openReportEvidence(ids));}
  async function update(id,body){await api(`/api/validation-experiments/${id}`,'PATCH',body);await load();}
  function measurementForm(e,card){
    const form=el('form',undefined,'filter-grid'),m=e.measurement||{};
    form.setAttribute('aria-label','確認量測規格');
    field(form,'指標名稱','metric','text',m.metric||e.spec.metric,true);
    const kind=select(form,'指標類型','metric_kind',[['ratio','比例'],['mean','平均值']],m.metric_kind||e.spec.metric_kind);
    field(form,'單位（比例結果以百分點呈現）','unit','text',m.unit||e.spec.unit,true);
    select(form,'改善方向','direction',[['increase','增加'],['decrease','降低']],m.direction||'increase');
    field(form,'絕對改善門檻（比例填百分點）','threshold','number',m.threshold,true).min='0.000001';
    const min=field(form,'前後各期最低樣本數','minimum_sample','number',m.minimum_sample,true);min.min='1';min.step='1';
    for(const [name,label] of [['before_start','改善前開始日'],['before_end','改善前結束日'],['after_start','改善後開始日'],['after_end','改善後結束日']])field(form,label,name,'date',m[name],true);
    const hint=el('p','門檻及樣本數由人確認，不是統計顯著性標準。開始後鎖定，前後期間不得重疊。');form.append(hint);
    kind.onchange=()=>{hint.textContent=kind.value==='ratio'?'例如 60% → 70% 的差值為 10 百分點。':'平均值使用填入單位的絕對差值。';};
    const save=el('button','保存量測草稿','secondary');save.type='submit';form.append(save);
    const confirm=field(form,'我已確認指標、門檻、最低樣本數及觀察期間','confirmed','checkbox');
    const begin=el('button','確認並開始實驗','primary');begin.type='submit';begin.name='begin';begin.disabled=!e.approved;form.append(begin);
    form.onsubmit=async event=>{event.preventDefault();const starting=event.submitter===begin;
      if(starting&&!confirm.checked){error(new Error('請先勾選確認量測規格'));return;}
      save.disabled=true;begin.disabled=true;
      try{const data=Object.fromEntries(new FormData(form));delete data.confirmed;delete data.begin;data.threshold=Number(data.threshold);data.minimum_sample=Number(data.minimum_sample);
        await update(e.id,{measurement:data,...(starting?{status:'RUNNING',confirmed:true}:{})});
      }catch(ex){error(ex);save.disabled=false;begin.disabled=!e.approved;}
    };
    card.append(form);
  }
  function resultForm(e,card){
    const form=el('form',undefined,'filter-grid');form.setAttribute('aria-label','回填實驗結果');
    const ratio=e.measurement.metric_kind==='ratio';
    for(const [key,label] of [['before','改善前'],['after','改善後']]){
      const n=field(form,`${label}樣本數`,`${key}_count`,'number');n.min='1';n.step='1';
      const value=field(form,`${label}${ratio?'成功數':'平均值'}`,`${key}_value`,'number');if(ratio){value.min='0';value.step='1';}
    }
    const comparable=select(form,'前後資料可比較嗎？','comparable',[['','請選擇'],['true','可比較'],['false','不可比較']],'');comparable.required=true;
    field(form,'干擾因素（每行一項，無則留空）','confounders','textarea');field(form,'回填備註','notes','textarea');
    form.append(el('p','缺少資料可留空，系統會判為資料不足。更正請新增回填，歷史保留。'));
    const submit=el('button','保存結果並判讀','secondary');form.append(submit);
    let pending;
    form.onsubmit=async event=>{event.preventDefault();submit.disabled=true;
      try{const raw=Object.fromEntries(new FormData(form)),body={comparable:raw.comparable==='true',confounders:raw.confounders.split('\n').map(s=>s.trim()).filter(Boolean),notes:raw.notes};
        for(const key of ['before','after']){body[key]={};if(raw[`${key}_count`]!=='')body[key].count=Number(raw[`${key}_count`]);if(raw[`${key}_value`]!=='')body[key][ratio?'successes':'mean']=Number(raw[`${key}_value`]);}
        const fingerprint=JSON.stringify(body);if(!pending||pending.fingerprint!==fingerprint)pending={fingerprint,id:crypto.randomUUID()};
        await api(`/api/validation-experiments/${e.id}/results`,'POST',{...body,submission_id:pending.id});await load();
      }catch(ex){error(ex);}finally{submit.disabled=false;}
    };
    card.append(form);
  }
  function render(data){
    runId=data.id;$('validation-form').hidden=!!runId;$('validation-refresh').hidden=!runId;
    if(!runId)return;
    const running=active(data.status);$('validation-cancel').hidden=!running;$('validation-retry').hidden=!['PARTIAL','CANCELED'].includes(data.status);
    $('validation-status').textContent=`${labels[data.status]||data.status} · ${data.options.context==='campus'?'校園服務':'通用企業'}${data.error?' · '+data.error:''} ${Object.entries(data.stages||{}).map(([key,s])=>`${stageLabels[key]||key}：${labels[s.status]||s.status}`).join(' / ')}`;
    const c=data.coverage;$('validation-coverage').textContent=c?`完整報告 ${c.snapshot_count} 筆，文字 ${c.text_count} 筆，選取 ${c.included} 筆，未選取文字 ${c.excluded} 筆。${c.note}`:'';
    $('validation-notes').replaceChildren(list([...(data.missing_information||[]),...(data.review?.reasons||[]),...(data.rule_errors||[])]));
    if(running)return;
    $('validation-cards').replaceChildren();
    for(const e of data.experiments||[]){
      const card=el('article',undefined,'insight-card');card.dataset.experimentId=e.id;
      const approval=e.approved?(e.confirmed_at?'審查通過，量測規格已確認':'審查通過，仍需人工確認量測'):'審查未通過，不可執行';
      card.append(el('h3',e.spec.title),el('p',`${e.spec.journey_stage} · ${labels[e.status]} · ${approval}`),el('p',`待驗證假設：${e.spec.hypothesis}`),el('h4','替代解釋'),list(e.spec.alternative_explanations),references('支持證據',e.spec.support_evidence_ids),references('相反證據',e.spec.counter_evidence_ids),el('p',e.spec.counter_note),el('h4','小型實驗'),list(e.spec.steps),el('p',`負責角色：${e.spec.owner_role} · ${e.spec.hours} 小時 · ${e.spec.cost_estimate}`),el('p',`建議觀察 ${e.spec.observation_days} 日；工作第 ${e.spec.start_week}–${e.spec.due_week} 週${e.spec.start_date?' · '+e.spec.start_date+' 至 '+e.spec.due_date:''}。${e.spec.schedule_assumption||''}`),el('h4','停止條件'),list(e.spec.stop_conditions));
      if(e.status==='DRAFT')measurementForm(e,card);
      else if(e.measurement){const m=e.measurement;card.append(el('p',`已鎖定：${m.metric}，${m.direction==='increase'?'增加':'降低'}至少 ${m.threshold} ${m.metric_kind==='ratio'?'百分點':m.unit}；每期至少 ${m.minimum_sample} 筆。${m.before_start}～${m.before_end} 對照 ${m.after_start}～${m.after_end}`));if(e.confirmed_at)resultForm(e,card);}
      if(['DRAFT','RUNNING'].includes(e.status))card.append(button('停止實驗',()=>update(e.id,{status:'STOPPED'})));
      if(e.status==='RUNNING')card.append(button('標記實驗完成',()=>update(e.id,{status:'COMPLETED'})));
      card.append(el('h4','結果與歷史'));
      if(!e.results.length)card.append(el('p','尚未回填結果'));
      for(const r of [...e.results].reverse()){const d=el('details');d.className='validation-result';const describe=o=>`${o.count??'未提供'} 筆，${e.measurement.metric_kind==='ratio'?'成功數 '+(o.successes??'未提供'):'平均值 '+(o.mean??'未提供')}`;d.append(el('summary',`${r.verdict.label} · ${r.created_at}`),el('p',`差值：${r.verdict.delta??'未提供'} ${r.verdict.unit} · 判讀日 ${r.verdict.evaluated_on}`),el('p',`前期：${describe(r.input.before)}；後期：${describe(r.input.after)}；${r.input.comparable?'前後可比較':'前後不可比較'}`),list(r.input.confounders),el('p',r.input.notes),el('p',r.verdict.note));card.append(d);}
      $('validation-cards').append(card);
    }
    if(!data.experiments.length)$('validation-cards').append(el('p','尚無可執行實驗；請查看待補資訊或錯誤。'));
  }
  async function load(){
    if(!planId||fetching||!alive)return;fetching=true;clearTimeout(timer);
    try{const data=await api(`/api/decisions/${planId}/validation`);if(!alive)return;render(data);$('validation-error').textContent='';if(active(data.status))timer=setTimeout(()=>load().catch(error),3000);}
    finally{fetching=false;}
  }
  document.addEventListener('decision-loaded',event=>{
    const data=event.detail;planId=data.id;
    const eligible=planId&&!active(data.status)&&data.diagnosis?.problems?.length;
    $('validation-eligibility').textContent=eligible?'以完整報告證據設計實驗，不受上方圖表篩選影響。':'請先完成具有有效問題與證據的改善計畫。';
    if(eligible&&loadedPlan!==planId){loadedPlan=planId;load().catch(error);}
  });
  $('validation-form').onsubmit=async event=>{event.preventDefault();const b=event.target.querySelector('button');b.disabled=true;
    try{const raw=Object.fromEntries(new FormData(event.target));raw.start_date||=null;raw.weekly_hours=raw.weekly_hours?Number(raw.weekly_hours):null;await api(`/api/decisions/${planId}/validation`,'POST',raw);await load();}catch(e){error(e);}finally{b.disabled=false;}
  };
  $('validation-cancel').onclick=()=>api(`/api/validations/${runId}/cancel`,'POST').then(load).catch(error);
  $('validation-retry').onclick=()=>api(`/api/validations/${runId}/retry`,'POST').then(load).catch(error);
  $('validation-refresh').onclick=()=>load().catch(error);
  window.addEventListener('pagehide',()=>{alive=false;clearTimeout(timer);});
  window.addEventListener('pageshow',()=>{alive=true;if(planId)load().catch(error);});
})();
