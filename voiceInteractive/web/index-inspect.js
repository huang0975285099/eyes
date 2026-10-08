// ===== 摄像头巡检模块（多路并行）：定时截帧 → /vision 结构化分析 → 口罩/人数异常 → 巡检窗锁定顶部+层级最高 + 独立告警详情窗 + 语音播报 =====
// 策略：截帧 10s / 告警纯 120s 冷却 / 只存告警帧 / 页面隐藏即暂停 / 支持多摄像头同时巡检
//   （按启动顺序顶部排列，每路独立状态机与告警窗，语音先后播报不重叠，/vision 串行排队）
// 依赖（全局，由先加载的脚本提供）：
//   audioContext,ttsLive(index-ui.js)；fetchWithTimeout,blobToBase64(voice-utils.js)；
//   camSlots,captureCamFrame,attachCamDrag(index-cam.js)；resolveCameraSlot,speakAnswer(index-voice.js)

const INSPECT_INTERVAL = 30000;   // 截帧间隔 30s
const INSPECT_COOLDOWN = 120000;  // 告警冷却 120s
const INSPECT_PROMPT =
  '巡检分析：逐个查看画面中每个人，重点检查其口鼻处是否有口罩遮挡。仅返回一个JSON对象，不要任何解释文字或markdown代码块。' +
  '格式：{"person_count":整数,"no_mask":整数,"summary":"一句话"}。' +
  'person_count=画面中可见的总人数；' +
  'no_mask=口罩未正确佩戴的人数（口鼻处无口罩遮挡、口罩未遮住口鼻、戴在下巴上、或佩戴不规范均算未佩戴）；' +
  '判断原则：只要某人口鼻处可见、未被口罩完全遮挡，就算未佩戴；若不确定某人是否佩戴，一律按未佩戴计入（宁可误报不漏报）；' +
  'summary=必须明确说明有几人未戴口罩；' +
  '一致性：no_mask 必须与 summary 描述一致——summary 提到有人未戴口罩时 no_mask 必须>0，summary 说所有人都戴了时 no_mask=0。';

let inspectStates = new Map(); // deviceId -> {slot,label,deviceId,order,timer,inFlight,alerting,lastAlertTs,alertWin,lastUrl,wasHidden,countdownEl,secsLeft,alertAutoHide}
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

