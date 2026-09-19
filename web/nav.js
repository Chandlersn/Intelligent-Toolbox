/* 收藏箱 · 统一导航（底部标签栏）
 * 每个主页面引入本文件即可获得一致的跳转导航，当前页面自动高亮。
 * 通过 data-nav 标示当前页；embed/host/ball-demo 不引入本文件，保持纯净。
 */
(function () {
  // 可选口令：后端配了 REPO_TOKEN 时，前端把口令随请求带上。
  // 口令存在 localStorage.repo_token（本机自用；不提交、不写进页面源码）。
  // 没配置口令时这段完全不介入 —— 不给默认用法增加任何东西。
  (function installToken() {
    var t = null;
    try { t = localStorage.getItem("repo_token"); } catch (e) {}
    if (!t || window.__repoTokenInstalled) return;
    window.__repoTokenInstalled = true;
    var orig = window.fetch;
    window.fetch = function (input, init) {
      try {
        init = init || {};
        var base = init.headers || (input && typeof input === "object" ? input.headers : undefined);
        var h = new Headers(base || {});
        h.set("X-Collector-Token", t);
        init.headers = h;
      } catch (e) {}
      return orig.call(this, input, init);
    };
  })();

  /* 观感应用（设置页驱动）：明暗 / 主题色 / 字体 / 字号，统一在 tool body 阶段应用。
   * 供设置页 live 调整：window.applyAppearance() */
  function readAppearance() {
    var a = {};
    try { a = JSON.parse(localStorage.getItem("appearance") || "{}") || {}; } catch (e) {}
    return a;
  }
  function applyAppearance(nosignal) {
    var a = readAppearance();
    var th = a.theme || "system";
    var dark = th === "dark" ||
      (th === "system" && window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches);
    var doc = document.documentElement;
    doc.setAttribute("data-theme", dark ? "dark" : "light");
    if (a.accent) doc.setAttribute("data-accent", a.accent); else doc.removeAttribute("data-accent");
    if (a.font) doc.setAttribute("data-font", a.font); else doc.removeAttribute("data-font");
    var size = ({ small: 0.92, large: 1.08 })[a.size] || 1;
    doc.style.zoom = size;
    // 被内嵌在壳 iframe 里时，把外观变更通知给父窗，让壳工具栏配色即时跟随
    if (!nosignal) {
      try {
        if (window.self !== window.top) window.parent.dispatchEvent(new Event("app:appearance"));
      } catch (e) {}
    }
  }
  applyAppearance();
  window.applyAppearance = applyAppearance;
  // 壳广播观感变更（在别的常驻 iframe 里改过）时，本页也重应用最新观感。
  // nosignal=true 避免把信号再回发给父窗，防止 ping-pong 无限循环。
  try {
    if (window.self !== window.top) window.addEventListener("app:appearance", function () { applyAppearance(true); });
  } catch (e) {}

  // 跨页跳转网关：被嵌在壳 iframe 里时交由父窗切 tab（保持各页常驻），独立打开时整页跳。
  window.goPage = function (href) {
    try {
      if (window.self !== window.top && window.parent && window.parent.__shellNav) {
        window.parent.__shellNav(href);
        return;
      }
    } catch (e) {}
    location.href = href;
  };

  var PAGES = [
    { key: "collect",   href: "/",                label: "收藏",   short: "收" },
    { key: "cards",     href: "/cards.html",      label: "卡片",   short: "卡" },
    { key: "explore",   href: "/explore.html",    label: "探索",   short: "索" },
    { key: "recommend", href: "/recommend.html",  label: "推荐",   short: "荐" },
    { key: "profile",   href: "/profile.html",    label: "画像",   short: "像" },
    { key: "settings",  href: "/settings.html",   label: "设置",   short: "设" }
  ];

  // 当前页判定：优先 data-nav，其次按路径匹配
  function currentKey() {
    var el = document.querySelector("[data-nav]");
    if (el && el.getAttribute("data-nav")) return el.getAttribute("data-nav");
    var p = location.pathname;
    if (p === "/" || p === "/index.html") return "collect";
    for (var i = 0; i < PAGES.length; i++) {
      if (p.indexOf(PAGES[i].key + ".html") > -1) return PAGES[i].key;
    }
    return "";
  }

  function build() {
    if (document.getElementById("repo-nav")) return;
    var cur = currentKey();

    var bar = document.createElement("nav");
    bar.id = "repo-nav";
    bar.className = "repo-nav";

    var html = "";
    PAGES.forEach(function (pg) {
      var active = pg.key === cur ? " active" : "";
      html +=
        '<a class="repo-nav__item' + active + '" href="' + pg.href + '">' +
          '<span class="repo-nav__ic" aria-hidden="true">' + pg.short + "</span>" +
          '<span class="repo-nav__tx">' + pg.label + "</span>" +
        "</a>";
    });
    bar.innerHTML = html;

    // 避免遮挡页面底部内容：给 body 留白
    var s = document.createElement("style");
    s.textContent =
      ".repo-nav{position:fixed;left:0;right:0;bottom:0;z-index:9999;" +
      "display:flex;background:rgba(251,250,247,.94);backdrop-filter:saturate(160%) blur(10px);" +
      "border-top:1px solid var(--line,#E6E2DA);padding-bottom:env(safe-area-inset-bottom);" +
      "max-width:760px;margin:0 auto}" +
      ".repo-nav__item{flex:1;display:flex;flex-direction:column;align-items:center;gap:2px;" +
      "padding:7px 0 6px;text-decoration:none;color:var(--muted,#6B655C);font-size:11px;line-height:1}" +
      ".repo-nav__ic{width:22px;height:22px;border-radius:7px;display:flex;align-items:center;justify-content:center;" +
      "font-size:13px;font-weight:600;background:var(--surface-2,#F2F0EB);color:var(--faint,#9A938A);" +
      "border:1px solid var(--line,#E6E2DA)}" +
      ".repo-nav__item.active{color:var(--accent,#B5673E)}" +
      ".repo-nav__item.active .repo-nav__ic{background:var(--accent,#B5673E);color:#fff;border-color:var(--accent,#B5673E)}" +
      ".repo-nav__tx{font-size:11px}" +
      "@media (min-width:761px){.repo-nav{right:auto;left:50%;transform:translateX(-50%);border-left:1px solid var(--line,#E6E2DA);border-right:1px solid var(--line,#E6E2DA)}}";
    document.head.appendChild(s);
    document.body.appendChild(bar);

    // 给 body 底部留白，防止被标签栏挡住（兼容不同页面已有的 padding）
    var sm = document.createElement("style");
    sm.textContent = "body{padding-bottom:calc(60px + env(safe-area-inset-bottom)) !important}";
    document.head.appendChild(sm);
  }

  // 被嵌在壳 iframe 里时不建底部导航/留白（外观已在上方 applyAppearance 应用过）——
  // 但独立打开页面（非 iframe）时按原样建导航。
  if (window.self !== window.top) {
    /* 壳内嵌：只保留外观，底部标签栏由壳工具栏承担 */
  } else if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", build);
  } else {
    build();
  }
})();

