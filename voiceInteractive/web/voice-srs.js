// ===== SRS 流媒体接入：巡检态把 USB 摄像头 WHIP 推到 219 SRS，并从 SRS 回拉画面 =====
// 设计：平时摄像头窗口看本地 getUserMedia 流（零延迟）；开始巡检后才推流，
//   通知 8771 飞桨服务启动分析，窗口切到 SRS 回拉的 usbN_labeled 标注流
//   （8771 不可用时回退 usbN 原始流）；停止巡检停推、停分析并恢复本地画面。
// 依赖（先加载的脚本提供）：camSlots, waitForIceGathering（index-cam.js）；
//   inspectStates, stopInspect, serverLog（index-inspect.js / voice-utils.js）

// SRS 媒体服务器地址：来自 8765 /app-config（config.json 的 srs_host），
// 与后端反向代理、飞桨 worker 拉推流共用同一个来源，换环境只改 config.json 一处。
let SRS_HOST = '10.0.20.219';
// 飞桨 PP-Human 分析服务（本机独立进程，8771）：拉 usbN 原始流分析，推 usbN_labeled 标注流
const PADDLE_API = 'http://localhost:8771';

// 口罩检测引擎（来自 8765 /app-config）：paddlehub=飞桨实时（默认）/ off=关闭。
// 配置接口不可达时按 paddlehub（不阻断巡检）；旧 qwen 60s 轮询链路已移除。
let MASK_ENGINE = 'paddlehub';
let _appConfigPromise = null;
function loadAppConfig(){
  if(_appConfigPromise) return _appConfigPromise;
  _appConfigPromise = fetch('app-config', {cache:'no-store'})
    .then(r => r.ok ? r.json() : {})
    .then(d => {
      MASK_ENGINE = (d && (d.mask_engine==='paddlehub'||d.mask_engine==='off')) ? d.mask_engine : 'paddlehub';
      if(d && typeof d.srs_host === 'string' && d.srs_host.trim()) SRS_HOST = d.srs_host.trim();
    })
    .catch(() => { MASK_ENGINE = 'paddlehub'; })
    .then(() => MASK_ENGINE);
  return _appConfigPromise;
}
loadAppConfig();

// 信令走 8765 同源反向代理（/srs/* → 219:1985），规避 Chrome localhost→私网的 PNA 拦截；
// WebRTC 媒体面仍直连 SRS（UDP 8000，candidate 由 SRS 配置提供）。
const SRS_SIGNAL = (location.origin || `http://${location.host}`) + '/srs/rtc/v1';

// 按摄像头窗口分配流名：1号摄像头 -> usb1
// 用 slot 上的自增序号而非 camSlots.indexOf：关闭中间窗口后 camSlots 会重排，
// 用索引会让新开的窗口拿到"正在被别的路推着"的流名（两路推同一流 -> 串台 +
// 8771 复用同一 runner -> 告警错路）。序号一经分配即固定，不随开关窗口回收。
let _usbSeq = 0;
function usbStreamName(slot){
  if(!slot) return 'usb1';
  if(!slot._usbIdx) slot._usbIdx = ++_usbSeq;
  return 'usb' + slot._usbIdx;
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
  // SRS 画面按真实方向显示（见 index.css 的 .no-mirror）：镜像只给本地自拍预览用
  if(video) video.classList.add('no-mirror');
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
  // 必须等到真的解出画面：SRS 对"尚不存在"的流也会返回 code=0，只看信令成功会在
  // 标注流还没注册时误判为切换成功，表现为"已切标注流"但画面全黑
  await _waitFirstFrame(video, 5000);
}

// 等待 video 真正拿到一帧（readyState>=2 且有宽高）；超时抛错由调用方决定是否重试
function _waitFirstFrame(video, ms){
  if(!video) return Promise.reject(new Error('video 元素不存在'));
  const done = () => video.videoWidth > 0 && video.readyState >= 2;
  if(done()) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const t0 = Date.now();
    let timer = null;
    const finish = (fn, arg) => {
      if(timer) clearTimeout(timer);
      video.removeEventListener('loadedmetadata', onLoaded);
      fn(arg);
    };
    const onLoaded = () => { if(done()) finish(resolve); };
    video.addEventListener('loadedmetadata', onLoaded);
    const tick = () => {
      if(done()) return finish(resolve);
      if(Date.now() - t0 > ms){
        return finish(reject, new Error('等待首帧超时（流已建立但无媒体）'));
      }
      timer = setTimeout(tick, 150);
    };
    tick();
  });
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
  if(video) video.classList.remove('no-mirror');  // 回到本地自拍预览，恢复镜像
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

