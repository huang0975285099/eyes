// ===== 语音助手文本判定规则（从 index-voice.js 拆出，纯函数） =====
// 视觉问句判定：含描述意图；有“摄像头”按编号/人名，无则用传入的 activeCamSlot 上下文
function isVisionQuestion(text, activeCamSlot){
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
  // 无“摄像头”但有描述意图 + 有活动摄像头上下文 → 描述刚打开的那个
  if(activeCamSlot)return {active:true};
  return null;
}
// 唤醒词判定：清理标点空白后“叮咚/丁冬/丁东”出现≥2次
function isWakeWord(text){
  const clean=String(text).replace(/[\s，,。.！!？?、~～]/g,'');
  return (clean.match(/叮咚|丁冬|丁东/g)||[]).length>=2;
}
// 退出词“再见”判定：清理标点空白后包含“再见”即命中
function isGoodbye(text){
  return /再见/.test(String(text).replace(/[\s，,。.！!？?、~～]/g,''));
}
