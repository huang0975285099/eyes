// 原型动效：待机时持续流动；用户授权麦克风后，声压包络驱动核心振幅。
const spaceCanvas = document.getElementById('space');
const spaceCtx = spaceCanvas.getContext('2d');
const coreCanvas = document.getElementById('core-canvas');
const coreCtx = coreCanvas.getContext('2d');
const core = document.querySelector('.core');
const micButton = document.getElementById('mic-button');
const micCaption = document.getElementById('mic-caption');
const voiceStatus = document.getElementById('voice-status');
const inputLevelEl = document.getElementById('input-level');
const stage = document.querySelector('.stage');
const reduceMotion = matchMedia('(prefers-reduced-motion: reduce)');
let width, height, stageWidth, pixelRatio, stars = [], audioContext, analyser, micStream, audioData, coreCanvasW = 0, coreCanvasH = 0, coreWidth = 0;
let targetLevel = 0, level = 0, gateLevel = 0, listening = false, frame = 0, silenceSince = 0;
const shockwaves = []; // 发送冲击波：光轮从球体爆发，冲出屏幕边缘
let chatState='idle'; // idle | listening | thinking | speaking
let ttsAnalyser=null,ttsSource=null,ttsLive=false; // ttsLive：TTS 已开始播报（false=仍在生成，显示思考动画）
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

function resize() {
  const rect = stage.getBoundingClientRect(); pixelRatio = Math.min(devicePixelRatio || 1, 2); width = innerWidth; height = innerHeight; stageWidth=rect.width;
  spaceCanvas.width = width * pixelRatio; spaceCanvas.height = height * pixelRatio;
  spaceCtx.setTransform(pixelRatio,0,0,pixelRatio,0,0);
  const coreRect = coreCanvas.getBoundingClientRect();
  coreCanvasW = coreRect.width; coreCanvasH = coreRect.height; coreWidth = core.clientWidth;
  coreCanvas.width = Math.max(1, Math.round(coreRect.width * pixelRatio));
  coreCanvas.height = Math.max(1, Math.round(coreRect.height * pixelRatio));
  coreCtx.setTransform(pixelRatio,0,0,pixelRatio,0,0);
  stars = Array.from({length:Math.min(750,Math.floor(width*height/2900))},(_,id)=>({id,x:Math.random()*width,y:Math.random()*height,vx:(Math.random()-.5)*.34,vy:(Math.random()-.5)*.34,r:Math.random()*1.9+.35,a:Math.random()*.5+.12,p:Math.random()*6.28,s:Math.random()*.13+.025,hue:Math.random()>.78?255:190,z:Math.random()}));
}

function sampleMicrophone() {
  // AI 播报时由 TTS 音频驱动；唤醒词监听时由唤醒分析器驱动；会话中由麦克风驱动
  const source = (chatState==='speaking' && ttsAnalyser) ? ttsAnalyser : (wakeListening && wakeAnalyser) ? wakeAnalyser : analyser;
  if (!source) { targetLevel = 0; return; }
  // 欢迎语音等场景麦克风未开启：按需创建采样缓冲（fftSize 均为 1024）
  if (!audioData || audioData.length !== source.fftSize) audioData = new Uint8Array(source.fftSize);
  source.getByteTimeDomainData(audioData);
  let sum = 0;
  for (let i=0;i<audioData.length;i++) { const n=(audioData[i]-128)/128; sum += n*n; }
  const rms = Math.sqrt(sum/audioData.length);
  // 提高灵敏度：更低噪声门 + 更大增益，正常说话即可明显驱动动效
  targetLevel = clamp((rms-.008)*14,0,1);
}

