'use strict';
// Explicit user controls. No connection to the model's read-only tool loop.
const dispatchView = (() => {
  let active=false, timer=null, loading=false, submitting=false, available=false;
  let snapshot=null, csrf=null, generation=0, lastRunId=null;
  const labels={offline:'离线',idle:'空闲',dispatching:'等待接受',running:'计算中',error:'连接异常',queued:'中央排队',succeeded:'已完成',failed:'执行失败',unknown:'结果未知'};
  const humanError={queue_full:'中央等待队列已满',record_limit:'本次运行记录已满，请归档后重启',invalid_request:'任务参数无效',not_retryable:'该任务当前不能重试',not_found:'任务不存在',stale_run:'中央已重启，请刷新后按新的运行编号操作',disconnected:'连接断开',heartbeat_timeout:'设备心跳超时',accept_timeout:'未收到接受确认',task_timeout:'等待结果超时',invalid_task:'设备拒绝任务参数',compute_error:'设备计算失败',nonfinite_result:'设备结果无效',duplicate_task:'设备拒绝重复任务',central_stopped:'中央服务已停止'};
  const fmt=(value,digits=2)=>typeof value==='number'&&Number.isFinite(value)?number(value,digits):'—';
  const errorName=value=>humanError[value]||String(value||'未取得确认');
  async function api(path,body){
    const options={cache:'no-store',credentials:'same-origin'};
    if(body!==undefined){if(!csrf)throw new Error('请先刷新并连接中央服务。');options.method='POST';options.headers={'Content-Type':'application/json','X-Edge-CSRF':csrf};options.body=JSON.stringify(body);}
    const response=await fetch(path,options);let data;
    try{data=await response.json();}catch(_){throw new Error('网页后端未返回有效数据。');}
    if(!response.ok){const e=new Error(data.error||'中央请求未完成。');e.receipts=data.receipts;throw e;}
    return data;
  }
  function controls(){
    $('dispatch-fields').disabled=!available||submitting||wifiView.busy();
    document.querySelectorAll('.dispatch-retry').forEach(button=>button.disabled=!available||submitting||wifiView.busy());
    $('dispatch-submit').textContent=submitting?'正在提交…':wifiView.busy()?'设备连接处理中…':'提交到中央队列';
  }
  function appendCell(row,value,className){const cell=row.insertCell();cell.textContent=String(value);if(className)cell.className=className;return cell;}
  function render(){
    const nodes=Array.isArray(snapshot?.nodes)?snapshot.nodes:[];
    const online=available&&nodes.some(node=>['idle','dispatching','running'].includes(node.state));
    $('dispatch-waiting').hidden=!available||online;
    $('dispatch-waiting').textContent='中央已启动，但还没有计算设备接入。任务只会在有界队列中等待，不会在电脑上代算；请先连接 ESP32 的 Wi-Fi，或检查 Uno 的 USB 串口。';
    $('dispatch-nodes').replaceChildren();
    for(const device of ['esp32c3-01','uno-01']){
      const node=nodes.find(x=>x.device===device),status=available?(node?.state||'offline'):'offline';
      const article=document.createElement('article');article.className='dispatch-node';
      const top=document.createElement('div');top.className='dispatch-node-heading';
      const name=document.createElement('strong');name.textContent=device==='esp32c3-01'?'ESP32-C3':'ColorEasyDuino / Uno';
      const badge=document.createElement('span');badge.className='dispatch-node-state '+status;badge.textContent=device==='esp32c3-01'?(status==='offline'?'中央未接入':'中央 · '+(labels[status]||'状态未知')):labels[status]||'状态未知';top.append(name,badge);
      const link=document.createElement('p');link.textContent=device==='esp32c3-01'?'Wi-Fi / TCP · 独立板端计算':'USB 串口 · 独立板端计算';
      const task=document.createElement('div');task.className='dispatch-node-task';task.textContent=available&&node?.task_id?`当前任务 #${node.task_id}`:status==='idle'?'已准备好接收中央任务':status==='offline'?'等待设备握手':'等待设备状态更新';
      article.append(top,link,task);
      if(device==='esp32c3-01'){
        const wifi=wifiView.snapshot(),wifiState=['idle','dispatching','running'].includes(status)?'connected':wifi.state;
        const summary=document.createElement('div');summary.className='wifi-node-summary';
        const wifiBadge=document.createElement('span');wifiBadge.className='wifi-badge '+wifiState;wifiBadge.textContent=({connected:'Wi-Fi 已连接',disconnected:'Wi-Fi 未连接',connecting:'Wi-Fi 连接中',unknown:'Wi-Fi 状态未确认'})[wifiState]||'Wi-Fi 状态未确认';summary.append(wifiBadge);
        const host=document.createElement('span');host.textContent=wifi.host?'网关 IP '+wifi.host:'尚未设置网关 IP';summary.append(host);
        const action=document.createElement('button');action.type='button';action.className='secondary-button wifi-open';action.textContent=wifiState==='connected'?'管理 Wi-Fi 连接':'连接 Wi-Fi';action.addEventListener('click',wifiView.open);
        article.append(summary,action);
      }
      if(node?.last_error&&available){const err=document.createElement('small');err.textContent='最近事件：'+errorName(node.last_error);article.append(err);}$('dispatch-nodes').append(article);
    }
    const counts=snapshot?.counts;
    $('dispatch-queue').textContent=available?`${count(snapshot.queue_depth)} / ${count(snapshot.queue_capacity)}`:'—';
    $('dispatch-active').textContent=available?count((counts?.dispatching||0)+(counts?.running||0)):'—';
    $('dispatch-completed').textContent=available?count(counts?.succeeded):'—';
    $('dispatch-failures').textContent=available?count((counts?.failed||0)+(counts?.unknown||0)):'—';
    $('dispatch-policy').textContent=available?(snapshot.policy==='priority'?'紧急优先，同级先到先派':'FIFO，先到先派'):'尚未连接';
    $('dispatch-run').textContent=available?String(snapshot.run_id||'—'):'—';
    const body=$('dispatch-tasks');body.replaceChildren();
    const filter=$('dispatch-filter').value;
    const rows=(Array.isArray(snapshot?.tasks)?snapshot.tasks:[]).filter(t=>filter==='all'||filter==='active'&&['queued','dispatching','running'].includes(t.state)||filter==='exception'&&['failed','unknown'].includes(t.state)||filter==='succeeded'&&t.state==='succeeded').slice().reverse();
    if(!available||!rows.length){const row=body.insertRow();const cell=appendCell(row,!available?'中央服务未连接，无法确认设备和任务状态。':snapshot.tasks?.length?'当前筛选没有任务。':'本次运行还没有任务，可以在上方提交。');cell.colSpan=9;}
    else for(const task of rows){
      const row=body.insertRow();const id=appendCell(row,'#'+task.id);if(task.retry_of){const relation=document.createElement('small');relation.textContent=`重试自 #${task.retry_of}`;id.append(relation);}
      appendCell(row,task.priority==='urgent'?'紧急':'普通',task.priority==='urgent'?'dispatch-urgent':'');
      appendCell(row,task.device||(online?'等待空闲设备':'尚无设备接入'));appendCell(row,task.state==='queued'&&!online?'等待设备联网 / 接入':labels[task.state]||task.state,'dispatch-task-state '+task.state);
      const result=appendCell(row,task.state==='succeeded'?`${fmt(task.mean,4)} / ${fmt(task.rms,4)}`:'—');if(task.error){const err=document.createElement('small');err.textContent=errorName(task.error);result.append(err);}
      appendCell(row,typeof task.wait_ms==='number'?fmt(task.wait_ms)+' ms':'—');
      appendCell(row,typeof task.roundtrip_ms==='number'?fmt(task.roundtrip_ms)+' ms':'—');
      appendCell(row,typeof task.compute_us==='number'?fmt(task.compute_us/1000)+' ms':'—');
      const operation=row.insertCell();if(['failed','unknown'].includes(task.state)){const button=document.createElement('button');button.type='button';button.className='text-button dispatch-retry';button.textContent='手动重试';button.setAttribute('aria-label',`重试任务 ${task.id}`);button.addEventListener('click',()=>retry(task.id));operation.append(button);}else operation.textContent='—';
    }
    controls();
  }
  function offline(message){available=false;snapshot=null;csrf=null;wifiView.updateCentral(null,false);$('dispatch-dot').className='dispatch-status-dot';$('dispatch-connection').textContent='中央调度服务未连接';$('dispatch-poll-time').textContent='';$('dispatch-error').hidden=false;$('dispatch-error').textContent=message||'请先运行第二版统一启动入口，再刷新设备。';render();}
  async function refresh(){
    if(loading)return;loading=true;const ticket=generation;
    try{const data=await api('/api/dispatch/state');if(ticket!==generation)return;if(data.available!==true||data.ok!==true||!Array.isArray(data.nodes)||!Array.isArray(data.tasks))throw new Error('中央状态不完整，不能据此提交任务。');if(lastRunId&&lastRunId!==data.run_id)receipt({},'中央运行已更换，旧任务请查已归档记录。');lastRunId=data.run_id;snapshot=data;csrf=data.csrf_token;available=true;wifiView.updateCentral(snapshot,true);$('dispatch-dot').className='dispatch-status-dot connected';$('dispatch-connection').textContent='中央已连接 · 设备在线由握手与心跳确认';$('dispatch-poll-time').textContent='更新于 '+new Date().toLocaleTimeString('zh-CN');$('dispatch-error').hidden=true;render();}
    catch(error){if(ticket===generation)offline(error.message);}
    finally{loading=false;}
  }
  function receipt(data,prefix){
    const element=$('dispatch-receipt');element.hidden=false;element.replaceChildren();
    const title=document.createElement('strong');title.textContent=prefix;element.append(title);
    if(data.accepted||data.rejected){const description=document.createElement('p');description.textContent=`已接收 ${(data.accepted||[]).length} 个，明确拒绝 ${(data.rejected||[]).length} 个。中央接收不等于设备完成。`;element.append(description);}
    for(const item of data.rejected||[]){const line=document.createElement('p');line.textContent=`第 ${(item.index??0)+1} 个任务：${errorName(item.error)}`;element.append(line);}
    if(data.unknown_index!==undefined){const warning=document.createElement('p');warning.textContent=`第 ${data.unknown_index+1} 个任务的提交结果未确认。后续未继续提交，请先刷新列表核对，避免重复执行。`;element.append(warning);}
  }
  async function submit(event){
    event.preventDefault();if(submitting||!available||wifiView.busy())return;
    const form=$('dispatch-form');if(!form.reportValidity())return;const values=new FormData(form);const body={priority:values.get('priority'),run_id:snapshot.run_id};for(const field of ['seed','count','window','passes','batch_size'])body[field]=Number(values.get(field));
    submitting=true;controls();
    try{const result=await api('/api/dispatch/tasks',body);receipt(result,'中央提交回执');}
    catch(error){receipt(error.receipts||{},error.message);}
    finally{submitting=false;await refresh();controls();}
  }
  async function retry(id){
    if(submitting||!available||wifiView.busy())return;submitting=true;controls();
    try{const result=await api(`/api/dispatch/tasks/${id}/retry`,{run_id:snapshot.run_id});receipt({},result.ok?`已创建重试任务 #${result.task_id}，原任务 #${id} 保留。`:errorName(result.error));}
    catch(error){receipt(error.receipts||{},error.message);}
    finally{submitting=false;await refresh();controls();}
  }
  $('dispatch-form').addEventListener('submit',submit);
  $('dispatch-priority').addEventListener('change',()=>{$('dispatch-count').value=$('dispatch-priority').value==='urgent'?'16':'256';});
  $('dispatch-filter').addEventListener('change',render);
  document.addEventListener('edge-wifi-state',render);
  render();
  return {start(){if(active)return;active=true;generation++;available=false;snapshot=null;wifiView.updateCentral(null,false);wifiView.start();render();refresh();timer=setInterval(()=>{if(active&&!document.hidden)refresh();},2000);},stop(){active=false;generation++;if(timer)clearInterval(timer);timer=null;wifiView.stop();},refresh(){wifiView.refresh();return refresh();}};
})();
if(location.hash==='#dispatch')showView('dispatch');
