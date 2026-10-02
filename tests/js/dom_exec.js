/* 在真实 DOM 桩里执行 map.html 的内联脚本，确认浏览器环境下也能跑通。
   比纯逻辑复刻强的地方：用的是文件里逐字的代码，不是抄的副本。
   跑法：node tests/js/dom_exec.js
   ⚠ 必须按 60fps 节奏推进：紧循环 tick() 会让 performance.now() 几乎不走，
     余波期永远结束不了，测出来是"假死循环"而不是真实行为（踩过）。 */
const fs = require('fs');
const html = fs.readFileSync(require('path').join(__dirname,'../../web/map.html'), 'utf8');
const src = [...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)][0][1];

// ---- 极简 DOM 桩 ----
let rafQ = [];
global.performance = { now: () => Date.now() };
const mkEl = (tag) => {
  const el = {
    tagName: tag, className: '', _cls: new Set(), children: [], attrs: {}, style: {},
    _text: '',
    classList: {
      add(...c){ c.forEach(x=>el._cls.add(x)); el.className=[...el._cls].join(' '); },
      remove(...c){ c.forEach(x=>el._cls.delete(x)); el.className=[...el._cls].join(' '); },
      contains(c){ return el._cls.has(c); },
      toggle(c,on){ on?el._cls.add(c):el._cls.delete(c); el.className=[...el._cls].join(' '); }
    },
    setAttribute(k,v){ el.attrs[k]=v; },
    getAttribute(k){ return el.attrs[k]; },
    addEventListener(t,f){ (el._ev=el._ev||{})[t]=f; },
    appendChild(c){ el.children.push(c); return c; },
    set textContent(v){ el._text=v; }, get textContent(){ return el._text; },
    set innerHTML(v){ el._html=v; }, get innerHTML(){ return el._html; },
    getBoundingClientRect(){ return {left:0,top:0,width:1200,height:760}; },
    querySelectorAll(){ return []; },
    get firstChild(){ return el.children[0]; }
  };
  return el;
};
const doc = {
  getElementById(id){
    if(!doc._c) doc._c = {};
    if(!doc._c[id]){
      const e = mkEl('div');
      if(id==='svg'){ e.viewBox='0 0 1200 760'; e._w=1200; e._h=760; }
      doc._c[id]=e;
    }
    return doc._c[id];
  },
  createElementNS(ns,t){ return mkEl(t); },
  createElement(t){ return mkEl(t); },
  addEventListener(){},
  removeEventListener(){},
  querySelectorAll(){ return []; }
};
global.document = doc;
global.window = {
  __nodeEls: [],
  __edgeEls: [],
  addEventListener(){},
  localStorage: { getItem:()=>null, setItem(){}, removeItem(){} },
  requestAnimationFrame(fn){ rafQ.push(fn); return rafQ.length; },
  cancelAnimationFrame(){},
  setTimeout: (fn,ms)=>setTimeout(fn,ms),
  clearTimeout: (h)=>clearTimeout(h),
  fetch: () => Promise.resolve({ json: () => Promise.resolve({ nodes: [], edges: [], total: 0 }) })
};
global.requestAnimationFrame = window.requestAnimationFrame;
global.cancelAnimationFrame = window.cancelAnimationFrame;
global.localStorage = window.localStorage;
global.fetch = window.fetch;

// ---- 注入并抓取内部函数 ----
const wrapped = src + `
;global.__api = { tick, wake, startDrag, dragMove, dragEnd, initGraph, isFree, inSettle,
  get nodes(){return nodes;}, set nodes(v){nodes=v;},
  get byId(){return byId;}, set byId(v){byId=v;},
  get edges(){return edges;}, set edges(v){edges=v;},
  get alpha(){return alpha;}, set alpha(v){alpha=v;},
  get dragNode(){return dragNode;}, set dragNode(v){dragNode=v;},
  get dragMoved(){return dragMoved;}, set dragMoved(v){dragMoved=v;},
  get releaseAt(){return releaseAt;}, set releaseAt(v){releaseAt=v;},
  get W(){return W;}, set W(v){W=v;},
  get H(){return H;}, set H(v){H=v;},
  get dragMovedFlag(){return dragMoved;},
  NODEELS: ()=>window.__nodeEls, C: (n)=>({LINK_REST,CONTACT_PAD,REPEL_FADE,DRAG_SHOVE,SETTLE_MS,RELEASE_DAMP,DAMP})
};`;
try {
  new Function(wrapped)();
  console.log("RUNTIME_EXEC_OK —— 脚本在 DOM 桩里执行成功");
} catch(e){
  console.log("RUNTIME_EXEC_FAIL:", e.message);
  console.log(e.stack.split('\n').slice(0,4).join('\n'));
  process.exit(1);
}
const api = global.__api;

