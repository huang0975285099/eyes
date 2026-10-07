// fetch 超时封装：后端卡住时不会让 UI 永久停在“正在思考/聆听”
function fetchWithTimeout(url, opts, ms=30000){
  const ctrl=new AbortController();
  const id=setTimeout(()=>ctrl.abort(),ms);
  return fetch(url,Object.assign({},opts,{signal:ctrl.signal})).finally(()=>clearTimeout(id));
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
let mediaRecorder=null,recordedChunks=[],asrBusy=false,asrQueue=[];
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
    const rec=new MediaRecorder(micStream);
    rec.ondataavailable=e=>{if(e.data.size>0)recordedChunks.push(e.data);};
    rec.onstop=()=>{
      const chunks=recordedChunks.splice(0,recordedChunks.length);
      if(chunks.length>0){
        const blob=new Blob(chunks,{type:rec.mimeType||'audio/webm'});
        asrQueue.push(blob);processASRQueue();
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
      if(isGoodbye(data.text))goodbye=true; // “再见”照常上屏，稍后走告别流程（发送→AI告别→结束）
      else { const c=isVoiceCommand(replyText.textContent); if(c)pendingCommand=c; } // 语音指令：先上屏，停顿后随 autoSendChat 一起执行（与正常提问时序一致）
    }
    if(data.error&&chatState==='listening')voiceStatus.textContent='✦ 识别失败：'+data.error;
    else if(chatState==='listening')voiceStatus.textContent='✦ 聆听中 · 停顿后自动提问';
  }catch(error){
    if(chatState==='listening')voiceStatus.textContent='✦ 识别失败：'+(error.name==='AbortError'?'识别超时，请重说':error.message);
  }
  asrBusy=false; // 识别完成不重置发送计时：lastVoiceAt 只由说话声音更新，文字出现后很快自动发送
  if(goodbye){sendGoodbye();return;} // “再见”走告别流程：发送音效+动画 → AI 告别 → 结束会话
  if(asrQueue.length>0)processASRQueue();
}
// VAD 流水线（抗远处杂音）：触发门槛 0.055 + 连续 160ms 确认才算真语音；
// 远处声音音量小且断续，过不了确认；杂音也不更新发送计时（否则对话永不发出）
const VAD_OPEN=.15, VAD_CONFIRM_MS=160;
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
// 唤醒确认语“我在。”：页面加载时预取音频字节，唤醒时用主音频上下文即时解码播放
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

