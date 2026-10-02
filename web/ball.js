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

  // 点球：打开收藏主页面（走 Rust 命令调系统默认浏览器；无 Tauri 环境退回 window.open）
  function openMainPage() {
    if (core && core.invoke) {
      core.invoke("open_main_page");
    } else {
      window.open(location.origin + "/", "_blank");
    }
  }

  function makeBall() {
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

    var downPos = null, dragging = false;

    ball.addEventListener("mousedown", function (e) {
      e.preventDefault();
      var w = curWin();
      if (!w || !w.startDragging) return;
      try {
        w.outerPosition().then(function (p) { downPos = { x: p.x, y: p.y }; });
        w.startDragging(); // 拖动整个窗口 = 球随窗口走，可任意摆放
      } catch (err) {}
      dragging = true;
    });

    ball.addEventListener("mouseup", function () {
      if (!dragging) return;
      dragging = false;
      var w = curWin();
      if (!w || !w.outerPosition) { openMainPage(); return; }
      try {
        w.outerPosition().then(function (p) {
          var moved = !downPos || Math.abs(p.x - downPos.x) > 3 || Math.abs(p.y - downPos.y) > 3;
          localStorage.setItem(KEY_POS, JSON.stringify({ x: p.x, y: p.y }));
          if (!moved) openMainPage(); // 没移动 = 点按 → 打开收藏页面
        });
      } catch (err) { openMainPage(); }
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
