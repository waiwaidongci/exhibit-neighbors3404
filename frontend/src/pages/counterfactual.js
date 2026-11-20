import {domain} from '../constants/domain.js';import {research} from '../stores/research.js';import {session} from '../stores/session.js';import {counterfactualApi} from '../api/research.js';import {runOptions} from '../components/common/business.js';import {status} from '../components/common/status.js';import {table} from '../components/common/table.js';import {esc,fmt,formObject} from '../utils/format.js';import {action,toast,bind} from '../hooks/actions.js';

const stateLabels={queued:'排队中',running:'搜索中',paused:'预算用尽 · 可续查',completed:'已证最少',none_found:'范围内无方案',failed:'搜索失败'};
function cfStatus(value){return '<span class="status '+esc(value)+'">'+esc(stateLabels[value]||value)+'</span>'}
function sizeLabel(n){return Number.isInteger(n)?n+' 条':'—'}
function rating(value){return fmt(Math.round(value*1000)/1000)}

function planTable(job){
  if(!job.best)return '<p class="empty">尚未找到可换掉首推的改动方案。已核对的分支不会因中断或重试重复计入。</p>';
  const rows=job.best.edits.map(e=>({reference:e.reference,visitor:e.visitor,exhibit:e.exhibit,from:e.from,to:e.to}));
  const before=job.best.base_ranking.map(x=>x.exhibit+' '+rating(x.prediction)).join(' · ');
  const after=job.best.after_ranking.map(x=>x.exhibit+' '+rating(x.prediction)).join(' · ');
  return '<h3>当前最佳方案 · 改动 '+job.best.size+' 条评分</h3>'+table(rows,[{key:'reference',label:'样本编号'},{key:'visitor',label:'匿名访客'},{key:'exhibit',label:'展品编号'},{key:'from',label:'原评分'},{key:'to',label:'改后评分'}])+'<p class="fine">基准排序：'+esc(before)+'<br>改动后排序：'+esc(after)+'<br>方案指纹 <code>'+esc((job.best.edited_checksum||'').slice(0,16))+'…</code> · 来源：'+esc(job.best.source==='guided'?'启发式先找到':'穷举核对')+'</p>';
}

function coverage(job){
  const counts=job.total_plans||[],checked=job.checked||{};
  const rows=[],last=Math.min(counts.length-1,Math.max(job.lower_bound+1,1));
  for(let d=0;d<=last;d++){
    const count=checked[String(d)]||0;
    const doneByDepth=d<=job.exhausted_depth;
    rows.push({size:d,checked:count,total:counts[d],state:doneByDepth?'已穷尽':(d<=job.lower_bound?'进行中':'待查')});
  }
  return '<h3>已查范围与可证下界</h3>'+'<p class="fine">下界：<strong>'+sizeLabel(job.lower_bound)+'改动</strong>'+(job.lower_bound>0?'（更少改动的全部组合已核对完毕）':'')+' · 预算 '+fmt(job.budget)+' · 已评估 '+fmt(job.nodes)+' 个方案 · 待查评分 '+job.scope_count+' 条</p>'+table(rows,[{key:'size',label:'改动条数'},{key:'checked',label:'已查方案'},{key:'total',label:'该层组合总数'},{key:'state',label:'状态'}],{limit:12});
}

function proofBlock(job){
  if(!job.proof)return '';
  const done=['completed','none_found'].includes(job.status);
  return '<div class="'+(done?'method-sheet':'pending')+'"><p class="eyebrow">SEARCH BOUND</p><p>'+esc(job.proof)+'</p>'+(done?'':'<p class="fine">预算用尽时先显示已证下界和已找到的方案；下界以下的方案不存在，下界本身尚未证齐前不报“最少”。</p>')+'</div>';
}

function nearMisses(job){
  const entries=Object.entries(job.near_misses||{}).filter(([,v])=>v&&v.plan&&v.plan.length);
  if(!entries.length)return '';
  return '<h3>最接近换推的未成立组合</h3>'+table(entries.map(([d,v])=>({size:d,top:v.top,runner:v.runner_up,margin:v.margin,plan:v.plan.map(p=>p.reference+' '+p.from+'→'+p.to).join('；')})),[{key:'size',label:'改动条数'},{key:'top',label:'仍居首'},{key:'runner',label:'最接近者'},{key:'margin',label:'领先差距'},{key:'plan',label:'组合'}],{limit:6})+'<p class="fine">差距越小说明这条评分越接近改变结论，但仍未换推，不计入方案。</p>';
}

