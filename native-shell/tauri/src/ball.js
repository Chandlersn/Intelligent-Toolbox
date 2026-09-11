(function () {
  // 悬浮球组件：可拖拽、可拖入链接收集、点按从剪贴板收集。跨页面/跨平台（web/webview）。
  // 用法：<script src="ball.js"></script>，可选 window.REPO_COLLECTOR 指向收集端点。
  var COLLECTOR = window.REPO_COLLECTOR || (location.origin + "/collect");
  var KEY_POS = "repo_ball_pos";

  // 颜色：先取宿主页面的主题变量，取不到再用同值兜底。
  // 球会被注入任意第三方页面（那些页面并没有 theme.css），所以必须靠 var() 第二参数兜底 ——
  // 纯写死 hex 的话，主题换色时球会是唯一跟不上的那块。
  var C = {
    accent: "var(--accent,#B5673E)",
    accentInk: "var(--accent-ink,#FFFFFF)",
    line: "var(--line,#E6E2DA)",
    surface: "var(--surface,#FFFFFF)",
    surface2: "var(--surface-2,#F2F0EB)",
    ink: "var(--ink,#23201B)",
    onInk: "var(--on-ink,#FBFAF7)"
  };

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
        "pointer-events:none;max-width:86%";
      document.body.appendChild(t);
    }
    t.textContent = msg;
    t.style.opacity = "1";
    clearTimeout(t._t);
    t._t = setTimeout(function () { t.style.opacity = "0"; }, 1600);
  }

  function openPanel() {
    var existing = document.getElementById("repo_panel");
    if (existing) { existing.remove(); return; }
    var p = document.createElement("div");
    p.id = "repo_panel";
    p.style.cssText = "position:fixed;right:16px;bottom:88px;width:min(320px,86vw);" +
      "background:" + C.surface + ";border:1px solid " + C.line + ";border-radius:13px;" +
      "box-shadow:0 18px 50px rgba(35,32,27,.18);z-index:2147483646;padding:14px;" +
      "font-family:-apple-system,'Segoe UI','PingFang SC',sans-serif";
    p.innerHTML =
      '<div style="font-size:13px;color:' + C.ink + ';opacity:.7;margin-bottom:8px">粘贴仓库地址</div>' +
      '<input id="repo_url" style="width:100%;font-size:16px;color:' + C.ink + ';background:' + C.surface2 + ';' +
      'border:1px solid ' + C.line + ';border-radius:10px;padding:12px;box-sizing:border-box;outline:none" ' +
      'placeholder="https://github.com/owner/repo">' +
      '<button id="repo_go" style="width:100%;margin-top:10px;min-height:46px;font-size:16px;' +
      'font-weight:600;color:' + C.accentInk + ';background:' + C.accent + ';border:none;border-radius:12px">收藏</button>';
    document.body.appendChild(p);
    var inp = p.querySelector("#repo_url");
    inp.focus();
    p.querySelector("#repo_go").addEventListener("click", function () {
      var u = inp.value.trim();
      if (!u) { toast("先粘贴地址"); return; }
      if (!isRepo(u)) { toast("仅支持 GitHub / GitLab 地址"); return; }
      collect(u, "", function (ok, err, dup) {
        toast(ok ? (dup ? "已收藏过" : "已收藏 ✓") : ("失败: " + (err || "")));
        if (ok) p.remove();
      });
    });
  }

  function makeBall() {
    var ball = document.createElement("div");
    ball.id = "repo_ball";
    var size = 56;
    ball.style.cssText = "position:fixed;width:" + size + "px;height:" + size + "px;border-radius:50%;" +
      "background:" + C.accent + ";color:" + C.accentInk + ";display:flex;align-items:center;justify-content:center;" +
      "font-size:20px;font-weight:600;cursor:grab;z-index:2147483647;" +
      "box-shadow:0 6px 20px rgba(181,103,62,.36);user-select:none;touch-action:none;" +
      "font-family:-apple-system,'Segoe UI',sans-serif";
    ball.textContent = "收";
    document.body.appendChild(ball);

    var pos = JSON.parse(localStorage.getItem(KEY_POS) || "null");
    var x = pos ? pos.x : window.innerWidth - size - 16;
    var y = pos ? pos.y : window.innerHeight - size - 88;
    function place(nx, ny) {
      nx = Math.max(8, Math.min(window.innerWidth - size - 8, nx));
      ny = Math.max(8, Math.min(window.innerHeight - size - 8, ny));
      ball.style.left = nx + "px";
      ball.style.top = ny + "px";
    }
    place(x, y);

    var dragging = false, sx = 0, sy = 0, ox = 0, oy = 0, moved = false;
    ball.addEventListener("pointerdown", function (e) {
      dragging = true; moved = false; ball.style.cursor = "grabbing";
      sx = e.clientX; sy = e.clientY;
      var r = ball.getBoundingClientRect(); ox = r.left; oy = r.top;
      ball.setPointerCapture(e.pointerId);
    });
    ball.addEventListener("pointermove", function (e) {
      if (!dragging) return;
      var dx = e.clientX - sx, dy = e.clientY - sy;
      if (Math.abs(dx) > 4 || Math.abs(dy) > 4) moved = true;
      place(ox + dx, oy + dy);
    });
    ball.addEventListener("pointerup", function (e) {
      if (!dragging) return;
      dragging = false; ball.style.cursor = "grab";
      var r = ball.getBoundingClientRect();
      var nx = (r.left < window.innerWidth / 2) ? 8 : window.innerWidth - size - 8;
      place(nx, r.top);
      localStorage.setItem(KEY_POS, JSON.stringify({ x: parseFloat(ball.style.left), y: parseFloat(ball.style.top) }));
      if (!moved) onTap();
    });

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

    function onTap() {
      if (navigator.clipboard && navigator.clipboard.readText) {
        navigator.clipboard.readText().then(function (txt) {
          var m = (txt || "").match(/https?:\/\/[^\s]+/);
          if (m && isRepo(m[0])) {
            collect(m[0], "", function (ok, err, dup) {
              toast(ok ? (dup ? "已收藏过" : "已收藏 ✓") : ("失败: " + (err || "")));
            });
          } else { openPanel(); }
        }).catch(function () { openPanel(); });
      } else { openPanel(); }
    }
  }

  if (document.readyState !== "loading") makeBall();
  else document.addEventListener("DOMContentLoaded", makeBall);
})();
