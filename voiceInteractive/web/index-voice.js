// fetch 超时封装：后端卡住时不会让 UI 永久停在"正在思考/聆听"
function fetchWithTimeout(url, opts, ms=30000){
  const ctrl=new AbortController();
  const id=setTimeout(()=>ctrl.abort(),ms);
  return fetch(url,Object.assign({},opts,{signal:ctrl.signal})).finally(()=>clearTimeout(id));
}

// 前端日志转发到后端终端：console.log 同时 fire-and-forget POST 到 asr_server /log
function serverLog(...args){
  const msg=args.join(' ');
  console.log(msg);
  try{fetch('http://localhost:8770/log',{method:'POST',headers:{'Content-Type':'text/plain'},body:msg}).catch(()=>{});}catch(_){}
}

// 语音对话闭环（本地 Qwen3-ASR + Ollama + edge-tts）：
// 说话实时上屏 → 停顿 1.4s 自动发送 → 思考动画 → 回答打字机上屏 → 语音播报（球体随 AI 音量起伏）
const replyText=document.getElementById('reply-text');
const aiText=document.getElementById('ai-text');
const replyBox=document.querySelector('.reply');
// 文字越多字号越小：超过 60 字逐级缩小，避免长回答盖住球体
function fitReplyFont(){
  const len=(replyText.textContent+aiText.textContent).length;
  const base=Math.min(27,Math.max(18,innerWidth*.0225)); // 对应 CSS clamp(18px,2.25vw,27px)
  let scale=1;
  if(len>160)scale=.68;else if(len>110)scale=.78;else if(len>60)scale=.88;
  replyBox.style.fontSize=(base*scale).toFixed(1)+'px';
}
let mediaRecorder=null,recordedChunks=[],asrBusy=false,asrQueue=[],micDest=null;
let segSilenceStart=0,hasVoiceInSeg=false,segVoiceStart=0;
let typeTimer=null; // 打字机定时器（外层引用，便于结束会话时清除）
let conversationHistory=[],turnActive=false,lastVoiceAt=0,asrContext='';
function startRecognition(){
  replyText.textContent='';aiText.textContent='';
  replyBox.style.fontSize=''; // 恢复默认字号
  recordedChunks=[];segSilenceStart=0;hasVoiceInSeg=false;segVoiceStart=0;asrQueue=[];
  conversationHistory=[];turnActive=false;asrContext='';
  chatState='listening';
  preloadSendCue(); // 提前加载发送音效
  preloadDoneCue(); // 提前加载回答完毕音效
  startNewRecorder();
}
function startNewRecorder(){
  if(!micStream){mediaRecorder=null;return;}
  recordedChunks.length=0;segSilenceStart=0;hasVoiceInSeg=false;
  try{
    const rec=new MediaRecorder(micDest?micDest.stream:micStream);
    rec.ondataavailable=e=>{if(e.data.size>0)recordedChunks.push(e.data);};
    rec.onstop=async()=>{
      const chunks=recordedChunks.splice(0,recordedChunks.length);
      if(chunks.length>0){
        const blob=new Blob(chunks,{type:rec.mimeType||'audio/webm'});
        const rms=await segRMS(audioContext,blob);
        if(rms>=SEG_RMS_MIN){asrQueue.push(blob);processASRQueue();}
        else if(chatState==='listening')voiceStatus.textContent='✦ 声音太轻，没听清，请靠近再说'; // 段能量过低丢弃：如实反馈，避免"球跳了却没字"的误导
      }
      if(listening&&chatState==='listening')startNewRecorder();
    };
    rec.start();mediaRecorder=rec;
  }catch(_){mediaRecorder=null;}
}
let pendingCommand=null; // 待执行的语音指令（识别时设标志，停顿后随 autoSendChat 执行，与正常提问时序一致）
async function processASRQueue(){
  if(asrBusy||asrQueue.length===0)return;
  asrBusy=true;
  const blob=asrQueue.shift();
  let goodbye=false;
  try{
    // context=已识别前文，帮助分段边界断词连贯（qwen-asr 原生支持）
    const resp=await fetchWithTimeout('http://localhost:8770/transcribe?context='+encodeURIComponent(asrContext.slice(-200)),{method:'POST',body:blob,headers:{'Content-Type':blob.type||'audio/webm'}},30000);
    const data=await resp.json();
    if(data.text&&chatState==='listening'){ // 结束会话后 chatState='idle'，丢弃残留识别结果，不再写屏
      if(!turnActive){replyText.textContent='';aiText.textContent='';turnActive=true;} // 新一轮清空上一轮内容
      replyText.textContent+=data.text; // 追加到现有文字，光标跟随
      asrContext+=data.text;
      fitReplyFont();
      if(isGoodbye(data.text))goodbye=true; // "再见"照常上屏，稍后走告别流程（发送→AI告别→结束）
      else { const c=isVoiceCommand(replyText.textContent); if(c)pendingCommand=c; } // 语音指令：先上屏，停顿后随 autoSendChat 一起执行（与正常提问时序一致）
    }
    if(chatState==='listening'){
      if(data.error)voiceStatus.textContent='✦ 识别失败：'+data.error;
      else if(data.text)voiceStatus.textContent='✦ 聆听中 · 停顿后自动提问';
      else voiceStatus.textContent='✦ 没听清，请再说一遍'; // ASR 返回空：如实反馈，避免"球跳了却没字"的误导
    }
  }catch(error){
    if(chatState==='listening')voiceStatus.textContent='✦ 识别失败：'+(error.name==='AbortError'?'识别超时，请重说':error.message);
  }
  asrBusy=false; // 识别完成不重置发送计时：lastVoiceAt 只由说话声音更新，文字出现后很快自动发送
  if(goodbye){sendGoodbye();return;} // "再见"走告别流程：发送音效+动画 → AI 告别 → 结束会话
  if(asrQueue.length>0)processASRQueue();
}
// VAD 流水线（抗远场人声串扰）：触发门槛 + 连续确认才算真语音；
// level 经 (rms-.008)*14 放大，远场也被推高，故瞬时门槛区分力有限
// ⚠ 0.5 米 vs 1 米仅 2 倍距离、RMS 区分窗口极窄：门限卡高则误杀近场、卡低则漏远场，软件无法兼顾
// 故 SEG_RMS_MIN 保持保守值保近场识别，1 米外串扰请靠硬件（指向性麦）或调低 Windows 麦克风增益
const VAD_OPEN=.22, VAD_CONFIRM_MS=220;
// 段能量门限：原始 RMS 低于此值视为远场/噪声，丢弃不送识别（0.5↔1 米场景下勿再调高，会误杀近场）
const SEG_RMS_MIN=0.02;
async function segRMS(ctx,blob){
  try{
    const buf=await ctx.decodeAudioData(await blob.arrayBuffer());
    const d=buf.getChannelData(0);
    let s=0;const n=d.length;
    for(let i=0;i<n;i++)s+=d[i]*d[i];
    return Math.sqrt(s/n);
  }catch(_){return 1;} // 解码失败不丢弃，避免误伤
}
let voiceCandStart=0;
function checkVAD(level,t){
  if(chatState!=='listening')return;
  if(level>VAD_OPEN){
    if(!voiceCandStart)voiceCandStart=t;
    if(t-voiceCandStart>VAD_CONFIRM_MS){ // 持续确认：短促杂音被忽略
      hasVoiceInSeg=true;segSilenceStart=0;lastVoiceAt=t;
      if(!segVoiceStart)segVoiceStart=t;
    }
  } else {
    voiceCandStart=0;
    if(hasVoiceInSeg){
      if(!segSilenceStart)segSilenceStart=t;
      else if(t-segSilenceStart>300){segSilenceStart=0;hasVoiceInSeg=false;segVoiceStart=0;cutSegment();}
    }
  }
  // 滚动切分：一段连续说话超过 2.5s 就切走送识别，说话期间文字持续上屏
  if(hasVoiceInSeg&&segVoiceStart&&t-segVoiceStart>2500){segVoiceStart=0;segSilenceStart=0;hasVoiceInSeg=false;cutSegment();}
  if(turnActive&&t-lastVoiceAt>1400&&!asrBusy&&asrQueue.length===0)autoSendChat();
}
function cutSegment(){try{mediaRecorder&&mediaRecorder.state==='recording'&&mediaRecorder.stop();}catch(_){}}
// 状态提示音：Web Audio 振荡器合成，轻柔不喧宾夺主
function playCue(kind){
  if(!audioContext||audioContext.state==='closed')return;
  if(audioContext.state==='suspended')audioContext.resume();
  const now=audioContext.currentTime;
  const master=audioContext.createGain();master.gain.value=.14;master.connect(audioContext.destination);
  const note=(freq,start,dur,peak)=>{
    const osc=audioContext.createOscillator(),g=audioContext.createGain();
    osc.type='sine';osc.frequency.value=freq;
    g.gain.setValueAtTime(0,now+start);
    g.gain.linearRampToValueAtTime(peak,now+start+.05);
    g.gain.exponentialRampToValueAtTime(.001,now+start+dur);
    osc.connect(g);g.connect(master);
    osc.start(now+start);osc.stop(now+start+dur+.05);
  };
  if(kind==='think'){note(523.25,0,.5,.4);note(783.99,.14,.6,.3);}    // 思考：柔和上行双音
  else if(kind==='reply'){note(659.25,0,.7,.42);note(987.77,.1,.8,.22);} // 回答：温暖提示音
  else if(kind==='done'){note(392,0,.55,.28);note(523.25,.12,.6,.18);} // 完毕：低音轻点，提示可以继续说话
}
// 发送音效：预加载 send.mp3（比合成音更有质感），未就绪时退回合成音
let sendCueBuffer=null;
async function preloadSendCue(){
  if(sendCueBuffer||!audioContext||audioContext.state==='closed')return;
  try{
    const resp=await fetch('/send.mp3');
    if(!resp.ok)return;
    sendCueBuffer=await audioContext.decodeAudioData(await resp.arrayBuffer());
  }catch(_){sendCueBuffer=null;}
}
// 唤醒确认语"我在。"：页面加载时预取音频字节，唤醒时用主音频上下文即时解码播放
let ackArrayBuffer=null; // null=未取，false=取失败，ArrayBuffer=就绪
async function preloadAck(){
  if(ackArrayBuffer!==null)return;
  try{
    const resp=await fetchWithTimeout('http://localhost:8770/tts',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:'我在。'})},30000);
    ackArrayBuffer=resp.ok?await resp.arrayBuffer():false;
  }catch(_){ackArrayBuffer=false;}
}
function playSendCue(){
  if(!sendCueBuffer||!audioContext||audioContext.state==='closed'){playCue('think');return;}
  if(audioContext.state==='suspended')audioContext.resume();
  const src=audioContext.createBufferSource();src.buffer=sendCueBuffer;
  const g=audioContext.createGain();g.gain.value=.6;
  src.connect(g);g.connect(audioContext.destination);src.start();
}
// 回答完毕音效：预加载 done.mp3，未就绪时退回合成音
let doneCueBuffer=null;
async function preloadDoneCue(){
  if(doneCueBuffer||!audioContext||audioContext.state==='closed')return;
  try{
    const resp=await fetch('/done.mp3');
    if(!resp.ok)return;
    doneCueBuffer=await audioContext.decodeAudioData(await resp.arrayBuffer());
  }catch(_){doneCueBuffer=null;}
}
function playDoneCue(){
  if(!doneCueBuffer||!audioContext||audioContext.state==='closed'){playCue('done');return;}
  if(audioContext.state==='suspended')audioContext.resume();
  const src=audioContext.createBufferSource();src.buffer=doneCueBuffer;
  const g=audioContext.createGain();g.gain.value=.1; // 调小 50%（原 .6）
  src.connect(g);g.connect(audioContext.destination);src.start();
}

