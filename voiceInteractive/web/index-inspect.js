// ===== 摄像头巡检模块（多路并行）：飞桨 8771 实时分析（口罩 3s / 摔倒 / 打架）→ 巡检窗锁定顶部+层级最高 + 独立告警详情窗 + 语音播报 =====
// 旧 qwen 60s 截帧轮询链路已移除（mask_engine 仅剩 paddlehub|off）；/vision 端点仍服务语音视觉问答
// 依赖（全局，由先加载的脚本提供）：
//   audioContext,ttsLive(index-ui.js)；fetchWithTimeout,blobToBase64(voice-utils.js)；
//   camSlots,captureCamFrame,attachCamDrag(index-cam.js)；resolveCameraSlot,speakAnswer(index-voice.js)

let inspectStates = new Map(); // deviceId -> {slot,label,deviceId,order,alertWin,lastUrl,countdownEl,alertAutoHide}
let inspectSeq = 0;             // 位置顺序递增（停不复用，避免窗口重叠）
let inspectHistory=[];          // 全部巡检路的告警历史 {label,url,person_count,no_mask,summary,time}
let _historyBtn=null;

// 巡检窗位置：按 order 顶部水平排列，超宽自动换行
function inspectSlotPos(order){
  const w=480, gap=16, topBase=80, step=w+gap, rowH=410+gap;
  const perRow=Math.max(1, Math.floor((innerWidth-24-24+gap)/step));
  const row=Math.floor(order/perRow), col=order%perRow;
  return { left: 24+col*step, top: topBase+row*rowH };
}
// 告警窗位置：挂在对应巡检窗正下方
function inspectAlertPos(order){
  const p=inspectSlotPos(order);
  return { left: p.left, top: p.top+410+16 };
}

// 独立播报：不侵入对话状态机，避让正在进行的对话播报；多路告警先后播报不重叠
async function inspectSpeak(text){
  if(!audioContext||audioContext.state==='closed') return;
  const t0=Date.now();
  while(ttsLive && Date.now()-t0<15000){ await new Promise(r=>setTimeout(r,200)); } // 避让对话播报，最多等 15s
  try{
    const resp=await fetchWithTimeout('http://localhost:8770/tts',
      {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text})},30000);
    if(!resp.ok) return;
    const arr=await resp.arrayBuffer();
    if(audioContext.state==='suspended')await audioContext.resume();
    const buf=await audioContext.decodeAudioData(arr);
    const src=audioContext.createBufferSource();src.buffer=buf;
    const g=audioContext.createGain();g.gain.value=.9;
    src.connect(g);g.connect(audioContext.destination);
    await new Promise(res=>{src.onended=()=>res();src.onerror=()=>res();src.start();});
  }catch(_){ /* 播报失败不影响巡检 */ }
}

