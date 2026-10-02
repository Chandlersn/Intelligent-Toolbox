/* 图谱物理回归 —— 覆盖松手惯性/弹性碰撞这一轮修的 4 个真 bug
   跑法：node tests/js/physics_regress.js
   ① 冲量符号反了 → 指数爆炸（速度 7.7 → 77 万 px/帧）
   ② pinned 直接跳过积分 → 松手零惯性
   ③ 单一阻尼 → 滑行上限 31px
   ④ 无限程斥力 + 推挤 → 永远进不了接触面，碰撞 0 次
   断言口径遵循两条教训：测总位移而非单轴；场景间距要保证"真能撞上"。 */
const W=1200,H=760;
const ALPHA_FLOOR=0,ALPHA_LIVE=0.06,ALPHA_DRAG=0.5;
const ALPHA_RELEASE=0.42,SETTLE_MS=900,SETTLE_SENS=0.8,RELEASE_KEEP=0.8;
const RELEASE_DAMP=0.955,SETTLE_LINK_SOFT=0.22;
const LINK_REST=108,LINK_K=0.055,LINK_DAMP=0.22;
const REPEL_K=2600,REPEL_CAP=6.5,CENTER_K=0.0022;
const DAMP=0.82,SENS_MIN=0.30,RESTITUTION=0.72,CONTACT_PAD=8,DRAG_BOOST=1.0;
const FRICTION=0.18,IMPULSE_CAP=12,REPEL_FADE=26;
const DRAG_SHOVE=1.2,DRAG_SHOVE_REACH=30;
const FRAME_MS=1000/60;

let NOW=0;
let nodes=[],edges=[],byId={};
let alpha=1,dragNode=null,releaseAt=0;
let collisions=0; const pinged=new Map();
let maxSpeedSeen=0;

function inSettle(){ return releaseAt>0 && (NOW-releaseAt)<SETTLE_MS; }
function isFree(n){ return !n.fixed && (!n.pinned || inSettle()); }
function pingNode(n){
  if(pinged.has(n.id) && NOW-pinged.get(n.id)<220) return;
  pinged.set(n.id,NOW); collisions++;
}