// blob 转 base64 字符串（去掉 data: 前缀）
function blobToBase64(blob){
  return new Promise((res,rej)=>{
    const r=new FileReader();
    r.onload=()=>{const s=String(r.result||'');res(s.includes(',')?s.split(',')[1]:s);};
    r.onerror=()=>rej(r.error);
    r.readAsDataURL(blob);
  });
}
let activeCamSlot=null; // 最近打开/操作的摄像头窗口，供"你看到了什么"等无指明视觉问句复用上下文
// 视觉问句判定：含描述意图；有"摄像头"按编号/人名，无则用 activeCamSlot 上下文
function isVisionQuestion(text){
  const s=String(text).replace(/[\s，,。.！!？?、~～]/g,'');
  if(!/(是什么|有什么|是啥|里有啥|里是啥|看到了什么|看到什么|看见什么|看见了什么|画面是什么|画面里|拍到了什么|拍到什么|描述一下|描述|里面有啥|里面有什么|看到了啥|看到啥)/.test(s))return null;
  if(/摄像头/.test(s)){
    const cn={'一':1,'二':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9};
    let idx=null,m=s.match(/([一二三四五六七八九]|\d+)\s*号摄像头/);
    if(!m)m=s.match(/摄像头\s*([一二三四五六七八九]|\d+)/);
    if(m){const raw=m[1];idx=cn[raw]!==undefined?cn[raw]:(/^\d+$/.test(raw)?parseInt(raw,10):null);}
    if(idx&&idx>=1)return {index:idx,fallback:false};
    const rm=s.match(/(.+?)的摄像头/);
    if(rm&&rm[1]&&!/^(本地|这个|那个|这些|那些|所有|全部)$/.test(rm[1])){
      return {remote:true,name:rm[1]};
    }
    return {index:1,fallback:true};
  }
  // 无"摄像头"但有描述意图 + 有活动摄像头上下文 → 描述刚打开的那个
  if(activeCamSlot)return {active:true};
  return null;
}
// 从"1号"/"张三"等指明文本定位已打开的摄像头窗口
function resolveCameraSlot(text){
  const s=String(text).replace(/[\s，,。.！!？?、~～]/g,'');
  const cn={'一':1,'二':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9};
  const m=s.match(/([一二三四五六七八九]|\d+)\s*号/);
  if(m){
    const raw=m[1];
    const idx=cn[raw]!==undefined?cn[raw]:(/^\d+$/.test(raw)?parseInt(raw,10):null);
    if(idx>=1&&idx<=camSlots.length){
      const sl=camSlots[idx-1];
      if(sl&&sl.win.classList.contains('open'))return sl;
    }
  }
  const rs=findRemoteSlot(s);
  if(rs&&rs.win.classList.contains('open'))return rs;
  return null;
}
let clarifyCam=false; // "打开摄像头"未指明本地/人名时，置位等下一句澄清
let clarifyVision=false,pendingVision=false; // 多摄像头追问"要看哪个"→指明后描述
function resetToListen(){
  recordedChunks=[];segSilenceStart=0;hasVoiceInSeg=false;segVoiceStart=0;asrQueue=[];asrContext='';
  chatState='listening';voiceStatus.textContent='✦ 聆听中 · 停顿后自动提问';
  startNewRecorder();
}
async function autoSendChat(){
  chatState='thinking';turnActive=false;playSendCue();spawnShockwave();
  asrContext=''; // 本轮已说完：清空识别上下文，避免跨轮累积导致 ASR 复述历史
  const question=replyText.textContent.trim();
  if(!question){chatState='listening';return;}
  if(mediaRecorder){const r=mediaRecorder;mediaRecorder=null;r.onstop=null;try{r.stop();}catch(_){}} // 暂停录音，防止播报被录入
  // 澄清态：上次"打开摄像头"未指明本地/人名，等这句回答
  if(clarifyCam){
    clarifyCam=false;
    const cs=String(question).replace(/[\s，,。.！!？?、~～]/g,'');
    if(/本地/.test(cs)){pendingCommand={type:'open_cam'};}
    else if(/(关闭|关掉|关了|不用|算了|不要)/.test(cs)){pendingCommand={type:'close_cam'};}
    else{const nm=String(question).replace(/[\s，,。.！!？?、~～]/g,'').replace(/^(?:本地|那个|这个|所有|全部)/,'');pendingCommand=nm?{type:'open_remote_cam',name:nm}:{type:'open_cam_ambiguous'};}
  }
  // 视觉澄清态：上次多摄像头追问"要看哪个"，等这句指明
  if(clarifyVision){
    clarifyVision=false;
    const slot=resolveCameraSlot(question);
    if(slot){activeCamSlot=slot;pendingVision=true;}
    else{await speakAnswer('没找到这个摄像头，请重新说要看哪个。',true);if(!listening){chatState='idle';return;}resetToListen();return;}
  }
  // 语音指令（打开/关闭摄像头）：说完停顿后随发送流程一起执行，共享发送音效+冲击波，跳过 Ollama
  if(pendingCommand){
    const cmd=pendingCommand;pendingCommand=null;
    if(cmd.type==='close_cam'){
      await speakAnswer('好的，摄像头已关闭。',true);
      if(!listening){chatState='idle';return;}
      resetToListen();closeAllCameras();return;
    }
    if(cmd.type==='open_cam'){
      await speakAnswer('好的，本地摄像头已打开。',true);
      if(!listening){chatState='idle';return;}
      resetToListen();openAllCameras();return;
    }
    if(cmd.type==='open_cam_ambiguous'){
      clarifyCam=true;
      await speakAnswer('本地的还是谁的？',true);
      if(!listening){chatState='idle';return;}
      resetToListen();return;
    }
    // 远端：按用户名查名单 → 多匹配多窗口 WHEP
    voiceStatus.textContent=`✦ 正在查找${cmd.name}的摄像头…`;
    try{
      const resp=await fetchWithTimeout('http://localhost:8770/cameras?q='+encodeURIComponent(cmd.name),{},15000);
      const data=await resp.json();
      const items=data.items||[];
      if(items.length===0){
        await speakAnswer(`没找到${cmd.name}的摄像头，请确认用户名或设备是否在线。`,true);
        if(!listening){chatState='idle';return;}resetToListen();return;
      }
      const names=items.map(i=>i.user_name||i.account||i.stream_name).filter(Boolean).join('、');
      await speakAnswer(`好的，已打开${names}的摄像头。`,true);
      if(!listening){chatState='idle';return;}resetToListen();
      openRemoteCameras(items);
      return;
    }catch(err){
      await speakAnswer(`查找${cmd.name}的摄像头失败，请稍后再试。`,true);
      if(!listening){chatState='idle';return;}resetToListen();return;
    }
  }
  // 视觉澄清后执行：用指明的活动摄像头描述
  if(pendingVision){
    pendingVision=false;
    if(!activeCamSlot){await speakAnswer('当前没有打开的摄像头。',true);if(!listening){chatState='idle';return;}resetToListen();return;}
    const pblob=await captureCamFrame(activeCamSlot);
    if(!pblob){await speakAnswer('摄像头画面还没准备好，稍等一下再问。',true);if(!listening){chatState='idle';return;}resetToListen();return;}
    const psrc=activeCamSlot.deviceId&&activeCamSlot.deviceId.startsWith('remote:')?'desktop':'camera';
    voiceStatus.textContent='✦ 正在分析摄像头画面…';
    try{
      const b64=await blobToBase64(pblob);
      const resp=await fetchWithTimeout('http://localhost:8770/vision',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:question,image:b64,source:psrc})},60000);
      const data=await resp.json();
      if(chatState!=='thinking')return;
      if(!data.text||data.error)throw new Error(data.error||'视觉模型没有返回描述');
      const ans=data.text.length>2000?data.text.slice(0,2000)+'…':data.text;
      conversationHistory.push({role:'user',content:question});
      conversationHistory.push({role:'assistant',content:ans});
      if(conversationHistory.length>12)conversationHistory.splice(0,conversationHistory.length-12);
      await speakAnswer(data.text);
    }catch(error){
      if(chatState==='idle')return;
      voiceStatus.textContent='✦ 画面分析失败：'+(error.name==='AbortError'?'视觉模型响应超时':error.message);
      chatState='listening';turnActive=false;asrContext='';startNewRecorder();
    }
    return;
  }
  // 视觉问句："X号摄像头里是什么" → 截取该窗口当前帧送后端视觉模型描述
  const vq=isVisionQuestion(question);
  if(vq){
    if(vq.remote){
      // 远端画面描述：截取已打开的远端窗口当前帧 → 本地视觉模型（与本地统一，不依赖 go-proxy）
      const rslot=findRemoteSlot(vq.name);
      if(!rslot){
        await speakAnswer(`没有打开${vq.name}的摄像头，请先说打开${vq.name}的摄像头。`,true);
        if(!listening){chatState='idle';return;}resetToListen();return;
      }
      const rblob=await captureCamFrame(rslot);
      if(!rblob){
        await speakAnswer(`${vq.name}的摄像头画面还没准备好，稍等一下再问。`,true);
        if(!listening){chatState='idle';return;}resetToListen();return;
      }
      voiceStatus.textContent=`✦ 正在分析${vq.name}的摄像头画面…`;
      try{
        const b64=await blobToBase64(rblob);
        const resp=await fetchWithTimeout('http://localhost:8770/vision',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:question,image:b64,source:'desktop'})},60000);
        const data=await resp.json();
        if(chatState!=='thinking')return;
        if(!data.text||data.error)throw new Error(data.error||'视觉模型没有返回描述');
        const ans=data.text.length>2000?data.text.slice(0,2000)+'…':data.text;
        conversationHistory.push({role:'user',content:question});
        conversationHistory.push({role:'assistant',content:ans});
        if(conversationHistory.length>12)conversationHistory.splice(0,conversationHistory.length-12);
        await speakAnswer(data.text);
      }catch(error){
        if(chatState==='idle')return;
        voiceStatus.textContent='✦ 画面分析失败：'+(error.name==='AbortError'?'视觉模型响应超时':error.message);
        chatState='listening';turnActive=false;asrContext='';startNewRecorder();
      }
      return;
    }
    if(vq.active){
      // 无指明的视觉问句（"你看到了什么"）：复用刚打开的活动摄像头
      const opens=camSlots.filter(s=>s.win.classList.contains('open'));
      if(opens.length===0){await speakAnswer('当前没有打开的摄像头。',true);if(!listening){chatState='idle';return;}resetToListen();return;}
      if(opens.length>1){
        clarifyVision=true;
        const labels=opens.map(s=>{
          if(s.deviceId&&s.deviceId.startsWith('remote:'))return s.userName||'远端';
          return (camSlots.indexOf(s)+1)+'号';
        }).join('还是');
        await speakAnswer(`要看哪个？${labels}？`,true);
        if(!listening){chatState='idle';return;}resetToListen();return;
      }
      activeCamSlot=opens[0];
      const ablob=await captureCamFrame(activeCamSlot);
      if(!ablob){await speakAnswer('摄像头画面还没准备好，稍等一下再问。',true);if(!listening){chatState='idle';return;}resetToListen();return;}
      const asrc=activeCamSlot.deviceId&&activeCamSlot.deviceId.startsWith('remote:')?'desktop':'camera';
      voiceStatus.textContent='✦ 正在分析摄像头画面…';
      try{
        const b64=await blobToBase64(ablob);
        const resp=await fetchWithTimeout('http://localhost:8770/vision',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:question,image:b64,source:asrc})},60000);
        const data=await resp.json();
        if(chatState!=='thinking')return;
        if(!data.text||data.error)throw new Error(data.error||'视觉模型没有返回描述');
        const ans=data.text.length>2000?data.text.slice(0,2000)+'…':data.text;
        conversationHistory.push({role:'user',content:question});
        conversationHistory.push({role:'assistant',content:ans});
        if(conversationHistory.length>12)conversationHistory.splice(0,conversationHistory.length-12);
        await speakAnswer(data.text);
      }catch(error){
        if(chatState==='idle')return;
        voiceStatus.textContent='✦ 画面分析失败：'+(error.name==='AbortError'?'视觉模型响应超时':error.message);
        chatState='listening';turnActive=false;asrContext='';startNewRecorder();
      }
      return;
    }
    const slot=camSlots[vq.index-1];
    if(!slot||!slot.stream){
      const msg=vq.fallback?'当前没有打开的摄像头，请先说"打开摄像头"。':`没有找到${vq.index}号摄像头，请先打开它。`;
      await speakAnswer(msg,true);
      if(!listening){chatState='idle';return;}
      recordedChunks=[];segSilenceStart=0;hasVoiceInSeg=false;segVoiceStart=0;asrQueue=[];
      chatState='listening';voiceStatus.textContent='✦ 聆听中 · 停顿后自动提问';
      startNewRecorder();
      return;
    }
    const blob=await captureCamFrame(slot);
    if(!blob){
      await speakAnswer(`${vq.index}号摄像头画面还没准备好，稍等一下再问。`,true);
      if(!listening){chatState='idle';return;}
      recordedChunks=[];segSilenceStart=0;hasVoiceInSeg=false;segVoiceStart=0;asrQueue=[];
      chatState='listening';voiceStatus.textContent='✦ 聆听中 · 停顿后自动提问';
      startNewRecorder();
      return;
    }
    voiceStatus.textContent=`✦ 正在分析${vq.index}号摄像头画面…`;
    try{
      const b64=await blobToBase64(blob);
      const resp=await fetchWithTimeout('http://localhost:8770/vision',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:question,image:b64,source:'camera'})},60000);
      const data=await resp.json();
      if(chatState!=='thinking')return; // 会话已结束，丢弃迟到的回答
      if(!data.text||data.error)throw new Error(data.error||'视觉模型没有返回描述');
      const ans=data.text.length>2000?data.text.slice(0,2000)+'…':data.text;
      conversationHistory.push({role:'user',content:question});
      conversationHistory.push({role:'assistant',content:ans});
      if(conversationHistory.length>12)conversationHistory.splice(0,conversationHistory.length-12);
      await speakAnswer(data.text);
    }catch(error){
      if(chatState==='idle')return; // 已结束会话
      voiceStatus.textContent='✦ 画面分析失败：'+(error.name==='AbortError'?'视觉模型响应超时':error.message);
      chatState='listening';turnActive=false;asrContext='';startNewRecorder();
    }
    return;
  }
  voiceStatus.textContent='✦ 正在思考…';
  conversationHistory.push({role:'user',content:question});
  if(conversationHistory.length>12)conversationHistory.splice(0,conversationHistory.length-12);
  asrContext=''; // 新一轮提问的识别上下文独立
  try{
    const resp=await fetchWithTimeout('http://localhost:8770/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({messages:conversationHistory})},60000);
    const data=await resp.json();
    if(chatState!=='thinking')return; // 会话已结束，丢弃迟到的回答，避免污染已重置的界面
    if(!data.text||data.error)throw new Error(data.error||'模型没有返回回答');
    // 限制历史单条内容长度，避免超长回复撑大后续上下文
    const ans=data.text.length>2000?data.text.slice(0,2000)+'…':data.text;
    conversationHistory.push({role:'assistant',content:ans});
    await speakAnswer(data.text);
  }catch(error){
    if(chatState==='idle')return; // 已结束会话：不恢复、不提示，保持待机态
    voiceStatus.textContent='✦ 回答失败：'+(error.name==='AbortError'?'模型响应超时':error.message);
    chatState='listening';turnActive=false;asrContext='';startNewRecorder();
  }
}
async function speakAnswer(text, endAfter){
  chatState='speaking';voiceStatus.textContent='✦ 正在回答…';
  let buffer=null;
  try{ // 获取语音（失败则纯文字展示）
    const resp=await fetchWithTimeout('http://localhost:8770/tts',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text})},30000);
    if(resp.ok){
      const arr=await resp.arrayBuffer();
      if(audioContext&&audioContext.state!=='closed')buffer=await audioContext.decodeAudioData(arr);
    }
  }catch(_){buffer=null;}
  const duration=buffer?buffer.duration:Math.max(2,text.length/4);
  const start=performance.now();
  typeTimer=setInterval(()=>{ // 打字机与语音同步
    const p=Math.min(1,(performance.now()-start)/(duration*1000));
    aiText.textContent=text.slice(0,Math.round(text.length*p));
    fitReplyFont();
  },80);
  try{
    if(buffer&&audioContext&&audioContext.state!=='closed'){
      if(audioContext.state==='suspended')await audioContext.resume();
      playCue('reply');
      ttsAnalyser=audioContext.createAnalyser();ttsAnalyser.fftSize=1024;ttsAnalyser.smoothingTimeConstant=.78;
      ttsSource=audioContext.createBufferSource();ttsSource.buffer=buffer;
      ttsSource.connect(ttsAnalyser);ttsAnalyser.connect(audioContext.destination);
      ttsLive=true; // TTS 开始播报：思考动画退场，球体改由 AI 音频驱动
      try{
        await new Promise((res,rej)=>{ttsSource.onended=res;ttsSource.onerror=()=>rej(new Error('tts play failed'));ttsSource.start();});
      }catch(_){ /* TTS 播放出错：文字已由打字机显示，按纯文字完成，不阻断对话 */ }
      ttsSource=null;ttsAnalyser=null;ttsLive=false;
    }else{
      ttsLive=true; // 纯文字模式：打字开始即视为"回答中"，停止思考动画
      await new Promise(res=>setTimeout(res,duration*1000));
      ttsLive=false;
    }
  }finally{
    clearInterval(typeTimer);typeTimer=null;
    if(listening)aiText.textContent=text; // 会话已结束则不写屏（resetUI 会清理），避免异步覆盖清空结果
  }
  if(!listening){chatState='idle';return;}
  if(endAfter)return; // 告别场景：播完即返回，由调用方结束会话（不回聆听、不重启录音）
  playDoneCue();
  voiceStatus.textContent='✦ 回答完毕 · 继续聆听';
  chatState='listening';startNewRecorder();
}
// 唤醒确认语：唤醒后先回一句（如"我在。"），球体随声起伏，播完进入聆听
async function speakWakeAck(text){
  chatState='speaking';voiceStatus.textContent='✦  我在';
  replyText.textContent='';aiText.textContent=text;fitReplyFont();
  // 优先用预取的音频字节解码（即时），否则现取现解码
  let buffer=null;
  if(ackArrayBuffer&&audioContext&&audioContext.state!=='closed'){
    try{buffer=await audioContext.decodeAudioData(ackArrayBuffer.slice(0));}catch(_){buffer=null;}
  }
  if(!buffer){
    try{
      const resp=await fetchWithTimeout('http://localhost:8770/tts',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text})},30000);
      if(resp.ok){const arr=await resp.arrayBuffer();if(audioContext&&audioContext.state!=='closed')buffer=await audioContext.decodeAudioData(arr);}
    }catch(_){buffer=null;}
  }
  if(buffer&&audioContext&&audioContext.state!=='closed'){
    if(audioContext.state==='suspended')await audioContext.resume();
    ttsAnalyser=audioContext.createAnalyser();ttsAnalyser.fftSize=1024;ttsAnalyser.smoothingTimeConstant=.78;
    ttsSource=audioContext.createBufferSource();ttsSource.buffer=buffer;
    ttsSource.connect(ttsAnalyser);ttsAnalyser.connect(audioContext.destination);
    ttsLive=true; // 球体改由确认语音频驱动
    try{await new Promise((res,rej)=>{ttsSource.onended=res;ttsSource.onerror=()=>rej(new Error('ack play failed'));ttsSource.start();});}catch(_){}
    ttsSource=null;ttsAnalyser=null;ttsLive=false;
  }else{
    ttsLive=true;await new Promise(res=>setTimeout(res,800));ttsLive=false; // 无音频：短暂停顿后继续
  }
  if(!listening){chatState='idle';return;} // 期间点了结束会话
  // 进入聆听：重置会话状态，保留"我在。"文字直到用户开口（首条 ASR 结果会清空它）
  recordedChunks=[];segSilenceStart=0;hasVoiceInSeg=false;segVoiceStart=0;asrQueue=[];
  conversationHistory=[];turnActive=false;asrContext='';
  chatState='listening';voiceStatus.textContent='✦  请说';
  preloadSendCue();preloadDoneCue();startNewRecorder();
}
async function stopRecognition(){
  if(!mediaRecorder)return;
  const rec=mediaRecorder;mediaRecorder=null;
  await new Promise(resolve=>{
    rec.onstop=()=>{
      const chunks=recordedChunks.splice(0,recordedChunks.length);
      if(chunks.length>0){
        const blob=new Blob(chunks,{type:rec.mimeType||'audio/webm'});
        asrQueue.push(blob);processASRQueue();
      }
      resolve();
    };
    try{rec.stop();}catch(_){resolve();}
  });
}