// ---- 飞桨 8771 事件订阅（SSE）：摔倒/打架/口罩/区域入侵/状态事件交给 index-inspect.js 统一告警 ----
// 同时在 slot._paddleReady 上产出 Promise：worker 模型全部加载完（running:true 带 modules）
// 时 resolve；模型加载前异常退出则 reject（前端据此决定是否回退原始流）。
function openPaddleEvents(slot, name){
  closePaddleEvents(slot);
  slot._paddleReady = new Promise((resolve, reject) => { slot._paddleReadyResolve = resolve; slot._paddleReadyReject = reject; });
  let es;
  try{
    es = new EventSource(PADDLE_API + '/events?stream=' + encodeURIComponent(name));
  }catch(err){
    serverLog('[飞桨] SSE 订阅失败:', err && err.message ? err.message : err);
    slot._paddleReadyReject(err);
    return;
  }
  slot.paddleES = es;
  es.onmessage = (msg) => {
    let ev = null;
    try{ ev = JSON.parse(msg.data); }catch(_){ return; }
    if(ev && ev.type==='status'){
      // 只认 worker 就绪事件（带 modules）：8771 建连即发的合成 status 无 modules，
      // 用它 resolve 会让 PADDLE_READY_TIMEOUT 与"崩溃前 reject"双双失效
      if(ev.running===true && Array.isArray(ev.modules) && slot._paddleReadyResolve){
        const fn = slot._paddleReadyResolve;
        slot._paddleReadyResolve = null; slot._paddleReadyReject = null;
        fn(ev);
      }else if(ev.running===false && ev.error && slot._paddleReadyReject){
        const fn = slot._paddleReadyReject;
        slot._paddleReadyResolve = null; slot._paddleReadyReject = null;
        fn(new Error(ev.error));
      }
    }
    if(typeof handlePaddleEvent === 'function') handlePaddleEvent(slot, ev);
  };
  // 断开时 EventSource 自动重连，无需手动处理；服务重启后会重新收到 status 事件
}
function closePaddleEvents(slot){
  if(slot && slot.paddleES){
    try{ slot.paddleES.close(); }catch(_){}
    slot.paddleES = null;
  }
  if(slot){ slot._paddleReady = null; slot._paddleReadyResolve = null; slot._paddleReadyReject = null; }
}

// 通知 8771 启动/停止分析；start 失败返回 false（调用方回退原始流，不阻断巡检）
async function paddleAnalyzeStart(name, modules){
  try{
    const resp = await fetchWithTimeout(PADDLE_API + '/analyze/start',
      {method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({stream:name, modules:modules||['fall','fight','intrusion']})}, 10000);
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
// 开始：推流 -> 启动飞桨分析 -> 先回拉原始流（模型加载约 25s，不黑屏等待）
//   -> SSE 等 worker 就绪 -> 标注流注册后自动切到 usbN_labeled。
// 8771 不可用 / 就绪前崩溃 / 标注流始终拉不到：保持原始流（SSE 已断，无告警事件）。
// 推流失败则自动停止该路巡检并恢复本地画面。
const PADDLE_READY_TIMEOUT = 90000;  // 含 PP-Human 模型加载（冷启动约 25s）
const LABELED_RETRY_MS = 3000;
const LABELED_RETRY_TIMES = 20;      // 标注流最长等 60s（含 FLV 断线重连场景）

function _withTimeout(promise, ms, label){
  return Promise.race([
    promise,
    new Promise((_, reject) => setTimeout(() => reject(new Error(label || 'timeout')), ms))
  ]);
}

async function beginSrsInspect(slot){
  let name;
  try{
    await loadAppConfig();
    serverLog('[SRS] 开始推流', usbStreamName(slot), '口罩引擎='+MASK_ENGINE);
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
  // intrusion 由 worker 按 conf/regions.json 判定：该路没配禁区时会自动降级剔除
  const modules = ['fall','fight','intrusion'];
  if(MASK_ENGINE === 'paddlehub') modules.push('mask');
  const paddleOk = await paddleAnalyzeStart(name, modules);
  // 无论飞桨是否可用，先切 SRS 同源原始流（推流已成功）
  try{
    await switchCamToSrsView(slot, name);
  }catch(err){
    serverLog('[SRS] 原始流回拉失败，停止该路巡检:', err && err.message ? err.message : err);
    endSrsInspect(slot);
    if(inspectStates.has(slot.deviceId)) stopInspect(slot, true);
    return;
  }
  addSrsViewBtn(slot, false);
  if(!paddleOk){
    serverLog('[SRS] 推流成功（无飞桨标注），巡检画面为原始流', name);
    return;
  }
  openPaddleEvents(slot, name);
  // 等 worker 模型加载完成（status running:true 带 modules）。
  // 先取局部引用：等待期间若巡检被停止，closePaddleEvents 会把 slot._paddleReady 置 null，
  // 而 Promise.race([null, ...]) 会立刻 resolve(null) —— 后面读 readyEv.modules 将抛 TypeError
  const readyPromise = slot._paddleReady;
  let readyEv = null;
  try{
    readyEv = await _withTimeout(readyPromise, PADDLE_READY_TIMEOUT, 'ready timeout');
  }catch(err){
    serverLog('[SRS] 飞桨分析未就绪，保持原始流:', err && err.message ? err.message : err);
    return;
  }
  if(!readyEv || !inspectStates.has(slot.deviceId)) return;
  // 标注流在首批帧处理后才注册：轮询回拉，成功后替换视角按钮
  for(let i = 0; i < LABELED_RETRY_TIMES; i++){
    if(!inspectStates.has(slot.deviceId)) return;
    try{
      await switchCamToSrsView(slot, slot.srsLabeledName);
      removeSrsViewBtn(slot);
      addSrsViewBtn(slot, true);
      serverLog('[SRS] 巡检画面已切到飞桨标注流', slot.srsLabeledName,
        'modules=' + ((readyEv.modules || []).join(',')));
      return;
    }catch(err){
      await new Promise(r => setTimeout(r, LABELED_RETRY_MS));
    }
  }
  serverLog('[SRS] 标注流等待超时，保持原始流（事件告警仍可用）');
  // 标注画面没切成功也不影响 fall/fight/mask 的 SSE 告警
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
