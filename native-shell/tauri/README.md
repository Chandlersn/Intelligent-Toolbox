# Tauri 桌面壳（收藏箱）

桌面端常驻悬浮球：一个无边框、透明、钉在最前的窗口，里面只跑 `ball.js`。复制 GitHub / GitLab 链接，点球或拖上去即收藏。

## 目录
```
tauri/
  package.json          # 前端脚本（tauri dev / build）
  src/
    host.html           # WebView 入口：加载 ball.js
    ball.js             # 原样复用，未改动
  src-tauri/
    Cargo.toml
    build.rs
    tauri.conf.json     # 窗口：透明 + alwaysOnTop + 无装饰
    capabilities/default.json
    src/main.rs
```

## 构建前提
- Rust 工具链：`rustup` 安装（https://rustup.rs）
- Node.js
- Tauri CLI：`npm install` 后本地可用 `npx tauri`

## 运行
```bash
# 1. 确保收集器后端在跑（D:\智能收藏箱\server.py :8732）
# 2. 安装依赖
npm install
# 3. 开发模式（会起一个静态服务器托管 src/，Tauri 窗口加载 host.html）
npm run dev
# 4. 打包（bundle.active 已关，可改回 true 并放图标后发布）
npm run build
```

## 关键点
- `host.html` 里 `window.REPO_COLLECTOR = "http://127.0.0.1:8732/collect"`：因为窗口来自 `localhost:4321`，需显式指向收集器，否则球会 POST 到错误源。
- 收集器 `server.py` 已支持 `OPTIONS` 预检 + CORS `*`，跨源 POST 不会被浏览器拦截。
- 球的拖拽、剪贴板读取、拖入链接等交互**全部沿用 web 版**，原生壳零逻辑改动。

## 系统深链直达（桌面）
壳注册了 `repocollector://` 协议。在浏览器/终端里点（或运行）下面任一链接，壳会被唤起并把链接甩给收集器：

```
repocollector://collect?url=https://github.com/owner/repo
repocollector://collect?text=快看这个 https://github.com/owner/repo 不错
```

- `main.rs` 用 `tauri-plugin-deep-link` 注册 `repocollector` scheme，监听 `on_open_url`，
  解析出 `url=` / `text=` 后 POST 到 `COLLECTOR_BASE/collect`（由服务端抽取链接）。
- macOS：在 `bundle.macOS.associatedSchemes` 声明；Windows：在 `bundle.windows.protocol.schemes` 声明。
- `host.html` 监听 `deep-link-received` 事件，球浮窗弹出「已收到收藏链接」提示。
- 冷启动带链接进入、运行中再次被唤起，两种路径都覆盖。

## 进阶（可选）
- 若要全屏透明叠加窗 + 仅球区域可点（其余点击穿透），可在 `main.rs` 用 `Window::set_ignore_cursor_events(true)` 配合球 hover 切换。本骨架先用 280×360 小窗，确保面板能完整显示。
- 若要壳完全独立（不依赖外部后端），可在 Rust 侧用一个轻量 HTTP 服务处理 `/collect`，复用 `server.py` 的逻辑。