// ===== 唤醒词"叮咚叮咚"：idle 态持续监听麦克风，命中后自动进入会话 =====
let wakeStream=null,wakeCtx=null,wakeAnalyser=null,wakeData=null,wakeDest=null;
let wakeRecorder=null,wakeChunks=[],wakeAsrBusy=false,wakeQueue=[];
let wakeListening=false,wakeCandStart=0,wakeSilence=0,wakeVoice=false,wakeVoiceStart=0;
// 唤醒词判定：清理标点空白后，"叮咚/丁冬/丁东"出现 ≥2 次即命中（兼容同音异字）
function isWakeWord(text){
  const clean=String(text).replace(/[\s，,。.！!？?、~～]/g,'');
  return (clean.match(/叮咚|丁冬|丁东/g)||[]).length>=2;
}
// 退出词"再见"判定：清理标点空白后包含"再见"即命中（兼容"再见"/"好的再见"/"再见啦"等）
function isGoodbye(text){
  return /再见/.test(String(text).replace(/[\s，,。.！!？?、~～]/g,''));
}
async function startWakeWord(){
  if(wakeListening||listening)return;
  try{
    wakeStream=await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true,autoGainControl:true,channelCount:1}});
    wakeCtx=new AudioContext();
    if(wakeCtx.state==='suspended')await wakeCtx.resume();
    const src=wakeCtx.createMediaStreamSource(wakeStream);
    const hp=wakeCtx.createBiquadFilter();hp.type='highpass';hp.frequency.value=150;hp.Q.value=.7;
    wakeAnalyser=wakeCtx.createAnalyser();wakeAnalyser.fftSize=1024;wakeAnalyser.smoothingTimeConstant=.78;
    src.connect(hp);hp.connect(wakeAnalyser);
    wakeDest=wakeCtx.createMediaStreamDestination();hp.connect(wakeDest); // 录音流也走高通，识别音频享到降噪
    wakeData=new Uint8Array(wakeAnalyser.fftSize);
    wakeListening=true;
    if(chatState==='idle'){voiceStatus.textContent='✦  说出"叮咚叮咚"唤醒我';micCaption.textContent='等待唤醒';}
    startWakeRecorder();
  }catch(_){
    wakeListening=false;
    voiceStatus.textContent='✦  唤醒需要麦克风权限，也可点击"开始会话"';
  }
}
function startWakeRecorder(){
  if(!wakeStream||!wakeListening){wakeRecorder=null;return;}
  wakeChunks.length=0;
  try{
    const rec=new MediaRecorder(wakeDest?wakeDest.stream:wakeStream);
    rec.ondataavailable=e=>{if(e.data.size>0)wakeChunks.push(e.data);};
    rec.onstop=async()=>{
      if(!wakeListening){wakeChunks.length=0;return;} // 已停止，丢弃残留
      const chunks=wakeChunks.splice(0,wakeChunks.length);
      if(chunks.length>0){
        const blob=new Blob(chunks,{type:rec.mimeType||'audio/webm'});
        const rms=await segRMS(wakeCtx,blob);
        serverLog('[wake] rms=',rms.toFixed(3),'min=',SEG_RMS_MIN,'pass=',rms>=SEG_RMS_MIN);
        if(rms>=SEG_RMS_MIN){wakeQueue.push(blob);processWakeASR();}
      }
      startWakeRecorder(); // 连续分段录制
    };
    rec.start();wakeRecorder=rec;
  }catch(_){wakeRecorder=null;}
}
function cutWakeSegment(){try{wakeRecorder&&wakeRecorder.state==='recording'&&wakeRecorder.stop();}catch(_){}}
async function processWakeASR(){
  if(wakeAsrBusy||wakeQueue.length===0||!wakeListening)return;
  wakeAsrBusy=true;
  const blob=wakeQueue.shift();
  try{
    const resp=await fetchWithTimeout('http://localhost:8770/transcribe',{method:'POST',body:blob,headers:{'Content-Type':blob.type||'audio/webm'}},30000);
    const data=await resp.json();
    serverLog('[wake] asr=',JSON.stringify(data),'isWake=',data.text?isWakeWord(data.text):false);
    if(data.text&&isWakeWord(data.text)){
      wakeListening=false; // 立即停掉监听，防止重复触发
      voiceStatus.textContent='✦  唤醒成功 · 进入会话';
      startMicrophone('我在。'); // 内部会 await stopWakeWord，并先回"我在。"再进入聆听
    }
  }catch(_){}
  wakeAsrBusy=false;
  if(wakeQueue.length>0&&wakeListening)processWakeASR();
}
// 唤醒 VAD：复用对话 VAD 的门槛与确认机制，切段送 ASR
function checkWakeVAD(level,t){
  if(!wakeListening)return;
  if(level>VAD_OPEN){
    if(!wakeCandStart)wakeCandStart=t;
    if(t-wakeCandStart>VAD_CONFIRM_MS){wakeVoice=true;wakeSilence=0;wakeVoiceStart=wakeVoiceStart||t;}
  }else{
    wakeCandStart=0;
    if(wakeVoice){
      if(!wakeSilence)wakeSilence=t;
      else if(t-wakeSilence>300){wakeSilence=0;wakeVoice=false;wakeVoiceStart=0;cutWakeSegment();}
    }
  }
  if(wakeVoice&&wakeVoiceStart&&t-wakeVoiceStart>2500){wakeVoiceStart=0;wakeSilence=0;wakeVoice=false;cutWakeSegment();}
}
async function stopWakeWord(){
  wakeListening=false;
  try{wakeRecorder&&wakeRecorder.state==='recording'&&wakeRecorder.stop();}catch(_){}
  wakeRecorder=null;
  wakeStream?.getTracks().forEach(track=>track.stop());wakeStream=null;
  await wakeCtx?.close();wakeCtx=null;wakeAnalyser=null;wakeData=null;wakeDest=null;
  wakeQueue.length=0;wakeAsrBusy=false;
  wakeCandStart=0;wakeSilence=0;wakeVoice=false;wakeVoiceStart=0;
  micCaption.textContent='开始会话';
}

