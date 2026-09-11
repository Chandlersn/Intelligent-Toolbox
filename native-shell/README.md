# 收藏箱 · 原生壳（M5）

把 web 层的悬浮球（`ball.js`）套进原生容器，做到**跨 App 常驻**——这是「随时一键拖入」的最终形态。

## 一、核心原则：ball.js 是复用的 webview 组件，原生壳只是薄容器

无论桌面（Tauri / Electron）还是移动（Android overlay），壳里跑的都是**同一段 `ball.js` + 同一个 `/collect` 端点**：

```
┌─────────────────────────────────────────────┐
│ 原生壳（薄容器）                              │
│   ├─ 窗口/悬浮窗管理（alwaysOnTop / 叠加层）  │
│   └─ WebView ── 加载 ball.js（原样复用）       │
│                   │ POST /collect            │
│                   ▼                           │
│         收集器后端（D:\智能收藏箱\server.py）   │
└─────────────────────────────────────────────┘
```

- 壳**不重写**任何收集逻辑，只负责「把球钉在屏幕上、跨 App 可见」。
- `ball.js` 通过 `window.REPO_COLLECTOR` 指收集端点；同源加载可省略，跨源加载需显式设置。
- 升级球的能力时，只改 `ball.js` 一处，所有壳自动受益——**不返工**。

## 二、两个现成骨架

| 骨架 | 目录 | 适用 | 构建前提 |
|---|---|---|---|
| 桌面壳（Tauri v2） | `tauri/` | Windows / macOS / Linux 上常驻悬浮球 | Rust + Node + Tauri CLI |
| Android 叠加层 | `android/` | 手机上跨所有 App 的悬浮球（SYSTEM_ALERT_WINDOW） | Android SDK + Android Studio |

> iOS 受系统限制无法做全局悬浮窗，不在范围内。

## 三、关于「真跨所有 App」

- 桌面 Tauri：窗口钉在最前（`alwaysOnTop`），可覆盖其它窗口——已是跨 App 常驻。
- Android：用 `TYPE_APPLICATION_OVERLAY` 悬浮窗，球浮在任意 App 之上，复制链接点球即可收。
- 两者都依赖「收集器后端在运行」。开发期后端跑在本机 `http://127.0.0.1:8732`；发布期应把后端部署到可访问地址，或在壳内内嵌同进程服务。

## 四、未来：壳内嵌后端（可选）

要让悬浮球完全独立、不依赖外部后端，可把 `server.py` 的行为编译进壳（如 Rust 侧直接处理 `/collect`）。本版先保持「壳=薄容器 + 外部后端」的清晰边界，便于维护和升级。
