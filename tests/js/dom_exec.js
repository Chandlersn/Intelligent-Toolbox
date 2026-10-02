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
;global.__api = { tick, wake, loop, startDrag, dragMove, dragEnd, initGraph, isFree, inSettle,
  get nodes(){return nodes;}, set nodes(v){nodes=v;},
  get byId(){return byId;}, set byId(v){byId=v;},
  get edges(){return edges;}, set edges(v){edges=v;},
  get alpha(){return alpha;}, set alpha(v){alpha=v;},
  get dragNode(){return dragNode;}, set dragNode(v){dragNode=v;},
  get dragMoved(){return dragMoved;}, set dragMoved(v){dragMoved=v;},
  get releaseAt(){return releaseAt;}, set releaseAt(v){releaseAt=v;},
  get W(){return W;}, set W(v){W=v;},
  get H(){return H;}, set H(v){H=v;},
  NODEELS: ()=>window.__nodeEls, C: (n)=>({LINK_REST,CONTACT_PAD,REPEL_FADE,DRAG_SHOVE,SETTLE_MS,RELEASE_DAMP,DAMP,HOME_K,HOME_SOFT})
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
// mk 必须带上 bx/by：新模型里 pinned 节点靠回锚弹簧回"家"，没有 bx/by 会算成 NaN。
// （真实页面 initGraph 会给每个节点设 bx/by；桩里要手动补齐，否则测的是残缺逻辑。）
const mk=(id,x,y,r,pinned)=>({id,x,y,vx:0,vy:0,r,fixed:false,pinned:!!pinned,settleUntil:0,
  bx:x,by:y,ph:0,stars:100,label:id,dvx:0,dvy:0});

// 一个通用的"按 60fps 推进"驱动器：紧循环 tick 会让 performance.now() 几乎不走，
// 余波期永远结束不了，测出来是"假死循环"而不是真实行为（踩过）。
function drive(ms, onFrame){
  const t0 = Date.now(), FRAME = 1000/60;
  let frames = 0;
  while(Date.now()-t0 < ms){
    api.tick();
    frames++;
    if(onFrame) onFrame(frames);
    if(!isFinite(api.nodes[0].x)){ console.log("★ 出现 NaN/Inf！"); process.exit(1); }
    const target = t0 + frames*FRAME;
    const wait = target - Date.now();
    if(wait > 1) Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, wait);
    if(frames>500) break;
  }
  return frames;
}
// 驱动到"真正静止"：alpha 归零的那一帧，代码会把所有速度清零（见 tick 末尾），
// 所以 alpha===0 即代表全图速度精确为 0。用来判断"动效结束后是否真的停干净"。
// onFrame 可选：每帧回调，用来跟踪峰值速度 / 最大位移等过程量。
function driveUntilRest(maxMs, onFrame){
  const t0 = Date.now(), FRAME = 1000/60;
  let frames = 0;
  while(Date.now()-t0 < maxMs){
    api.tick();
    frames++;
    if(onFrame) onFrame(frames);
    if(!isFinite(api.nodes[0].x)){ console.log("★ 出现 NaN/Inf！"); process.exit(1); }
    const target = t0 + frames*FRAME;
    const wait = target - Date.now();
    if(wait > 1) Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, wait);
    if(api.alpha === 0) break;     // alpha=0 且上一帧已清零速度 → 真静止
    if(frames>800) break;
  }
  return frames;
}
function harness(nodes, edges){
  api.nodes = nodes;
  api.byId = {}; nodes.forEach(n=>api.byId[n.id]=n);
  api.edges = edges;
  window.__nodeEls = nodes.map(n=>{
    const g = mkEl('g'), pos = mkEl('g'), c = mkEl('circle'), t = mkEl('text');
    pos.appendChild(c); pos.appendChild(t); g.appendChild(pos);
    return { n, g, pos, c, t };
  });
  window.__edgeEls = edges.map(e=>({ e, el: mkEl('path') }));
  window.__edgeEls.forEach(({e,el})=>{ el.setAttribute('class','edge grow'); });
  api.alpha = 0; api.dragMoved = false;
  api.W = 1200; api.H = 760;   // initGraph 没跑，手动给画布尺寸（否则 clamp 会把所有节点钉在 x=14）
}
// 模拟真实匀速拖拽（不跑物理）：每帧移动固定 (dx,dy)，dvx/dvy 恒为 (dx,dy)，和松手速度挂钩。
function drag(node, dx, dy, frames){
  api.startDrag(node);
  for(let i=0;i<frames;i++){ api.dragMove(node.x + dx, node.y + dy); }
}
// 拖拽"带着物理跑"：每帧先 dragMove 再 tick，用来观察"拖一个、连着的邻居跟着动"。
function dragWithTicks(node, dx, dy, frames){
  api.startDrag(node);
  for(let i=0;i<frames;i++){ api.dragMove(node.x + dx, node.y + dy); api.tick(); }
}

