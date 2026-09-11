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

  var PAGES = [
    { key: "collect",   href: "/",                label: "收藏",   short: "收" },
    { key: "cards",     href: "/cards.html",      label: "卡片",   short: "卡" },
    { key: "map",       href: "/map.html",        label: "图谱",   short: "图" },
    { key: "recommend", href: "/recommend.html",  label: "推荐",   short: "荐" },
    { key: "share",     href: "/share.html",      label: "分享",   short: "享" },
    { key: "profile",   href: "/profile.html",    label: "画像",   short: "像" }
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

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", build);
  } else {
    build();
  }
})();