// 发光点 sprite 预渲染：把"圆点 + shadow 光晕"烘焙进离屏 canvas，
// 用 drawImage 复用替代逐点 arc+fill+shadow（星点 750 + 球体粒子 190），大幅降低 shadow 渲染开销。
function buildGlowSprite(size,dotColor,glowColor,dotR,glowBlur){
  const c=document.createElement('canvas');c.width=c.height=size;
  const g=c.getContext('2d');g.shadowColor=glowColor;g.shadowBlur=glowBlur;g.fillStyle=dotColor;
  g.beginPath();g.arc(size/2,size/2,dotR,0,Math.PI*2);g.fill();return c;
}
// 星点 sprite：青(190)/紫(255) 两色相；drawImage 按 radius 缩放、globalAlpha 控透明
const starSpriteSize=96,starSpriteDotR=3.2;
const starSprite190=buildGlowSprite(starSpriteSize,'hsla(190,98%,84%,1)','hsla(190,100%,76%,1)',starSpriteDotR,13);
const starSprite255=buildGlowSprite(starSpriteSize,'hsla(255,98%,84%,1)','hsla(255,100%,76%,1)',starSpriteDotR,13);
// 球体粒子 sprite：青点/紫点，统一青色光晕
const dotSpriteSize=32,dotSpriteDotR=2;
const dotSpriteCyan=buildGlowSprite(dotSpriteSize,'rgba(139,239,255,1)','#69dfff',dotSpriteDotR,6);
const dotSpritePurple=buildGlowSprite(dotSpriteSize,'rgba(194,160,255,1)','#69dfff',dotSpriteDotR,6);