function jobLedger(jobs,activeId){
  if(!jobs.length)return '<p class="empty">这部研究还没有反事实复核记录。</p>';
  return '<div class="ledger">'+jobs.map(j=>'<a href="#counterfactual" data-cf="'+j.id+'" class="'+(j.id===activeId?'active':'')+'"><small>#'+j.id+'</small><strong>'+esc(j.visitor)+' · '+j.scope_count+' 条待查</strong>'+cfStatus(j.status)+'<time>下界 '+sizeLabel(j.lower_bound)+(j.best_size?' · 已有 '+j.best_size+' 条方案':'')+'</time></a>').join('')+'</div>';
}

export async function counterfactualPage(){
  const runId=research.runId;
  if(!runId)return '<p class="empty">还没有已完成的研究，请先在“分析配置”执行一轮计算。</p>';
  let options=null,jobs=[],detail=null,error='';
  try{
    options=await counterfactualApi.options(runId);
    jobs=await counterfactualApi.list(runId);
    research.cfJobs=jobs;
    if(!research.cfId&&jobs.length)research.cfId=jobs[0].id;
    if(research.cfId&&!jobs.some(j=>j.id===research.cfId))research.cfId=jobs.length?jobs[0].id:null;
    if(research.cfId)detail=await counterfactualApi.get(research.cfId);
    research.cfDetail=detail;
  }catch(e){error=e.message}
  if(error)return '<section class="page-heading"><p class="eyebrow">07 / COUNTERFACTUAL</p><h1>评分记错，会不会换掉首推？</h1><p>从一条已完成研究出发，找出哪些记录评分若被误记一星以内，就会换掉某位访客的首推展品。</p></section><div class="failure"><h2>读取复核记录失败</h2><p>'+esc(error)+'</p><p>请先在“完整结果”确认该研究已完成计算。</p></div><label class="compact">已完成研究<select id="cf-run">'+runOptions(research.runs,runId,true)+'</select></label>';
  if(!options||options.run_id!==runId){
    const detailBlock=options?'<p class="fine">该任务尚未完成计算，只能核对已完成研究。当前研究：'+esc(options.name)+'</p>':'';
    return '<section class="page-heading"><p class="eyebrow">07 / COUNTERFACTUAL</p><h1>评分误记复核</h1><p>从一条已完成研究出发，找出哪些记录评分若被误记一星以内，就会换掉某位访客的首推展品。</p></section><label class="compact">已完成研究<select id="cf-run">'+runOptions(research.runs,runId,true)+'</select></label>'+detailBlock+'<div class="pending">该研究的快照尚不具备复核条件。</div>';
  }
  const visitors=options.visitors.map(v=>'<option value="'+esc(v.visitor)+'">'+esc(v.visitor)+' · '+v.ratings+' 条评分</option>').join('');
  const form=session.writable?'<form id="cf-form"><label>待查访客<select name="visitor">'+visitors+'</select></label><label>待查范围<select name="scope_mode" id="cf-scope-mode"><option value="source">整份研究快照（'+options.record_count+' 条）</option><option value="custom">指定样本编号</option></select></label><label id="cf-refs-label" hidden>样本编号<textarea name="scope_refs" rows="3" placeholder="用逗号或换行分隔，例如 WP-202611-01-0001, WP-202611-01-0002"></textarea></label><label>搜索预算（评估方案数）<input type="number" name="budget" min="100" max="5000000" step="100" value="200000" required></label><p class="fine">每条改动最多一星，且保持 1–5 分。相同访客、参数与范围会续用已有任务，不重复建单。</p><button type="submit">发起反事实复核 →</button></form>':'<p>阅读者可以查看复核结论与核对结果；发起与续查由分析师或负责人执行。</p>';
  return '<section class="page-heading"><p class="eyebrow">07 / COUNTERFACTUAL</p><h1>评分记错，会不会换掉首推？</h1><p>'+esc(options.name)+' · 冻结 '+options.record_count+' 条评分 · 参数 '+esc(JSON.stringify(options.parameters))+' · 输入 SHA256 <code>'+esc(options.checksum.slice(0,16))+'…</code></p></section><div class="split"><section><label class="compact">已完成研究<select id="cf-run">'+runOptions(research.runs,runId,true)+'</select></label><h2>该研究的复核任务</h2>'+jobLedger(jobs,research.cfId)+(detail?'<div class="run-title"><h2>任务 #'+detail.id+' · '+esc(detail.visitor)+'</h2>'+cfStatus(detail.status)+'<small>'+detail.scope_count+' 条待查 · 预算 '+fmt(detail.budget)+'</small>'+(!['completed','none_found'].includes(detail.status)&&session.writable?'<button id="cf-resume" class="secondary">续查（沿用同一评分版本）↻</button>':'')+'<button id="cf-verify" class="secondary">核对结论 ✓</button></div>'+(detail.error?'<div class="failure"><h2>搜索未完成</h2><p>'+esc(detail.error)+'</p></div>':'')+proofBlock(detail)+coverage(detail)+planTable(detail)+nearMisses(detail)+'<div id="cf-verify-result"></div>':'<p class="empty">选择访客与范围后发起一次复核，页面会显示可证下界与已找到的方案。</p>')+'</section><aside class="entry-sheet"><h2>发起复核</h2>'+form+'<div class="method-note"><b>口径</b><p>基准首推与预测值直接来自已完成研究的算法结果。改动只作用于本复核的评分版本，原研究与既有说明保持不动。搜索按改动条数从少到多推进，已查分支写入游标；中断或重试从同一版本继续，不会重复计入。</p></div></aside></div>';
}