// 解析 /vision 文本为结构化结果；失败返回 null（跳过本轮，不推进状态机）
function parseInspectResult(text){
  try{
    const m=String(text).match(/\{[\s\S]*\}/);
    if(!m) return null;
    const o=JSON.parse(m[0]);
    const pc=parseInt(o.person_count,10);
    let nm=parseInt(o.no_mask,10);
    if(isNaN(pc)||isNaN(nm)) return null;
    const summary=String(o.summary||'');
    // 兜底修正：小模型 no_mask 数值常与 summary 自相矛盾（summary 说口鼻无遮挡，no_mask 却=0）
    // summary 明确说有人未戴/口鼻无口罩，且 no_mask=0 且有人，按 summary 修正（排除"未发现/都戴了"等否定表述）
    const neg=/未发现.{0,8}未|没有.{0,6}未|均.{0,6}佩戴|都.{0,6}佩戴|所有人.{0,6}佩戴|均已佩戴/.test(summary);
    const hasUnmasked=!neg&&pc>0&&nm===0&&/未戴|无口罩|未佩戴|口鼻处.{0,4}(无|未见|未).{0,5}口罩|口鼻.{0,4}(可见|暴露|无遮挡)|没.{0,4}口罩/.test(summary);
    if(hasUnmasked){
      let dn=1;
      const cn={'一':1,'二':2,'两':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9,'十':10};
      let v=null;
      const md=summary.match(/(\d+)\s*人.{0,6}未|未.{0,6}(\d+)\s*人|(\d+)\s*人未/);
      if(md){const n=parseInt(md[1]||md[2]||md[3],10);if(!isNaN(n))v=n;}
      if(v===null){
        const mc=summary.match(/([一二两三四五六七八九十])\s*人.{0,6}未|未.{0,6}([一二两三四五六七八九十])\s*人|([一二两三四五六七八九十])\s*人未/);
        if(mc){const k=mc[1]||mc[2]||mc[3];if(k&&cn[k]!==undefined)v=cn[k];}
      }
      if(v!==null&&v>=1)dn=Math.min(v,pc);
      nm=dn;
      serverLog('[巡检] 字段修正 no_mask 0→'+nm, 'summary='+summary);
    }
    return { person_count:pc, no_mask:nm, summary };
  }catch(_){ return null; }
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
function showAlertWin(e, blobUrl, result){
  const win=ensureAlertWin(e);
  win.querySelector('.cam-title').innerHTML='<span class="alert-dot"></span>⚠ '+e.label+' 巡检告警';
  win.querySelector('.alert-img').src=blobUrl;
  win.querySelector('.alert-body').innerHTML=
    '<div class="alert-row"><b>画面人数</b><span>'+result.person_count+'</span></div>'+
    '<div class="alert-row alert-bad"><b>未戴口罩</b><span>'+result.no_mask+'</span></div>'+
    (result.summary?'<div class="alert-summary">'+result.summary+'</div>':'')+
    '<div class="alert-time">'+new Date().toLocaleTimeString('zh-CN')+'</div>';
  if(!win.classList.contains('open')){
    const p=inspectAlertPos(e.order);
    win.style.left=p.left+'px'; win.style.right='auto'; win.style.top=p.top+'px';
    win.classList.add('open');
  }
  if(e.alertAutoHide)clearTimeout(e.alertAutoHide);
  e.alertAutoHide=setTimeout(()=>hideAlertWin(e),20000); // 20s 后自动消失
}
function hideAlertWin(e){
  if(!e||!e.alertWin) return;
  if(e.alertAutoHide){clearTimeout(e.alertAutoHide);e.alertAutoHide=null;}
  const w=e.alertWin; e.alertWin=null;
  if(e.lastUrl){URL.revokeObjectURL(e.lastUrl);e.lastUrl=null;}
  w.classList.remove('open');
  setTimeout(()=>{if(w.parentNode)w.remove();},320);
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
      slot.win.querySelector('.cam-head').appendChild(cd);
    }
  }else{
    slot.win.classList.remove('inspecting','alerting');
    const cd=slot.win.querySelector('.cam-countdown');
    if(cd)cd.remove();
  }
}