const orbitLabels=[...document.querySelectorAll('.orbit-label')];
// 每个标签一颗独立“卫星”：各自轨道半径、速度、倾角与升交点方位，多平面交错环绕球体。
const satelliteParams=orbitLabels.map((_,index)=>({
  radius:.76+((index*7)%4)*.11,                      // 0.76 / 0.87 / 0.98 / 1.09
  speed:(.17+((index*5)%3)*.05)*(index%3===1?-1:1),  // 三档速度，部分逆行
  phase:index*1.87,
  incl:(.62+((index*3)%4)*.18)*(index%2?1:-1),       // 约 ±35° 到 ±66°，正负交错
  node:index*.97+(index%2)*.55,                      // 轨道平面方位角铺开
}));
function drawCore(t) {
  const cw=coreCanvasW, ch=coreCanvasH;
  coreCtx.clearRect(0,0,cw,ch);
  level += (targetLevel-level) * (targetLevel>level ? .3 : .08);
  // 视觉门限（关联降噪）：低于 VAD_OPEN 的声音大幅衰减，中心球只在"会被识别的声音"时显著跳动
  const vadOpen = typeof VAD_OPEN !== 'undefined' ? VAD_OPEN : 0.22; // 跨文件取 VAD_OPEN，兜底防脚本未加载导致 drawCore 崩溃
  const gateTarget = level < vadOpen ? 0 : level; // 远场（低于门槛）直接归零，中心球只剩待机呼吸
  gateLevel += (gateTarget-gateLevel) * (gateTarget>gateLevel ? .3 : .08);
  const idle = reduceMotion.matches ? .1 : .08*Math.sin(t*.0011)+.045*Math.sin(t*.0023+1.4);
  // 音量驱动：会话聆听中（麦克风）或唤醒监听中（唤醒分析器）或 TTS 播报中（ttsAnalyser，欢迎语音同理）
  const energy=(chatState==='thinking'||(chatState==='speaking'&&!ttsLive)) ? .5+.22*Math.sin(t*.006) : ((listening||wakeListening) ? gateLevel : (chatState==='speaking'&&ttsLive) ? level : 0);
  // 画布扩到 200% 后按比例缩小基准半径，球体视觉大小与旧版一致（.36×128% ≈ .2304×200%）
  const base = Math.min(cw,ch)*.2304;
  const time = t*.00045;
  // 音量驱动的整体游走：改为画布内变换实现，避免 CSS transform 缩放整个画布位图导致内容越界截断
  const motion=reduceMotion.matches?0:1;
  const wander=base*.05*(1+energy*3.5);
  const wx=motion*(Math.sin(t*.0011)*wander+Math.sin(t*.0031+2.1)*wander*.35);
  const wy=motion*(Math.cos(t*.0014+.7)*wander+Math.sin(t*.0026+1.4)*wander*.5);
  const grow=1+idle*.18+energy*.16;
  const rot=(motion*(Math.sin(time*.42)*1.2+energy*4))*Math.PI/180;
  const cx=cw/2+wx, cy=ch/2+wy;
  // 软性限幅：极端峰值只被平滑压缩，永不越过画布边界（消灭截断直线）
  const fitR=Math.min(cw,ch)*.5-base*.1;
  const knee=fitR*.86, soft=(fitR-knee)||1;
  const fit=v=>v<=knee?v:knee+soft*Math.tanh((v-knee)/soft);
  coreCtx.save();
  coreCtx.translate(cx,cy);coreCtx.rotate(rot);coreCtx.scale(grow,grow);coreCtx.translate(-cx,-cy);
  // 土星式光轮：整圈先画（后侧被球体遮挡），球体完成后再补前侧弧线。
  // 两根光环各自独立摆动：上下浮动、倾角晃动、平面进动与半径微呼吸，互不同步。
  const ringSway=[
    {rr:base*1.46, color:'rgba(137,231,255,', lw:1.4, glow:'#57d7ff',
     bobAmp:base*.05, bobSpd:.9,  bobPh:0,
     tilt:.38, tiltAmp:.09, tiltSpd:.7, tiltPh:1.2,
     spinAmp:.16, spinSpd:.35, spinPh:.4},
    {rr:base*1.64, color:'rgba(150,158,255,', lw:1.1, glow:'#8e88ff',
     bobAmp:base*.07, bobSpd:.6,  bobPh:2.4,
     tilt:.34, tiltAmp:.11, tiltSpd:.5, tiltPh:3.7,
     spinAmp:.2, spinSpd:.25, spinPh:1.9},
  ];
  const drawRings=(front)=>{
    for(const k of ringSway){
      const cyr=cy+Math.sin(time*k.bobSpd+k.bobPh)*k.bobAmp*(1+energy*1.1);
      const tilt=k.tilt+Math.sin(time*k.tiltSpd+k.tiltPh)*k.tiltAmp;
      const rot=Math.sin(time*k.spinSpd+k.spinPh)*k.spinAmp;
      const rr=k.rr*(1+Math.sin(time*.8+k.bobPh*2)*.015);
      const alpha=front ? .34+energy*.2 : .11;
      coreCtx.save();
      coreCtx.lineWidth=k.lw;
      coreCtx.strokeStyle=k.color+alpha.toFixed(3)+')';
      coreCtx.shadowColor=k.glow;
      coreCtx.shadowBlur=front ? 12+energy*20 : 6;
      coreCtx.beginPath();
      coreCtx.ellipse(cx,cyr,rr,rr*tilt,rot,0,front ? Math.PI : Math.PI*2);
      coreCtx.stroke();
      coreCtx.restore();
    }
  };
  drawRings(false);
  // Organic fluid body: a smooth asymmetric silhouette with drifting inner light currents.
  const bodyPath=new Path2D();
  const bodyPoints=180;
  for(let i=0;i<bodyPoints;i++) {
    const a=i/bodyPoints*Math.PI*2;
    const slow=Math.sin(a*3+time*1.15)*.044+Math.cos(a*5-time*.82)*.03;
    const fast=Math.sin(a*2-time*1.9+.8)*(.022+energy*.12)+Math.cos(a*7+time*1.4)*(.015+energy*.09);
    const breath=Math.sin(a*4+time*2.6)*(.016+energy*.16);
    const tremor=Math.sin(a*11+time*6.3)*(.004+energy*.07); // 音量驱动的高频颤动
    const r=fit(base*(1+slow+fast+breath+tremor+idle+energy*.15));
    const px=cx+Math.cos(a)*r,py=cy+Math.sin(a)*r;
    if(i===0)bodyPath.moveTo(px,py);else bodyPath.lineTo(px,py);
  }
  bodyPath.closePath();
  coreCtx.save();
  coreCtx.shadowColor=`rgba(71,190,255,${.5+energy*.22})`;coreCtx.shadowBlur=28+energy*32;
  const bodyFill=coreCtx.createRadialGradient(cx-base*.34+Math.sin(time)*base*.12,cy-base*.42,base*.04,cx,cy,base*1.08);
  bodyFill.addColorStop(0,'rgba(172,246,255,.96)');bodyFill.addColorStop(.08,'rgba(91,225,255,.92)');bodyFill.addColorStop(.29,'rgba(43,142,255,.94)');bodyFill.addColorStop(.54,'rgba(54,83,231,.9)');bodyFill.addColorStop(.76,'rgba(41,45,151,.78)');bodyFill.addColorStop(1,'rgba(8,12,43,.22)');
  coreCtx.fillStyle=bodyFill;coreCtx.fill(bodyPath);
  coreCtx.shadowBlur=0;coreCtx.save();coreCtx.clip(bodyPath);coreCtx.globalCompositeOperation='screen';
  for(let i=0;i<5;i++) {
    const a=time*(i%2?-.42:.31)+i*2.38;
    const hx=cx+Math.cos(a)*base*.52,hy=cy+Math.sin(a*1.23)*base*.48;
    const light=coreCtx.createRadialGradient(hx,hy,0,hx,hy,base*(.25+(i%3)*.11+energy*.16));
    const colors=['133,255,250','96,185,255','177,139,255'];
    light.addColorStop(0,`rgba(${colors[i%3]},${.3+energy*.2})`);light.addColorStop(.45,`rgba(${colors[i%3]},${.12+energy*.1})`);light.addColorStop(1,'rgba(70,135,255,0)');
    coreCtx.fillStyle=light;coreCtx.fillRect(cx-base*1.2,cy-base*1.2,base*2.4,base*2.4);
  }
  coreCtx.restore();
  // Soft glass highlight and a quiet shifting contour keep the orb dimensional.
  const sheen=coreCtx.createRadialGradient(cx-base*.38,cy-base*.48,0,cx-base*.18,cy-base*.2,base*.72);
  sheen.addColorStop(0,'rgba(238,255,255,.26)');sheen.addColorStop(.18,'rgba(158,242,255,.1)');sheen.addColorStop(.55,'rgba(107,158,255,.02)');sheen.addColorStop(1,'rgba(100,130,255,0)');
  coreCtx.fillStyle=sheen;coreCtx.fill(bodyPath);
  coreCtx.lineWidth=.7;coreCtx.strokeStyle=`rgba(169,248,255,${.07+energy*.12})`;coreCtx.shadowColor='#73dfff';coreCtx.shadowBlur=7+energy*9;coreCtx.stroke(bodyPath);coreCtx.restore();
  // Fine moving contour layers and surface particles.
  for (let layer=3;layer>=0;layer--) {
    const points=layer===0?170:100;
    coreCtx.beginPath();
    for(let i=0;i<=points;i++) {
      const a=i/points*Math.PI*2;
      const wave=Math.sin(a*5+time*2.4+layer)*(.04+energy*.2)+Math.sin(a*9-time*1.7+layer*2)*(.028+energy*.14)+Math.sin(a*3+time*.8)*(.04+energy*.09)+Math.cos(a*2-time*1.25+layer*.7)*(.024+energy*.08);
      const radius=fit(base*(1+wave+idle+energy*.095)*(.72+layer*.105));
      const x=cx+Math.cos(a+time*(layer%2?-.18:.13))*radius;
      const y=cy+Math.sin(a+time*(layer%2?-.18:.13))*radius;
      if(i===0) coreCtx.moveTo(x,y); else coreCtx.lineTo(x,y);
    }
    coreCtx.closePath();
    const hue=layer%2?193:224;
    coreCtx.fillStyle=`hsla(${hue},95%,${52+energy*20}%,${.018+energy*.022})`;
    coreCtx.strokeStyle=`hsla(${hue},100%,${68+energy*15}%,${.045+energy*.1})`;
    coreCtx.lineWidth=layer===0?1.15: .7; coreCtx.shadowBlur=9+energy*24; coreCtx.shadowColor=`hsla(${hue},100%,70%,${.2+energy*.35})`; coreCtx.fill();coreCtx.stroke();
  }
  drawRings(true);
  coreCtx.shadowBlur=0;
  const dots=190;
  for(let i=0;i<dots;i++) {
    const a=i*2.399963+time*(i%2?1:-.72);
    const radial=Math.sqrt((i+.5)/dots)*base*(.97+Math.sin(time*2+i)*.018+energy*.11);
    const wobble=Math.sin(a*4+time*4+i*.03)*(2+energy*8);
    const x=cx+Math.cos(a)*radial+wobble*.22, y=cy+Math.sin(a)*radial+wobble;
    const size=(i%13===0?1.45:.65)+(Math.sin(time*3+i)*.22)+energy*.9;
    const alpha=.16+((Math.sin(time*2.4+i*1.73)+1)*.16)+energy*.36;
    const sprite=i%5===0?dotSpritePurple:dotSpriteCyan;
    const dw=dotSpriteSize*size/dotSpriteDotR; // 点视觉半径=size：sprite 缩放比=size/dotR
    coreCtx.globalAlpha=Math.max(0,Math.min(1,alpha));
    coreCtx.drawImage(sprite,x-dw/2,y-dw/2,dw,dw);
  }
  coreCtx.globalAlpha=1;
  coreCtx.restore();
  // 思考态动画（含 TTS 生成等待）：能量光环——呼吸光晕 + 向外扩散的声呐环 + 螺旋上升光尘（柔和，无生硬光束）
  if(chatState==='thinking'||(chatState==='speaking'&&!ttsLive)){
    const tp=t*.001;
    coreCtx.save();coreCtx.globalCompositeOperation='lighter';
    const haloR=base*(1.28+.12*Math.sin(tp*2.4));
    const halo=coreCtx.createRadialGradient(cx,cy,base*.5,cx,cy,haloR);
    halo.addColorStop(0,`rgba(120,225,255,${(.15+.05*Math.sin(tp*2.4)).toFixed(3)})`);
    halo.addColorStop(.6,'rgba(90,180,255,.06)');
    halo.addColorStop(1,'rgba(90,180,255,0)');
    coreCtx.fillStyle=halo;coreCtx.beginPath();coreCtx.arc(cx,cy,haloR,0,Math.PI*2);coreCtx.fill();
    for(let i=0;i<3;i++){ // 三道声呐环，相位错开，越扩越淡
      const p=((tp*.4)+i/3)%1;
      const rr=base*(1.06+p*1.3);
      coreCtx.beginPath();
      coreCtx.strokeStyle=`rgba(140,230,255,${(.3*(1-p)*(1-p)).toFixed(3)})`;
      coreCtx.lineWidth=1.6-p*1.2;
      coreCtx.shadowColor='#69dfff';coreCtx.shadowBlur=10;
      coreCtx.arc(cx,cy,rr,0,Math.PI*2);coreCtx.stroke();
    }
    for(let i=0;i<12;i++){ // 光尘绕球螺旋上升，仿佛能量被吸入
      const p=((tp*(.2+(i%5)*.05))+i*.41)%1;
      const ang=i*2.4+tp*.6;
      const rr=base*(1.45-p*.5);
      const mx=cx+Math.cos(ang)*rr;
      const my=cy+Math.sin(ang)*rr*.78-p*base*.5;
      coreCtx.beginPath();
      coreCtx.fillStyle=`rgba(170,240,255,${(Math.sin(p*Math.PI)*.5).toFixed(3)})`;
      coreCtx.shadowBlur=6;coreCtx.shadowColor='#8ae8ff';
      coreCtx.arc(mx,my,1.4,0,Math.PI*2);coreCtx.fill();
    }
    coreCtx.restore();
  }
  coreCtx.restore(); // 还原 drawCore 开头 save()：避免 translate/rotate/scale 每帧累积与状态栈泄漏
  core.style.filter=`brightness(${1+energy*.24}) saturate(${1+energy*.2})`;
  const orbit=Math.min(coreWidth*1.434,stageWidth*.5-112);
  const persp=1400; // 透视焦距：近处卫星放大、远处缩小
  orbitLabels.forEach((label,index)=>{
    const p=satelliteParams[index];
    const angle=time*p.speed+p.phase;
    const rr=orbit*p.radius;
    // 轨道面内圆周位置
    const ox=Math.cos(angle)*rr, oy=Math.sin(angle)*rr;
    // 倾角（绕 x 轴）+ 升交点方位（绕屏幕法线）：每颗卫星一个独立轨道平面
    const yTilt=oy*Math.cos(p.incl), z=oy*Math.sin(p.incl);
    const x=ox*Math.cos(p.node)-yTilt*Math.sin(p.node);
    const y=ox*Math.sin(p.node)+yTilt*Math.cos(p.node);
    const perspScale=persp/(persp-z);
    const depth=(z/(rr*Math.abs(Math.sin(p.incl)))+1)/2;
    const scale=.58+depth*.52;
    label.style.transform=`translate(-50%,-50%) translate3d(${(x*perspScale).toFixed(1)}px,${(y*perspScale).toFixed(1)}px,0) scale(${(scale*perspScale).toFixed(3)}) perspective(240px) rotateX(${(-Math.sign(p.speed)*(oy/rr)*12).toFixed(1)}deg)`;
    label.style.opacity=String(.26+.74*depth);
    label.style.filter=`blur(${((1-depth)*1.3).toFixed(2)}px)`;
    label.style.zIndex=z>0?'4':'0';
  });
  if(inputLevelEl) inputLevelEl.textContent=Math.round(level*100)+'%';
  // 实时音量条：直观确认麦克风信号在驱动动效
  if(listening){
    checkVAD(level,t); // VAD 自动分段：停顿后截取一段送识别
    const bars=Math.round(level*6);
    micCaption.textContent='▮'.repeat(bars)+'▯'.repeat(6-bars);
    // 信号诊断：高通滤波后 RMS 整体降低，阈值相应调低（0.003）
    if(level<.003){
      if(!silenceSince) silenceSince=t;
      else if(t-silenceSince>8000) voiceStatus.textContent='✦  信号很弱：请检查 Windows 默认输入设备与浏览器麦克风权限';
    } else silenceSince=0;
  } else {
    silenceSince=0;
    if(wakeListening) checkWakeVAD(level,t); // 唤醒词 VAD：切段送 ASR，命中“叮咚叮咚”即进入会话
  }
}