/* ───────────────────────────────────────────────────────────────
 * 仓库更新反馈：本地比对 GitHub 元数据后落下的「事件」，做成顶部提示条。
 * 不擅自联网 —— 只在卡片页且距上次检查超过 24h、且没有未读时才自动跑一次；
 * 其余时候靠用户点「检查更新」主动触发。事件可点「知道了」消除。
 * ─────────────────────────────────────────────────────────────── */
(function () {
  function el(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function removeBanner() { var b = el("upd-banner"); if (b) b.remove(); }

  // 暴露给卡片页：repo_id -> [events]，渲染角标用
  window.__repoUpdates = window.__repoUpdates || {};

  function showBanner(data) {
    removeBanner();
    if (!data || !data.events || !data.events.length) {
      window.__repoUpdates = {};
      window.dispatchEvent(new CustomEvent("repo:updates", { detail: { events: [], byRepo: {} } }));
      return;
    }
    var byRepo = {};
    data.events.forEach(function (e) { (byRepo[e.repo_id] = byRepo[e.repo_id] || []).push(e); });
    window.__repoUpdates = byRepo;
    var b = document.createElement("div");
    b.id = "upd-banner";
    b.className = "upd-banner";
    var head = data.events.slice(0, 3).map(function (e) { return e.summary; }).join("；");
    if (data.events.length > 3) head += " 等";
    b.innerHTML =
      '<span class="upd-t">有 ' + data.events.length + " 个仓库有更新</span>" +
      '<span class="upd-list">' + esc(head) + "</span>" +
      '<a class="upd-go" id="upd-go">查看</a>' +
      '<a class="upd-go" id="upd-check">检查更新</a>' +
      '<a class="upd-go" id="upd-ok">知道了</a>';
    document.body.insertBefore(b, document.body.firstChild);
    el("upd-go").onclick = function () { goPage("/cards.html"); };
    el("upd-check").onclick = function () { checkNow(); };
    el("upd-ok").onclick = function () { markSeen(); };
    window.dispatchEvent(new CustomEvent("repo:updates", { detail: { events: data.events, byRepo: byRepo } }));
  }

  function loadUpdates() {
    fetch("/api/updates").then(function (r) { return r.json(); })
      .then(showBanner).catch(function () {});
  }

  function markSeen() {
    fetch("/api/updates/seen", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" })
      .then(function () { removeBanner(); window.__repoUpdates = {};
        window.dispatchEvent(new CustomEvent("repo:updates", { detail: { events: [], byRepo: {} } })); })
      .catch(function () {});
  }

  function checkNow() {
    fetch("/api/updates/check", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d && d.ok) {
          loadUpdates();
          if (d.new_events) {
            // 轻提示交给各页的 toast（如有）；这里不强依赖
          }
        }
      }).catch(function () {});
  }

  function maybeAutoCheck() {
    // 只在卡片页主动检查，避免多页面并发打 GitHub；且没未读时才查，不打扰正在阅读的人
    if (location.pathname.indexOf("cards.html") < 0) return;
    fetch("/api/updates").then(function (r) { return r.json(); }).then(function (d) {
      if (d && d.events && d.events.length) return;       // 有未读就不自动查
      var stale = !d.last_checked_at;
      if (!stale) {
        try {
          var dt = new Date(d.last_checked_at);
          stale = (Date.now() - dt.getTime()) > 24 * 3600 * 1000;
        } catch (e) { stale = true; }
      }
      if (stale) checkNow();
    }).catch(function () {});
  }

  loadUpdates();
  maybeAutoCheck();
})();