const C = api.C(mk("x",0,0,10));
console.log("");
console.log("常量确认: LINK_REST="+C.LINK_REST+" CONTACT_PAD="+C.CONTACT_PAD+" REPEL_FADE="+C.REPEL_FADE+
            " DRAG_SHOVE="+C.DRAG_SHOVE+" SETTLE_MS="+C.SETTLE_MS+" RELEASE_DAMP="+C.RELEASE_DAMP+
            " DAMP="+C.DAMP+" HOME_K="+C.HOME_K+" HOME_SOFT="+C.HOME_SOFT);
let allOk = true;

// ===== 场景 1：开阔地松手 —— 惯性滑行必须肉眼可见 =====
// 单节点、无连线：纯粹验证"松手后顺着拖拽方向滑一段再停"，不被邻居的弹簧分食。
harness([ mk("A",300,380,14) ], []);
{
  const A = api.nodes[0];
  drag(A, 10, 0, 16);              // 匀速拖 16 帧 × 10px → 松手速度 8px/帧
  const relX = A.x;                // 落点
  api.dragEnd();
  const v0 = A.vx;
  let peak = 0, maxGlide = 0;
  driveUntilRest(5000, ()=>{
    const sp = Math.hypot(A.vx, A.vy);
    if(sp > peak) peak = sp;
    const gl = Math.hypot(A.x - relX, A.y - 380);
    if(gl > maxGlide) maxGlide = gl;
  });
  const glide = Math.hypot(A.x - relX, A.y - 380);
  const finalV = Math.hypot(A.vx, A.vy);
  const pass = glide > 100 && peak > 5 && finalV === 0 && api.alpha === 0 && isFinite(A.x);
  allOk = allOk && pass;
  console.log("[场景1 惯性滑行] 松手速度="+v0.toFixed(1)+" 峰值速度="+peak.toFixed(1)+
              " 滑行="+glide.toFixed(0)+"px 终速=0="+(finalV===0)+" alpha=0="+(api.alpha===0)+
              "  →  "+(pass?"OK":"FAIL（滑行太短=松手即停）"));
}

// ===== 场景 2：松手甩向已摆(pinned)节点 —— 弹性碰撞，对方必须被撞开（不是撞墙）=====
// 旧模型：pinned 被排除在碰撞之外（invB=0）→ 撞上去像撞墙，零动量传递。
// 新模型：pinned 照样吃冲量。验证被撞的已摆节点确实位移。
harness([ mk("A",300,380,14), mk("B",480,380,12,true) ], []);   // B 已摆、无连线
{
  const A = api.nodes[0], B = api.nodes[1];
  const Bx0 = B.x;
  drag(A, 10, 0, 14);              // A: 300 → 440（距 B 还有 40px，迎面接近）
  api.dragEnd();                   // 松手：A 带着 8px/帧 滑向 B
  let Bmax = 0;
  driveUntilRest(5000, ()=>{ const d = Math.abs(B.x - Bx0); if(d > Bmax) Bmax = d; });
  const moved = Math.abs(B.x - Bx0);
  // 已摆节点被撞后会"弹开再弹回原位"（有家可回），所以看的是碰撞瞬间的峰值位移，
  // 不是最终停在哪。只要峰值明显非零，就证明它不是焊死的墙。
  const pass = Bmax > 5;
  allOk = allOk && pass;
  console.log("[场景2 撞已摆节点] B 初始家="+Bx0.toFixed(0)+" 碰撞峰值位移="+Bmax.toFixed(1)+
              "px（最终回弹至 "+moved.toFixed(1)+"px）  →  "+(pass?"OK 已摆节点会被撞开":"FAIL 仍是焊死墙"));
}