function drawSpace(t) {
  spaceCtx.clearRect(0,0,width,height);
  const cx=width/2,cy=height*.47,pulse=gateLevel*(listening?1:0);
  // Multiple oversized light fields travel across the entire viewport.
  spaceCtx.globalCompositeOperation='screen';
  for(let i=0;i<5;i++) {
    const driftX=cx+Math.sin(t*.00011+i*1.7)*width*.47;
    const driftY=height*(.12+i*.18)+Math.cos(t*.00014+i*2.1)*height*.2;
    const radius=Math.max(width,height)*(.19+((i%3)*.055)+pulse*.14);
    const field=spaceCtx.createRadialGradient(driftX,driftY,0,driftX,driftY,radius);
    field.addColorStop(0,`hsla(${i%2?190:225},100%,62%,${.038+pulse*.08})`);
    field.addColorStop(.22,`hsla(${i%2?207:255},100%,58%,${.018+pulse*.055})`);
    field.addColorStop(1,'rgba(0,0,0,0)');spaceCtx.fillStyle=field;spaceCtx.fillRect(0,0,width,height);
  }
  const wash=spaceCtx.createRadialGradient(cx+Math.sin(t*.00016)*width*.12,cy+Math.cos(t*.00013)*height*.1,4,cx,cy,Math.max(width,height)*.58);
  wash.addColorStop(0,`rgba(31,102,210,${.024+pulse*.06})`);wash.addColorStop(.34,`rgba(34,206,231,${.012+pulse*.045})`);wash.addColorStop(.72,'rgba(83,67,191,.008)');wash.addColorStop(1,'rgba(0,0,0,0)');
  spaceCtx.fillStyle=wash;spaceCtx.fillRect(0,0,width,height);
  // Full-screen particle currents, layered at different speeds and depths.
  for(let stream=0;stream<6;stream++) {
    const phase=t*(.00012+stream*.000027)+stream*2.1;
    spaceCtx.beginPath();
    for(let x=-40;x<=width+40;x+=Math.max(12,width/95)) {
      const progress=(x+40)/(width+80);
      const y=height*(.12+stream*.15)+Math.sin(progress*8+phase)*height*.075+Math.sin(progress*17-phase*1.4)*height*.025;
      if(x===-40)spaceCtx.moveTo(x,y);else spaceCtx.lineTo(x,y);
    }
    const flow=spaceCtx.createLinearGradient(0,0,width,height);
    flow.addColorStop(0,'rgba(63,121,255,0)');flow.addColorStop(.25,`rgba(85,190,255,${.032+pulse*.13})`);flow.addColorStop(.53,`rgba(83,244,231,${.026+pulse*.11})`);flow.addColorStop(.8,`rgba(135,100,255,${.032+pulse*.13})`);flow.addColorStop(1,'rgba(63,121,255,0)');
      spaceCtx.strokeStyle=flow;spaceCtx.lineWidth=1.3+stream*.35+pulse*2;spaceCtx.shadowBlur=20+pulse*22;spaceCtx.shadowColor='#42caff';spaceCtx.stroke();
  }
  spaceCtx.shadowBlur=0;spaceCtx.globalCompositeOperation='source-over';
  if(pulse>.015) for(let i=0;i<4;i++) {
    const radius=((t*.055+i*115)%Math.max(width,height)*.65);
    spaceCtx.beginPath();spaceCtx.ellipse(cx,cy,radius,radius*.58,Math.sin(t*.0002+i)*.08,0,Math.PI*2);
    spaceCtx.strokeStyle=`rgba(80,207,255,${pulse*(1-radius/(Math.max(width,height)*.72))*.13})`;spaceCtx.lineWidth=1+pulse*2;spaceCtx.stroke();
  }
  for(const p of stars) {
    p.x+=p.vx*(.45+p.z*1.5+pulse*5);p.y+=p.vy*(.45+p.z*1.5+pulse*5);
    p.x+=Math.sin(t*.0002+p.p)*(.08+p.z*.18);p.y+=Math.cos(t*.00017+p.p)*(.08+p.z*.18);
    if(p.x<-8)p.x=width+8;if(p.x>width+8)p.x=-8;if(p.y<-8)p.y=height+8;if(p.y>height+8)p.y=-8;
  }
  const cellSize=130,grid=new Map();
  for(const star of stars) { const gx=Math.floor(star.x/cellSize),gy=Math.floor(star.y/cellSize),key=`${gx},${gy}`;if(!grid.has(key))grid.set(key,[]);grid.get(key).push(star); }
  for(const a of stars) {
    const gx=Math.floor(a.x/cellSize),gy=Math.floor(a.y/cellSize);
    for(let ox=-1;ox<=1;ox++)for(let oy=-1;oy<=1;oy++)for(const b of grid.get(`${gx+ox},${gy+oy}`)||[]) {
      if(b.id<=a.id)continue;const dx=a.x-b.x,dy=a.y-b.y,dist=Math.hypot(dx,dy),limit=66+pulse*35;if(dist>=limit)continue;
      const alpha=(1-dist/limit)*(.055+pulse*.14);spaceCtx.beginPath();spaceCtx.moveTo(a.x,a.y);spaceCtx.lineTo(b.x,b.y);spaceCtx.strokeStyle=`rgba(83,197,255,${alpha})`;spaceCtx.lineWidth=.5+pulse*.6;spaceCtx.stroke();
    }
  }
  spaceCtx.shadowBlur=0; // drawImage 复用烘焙好的光晕，关闭画布 shadow 避免图像被二次投影
  for(const p of stars) {
    const flicker=.45+Math.sin(t*.001*p.s*8+p.p)*.3+Math.sin(t*.00031+p.p*2)*.11;
    const radius=p.r*(1+pulse*1.2*(.5+.5*Math.sin(t*.002+p.p)));
    const sprite=p.hue>200?starSprite255:starSprite190;
    const dw=starSpriteSize*radius/starSpriteDotR; // 点视觉半径=radius：sprite 缩放比=radius/dotR
    spaceCtx.globalAlpha=Math.max(0,Math.min(1,p.a*flicker+pulse*.24));
    spaceCtx.drawImage(sprite,p.x+Math.sin(t*.0004+p.p)*2-dw/2,p.y+Math.cos(t*.00032+p.p)*2-dw/2,dw,dw);
  }
  spaceCtx.globalAlpha=1;
  spaceCtx.shadowBlur=0;
  // 发送冲击波：光轮从球心爆发，2.2 秒由慢到快冲出屏幕边缘后消散
  if(shockwaves.length){
    spaceCtx.save();spaceCtx.globalCompositeOperation='lighter';
    const maxR=Math.hypot(width,height)*.62; // 超过对角线半径，确保冲出屏幕
    for(let i=shockwaves.length-1;i>=0;i--){
      const s=shockwaves[i],p=(t-s.start)/2200;
      if(p>=1){shockwaves.splice(i,1);continue;}
      const e=Math.pow(p,2.3); // 先蓄势后加速：从慢到快冲出屏幕
      const r=70+(maxR-70)*e;
      const fade=Math.pow(1-p,1.2);
      spaceCtx.beginPath(); // 主光轮：粗亮边+辉光
      spaceCtx.arc(s.cx,s.cy,r,0,Math.PI*2);
      spaceCtx.strokeStyle=`rgba(150,238,255,${(.62*fade).toFixed(3)})`;
      spaceCtx.lineWidth=4+13*fade;
      spaceCtx.shadowColor='#6fe0ff';spaceCtx.shadowBlur=34*fade+10;
      spaceCtx.stroke();
      if(p>.08){ // 次级光轮：跟随其后，稍细更淡
        const p2=Math.max(0,p-.09),e2=Math.pow(p2,2.3),r2=70+(maxR-70)*e2;
        spaceCtx.beginPath();
        spaceCtx.arc(s.cx,s.cy,r2,0,Math.PI*2);
        spaceCtx.strokeStyle=`rgba(120,170,255,${(.3*fade).toFixed(3)})`;
        spaceCtx.lineWidth=2.2+7*fade;
        spaceCtx.stroke();
      }
    }
    spaceCtx.restore();
  }
}

function animate(t) {
  // 麦克风采样不受系统“减少动态效果”设置影响（否则该设置开启时永远采不到音量）
  sampleMicrophone();
  drawSpace(t); drawCore(t); frame=requestAnimationFrame(animate);
}

function spawnShockwave(){
  const rect=coreCanvas.getBoundingClientRect(); // 光轮从球体实际屏幕位置爆发
  shockwaves.push({cx:rect.left+rect.width/2,cy:rect.top+rect.height/2,start:performance.now()});
}