// 告警详情窗（每路独立，可拖可关；复用 .cam-window 结构 + attachCamDrag 拖动）
function ensureAlertWin(e){
  if(e.alertWin) return e.alertWin;
  const win=document.createElement('div');
  win.className='cam-window inspect-alert';
  win.innerHTML=
    '<div class="cam-head"><span class="cam-title"><span class="alert-dot"></span>巡检告警</span><span class="cam-close" title="关闭">✕</span></div>'+
    '<div class="cam-stage"><img class="alert-img" alt="告警截图"></div>'+
    '<div class="alert-body"></div>';
  document.body.appendChild(win);
  attachCamDrag(win);
  win.querySelector('.cam-close').addEventListener('click',()=>hideAlertWin(e));
  e.alertWin=win;
  return win;
}
// 告警类型元信息：口罩 / 摔倒 / 打架 / 区域入侵（均飞桨 8771 SSE 事件）
const ALERT_META={
  mask:{title:'巡检告警', bad:'未戴口罩'},
  fall:{title:'摔倒告警', bad:'人员摔倒'},
  fight:{title:'打架告警', bad:'打架斗殴'},
  intrusion:{title:'区域入侵告警', bad:'区域入侵'}
};
// 告警窗/红脉冲保留时长（ms）：口罩与入侵的后端冷却为 30s，留到下次刷新再消失
const ALERT_TTL={mask:30000, fall:20000, fight:20000, intrusion:30000};
function showAlertWin(e, blobUrl, result){
  const win=ensureAlertWin(e);
  const kind=result.kind||'mask';
  const meta=ALERT_META[kind]||ALERT_META.mask;
  win.querySelector('.cam-title').innerHTML='<span class="alert-dot"></span>⚠ '+e.label+' '+meta.title;
  const img=win.querySelector('.alert-img');
  if(blobUrl){ img.src=blobUrl; img.hidden=false; }
  else { img.removeAttribute('src'); img.hidden=true; }
  let rows='';
  if(kind==='mask'){
    // person_count 实为"检出人脸数"：侧脸/远景小脸检不出人脸，它不等于画面人数，
    // 按人数显示会让人误以为"画面就这些人"
    rows='<div class="alert-row"><b>检出人脸</b><span>'+result.person_count+'</span></div>'+
      '<div class="alert-row alert-bad"><b>未戴口罩</b><span>'+result.no_mask+'</span></div>';
  }else if(kind==='intrusion'){
    rows='<div class="alert-row alert-bad"><b>事件</b><span>人员进入禁区</span></div>'+
      '<div class="alert-row"><b>禁区</b><span>'+(result.region||'-')+'</span></div>'+
      (result.track_id!=null?'<div class="alert-row"><b>目标ID</b><span>'+result.track_id+'</span></div>':'');
  }else if(kind==='fall'){
    rows='<div class="alert-row alert-bad"><b>事件</b><span>人员摔倒</span></div>'+
      (result.track_id!=null?'<div class="alert-row"><b>目标ID</b><span>'+result.track_id+'</span></div>':'')+
      '<div class="alert-row"><b>置信度</b><span>'+Math.round((result.score||0)*100)+'%</span></div>';
  }else{
    rows='<div class="alert-row alert-bad"><b>事件</b><span>打架斗殴</span></div>'+
      '<div class="alert-row"><b>置信度</b><span>'+Math.round((result.score||0)*100)+'%</span></div>';
  }
  win.querySelector('.alert-body').innerHTML=
    rows+
    (result.summary?'<div class="alert-summary">'+result.summary+'</div>':'')+
    '<div class="alert-time">'+new Date().toLocaleTimeString('zh-CN')+'</div>';
  if(!win.classList.contains('open')){
    const p=inspectAlertPos(e.order);
    win.style.left=p.left+'px'; win.style.right='auto'; win.style.top=p.top+'px';
    win.classList.add('open');
  }
  if(e.alertAutoHide)clearTimeout(e.alertAutoHide);
  e.alertAutoHide=setTimeout(()=>hideAlertWin(e), ALERT_TTL[kind]||20000);
}
function hideAlertWin(e){
  if(!e||!e.alertWin) return;
  if(e.alertAutoHide){clearTimeout(e.alertAutoHide);e.alertAutoHide=null;}
  const w=e.alertWin; e.alertWin=null;
  if(e.lastUrl){URL.revokeObjectURL(e.lastUrl);e.lastUrl=null;}
  w.classList.remove('open');
  setTimeout(()=>{if(w.parentNode)w.remove();},320);
}

// 巡检按钮（index-cam.js 标题栏）与巡检状态同步：巡检中显示"停止"，否则显示"巡检"
function syncInspectBtn(slot){
  if(!slot||!slot.win)return;
  const btn=slot.win.querySelector('.cam-inspect-btn');
  if(!btn)return;
  const on=inspectStates.has(slot.deviceId);
  btn.textContent=on?'停止':'巡检';
  btn.title=on?'停止巡检':'开始巡检';
  btn.classList.toggle('on',on);
}