async function startMicrophone(ack) {
  if (listening) return;
  try {
    stopGreeting(); // 欢迎语音还在播时先停掉，避免被麦克风录进去
    await stopWakeWord(); // 停掉唤醒词监听，释放其麦克风资源
    micStream=await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true,autoGainControl:true,channelCount:1}});
    audioContext=new AudioContext();
    if(audioContext.state==='suspended') await audioContext.resume(); // 浏览器自动播放策略可能挂起上下文
    analyser=audioContext.createAnalyser();analyser.fftSize=1024;analyser.smoothingTimeConstant=.78;
    // 高通滤波器：切除 150Hz 以下的低频（远处人声经距离衰减后只剩低频闷声，近处说话不受影响）
    const highpass=audioContext.createBiquadFilter();highpass.type='highpass';highpass.frequency.value=150;highpass.Q.value=.7;
    audioContext.createMediaStreamSource(micStream).connect(highpass);highpass.connect(analyser);
    micDest=audioContext.createMediaStreamDestination();highpass.connect(micDest); // 录音流也走高通，识别音频享到降噪
    audioData=new Uint8Array(analyser.fftSize);listening=true;
    micCaption.textContent='麦克风监听中';micButton.setAttribute('aria-label','麦克风输入中');
    if(ack){
      await speakWakeAck(ack); // 唤醒确认：先回一句"我在。"再进入聆听
    }else{
      voiceStatus.textContent='✦  正在聆听 · 说出的话将实时呈现';
      startRecognition();
    }
  } catch(error) {
    const message=error.name==='NotAllowedError'?'请在浏览器地址栏允许麦克风权限后重试':error.name==='NotFoundError'?'未检测到可用麦克风':'麦克风暂不可用，请检查浏览器权限或设备';
    voiceStatus.textContent='✦  '+message;
    micCaption.textContent='点击重试';
  }
}

