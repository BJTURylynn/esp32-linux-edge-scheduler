'use strict';
const paths = {
  chat:'M21 11.5a8.4 8.4 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.4 8.4 0 0 1-3.8-.9L3 21l1.9-5.7a8.4 8.4 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.4 8.4 0 0 1 3.8-.9h.5a8.5 8.5 0 0 1 8 8v.5Z',
  chart:'M4 3v17h17M9 15V9m5 6V5m5 10v-4',book:'M4 3h7a3 3 0 0 1 3 3v15a4 4 0 0 0-4-2H4V3Zm10 3a3 3 0 0 1 3-3h4v16h-3a4 4 0 0 0-4 2',
  external:'M14 3h7v7m0-7L10 14M10 3H4a1 1 0 0 0-1 1v16a1 1 0 0 0 1 1h16a1 1 0 0 0 1-1v-6',
  shield:'m12 3 8 4v5c0 4-3 7-8 9-5-2-8-5-8-9V7l8-4Zm-4 9 3 3 5-6',settings:'M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8Zm-2-5h4l.7 3 2 .9L19.5 6l2 3.5-2.1 2.1v1.8l2.1 2.1-2 3.5-2.8-.9-2 .9-.7 3h-4l-.7-3-2-.9-2.8.9-2-3.5 2.1-2.1v-1.8L2.5 9.5l2-3.5 2.8.9 2-.9.7-3Z',
  refresh:'M20 7v5h-5M4 17v-5h5M6 7a7 7 0 0 1 12-1l2 3M4 15l2 3a7 7 0 0 0 12-1',clock:'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18Zm0 4v5l3 2',
  layers:'m12 3 10 5-10 5L2 8l10-5Zm-9 9 9 5 9-5M3 16l9 5 9-5',bolt:'m13 2-9 12h7l-1 8 10-13h-7l0-7Z',activity:'M3 12h4l3-8 4 16 3-8h4',
  spark:'m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5L12 3Zm7-1 .7 2.3L22 5l-2.3.7L19 8l-.7-2.3L16 5l2.3-.7L19 2Z',
  plus:'M12 5v14M5 12h14',arrow:'M4 12h16m-6-6 6 6-6 6', 'arrow-up':'M12 19V5m-6 6 6-6 6 6',lock:'M6 10h12v11H6V10Zm3 0V6a3 3 0 0 1 6 0v4m-3 5v2',
  file:'M14 2H5v20h14V7l-5-5Zm0 0v5h5M8 12h8m-8 4h6',files:'M8 2h12v16H8V2ZM4 6v16h12',cpu:'M7 7h10v10H7V7Zm3 3h4v4h-4v-4ZM9 2v5m6-5v5M9 17v5m6-5v5M2 9h5m-5 6h5m10-6h5m-5 6h5',
  close:'m6 6 12 12M6 18 18 6',menu:'M4 6h16M4 12h16M4 18h16',check:'m5 12 4 4L19 6'
};
function icon(name) { return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${paths[name] || paths.file}"/></svg>`; }
document.querySelectorAll('[data-icon]').forEach(el => { el.innerHTML = icon(el.dataset.icon); });
const $ = id => document.getElementById(id);
let state = null;
let busy = false;
let pendingQuestion = null;
let currentSource = 'example';
let refreshing = false;
let stateRequest = 0;

function escapeHTML(text) { return String(text).replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char])); }
function inline(text) { return escapeHTML(text).replace(/`([^`]+)`/g,'<code>$1</code>').replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>'); }
function markdown(text) {
  const lines = String(text || '').replace(/\r\n/g,'\n').split('\n');
  const output = [];
  const cells = line => line.trim().replace(/^\|/,'').replace(/\|$/,'').split('|').map(x=>x.trim());
  for (let i=0;i<lines.length;) {
    const line=lines[i];
    if (!line.trim()) {i++;continue;}
    if (/^\s*```/.test(line)) { const code=[];i++;while(i<lines.length&&!/^\s*```/.test(lines[i]))code.push(lines[i++]);if(i<lines.length)i++;output.push(`<pre><code>${escapeHTML(code.join('\n'))}</code></pre>`);continue; }
    if (line.includes('|') && i+1<lines.length && /^\s*\|?\s*:?-{3,}/.test(lines[i+1])) {
      const header=cells(line);i+=2;const rows=[];
      while(i<lines.length&&lines[i].includes('|')&&lines[i].trim())rows.push(cells(lines[i++]));
      output.push(`<div class="answer-table-wrap"><table><thead><tr>${header.map(cell=>`<th>${inline(cell)}</th>`).join('')}</tr></thead><tbody>${rows.map(row=>`<tr>${header.map((_,n)=>`<td>${inline(row[n]||'')}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`);continue;
    }
    if (/^#{1,6}\s/.test(line)) {output.push(`<h4>${inline(line.replace(/^#{1,6}\s+/,''))}</h4>`);i++;continue;}
    if (/^\s*[-*]\s+/.test(line)||/^\s*\d+[.)]\s+/.test(line)) {
      const ordered=/^\s*\d+[.)]\s+/.test(line),pattern=ordered?/^\s*\d+[.)]\s+/:/^\s*[-*]\s+/;const items=[];
      while(i<lines.length&&pattern.test(lines[i]))items.push(`<li>${inline(lines[i++].replace(pattern,''))}</li>`);
      const tag=ordered?'ol':'ul';output.push(`<${tag}>${items.join('')}</${tag}>`);continue;
    }
    if (/^>\s?/.test(line)){output.push(`<blockquote>${inline(line.replace(/^>\s?/,''))}</blockquote>`);i++;continue;}
    const paragraph=[line];i++;
    while(i<lines.length&&lines[i].trim()&&!/^(#{1,6}\s|\s*[-*]\s|\s*\d+[.)]\s|\s*```|>)/.test(lines[i])&&!(lines[i].includes('|')&&i+1<lines.length&&/^\s*\|?\s*:?-{3,}/.test(lines[i+1])))paragraph.push(lines[i++]);
    output.push(`<p>${inline(paragraph.join('\n')).replace(/\n/g,'<br>')}</p>`);
  }
  return output.join('');
}
function number(value,digits=2) { return typeof value==='number'&&Number.isFinite(value)?value.toLocaleString('en-US',{minimumFractionDigits:digits,maximumFractionDigits:digits}):'—'; }
function count(value) {return typeof value==='number'&&Number.isFinite(value)?value.toLocaleString('en-US'):'—';}
function metric(report,policy,kind,name='wait_ms',field='p95') {return report?.pooled?.[policy]?.[kind]?.[name]?.[field];}
function errorText(data,fallback) {return typeof data?.error==='string'?data.error:typeof data?.message==='string'?data.message:fallback;}
async function request(path,body) {
  const options={cache:'no-store',credentials:'same-origin'};
  if(body!==undefined){if(!state?.csrf_token)throw new Error('本地服务尚未连接，请刷新证据后重试。');options.method='POST';options.headers={'Content-Type':'application/json','X-Edge-CSRF':state.csrf_token};options.body=JSON.stringify(body);}
  const response=await fetch(path,options);let data;
  try{data=await response.json();}catch(_){throw new Error('本地服务返回了无法读取的响应。');}
  if(!response.ok)throw new Error(errorText(data,`请求未完成（${response.status}），请稍后重试。`));
  return data;
}
function setPageError(message){$('page-error').hidden=!message;$('page-error').textContent=message||'';}
function renderState(){
  const status=state?.evidence?.status,report=state?.evidence?.report;
  const s=status?.available?status.data:null,r=report?.available?report.data:null;
  const local=currentSource==='local';
  document.querySelectorAll('[data-source]').forEach(button=>{const active=button.dataset.source===currentSource;button.classList.toggle('active',active);button.setAttribute('aria-pressed',String(active));button.disabled=busy||refreshing;});
  $('snapshot-label').textContent=state?.evidence?.label||(local?'本机实验 · 文件快照':'示例数据 · 作者历史结果');
  $('snapshot-description').textContent=state?.evidence?.description||(local?'只读取本机配置的结果文件，不代表设备或服务当前在线。':'项目作者保存的历史结果，不是你这台电脑的测试结果。');
  $('source-hint').textContent=local?'先运行自己的实验，再用启动参数 --status 和 --report 指定结果文件；刷新不会运行实验。':'示例数据用于了解项目，不是你这台电脑的测试结果。';
  const empty=Boolean(state?.evidence?.empty);
  $('empty-state').hidden=!empty;$('partial-state').hidden=!state?.evidence?.partial;
  $('empty-title').textContent=local?'暂无实验记录':'示例记录暂不可用';
  $('empty-description').textContent=local?'先运行本机实验，再在启动工作台时用 --status 和 --report 指定自己的结果文件。刷新证据不会启动实验，也不会补用作者的示例数据。':'请检查项目自带的示例记录是否完整，再刷新证据。';
  $('evidence-title').textContent=local?'本机实验的调度证据':'示例数据的调度证据';
  $('evidence-description').textContent=local?'只展示启动时配置的本机对照报告；文件快照不替代设备或进程在线检查。':'以下分组统计来自作者保存的对照报告，不是本机实验结果。';
  $('completed-value').textContent=count(s?.completed);$('rejected-value').textContent=count(s?.rejected);
  $('urgent-value').textContent=number(metric(r,'priority','urgent'));$('normal-value').textContent=number(metric(r,'priority','normal'));
  $('completed-note').textContent=s?`状态记录 · 已接收 ${count(s.accepted)} 个`:'状态记录暂不可读';
  $('rejected-note').textContent=s?(s.rejected===0?'本轮记录无拒绝':'请结合任务日志分析拒绝原因'):'以状态记录为准';
  $('queue-value').textContent=s?`${count(s.queue_depth)} / ${count(s.queue_capacity)}`:'—';
  $('snapshot-state').textContent=s?(s.fatal?'记录为异常':s.running?'记录未结束':'记录已结束'):'不可读取';
  for(const [id,value] of [['status-available',status?.available],['report-available',report?.available]]) {const el=$(id);el.className='available-dot '+(value?'available':'unavailable');el.title=value?'证据可读取':'证据暂不可读';}
  const values=['urgent','normal'].flatMap(kind=>['fifo','priority'].map(policy=>metric(r,policy,kind))).filter(v=>typeof v==='number'&&Number.isFinite(v));
  const maximum=Math.max(1,...values);
  for(const kind of ['urgent','normal'])for(const policy of ['fifo','priority']){const value=metric(r,policy,kind);$(`${kind}-${policy}-label`).textContent=number(value,1);$(`${kind}-${policy}-bar`).style.width=(typeof value==='number'&&Number.isFinite(value)?Math.max(0,Math.min(100,value/maximum*100)):0)+'%';}
  $('model-name').textContent=state?.model||'尚未配置模型';$('settings-model').textContent=state?.model||'尚未配置模型';
  $('model-key-state').textContent=state?.key_configured?'密钥已在后端配置 · 点击管理':'尚未配置密钥 · 点击连接';
  $('comparison-status').textContent=r?(r.comparable_configuration?'报告标记：配置可比':'报告标记：配置不可比'):'报告不可读取';
  const body=$('metrics-table');body.replaceChildren();
  if(!r){const row=body.insertRow();const cell=row.insertCell();cell.colSpan=7;cell.textContent=refreshing?'正在读取所选来源的实验记录…':local?'暂无本机对照报告，请运行实验并配置 --report。':'示例报告暂不可读，请检查项目自带的证据文件。';}
  else for(const policy of ['fifo','priority'])for(const kind of ['urgent','normal']){const row=body.insertRow();const m=r.pooled?.[policy]?.[kind];const vals=[policy==='fifo'?'FIFO':'priority',kind==='urgent'?'紧急任务':'普通任务',count(m?.completed),count(m?.rejected),number(m?.wait_ms?.mean,3),number(m?.wait_ms?.p95,3),number(m?.total_ms?.p95,3)];vals.forEach((value,i)=>{const cell=row.insertCell();cell.textContent=value;if(i===5&&policy==='priority')cell.className='emphasis';});}
  updateControls();
}
async function refresh(){
  const selected=currentSource,ticket=++stateRequest;
  refreshing=true;state=null;renderState();setPageError('');
  try{const data=await request(`/api/state?source=${selected}`);if(ticket!==stateRequest||selected!==currentSource)return;if(data.source!==selected||data.evidence?.source!==selected)throw new Error('证据来源未匹配，请刷新后重试。');state=data;renderState();$('backend-state').className='local-indicator ready';$('backend-state').innerHTML='<span class="tiny-dot"></span>本地后端已连接';}
  catch(error){if(ticket!==stateRequest)return;state=null;renderState();setPageError(error.message);$('backend-state').className='local-indicator error';$('backend-state').innerHTML='<span class="tiny-dot"></span>本地服务未连接';}
  finally{if(ticket===stateRequest){refreshing=false;renderState();}}
}
function showView(view){
  const chat=view!=='evidence';$('chat-view').hidden=!chat;$('evidence-view').hidden=chat;
  document.querySelectorAll('[data-view]').forEach(button=>{const selected=button.dataset.view===(chat?'chat':'evidence');button.classList.toggle('active',selected);if(selected)button.setAttribute('aria-current','page');else button.removeAttribute('aria-current');});
  $('breadcrumb-current').textContent=chat?'运维对话':'调度证据';$('page-title').textContent=chat?'让调度数据，回答你的问题。':'每一个结论，都有数据可查。';$('page-description').textContent=chat?'结合设备任务记录与对照实验，理解系统表现和调度取舍。':'查看所选来源的文件记录，核对分组指标与结论范围。';
  closeSidebar();
}
function updateControls(){const blocked=busy||refreshing||!state||Boolean(state.evidence?.empty);$('question').disabled=blocked;$('answer-mode').disabled=busy||refreshing;$('new-chat').disabled=busy;$('send-button').disabled=blocked||!$('question').value.trim();$('refresh-button').disabled=busy||refreshing;$('model-settings').disabled=busy||refreshing||!state;$('model-key-state').disabled=busy||refreshing||!state;document.querySelectorAll('.suggestion').forEach(button=>button.disabled=blocked);document.querySelectorAll('[data-source]').forEach(button=>button.disabled=busy||refreshing);$('chat-form').setAttribute('aria-busy',String(busy));$('send-button').innerHTML=icon(busy?'clock':'arrow-up');}
function setBusy(value){busy=value;updateControls();}
function scrollChat(){$('chat-scroll').scrollTop=$('chat-scroll').scrollHeight;}
function userMessage(text){const row=document.createElement('div');row.className='message message-user';const bubble=document.createElement('div');bubble.className='user-bubble';bubble.textContent=text;row.append(bubble);$('messages').append(row);}
function assistantSkeleton(){const row=document.createElement('div');row.className='message assistant-message';row.innerHTML=`<div class="assistant-avatar">${icon('spark')}</div><div class="assistant-content"></div>`;$('messages').append(row);return row.querySelector('.assistant-content');}
function renderAnswer(container,data){
  const mode=data.mode||'fallback';const label=mode==='llm'?'大模型回答':mode==='rules'?'离线规则':'失败回退 · 规则解释';
  container.innerHTML=`<div class="message-meta"><strong>EdgeScope</strong><span class="mode-badge ${mode==='llm'?'':mode==='rules'?'rules':'fallback'}">${label}</span></div>`;
  const source=document.createElement('span');source.className='source-badge';source.textContent=data.source_label||(data.source==='local'?'本机实验':'示例数据');container.querySelector('.message-meta').append(source);
  if(data.reason){const reason=document.createElement('div');reason.className='fallback-reason';reason.textContent='模型未完成正常回答：'+data.reason;container.append(reason);}
  const answer=document.createElement('div');answer.className='answer-body';answer.innerHTML=markdown(data.answer||'没有可展示的回答，请核对证据与服务配置。');container.append(answer);
  const receipt=document.createElement('div');receipt.className='tool-receipt';receipt.innerHTML=`<div class="receipt-heading">${icon('shield')}<span>本次实际执行记录</span></div>`;
  const tools=document.createElement('div');tools.className='receipt-tools';
  const trace=Array.isArray(data.tool_trace)?data.tool_trace:[];
  for(const item of trace){const chip=document.createElement('span');chip.className='tool-chip'+(item.available?'':' failed');chip.innerHTML=icon(item.available?'check':'clock');chip.append(document.createTextNode(String(item.tool||'未知工具')+(item.available?'':' · 未取得证据')));tools.append(chip);}
  if(!trace.length){const note=document.createElement('span');note.className='receipt-stats';note.textContent=mode==='rules'?'离线规则直接读取配置证据；未调用模型。':'本次没有成功执行的模型工具记录。';tools.append(note);}
  receipt.append(tools);const stats=document.createElement('div');stats.className='receipt-stats';let details=`模型请求 ${count(data.model_calls??0)} 次`;
  if(typeof data.elapsed_seconds==='number')details+=` · 耗时 ${number(data.elapsed_seconds,1)}s`;
  const apiTrace=Array.isArray(data.api_trace)?data.api_trace:[];const total=apiTrace.reduce((sum,item)=>sum+(Number.isInteger(item.usage?.total_tokens)?item.usage.total_tokens:0),0);if(total)details+=` · ${count(total)} tokens`;
  stats.textContent=details;receipt.append(stats);container.append(receipt);
  if(data.evidence_text){const evidence=document.createElement('details');evidence.className='program-evidence';const summary=document.createElement('summary');summary.textContent='展开程序提供的证据摘要';const content=document.createElement('pre');content.textContent=data.evidence_text;evidence.append(summary,content);container.append(evidence);}
}
async function submitQuestion(text){
  if(busy||refreshing)return;text=String(text||'').trim();if(!text)return;if(text.length>2000){setPageError('问题请控制在2000字以内。');return;}
  if(!state){setPageError('请先连接本地服务并刷新证据。');return;}
  if(state.evidence?.empty){setPageError(currentSource==='local'?'暂无实验记录，请先运行本机实验并配置结果文件。':'示例记录不可用，请检查项目文件。');return;}
  if($('answer-mode').value==='llm'&&!state.key_configured){pendingQuestion=text;openSettings();return;}
  showView('chat');$('welcome').hidden=true;userMessage(text);$('question').value='';resizeInput();const content=assistantSkeleton();content.innerHTML='<div class="loading-message"><span class="spinner"></span><span>正在请求回答并核对证据，请稍候…</span></div>';setBusy(true);setPageError('');scrollChat();
  const selected=currentSource;
  try{const data=await request('/api/chat',{question:text,mode:$('answer-mode').value,source:selected});if(data.source!==selected)throw new Error('回答来源未匹配，本次回答未展示，请刷新后重试。');if(typeof data.answer!=='string')throw new Error(errorText(data,'没有取得有效回答，请稍后重试。'));renderAnswer(content,data);}
  catch(error){content.replaceChildren();const node=document.createElement('div');node.className='message-error';node.textContent=error.message;content.append(node);}
  finally{setBusy(false);scrollChat();$('question').focus();}
}
function resizeInput(){const input=$('question');input.style.height='auto';input.style.height=Math.min(145,Math.max(45,input.scrollHeight))+'px';$('char-count').textContent=`${input.value.length} / 2000`;updateControls();}
function openSettings(){$('settings-status').textContent=state?.key_configured?'后端已有密钥。填写下方输入框可替换；留空不会读取已有值。':'输入密钥后即可使用大模型解读，也可直接选择离线规则模式。';$('api-key').value='';$('settings-dialog').showModal();}
function closeSidebar(){$('sidebar').classList.remove('open');$('sidebar-scrim').hidden=true;}

$('chat-form').addEventListener('submit',event=>{event.preventDefault();submitQuestion($('question').value);});
$('question').addEventListener('input',resizeInput);
$('question').addEventListener('keydown',event=>{if(event.key==='Enter'&&!event.shiftKey&&!event.isComposing&&event.keyCode!==229){event.preventDefault();submitQuestion($('question').value);}});
document.querySelectorAll('.suggestion').forEach(button=>button.addEventListener('click',()=>submitQuestion(button.dataset.question)));
document.querySelectorAll('[data-view]').forEach(button=>button.addEventListener('click',()=>showView(button.dataset.view)));
$('new-chat').addEventListener('click',()=>{$('messages').replaceChildren();$('welcome').hidden=false;$('question').value='';resizeInput();pendingQuestion=null;setPageError('');$('question').focus();});
document.querySelectorAll('[data-source]').forEach(button=>button.addEventListener('click',()=>{if(busy||refreshing||button.dataset.source===currentSource)return;currentSource=button.dataset.source;state=null;pendingQuestion=null;$('messages').replaceChildren();$('welcome').hidden=false;$('question').value='';resizeInput();refresh();}));
$('refresh-button').addEventListener('click',refresh);
$('view-evidence').addEventListener('click',()=>showView('evidence'));
$('back-to-chat').addEventListener('click',()=>{showView('chat');$('question').focus();});
$('model-settings').addEventListener('click',openSettings);$('model-key-state').addEventListener('click',openSettings);
$('help-nav').addEventListener('click',()=>{$('help-dialog').showModal();closeSidebar();});
document.querySelector('.brand').addEventListener('click',event=>{event.preventDefault();showView('chat');});
$('menu-button').addEventListener('click',()=>{$('sidebar').classList.add('open');$('sidebar-scrim').hidden=false;});$('sidebar-scrim').addEventListener('click',closeSidebar);
document.querySelectorAll('.close-dialog').forEach(button=>button.addEventListener('click',()=>button.closest('dialog').close()));
$('settings-dialog').addEventListener('close',()=>{$('api-key').value='';pendingQuestion=null;});
$('settings-form').addEventListener('submit',async event=>{
  event.preventDefault();const key=$('api-key').value;if(!key){$('settings-status').textContent='请输入新的API Key，或关闭设置后使用当前配置。';return;}
  $('save-key').disabled=true;$('settings-status').textContent='正在更新本次后端进程的配置…';
  try{const data=await request('/api/config',{api_key:key});$('api-key').value='';state.key_configured=Boolean(data.key_configured);if(data.model)state.model=data.model;renderState();const next=pendingQuestion;pendingQuestion=null;$('settings-dialog').close();if(next)submitQuestion(next);}
  catch(error){$('api-key').value='';$('settings-status').textContent=error.message;}
  finally{$('save-key').disabled=false;}
});
$('clear-key').addEventListener('click',async()=>{
  $('clear-key').disabled=true;
  try{const data=await request('/api/config',{action:'clear'});$('api-key').value='';state.key_configured=Boolean(data.key_configured);pendingQuestion=null;renderState();$('settings-status').textContent='已清除后端当前进程的密钥。仍可使用离线规则模式。';}
  catch(error){$('settings-status').textContent=error.message;}
  finally{$('clear-key').disabled=false;}
});
resizeInput();refresh();