// 巡检窗锁定/解锁（顶部定位 + 禁关禁拖 + 金色强调 + 层级最高）
function setInspectingClass(slot, on, order){
  if(!slot||!slot.win) return;
  if(on){
    const p=inspectSlotPos(order);
    slot.win.classList.add('inspecting');
    slot.win.style.left=p.left+'px'; slot.win.style.top=p.top+'px'; slot.win.style.right='auto';
    if(!slot.win.querySelector('.cam-countdown')){
      const cd=document.createElement('span');
      cd.className='cam-countdown';
      // 倒计时插到操作区最前（巡检按钮/关闭按钮左侧）
      const actions=slot.win.querySelector('.cam-actions');
      if(actions)actions.insertBefore(cd,actions.firstChild);
      else slot.win.querySelector('.cam-head').appendChild(cd);
    }
  }else{
    slot.win.classList.remove('inspecting','alerting');
    const cd=slot.win.querySelector('.cam-countdown');
    if(cd)cd.remove();
  }
}

// 倒计时位置显示口罩引擎状态：开=口罩实时 / off=口罩关（全局 1s 定时器，巡检全停时清除）
let _inspectCountdownTimer=null;
function _ensureCountdownTimer(){
  if(_inspectCountdownTimer)return;
  _inspectCountdownTimer=setInterval(()=>{
    const off=(typeof MASK_ENGINE!=='undefined'&&MASK_ENGINE==='off');
    inspectStates.forEach(e=>{
      if(e.countdownEl)e.countdownEl.textContent=off?'口罩关':'口罩实时';
    });
  },1000);
}
function _maybeStopCountdownTimer(){
  if(inspectStates.size===0&&_inspectCountdownTimer){clearInterval(_inspectCountdownTimer);_inspectCountdownTimer=null;}
}

// 告警历史：右下角按钮 + 面板（点击查看历史截图与描述）
function _showHistoryBtn(){
  if(_historyBtn)return;
  _historyBtn=document.createElement('button');
  _historyBtn.className='inspect-history-btn';
  _historyBtn.textContent='告警历史';
  _historyBtn.addEventListener('click',_showHistoryPanel);
  document.body.appendChild(_historyBtn);
  requestAnimationFrame(()=>_historyBtn.classList.add('show'));
}
function _showHistoryPanel(){
  let panel=document.querySelector('.inspect-history-panel');
  if(panel){panel.remove();return;}
  panel=document.createElement('div');
  panel.className='inspect-history-panel';
  const items=inspectHistory.slice().reverse();
  panel.innerHTML='<div class="ihp-head"><span>巡检告警历史（'+inspectHistory.length+'条）</span><span class="ihp-close">✕</span></div>'+
    '<div class="ihp-body">'+(items.length?items.map(h=>{
      const kind=h.kind||'mask';
      const nums=kind==='mask'
        ? ('人脸 '+h.person_count+' · 未戴 '+h.no_mask)
        : ((ALERT_META[kind]?ALERT_META[kind].bad:'事件')+(h.region?' · '+h.region:''));
      return '<div class="ihp-item"><img class="ihp-img" src="'+h.url+'">'+
        '<div class="ihp-meta"><b>'+h.label+'</b><span class="ihp-time">'+new Date(h.time).toLocaleString('zh-CN')+'</span>'+
        '<div class="ihp-nums">'+nums+'</div>'+
        '<div class="ihp-sum">'+(h.summary||'')+'</div></div></div>';
    }).join(''):'<div class="ihp-empty">暂无告警记录</div>')+'</div>';
  document.body.appendChild(panel);
  panel.querySelector('.ihp-close').addEventListener('click',()=>panel.remove());
  requestAnimationFrame(()=>panel.classList.add('show'));
}

