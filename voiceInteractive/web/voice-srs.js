// ===== SRS 流媒体接入：巡检态把 USB 摄像头 WHIP 推到 219 SRS，并从 SRS 回拉画面 =====
// 设计：平时摄像头窗口看本地 getUserMedia 流（零延迟）；开始巡检后才推流，
//   通知 8771 飞桨服务启动分析，窗口切到 SRS 回拉的 usbN_labeled 标注流
//   （8771 不可用时回退 usbN 原始流）；停止巡检停推、停分析并恢复本地画面。
// 依赖（先加载的脚本提供）：camSlots, waitForIceGathering（index-cam.js）；
//   inspectStates, stopInspect, serverLog（index-inspect.js / voice-utils.js）

const SRS_HOST = '10.0.20.219';
// 飞桨 PP-Human 分析服务（本机独立进程，8771）：拉 usbN 原始流分析，推 usbN_labeled 标注流
const PADDLE_API = 'http://localhost:8771';
// 信令走 8765 同源反向代理（/srs/* → 219:1985），规避 Chrome localhost→私网的 PNA 拦截；
// WebRTC 媒体面仍直连 SRS（UDP 8000，candidate 由 SRS 配置提供）。
const SRS_SIGNAL = (location.origin || `http://${location.host}`) + '/srs/rtc/v1';

// 按 camSlots 序号生成稳定流名：1号摄像头 -> usb1
function usbStreamName(slot){
  return 'usb' + (camSlots.indexOf(slot) + 1);
}

// WHIP 推流（SRS 私有信令 /rtc/v1/publish/，与现有播放信令 /rtc/v1/play/ 对称）
// 仅推视频轨：不推麦克风，避免巡检期间语音被带入媒体链路。
// 成功后写入 slot.srsName；失败抛错，由调用方决定回退。
async function startSrsPublish(slot){
  if(slot.pubPc) return slot.srsName;
  if(!slot.stream) throw new Error('本地摄像头流不存在');
  const conn = new RTCPeerConnection();
  slot.pubPc = conn;
  slot.stream.getVideoTracks().forEach(t => conn.addTrack(t, slot.stream));
  conn.onconnectionstatechange = () => {
    const st = conn.connectionState;
    if(st === 'failed' || st === 'disconnected'){
      serverLog('[SRS] 推流连接状态:', st, slot.srsName || '');
    }
  };
  const offer = await conn.createOffer();
  await conn.setLocalDescription(offer);
  await waitForIceGathering(conn, 2000);
  const name = usbStreamName(slot);
  const apiUrl = `${SRS_SIGNAL}/publish/`;
  let data;
  try{
    const resp = await fetch(apiUrl, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        // api 仅为 SRS 自身识别用（浏览器不会请求该地址），给真实地址避免服务端校验失败
        api: `http://${SRS_HOST}:1985/rtc/v1/publish/`,
        streamurl: `webrtc://${SRS_HOST}/live/${name}`,
        sdp: conn.localDescription.sdp
      })
    });
    data = await resp.json();
  }catch(err){
    throw new Error('SRS 推流信令请求失败: ' + (err && err.message ? err.message : err));
  }
  if(!data || data.code !== 0) throw new Error((data && data.message) || `SRS publish 错误 code=${data && data.code}`);
  await conn.setRemoteDescription({type: 'answer', sdp: data.sdp});
  slot.srsName = name;
  return name;
}

function stopSrsPublish(slot){
  if(!slot) return;
  if(slot.pubPc){
    try{ slot.pubPc.close(); }catch(_){}
    slot.pubPc = null;
  }
}