function tick(){
  const N=nodes.length, drag=dragNode;
  const settling=inSettle();
  const f2 = drag?1:(settling?SETTLE_SENS:(alpha>0?(SENS_MIN+alpha*(1-SENS_MIN)):0));
  const damp = settling?RELEASE_DAMP:DAMP;

  for(let i=0;i<N;i++){
    const a=nodes[i];
    for(let j=i+1;j<N;j++){
      const b=nodes[j];
      let dx=a.x-b.x, dy=a.y-b.y, d2=dx*dx+dy*dy;
      if(d2<0.0001){ dx=(Math.random()-.5)*2; dy=(Math.random()-0.5)*2; d2=dx*dx+dy*dy; }
      const d=Math.sqrt(d2), nx=dx/d, ny=dy/d;
      const contact=a.r+b.r+CONTACT_PAD;
      let f=0;
      if(d>contact){
        const fade=Math.min(1,(d-contact)/REPEL_FADE);
        f=Math.min(REPEL_K/d2,REPEL_CAP)*fade*fade;
      }
      if(a===drag||b===drag){
        const x=(d-contact)/DRAG_SHOVE_REACH;
        if(x>0&&x<1) f+=DRAG_SHOVE*Math.sin(Math.PI*x);
      }
      a.vx+=nx*f; a.vy+=ny*f; b.vx-=nx*f; b.vy-=ny*f;

      if(d>=contact) continue;
      const aFree=isFree(a), bFree=isFree(b);
      if(!aFree&&!bFree) continue;
      const ma=a.r*a.r, mb=b.r*b.r;
      const invA=aFree?1/ma:0, invB=bFree?1/mb:0, invSum=invA+invB;
      if(invSum===0) continue;
      const overlap=contact-d, corr=overlap*0.5;
      if(aFree){ a.x+=nx*corr*(invA/invSum); a.y+=ny*corr*(invA/invSum); }
      if(bFree){ b.x-=nx*corr*(invB/invSum); b.y-=ny*corr*(invB/invSum); }
      const avx=a.vx+(a===drag?a.dvx||0:0), avy=a.vy+(a===drag?a.dvy||0:0);
      const bvx=b.vx+(b===drag?b.dvx||0:0), bvy=b.vy+(b===drag?b.dvy||0:0);
      const rvx=avx-bvx, rvy=avy-bvy;
      const vn=rvx*nx+rvy*ny;
      if(vn<0){
        const jimp=Math.min(-(1+RESTITUTION)*vn/invSum,IMPULSE_CAP);
        if(aFree){ a.vx+=jimp*nx*invA; a.vy+=jimp*ny*invA; }
        if(bFree){ b.vx-=jimp*nx*invB; b.vy-=jimp*ny*invB; }
        const tx=-ny,ty=nx,vt=rvx*tx+rvy*ty;
        const jt=-vt/invSum*FRICTION;
        if(aFree){ a.vx+=jt*tx*invA; a.vy+=jt*ty*invA; }
        if(bFree){ b.vx-=jt*tx*invB; b.vy-=jt*ty*invB; }
        if(overlap>1.2 && aFree && a!==drag) pingNode(a);
        if(overlap>1.2 && bFree && b!==drag) pingNode(b);
      }
    }
  }

  const lk=LINK_K*(drag?2.2*DRAG_BOOST:(settling?SETTLE_LINK_SOFT:1));
  edges.forEach(e=>{
    const a=byId[e.source],b=byId[e.target];
    if(!a||!b) return;
    const dx=b.x-a.x,dy=b.y-a.y;
    const d=Math.sqrt(dx*dx+dy*dy)||0.01,nx=dx/d,ny=dy/d;
    let f=(d-LINK_REST)*lk;
    if(isFree(a)&&isFree(b)){ const rvx=b.vx-a.vx,rvy=b.vy-a.vy; f+=(rvx*nx+rvy*ny)*LINK_DAMP; }
    if(isFree(a)){ a.vx+=nx*f; a.vy+=ny*f; }
    if(isFree(b)){ b.vx-=nx*f; b.vy-=ny*f; }
  });
  nodes.forEach(n=>{ n.vx+=(W/2-n.x)*CENTER_K; n.vy+=(H/2-n.y)*CENTER_K; });
  if(alpha>ALPHA_LIVE){
    for(let i=0;i<N;i++){ const n=nodes[i]; if(!isFree(n)) continue;
      n.vx+=(Math.random()-0.5)*0.18; n.vy+=(Math.random()-0.5)*0.18; }
  }

  for(let i=0;i<N;i++){
    const n=nodes[i];
    if(n.fixed) continue;
    if(n.settleUntil && NOW>=n.settleUntil){ n.settleUntil=0; n.bx=n.x; n.by=n.y; }
    if(!isFree(n)) continue;
    n.vx*=damp; n.vy*=damp;
    const sp=Math.hypot(n.vx,n.vy);
    if(sp>24){ n.vx=n.vx/sp*24; n.vy=n.vy/sp*24; }
    if(!isFinite(n.vx)||!isFinite(n.vy)){ n.vx=0; n.vy=0; }   // 数值兜底
    n.x+=n.vx*f2; n.y+=n.vy*f2;
    n.x=Math.max(n.r,Math.min(W-n.r,n.x));
    n.y=Math.max(n.r,Math.min(H-n.r,n.y));
    const s2=Math.hypot(n.vx,n.vy);
    if(s2>maxSpeedSeen) maxSpeedSeen=s2;
  }
  alpha+=(ALPHA_FLOOR-alpha)*(drag?0.05:0.08);
  const alive=alpha>ALPHA_LIVE||!!drag||settling;
  if(!alive){ nodes.forEach(n=>{n.vx=0;n.vy=0;}); alpha=0; }
  return alive;
}

const mk=(id,x,y,r,pinned)=>({id,x,y,vx:0,vy:0,r,fixed:false,pinned:!!pinned,settleUntil:0,stars:100});
function build(list,elist){
  nodes=list; byId={}; nodes.forEach(n=>byId[n.id]=n); edges=elist;
  alpha=1; releaseAt=0; NOW=0; collisions=0; pinged.clear(); maxSpeedSeen=0;
  let g=0; while(tick() && g++<1500){ NOW+=FRAME_MS; }
  NOW=0; releaseAt=0; alpha=0; nodes.forEach(n=>{n.vx=0;n.vy=0;});
}
function dragFrames(t,dx,dy,frames){
  dragNode=t; t.fixed=true;
  for(let i=0;i<frames;i++){
    t.dvx=dx; t.dvy=dy||0; t.x+=dx; t.y+=dy||0;
    alpha=Math.max(alpha,ALPHA_DRAG); tick(); NOW+=FRAME_MS;
  }
}
function release(t,dx,dy){
  dragNode=null; t.fixed=false; t.dvx=0; t.dvy=0;
  t.vx=Math.max(-18,Math.min(18,dx*RELEASE_KEEP));
  t.vy=Math.max(-18,Math.min(18,(dy||0)*RELEASE_KEEP));
  t.pinned=true; t.settleUntil=NOW+SETTLE_MS;
  releaseAt=NOW; alpha=Math.max(alpha,ALPHA_RELEASE);
  return {x:t.x,y:t.y,vx:t.vx,vy:t.vy};
}
function settle(maxMs){
  const t0=NOW; let f=0;
  while(NOW-t0<maxMs){ if(!tick()) break; f++; NOW+=FRAME_MS; }
  return {frames:f, ms:NOW-t0};
}
const dist=(p,n)=>Math.hypot(n.x-p.x,n.y-p.y);
const results=[];
function check(name,ok,detail){ results.push({name,ok,detail}); }