// 启动单路巡检（内部，不播报；已在巡检则跳过返回 false）
function _startOne(slot){
  if(inspectStates.has(slot.deviceId)) return false;
  const label=(camSlots.indexOf(slot)+1)+'号摄像头';
  const order=inspectSeq++;
  const e={slot,label,deviceId:slot.deviceId,order,alertWin:null,lastUrl:null,
    countdownEl:null,alertAutoHide:null,_lastAlertKind:null,_lastMaskAlertTs:0};
  inspectStates.set(slot.deviceId,e);
  setInspectingClass(slot,true,order);
  syncInspectBtn(slot);
  e.countdownEl=slot.win.querySelector('.cam-countdown');
  _ensureCountdownTimer();
  // 异步推流到 SRS 并切同源画面；不 await，不阻塞语音播报。推流失败其内部会自动 stopInspect 回退
  if(typeof beginSrsInspect==='function') beginSrsInspect(slot);
  return true;
}

// 启动巡检：text 为空（"本地摄像头开始巡检"）→ 启动所有本地窗口；指明"N号" → 启动指定路
function _inspectSpeakSuffix(){
  const eng=(typeof MASK_ENGINE!=='undefined')?MASK_ENGINE:'paddlehub';
  return eng==='off' ? '，摔倒、打架和区域入侵实时告警。'
    : '，飞桨实时检测口罩、摔倒、打架和区域入侵。';
}

async function startInspect(text){
  if(typeof loadAppConfig==='function') await loadAppConfig();
  const opens=camSlots.filter(s=>s.win.classList.contains('open')&&!(s.deviceId||'').startsWith('remote:'));
  if(opens.length===0){ await speakAnswer('请先打开本地摄像头再开始巡检。',true); return; }
  if(!text){
    let started=0;
    for(const s of opens){ if(_startOne(s)) started++; }
    if(started===0){ await speakAnswer('本地摄像头已在巡检中。',true); return; }
    await speakAnswer('已开始巡检全部本地摄像头'+_inspectSpeakSuffix(),true);
    return;
  }
  let slot=null;
  const rs=resolveCameraSlot(text);
  if(rs&&!(rs.deviceId||'').startsWith('remote:'))slot=rs;
  if(!slot){ await speakAnswer('没找到'+text+'。',true); return; }
  if(inspectStates.has(slot.deviceId)){ await speakAnswer((camSlots.indexOf(slot)+1)+'号摄像头已在巡检中。',true); return; }
  _startOne(slot);
  await speakAnswer('已开始巡检'+(camSlots.indexOf(slot)+1)+'号摄像头'+_inspectSpeakSuffix(),true);
}

// 停止巡检：slot=指定停单路；slot=null 停全部；silent=true 时不语音（关闭摄像头/切换时调用）
async function stopInspect(slot, silent){
  if(slot){
    const e=inspectStates.get(slot.deviceId);
    if(!e){
      if(!silent) await speakAnswer((camSlots.indexOf(slot)+1)+'号摄像头未在巡检中。',true);
      return;
    }
    inspectStates.delete(slot.deviceId);
    _stopOne(e);
    if(!silent) await speakAnswer((camSlots.indexOf(slot)+1)+'号巡检已停止。',true);
  }else{
    const all=[...inspectStates.values()];
    inspectStates.clear();
    all.forEach(_stopOne);
    if(!silent && all.length) await speakAnswer('巡检已全部停止。',true);
  }
  _maybeStopCountdownTimer();
}
function _stopOne(e){
  if(e.slot&&e.slot.win){
    e.slot.win.classList.remove('inspecting','alerting');
    const cd=e.slot.win.querySelector('.cam-countdown'); if(cd)cd.remove(); // 原仅靠 CSS 隐藏，顺手清除残留节点
    syncInspectBtn(e.slot);
  }
  // 清掉告警相关悬挂定时器：否则巡检停止后仍会触发，对已释放的 blob URL 二次 revoke
  if(e.alertAutoHide){clearTimeout(e.alertAutoHide);e.alertAutoHide=null;}
  if(e._alertClsTimer){clearTimeout(e._alertClsTimer);e._alertClsTimer=null;}
  if(e.alertWin){
    const w=e.alertWin; e.alertWin=null;
    w.classList.remove('open');
    setTimeout(()=>{if(w.parentNode)w.remove();},320);
  }
  if(e.lastUrl){URL.revokeObjectURL(e.lastUrl);e.lastUrl=null;}
  e._lastAlertKind=null;
  // 停 SRS 推流、移除视角按钮、窗口恢复本地零延迟画面（幂等）
  if(typeof endSrsInspect==='function') endSrsInspect(e.slot);
}

