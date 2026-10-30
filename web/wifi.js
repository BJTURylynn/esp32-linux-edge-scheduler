'use strict';
// Human-only local setup. Credentials never enter the model tool loop or storage.
const wifiView = (() => {
  let active=false,timer=null,loading=false,posting=false,portsLoading=false,generation=0;
  let data=null,csrf=null,central=null,centralAvailable=false,portsLoaded=false;
  const connectedStates=new Set(['idle','dispatching','running']);
  const phaseText={probing:'正在通过 USB 读取板端状态…',connecting:'正在连接 Wi-Fi，请等待板端确认…',attaching:'正在更新网关连接…'};
  const wifiNames={connected:'Wi-Fi 已连接',disconnected:'Wi-Fi 未连接',unknown:'Wi-Fi 状态未确认',connecting:'Wi-Fi 连接中'};
  const centralNames={idle:'中央已接入 · 空闲',running:'中央已接入 · 计算中',dispatching:'中央已接入 · 等待接受',offline:'尚未接入中央',unknown:'中央状态未确认',error:'中央连接异常'};
  const pending=()=>{
    if(!centralAvailable)return false;
    const node=central?.nodes?.find(item=>item.device==='esp32c3-01');
    return ['dispatching','running'].includes(node?.state)||(node?.state==='idle'&&(central?.queue_depth||0)>0);
  };
  const busy=()=>posting||data?.busy===true;
  function current(){
    const node=centralAvailable?central?.nodes?.find(item=>item.device==='esp32c3-01'):null;
    const centralState=centralAvailable?(node?.state||'offline'):(data?.central_available?data.central_state||'unknown':'unknown');
    const state=connectedStates.has(centralState)?'connected':busy()&&data?.phase==='connecting'?'connecting':data?.wifi_state||'unknown';
    return {state,centralState,host:data?.host||null,supported:data?.supported===true};
  }
  async function api(path,body){
    const options={cache:'no-store',credentials:'same-origin'};
    if(body!==undefined){if(!csrf)throw new Error('页面连接凭证尚未就绪，请刷新连接状态。');options.method='POST';options.headers={'Content-Type':'application/json','X-Edge-CSRF':csrf};options.body=JSON.stringify(body);}
    const response=await fetch(path,options);let value;
    try{value=await response.json();}catch(_){throw new Error('本机连接服务没有返回有效数据。');}
    if(!response.ok)throw new Error(value.error||'本次连接操作未完成。');
    return value;
  }
  function error(message){$('wifi-form-error').textContent=message||'';$('wifi-form-error').hidden=!message;}
  function paint(notify=true){
    const status=current(),supported=status.supported,isBusy=busy();
    const badge=$('wifi-status-badge');badge.textContent=wifiNames[status.state]||wifiNames.unknown;badge.className='wifi-badge '+status.state;
    const centralBadge=$('wifi-central-badge');centralBadge.textContent=centralNames[status.centralState]||centralNames.unknown;centralBadge.className='wifi-badge '+(connectedStates.has(status.centralState)?'connected':'unknown');
    $('wifi-ip').textContent='网关 IP '+(status.host||'未设置');
    $('wifi-message').textContent=phaseText[data?.phase]||data?.message||(data?'尚未取得板端 Wi-Fi 诊断。中央未接入不等于 Wi-Fi 未连接，可连接 USB 后主动读取状态。':'正在读取本机连接服务…');
    const observation=data?.observation;
    if(observation?.state==='connected'&&observation.ip&&!$('wifi-host').value)$('wifi-host').value=observation.ip;
    if(observation?.observed_at){
      const stamp=typeof observation.observed_at==='number'?new Date(observation.observed_at*1000):new Date(observation.observed_at);
      const clock=Number.isNaN(stamp.getTime())?String(observation.observed_at):stamp.toLocaleTimeString('zh-CN');
      const details=[`最近板端检测 ${clock}`,wifiNames[observation.state]||wifiNames.unknown];
      if(observation.ip)details.push('IP '+observation.ip);
      if(typeof observation.rssi_dbm==='number')details.push(`信号 ${observation.rssi_dbm} dBm`);
      $('wifi-observation').textContent=details.join(' · ')+'。此记录不替代当前中央握手。';
    }else $('wifi-observation').textContent='Wi-Fi 状态由板端诊断或当前 TCP 握手确认；没有证据时显示“未确认”。';
    $('wifi-unsupported').hidden=!data||supported;
    $('wifi-forms').hidden=Boolean(data)&&!supported;
    $('wifi-config-fields').disabled=!supported||isBusy;
    $('wifi-attach-fields').disabled=!supported||isBusy||pending()||!centralAvailable;
    $('wifi-connect').disabled=!supported||isBusy||pending()||!centralAvailable||!$('wifi-port').value;
    $('wifi-probe').disabled=!supported||isBusy||!$('wifi-port').value;
    $('wifi-ports-refresh').disabled=!supported||isBusy||portsLoading;
    $('wifi-ports-refresh').textContent=portsLoading?'正在刷新…':'刷新串口';
    $('wifi-connect').textContent=data?.phase==='connecting'?'正在连接…':'连接 Wi-Fi 并接入中央';
    const lock=isBusy?'设备连接操作进行中，暂不接受新的网页任务。':pending()?'ESP32 正在执行任务，或已接入且有任务等待派发。完成后才能更换它的 Wi-Fi 或网关地址。':supported&&!centralAvailable?'请先连接中央调度服务，再更换 Wi-Fi 或网关地址。':'';
    $('wifi-operation-lock').textContent=lock;$('wifi-operation-lock').hidden=!lock;
    if(notify)document.dispatchEvent(new CustomEvent('edge-wifi-state'));
  }
  async function refresh(){
    if(loading)return;loading=true;const ticket=generation;
    try{const value=await api('/api/dispatch/wifi');if(ticket!==generation)return;data=value;csrf=value.csrf_token||csrf;paint();}
    catch(_){if(ticket===generation){data={supported:false,phase:'error',message:'连接管理服务暂不可用，请检查本机启动终端后重试。'};paint();}}
    finally{loading=false;}
  }
  async function ports(){
    if(portsLoading||!data?.supported)return;portsLoading=true;paint(false);error('');
    const selected=$('wifi-port').value||data.serial_port||'';
    try{
      const result=await api('/api/dispatch/wifi/ports');
      const select=$('wifi-port');select.replaceChildren();const placeholder=document.createElement('option');placeholder.value='';placeholder.textContent=result.serial_available===false?'缺少串口依赖，请安装 requirements-device.txt':result.ports?.length?'选择 ESP32 对应的 USB 串口':'未发现可用串口，请连接 USB 数据线';select.append(placeholder);
      for(const item of result.ports||[]){if(typeof item.port!=='string')continue;const option=document.createElement('option');option.value=item.port;option.textContent=item.port+(item.description?' · '+item.description:'');select.append(option);}
      if(Array.from(select.options).some(option=>option.value===selected))select.value=selected;
      portsLoaded=true;
    }catch(failure){error(failure.message);}
    finally{portsLoading=false;paint(false);}
  }
  async function operation(path,body){
    if(busy())return;posting=true;error('');paint();
    try{const value=await api(path,body);data={...data,...value};if(value.csrf_token)csrf=value.csrf_token;}
    catch(failure){error(failure.message);}
    finally{body.password='';posting=false;await refresh();paint();}
  }
  function open(){
    $('wifi-panel').hidden=false;$('wifi-title').scrollIntoView({behavior:'smooth',block:'center'});
    if(!portsLoaded)ports();
  }
  $('wifi-close').addEventListener('click',()=>{$('wifi-password').value='';$('wifi-panel').hidden=true;});
  $('wifi-ports-refresh').addEventListener('click',ports);
  $('wifi-port').addEventListener('change',()=>paint(false));
  $('wifi-probe').addEventListener('click',()=>{if($('wifi-port').value)operation('/api/dispatch/wifi/probe',{port:$('wifi-port').value});});
  $('wifi-connect-form').addEventListener('submit',event=>{
    event.preventDefault();if(busy()||pending()||!centralAvailable)return;
    const form=$('wifi-connect-form');if(!form.reportValidity())return;
    const body={port:$('wifi-port').value,ssid:$('wifi-ssid').value,password:$('wifi-password').value};
    $('wifi-password').value='';operation('/api/dispatch/wifi/connect',body);
  });
  $('wifi-attach-form').addEventListener('submit',event=>{
    event.preventDefault();if(busy()||pending()||!centralAvailable)return;
    if($('wifi-attach-form').reportValidity())operation('/api/dispatch/wifi/attach',{host:$('wifi-host').value.trim()});
  });
  paint(false);
  return {busy,snapshot:current,open,refresh,
    updateCentral(value,available){central=value;centralAvailable=available;paint(false);},
    start(){if(active)return;active=true;generation++;refresh();timer=setInterval(()=>{if(active&&!document.hidden)refresh();},2000);},
    stop(){active=false;generation++;if(timer)clearInterval(timer);timer=null;$('wifi-password').value='';}
  };
})();