async function stopMicrophone() {
  listening=false;targetLevel=0;chatState='idle';
  try{ttsSource&&ttsSource.stop();}catch(_){} // 停止 AI 播报（会触发 speakAnswer 的 onended → clearInterval）
  if(typeTimer){clearInterval(typeTimer);typeTimer=null;} // 兜底：确保打字机停止
  await stopRecognition(); // 先停止录音并识别（需在 micStream 仍存活时完成数据收集）
  micStream?.getTracks().forEach(track=>track.stop());micStream=null;
  await audioContext?.close();audioContext=null;analyser=null;audioData=null;micDest=null;
  ttsAnalyser=null;ttsSource=null;ttsLive=false;
  asrQueue.length=0;asrBusy=false; // 丢弃队列里待识别的音频，防止结束后还有文字写回
  micCaption.textContent='开始会话';micButton.setAttribute('aria-label','开启麦克风');
}
// 结束会话：界面恢复到初始状态（文字、字号、状态）
function resetUI(){
  if(typeTimer){clearInterval(typeTimer);typeTimer=null;}
  replyText.innerHTML='夜色已经降临，所有的设备都准备好了。<br>你想先从什么开始？';
  aiText.textContent='';
  replyBox.style.fontSize='';
  voiceStatus.textContent='✦ 待机中';
  conversationHistory=[];asrContext='';turnActive=false;pendingCommand=null;
  shockwaves.length=0;
}