// SRS 回拉播放（信令走同源代理；结构与 index-cam.js 的 startWebRtcPlayback 对称）
async function startSrsPlayback(slot, name){
  const video = slot.win.querySelector('video');
  const fb = slot.win.querySelector('.cam-fallback');
  if(slot.pc){ try{ slot.pc.close(); }catch(_){} slot.pc = null; }
  const conn = new RTCPeerConnection();
  slot.pc = conn;
  // 仅接收视频：usbN 只有视频轨，声明 audio 会导致 SRS answer 的 m-line 顺序/数量不匹配
  conn.addTransceiver('video', {direction: 'recvonly'});
  conn.ontrack = (e) => {
    if(slot.pc !== conn) return;
    video.srcObject = e.streams[0];
    video.hidden = false; fb.hidden = true;
    video.play().catch(()=>{});
  };
  conn.onconnectionstatechange = () => {
    if(slot.pc !== conn) return;
    const st = conn.connectionState;
    if(st === 'failed' || st === 'disconnected'){ video.hidden = true; fb.hidden = false; fb.textContent = 'SRS画面已断开'; }
  };
  const offer = await conn.createOffer();
  await conn.setLocalDescription(offer);
  await waitForIceGathering(conn, 2000);
  const apiUrl = `${SRS_SIGNAL}/play/`;
  let data;
  try{
    const resp = await fetch(apiUrl, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        api: `http://${SRS_HOST}:1985/rtc/v1/play/`,
        streamurl: `webrtc://${SRS_HOST}/live/${name}`,
        sdp: conn.localDescription.sdp
      })
    });
    data = await resp.json();
  }catch(err){
    throw new Error('SRS 播放信令请求失败: ' + (err && err.message ? err.message : err));
  }
  if(!data || data.code !== 0) throw new Error((data && data.message) || `SRS play 错误 code=${data && data.code}`);
  await conn.setRemoteDescription({type: 'answer', sdp: data.sdp});
}

// 窗口画面切到 SRS 回拉流
async function switchCamToSrsView(slot, name){
  await startSrsPlayback(slot, name);
  slot.view = 'srs:' + name;
}

// 窗口画面恢复本地 getUserMedia 流（关闭 SRS 播放连接）
function switchCamToLocalView(slot){
  if(!slot) return;
  if(slot.pc){ try{ slot.pc.close(); }catch(_){} slot.pc = null; }
  const video = slot.win && slot.win.querySelector('video');
  const fb = slot.win && slot.win.querySelector('.cam-fallback');
  if(video && slot.stream){
    video.srcObject = slot.stream;
    video.hidden = false;
    if(fb) fb.hidden = true;
  }
  slot.view = 'local';
}

// ---- 巡检视角切换按钮：SRS 画面（默认巡检态）<-> 本地零延迟画面 ----
// labeled=true 时 SRS 侧为飞桨标注流（usbN_labeled），否则为原始流（usbN）
function addSrsViewBtn(slot, labeled){
  const actions = slot.win.querySelector('.cam-actions');
  if(!actions || slot.win.querySelector('.cam-srs-btn')) return;
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'cam-srs-btn';
  btn.textContent = '本地画面';
  btn.title = '巡检中：切回本地零延迟画面（SRS 推流与分析不受影响）';
  let onSrs = true;
  btn.addEventListener('click', async () => {
    btn.disabled = true;
    try{
      if(onSrs){
        switchCamToLocalView(slot);
        btn.textContent = 'SRS画面';
        btn.title = labeled ? '切回飞桨标注画面' : '切回 SRS 同源画面';
      }else{
        await switchCamToSrsView(slot, labeled ? slot.srsLabeledName : slot.srsName);
        btn.textContent = '本地画面';
        btn.title = '巡检中：切回本地零延迟画面（SRS 推流与分析不受影响）';
      }
      onSrs = !onSrs;
    }catch(err){
      serverLog('[SRS] 视角切换失败:', err && err.message ? err.message : err);
    }finally{
      btn.disabled = false;
    }
  });
  // 插到关闭按钮左侧
  const closeBtn = actions.querySelector('.cam-close');
  actions.insertBefore(btn, closeBtn);
}

function removeSrsViewBtn(slot){
  const btn = slot.win && slot.win.querySelector('.cam-srs-btn');
  if(btn) btn.remove();
}

// ---- 飞桨 8771 事件订阅（SSE）：摔倒/打架/状态事件交给 index-inspect.js 统一告警 ----
function openPaddleEvents(slot, name){
  closePaddleEvents(slot);
  let es;
  try{
    es = new EventSource(PADDLE_API + '/events?stream=' + encodeURIComponent(name));
  }catch(err){
    serverLog('[飞桨] SSE 订阅失败:', err && err.message ? err.message : err);
    return;
  }
  slot.paddleES = es;
  es.onmessage = (msg) => {
    let ev = null;
    try{ ev = JSON.parse(msg.data); }catch(_){ return; }
    if(typeof handlePaddleEvent === 'function') handlePaddleEvent(slot, ev);
  };
  // 断开时 EventSource 自动重连，无需手动处理；服务重启后会重新收到 status 事件
}
function closePaddleEvents(slot){
  if(slot && slot.paddleES){
    try{ slot.paddleES.close(); }catch(_){}
    slot.paddleES = null;
  }
}