async function autoSendChat(){
  chatState='thinking';turnActive=false;playSendCue();spawnShockwave();
  const question=replyText.textContent.trim();
  if(!question){chatState='listening';return;}
  if(mediaRecorder){const r=mediaRecorder;mediaRecorder=null;r.onstop=null;try{r.stop();}catch(_){}} // 暂停录音，防止播报被录入
  // 语音指令（打开/关闭摄像头）：说完停顿后随发送流程一起执行，共享发送音效+冲击波，跳过 Ollama
  if(pendingCommand){
    const cmd=pendingCommand;pendingCommand=null;
    const reply=cmd==='open_cam'?'好的，摄像头已打开。':'好的，摄像头已关闭。';
    await speakAnswer(reply,true); // 先语音反馈
    if(!listening){chatState='idle';return;} // 期间点了结束会话
    recordedChunks=[];segSilenceStart=0;hasVoiceInSeg=false;segVoiceStart=0;asrQueue=[];
    chatState='listening';voiceStatus.textContent='✦ 聆听中 · 停顿后自动提问';
    startNewRecorder(); // 回聆听
    if(cmd==='open_cam')openAllCameras(); else if(cmd==='close_cam')closeAllCameras(); // 最后执行动作（异步，不阻塞对话）
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
      ttsLive=true; // 纯文字模式：打字开始即视为“回答中”，停止思考动画
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
// 唤醒确认语：唤醒后先回一句（如“我在。”），球体随声起伏，播完进入聆听
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
  // 进入聆听：重置会话状态，保留“我在。”文字直到用户开口（首条 ASR 结果会清空它）
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

// ===== 唤醒词“眉州眉州”：idle 态持续监听麦克风，命中后自动进入会话 =====
let wakeStream=null,wakeCtx=null,wakeAnalyser=null,wakeData=null;
let wakeRecorder=null,wakeChunks=[],wakeAsrBusy=false,wakeQueue=[];
let wakeListening=false,wakeCandStart=0,wakeSilence=0,wakeVoice=false,wakeVoiceStart=0;
// 唤醒词判定：清理标点空白后，“眉州”出现 ≥2 次即命中（兼容“眉州眉州”/“眉州 眉州”等）
function isWakeWord(text){
  const clean=String(text).replace(/[\s，,。.！!？?、~～]/g,'');
  return (clean.match(/眉州/g)||[]).length>=2;
}
// 退出词“再见”判定：清理标点空白后包含“再见”即命中（兼容“再见”/“好的再见”/“再见啦”等）
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
    src.connect(hp);hp.connect(wakeAnalyser);wakeData=new Uint8Array(wakeAnalyser.fftSize);
    wakeListening=true;
    if(chatState==='idle'){voiceStatus.textContent='✦  说出“眉州眉州”唤醒我';micCaption.textContent='等待唤醒';}
    startWakeRecorder();
  }catch(_){
    wakeListening=false;
    voiceStatus.textContent='✦  唤醒需要麦克风权限，也可点击“开始会话”';
  }
}
function startWakeRecorder(){
  if(!wakeStream||!wakeListening){wakeRecorder=null;return;}
  wakeChunks.length=0;
  try{
    const rec=new MediaRecorder(wakeStream);
    rec.ondataavailable=e=>{if(e.data.size>0)wakeChunks.push(e.data);};
    rec.onstop=()=>{
      if(!wakeListening){wakeChunks.length=0;return;} // 已停止，丢弃残留
      const chunks=wakeChunks.splice(0,wakeChunks.length);
      if(chunks.length>0){
        const blob=new Blob(chunks,{type:rec.mimeType||'audio/webm'});
        wakeQueue.push(blob);processWakeASR();
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
    if(data.text&&isWakeWord(data.text)){
      wakeListening=false; // 立即停掉监听，防止重复触发
      voiceStatus.textContent='✦  唤醒成功 · 进入会话';
      startMicrophone('我在。'); // 内部会 await stopWakeWord，并先回“我在。”再进入聆听
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
  await wakeCtx?.close();wakeCtx=null;wakeAnalyser=null;wakeData=null;
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
    audioContext.createMediaStreamSource(micStream).connect(highpass);highpass.connect(analyser);audioData=new Uint8Array(analyser.fftSize);listening=true;
    micCaption.textContent='麦克风监听中';micButton.setAttribute('aria-label','麦克风输入中');
    if(ack){
      await speakWakeAck(ack); // 唤醒确认：先回一句“我在。”再进入聆听
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
  await audioContext?.close();audioContext=null;analyser=null;audioData=null;
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

micButton.addEventListener('click',()=>startMicrophone()); // 箭头包裹：避免 click 事件被当作 ack 参数传入
// 语音“再见”告别流程：发送音效+冲击波 → 思考动画 → AI 回答固定告别语 → 结束会话
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
// 语音“再见”结束会话：等同点“结束会话”按钮（停录音识别→恢复界面→回唤醒监听）
async function endByVoice(){
  if(!listening)return;
  voiceStatus.textContent='✦  再见 · 结束会话中…';
  await stopMicrophone();
  resetUI();
  startWakeWord(); // 结束后回到唤醒词监听，可再次“眉州眉州”唤醒
}

document.getElementById('end-button').addEventListener('click',async()=>{
  if (!listening) return;
  voiceStatus.textContent='✦ 正在结束并识别…';
  await stopMicrophone();
  resetUI();
  startWakeWord(); // 结束会话后回到唤醒词监听，可再次“眉州眉州”唤醒
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
preloadAck(); // 预取唤醒确认语“我在。”音频字节，唤醒时即时解码播放
// 首次交互：恢复欢迎语（如被自动播放拦截）→ 启动唤醒词监听
document.addEventListener('pointerdown',async(e)=>{
  if(e.target.closest('#mic-button')||e.target.closest('#end-button'))return; // 按钮各自处理
  if(greetBuffer&&greetCtx&&greetCtx.state!=='running')await playGreeting();
  if(!listening&&!wakeListening)startWakeWord();
});
resize();addEventListener('resize',resize);document.addEventListener('visibilitychange',()=>{if(!document.hidden) resize()});
frame=requestAnimationFrame(animate);
