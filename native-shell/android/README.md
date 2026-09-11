# Android 原生壳（收藏箱）

两个入口，覆盖手机上的两种收藏场景：

1. **全局悬浮球**（OverlayService）：用 `SYSTEM_ALERT_WINDOW` 在**任意 App 之上**叠加一个透明层，WebView 加载收集器的 `host.html`（内部 `ball.js`）。复制 GitHub / GitLab 链接，点球即收藏，跨 App 常驻。
2. **系统分享直达**（ShareReceiver → ResultActivity）：在抖音 / B 站 / 微信 / 浏览器的「分享」菜单里选「收藏到收藏箱」，把链接或整段文本甩给收集器的 `/collect?text=...` 深链，由服务端抽取仓库链接并收藏。把「复制 → 打开网页 → 粘贴」压成「分享 → 选本 App」一次点击。

## 文件
```
android/
  MainActivity.kt        # 悬浮球入口：申请悬浮窗权限后启动 OverlayService
  OverlayService.kt      # 悬浮窗服务：WindowManager + WebView 加载 host.html
  ShareReceiver.kt       # 分享入口：截取 SEND / SEND_MULTIPLE 文本
  ResultActivity.kt      # 分享结果页：WebView 打开 /collect?text=... 显示结果
  AndroidManifest.xml    # 权限 + service + 两个 Activity（含分享 intent-filter）
  res/layout/overlay.xml # 透明 FrameLayout 包裹 WebView
  README.md
```

## 构建前提
- Android SDK（API 23+，overlay 需 23）
- Android Studio

## 运行（悬浮球）
1. 收集器后端在开发机跑（默认 `127.0.0.1:8732`）。
2. `OverlayService.kt` / `ResultActivity.kt` 的 `COLLECTOR_BASE`：
   - 模拟器：`http://10.0.2.2:8732`（10.0.2.2 回环到宿主机）
   - 真机：`http://<电脑局域网IP>:8732`（手机与电脑同一 Wi-Fi）
3. Android Studio 打开本目录 → 运行到设备或模拟器。
4. 首次启动会跳「在其他应用上层显示」授权页，授予后球出现在屏幕上。

## 运行（系统分享）
1. 同一 Wi-Fi 下，把 `COLLECTOR_BASE` 改成电脑局域网 IP。
2. 构建安装后，在任意 App 点「分享」→ 选「收藏到收藏箱」，即完成收藏。

## 关键点
- 悬浮球：`host.html` 同源加载 `ball.js`，POST 自然打到 `COLLECTOR_BASE/collect`，**无需改 ball.js**。
- 分享：`ShareReceiver` 只取文本，`ResultActivity` 打开 `/collect?text=...`，**收藏逻辑全在服务端**（`server.py` 的 `/collect` 深链会抽取链接）。
- `usesCleartextTraffic="true"` + `MIXED_CONTENT_ALWAYS_ALLOW`：允许 WebView 加载 http 收集器（开发期）。
- 球的拖拽、剪贴板、拖入链接等交互沿用 web 版，原生层零改动。

## 进阶（可选）
- 发布期应把收集器部署到 https 公网地址，并把 `COLLECTOR_BASE` 指向它，去掉 cleartext 配置。
- 若要壳内独立后端，可将 `server.py` 逻辑用 Ktor / 原生 HTTP 服务内嵌。