// 飞桨 8771 SSE 事件入口（由 voice-srs.js 的 EventSource 回调）：
// mask=实时口罩 / fall/fight=摔倒打架，共用同一告警链路
async function handlePaddleEvent(slot, ev){
  if(!slot||!ev) return;
  const e=inspectStates.get(slot.deviceId);
  if(!e) return; // 已停止巡检的路忽略迟到事件

  if(ev.type==='status'){
    if(ev.running===false && ev.error){
      serverLog('[飞桨]',e.label,'分析异常停止:',ev.error);
    }else if(ev.running===true && Array.isArray(ev.modules)
      && (typeof MASK_ENGINE!=='undefined'?MASK_ENGINE:'paddlehub')==='paddlehub'
      && !ev.modules.includes('mask')){
      // mask 模型加载失败被 worker 降级：口罩检测不可用（无 qwen 兜底，仅提示）
      serverLog('[飞桨]',e.label,'口罩模块未生效，实际模块:',ev.modules.join(','));
    }else if(ev.running===true && Array.isArray(ev.modules)
      && !ev.modules.includes('intrusion')){
      // conf/regions.json 未给该路配禁区（或坐标无效），区域入侵自动关闭
      serverLog('[飞桨]',e.label,'未配置禁区，区域入侵未启用。实际模块:',ev.modules.join(','));
    }
    return;
  }

  if(ev.type==='mask'){
    if((typeof MASK_ENGINE!=='undefined'?MASK_ENGINE:'paddlehub')!=='paddlehub') return;
    const faces=parseInt(ev.faces,10)||0;
    const noMask=parseInt(ev.no_mask,10)||0;
    serverLog('[飞桨口罩]',e.label,'faces='+faces,'no_mask='+noMask);
    if(noMask<=0){
      // 恢复正常：仅当当前告警态来自口罩时复位（避免冲掉摔倒/打架告警）
      if(e._lastAlertKind==='mask'){
        e._lastAlertKind=null;
        e.slot.win.classList.remove('alerting');
        hideAlertWin(e);
      }
      return;
    }
    e._lastAlertKind='mask';
    const result={kind:'mask',person_count:faces,no_mask:noMask,
      summary:'飞桨实时检测：检出'+faces+'张人脸，其中'+noMask+'人未戴口罩。'};
    e.slot.win.classList.add('alerting');
    if(e._alertClsTimer)clearTimeout(e._alertClsTimer);
    e._alertClsTimer=setTimeout(()=>{
      e._alertClsTimer=null;
      if(e._lastAlertKind==='mask'&&e.slot&&e.slot.win)e.slot.win.classList.remove('alerting');
    },ALERT_TTL.mask);
    // 每 3s 来一次违规事件：告警窗画面/人数持续刷新；语音/历史/存盘 20s 冷却一次
    let blob=null;
    try{ blob=await captureCamFrame(e.slot); }catch(_){ /* 截帧失败仍弹窗 */ }
    if(e.lastUrl)URL.revokeObjectURL(e.lastUrl);
    e.lastUrl=blob?URL.createObjectURL(blob):null;
    showAlertWin(e,e.lastUrl,result);
    const now=Date.now();
    const speakCd = now-e._lastMaskAlertTs>20000;
    if(speakCd){
      e._lastMaskAlertTs=now;
      inspectSpeak(e.label+'巡检告警：检出'+faces+'张人脸，其中'+noMask+'人未戴口罩。');
      if(blob){
        inspectHistory.push({kind:'mask', label:e.label, url:URL.createObjectURL(blob),
          person_count:faces, no_mask:noMask, summary:result.summary, time:new Date()});
        if(inspectHistory.length>30){const old=inspectHistory.shift(); URL.revokeObjectURL(old.url);}
        _showHistoryBtn();
        blobToBase64(blob).then(b64=>fetchWithTimeout('http://localhost:8770/vision_save',
          {method:'POST',headers:{'Content-Type':'application/json'},
            body:JSON.stringify({image:b64, text:'飞桨实时口罩检测', answer:result.summary})},30000)).catch(()=>{});
      }
    }
    return;
  }

  if(ev.type!=='fall'&&ev.type!=='fight'&&ev.type!=='intrusion') return;
  const kind=ev.type;
  e._lastAlertKind=kind;
  const result=kind==='fall'
    ? {kind, track_id:ev.track_id, score:ev.score, summary:'检测到人员摔倒，请立即现场确认。'}
    : kind==='fight'
    ? {kind, score:ev.score, summary:'检测到打架斗殴行为，请立即现场确认。'}
    : {kind, track_id:ev.track_id, region:ev.region,
       summary:'检测到人员进入禁区「'+(ev.region||'未知区域')+'」，请立即现场确认。'};
  serverLog('[飞桨告警]',e.label,kind,JSON.stringify(ev));
  e.slot.win.classList.add('alerting');
  // 飞桨事件无"恢复正常"信号：到时后自动解除红脉冲（与告警窗自动消失同步）
  if(e._alertClsTimer)clearTimeout(e._alertClsTimer);
  e._alertClsTimer=setTimeout(()=>{ e._alertClsTimer=null; if(e.slot&&e.slot.win)e.slot.win.classList.remove('alerting'); },ALERT_TTL[kind]||20000);
  let blob=null;
  try{ blob=await captureCamFrame(e.slot); }catch(_){ /* 截帧失败仍弹窗（无图） */ }
  if(e.lastUrl)URL.revokeObjectURL(e.lastUrl);
  e.lastUrl=blob?URL.createObjectURL(blob):null;
  showAlertWin(e,e.lastUrl,result);
  // 同类事件 10s 内只播报一次：多路/多目标并发时避免 TTS 排队堆叠
  const now=Date.now();
  if(!e._kindTs) e._kindTs={};
  const canSpeak = now-(e._kindTs[kind]||0) > 10000;
  if(!canSpeak) return;
  e._kindTs[kind]=now;
  const spoken = kind==='fall' ? '巡检告警：检测到人员摔倒。'
    : kind==='fight' ? '巡检告警：检测到打架斗殴行为。'
    : '巡检告警：检测到人员进入禁区'+(ev.region||'')+'。';
  inspectSpeak(e.label+spoken);
  if(blob){
    // 历史（独立 URL）+ 存盘取证（复用 8770 轻量存盘端点）
    inspectHistory.push({kind, label:e.label, url:URL.createObjectURL(blob),
      person_count:null, no_mask:0, region:ev.region||'', summary:result.summary, time:new Date()});
    if(inspectHistory.length>30){const old=inspectHistory.shift(); URL.revokeObjectURL(old.url);}
    _showHistoryBtn();
    const saveText = kind==='fall' ? '飞桨摔倒事件' : kind==='fight' ? '飞桨打架事件' : '飞桨区域入侵事件';
    blobToBase64(blob).then(b64=>fetchWithTimeout('http://localhost:8770/vision_save',
      {method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({image:b64, text:saveText, answer:result.summary})},30000)).catch(()=>{});
  }
}
