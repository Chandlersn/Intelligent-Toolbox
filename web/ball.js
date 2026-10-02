(function () {
  // 悬浮球组件（桌面壳专用）：只保留「收」球，不弹任何面板。
  // 拖球 = 拖动整个透明窗口（Tauri startDragging），因此能在整屏任意放置，
  // 窗口之外不挡点击。点球 = 用系统默认浏览器打开收藏主页面。
  // 页面与脚本由收集器后端 8732 直供，唯一真源不漂移。
  // 注意：壳窗口 url 是 http://127.0.0.1:8732（对 Tauri 属"远程页面"），
  // 必须在 capabilities 里给该域开 remote 访问，window.__TAURI__ 才会被注入。
  var COLLECTOR = window.REPO_COLLECTOR || (location.origin + "/collect");
  var KEY_POS = "repo_ball_pos";

  var C = {
    accent: "var(--accent,#B5673E)",
    accentInk: "var(--accent-ink,#FFFFFF)",
    onInk: "var(--on-ink,#FBFAF7)"
  };

  // Tauri v2 全局 API（远程域已授权 IPC 后可用）。不在 Tauri 里则 curWin() 为 null，
  // 此时退化为普通页面上的静态球（不影响其他用途）。
  var wmod = (window.__TAURI__ && window.__TAURI__.window) ? window.__TAURI__.window : null;
  var core = (window.__TAURI__ && window.__TAURI__.core) ? window.__TAURI__.core : null;
  function curWin() { return wmod ? wmod.getCurrentWindow() : null; }
  function Phys(x, y) {
    try { return new wmod.PhysicalPosition(x, y); } catch (e) { return null; }
  }

  function isRepo(u) {
    try {
      var p = new URL(u);
      var h = p.hostname.toLowerCase();
      if ((h === "github.com" || h.endsWith("gitlab.com")) &&
          p.pathname.split("/").filter(Boolean).length >= 2) return true;
    } catch (e) {}
    return false;
  }

  function collect(url, note, done) {
    fetch(COLLECTOR, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: url, note: note || "", source: "ball" })
    })
      .then(function (r) { return r.json(); })
      .then(function (d) { done(d && d.ok, d && d.error, d && d.dup); })
      .catch(function () { done(false, "网络异常"); });
  }

  function toast(msg) {
    var t = document.getElementById("repo_toast");
    if (!t) {
      t = document.createElement("div");
      t.id = "repo_toast";
      t.style.cssText = "position:fixed;left:50%;top:16px;transform:translateX(-50%);" +
        "background:rgba(35,32,27,.95);color:" + C.onInk + ";font:14px/1.4 -apple-system,'Segoe UI',sans-serif;" +
        "padding:10px 16px;border-radius:10px;z-index:2147483646;opacity:0;transition:opacity .25s;" +
        "pointer-events:none;max-width:86%;white-space:nowrap";
      document.body.appendChild(t);
    }
    t.textContent = msg;
    t.style.opacity = "1";
    clearTimeout(t._t);
    t._t = setTimeout(function () { t.style.opacity = "0"; }, 1600);
  }

  // 点球：打开收藏主页面。自定义命令对远程页被 ACL 拦，走事件通道（Rust 监听 ball-tap）。
  function openMainPage() {
    var em = (window.__TAURI__ && window.__TAURI__.event) ? window.__TAURI__.event : null;
    if (em && em.emit) {
      em.emit("ball-tap");
      probe({ probe: "tap-emit" });
    } else {
      window.open(location.origin + "/", "_blank");
    }
  }

  // 探针统一入口：回报到后端日志（诊断用）
  function probe(info) {
    try {
      fetch("/api/ball_probe", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(info)
      });
    } catch (e) {}
  }

  function makeBall() {
    // 环境探针：回报 Tauri API 注入状态（诊断远程页 IPC 授权是否生效）
    try {
      fetch("/api/ball_probe", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          hasTauri: !!window.__TAURI__,
          tauriKeys: window.__TAURI__ ? Object.keys(window.__TAURI__) : [],
          hasWindowMod: !!(window.__TAURI__ && window.__TAURI__.window),
          href: location.href,
          ua: navigator.userAgent.slice(0, 100)
        })
      });
    } catch (e) {}
    // IPC 实调探针：0.6s 后真调一次 outerPosition，回报成败与错误串
    setTimeout(function () {
      var w = curWin();
      if (!w || !w.outerPosition) {
        probe({ probe: "ipc", ok: false, err: "no-api" });
        return;
      }
      w.outerPosition().then(
        function (p) { probe({ probe: "ipc", ok: true, x: p.x, y: p.y }); },
        function (e) { probe({ probe: "ipc", ok: false, err: String(e).slice(0, 200) }); }
      );
      // 事件通道实调探针：emit 是否放行
      if (window.__TAURI__ && window.__TAURI__.event && window.__TAURI__.event.emit) {
        window.__TAURI__.event.emit("ball-probe-ping");
        probe({ probe: "emit", ok: true });
      } else {
        probe({ probe: "emit", ok: false, err: "no-event-mod" });
      }
    }, 600);

    var ball = document.createElement("div");
    ball.id = "repo_ball";
    var size = 60;
    // 窗口就是 100x100 的透明浮层，球居中放（left/top 固定，移动靠拖动窗口）。
    ball.style.cssText = "position:fixed;width:" + size + "px;height:" + size + "px;left:20px;top:20px;" +
      "border-radius:50%;background:" + C.accent + ";color:" + C.accentInk + ";" +
      "display:flex;align-items:center;justify-content:center;font-size:20px;font-weight:600;" +
      "cursor:grab;z-index:2147483647;box-shadow:0 6px 20px rgba(181,103,62,.36);" +
      "user-select:none;touch-action:none;font-family:-apple-system,'Segoe UI',sans-serif";
    ball.textContent = "收";
    document.body.appendChild(ball);

    // 恢复上次窗口位置（满屏任意放置）
    try {
      var pos = JSON.parse(localStorage.getItem(KEY_POS) || "null");
      var w = curWin();
      if (pos && w && Phys(pos.x, pos.y)) { w.setPosition(Phys(pos.x, pos.y)); }
      else if (!pos && w && window.screen && Phys(0, 0)) {
        // 首次：放到右下角
        w.setPosition(Phys(window.screen.availWidth - 130, window.screen.availHeight - 130));
      }
    } catch (e) {}

    // 鼠标事件探针：mousedown/mouseup/click 各回报一条（诊断事件是否到达 WebView2）
    ["mousedown", "mouseup", "click"].forEach(function (ev) {
      ball.addEventListener(ev, function (e) {
        try {
          fetch("/api/ball_probe", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ probe: "mouse", ev: ev, x: e.clientX, y: e.clientY })
          });
        } catch (err) {}
      });
    });

    // 位置定时探针（临时诊断）：每 2s 回报窗口坐标，共 15 次
    var _n = 0;
    var _posTimer = setInterval(function () {
      var w = curWin();
      if (!w || !w.outerPosition || _n++ > 15) { clearInterval(_posTimer); return; }
      w.outerPosition().then(function (p) {
        try {
          fetch("/api/ball_probe", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ probe: "pos", x: p.x, y: p.y })
          });
        } catch (e) {}
      });
    }, 2000);

    var downPos = null, dragging = false, moved_ = false;
    var startScreen = null, winOrigin = null;
    var setposErrLogged = false;
    function logSetposErr(e) {
      if (setposErrLogged) return;
      setposErrLogged = true;
      probe({ probe: "setpos-err", err: String(e).slice(0, 160) });
    }

    ball.addEventListener("mousedown", function (e) {
      // 不用系统 startDragging（透明 WebView2 窗口上实测无效），
      // 改为手动拖拽：记录起点，pointermove 里 setPosition 跟随。
      var w = curWin();
      if (!w || !w.setPosition) return;
      startScreen = { x: e.screenX, y: e.screenY };
      winOrigin = null;
      moved_ = false;
      dragging = true;
      try { ball.setPointerCapture(e.pointerId); } catch (err) {}
    });

    ball.addEventListener("pointermove", function (e) {
      if (!dragging) return;
      var dx = e.screenX - startScreen.x, dy = e.screenY - startScreen.y;
      if (!moved_ && Math.abs(dx) < 5 && Math.abs(dy) < 5) return;
      var w = curWin();
      if (!w) return;
      if (!moved_) {
        moved_ = true;
        probe({ probe: "drag-start" });
        w.outerPosition().then(function (p) {
          winOrigin = { x: p.x, y: p.y };
        });
        return;
      }
      if (!winOrigin) return; // 等起点坐标回来
      var dpr = window.devicePixelRatio || 1; // screenX 是 CSS 像素，位置是物理像素
      try {
        var np = Phys(Math.round(winOrigin.x + dx * dpr), Math.round(winOrigin.y + dy * dpr));
        var pr = w.setPosition(np);
        if (pr && pr.catch) pr.catch(logSetposErr); // ACL 拒绝时不再静默
      } catch (err) { logSetposErr(err); }
    });

    ball.addEventListener("pointerup", function () {
      if (!dragging) return;
      dragging = false;
      if (moved_) {
        var w = curWin();
        if (w) {
          try {
            w.outerPosition().then(function (p) {
              localStorage.setItem(KEY_POS, JSON.stringify({ x: p.x, y: p.y }));
              probe({ probe: "drag-end", x: p.x, y: p.y });
            }, logSetposErr);
          } catch (err) { logSetposErr(err); }
        }
      }
    });
    ball.addEventListener("click", function () {
      // 点按入口：手动拖拽不再吞事件，click 只在未拖动时触发
      if (!dragging && !moved_) { probe({ probe: "tap-click" }); openMainPage(); }
    });

    // 拖入链接直接收藏
    ball.addEventListener("dragover", function (e) { e.preventDefault(); ball.style.transform = "scale(1.12)"; });
    ball.addEventListener("dragleave", function () { ball.style.transform = ""; });
    ball.addEventListener("drop", function (e) {
      e.preventDefault(); ball.style.transform = "";
      var txt = e.dataTransfer.getData("text/uri-list") || e.dataTransfer.getData("text/plain") || "";
      var m = txt.match(/https?:\/\/[^\s]+/);
      if (m && isRepo(m[0])) {
        collect(m[0], "", function (ok, err, dup) {
          toast(ok ? (dup ? "已收藏过" : "已收藏 ✓") : ("失败: " + (err || "")));
        });
      } else { toast("拖入的链接不是仓库地址"); }
    });
  }

  if (document.readyState !== "loading") makeBall();
  else document.addEventListener("DOMContentLoaded", makeBall);
})();
