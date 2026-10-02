(function () {
  // 悬浮球组件（桌面壳专用）：只保留「收」球。
  // 交互分配：单击=收藏剪贴板链接；双击=打开收藏主页；右键=小菜单（主页/收藏/退出）；
  // 拖球=拖动窗口满屏放置；拖链接/标签页到球上=直接收藏；
  // 全局快捷键 Alt+Q=任意界面收剪贴板（Rust 侧处理，结果经 collect-result 事件回传）。
  // 页面与脚本由收集器后端 8732 直供，唯一真源不漂移。
  // 注意：壳窗口 url 是 http://127.0.0.1:8732（对 Tauri 属"远程页面"），
  // 必须在 capabilities 里给该域开 remote 访问，window.__TAURI__ 才会被注入。
  var COLLECTOR = window.REPO_COLLECTOR || (location.origin + "/collect");
  var KEY_POS = "repo_ball_pos";
  var WIN_W = 100, WIN_H = 100;          // 常态窗口尺寸（物理，见 tauri.conf）
  var MENU_W = 224, MENU_H = 190;        // 右键菜单时临时扩窗尺寸（逻辑）

  var C = {
    accent: "var(--accent,#B5673E)",
    accentInk: "var(--accent-ink,#FFFFFF)",
    onInk: "var(--on-ink,#FBFAF7)"
  };

  var wmod = (window.__TAURI__ && window.__TAURI__.window) ? window.__TAURI__.window : null;
  var emod = (window.__TAURI__ && window.__TAURI__.event) ? window.__TAURI__.event : null;
  var cmod = (window.__TAURI__ && window.__TAURI__.clipboardManager) ? window.__TAURI__.clipboardManager : null;
  var core = (window.__TAURI__ && window.__TAURI__.core) ? window.__TAURI__.core : null;
  function curWin() { return wmod ? wmod.getCurrentWindow() : null; }
  function Phys(x, y) {
    try { return new wmod.PhysicalPosition(x, y); } catch (e) { return null; }
  }
  function Logical(w, h) {
    try { return new wmod.LogicalSize(w, h); } catch (e) { return null; }
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

  // 主题变量：与 web/theme.css 的 :root 保持一致（换肤时同步改这里）。
  // ball.html 不加载完整 theme.css（其 body 背景会破坏窗口透明），只取变量。
  function ensureThemeVars() {
    if (document.getElementById("repo_theme_vars")) return;
    var s = document.createElement("style");
    s.id = "repo_theme_vars";
    s.textContent = ":root{" +
      "--bg:#FBFAF7;--surface:#FFFFFF;--surface-2:#F2F0EB;" +
      "--ink:#23201B;--muted:#6B655C;--faint:#9A938A;--on-ink:#FBFAF7;" +
      "--line:#E6E2DA;--line-soft:#EFECE5;" +
      "--accent:#B5673E;--accent-2:#C67B52;--accent-ink:#FFFFFF;" +
      "--accent-dim:rgba(181,103,62,.10);--accent-line:rgba(181,103,62,.34);" +
      "--r:9px;--r-lg:13px;" +
      "--sans:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif}";
    document.head.appendChild(s);
  }

  function ensureFxStyle() {
    ensureThemeVars();
    if (document.getElementById("repo_ball_fx")) return;
    var s = document.createElement("style");
    s.id = "repo_ball_fx";
    s.textContent =
      "@keyframes ballpop{0%{transform:scale(1)}40%{transform:scale(1.28)}100%{transform:scale(1)}}" +
      "@keyframes ballshake{0%,100%{translate:0 0}25%{translate:-5px 0}75%{translate:5px 0}}" +
      "@keyframes ballfadein{from{opacity:0}to{opacity:1}}";
    document.head.appendChild(s);
  }
  function fx(el, name, ms) {
    ensureFxStyle();
    el.style.animation = "none";
    void el.offsetWidth; // 重启动画
    el.style.animation = name + " " + ms + "ms ease";
    setTimeout(function () { el.style.animation = ""; }, ms + 20);
  }

  // 读剪贴板：优先 Tauri 插件（Rust 侧，不受 WebView2 权限影响），浏览器 API 兜底
  function readClipboard() {
    if (cmod && cmod.readText) {
      return cmod.readText().then(function (t) { return t; }, function () {
        return navigator.clipboard ? navigator.clipboard.readText() : Promise.reject("no-clipboard");
      });
    }
    return navigator.clipboard ? navigator.clipboard.readText() : Promise.reject("no-clipboard");
  }

  var ballEl = null; // makeBall 里赋值，供动画用

  // 单击动作：收藏剪贴板里的链接。
  // 首选事件通道（Rust 读剪贴板，clipboardManager 的 JS API 不在全局包里）；
  // 非 Tauri 环境才退回浏览器剪贴板 API。
  function collectClipboard() {
    if (emod && emod.emit) {
      var p = emod.emit("ball-collect-clipboard");
      if (p && p.catch) p.catch(function (e) { probe({ probe: "clip-emit-err", err: String(e).slice(0, 120) }); });
      return;
    }
    readClipboard().then(function (txt) {
      var m = (txt || "").match(/https?:\/\/[^\s]+/);
      if (m && isRepo(m[0])) {
        collect(m[0], "", function (ok, err, dup) {
          if (ok) { if (ballEl) fx(ballEl, "ballpop", 320); toast(dup ? "已收藏过" : "已收藏"); }
          else { if (ballEl) fx(ballEl, "ballshake", 260); toast("失败: " + (err || "")); }
        });
      } else {
        if (ballEl) fx(ballEl, "ballshake", 260);
        toast("剪贴板里没有仓库链接");
      }
    }).catch(function (e) {
      probe({ probe: "clip-err", err: String(e).slice(0, 140) });
      if (ballEl) fx(ballEl, "ballshake", 260);
      toast("无法读取剪贴板");
    });
  }

  // 双击动作：打开收藏主页。自定义命令对远程页被 ACL 拦，走事件通道（Rust 监听 ball-tap）。
  function openMainPage() {
    if (emod && emod.emit) {
      var p = emod.emit("ball-tap");
      if (p && p.catch) p.catch(function (e) { probe({ probe: "tap-emit-err", err: String(e).slice(0, 120) }); });
    } else {
      window.open(location.origin + "/", "_blank");
    }
  }

  function makeBall() {
    // 全局错误探针：任何未捕获异常都回报（诊断"点了没反应"类问题）
    window.addEventListener("error", function (e) {
      probe({ probe: "js-err", msg: String(e.message).slice(0, 160), where: String(e.filename || "").slice(-40) + ":" + e.lineno });
    });
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

    var ball = document.createElement("div");
    ball.id = "repo_ball";
    ballEl = ball;
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

    // ---------- 快捷键 / 深链的结果回传（Rust -> 球 toast） ----------
    if (emod && emod.listen) {
      emod.listen("collect-result", function (e) {
        var p = e.payload || {};
        if (p.ok) { fx(ball, "ballpop", 320); toast(p.dup ? "已收藏过" : "已收藏"); }
        else { fx(ball, "ballshake", 260); toast("失败: " + (p.error || "")); }
      });
      emod.listen("deep-link-received", function () {
        fx(ball, "ballpop", 320);
        toast("链接已收到，后台收藏中");
      });
    }

    // ---------- 拖动窗口 = 拖球（手动 setPosition，已授权） ----------
    var startScreen = null, winOrigin = null;
    var dragging = false, moved_ = false;
    var setposErrLogged = false;
    function logSetposErr(e) {
      if (setposErrLogged) return;
      setposErrLogged = true;
      probe({ probe: "setpos-err", err: String(e).slice(0, 160) });
    }

    ball.addEventListener("mousedown", function (e) {
      if (e.button !== 0) return; // 只处理左键；右键归菜单
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

    // ---------- 点按：单击收剪贴板，双击开主页（280ms 判定窗防误触） ----------
    var clickTimer = null;
    ball.addEventListener("click", function () {
      if (dragging || moved_) return;
      if (clickTimer) return;
      clickTimer = setTimeout(function () {
        clickTimer = null;
        probe({ probe: "tap-single" });
        collectClipboard();
      }, 280);
    });
    ball.addEventListener("dblclick", function () {
      if (clickTimer) { clearTimeout(clickTimer); clickTimer = null; }
      probe({ probe: "tap-double" });
      openMainPage();
    });

    // ---------- 右键菜单：临时扩窗显示，选完或点空白恢复 ----------
    // 配色跟主题卡片走：白面 + 发丝线 + 墨字，悬停陶土淡底（对应 .btn:hover）。
    var menu = null;
    function showMenu() {
      if (menu) return;
      var w = curWin();
      if (!w) { openMainPage(); return; } // 非 Tauri 环境退化
      try {
        if (Logical(MENU_W, MENU_H)) w.setSize(Logical(MENU_W, MENU_H));
        // 确保球窗口持有焦点，否则"点屏幕别处"不会触发 blur（收不起来）
        var f = w.setFocus();
        if (f && f.catch) f.catch(function () {});
      } catch (err) { probe({ probe: "menu-size-err", err: String(err).slice(0, 120) }); }
      probe({ probe: "menu-open" });
      menu = document.createElement("div");
      menu.style.cssText = "position:fixed;left:6px;top:88px;right:6px;bottom:6px;" +
        "background:var(--surface,#FFFFFF);border:1px solid var(--line,#E6E2DA);" +
        "border-radius:var(--r-lg,13px);padding:5px;z-index:2147483645;" +
        "box-shadow:0 8px 24px rgba(35,32,27,.14);font:13px/1 var(--sans);" +
        "animation:ballfadein .12s ease";
      [["打开收藏主页", openMainPageAndClose],
       ["收藏剪贴板链接", collectAndClose],
       ["退出悬浮球", quitAndClose]].forEach(function (item) {
        var it = document.createElement("div");
        it.textContent = item[0];
        it.style.cssText = "padding:9px 11px;border-radius:var(--r,9px);color:var(--ink,#23201B)" +
          ";cursor:pointer;white-space:nowrap";
        it.addEventListener("mouseenter", function () {
          it.style.background = "var(--accent-dim,rgba(181,103,62,.10))";
          it.style.color = "var(--accent,#B5673E)";
        });
        it.addEventListener("mouseleave", function () {
          it.style.background = ""; it.style.color = "var(--ink,#23201B)";
        });
        it.addEventListener("click", function (ev) {
          ev.stopPropagation();
          probe({ probe: "menu-item", label: item[0] });
          item[1]();
        });
        menu.appendChild(it);
      });
      document.body.appendChild(menu);
      // 点菜单外的空白处收起
      var shade = document.createElement("div");
      shade.id = "repo_menu_shade";
      shade.style.cssText = "position:fixed;inset:0;z-index:2147483644";
      shade.addEventListener("mousedown", function () { hideMenu(); });
      document.body.appendChild(shade);
    }
    function hideMenu() {
      if (!menu) return;
      menu.remove(); menu = null;
      var s = document.getElementById("repo_menu_shade");
      if (s) s.remove();
      var w = curWin();
      if (w) { try { w.setSize(Logical(WIN_W, WIN_H)); } catch (err) {} }
    }
    // 点球窗口以外的任何地方 = 球窗口失焦 → 自动收起菜单（弹层的标准关法）
    if (emod && emod.listen) {
      emod.listen("tauri://blur", function () {
        if (menu) { probe({ probe: "menu-blur-close" }); hideMenu(); }
      });
    }
    // Esc 也能收起
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && menu) hideMenu();
    });
    function openMainPageAndClose() { hideMenu(); openMainPage(); }
    function collectAndClose() { hideMenu(); collectClipboard(); }
    function quitAndClose() {
      hideMenu();
      var w = curWin();
      // 首选直接销毁窗口（最后一个窗口关闭时 Tauri 自动退出），
      // 事件通道 ball-quit 作兜底（Rust 收到后 exit(0)）。
      if (w && w.destroy) {
        try {
          var pr = w.destroy();
          if (pr && pr.catch) pr.catch(function (e) { emitQuit(e); });
          return;
        } catch (err) { emitQuit(err); }
      } else { emitQuit("no-destroy"); }
    }
    function emitQuit(why) {
      probe({ probe: "quit-fallback", why: String(why).slice(0, 120) });
      if (emod && emod.emit) {
        var p = emod.emit("ball-quit");
        if (p && p.catch) p.catch(function (e) { probe({ probe: "quit-emit-err", err: String(e).slice(0, 120) }); });
      }
    }
    ball.addEventListener("contextmenu", function (e) {
      e.preventDefault();
      showMenu();
    });

    // ---------- 拖入链接直接收藏 ----------
    ball.addEventListener("dragover", function (e) { e.preventDefault(); ball.style.transform = "scale(1.12)"; });
    ball.addEventListener("dragleave", function () { ball.style.transform = ""; });
    ball.addEventListener("drop", function (e) {
      e.preventDefault(); ball.style.transform = "";
      var txt = e.dataTransfer.getData("text/uri-list") || e.dataTransfer.getData("text/plain") || "";
      var m = txt.match(/https?:\/\/[^\s]+/);
      if (m && isRepo(m[0])) {
        collect(m[0], "", function (ok, err, dup) {
          if (ok) { fx(ball, "ballpop", 320); toast(dup ? "已收藏过" : "已收藏"); }
          else { fx(ball, "ballshake", 260); toast("失败: " + (err || "")); }
        });
      } else {
        fx(ball, "ballshake", 260);
        toast("拖入的链接不是仓库地址");
      }
    });
  }

  if (document.readyState !== "loading") makeBall();
  else document.addEventListener("DOMContentLoaded", makeBall);
})();