// ===== 场景 3：拖动牵引链 —— 拖一个有连线的节点，pinned 邻居要跟着动 =====
// 旧模型：边力排除 pinned → 拖一个、连着的已摆邻居纹丝不动（死的一团）。
// 新模型：边力照常作用于 pinned。验证拖拽过程中邻居被拉动。
harness([ mk("A",300,380,14), mk("B",480,380,12,true) ], [{source:"A",target:"B"}]);
{
  const A = api.nodes[0], B = api.nodes[1];
  const Bx0 = B.x;
  dragWithTicks(A, 10, 0, 14);     // 拖 A 的同时跑物理：边弹簧把 pinned B 拉过来
  const moved = Math.abs(B.x - Bx0);
  api.dragEnd();
  const pass = moved > 5;
  allOk = allOk && pass;
  console.log("[场景3 链式牵引] 拖 A 时 pinned B 被边拉动位移="+moved.toFixed(1)+
              "px  →  "+(pass?"OK 邻居跟着动":"FAIL 邻居不动"));
}

// ===== 场景 4：静止性回归 —— 摆好的图不应永远在动 =====
harness([ mk("A",300,380,14,true), mk("B",430,380,12,true) ], [{source:"A",target:"B"}]);
{
  api.alpha = 0.5; api.wake(0.5);
  const frames = driveUntilRest(5000);
  const settled = api.alpha === 0 && api.nodes.every(n=>Math.hypot(n.vx,n.vy)===0);
  const pass = settled;
  allOk = allOk && pass;
  console.log("[场景4 静止性] 摆好的图能否彻底静止(alpha=0, 全节点速度=0)  →  "+
              (pass?"OK":"FAIL")+"  (用了"+frames+"帧)");
}

// ===== 场景 5：调度器一致性 —— 必须走真实的 loop() + rAF，不能手动循环 tick() =====
// 这个场景是"松手惯性演到一半被冻住 / 图永远停不下来"的唯一可靠回归网：
// 那两个 bug 的根源都在调度器（loop 的续排条件 ≠ tick 的 alive 判据），
// 而手动循环 api.tick() 会完全绕过 loop()，所以前面 4 个场景全绿也照样漏掉。
// 桩里 rAF 只是把回调排进 rafQ，这里把它排空 —— 排空即代表调度器自己停表了。
harness([ mk("A",300,380,14), mk("B",560,300,12) ], [{source:"A",target:"B"}]);
{
  api.alpha = 0;                 // 从 0 起：wake() 之外的路径也要能被调度器接住
  api.alpha = 1;                 // 模拟 initGraph 结尾：alpha=1 然后 loop()
  api.loop();
  let frames = 0;
  while(rafQ.length && frames < 1500){ const fn = rafQ.shift(); fn(); frames++; }
  const stopped = api.alpha === 0 && rafQ.length === 0;
  const pass = stopped && frames < 1500;
  allOk = allOk && pass;
  console.log("[场景5 调度器] 用真实 loop()+rAF 驱动：自行停表=" + stopped +
              " 用了" + frames + "帧（上限1500）  →  " +
              (pass ? "OK 调度器会自己停" : "FAIL 死循环/半路冻住（loop 与 alive 判据不一致）"));
}

console.log("");
console.log(allOk ? "浏览器环境验证通过（惯性滑行 + 撞已摆节点 + 链式牵引 + 静止性 + 调度器自行停表）"
                  : "浏览器环境验证未通过");
process.exit(allOk?0:1);