// 通知 8771 启动/停止分析；start 失败返回 false（调用方回退原始流，不阻断巡检）
async function paddleAnalyzeStart(name){
  try{
    const resp = await fetchWithTimeout(PADDLE_API + '/analyze/start',
      {method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({stream:name})}, 10000);
    const data = await resp.json();
    return !!(data && data.ok);
  }catch(err){
    serverLog('[飞桨] 分析服务不可用，回退原始流:', err && err.message ? err.message : err);
    return false;
  }
}
function paddleAnalyzeStop(name){
  // best effort：不 await、不阻塞 UI 恢复；服务端异步停止 runner
  try{
    fetch(PADDLE_API + '/analyze/stop',
      {method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({stream:name})}).catch(()=>{});
  }catch(_){}
}

// ---- 巡检生命周期对接（由 index-inspect.js 的 _startOne / _stopOne 调用）----
// 开始：推流 -> 启动飞桨分析 -> 回拉标注流（8771 不可用/失败则回退原始流）-> SSE 订阅。
// 推流失败则自动停止该路巡检并恢复本地画面。
async function beginSrsInspect(slot){
  let name;
  try{
    serverLog('[SRS] 开始推流', usbStreamName(slot));
    name = await startSrsPublish(slot);
  }catch(err){
    serverLog('[SRS] 推流失败，停止该路巡检:', err && err.message ? err.message : err);
    stopSrsPublish(slot);
    if(typeof inspectStates !== 'undefined' && inspectStates.has(slot.deviceId)) stopInspect(slot, true);
    return;
  }
  // 推流是异步的：等待期间用户可能已停止巡检
  if(typeof inspectStates === 'undefined' || !inspectStates.has(slot.deviceId)){
    stopSrsPublish(slot);
    return;
  }
  slot.srsName = name;
  slot.srsLabeledName = name + '_labeled';
  // 启动飞桨分析（best effort）；标注流刚启动可能需要 1~2 秒才在 SRS 可拉
  const labeled = await paddleAnalyzeStart(name);
  try{
    if(labeled){
      // 标注流就绪有延迟：失败重试 2 次（每次间隔 2s），仍失败则回退原始流
      let ok = false, lastErr = null;
      for(let i = 0; i < 3 && inspectStates.has(slot.deviceId); i++){
        try{ await switchCamToSrsView(slot, slot.srsLabeledName); ok = true; break; }
        catch(err){ lastErr = err; await new Promise(r=>setTimeout(r,2000)); }
      }
      if(!ok) throw lastErr || new Error('标注流回拉失败');
      openPaddleEvents(slot, name);
      addSrsViewBtn(slot, true);
      serverLog('[SRS] 推流+飞桨分析成功，巡检画面已切到', slot.srsLabeledName);
    }else{
      await switchCamToSrsView(slot, name);
      addSrsViewBtn(slot, false);
      serverLog('[SRS] 推流成功（无飞桨标注），巡检画面为原始流', name);
    }
  }catch(err){
    serverLog('[SRS] 标注流回拉失败，尝试回退原始流:', err && err.message ? err.message : err);
    try{
      await switchCamToSrsView(slot, name);
      addSrsViewBtn(slot, false);
      serverLog('[SRS] 已回退原始流', name);
    }catch(err2){
      serverLog('[SRS] 原始流回拉也失败，停止该路巡检:', err2 && err2.message ? err2.message : err2);
      endSrsInspect(slot);
      if(inspectStates.has(slot.deviceId)) stopInspect(slot, true);
    }
  }
}

// 结束：关 SSE -> 通知飞桨停分析 -> 停推 -> 移除按钮 -> 恢复本地画面（幂等）
function endSrsInspect(slot){
  if(!slot) return;
  closePaddleEvents(slot);
  if(slot.srsName) paddleAnalyzeStop(slot.srsName);
  stopSrsPublish(slot);
  removeSrsViewBtn(slot);
  switchCamToLocalView(slot);
}
