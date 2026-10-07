// ===== 科技感摄像头窗口：点击任意卫星标签弹出所有 USB 摄像头（每个设备一个窗口，随机分布） =====
let camSlots=[]; // [{deviceId, win, stream}]
async function enumerateVideoDevices(){
  // 首次需先获取权限，enumerateDevices 才能拿到 label 与稳定 deviceId
  let probe=null;
  try{ probe=await navigator.mediaDevices.getUserMedia({video:true}); }catch(_){ return []; }
  probe.getTracks().forEach(t=>t.stop());
  try{
    const devices=await navigator.mediaDevices.enumerateDevices();
    const vids=devices.filter(d=>d.kind==='videoinput');
    return vids.length?vids:[{deviceId:'',label:'摄像头'}]; // 有权限但枚举为空时回退
  }catch(_){ return []; }
}
function attachCamDrag(win){
  const head=win.querySelector('.cam-head');
  let dragging=false, sx=0, sy=0, ox=0, oy=0;
  head.addEventListener('pointerdown',(e)=>{
    if(e.target.closest('.cam-close'))return; // 点关闭按钮不触发拖动
    dragging=true; sx=e.clientX; sy=e.clientY;
    const rect=win.getBoundingClientRect(); ox=rect.left; oy=rect.top;
    win.classList.add('dragging'); head.setPointerCapture(e.pointerId);
  });
  head.addEventListener('pointermove',(e)=>{
    if(!dragging)return;
    const nx=ox+(e.clientX-sx), ny=oy+(e.clientY-sy);
    const w=win.offsetWidth, h=win.offsetHeight;
    win.style.left=Math.max(0,Math.min(innerWidth-w,nx))+'px'; // 限制不拖出视口
    win.style.top=Math.max(0,Math.min(innerHeight-h,ny))+'px';
  });
  const endDrag=(e)=>{ if(!dragging)return; dragging=false; win.classList.remove('dragging'); try{head.releasePointerCapture(e.pointerId);}catch(_){} };
  head.addEventListener('pointerup',endDrag);
  head.addEventListener('pointercancel',endDrag);
}
function ensureCamWindow(deviceId, label){
  let slot=camSlots.find(s=>s.deviceId===deviceId);
  if(slot){ slot.win.querySelector('.cam-title').innerHTML='<span class="live-dot"></span>'+label; return slot; }
  const win=document.createElement('div');
  win.className='cam-window';
  win.innerHTML='<div class="cam-head"><span class="cam-title"><span class="live-dot"></span>'+label+'</span><span class="cam-close" title="关闭">✕</span></div><div class="cam-stage"><video autoplay playsinline muted></video><div class="cam-grid"></div><div class="cam-scan"></div><i class="cam-corner tl"></i><i class="cam-corner tr"></i><i class="cam-corner bl"></i><i class="cam-corner br"></i><div class="cam-fallback" hidden>摄像头不可用<br>请检查设备与浏览器权限</div></div>';
  document.body.appendChild(win);
  attachCamDrag(win);
  win.querySelector('.cam-close').addEventListener('click',()=>closeCamWindow(deviceId));
  slot={deviceId, win, stream:null};
  camSlots.push(slot);
  return slot;
}
function placeCamWindow(win){
  // offsetHeight 触发同步 layout，可直接量取（transform 不影响布局尺寸，无需临时 add open）
  const w=480, h=win.offsetHeight||360;
  const mx=Math.max(40,innerWidth*0.06), my=Math.max(40,innerHeight*0.06);
  const xRange=Math.max(0,innerWidth-w-mx*2), yRange=Math.max(0,innerHeight-h-my*2);
  // 已打开窗口的中心点：新窗口尽量远离它们，避免多个窗口挤在一起
  const placed=camSlots.filter(s=>s.win!==win&&s.win.classList.contains('open')).map(s=>{
    const px=parseFloat(s.win.style.left)||0, py=parseFloat(s.win.style.top)||0;
    return {cx:px+(s.win.offsetWidth||w)/2, cy:py+(s.win.offsetHeight||h)/2};
  });
  let bestX=mx+Math.random()*xRange, bestY=my+Math.random()*yRange, bestDist=placed.length?0:Infinity;
  for(let i=0;i<30;i++){ // 多次随机采样，取离最近邻居最远的位置（最远点采样）
    const x=mx+Math.random()*xRange, y=my+Math.random()*yRange;
    const cx=x+w/2, cy=y+h/2;
    let minD=Infinity;
    for(const p of placed) minD=Math.min(minD,Math.hypot(cx-p.cx,cy-p.cy));
    if(minD>bestDist){ bestDist=minD; bestX=x; bestY=y; }
    if(placed.length===0) break;
  }
  win.style.left=bestX.toFixed(0)+'px'; win.style.top=bestY.toFixed(0)+'px';
  win.classList.add('open'); // 触发“从小到大”弹出动画
}
async function startCamStream(slot){
  const win=slot.win;
  const video=win.querySelector('video');
  const fb=win.querySelector('.cam-fallback');
  try{
    if(!slot.stream) slot.stream=await navigator.mediaDevices.getUserMedia({video:{deviceId:slot.deviceId?{exact:slot.deviceId}:undefined,width:{ideal:1280},height:{ideal:720}}});
    video.srcObject=slot.stream; video.hidden=false; fb.hidden=true;
  }catch(_){
    video.hidden=true; fb.hidden=false;
  }
}
async function openAllCameras(){
  const devices=await enumerateVideoDevices();
  if(devices.length===0){
    // 无摄像头或无权限：弹一个提示窗口
    const slot=ensureCamWindow('__none__','摄像头');
    placeCamWindow(slot.win);
    const v=slot.win.querySelector('video'); v.hidden=true;
    slot.win.querySelector('.cam-fallback').hidden=false;
    return;
  }
  // 清理已拔出设备对应的窗口
  const ids=devices.map(d=>d.deviceId);
  [...camSlots].forEach(s=>{ if(s.deviceId!=='__none__'&&!ids.includes(s.deviceId)) closeCamWindow(s.deviceId); });
  // 为每个设备创建/复用窗口，随机分布（已打开的窗口保留原位置，避免反复点击抖动）
  devices.forEach((d,i)=>{
    const label=(i+1)+'号摄像头';
    const slot=ensureCamWindow(d.deviceId, label);
    if(!slot.win.classList.contains('open')) placeCamWindow(slot.win);
    startCamStream(slot);
  });
}
function closeCamWindow(deviceId){
  const slot=camSlots.find(s=>s.deviceId===deviceId);
  if(!slot)return;
  slot.win.classList.remove('open');
  setTimeout(()=>{ // 延迟释放摄像头与移除窗口，让退出动画跑完
    if(slot.stream){slot.stream.getTracks().forEach(t=>t.stop());slot.stream=null;}
    const v=slot.win.querySelector('video'); if(v)v.srcObject=null;
    slot.win.remove();
    camSlots=camSlots.filter(s=>s!==slot);
  },320);
}
function closeAllCameras(){[...camSlots].forEach(s=>closeCamWindow(s.deviceId));} // 关闭所有摄像头窗口
// 语音指令判定：清理标点空白后匹配“打开/关闭 + 摄像头”（兼容“帮我打开摄像头”“把摄像头关掉”等）
function isVoiceCommand(text){
  const s=String(text).replace(/[\s，,。.！!？?、~～]/g,'');
  if(/摄像头/.test(s)&&/(打开|开启|显示|出来|调出|开一下)/.test(s))return 'open_cam';
  if(/摄像头/.test(s)&&/(关闭|关掉|关上|收起|关了)/.test(s))return 'close_cam';
  return null;
}
// 点击任意卫星标签：同时弹出所有摄像头窗口
orbitLabels.forEach(label=>label.addEventListener('click',openAllCameras));