micButton.addEventListener('click',()=>startWakeWord()); // 点"开始会话"进入唤醒监听，说"叮咚叮咚"后自动进入会话
// 语音"再见"告别流程：发送音效+冲击波 → 思考动画 → AI 回答固定告别语 → 结束会话
async function sendGoodbye(){
  chatState='thinking';turnActive=false;
  playSendCue();spawnShockwave(); // 与正常提问一致的发送音效与冲击波动画
  if(mediaRecorder){const r=mediaRecorder;mediaRecorder=null;r.onstop=null;try{r.stop();}catch(_){}} // 暂停录音，防止 AI 告别语被录入
  voiceStatus.textContent='✦ 正在思考…';
  try{
    await speakAnswer('好的，下次见，有需要随时喊我！',true); // 告别语：播完即返回，不回聆听
  }catch(_){}
  await endByVoice(); // AI 告别播完后结束会话、恢复界面、回到唤醒监听
}
// 语音"再见"结束会话：等同点"结束会话"按钮（停录音识别→恢复界面→回唤醒监听）
async function endByVoice(){
  if(!listening)return;
  voiceStatus.textContent='✦  再见 · 结束会话中…';
  await stopMicrophone();
  resetUI();
  startWakeWord(); // 结束后回到唤醒词监听，可再次"叮咚叮咚"唤醒
}