/* ───────────────────────────────────────────────────────────────
 * 自绘模态框（替换原生 confirm / alert）：适配观感主题（明暗/主题色/字体/字号）。
 * 统一注入一个覆盖层 + 居中对话框，按钮区含「取消 / 主操作」，危险操作主按钮用红色。
 * 暴露：window.showConfirm(opts) -> Promise<boolean>
 *        window.showAlert(opts)   -> Promise<boolean>
 * opts: { title, message, confirmText, cancelText, danger }
 * ─────────────────────────────────────────────────────────────── */
(function () {
  if (window.__mbxInstalled) return;
  window.__mbxInstalled = true;

  var MBOX_TAG = "__wb-mbx__";
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function ensure() {
    var old = document.getElementById(MBOX_TAG);
    if (old) old.remove();
    var css = document.createElement("style");
    css.textContent =
      "#" + MBOX_TAG + "{position:fixed;inset:0;z-index:100000;display:flex;align-items:center;justify-content:center;" +
      "background:rgba(0,0,0,.38);backdrop-filter:blur(2px);opacity:0;transition:opacity .16s ease}" +
      "#" + MBOX_TAG + ".on{opacity:1}" +
      "#" + MBOX_TAG + " .wb-mbx-card{min-width:300px;max-width:min(88vw,420px);border-radius:14px;" +
      "background:var(--card,#FFFDF8);border:1px solid var(--line,#E6E2DA);box-shadow:0 18px 50px rgba(0,0,0,.22);" +
      "padding:20px 20px 16px;transform:translateY(10px) scale(.97);transition:transform .16s ease}" +
      "#" + MBOX_TAG + ".on .wb-mbx-card{transform:none}" +
      "#" + MBOX_TAG + " .wb-mbx-title{font-size:15px;font-weight:600;color:var(--ink,#262019);margin:0 0 8px}" +
      "#" + MBOX_TAG + " .wb-mbx-msg{font-size:13px;line-height:1.6;color:var(--muted,#6B655C);margin:0 0 18px;white-space:pre-wrap;word-break:break-word}" +
      "#" + MBOX_TAG + " .wb-mbx-actions{display:flex;justify-content:flex-end;gap:10px}" +
      "#" + MBOX_TAG + " .wb-mbx-btn{min-width:76px;padding:8px 16px;border-radius:10px;border:1px solid var(--line,#E6E2DA);" +
      "background:var(--surface-2,#F2F0EB);color:var(--ink,#262019);font-size:13px;cursor:pointer}" +
      "#" + MBOX_TAG + " .wb-mbx-btn:hover{background:var(--surface,#F7F5F0)}" +
      "#" + MBOX_TAG + " .wb-mbx-btn.primary{background:var(--accent,#B5673E);border-color:var(--accent,#B5673E);color:#fff}" +
      "#" + MBOX_TAG + " .wb-mbx-btn.danger{background:#C0392B;border-color:#C0392B;color:#fff}" +
      "#" + MBOX_TAG + " .wb-mbx-btn.danger:hover{background:#A93226}";
    document.head.appendChild(css);

    var ov = document.createElement("div");
    ov.id = MBOX_TAG;
    ov.addEventListener("click", function (ev) { if (ev.target === ov) closeBox(); });
    document.body.appendChild(ov);
    return ov;
  }

  function closeBox(v) {
    var nav = document.getElementById(MBOX_TAG);
    if (!nav || !nav._resolve) return;
    var res = nav._resolve;
    nav._resolve = null;
    nav.classList.remove("on");
    setTimeout(function () { nav.innerHTML = ""; }, 180);
    res(v);
  }

  function show(opts, confirmAndVal) {
    opts = opts || (typeof opts === "string" ? { message: opts } : {});
    var nav = ensure();
    nav._resolve = null;                                   // 每次重建解析器
    nav.innerHTML =
      '<div class="wb-mbx-card">' +
        (opts.title ? '<p class="wb-mbx-title">' + esc(opts.title) + "</p>" : "") +
        '<p class="wb-mbx-msg">' + esc(opts.message || "") + "</p>" +
        '<div class="wb-mbx-actions">' +
          '<button class="wb-mbx-btn" data-act="cancel">' + esc(opts.cancelText || "取消") + "</button>" +
          '<button class="wb-mbx-btn ' + (opts.danger ? "danger" : "primary") + '" data-act="ok" autofocus>' +
            esc(opts.confirmText || "确定") + "</button>" +
        "</div>" +
      "</div>";
    nav.classList.add("on");
    var okBtn = nav.querySelector('[data-act="ok"]');
    okBtn.addEventListener("click", function () { closeBox(true); });
    nav.querySelector('[data-act="cancel"]').addEventListener("click", function () { closeBox(false); });
    okBtn.focus();
    document.addEventListener("keydown", kd, true);
    var p = new Promise(function (res) { nav._resolve = res; });
    return p;
  }

  function kd(e) { if (e.key === "Escape") closeBox(false); }

  // 等待确认（confirm）或仅提示（alert）：返回 Promise<boolean>
  window.showConfirm = function (opts) { return show(opts, true); };
  window.showAlert = function (opts) { return show(opts, true); };
})();