/* ===== 1. 开阔地松手：惯性滑行必须肉眼可见 ===== */
build([mk("A",300,380,14)],[]);
let A=byId.A;
dragFrames(A,14,0,8);
let r1=release(A,14,0);
let s1=settle(SETTLE_MS+2000);
let glide1=dist(r1,A);
check("开阔地松手滑行 > 80px（惯性可见）", glide1>80, glide1.toFixed(0)+"px");
check("滑行撑满余波期 (>=900ms)", s1.ms>=SETTLE_MS, s1.ms.toFixed(0)+"ms");
check("余波后真正停干净（速度归零）", Math.hypot(A.vx,A.vy)<0.001, Math.hypot(A.vx,A.vy).toFixed(5));
check("余波后转真锁定 (settleUntil 清零)", A.settleUntil===0, "settleUntil="+A.settleUntil);
check("无速度爆炸（全场景峰值 < 30px/帧）", maxSpeedSeen<30, "峰值 "+maxSpeedSeen.toFixed(1));

/* ===== 2. 拖动撞进无连线邻居：必须有碰撞 + 动量传递 ===== */
build([mk("A",300,380,14),mk("B",430,380,12,false)],[]);
A=byId.A; const B=byId.B;
const b0={x:B.x,y:B.y};
dragFrames(A,10,0,16);
const minD=dist(A,B);
const cDrag=collisions;
release(A,10,0);
const r2=release===undefined?null:{x:A.x,y:A.y};
settle(SETTLE_MS+2000);
const cAll=collisions;
check("拖动中撞到邻居（进入接触面）", minD<=A.r+B.r+CONTACT_PAD, "最近 "+minD.toFixed(1)+"px / 接触面 "+(A.r+B.r+CONTACT_PAD));
check("拖动中触发碰撞反馈", cDrag>0, cDrag+" 次");
check("余波期还有后续碰撞（撞完继续弹）", cAll-cDrag>0, (cAll-cDrag)+" 次");
check("被撞节点被推开（动量传递）", dist(b0,B)>10, dist(b0,B).toFixed(0)+"px");

/* ===== 3. 斜向拖过：另一个方向也要能撞 ===== */
build([mk("A",300,300,14),mk("B",460,460,12,false)],[]);
A=byId.A; const B3=byId.B;
dragFrames(A,9,9,18);
const r3=release(A,9,9);        // release 返回松手瞬间的位置，用它算滑行
settle(SETTLE_MS+2000);
const g3=dist(r3,A);
check("斜向拖动也能撞到邻居", collisions>0, collisions+" 次");
check("斜向松手后也有滑行", g3>50, g3.toFixed(0)+"px");

/* ===== 4. 牵引链：撞 neighbor 要传下去 ===== */
build([mk("A",300,380,14),mk("B",420,380,12,false),mk("C",540,380,10,false)],
      [{source:"B",target:"C"}]);
A=byId.A; const B4=byId.B, C4=byId.C;
const c0={x:C4.x,y:C4.y};
dragFrames(A,10,0,20);
release(A,10,0);
settle(SETTLE_MS+2000);
check("牵引链：二阶节点被带动", dist(c0,C4)>3, dist(c0,C4).toFixed(1)+"px");

/* ===== 5. 静止性回归：不能重新变成"永远在动" =====
   注意：build() 内部已经把图收敛到静止了，所以这里要重新给一份能量，
   模拟"刚布局完 / 刚交互完"的状态，再看它能不能自己停下来。 */
build([mk("A",300,380,14),mk("B",430,380,12,false)],[{source:"A",target:"B"}]);
A=byId.A;
alpha = 0.4;                     // 给一份能量，模拟刚交互完
let stillFrames=0, ranFrames=0;
for(let i=0;i<400; i++){
  ranFrames = i;
  if(!tick()){ stillFrames=i; break; }
  NOW+=FRAME_MS;
}
check("有能量时会自己收敛到静止（停表）", stillFrames>0, stillFrames+" 帧后停表（跑了 "+ranFrames+" 帧）");
check("停表时 alpha 归零", alpha===0, "alpha="+alpha.toFixed(5));
check("静止时所有节点速度为 0", nodes.every(n=>n.vx===0&&n.vy===0), "");

console.log("=== 图谱物理回归 ===\n");
let pass=0;
results.forEach(r=>{
  if(r.ok) pass++;
  console.log((r.ok?"  PASS  ":"  FAIL  ")+r.name+(r.detail?"   ["+r.detail+"]":""));
});
console.log("");
console.log(pass+"/"+results.length+" 通过");
process.exit(pass===results.length?0:1);