// ---- 塞一个真实场景进去，驱动 tick ----
const mk=(id,x,y,r,pinned)=>({id,x,y,vx:0,vy:0,r,fixed:false,pinned:!!pinned,settleUntil:0,ph:0,stars:100,label:id});
api.nodes = [ mk("A",300,380,14), mk("B",430,380,12,false), mk("C",560,380,10,false) ];
api.byId = {}; api.nodes.forEach(n=>api.byId[n.id]=n);
api.edges = [{source:"A",target:"B",bridge:false},{source:"B",target:"C",bridge:false}];
window.__nodeEls = api.nodes.map(n=>{
  const g = mkEl('g'), pos = mkEl('g'), c = mkEl('circle'), t = mkEl('text');
  pos.appendChild(c); pos.appendChild(t); g.appendChild(pos);
  return { n, g, pos, c, t };
});
window.__edgeEls = api.edges.map(e=>({ e, el: mkEl('path') }));
window.__edgeEls.forEach(({e,el})=>{ el.setAttribute('class','edge grow'); });
api.alpha = 0;
api.dragMoved = false;
api.W = 1200; api.H = 760;   // initGraph 没跑，手动给画布尺寸（否则 clamp 会把所有节点钉在 x=14）

const C = api.C(api.nodes[0]);
console.log("");
console.log("常量确认: LINK_REST="+C.LINK_REST+" CONTACT_PAD="+C.CONTACT_PAD+" REPEL_FADE="+C.REPEL_FADE+
            " DRAG_SHOVE="+C.DRAG_SHOVE+" SETTLE_MS="+C.SETTLE_MS+" RELEASE_DAMP="+C.RELEASE_DAMP+" DAMP="+C.DAMP);

// 拖动 → 撞 → 松手
api.startDrag(api.nodes[0]);
for(let i=0;i<16;i++){ api.dragMove(300+10*(i+1), 380); }
const A = api.nodes[0];
const relX = A.x, relV = A.vx;
api.dragEnd();
console.log("");
console.log("松手后: pinned="+A.pinned+" settleUntil="+A.settleUntil+" vx="+A.vx.toFixed(2)+" inSettle="+api.inSettle());

// 跑余波
// 按真实 rAF 节奏推进：紧循环 tick 会让 performance.now() 几乎不走，
// 余波期永远结束不了，测出来的是"假死循环"而不是真实行为。
let glide=0, maxSp=0, frames=0;
const t0 = Date.now();
const FRAME = 1000/60;
while(Date.now()-t0 < 2600){
  api.tick();
  frames++;
  maxSp = Math.max(maxSp, Math.hypot(A.vx,A.vy));
  if(!isFinite(A.x)||!isFinite(A.vx)){ console.log("★ 出现 NaN/Inf！"); process.exit(1); }
  // 睡到下一帧：模拟 60fps
  const target = t0 + frames*FRAME;
  const wait = target - Date.now();
  if(wait > 1) Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, wait);
  if(frames>400) break;
}
glide = Math.hypot(A.x-relX, A.y-380);
console.log("余波跑了 "+frames+" 帧");
console.log("滑行 = "+glide.toFixed(1)+"px   峰值速度 = "+maxSp.toFixed(1)+"px/帧");
console.log("终态: vx="+A.vx.toFixed(5)+" settleUntil="+A.settleUntil+" pinned="+A.pinned);
console.log("节点 B 位置 = "+api.nodes[1].x.toFixed(1)+"  (被撞动过)");
console.log("");
const ok = glide>50 && maxSp<30 && Math.hypot(A.vx,A.vy)<0.001 && A.settleUntil===0 && isFinite(A.x);
console.log(ok ? "浏览器环境验证通过" : "浏览器环境验证未通过");
process.exit(ok?0:1);