// 巡检一帧：截帧 → /vision(save=false) → 解析 → 状态机（全程 serverLog 调试）
async function inspectOnce(e){
  if(!e||e.inFlight) return;
  if(!e.slot||!e.slot.stream||!camSlots.includes(e.slot)){ serverLog('[巡检]',e.label,'摄像头已关闭，停止该路'); stopInspect(e.slot,true); return; }
  e.inFlight=true;
  serverLog('[巡检]',e.label,'开始截帧');
  try{
    const blob=await captureCamFrame(e.slot);
    if(!blob){ serverLog('[巡检]',e.label,'画面未就绪，跳过本轮'); return; }
    const b64=await blobToBase64(blob);
    const resp=await fetchWithTimeout('http://localhost:8770/vision',
      {method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({text:INSPECT_PROMPT, image:b64, source:'camera', save:false})},60000);
    const data=await resp.json();
    if(!data.text||data.error){ serverLog('[巡检]',e.label,'视觉返回错误:',data.error||'(空)'); return; }
    serverLog('[巡检]',e.label,'模型原始返回:',data.text);
    const result=parseInspectResult(data.text);
    if(!result){ serverLog('[巡检]',e.label,'JSON解析失败，跳过'); return; }
    const now=Date.now();
    serverLog('[巡检]',e.label,'解析: person_count='+result.person_count,'no_mask='+result.no_mask,'alerting='+e.alerting,'summary='+JSON.stringify(result.summary));
    if(result.no_mask>0){
      // 异常态：红脉冲 + 告警窗
      e.slot.win.classList.add('alerting');
      if(e.lastUrl)URL.revokeObjectURL(e.lastUrl);
      e.lastUrl=URL.createObjectURL(blob);
      const shouldAlert=!e.alerting||(now-e.lastAlertTs>=INSPECT_COOLDOWN);
      serverLog('[巡检]',e.label,'异常态 shouldAlert='+shouldAlert,'距上次='+(e.alerting?(now-e.lastAlertTs)+'ms':'首次'));
      if(shouldAlert){
        showAlertWin(e, e.lastUrl, result);
        inspectSpeak(e.label+'巡检告警：画面中'+result.person_count+'人，其中'+result.no_mask+'人未戴口罩。');
        // 存盘取证（轻量端点，不重复推理）
        fetchWithTimeout('http://localhost:8770/vision_save',
          {method:'POST',headers:{'Content-Type':'application/json'},
            body:JSON.stringify({image:b64, text:INSPECT_PROMPT, answer:data.text})},30000).catch(()=>{});
        e.alerting=true; e.lastAlertTs=now;
        // 记入历史（独立 URL，不随告警窗关闭 revoke）
        inspectHistory.push({label:e.label, url:URL.createObjectURL(blob), person_count:result.person_count, no_mask:result.no_mask, summary:result.summary, time:new Date()});
        if(inspectHistory.length>30){const old=inspectHistory.shift(); URL.revokeObjectURL(old.url);}
        _showHistoryBtn();
        serverLog('[巡检]',e.label,'已触发告警+播报+存盘');
      }else{
        // 冷却内：只更新告警窗截图，不播报
        if(e.alertWin)e.alertWin.querySelector('.alert-img').src=e.lastUrl;
        serverLog('[巡检]',e.label,'冷却内，仅更新截图');
      }
    }else{
      // 正常态：恢复不语音，仅视觉复位 + 静默关告警窗
      e.slot.win.classList.remove('alerting');
      hideAlertWin(e);
      e.alerting=false;
      serverLog('[巡检]',e.label,'正常态，复位');
    }
  }catch(err){
    serverLog('[巡检]',e.label,'异常:',err&&err.message?err.message:err);
  }finally{
    e.inFlight=false;
    // 仍在巡检态且未隐藏 → 30s 后调度下一帧（递归 setTimeout，与倒计时同步）
    if(inspectStates.has(e.deviceId) && !e.wasHidden) _scheduleNext(e);
  }
}

// 调度下一帧（递归 setTimeout：上一帧完成后才起 30s 倒计时，到 0 触发下一帧，与倒计时同步）
function _scheduleNext(e){
  e.secsLeft=Math.round(INSPECT_INTERVAL/1000);
  e.timer=setTimeout(()=>inspectOnce(e),INSPECT_INTERVAL);
}