document.getElementById('end-button').addEventListener('click',async()=>{
  if (!listening) return;
  voiceStatus.textContent='✦ 正在结束并识别…';
  await stopMicrophone();
  resetUI();
  startWakeWord(); // 结束会话后回到唤醒词监听，可再次"叮咚叮咚"唤醒
});

// 开场欢迎语音：页面加载即播放 test_chinese.wav，球体随语音起伏；
// 浏览器拦截自动播放时，等待首次点击再播
let greetBuffer=null,greetCtx=null;
function stopGreeting(){
  if(ttsSource&&!analyser){try{ttsSource.stop();}catch(_){}} // 停掉属于欢迎语音的播报源
  if(greetCtx){try{greetCtx.close();}catch(_){}}
  greetCtx=null;greetBuffer=null;
  if(!analyser){ttsAnalyser=null;ttsSource=null;ttsLive=false;if(chatState==='speaking')chatState='idle';}
}
async function playGreeting(){
  if(!greetBuffer||!greetCtx)return;
  try{if(greetCtx.state==='suspended')await greetCtx.resume();}catch(_){}
  if(greetCtx.state!=='running')return; // 自动播放被拦截，等首次点击
  chatState='speaking';ttsLive=true; // 复用 AI 播报通路：球体随音量起伏
  const an=greetCtx.createAnalyser();an.fftSize=1024;an.smoothingTimeConstant=.78;
  const src=greetCtx.createBufferSource();src.buffer=greetBuffer;
  src.connect(an);an.connect(greetCtx.destination);
  ttsAnalyser=an;ttsSource=src;
  await new Promise(res=>{src.onended=res;src.start();});
  ttsAnalyser=null;ttsSource=null;ttsLive=false;
  greetBuffer=null;
  if(chatState==='speaking')chatState='idle';
  try{greetCtx.close();}catch(_){}greetCtx=null; // 欢迎语音只播一次，播完即释放 AudioContext，避免泄漏
}
(async()=>{
  try{
    greetCtx=new (window.AudioContext||window.webkitAudioContext)();
    const resp=await fetch('/test_chinese.wav');
    if(resp.ok){greetBuffer=await greetCtx.decodeAudioData(await resp.arrayBuffer());await playGreeting();}
  }catch(_){greetCtx=null;greetBuffer=null;}
})();
preloadAck(); // 预取唤醒确认语"我在。"音频字节，唤醒时即时解码播放
// 首次交互：恢复欢迎语（如被自动播放拦截）→ 启动唤醒词监听
document.addEventListener('pointerdown',async(e)=>{
  if(e.target.closest('#mic-button')||e.target.closest('#end-button'))return; // 按钮各自处理
  if(greetBuffer&&greetCtx&&greetCtx.state!=='running')await playGreeting();
  if(!listening&&!wakeListening)startWakeWord();
});
resize();addEventListener('resize',resize);document.addEventListener('visibilitychange',()=>{if(!document.hidden) resize()});
frame=requestAnimationFrame(animate);