export function bindCounterfactual(refresh){
  bind('#cf-run','change',async e=>{research.runId=Number(e.target.value);research.cfId=null;await refresh()});
  bind('#cf-scope-mode','change',e=>{const label=document.getElementById('cf-refs-label');if(label)label.hidden=e.target.value!=='custom'});
  for(const a of document.querySelectorAll('[data-cf]'))a.onclick=async e=>{e.preventDefault();research.cfId=Number(e.currentTarget.dataset.cf);await refresh()};
  action('#cf-form',async form=>{
    const data=formObject(form);
    const refs=data.scope_mode==='custom'?(data.scope_refs||'').split(/[\n,，;；\s]+/).map(x=>x.trim()).filter(Boolean):null;
    if(data.scope_mode==='custom'&&!refs.length)throw Error('请填写至少一个样本编号');
    const job=await counterfactualApi.create(research.runId,{visitor:data.visitor,scope_mode:data.scope_mode,scope_refs:refs,budget:Number(data.budget)});
    research.cfId=job.id;await refresh();toast('复核任务已进入队列，可在本页查看进度与下界');
  });
  bind('#cf-resume','click',async e=>{e.currentTarget.disabled=true;try{await counterfactualApi.resume(research.cfId);await refresh();toast('已沿用同一评分版本继续搜索')}catch(error){toast(error.message,true);e.currentTarget.disabled=false}});
  bind('#cf-verify','click',async e=>{const box=document.getElementById('cf-verify-result');e.currentTarget.disabled=true;try{
    const result=await counterfactualApi.verify(research.cfId);
    box.innerHTML='<h3>独立核对 · '+(result.all_ok?'全部一致 ✓':'发现差异 ✗')+(result.provisional?' · 结论尚未证齐':'')+'</h3>'+table(result.checks.map(c=>({label:c.label,ok:c.ok?'一致':'不一致',actual:c.actual||c.detail||'',expected:c.expected||''})),[{key:'label',label:'核对项'},{key:'ok',label:'结果'},{key:'actual',label:'复算'},{key:'expected',label:'记录'}])+'<p class="fine">核对使用原研究的算法函数重新计算，不修改任何已存结果；'+esc(result.proof||'尚无完整结论')+'</p>';
  }catch(error){toast(error.message,true)}finally{e.currentTarget.disabled=false}});
}