// 倒计时：全局 1s 定时器遍历所有巡检路，递减 secsLeft 并更新标题右侧倒计时；巡检全停时自动清除
let _inspectCountdownTimer=null;
function _ensureCountdownTimer(){
  if(_inspectCountdownTimer)return;
  _inspectCountdownTimer=setInterval(()=>{
    inspectStates.forEach(e=>{
      if(e.inFlight){
        if(e.countdownEl)e.countdownEl.textContent='检测中';
      }else{
        if(e.secsLeft>0)e.secsLeft--;
        if(e.countdownEl)e.countdownEl.textContent=e.secsLeft+'s';
      }
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
    '<div class="ihp-body">'+(items.length?items.map(h=>
      '<div class="ihp-item"><img class="ihp-img" src="'+h.url+'">'+
      '<div class="ihp-meta"><b>'+h.label+'</b><span class="ihp-time">'+new Date(h.time).toLocaleString('zh-CN')+'</span>'+
      '<div class="ihp-nums">人数 '+h.person_count+' · 未戴 '+h.no_mask+'</div>'+
      '<div class="ihp-sum">'+(h.summary||'')+'</div></div></div>'
    ).join(''):'<div class="ihp-empty">暂无告警记录</div>')+'</div>';
  document.body.appendChild(panel);
  panel.querySelector('.ihp-close').addEventListener('click',()=>panel.remove());
  requestAnimationFrame(()=>panel.classList.add('show'));
}

// 启动单路巡检（内部，不播报；已在巡检则跳过返回 false）
function _startOne(slot){
  if(inspectStates.has(slot.deviceId)) return false;
  const label=(camSlots.indexOf(slot)+1)+'号摄像头';
  const order=inspectSeq++;
  const e={slot,label,deviceId:slot.deviceId,order,timer:null,inFlight:false,alerting:false,lastAlertTs:0,alertWin:null,lastUrl:null,wasHidden:false,countdownEl:null,secsLeft:Math.round(INSPECT_INTERVAL/1000),alertAutoHide:null};
  inspectStates.set(slot.deviceId,e);
  setInspectingClass(slot,true,order);
  e.countdownEl=slot.win.querySelector('.cam-countdown');
  _ensureCountdownTimer();
  _scheduleNext(e); // 30s 后跑第一帧（完成后递归调度下一帧）
  return true;
}

// 启动巡检：text 为空（"本地摄像头开始巡检"）→ 启动所有本地窗口；指明"N号" → 启动指定路
async function startInspect(text){
  const opens=camSlots.filter(s=>s.win.classList.contains('open')&&!(s.deviceId||'').startsWith('remote:'));
  if(opens.length===0){ await speakAnswer('请先打开本地摄像头再开始巡检。',true); return; }
  if(!text){
    let started=0;
    for(const s of opens){ if(_startOne(s)) started++; }
    if(started===0){ await speakAnswer('本地摄像头已在巡检中。',true); return; }
    await speakAnswer('已开始巡检全部本地摄像头，每'+(INSPECT_INTERVAL/1000)+'秒检测一次，发现未戴口罩将告警。',true);
    return;
  }
  let slot=null;
  const rs=resolveCameraSlot(text);
  if(rs&&!(rs.deviceId||'').startsWith('remote:'))slot=rs;
  if(!slot){ await speakAnswer('没找到'+text+'。',true); return; }
  if(inspectStates.has(slot.deviceId)){ await speakAnswer((camSlots.indexOf(slot)+1)+'号摄像头已在巡检中。',true); return; }
  _startOne(slot);
  await speakAnswer('已开始巡检'+(camSlots.indexOf(slot)+1)+'号摄像头，每'+(INSPECT_INTERVAL/1000)+'秒检测一次，发现未戴口罩将告警。',true);
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
  if(e.timer){clearTimeout(e.timer);e.timer=null;}
  if(e.slot&&e.slot.win)e.slot.win.classList.remove('inspecting','alerting');
  if(e.alertWin){const w=e.alertWin;w.classList.remove('open');setTimeout(()=>{if(w.parentNode)w.remove();},320);}
  if(e.lastUrl)URL.revokeObjectURL(e.lastUrl);
}

// 页面可见性：隐藏即全部暂停，可见恢复（仅仍在巡检态的路）
document.addEventListener('visibilitychange',()=>{
  if(inspectStates.size===0) return;
  if(document.hidden){
    inspectStates.forEach(e=>{ if(e.timer){clearTimeout(e.timer);e.timer=null;} e.wasHidden=true; });
  }else{
    inspectStates.forEach(e=>{
      if(e.wasHidden){
        e.wasHidden=false;
        inspectOnce(e); // 恢复立即跑一帧，完成后由 inspectOnce 递归调度
      }
    });
  }
});
