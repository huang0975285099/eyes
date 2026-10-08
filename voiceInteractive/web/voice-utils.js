// ===== 语音助手公共工具函数（从 index-voice.js 拆出，无状态依赖） =====
// fetch 超时封装：后端卡住时不会让 UI 永久停在“正在思考/聆听”
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
// blob 转 base64 字符串（去掉 data: 前缀）
function blobToBase64(blob){
  return new Promise((res,rej)=>{
    const r=new FileReader();
    r.onload=()=>{const s=String(r.result||'');res(s.includes(',')?s.split(',')[1]:s);};
    r.onerror=()=>rej(r.error);
    r.readAsDataURL(blob);
  });
}
