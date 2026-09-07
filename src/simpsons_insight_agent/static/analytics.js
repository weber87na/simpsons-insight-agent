/* Snapshot-scoped report exploration. Source content is always rendered as text. */
(() => {
  const $ = id => document.getElementById(id);
  const v=window.reportVisuals;
  const {el,button,table,percent,number,describe,labels,values}=v;
  function chart(points,series,title,onPoint,average=false,options={}){return v.chart(points,series,title,onPoint,average,{context:()=>describe(applied)+'；分析期間 '+(current?.trend.date_from||'未知')+'～'+(current?.trend.date_to||'未知'),...options});}
  if(!$('analytics-summary'))return;
  const reportId=document.body.dataset.reportId, base=`/api/reports/${reportId}`;
  let applied=new URLSearchParams(), controller, generation=0, current, threadPage=1, keywordRequest=0, threadRequest=0;
  const fields={date_from:'trend-from',date_to:'trend-to',interval:'trend-interval',source:'trend-source',topic_key:'trend-topic',content_type:'analytics-filter-type',sentiment:'analytics-filter-sentiment',aspect:'analytics-filter-aspect',board:'analytics-filter-board',q:'analytics-filter-q',rating:'analytics-filter-rating'};
  const termFields={any_terms:'analytics-filter-any',all_terms:'analytics-filter-all',exclude_terms:'analytics-filter-exclude'};
  const restored=new URLSearchParams(location.search);
  for(const [k,id] of Object.entries(fields))if(restored.has(k)&&$(id))$(id).value=restored.get(k);
  for(const [k,id] of Object.entries(termFields))$(id).value=restored.getAll(k).join('、');
  const defaults={...fields};
  function readFilters(){const q=new URLSearchParams({precision_policy:'interval'});for(const [k,id] of Object.entries(defaults))if($(id).value)q.set(k,$(id).value);for(const [k,id] of Object.entries(termFields))for(const term of $(id).value.split(/[、,\n]/).map(t=>t.trim()).filter(Boolean))q.append(k,term);return q;}
  async function api(path,q,signal){const r=await fetch(`${path}?${q}`,{signal});const d=await r.json();if(!r.ok)throw Error(typeof d.detail==='string'?d.detail:JSON.stringify(d.detail));return d;}
  function links(enabled){$('all-evidence').disabled=!enabled;for(const [id,suffix] of [['filtered-csv','/export'],['filtered-json','/export'],['trend-csv','/trends/export']]){const a=$(id),q=new URLSearchParams(applied);q.set('format',id==='filtered-json'?'json':'csv');a.href=base+suffix+'?'+q;a.setAttribute('aria-disabled',String(!enabled));a.onclick=e=>{if(!enabled)e.preventDefault();};}const p=$('filtered-print');p.href=`/reports/${reportId}/print?${applied}`;p.onclick=e=>{if(!enabled)e.preventDefault();};p.setAttribute('aria-disabled',String(!enabled));}
  function drawTrends(){if(!current)return;const points=current.trend.points;
    const click=(p,s)=>{const from=applied.get('date_from');openEvidence({date_from:from&&from>p.period?from:p.period,date_to:p.period_end,...(s.filter||{})});};
    const series=(key,label,color,filter)=>({key,label,color,filter,value:p=>p[key]});
    $('trend-charts').replaceChildren(
      chart(points,[series('count','樣本聲量','#267b77')],'樣本聲量（筆）',click,$('trend-average').checked),
      chart(points,[series('positive_count','正面','#26804c',{sentiment:'positive'}),series('neutral_count','中立','#967414',{sentiment:'neutral'}),series('negative_count','負面','#a23435',{sentiment:'negative'})],'文字情緒（筆）',click),
      chart(points,[series('negative_ratio','負評比例','#a23435')],'負評比例',click,false,{percent:true}),
      chart(points,[series('pn_ratio','P/N','#66519b')],'P/N（正面 ÷ 負面；空值不連線）',click),
      chart(points,Object.keys(current.summary.source_counts).map((source,i)=>({key:source,label:source,color:['#267b77','#a23435','#967414'][i],filter:{source},value:p=>p.source_counts[source]||0})),'來源聲量（筆）',click));
    $('trend-table').replaceChildren(table(['期間','樣本數','正面','中立','負面','負評比例','P/N'],points.map(p=>[button(p.period,()=>click(p,{})),p.count,p.positive_count,p.neutral_count,p.negative_count,percent(p.negative_ratio),number(p.pn_ratio)])));
  }
  async function load(){const mine=++generation;controller?.abort();controller=new AbortController();const q=readFilters();$('trend-note').textContent='更新中；目前畫面為上次已套用結果';links(false);
    try{const [summary,trend,channels]=await Promise.all(['summary','trends','channels'].map(path=>api(`${base}/${path}`,q,controller.signal)));if(mine!==generation)return;
      if(new Set([summary,trend,channels].map(d=>d.scope.scope_key)).size!==1)throw Error('資料版本已變更，請重新套用條件');
      applied=q;current={summary,trend,channels};history.replaceState(null,'',location.pathname+'?'+applied);
      const scope=summary.scope; $('trend-note').textContent=`已套用：${describe(q)} · 納入 ${scope.included_count} 筆，日期未知 ${scope.unknown_date_count}／精度不足 ${scope.imprecise_date_count} 筆。${scope.data_basis==='legacy_live'?'舊報告使用目前資料，非不可變快照。':'報告快照。'} ${scope.collection.complete===false?'部分來源未完成；統計僅代表已取得樣本。':''} ${scope.unavailable_fields.length?'不可用欄位：'+scope.unavailable_fields.map(k=>labels[k]||k).join('、'):''}`;
      $('analytics-summary').replaceChildren(...[['樣本數',summary.count],['已分類文字',summary.classified_count],['負評比例',percent(summary.negative_ratio)],['P/N',number(summary.pn_ratio)],['可識別討論串',summary.thread_count]].map(([label,v])=>{const card=el('article',undefined,'insight-card');card.append(el('h3',label),el('p',String(v)));return card;}));
      renderChannels(channels.items);
      $('distribution-charts').replaceChildren(
        v.bars(['positive','neutral','negative'].map((key,i)=>({label:values[key],value:summary[key+'_count'],key,color:['#26804c','#967414','#a23435'][i]})),'文字情緒分布',{toggle:true,denominator:summary.classified_count,context:()=>describe(applied)+'；分析期間 '+(current?.trend.date_from||'未知')+'～'+(current?.trend.date_to||'未知'),onSelect:r=>openEvidence({sentiment:r.key})}),
        v.bars(Object.entries(summary.source_counts).map(([key,value])=>({key,label:values[key]||key,value})),'來源組成',{toggle:true,denominator:summary.count,context:()=>describe(applied)+'；分析期間 '+(current?.trend.date_from||'未知')+'～'+(current?.trend.date_to||'未知'),onSelect:r=>openEvidence({source:r.key})}));
      $('distribution-note').textContent=`未知 ${summary.unknown_count} 筆、僅星等 ${summary.rating_only_count} 筆；不列入文字情緒分母。`;
      const distribution=el('div',undefined,'actions');
      for(const [key,label] of [['positive','正面'],['neutral','中立'],['negative','負面']])distribution.append(button(`${label} ${summary[key+'_count']} 筆`,()=>openEvidence({sentiment:key})));
      for(const [source,count] of Object.entries(summary.source_counts))distribution.append(button(`${values[source]||source} ${count} 筆`,()=>openEvidence({source})));
      $('analytics-summary').append(distribution);
      drawTrends();links(true);threadPage=1;await Promise.all([loadChannels(),loadThreads(),loadKeywords()]);
    }catch(e){if(e.name!=='AbortError'&&mine===generation)$('trend-note').textContent='載入失敗：'+e.message+'。請重新套用；未更新的畫面仍屬上次結果。';}
  }
  function openEvidence(extra={}){for(const key of ['source','sentiment'])if(!extra.keyword&&extra[key]&&applied.get(key)&&extra[key]!==applied.get(key))return;const q=new URLSearchParams(applied);for(const [k,value] of Object.entries(extra))q.set(k,value);return v.openEvidence({reportId,query:q,onWork:task=>document.getElementById('task-'+task.id)?.scrollIntoView({behavior:'smooth'})});}
  let channelRequest=0;
  function renderChannels(rows){const sourceOnly=$('channel-group').value==='source',sort=$('channel-sort').value;const action=x=>openEvidence({source:x.source,...(!sourceOnly?{channel_label:x.channel_label}:{})});
    $('channel-table').replaceChildren(table(['來源／頻道','樣本','已分類文字','負面筆數','負評比例','討論串'],rows.map(x=>[button(sourceOnly?(values[x.source]||x.source):`${values[x.source]||x.source}／${x.channel_label}`,()=>action(x)),x.sample_count,x.classified_count,x.negative_count,percent(x.negative_ratio),x.thread_count])));
    $('channel-chart').replaceChildren(v.bars(rows.map(x=>({label:sourceOnly?(values[x.source]||x.source):x.channel_label,value:x[sort],row:x})),sourceOnly?'來源排行':'頻道排行',{ratio:sort==='negative_ratio',context:()=>describe(applied)+'；分析期間 '+(current?.trend.date_from||'未知')+'～'+(current?.trend.date_to||'未知')+'；比例分母見資料表',onSelect:r=>action(r.row)}));
    $('channel-table').hidden=$('channel-view').value==='chart';$('channel-chart').hidden=$('channel-view').value==='table';
  }
  async function loadChannels(){const request=++channelRequest,mine=generation,q=new URLSearchParams(applied);q.set('group_by',$('channel-group').value);q.set('sort',$('channel-sort').value);$('channel-status').textContent='更新中；目前顯示上次完成的排行';try{const d=await api(base+'/channels',q);if(request!==channelRequest||mine!==generation)return;renderChannels(d.items);$('channel-status').textContent=d.items.length?'已套用排行條件':'沒有可排行資料';}catch(e){if(request===channelRequest&&mine===generation)$('channel-status').textContent='載入失敗，保留上次完成結果：'+e.message;}}
  async function loadThreads(){const request=++threadRequest,mine=generation,q=new URLSearchParams(applied),contents=$('content-mode').value==='items';q.set('sort',contents?$('content-sort').value:$('thread-sort').value);q.set('page',threadPage);$('thread-status').textContent='更新中；目前顯示上次完成的內容';try{const d=await api(base+(contents?'/items':'/threads'),q);if(mine!==generation||request!==threadRequest)return;
    $('thread-table').replaceChildren(contents?table(['內容','摘要','來源／頻道','日期與精度','平台互動'],d.items.map(x=>[button(x.title||'查看內容',()=>openEvidence({review_id:x.id})),x.text.slice(0,160),`${values[x.source]||x.source}／${x.board||'未提供'}`,`${x.published_at_estimated||'未知'}（${x.date_precision}）`,x.platform_data?.reaction_count??'未提供'])):table(['討論串','摘要','來源／頻道','最近日期與精度','已蒐集','平台回應'],d.items.map(x=>[button(x.title||x.thread_source_id,()=>openEvidence({source:x.source,thread_source_id:x.thread_source_id})),x.excerpt,`${values[x.source]||x.source}／${x.board||'未提供'}`,`${x.latest_date||'未知'}（${x.date_precision}）`,x.collected_count,x.reported_reply_count??'未提供'])));
    $('thread-status').textContent=`${d.total} ${contents?'筆內容':'個討論串'} · 第 ${threadPage} 頁`;$('thread-prev').disabled=threadPage===1;$('thread-next').disabled=threadPage*25>=d.total;
  }catch(e){if(mine!==generation||request!==threadRequest)return;$('thread-status').textContent='載入失敗，保留上次完成結果：'+e.message;}}
  async function loadKeywords(){const request=++keywordRequest,mine=generation,q=new URLSearchParams(applied);q.set('metric',$('keyword-metric').value);q.set('show_brand',$('keyword-brand').checked);const sentiment=$('keyword-sentiment').value;if(sentiment)q.set('sentiment',sentiment);$('keyword-status').textContent='更新中；目前顯示上次完成的詞語';try{const d=await api(base+'/keywords',q);if(mine!==generation||request!==keywordRequest)return;$('keyword-status').textContent=`${d.scope.included_count} 筆文字候選 · ${d.tokenizer_version} · 範圍：${sentiment||'目前套用情緒'}`;
      const action=t=>openEvidence({keyword:t,...(sentiment?{sentiment}:{})});$('keyword-table').replaceChildren(table(['詞語','詞頻','文件頻率'],d.items.map(x=>[button(x.term,()=>action(x.term)),x.term_frequency,x.document_frequency])));const max=Math.max(1,...d.items.map(x=>x[$('keyword-metric').value]));$('keyword-cloud').replaceChildren(...d.items.map(x=>{const b=button(x.term,()=>action(x.term));b.style.fontSize=(16+32*x[$('keyword-metric').value]/max)+'px';b.title=`詞頻 ${x.term_frequency}／文件頻率 ${x.document_frequency}`;return b;}));}catch(e){if(mine!==generation||request!==keywordRequest)return;$('keyword-status').textContent='載入失敗，保留上次完成結果：'+e.message;}}
  $('trend-form').addEventListener('input',()=>{$('trend-note').textContent='條件尚未套用；圖表與匯出仍使用上次條件';});
  $('trend-average').onchange=drawTrends;$('thread-sort').onchange=()=>{threadPage=1;loadThreads();};$('thread-prev').onclick=()=>{threadPage--;loadThreads();};$('thread-next').onclick=()=>{threadPage++;loadThreads();};
  for(const id of ['keyword-metric','keyword-sentiment','keyword-brand'])$(id).onchange=loadKeywords;
  for(const id of ['channel-group','channel-sort','channel-view'])$(id).onchange=loadChannels;
  for(const id of ['content-mode','content-sort'])$(id).onchange=()=>{threadPage=1;loadThreads();};
  $('channel-retry').onclick=loadChannels;$('content-retry').onclick=loadThreads;$('keyword-retry').onclick=loadKeywords;
  $('all-evidence').onclick=()=>openEvidence();window.operationalAnalytics={load,getFilters:()=>v.query(applied),async applyFilters(saved){for(const [key,id]of Object.entries(fields))$(id).value=saved[key]??(key==='interval'?'month':'');for(const [key,id]of Object.entries(termFields))$(id).value=(saved[key]||[]).join('、');await load();}};
})();
